#!/usr/bin/env python3
"""Coordinator-side, single-pass discussion orchestration.

This module is the actuator.  It consumes durable receipts, creates immutable
instructions, records wake attempts, and advances the machine state.  It never
pretends that observing a file can itself wake an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.common.wake_protocol import WakeAdapter, WakeRequest, WakeResult
from workflow_core import (
    E_OUTPUT_FORMAT, E_PLATFORM_UNAVAILABLE, E_RETRY_EXHAUSTED, E_SCHEMA,
    StateLock, WorkflowError, atomic_write_json, atomic_write_text,
    ensure_content_consistency, load_json, load_state, main_markdown_path,
    path_in_workspace, sha256_file, update_content_hash,
    validate_coordinator_execution, validate_state_shape,
)
from participant_runtime.protocol import (
    Instruction, Receipt, instruction_path, isolation_evidence_path, load_isolation_evidence,
    load_stop_attestation,
    receipt_path, validate_input_manifest, write_instruction,
)
from instruction_prompts import build_task_prompt
from participant_views import access_scope as sealed_access_scope, output_path as sealed_output_path, publish_inputs
import export_docx
from openclaw_automation import render_openclaw_operational_directive


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class FakeWakeAdapter:
    """A deterministic test adapter: accepts delivery without claiming execution."""
    def __init__(self) -> None:
        self.requests: list[WakeRequest] = []

    def wake(self, request: WakeRequest) -> WakeResult:
        self.requests.append(request)
        return WakeResult("accepted")


class UnavailableWakeAdapter:
    def wake(self, request: WakeRequest) -> WakeResult:
        return WakeResult("unavailable", detail="No configured platform wake endpoint")


@dataclass
class OrchestrationResult:
    blocking_error_codes: list[str] = field(default_factory=list)
    issued_instruction_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stage: str = "unknown"


@dataclass(frozen=True)
class Observation:
    workspace: str
    revision: int | None
    stage: str | None
    files: tuple[str, ...]


def monitor_once(workspace: Path) -> Observation:
    """Read-only sensor snapshot; no state write and no platform action."""
    workspace = Path(workspace).resolve()
    try:
        state = load_json(_state_path(workspace))
    except Exception:
        state = {}
    files = tuple(sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file() and ".state.lock" not in path.parts))
    return Observation(str(workspace), state.get("revision"), state.get("stage"), files)


def _state_path(workspace: Path) -> Path:
    return workspace / ".multiagent" / "state.json"


def _instruction_root(workspace: Path) -> Path:
    return workspace / ".multiagent" / "instructions"


def _receipt_root(workspace: Path) -> Path:
    return workspace / ".multiagent" / "receipts"


def _read_receipts(workspace: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = _receipt_root(workspace)
    if not root.is_dir():
        return records
    for path in sorted(root.rglob("*.json")):
        try:
            value = load_json(path)
        except Exception:
            continue
        if isinstance(value, dict):
            value = dict(value)
            value["_path"] = path
            records.append(value)
    return records


def _has_completed(receipts: list[dict[str, Any]], agent_id: str, kind: str) -> bool:
    return any(record.get("agent_id") == agent_id and record.get("status") == "completed" and record.get("kind") == kind for record in receipts)


def _has_bootstrapped(receipts: list[dict[str, Any]], agent_id: str) -> bool:
    """Bootstrap acceptance proves the participant validated the Runtime and is online."""
    return any(record.get("agent_id") == agent_id and record.get("kind") == "bootstrap" and record.get("status") in {"accepted", "completed"} for record in receipts)


def _instruction_exists(workspace: Path, agent_id: str, kind: str) -> bool:
    directory = _instruction_root(workspace) / agent_id
    return directory.is_dir() and any(directory.glob("*-%s-*.json" % kind))


def _terminal_instruction_ids(receipts: list[dict[str, Any]], agent_id: str) -> set[str]:
    terminal = {
        str(record.get("instruction_id"))
        for record in receipts
        if record.get("agent_id") == agent_id and record.get("status") in {"completed", "failed"}
    }
    terminal.update(
        str(record.get("instruction_id"))
        for record in receipts
        if record.get("agent_id") == agent_id
        and record.get("kind") == "bootstrap"
        and record.get("status") == "accepted"
    )
    return terminal


def _project_machine_indexes(
    state: dict[str, Any],
    workspace: Path,
    receipts: list[dict[str, Any]],
    participants: list[str],
) -> bool:
    """Project durable instructions/receipts and contribution completion into state."""
    changed = False
    submission = state.setdefault("submission_status", {agent: "pending" for agent in participants})
    responses = state.setdefault("response_status", {agent: "pending" for agent in participants})
    queue: dict[str, list[str]] = {}
    receipt_index: dict[str, list[str]] = {}

    for agent in participants:
        if _valid_completed_outputs(workspace, state, receipts, [agent], {"propose", "repair"}, "proposals"):
            if submission.get(agent) != "submitted":
                submission[agent] = "submitted"
                changed = True
        if _valid_completed_outputs(workspace, state, receipts, [agent], {"respond"}, "responses"):
            if responses.get(agent) != "submitted":
                responses[agent] = "submitted"
                changed = True

        terminal = _terminal_instruction_ids(receipts, agent)
        pending: list[str] = []
        instruction_dir = _instruction_root(workspace) / agent
        if instruction_dir.is_dir():
            for path in sorted(instruction_dir.glob("*.json")):
                try:
                    instruction = Instruction.from_dict(load_json(path))
                except Exception:
                    continue
                if instruction.instruction_id not in terminal:
                    pending.append(instruction.instruction_id)
        queue[agent] = pending
        receipt_index[agent] = sorted(
            str(record["_path"].relative_to(workspace).as_posix())
            for record in receipts
            if record.get("agent_id") == agent and isinstance(record.get("_path"), Path)
        )

    if state.get("instruction_queue") != queue:
        state["instruction_queue"] = queue
        changed = True
    if state.get("receipt_index") != receipt_index:
        state["receipt_index"] = receipt_index
        changed = True
    return changed


def _has_open_instruction(workspace: Path, agent_id: str, kind: str) -> bool:
    """True only while an instruction of that kind has no terminal receipt."""
    directory = _instruction_root(workspace) / agent_id
    if not directory.is_dir():
        return False
    for path in directory.glob("*-%s-*.json" % kind):
        try:
            instruction = Instruction.from_dict(load_json(path))
        except Exception:
            continue
        receipt_root = _receipt_root(workspace) / agent_id
        if not any((receipt_root / (instruction.instruction_id + "-" + status + ".json")).exists() for status in ("completed", "failed")):
            return True
    return False


def _new_instruction(
    state: dict[str, Any],
    workspace: Path,
    agent_id: str,
    kind: str,
    *,
    attempt: int = 1,
    output_path: str | None = None,
    origin_instruction_id: str | None = None,
    root_instruction_id: str | None = None,
    failure_receipt: dict[str, Any] | None = None,
) -> Instruction:
    counter = int(state.get("instruction_sequence", 0)) + 1
    state["instruction_sequence"] = counter
    state_revision = int(state.get("revision", 0)) + 1
    input_sources: list[tuple[Path, str]] = []
    context = workspace / "project-context.md"
    if context.is_file() and kind in {"bootstrap", "propose", "respond", "repair"}:
        input_sources.append((context, "project-context.md"))
    if kind == "bootstrap":
        runtime_version = str((state.get("runtime_distribution") or {}).get("version", "1.0.0"))
        manifest = workspace / ".multiagent" / "runtime" / "participant" / runtime_version / "manifest.json"
        if manifest.is_file():
            input_sources.append((manifest, "runtime-manifest.json"))
    if kind == "respond":
        discussion = _main_markdown(workspace)
        if discussion is not None:
            input_sources.append((discussion, "discussion.md"))
    if kind == "repair":
        if not isinstance(failure_receipt, dict) or not isinstance(failure_receipt.get("_path"), Path):
            raise WorkflowError("repair requires the participant's own failed receipt", E_SCHEMA, agent_id=agent_id)
        input_sources.append((failure_receipt["_path"], "failure-receipt.json"))
        original = _find_instruction(workspace, agent_id, str(failure_receipt.get("instruction_id", "")))
        original_path = path_in_workspace(workspace, original.output_path) if original is not None else None
        if original_path is not None and original_path.is_file():
            input_sources.append((original_path, "original-output.md"))
        else:
            missing_path = path_in_workspace(
                workspace,
                ".multiagent/receipts/%s/repair-source-%s.json" % (agent_id, failure_receipt.get("instruction_id", "unknown")),
            )
            atomic_write_json(missing_path, {
                "source_instruction_id": failure_receipt.get("instruction_id"),
                "expected_output_path": original.output_path if original is not None else None,
                "status": "original_output_missing",
            })
            input_sources.append((missing_path, "original-output-status.json"))
    binding = _participant_binding(state, agent_id)
    # Even instructions with no source files (notably stop) need an explicit
    # empty immutable manifest so the Runtime can verify the exact artifact scope.
    inputs = publish_inputs(workspace, agent_id, input_sources, instruction_kind=kind)
    if output_path is None:
        output_path = sealed_output_path(workspace, agent_id, kind)
    scope = sealed_access_scope(workspace, agent_id)
    payload: dict[str, Any] = {
        "instruction_id": "I-%06d" % counter,
        "discussion_id": state["discussion_id"],
        "sequence": counter,
        "kind": kind,
        "agent_id": agent_id,
        "runtime_version": str((state.get("runtime_distribution") or {}).get("version", "1.0.0")),
        "state_revision": state_revision,
        "input_paths": inputs,
        "output_path": output_path,
        "task_prompt": build_task_prompt(
            kind,
            agent_id,
            str(state.get("coordinator", "unknown")),
            inputs,
            output_path,
        ) + (
            "\n\n执行身份绑定：participant agent_id=%s；platform_id=%s；session_id=%s。"
            "协调者 agent_id=%s / platform_id=%s / session_id=%s。三种身份字段必须分别核对，不得按名称推断。"
            % (
                agent_id, binding["platform_id"], binding["session_id"],
                state["coordinator"], state["coordinator_binding"]["platform_id"],
                state["coordinator_binding"]["session_id"],
            )
        ),
        "access_scope": scope,
        "platform_id": binding["platform_id"],
        "session_id": binding["session_id"],
        "attempt": attempt,
        "max_attempts": int((state.get("retry_policy") or {}).get("max_attempts", 3)),
        "issued_at": _now(),
    }
    if origin_instruction_id is not None:
        payload["origin_instruction_id"] = origin_instruction_id
    if root_instruction_id is not None:
        payload["root_instruction_id"] = root_instruction_id
    if agent_id == "openclaw":
        payload["operational_directive"] = render_openclaw_operational_directive(
            workspace,
            str(state.get("discussion_id", workspace.name)),
            str(state.get("coordinator", "unknown")),
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["sha256"] = hashlib.sha256(canonical).hexdigest()
    return Instruction.from_dict(payload)


def _participant_binding(state: dict[str, Any], agent_id: str) -> dict[str, str]:
    bindings = state.get("participant_bindings")
    binding = bindings.get(agent_id) if isinstance(bindings, dict) else None
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("platform_id"), str)
        or not binding["platform_id"].strip()
        or not isinstance(binding.get("session_id"), str)
        or not binding["session_id"].strip()
    ):
        raise WorkflowError("participant platform/session binding 缺失或非法", "E_PARTICIPANT_BINDING", agent_id=agent_id)
    return {"platform_id": binding["platform_id"], "session_id": binding["session_id"]}


def _record_wake(workspace: Path, request: WakeRequest, result: WakeResult) -> None:
    path = path_in_workspace(workspace, ".multiagent/audit/wake-events.json")
    store = _load_wake_event_store(path)
    entries = store["events"]
    entry = {
        "at": _now(),
        "agent_id": request.agent_id,
        "instruction_id": request.instruction_id,
        "runtime_version": request.runtime_version,
        "platform_id": request.platform_id,
        "session_id": request.session_id,
        "status": result.status,
        "detail": result.detail,
        "evidence": result.evidence,
    }
    if not any(
        isinstance(existing, dict)
        and existing.get("agent_id") == request.agent_id
        and existing.get("instruction_id") == request.instruction_id
        for existing in entries
    ):
        entries.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, store)


def _load_wake_event_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "events": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"schema_version": "1.0", "events": payload}
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return {
            "schema_version": str(payload.get("schema_version", "1.0")),
            "events": list(payload["events"]),
        }
    raise ValueError(".multiagent/audit/wake-events.json must be an object containing events[]")


def _wake_existing(workspace: Path, state: dict[str, Any], wake: WakeAdapter, result: OrchestrationResult, agent_id: str, instruction: Instruction) -> None:
    """Deliver an initialization-published instruction once, without rewriting it."""
    audit = workspace / ".multiagent" / "audit" / "wake-events.json"
    events = _load_wake_event_store(audit)["events"]
    if any(isinstance(event, dict) and event.get("instruction_id") == instruction.instruction_id for event in events):
        return
    binding = _participant_binding(state, agent_id)
    if instruction.platform_id != binding["platform_id"] or instruction.session_id != binding["session_id"]:
        raise WorkflowError("published instruction target differs from participant binding", "E_PARTICIPANT_BINDING", agent_id=agent_id)
    request = WakeRequest(workspace, agent_id, instruction.instruction_id, instruction.runtime_version,
                          binding["platform_id"], binding["session_id"])
    wake_result = wake.wake(request)
    _record_wake(workspace, request, wake_result)
    if wake_result.status != "accepted" and E_PLATFORM_UNAVAILABLE not in result.blocking_error_codes:
        result.blocking_error_codes.append(E_PLATFORM_UNAVAILABLE)


def _issue(
    workspace: Path,
    state: dict[str, Any],
    wake: WakeAdapter,
    result: OrchestrationResult,
    agent_id: str,
    kind: str,
    *,
    attempt: int = 1,
    origin_instruction_id: str | None = None,
    root_instruction_id: str | None = None,
    failure_receipt: dict[str, Any] | None = None,
) -> None:
    instruction = _new_instruction(
        state,
        workspace,
        agent_id,
        kind,
        attempt=attempt,
        origin_instruction_id=origin_instruction_id,
        root_instruction_id=root_instruction_id,
        failure_receipt=failure_receipt,
    )
    write_instruction(workspace, instruction)
    result.issued_instruction_ids.append(instruction.instruction_id)
    binding = _participant_binding(state, agent_id)
    request = WakeRequest(workspace, agent_id, instruction.instruction_id, instruction.runtime_version,
                          binding["platform_id"], binding["session_id"])
    wake_result = wake.wake(request)
    _record_wake(workspace, request, wake_result)
    if wake_result.status != "accepted" and E_PLATFORM_UNAVAILABLE not in result.blocking_error_codes:
        result.blocking_error_codes.append(E_PLATFORM_UNAVAILABLE)


def _find_instruction(workspace: Path, agent_id: str, instruction_id: str) -> Instruction | None:
    directory = _instruction_root(workspace) / agent_id
    if not directory.is_dir():
        return None
    for path in directory.glob("*.json"):
        try:
            instruction = Instruction.from_dict(load_json(path))
        except Exception:
            continue
        if instruction.instruction_id == instruction_id:
            return instruction
    return None


def _failure_key(workspace: Path, receipt: dict[str, Any]) -> str:
    """Stable identity for one immutable terminal failure receipt."""
    path = receipt.get("_path")
    if isinstance(path, Path):
        return path.relative_to(workspace).as_posix()
    return "%s:%s:failed" % (receipt.get("agent_id", "?"), receipt.get("instruction_id", "?"))


def _failure_chain(receipt: dict[str, Any], source: Instruction | None) -> tuple[str, str]:
    origin = str(receipt.get("instruction_id") or "unknown")
    root = receipt.get("root_instruction_id")
    if not isinstance(root, str) or not root:
        root = source.root_instruction_id if source is not None else None
    if not root:
        root = source.origin_instruction_id if source is not None else None
    if not root:
        root = origin
    return origin, root


def _automation_audit(state: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    automation = state.setdefault("automation", {})
    if not isinstance(automation, dict):
        automation = {}
        state["automation"] = automation
    processed = automation.setdefault("processed_failure_receipts", [])
    blockers = automation.setdefault("blockers", [])
    if not isinstance(processed, list):
        processed = []
        automation["processed_failure_receipts"] = processed
    if not isinstance(blockers, list):
        blockers = []
        automation["blockers"] = blockers
    return automation, processed, blockers


def _main_markdown(workspace: Path) -> Path | None:
    docs = sorted(workspace.glob("*讨论文档_*.md"))
    return docs[0] if docs else None


def _append_section(path: Path, title: str, sections: list[tuple[str, str]]) -> None:
    """Idempotently append material inside one existing canonical Markdown section."""
    if not path or not path.is_file():
        raise WorkflowError("主讨论 Markdown 不存在", E_SCHEMA, path=str(path))
    content = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^" + re.escape(title) + r"\s*$", content))
    if len(matches) != 1:
        raise WorkflowError("权威讨论文档必须恰有一个规范章节标题", E_SCHEMA, heading=title, matches=len(matches))
    heading = matches[0]
    body_start = heading.end()
    next_heading = re.search(r"(?m)^## ", content[body_start:])
    body_end = body_start + next_heading.start() if next_heading else len(content)
    body = content[body_start:body_end]
    body = re.sub(r"(?m)^<[^\n]*>\s*$", "", body).rstrip()
    additions: list[str] = []
    for name, text in sections:
        if re.search(r"(?m)^### " + re.escape(name) + r"\s*$", body):
            continue
        additions.append("### %s\n\n%s" % (name, text.strip()))
    if not additions:
        return
    replacement = content[:body_start] + "\n\n" + "\n\n".join(filter(None, (body, *additions))) + "\n\n" + content[body_end:].lstrip("\n")
    atomic_write_text(path, replacement)


def _completed_output(receipts: list[dict[str, Any]], agent_id: str, kinds: set[str]) -> dict[str, Any] | None:
    candidates = [record for record in receipts if record.get("agent_id") == agent_id and record.get("status") == "completed" and record.get("kind") in kinds]
    return candidates[-1] if candidates else None


def _participant_output_path(workspace: Path, agent_id: str, folder: str) -> Path:
    name = "%s-%s" % (agent_id, "提案文档.md" if folder == "proposals" else "交叉回应文档.md")
    return workspace / ".multiagent" / "views" / agent_id / "outputs" / name.replace(agent_id + "-", "", 1)


def _valid_completed_outputs(workspace: Path, state: dict[str, Any], receipts: list[dict[str, Any]], participants: list[str], kinds: set[str], folder: str) -> bool:
    for agent in participants:
        receipt = _completed_output(receipts, agent, kinds)
        if receipt is None:
            return False
        relative = receipt.get("output_path")
        if not isinstance(relative, str) or not relative:
            return False
        try:
            expected = path_in_workspace(workspace, relative)
        except Exception:
            return False
        if expected.resolve() != _participant_output_path(workspace, agent, folder).resolve():
            return False
        if not expected.is_file() or not expected.read_text(encoding="utf-8").strip():
            return False
        output_hash = receipt.get("output_sha256")
        if not isinstance(output_hash, str) or output_hash != sha256_file(expected):
            return False
        instruction_id = receipt.get("instruction_id")
        instruction = _find_instruction(workspace, agent, str(instruction_id)) if instruction_id else None
        if instruction is None or instruction.kind not in kinds:
            return False
        try:
            binding = _participant_binding(state, agent)
            if instruction.platform_id != binding["platform_id"] or instruction.session_id != binding["session_id"]:
                return False
            validate_input_manifest(workspace, instruction)
        except Exception:
            return False
        if instruction.kind in {"propose", "repair"}:
            try:
                evidence = load_isolation_evidence(workspace, instruction)
            except Exception:
                return False
            if receipt.get("isolation_evidence") != evidence:
                return False
    return True


def _stop_instructions(workspace: Path, participants: list[str]) -> dict[str, Instruction]:
    """Load the unique immutable stop instruction for each participant."""
    if not participants or len(participants) != len(set(participants)):
        return {}
    found: dict[str, Instruction] = {}
    for agent in participants:
        directory = _instruction_root(workspace) / agent
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
        stops: list[tuple[Path, Instruction]] = []
        for path in paths:
            try:
                value = load_json(path)
                if not isinstance(value, dict):
                    return {}
                if value.get("kind") == "stop":
                    instruction = Instruction.from_dict(value)
                    stops.append((path, instruction))
            except Exception:
                # A malformed instruction file cannot be used to establish uniqueness.
                return {}
        if len(stops) != 1:
            return {}
        path, instruction = stops[0]
        if (
            instruction.agent_id != agent
            or instruction.kind != "stop"
            or path.resolve() != instruction_path(workspace, agent, instruction).resolve()
        ):
            return {}
        found[agent] = instruction
    return found


def _all_stop_receipts_completed(
    workspace: Path,
    state: dict[str, Any],
    receipts: list[dict[str, Any]],
    participants: list[str],
) -> bool:
    """Require one authentic, instruction-bound stop receipt per participant."""
    instructions = _stop_instructions(workspace, participants)
    if not participants or len(instructions) != len(participants):
        return False

    listed_receipt_paths = {
        record.get("_path").resolve()
        for record in receipts
        if isinstance(record.get("_path"), Path)
    }
    # _read_receipts intentionally skips unreadable JSON. Do not let an unreadable
    # extra receipt hide from the uniqueness check.
    for agent in participants:
        receipt_dir = _receipt_root(workspace) / agent
        if receipt_dir.is_dir() and any(
            path.resolve() not in listed_receipt_paths
            for path in receipt_dir.rglob("*.json")
        ):
            return False

    for agent in participants:
        instruction = instructions[agent]
        try:
            binding = _participant_binding(state, agent)
        except Exception:
            return False
        if (
            instruction.discussion_id != state.get("discussion_id")
            or instruction.platform_id != binding["platform_id"]
            or instruction.session_id != binding["session_id"]
            or instruction.output_path != ".multiagent/receipts/%s" % agent
            or instruction.state_revision > int(state.get("revision", -1))
        ):
            return False
        matches = [
            record for record in receipts
            if record.get("agent_id") == agent
            and record.get("kind") == "stop"
            and record.get("status") == "completed"
        ]
        if len(matches) != 1:
            return False
        receipt = matches[0]
        if receipt.get("instruction_id") != instruction.instruction_id:
            return False
        expected_receipt_path = receipt_path(
            workspace, agent, instruction.instruction_id, "completed",
        )
        if (
            not isinstance(receipt.get("_path"), Path)
            or receipt["_path"].resolve() != expected_receipt_path.resolve()
            or receipt.get("runtime_version") != instruction.runtime_version
            or receipt.get("state_revision") != instruction.state_revision
            or receipt.get("attempt") != instruction.attempt
        ):
            return False
        try:
            # Re-verify the receipt signature against the external trust registry;
            # a runner-authored marker alone is never stop evidence.
            Receipt.from_dict(receipt)
        except Exception:
            return False
        marker_path = ".multiagent/receipts/%s/%s-stop-marker.txt" % (agent, instruction.instruction_id)
        if receipt.get("output_path") != marker_path:
            return False
        try:
            marker = path_in_workspace(workspace, marker_path)
            if not marker.is_file() or receipt.get("output_sha256") != sha256_file(marker):
                return False
            manifest = validate_input_manifest(workspace, instruction)
            stop_evidence = load_stop_attestation(workspace, instruction)
        except Exception:
            return False
        evidence = receipt.get("isolation_evidence")
        if not isinstance(evidence, dict) or evidence != stop_evidence:
            return False
    return True


def _ensure_stop_instructions(
    workspace: Path,
    state: dict[str, Any],
    wake: WakeAdapter,
    result: OrchestrationResult,
    participants: list[str],
) -> bool:
    """Publish and wake one stop instruction per participant, idempotently."""
    changed = False
    monitoring = dict(state.get("monitoring") or {})
    if monitoring.get("status") != "stopping" or monitoring.get("enabled") is not True:
        monitoring.update({
            "enabled": True,
            "status": "stopping",
            "stop_requested_at": monitoring.get("stop_requested_at") or _now(),
        })
        state["monitoring"] = monitoring
        changed = True
    automation = dict(state.get("automation") or {})
    if not automation.get("stop_requested"):
        automation.update({
            "enabled": True,
            "stop_requested": True,
            "stop_requested_at": monitoring["stop_requested_at"],
            "stop_requested_by": state["coordinator"],
        })
        state["automation"] = automation
        changed = True
    instruction_ids = dict(monitoring.get("stop_instruction_ids") or {})
    for agent in participants:
        existing = _stop_instructions(workspace, [agent])
        if existing:
            instruction = existing[agent]
            instruction_ids[agent] = instruction.instruction_id
            _wake_existing(workspace, state, wake, result, agent, instruction)
        elif _instruction_exists(workspace, agent, "stop"):
            if "E_STOP_INSTRUCTION" not in result.blocking_error_codes:
                result.blocking_error_codes.append("E_STOP_INSTRUCTION")
        else:
            before = len(result.issued_instruction_ids)
            _issue(workspace, state, wake, result, agent, "stop")
            instruction_ids[agent] = result.issued_instruction_ids[-1] if len(result.issued_instruction_ids) > before else ""
            changed = True
    if instruction_ids and monitoring.get("stop_instruction_ids") != instruction_ids:
        monitoring["stop_instruction_ids"] = instruction_ids
        state["monitoring"] = monitoring
        changed = True
    return changed


def _merge_and_dispose(workspace: Path, state: dict[str, Any], participants: list[str]) -> None:
    document = _main_markdown(workspace)
    sections: list[tuple[str, str]] = []
    manifest: list[dict[str, Any]] = []
    for agent in participants:
        path = _participant_output_path(workspace, agent, "proposals")
        text = path.read_text(encoding="utf-8")
        sections.append((agent, "> 来源 SHA-256：`%s`\n\n%s" % (sha256_file(path), text)))
        manifest.append({"path": path.relative_to(workspace).as_posix(), "sha256": sha256_file(path), "merged_revision": int(state.get("revision", 0)) + 1, "disposed_at": _now()})
    _append_section(document, "## 二、独立提案", sections)
    submission = state.setdefault("submission_status", {})
    for agent in participants:
        submission[agent] = "submitted"
    disposition = state.get("proposal_disposition", "archive")
    manifest_path = workspace / ".multiagent" / "archive" / "proposals" / "manifest.json"
    archive = manifest_path.parent
    archive.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if manifest_path.is_file():
        previous = load_json(manifest_path)
        existing = list(previous.get("entries", [])) if isinstance(previous.get("entries"), list) else []
    atomic_write_json(manifest_path, {
        "discussion_id": state.get("discussion_id"),
        "disposition": disposition,
        "entries": existing + manifest,
    })
    if disposition == "delete":
        for item in manifest:
            path_in_workspace(workspace, item["path"]).unlink(missing_ok=True)


def _build_candidate(workspace: Path, state: dict[str, Any], participants: list[str]) -> None:
    document = _main_markdown(workspace)
    sections: list[tuple[str, str]] = []
    for agent in participants:
        path = _participant_output_path(workspace, agent, "responses")
        sections.append((agent, path.read_text(encoding="utf-8")))
    _append_section(document, "## 三、交叉回应", [
        (agent, "> 来源 SHA-256：`%s`\n\n%s" % (sha256_file(_participant_output_path(workspace, agent, "responses")), text))
        for agent, text in sections
    ])
    _append_section(document, "## 四、结构化决策包", [
        ("候选综合", "以下内容综合独立提案与交叉回应，仅形成候选供 Rainier 审阅；不构成已确认决策。请检查共识、分歧、风险和待选择事项。")
    ])
    _append_section(document, "## 五、候选决策与 Word 审阅", [
        ("候选审阅", "状态：候选、待 Rainier 审阅，不是正式决策。\n\n- 候选 ID：C-0001\n- 候选 Markdown：`.multiagent/deliverables/candidate.md`\n- 候选 Word：`候选决策.docx`\n- 系统打开成功后，自动停止常规监测；只有 Rainier 明确确认才进入正式交付。")
    ])
    if not state.get("candidate_decision_ids"):
        state["candidate_decision_ids"] = ["C-0001"]


def orchestrate_once(
    workspace: Path,
    wake: WakeAdapter | None = None,
    open_candidate: bool = True,
    *,
    actor: str | None = None,
    platform_id: str | None = None,
    session_id: str | None = None,
    opener=None,
) -> OrchestrationResult:
    """Advance exactly the deterministic work permitted by durable evidence."""
    workspace = Path(workspace).resolve()
    wake = wake or UnavailableWakeAdapter()
    with StateLock(workspace):
        state_path = _state_path(workspace)
        state = load_state(workspace)
        validate_state_shape(state)
        validate_coordinator_execution(state, actor or "", platform_id or "", session_id or "")
        ensure_content_consistency(workspace, state)
        if state.get("stage") not in {
            "initialized", "independent_proposal", "cross_response", "candidate_decision",
            "user_confirmation", "confirmed_decision", "delivered", "monitoring_stopped",
        }:
            raise WorkflowError("旧阶段不能由新编排器推进", "E_PHASE", stage=state.get("stage"))
        result = OrchestrationResult(stage=str(state.get("stage", "unknown")))
        participants = list(state.get("expected_participants") or [])
        for participant in participants:
            _participant_binding(state, participant)
        receipts = _read_receipts(workspace)
        changed = False
        _, processed_failures, blockers = _automation_audit(state)
        for blocker in blockers:
            if isinstance(blocker, dict) and blocker.get("code") == E_RETRY_EXHAUSTED and E_RETRY_EXHAUSTED not in result.blocking_error_codes:
                result.blocking_error_codes.append(E_RETRY_EXHAUSTED)

        # Participant-correctable failures get exactly one repair instruction.
        for receipt in receipts:
            code = receipt.get("error_code")
            if receipt.get("status") != "failed" or code not in {E_SCHEMA, E_OUTPUT_FORMAT} or not receipt.get("recoverable"):
                continue
            agent_id = receipt.get("agent_id")
            failure_key = _failure_key(workspace, receipt)
            if failure_key in processed_failures:
                continue
            source = _find_instruction(workspace, str(agent_id), str(receipt.get("instruction_id", ""))) if isinstance(agent_id, str) else None
            attempt = int(receipt.get("attempt", source.attempt if source is not None else 1))
            limit = int((state.get("retry_policy") or {}).get("max_attempts", 3))
            if not isinstance(agent_id, str) or agent_id not in participants:
                continue
            origin_instruction_id, root_instruction_id = _failure_chain(receipt, source)
            if attempt >= limit:
                blocker_key = "%s:%s:%s" % (agent_id, root_instruction_id, E_RETRY_EXHAUSTED)
                if not any(isinstance(item, dict) and item.get("blocker_key") == blocker_key for item in blockers):
                    blockers.append({
                        "blocker_key": blocker_key,
                        "code": E_RETRY_EXHAUSTED,
                        "agent_id": agent_id,
                        "origin_instruction_id": origin_instruction_id,
                        "root_instruction_id": root_instruction_id,
                        "attempt": attempt,
                        "max_attempts": limit,
                        "created_at": _now(),
                    })
                    changed = True
                if E_RETRY_EXHAUSTED not in result.blocking_error_codes:
                    result.blocking_error_codes.append(E_RETRY_EXHAUSTED)
                processed_failures.append(failure_key)
                changed = True
                continue
            if not _has_open_instruction(workspace, agent_id, "repair"):
                _issue(
                    workspace,
                    state,
                    wake,
                    result,
                    agent_id,
                    "repair",
                    attempt=attempt + 1,
                    origin_instruction_id=origin_instruction_id,
                    root_instruction_id=root_instruction_id,
                    failure_receipt=receipt,
                )
                changed = True
            processed_failures.append(failure_key)
            changed = True

        if state.get("stage") == "initialized":
            all_bootstrapped = bool(participants) and all(_has_bootstrapped(receipts, agent) for agent in participants)
            if all_bootstrapped:
                for agent in participants:
                    if not _instruction_exists(workspace, agent, "propose"):
                        _issue(workspace, state, wake, result, agent, "propose")
                        changed = True
                state["stage"] = "independent_proposal"
                changed = True
            else:
                for agent in participants:
                    if _has_bootstrapped(receipts, agent):
                        continue
                    if _instruction_exists(workspace, agent, "bootstrap"):
                        instruction_dir = _instruction_root(workspace) / agent
                        for path in sorted(instruction_dir.glob("*-bootstrap-*.json")):
                            _wake_existing(workspace, state, wake, result, agent, Instruction.from_dict(load_json(path)))
                    else:
                        _issue(workspace, state, wake, result, agent, "bootstrap")
                        changed = True

        if state.get("stage") in {"initialized", "independent_proposal"} and _valid_completed_outputs(workspace, state, receipts, participants, {"propose", "repair"}, "proposals"):
            _merge_and_dispose(workspace, state, participants)
            for agent in participants:
                if not _instruction_exists(workspace, agent, "respond"):
                    _issue(workspace, state, wake, result, agent, "respond")
            state["stage"] = "cross_response"
            changed = True

        if state.get("stage") == "cross_response" and _valid_completed_outputs(workspace, state, receipts, participants, {"respond"}, "responses"):
            _build_candidate(workspace, state, participants)
            state["stage"] = "candidate_decision"
            changed = True

        prior_delivery = state.get("candidate_delivery")
        if state.get("stage") == "candidate_decision" and (not isinstance(prior_delivery, dict) or prior_delivery.get("opened") is not True):
            if not state.get("candidate_decision_ids"):
                state["candidate_decision_ids"] = ["C-0001"]
            document = _main_markdown(workspace)
            try:
                if document is None:
                    raise FileNotFoundError("未找到主讨论 Markdown，无法生成候选交付")
                delivery = export_docx.create_candidate_delivery(
                    workspace,
                    state,
                    document,
                    open_after=open_candidate,
                    opener=opener,
                )
            except Exception as exc:
                warning = "候选 Word 生成失败；Markdown 保持权威，可重跑单次编排恢复：%s" % exc
                result.warnings.append(warning)
                if "E_CANDIDATE_DELIVERY" not in result.blocking_error_codes:
                    result.blocking_error_codes.append("E_CANDIDATE_DELIVERY")
            else:
                delivery["created_at"] = _now()
                state["candidate_delivery"] = delivery
                if delivery.get("opened") is not True:
                    result.warnings.append(str(delivery.get("open_error")))
                changed = True

        # Opening the candidate Word requests shutdown but is not proof that
        # participant monitors stopped. Keep the stage at candidate_decision
        # until every participant returns a binding- and artifact-checked receipt.
        if state.get("stage") == "candidate_decision":
            delivery = state.get("candidate_delivery")
            if isinstance(delivery, dict) and delivery.get("opened") is True:
                if _ensure_stop_instructions(workspace, state, wake, result, participants):
                    changed = True
                receipts = _read_receipts(workspace)
                if _all_stop_receipts_completed(workspace, state, receipts, participants):
                    stopped_at = _now()
                    monitoring = dict(state.get("monitoring") or {})
                    monitoring.update({
                        "enabled": False,
                        "status": "stopped",
                        "stopped_at": stopped_at,
                    })
                    state["monitoring"] = monitoring
                    automation = dict(state.get("automation") or {})
                    automation.update({"enabled": False, "stopped_at": stopped_at})
                    state["automation"] = automation
                    state["stage"] = "user_confirmation"
                    changed = True
                else:
                    missing = [
                        agent for agent in participants
                        if not any(
                            record.get("agent_id") == agent
                            and record.get("kind") == "stop"
                            and record.get("status") == "completed"
                            for record in receipts
                        )
                    ]
                    result.warnings.append(
                        "候选 Word 已打开；常规监测仍处于 stopping，等待参与者 stop completed 回执%s。"
                        % ("：" + ", ".join(missing) if missing else "（现有回执未通过绑定/产物校验）")
                    )

        if _project_machine_indexes(state, workspace, receipts, participants):
            changed = True

        if changed:
            update_content_hash(workspace, state)
            state["revision"] = int(state.get("revision", 0)) + 1
            state["last_orchestrated_at"] = _now()
            atomic_write_json(state_path, state)
        result.stage = str(state.get("stage", "unknown"))
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one coordinator orchestration pass")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-open", action="store_true", help="生成候选 Word 但不打开系统程序")
    parser.add_argument("--actor", required=True, help="state.json 中登记的 coordinator agent_id")
    parser.add_argument("--platform-id", required=True, help="当前执行会话的平台标识")
    parser.add_argument("--session-id", required=True, help="当前执行会话的精确标识")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required; scheduling is owned by the platform")
    try:
        result = orchestrate_once(
            Path(args.workspace), open_candidate=not args.no_open,
            actor=args.actor, platform_id=args.platform_id, session_id=args.session_id,
        )
    except WorkflowError as error:
        print(json.dumps({"stage": "blocked", "error_code": error.code, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"stage": result.stage, "issued_instruction_ids": result.issued_instruction_ids, "blocking_error_codes": result.blocking_error_codes, "warnings": result.warnings}, ensure_ascii=False))
    return 0 if not result.blocking_error_codes else 1


if __name__ == "__main__":
    sys.exit(main())
