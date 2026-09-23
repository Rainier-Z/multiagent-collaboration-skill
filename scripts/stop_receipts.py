"""Shared validation for participant stop-completion receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from participant_runtime.protocol import (
    Instruction,
    Receipt,
    load_stop_attestation,
    receipt_path,
    validate_input_manifest,
)
from workflow_core import path_in_workspace, sha256_file


def validate_completed_stop_receipt(
    workspace: Path,
    state: Mapping[str, Any],
    agent_id: str,
    instruction: Instruction,
    receipt: Mapping[str, Any],
) -> bool:
    """Return whether one receipt authorizes this participant monitor to stop."""
    bindings = state.get("participant_bindings")
    binding = bindings.get(agent_id) if isinstance(bindings, Mapping) else None
    if not isinstance(binding, Mapping):
        return False
    if (
        instruction.agent_id != agent_id
        or instruction.kind != "stop"
        or instruction.discussion_id != state.get("discussion_id")
        or instruction.platform_id != binding.get("platform_id")
        or instruction.session_id != binding.get("session_id")
        or instruction.output_path != ".multiagent/receipts/%s" % agent_id
        or instruction.state_revision > int(state.get("revision", -1))
        or receipt.get("instruction_id") != instruction.instruction_id
        or receipt.get("agent_id") != agent_id
        or receipt.get("kind") != "stop"
        or receipt.get("status") != "completed"
        or receipt.get("runtime_version") != instruction.runtime_version
        or receipt.get("state_revision") != instruction.state_revision
        or receipt.get("attempt") != instruction.attempt
    ):
        return False
    expected_path = receipt_path(workspace, agent_id, instruction.instruction_id, "completed")
    actual_path = receipt.get("_path")
    if not isinstance(actual_path, Path) or actual_path.resolve() != expected_path.resolve():
        return False
    try:
        Receipt.from_dict(dict(receipt))
        marker_path = ".multiagent/receipts/%s/%s-stop-marker.txt" % (agent_id, instruction.instruction_id)
        marker = path_in_workspace(workspace, marker_path)
        if receipt.get("output_path") != marker_path or not marker.is_file():
            return False
        if receipt.get("output_sha256") != sha256_file(marker):
            return False
        validate_input_manifest(workspace, instruction)
        evidence = receipt.get("isolation_evidence")
        if not isinstance(evidence, dict):
            return False
        if instruction.access_scope.get("security_mode", "strict") == "strict":
            return evidence == load_stop_attestation(workspace, instruction)
        return True
    except Exception:
        return False
