"""Trusted OpenClaw cron removal and signed stop-attestation adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from attestation_keys import public_key_b64, sign_payload
    from participant_runtime.protocol import Instruction, validate_instruction_id
except ModuleNotFoundError:
    from scripts.attestation_keys import public_key_b64, sign_payload
    from scripts.participant_runtime.protocol import Instruction, validate_instruction_id


class OpenClawStopError(RuntimeError):
    """Raised when cron removal cannot be proven and safely attested."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]
Clock = Callable[[], datetime]
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _run_openclaw(command: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _safe_workspace(workspace: Path) -> Path:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise OpenClawStopError("workspace must be a directory")
    return root


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    target = workspace / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.resolve(strict=True).relative_to(workspace)
    except ValueError as exc:
        raise OpenClawStopError("audit directory resolves outside the workspace") from exc
    return target


def _read_workspace_file(workspace: Path, relative: str) -> Path:
    path = workspace / Path(relative)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise OpenClawStopError("required input file is missing or resolves outside the workspace") from exc
    if not resolved.is_file():
        raise OpenClawStopError("required input path is not a file")
    return resolved


def _load_instruction(workspace: Path, instruction_id: str) -> Instruction:
    try:
        validate_instruction_id(instruction_id)
    except (RuntimeError, ValueError) as exc:
        raise OpenClawStopError("invalid stop instruction id") from exc
    directory = workspace / ".multiagent" / "instructions" / "openclaw"
    try:
        directory = directory.resolve(strict=True)
        directory.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise OpenClawStopError("OpenClaw instruction directory is missing or outside the workspace") from exc
    if not directory.is_dir():
        raise OpenClawStopError("OpenClaw instruction directory is not a directory")
    matches = sorted(directory.glob("*-stop-%s.json" % instruction_id))
    if len(matches) != 1:
        raise OpenClawStopError("expected exactly one matching OpenClaw stop instruction")
    try:
        instruction_path = _read_workspace_file(
            workspace,
            matches[0].relative_to(workspace).as_posix(),
        )
        value = json.loads(instruction_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenClawStopError("OpenClaw stop instruction is unreadable") from exc
    if not isinstance(value, dict):
        raise OpenClawStopError("OpenClaw stop instruction must be an object")
    try:
        instruction = Instruction.from_dict(value)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise OpenClawStopError("OpenClaw stop instruction failed protocol validation") from exc
    if (
        instruction.kind != "stop"
        or instruction.agent_id != "openclaw"
        or instruction.platform_id is None
        or instruction.platform_id.casefold() != "openclaw"
        or instruction.session_id is None
    ):
        raise OpenClawStopError("instruction is not bound to the OpenClaw stop identity")
    return instruction


def _load_discussion_id(workspace: Path) -> str:
    try:
        state_path = _read_workspace_file(workspace, ".multiagent/state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenClawStopError("discussion state is unreadable") from exc
    discussion_id = state.get("discussion_id") if isinstance(state, dict) else None
    if not isinstance(discussion_id, str) or not discussion_id.strip():
        raise OpenClawStopError("state.json has no valid discussion_id")
    return discussion_id


def _job_id_from_record(workspace: Path) -> str:
    try:
        path = _read_workspace_file(workspace, ".multiagent/receipts/openclaw/automation-job.md")
        content = path.read_text(encoding="utf-8")
    except (OSError, OpenClawStopError) as exc:
        raise OpenClawStopError("saved OpenClaw automation-job.md is missing or unreadable") from exc

    found: set[str] = set()
    for line in content.splitlines():
        table_cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(table_cells) >= 2 and re.fullmatch(r"(?:automation\s+)?job\s+id", table_cells[0].strip("* `_"), re.I):
            found.add(table_cells[1].strip("` *_\"'"))
        match = re.search(
            r"(?:automation[_\s-]?job[_\s-]?id|job[_\s-]?id)\s*[:=]\s*([^\s|`]+)",
            line,
            re.I,
        )
        if match:
            found.add(match.group(1).strip("*_\"'"))
    found.discard("")
    if len(found) != 1:
        raise OpenClawStopError("saved automation-job.md must identify exactly one job_id")
    job_id = found.pop()
    if not _JOB_ID_RE.fullmatch(job_id):
        raise OpenClawStopError("saved automation job_id has an invalid format")
    return job_id


def _job_identity(job: dict[str, Any]) -> str:
    values = [job[key] for key in ("id", "jobId", "job_id") if key in job]
    if not values:
        raise OpenClawStopError("cron list JSON contains a job without a recognized id")
    if any(not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value) for value in values):
        raise OpenClawStopError("cron list JSON contains a job with an invalid id")
    identities = set(values)
    if len(identities) != 1:
        raise OpenClawStopError("cron list JSON contains a job with conflicting ids")
    return identities.pop()


def _job_list_sources(value: Any, location: str = "$") -> list[tuple[str, set[str]]]:
    if isinstance(value, list):
        if not all(isinstance(item, dict) for item in value):
            raise OpenClawStopError("cron list JSON contains a non-object job entry")
        return [(location, {_job_identity(item) for item in value})]
    if not isinstance(value, dict):
        raise OpenClawStopError("cron list JSON contains an invalid job-list container")

    sources: list[tuple[str, set[str]]] = []
    for key in ("jobs", "crons", "items"):
        if key not in value:
            continue
        candidate = value[key]
        if not isinstance(candidate, list):
            raise OpenClawStopError("cron list JSON contains a malformed recognized job-list source")
        if not all(isinstance(item, dict) for item in candidate):
            raise OpenClawStopError("cron list JSON contains a non-object job entry")
        sources.append(("%s.%s" % (location, key), {_job_identity(item) for item in candidate}))

    for key in ("data", "result", "cron"):
        if key not in value:
            continue
        nested_value = value[key]
        if not isinstance(nested_value, (dict, list)):
            raise OpenClawStopError("cron list JSON contains a malformed recognized wrapper")
        nested_sources = _job_list_sources(nested_value, "%s.%s" % (location, key))
        if not nested_sources:
            raise OpenClawStopError("cron list JSON contains an unrecognized wrapper shape")
        sources.extend(nested_sources)

    return sources


def _extract_job_identities(value: Any) -> set[str]:
    sources = _job_list_sources(value)
    if not sources:
        raise OpenClawStopError("cron list JSON has an unrecognized job-list shape")
    expected = sources[0][1]
    if any(identities != expected for _, identities in sources[1:]):
        raise OpenClawStopError("cron list JSON contains inconsistent recognized job-list sources")
    return expected


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("cron list JSON contains a duplicate object key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("cron list JSON contains a non-standard constant")


def _verify_absent(stdout: str, automation_job_id: str) -> dict[str, Any]:
    try:
        value = json.loads(
            stdout,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise OpenClawStopError("openclaw cron list --json did not return valid JSON") from exc
    identities = _extract_job_identities(value)
    if automation_job_id not in identities:
        return {
            "target_job_id": automation_job_id,
            "target_job_found": False,
            "verification_result": "absent",
            "matching_job_count": 0,
        }
    raise OpenClawStopError("target cron job still exists after removal")


def _timestamp(clock: Clock | None) -> str:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise OpenClawStopError("clock must return a timezone-aware datetime")
    return value.isoformat()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write_new_file(path: Path, content: bytes) -> None:
    if path.exists():
        raise OpenClawStopError("refusing to overwrite existing OpenClaw stop evidence")
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def stop_openclaw_automation(
    *,
    workspace: Path,
    instruction_id: str,
    private_key_path: Path,
    automation_job_id: str | None = None,
    key_id: str | None = None,
    runner: Runner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Remove and natively verify one saved project cron job, then attest it.

    The private key is read only from an explicit path outside the workspace.
    A signature is never produced unless both the removal command succeeds and
    the native JSON listing proves the target job is completely absent.
    """
    root = _safe_workspace(workspace)
    instruction = _load_instruction(root, instruction_id)
    discussion_id = _load_discussion_id(root)
    if instruction.discussion_id != discussion_id:
        raise OpenClawStopError("stop instruction discussion_id differs from workspace state")
    saved_job_id = _job_id_from_record(root)
    if automation_job_id is not None and automation_job_id != saved_job_id:
        raise OpenClawStopError("requested automation_job_id does not match the saved project job")
    target_job_id = saved_job_id
    verifier = instruction.access_scope.get("attestation_verifier")
    if not isinstance(verifier, dict):
        raise OpenClawStopError("stop instruction has no pinned attestation verifier")
    pinned_key_id = verifier.get("key_id")
    pinned_public_key = verifier.get("public_key_b64")
    if not isinstance(pinned_key_id, str) or not pinned_key_id.strip() or not isinstance(pinned_public_key, str):
        raise OpenClawStopError("stop instruction has an invalid attestation verifier")
    if key_id is not None and key_id != pinned_key_id:
        raise OpenClawStopError("requested key_id differs from the stop instruction pin")
    key_path = Path(private_key_path).expanduser()
    if not key_path.is_absolute():
        raise OpenClawStopError("private key path must be absolute")
    try:
        key_path = key_path.resolve(strict=True)
        key_path.relative_to(root)
    except ValueError:
        pass
    except OSError as exc:
        raise OpenClawStopError("external private key path is unavailable") from exc
    else:
        raise OpenClawStopError("private key must be stored outside the workspace")
    try:
        if public_key_b64(key_path) != pinned_public_key:
            raise OpenClawStopError("external private key does not match the stop instruction verifier pin")
    except (OSError, ValueError) as exc:
        if isinstance(exc, OpenClawStopError):
            raise
        raise OpenClawStopError("external private key is not a usable Ed25519 key") from exc

    directory = ".multiagent/audit/platform-evidence/openclaw"
    proof_reference = "%s/%s-removal-proof.json" % (directory, instruction.instruction_id)
    proof_path = _safe_workspace_path(root, proof_reference)
    attestation_path = _safe_workspace_path(
        root,
        "%s/%s-stop.json" % (directory, instruction.instruction_id),
    )
    if proof_path.exists() or attestation_path.exists():
        raise OpenClawStopError("stop evidence already exists for this instruction")

    removal_command = ["openclaw", "cron", "rm", target_job_id]
    verification_command = ["openclaw", "cron", "list", "--json"]
    execute = runner or _run_openclaw
    try:
        removal = execute(removal_command)
        verification = execute(verification_command)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise OpenClawStopError("OpenClaw cron removal or verification command failed to run") from exc
    if not isinstance(removal.returncode, int) or not isinstance(verification.returncode, int):
        raise OpenClawStopError("OpenClaw command runner returned invalid exit codes")
    if removal.returncode != 0:
        raise OpenClawStopError("openclaw cron rm returned a non-zero exit code")
    if verification.returncode != 0:
        raise OpenClawStopError("openclaw cron list --json returned a non-zero exit code")
    verification_details = _verify_absent(verification.stdout, target_job_id)
    removal_checked_at = _timestamp(clock)
    stopped_at = removal_checked_at
    mechanism = "openclaw_cron_removal"
    target = "openclaw cron job %s" % target_job_id
    proof = {
        "discussion_id": discussion_id,
        "instruction_id": instruction.instruction_id,
        "instruction_sha256": instruction.sha256,
        "agent_id": instruction.agent_id,
        "platform_id": instruction.platform_id,
        "session_id": instruction.session_id,
        "stopped_at": stopped_at,
        "mechanism": mechanism,
        "target": target,
        "automation_job_id": target_job_id,
        "removal_command": removal_command,
        "removal_exit_code": removal.returncode,
        "verification_command": verification_command,
        "verification_exit_code": verification.returncode,
        "action_verified": True,
        "removal_verified": True,
        "removal_checked_at": removal_checked_at,
        "proof_details": verification_details,
    }
    proof_bytes = _json_bytes(proof)
    proof_sha256 = hashlib.sha256(proof_bytes).hexdigest()
    attestation = {
        "discussion_id": discussion_id,
        "agent_id": instruction.agent_id,
        "instruction_id": instruction.instruction_id,
        "instruction_sha256": instruction.sha256,
        "platform_id": instruction.platform_id,
        "session_id": instruction.session_id,
        "stopped_at": stopped_at,
        "mechanism": mechanism,
        "target": target,
        "action_verified": True,
        "proof_reference": proof_reference,
        "proof_sha256": proof_sha256,
        "automation_job_id": target_job_id,
        "removal_verified": True,
        "removal_checked_at": removal_checked_at,
    }
    signed_attestation = sign_payload(attestation, private_key_path=key_path, key_id=pinned_key_id)
    _write_new_file(proof_path, proof_bytes)
    _write_new_file(attestation_path, _json_bytes(signed_attestation))
    return {
        "status": "completed",
        "proof_reference": proof_reference,
        "proof_sha256": proof_sha256,
        "attestation_path": attestation_path.relative_to(root).as_posix(),
        "automation_job_id": target_job_id,
        "removal_checked_at": removal_checked_at,
    }
