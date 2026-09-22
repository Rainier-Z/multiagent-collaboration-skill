#!/usr/bin/env python3
"""Single-instruction participant executor.

The runner is a deliberately constrained local capability.  It does not call a
model and does not poll.  A platform adapter wakes an already-authorized agent,
which writes its own Markdown, then invokes this runner once to validate and
record the result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from workflow_core import E_OUTPUT_FORMAT, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, WorkflowError, path_in_workspace, sha256_file
except ModuleNotFoundError:
    try:
        from .runtime_core import E_OUTPUT_FORMAT, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, WorkflowError, path_in_workspace, sha256_file
    except ImportError:
        from runtime_core import E_OUTPUT_FORMAT, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, WorkflowError, path_in_workspace, sha256_file  # type: ignore[no-redef]

try:
    from .protocol import (
        Instruction, InputManifest, RUNTIME_VERSION, load_isolation_evidence,
        load_stop_attestation,
        receipt_path, validate_agent_id, validate_input_manifest,
        validate_instruction_id, verify_manifest, write_receipt,
    )
except ImportError:
    from protocol import (  # type: ignore[no-redef]
        Instruction, InputManifest, RUNTIME_VERSION, load_isolation_evidence,
        load_stop_attestation,
        receipt_path, validate_agent_id, validate_input_manifest,
        validate_instruction_id, verify_manifest, write_receipt,
    )

EXIT_OK = 0
EXIT_USAGE = 2


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _failure(workspace: Path, agent_id: str, instruction: Instruction, code: str, message: str, recoverable: bool) -> None:
    payload = {
        "instruction_id": instruction.instruction_id,
        "kind": instruction.kind,
        "kind": instruction.kind,
        "agent_id": agent_id,
        "runtime_version": instruction.runtime_version,
        "state_revision": instruction.state_revision,
        "status": "failed",
        "at": _now(),
        "attempt": instruction.attempt,
        "error_code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if instruction.origin_instruction_id is not None:
        payload["origin_instruction_id"] = instruction.origin_instruction_id
    if instruction.root_instruction_id is not None:
        payload["root_instruction_id"] = instruction.root_instruction_id
    write_receipt(workspace, agent_id, payload)


def _accepted(
    workspace: Path,
    agent_id: str,
    instruction: Instruction,
    isolation_evidence: dict[str, Any],
) -> None:
    write_receipt(workspace, agent_id, {
        "instruction_id": instruction.instruction_id,
        "kind": instruction.kind,
        "kind": instruction.kind,
        "agent_id": agent_id,
        "runtime_version": instruction.runtime_version,
        "state_revision": instruction.state_revision,
        "status": "accepted",
        "at": _now(),
        "attempt": instruction.attempt,
        "isolation_evidence": isolation_evidence,
    })


def _completed(
    workspace: Path,
    agent_id: str,
    instruction: Instruction,
    output: Path,
    isolation_evidence: dict[str, Any],
) -> None:
    write_receipt(workspace, agent_id, {
        "instruction_id": instruction.instruction_id,
        "instruction_sha256": instruction.sha256,
        "kind": instruction.kind,
        "kind": instruction.kind,
        "agent_id": agent_id,
        "runtime_version": instruction.runtime_version,
        "state_revision": instruction.state_revision,
        "status": "completed",
        "at": _now(),
        "attempt": instruction.attempt,
        "output_path": output.relative_to(workspace).as_posix(),
        "output_sha256": sha256_file(output),
        "isolation_evidence": isolation_evidence,
    })


def _expected_output(workspace: Path, agent_id: str, instruction: Instruction) -> Path:
    output = path_in_workspace(workspace, instruction.output_path)
    view_root = path_in_workspace(workspace, ".multiagent/views/%s" % agent_id)
    if instruction.kind in {"propose", "repair"}:
        permitted = view_root / "outputs" / "提案文档.md"
    elif instruction.kind == "respond":
        permitted = view_root / "outputs" / "交叉回应文档.md"
    elif instruction.kind in {"final_ack", "stop"}:
        permitted = path_in_workspace(workspace, ".multiagent/receipts/%s" % agent_id)
    else:
        permitted = path_in_workspace(workspace, ".multiagent/receipts/%s" % agent_id)
    response_round_output = (
        instruction.kind == "respond"
        and re.fullmatch(
            rf"\.multiagent/views/{re.escape(agent_id)}/outputs/round-[1-9][0-9]*/交叉回应文档\.md",
            output.relative_to(workspace).as_posix(),
        )
    )
    if instruction.kind in {"propose", "repair", "respond"} and output != permitted and not response_round_output:
        raise WorkflowError("%s: output path is outside participant scope" % E_PATH_SCOPE, E_PATH_SCOPE, output_path=instruction.output_path)
    if instruction.kind not in {"propose", "repair", "respond"} and output != permitted:
        raise WorkflowError("%s: system output path is outside participant receipt scope" % E_PATH_SCOPE, E_PATH_SCOPE, output_path=instruction.output_path)
    return output


def _load_instruction(workspace: Path, agent_id: str, instruction_id: str) -> Instruction:
    agent_id = validate_agent_id(agent_id)
    instruction_id = validate_instruction_id(instruction_id)
    directory = path_in_workspace(workspace, ".multiagent/instructions/%s" % agent_id)
    matches = sorted(directory.glob("*-%s.json" % instruction_id))
    if len(matches) != 1:
        raise WorkflowError("%s: instruction not found or ambiguous" % E_PATH_SCOPE, E_PATH_SCOPE, instruction_id=instruction_id)
    instruction = Instruction.from_dict(json.loads(matches[0].read_text(encoding="utf-8")))
    if instruction.agent_id != agent_id:
        raise WorkflowError("%s: instruction belongs to another agent" % E_PATH_SCOPE, E_PATH_SCOPE, instruction_agent=instruction.agent_id, runner_agent=agent_id)
    return instruction


def _validate_inputs(workspace: Path, instruction: Instruction) -> InputManifest:
    return validate_input_manifest(workspace, instruction)


def _restricted_view_summary(instruction: Instruction, manifest: InputManifest) -> dict[str, Any]:
    """Describe hash-checked path scoping without claiming hard isolation."""
    return {
        "mode": "restricted_view",
        "agent_id": instruction.agent_id,
        "instruction_id": instruction.instruction_id,
        "view_root": manifest.view_root,
        "scope_digest": manifest.scope_digest,
        "input_manifest_sha256": instruction.access_scope["input_manifest_sha256"],
        "allowed_read_roots": list(instruction.access_scope["allowed_read_roots"]),
        "allowed_write_roots": list(instruction.access_scope["allowed_write_roots"]),
        "contract_validation": "passed",
        "authenticity": "path_and_hash_contract_only",
        "cryptographic_verification": "not_performed",
    }


def _terminal_exists(
    workspace: Path,
    agent_id: str,
    instruction_id: str,
    instruction_kind: str | None = None,
) -> bool:
    if any(receipt_path(workspace, agent_id, instruction_id, status).exists() for status in ("completed", "failed")):
        return True
    # Bootstrap has no participant content output. A verified accepted receipt is
    # therefore its explicit terminal state and remains compatible with legacy
    # workflows that count only business completed receipts.
    return instruction_kind == "bootstrap" and receipt_path(
        workspace, agent_id, instruction_id, "accepted"
    ).exists()


def run_instruction(workspace: Path, agent_id: str, instruction_id: str) -> int:
    """Consume at most one already-published instruction for ``agent_id``.

    All errors are represented as stable exit codes.  We only emit a failed
    receipt after confirming that the instruction itself belongs to this agent.
    """
    workspace = Path(workspace).resolve()
    try:
        agent_id = validate_agent_id(agent_id)
        instruction_id = validate_instruction_id(instruction_id)
        instruction = _load_instruction(workspace, agent_id, instruction_id)
    except WorkflowError as error:
        return error.code
    try:
        if _terminal_exists(workspace, agent_id, instruction.instruction_id, instruction.kind):
            return EXIT_OK
        manifest = verify_manifest(workspace, runtime_version=instruction.runtime_version)
        _expected_output(workspace, agent_id, instruction)
        input_manifest = _validate_inputs(workspace, instruction)
        if instruction.kind in {"propose", "repair", "final_ack"}:
            if instruction.access_scope.get("security_mode", "strict") == "strict":
                isolation_evidence = load_isolation_evidence(workspace, instruction)
            else:
                isolation_evidence = _restricted_view_summary(instruction, input_manifest)
        elif instruction.kind == "stop":
            # A marker written by this runner is only a receipt artifact, never
            # proof that a platform monitor or automation was actually stopped.
            if instruction.access_scope.get("security_mode", "strict") == "strict":
                isolation_evidence = load_stop_attestation(workspace, instruction)
            else:
                isolation_evidence = _restricted_view_summary(instruction, input_manifest)
        else:
            isolation_evidence = _restricted_view_summary(instruction, input_manifest)
        _accepted(workspace, agent_id, instruction, isolation_evidence)
        if instruction.kind == "bootstrap":
            return EXIT_OK
        if instruction.kind == "upgrade":
            manifest_path = path_in_workspace(
                workspace,
                ".multiagent/runtime/participant/%s/manifest.json" % manifest.runtime_version,
            )
            marker = path_in_workspace(workspace, ".multiagent/receipts/%s/%s-upgrade-marker.txt" % (agent_id, instruction.instruction_id))
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("upgraded %s\n" % manifest.runtime_version, encoding="utf-8")
            _completed(workspace, agent_id, instruction, marker, isolation_evidence)
            return EXIT_OK
        if instruction.kind == "final_ack":
            marker = path_in_workspace(workspace, ".multiagent/receipts/%s/%s-final-ack-marker.txt" % (agent_id, instruction.instruction_id))
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("acknowledged\n", encoding="utf-8")
            _completed(workspace, agent_id, instruction, marker, isolation_evidence)
            return EXIT_OK
        if instruction.kind == "stop":
            marker = path_in_workspace(workspace, ".multiagent/receipts/%s/%s-stop-marker.txt" % (agent_id, instruction.instruction_id))
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("stopped\n", encoding="utf-8")
            _completed(workspace, agent_id, instruction, marker, isolation_evidence)
            return EXIT_OK
        output = _expected_output(workspace, agent_id, instruction)
        if not output.is_file() or not output.read_text(encoding="utf-8").strip():
            _failure(workspace, agent_id, instruction, E_OUTPUT_FORMAT, "declared Markdown output is missing or empty", True)
            return E_OUTPUT_FORMAT
        if output.suffix.lower() != ".md":
            _failure(workspace, agent_id, instruction, E_OUTPUT_FORMAT, "participant output must be Markdown", True)
            return E_OUTPUT_FORMAT
        _completed(workspace, agent_id, instruction, output, isolation_evidence)
        return EXIT_OK
    except WorkflowError as error:
        try:
            _failure(workspace, agent_id, instruction, error.code, str(error), error.code in {E_SCHEMA, E_OUTPUT_FORMAT})
        except WorkflowError:
            pass
        return error.code
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        try:
            _failure(workspace, agent_id, instruction, E_SCHEMA, str(error), True)
        except WorkflowError:
            pass
        return E_SCHEMA


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume one project-scoped participant instruction")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instruction-id", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required; the runtime never polls")
    try:
        validate_agent_id(args.agent)
    except WorkflowError as error:
        return error.code
    directory = path_in_workspace(Path(args.workspace), ".multiagent/instructions/%s" % args.agent)
    candidates = sorted(directory.glob("*.json"))
    if args.instruction_id:
        return run_instruction(Path(args.workspace), args.agent, args.instruction_id)
    for candidate in candidates:
        instruction = Instruction.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
        if not _terminal_exists(
            Path(args.workspace), args.agent, instruction.instruction_id, instruction.kind
        ):
            return run_instruction(Path(args.workspace), args.agent, instruction.instruction_id)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
