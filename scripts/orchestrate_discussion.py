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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.common.wake_protocol import WakeAdapter, WakeRequest, WakeResult
from workflow_core import (
    E_OUTPUT_FORMAT, E_PLATFORM_UNAVAILABLE, E_RETRY_EXHAUSTED, E_SCHEMA, E_STATE_CONFLICT,
    E_RECEIPT_UNREADABLE,
    StateLock, WorkflowError, atomic_write_json, atomic_write_text,
    ensure_content_consistency, load_json, load_state, main_markdown_path,
    path_in_workspace, sha256_file, update_content_hash,
    validate_coordinator_execution, validate_state_shape,
)
from events import EventStream, reconcile_instruction_events
from transactions import EventTransaction, commit_transaction, recover_transactions
from transactions import commit_round_transition
from convergence import AgentResponse, ConvergenceEvaluator, validate_convergence_assessment, ConvergenceSchemaError
from participant_runtime.protocol import (
    Instruction, instruction_path, isolation_evidence_path, load_isolation_evidence,
    validate_input_manifest, write_instruction,
)
from stop_receipts import validate_completed_stop_receipt
from instruction_prompts import build_task_prompt
from participant_views import (
    access_scope as sealed_access_scope,
    access_scope_for_manifest,
    output_path as sealed_output_path,
    prepare_inputs,
    publish_inputs,
    PreparedInputs,
)
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
        except Exception as exc:
            raise WorkflowError(
                "receipt JSON 无法解析，流程已 fail-closed",
                E_RECEIPT_UNREADABLE,
                path=str(path),
            ) from exc
        if isinstance(value, dict):
            value = dict(value)
            value["_path"] = path
            records.append(value)
        else:
            raise WorkflowError(
                "receipt JSON 顶层必须是对象，流程已 fail-closed",
                E_RECEIPT_UNREADABLE,
                path=str(path),
            )
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
    round_number: int | None = None,
    origin_instruction_id: str | None = None,
    root_instruction_id: str | None = None,
    failure_receipt: dict[str, Any] | None = None,
    prepared_inputs: PreparedInputs | None = None,
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
    if kind in {"respond", "final_ack"}:
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
    if prepared_inputs is None:
        inputs = publish_inputs(workspace, agent_id, input_sources, instruction_kind=kind)
        scope = sealed_access_scope(workspace, agent_id)
        input_artifacts: dict[str, bytes] = {}
    else:
        inputs = list(prepared_inputs.published)
        scope = access_scope_for_manifest(
            workspace, agent_id, prepared_inputs.manifest_relative,
            prepared_inputs.manifest_sha256, prepared_inputs.scope_digest,
        )
        input_artifacts = dict(prepared_inputs.artifacts)
    if output_path is None:
        output_path = sealed_output_path(workspace, agent_id, kind)
    if kind == "respond" and round_number is not None:
        output_path = (
            workspace / ".multiagent" / "views" / agent_id / "outputs"
            / ("round-%d" % round_number) / "交叉回应文档.md"
        ).relative_to(workspace).as_posix()
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
    if kind == "respond" and round_number is not None:
        payload["task_prompt"] += "\n\n当前交叉回应轮次：%d。只回应本轮输入快照，不得覆盖其他轮次产物。" % round_number
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["sha256"] = hashlib.sha256(canonical).hexdigest()
    instruction = Instruction.from_dict(payload)
    # The attribute is intentionally private and consumed only by the round
    # transition planner; ordinary _issue continues to publish immediately.
    if input_artifacts:
        object.__setattr__(instruction, "_prepared_artifacts", input_artifacts)
    return instruction


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


def _modern_instruction_event(
    workspace: Path,
    state: dict[str, Any],
    instruction: Instruction,
    *,
    activation_status: str = "pending_monitor",
) -> None:
    """Publish an instruction event without activating a participant.

    Modern workspaces separate the coordinator (publisher) from the
    participant monitor (activation bridge).  The coordinator may append the
    durable event, but must never call a platform wake adapter here.
    """
    event_id = "instruction-%s" % instruction.instruction_id
    if any(isinstance(event, dict) and event.get("event_id") == event_id for event in EventStream(workspace).read()):
        return
    EventStream(workspace).append(
        "instruction_issued",
        {
            "agent_id": instruction.agent_id,
            "instruction_id": instruction.instruction_id,
            "kind": instruction.kind,
            "activation_status": activation_status,
        },
        transaction_id="instruction-%s" % instruction.instruction_id,
        revision_before=int(state.get("revision", 0)),
        revision_after=int(state.get("revision", 0)),
        event_id=event_id,
    )


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
    if _round_lifecycle(state):
        if not any(
            isinstance(event, dict)
            and event.get("event_id") == "instruction-%s" % instruction.instruction_id
            for event in EventStream(workspace).read()
        ):
            _modern_instruction_event(workspace, state, instruction)
        return
    if any(isinstance(event, dict) and event.get("instruction_id") == instruction.instruction_id for event in events):
        return
    binding = _participant_binding(state, agent_id)
    if instruction.platform_id != binding["platform_id"] or instruction.session_id != binding["session_id"]:
        raise WorkflowError("published instruction target differs from participant binding", "E_PARTICIPANT_BINDING", agent_id=agent_id)
    request = WakeRequest(workspace, agent_id, instruction.instruction_id, instruction.runtime_version,
                          binding["platform_id"], binding["session_id"])
    wake_result = wake.wake(request)
    _record_wake(workspace, request, wake_result)
    EventStream(workspace).append(
        "instruction_issued",
        {
            "agent_id": agent_id,
            "instruction_id": instruction.instruction_id,
            "kind": instruction.kind,
            "activation_status": wake_result.status,
        },
        transaction_id="instruction-%s" % instruction.instruction_id,
        revision_before=int(state.get("revision", 0)),
        revision_after=int(state.get("revision", 0)),
        event_id="instruction-%s" % instruction.instruction_id,
    )
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
    round_number: int | None = None,
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
        round_number=round_number,
        origin_instruction_id=origin_instruction_id,
        root_instruction_id=root_instruction_id,
        failure_receipt=failure_receipt,
    )
    if _round_lifecycle(state):
        revision_before = int(state.get("revision", 0))
        EventTransaction(
            workspace,
            transaction_id="instruction-%s-%s-%s"
            % (instruction.agent_id, instruction.instruction_id, uuid.uuid4().hex),
        ).commit(
            event_type="instruction_issued",
            event_id="instruction-" + instruction.instruction_id,
            event_payload={
                "agent_id": instruction.agent_id,
                "instruction_id": instruction.instruction_id,
                "kind": instruction.kind,
                "activation_status": "pending_monitor",
            },
            artifacts=_instruction_artifacts(workspace, instruction),
            expected_revision=revision_before,
            state_update=state,
        )
        state["revision"] = revision_before + 1
        result.issued_instruction_ids.append(instruction.instruction_id)
        return
    write_instruction(workspace, instruction)
    result.issued_instruction_ids.append(instruction.instruction_id)
    binding = _participant_binding(state, agent_id)
    request = WakeRequest(workspace, agent_id, instruction.instruction_id, instruction.runtime_version,
                          binding["platform_id"], binding["session_id"])
    wake_result = wake.wake(request)
    _record_wake(workspace, request, wake_result)
    EventStream(workspace).append(
        "instruction_issued",
        {
            "agent_id": agent_id,
            "instruction_id": instruction.instruction_id,
            "kind": instruction.kind,
            "activation_status": wake_result.status,
        },
        transaction_id="instruction-%s" % instruction.instruction_id,
        revision_before=int(state.get("revision", 0)),
        revision_after=int(state.get("revision", 0)),
        event_id="instruction-%s" % instruction.instruction_id,
    )
    if wake_result.status != "accepted" and E_PLATFORM_UNAVAILABLE not in result.blocking_error_codes:
        result.blocking_error_codes.append(E_PLATFORM_UNAVAILABLE)


def _instruction_artifacts(workspace: Path, instruction: Instruction) -> dict[str, bytes]:
    """Serialize an already validated instruction for a transaction."""
    path = instruction_path(workspace, instruction.agent_id, instruction)
    return {path.relative_to(workspace).as_posix(): json.dumps(
        instruction.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")}


def _round_snapshot(
    workspace: Path,
    receipts: list[dict[str, Any]],
    participants: list[str],
    round_number: int,
) -> tuple[str, bytes, str]:
    """Build the immutable public snapshot for one completed response round."""
    lines = [
        "# Public Round Snapshot %d" % round_number,
        "",
        "本文件是第 %d 轮交叉回应完成后的公共快照。后续轮次只能读取本快照，不得直接读取其他参与者私有输出。" % round_number,
        "",
    ]
    for agent in participants:
        records = [
            record for record in receipts
            if record.get("agent_id") == agent
            and record.get("kind") == "respond"
            and record.get("status") == "completed"
            and _response_round(workspace, record) == round_number
        ]
        if not records:
            raise WorkflowError("缺少 round response receipt", E_SCHEMA, agent_id=agent, round=round_number)
        record = records[-1]
        output = path_in_workspace(workspace, str(record.get("output_path", "")))
        if not output.is_file():
            raise WorkflowError("round response output missing", E_SCHEMA, agent_id=agent, round=round_number)
        text = output.read_text(encoding="utf-8").strip()
        lines.extend([
            "## %s" % agent,
            "",
            "> 来源 SHA-256：`%s`" % sha256_file(output),
            "",
            text,
            "",
        ])
    content = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    relative = ".multiagent/rounds/round-%d.md" % round_number
    return relative, content, hashlib.sha256(content).hexdigest()


def _validate_round_snapshots(workspace: Path, state: dict[str, Any]) -> None:
    """Fail closed if any sealed public round snapshot changed on disk."""
    rounds = state.get("rounds")
    if not isinstance(rounds, dict):
        return
    for key, metadata in rounds.items():
        if not isinstance(metadata, dict):
            raise WorkflowError("round metadata is malformed", E_SCHEMA, round=key)
        path_value = metadata.get("snapshot_path")
        expected = metadata.get("snapshot_sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise WorkflowError("round metadata lacks immutable snapshot binding", E_SCHEMA, round=key)
        snapshot = path_in_workspace(workspace, path_value)
        if not snapshot.is_file() or sha256_file(snapshot) != expected:
            raise WorkflowError("round snapshot hash mismatch", E_STATE_CONFLICT, round=key, path=path_value)
        assessment_value = metadata.get("assessment_path")
        assessment_hash = metadata.get("assessment_sha256")
        if assessment_value is not None or assessment_hash is not None:
            if not isinstance(assessment_value, str) or not isinstance(assessment_hash, str):
                raise WorkflowError("round convergence assessment binding is malformed", E_SCHEMA, round=key)
            assessment = path_in_workspace(workspace, assessment_value)
            if not assessment.is_file() or sha256_file(assessment) != assessment_hash:
                raise WorkflowError("round convergence assessment hash mismatch", E_STATE_CONFLICT, round=key, path=assessment_value)


def _publish_round_snapshot(
    workspace: Path,
    state: dict[str, Any],
    receipts: list[dict[str, Any]],
    participants: list[str],
    current_round: int,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Publish the immutable public snapshot as the first round transition.

    A completed response round has two durable phases.  This phase publishes
    only the public snapshot and its state binding.  The coordinator must then
    read that snapshot and write the semantic assessment before the next-round
    transition is allowed.  Keeping these commits separate prevents a
    pre-existing assessment from making it appear as though a snapshot had
    already been published and reviewed.
    """
    snapshot_relative, snapshot_data, snapshot_sha256 = _round_snapshot(
        workspace, receipts, participants, current_round,
    )
    snapshot_path = path_in_workspace(workspace, snapshot_relative)
    if snapshot_path.exists() and snapshot_path.read_bytes() != snapshot_data:
        raise WorkflowError("round snapshot is immutable", E_STATE_CONFLICT, path=snapshot_relative)

    rounds = dict(state.get("rounds") or {})
    existing = rounds.get(str(current_round))
    if isinstance(existing, dict):
        existing_path = existing.get("snapshot_path")
        existing_hash = existing.get("snapshot_sha256")
        if existing_path != snapshot_relative or existing_hash != snapshot_sha256:
            raise WorkflowError("round snapshot binding conflict", E_STATE_CONFLICT, round=current_round)
        # The snapshot transaction was already committed.  Do not append a
        # duplicate event or advance the revision on a retry.
        return state

    rounds[str(current_round)] = {
        "round": current_round,
        "snapshot_path": snapshot_relative,
        "snapshot_sha256": snapshot_sha256,
        "status": "snapshot_published",
        "snapshot_published_revision": expected_revision + 1,
        "published_at": _now(),
    }
    state_update = dict(state)
    state_update["rounds"] = rounds
    commit_round_transition(
        workspace,
        round_number=current_round,
        snapshot_path=snapshot_relative,
        snapshot=snapshot_data,
        event_payload={
            "phase": "snapshot_published",
            "snapshot_path": snapshot_relative,
            "snapshot_sha256": snapshot_sha256,
        },
        expected_revision=expected_revision,
        state_update=state_update,
        event_type="round_snapshot_published",
    )
    state = dict(state_update)
    state["revision"] = expected_revision + 1
    return state


def _round_transition(
    workspace: Path,
    state: dict[str, Any],
    receipts: list[dict[str, Any]],
    participants: list[str],
    current_round: int,
    convergence: dict[str, Any],
    wake: WakeAdapter,
    result: OrchestrationResult,
    *,
    expected_revision: int,
) -> tuple[dict[str, Any], bool]:
    """Atomically publish the next-round transition after assessment.

    The immutable snapshot is published by :func:`_publish_round_snapshot` in
    a prior transaction.  This second transaction is therefore the semantic
    assessment gate: it seals the assessment binding, next-round inputs and
    instructions, ``state.round``/round metadata and the ``round_completed``
    event together. Wake delivery is deliberately performed only after commit
    because it is an external side effect.
    """
    snapshot_relative, snapshot_data, snapshot_sha256 = _round_snapshot(
        workspace, receipts, participants, current_round,
    )
    snapshot_path = path_in_workspace(workspace, snapshot_relative)
    if snapshot_path.exists() and snapshot_path.read_bytes() != snapshot_data:
        raise WorkflowError("round snapshot is immutable", E_STATE_CONFLICT, path=snapshot_relative)
    rounds = dict(state.get("rounds") or {})
    round_metadata = rounds.get(str(current_round))
    if not isinstance(round_metadata, dict):
        raise WorkflowError("round snapshot must be published before assessment", E_STATE_CONFLICT, round=current_round)
    if round_metadata.get("snapshot_path") != snapshot_relative or round_metadata.get("snapshot_sha256") != snapshot_sha256:
        raise WorkflowError("round snapshot binding conflict", E_STATE_CONFLICT, round=current_round)
    if round_metadata.get("status") not in {"snapshot_published", "completed"}:
        raise WorkflowError("round snapshot state is malformed", E_SCHEMA, round=current_round)
    artifacts: dict[str, bytes] = {}
    assessment_relative = ".multiagent/convergence/round-%d.json" % current_round
    assessment_path = path_in_workspace(workspace, assessment_relative)
    if not assessment_path.is_file():
        raise WorkflowError("缺少 Coordinator 收敛评估 JSON", E_SCHEMA, path=assessment_relative, round=current_round)
    assessment_data = assessment_path.read_bytes()
    assessment_sha256 = hashlib.sha256(assessment_data).hexdigest()
    artifacts[assessment_relative] = assessment_data
    if round_metadata.get("status") == "completed":
        if round_metadata.get("assessment_path") != assessment_relative or round_metadata.get("assessment_sha256") != assessment_sha256:
            raise WorkflowError("round assessment binding conflict", E_STATE_CONFLICT, round=current_round)
        # A coordinator crash may occur after this transaction commits but
        # before the follow-up candidate materialization.  Treat the durable
        # completed metadata as the source of truth and avoid duplicating the
        # round event or next-round instructions on retry.
        return state, round_metadata.get("next_round") is not None
    next_round = None
    instructions: list[Instruction] = []
    configured_max = int((state.get("convergence") or {}).get("max_response_rounds", 3))
    if not convergence.get("converged") and current_round < configured_max:
        next_round = current_round + 1
        snapshot_source = path_in_workspace(workspace, snapshot_relative)
        discussion_source = _main_markdown(workspace)
        if discussion_source is None or not discussion_source.is_file():
            raise WorkflowError("next round requires the authoritative discussion Markdown", E_SCHEMA)
        for agent in participants:
            prepared = prepare_inputs(
                workspace,
                agent,
                [
                    (workspace / "project-context.md", "project-context.md"),
                    (discussion_source, "discussion.md"),
                    (snapshot_source, "round-%d.md" % current_round),
                ],
                instruction_kind="respond",
                source_contents={snapshot_relative: snapshot_data},
            )
            instruction = _new_instruction(
                state, workspace, agent, "respond", round_number=next_round,
                prepared_inputs=prepared,
            )
            instructions.append(instruction)
            artifacts.update(prepared.artifacts)
            artifacts.update(_instruction_artifacts(workspace, instruction))

    rounds[str(current_round)] = {
        **round_metadata,
        "round": current_round,
        "snapshot_path": snapshot_relative,
        "snapshot_sha256": snapshot_sha256,
        "assessment_path": assessment_relative,
        "assessment_sha256": assessment_sha256,
        "status": "completed",
        "completed_at": _now(),
        "convergence": convergence,
        "next_round": next_round,
        "instruction_ids": [item.instruction_id for item in instructions],
    }
    state["rounds"] = rounds
    state["convergence"] = {
        **(state.get("convergence") if isinstance(state.get("convergence"), dict) else {}),
        "last_result": convergence,
        "evaluated_round": current_round,
    }
    if next_round is not None:
        state["round"] = next_round
        queue = {agent: list(values) for agent, values in (state.get("instruction_queue") or {}).items()}
        for instruction in instructions:
            queue.setdefault(instruction.agent_id, []).append(instruction.instruction_id)
        state["instruction_queue"] = queue
    commit_round_transition(
        workspace,
        round_number=current_round,
        snapshot_path=snapshot_relative,
        snapshot=snapshot_data,
        next_round_instructions=artifacts,
        event_payload={
            "round": current_round,
            "snapshot_path": snapshot_relative,
            "snapshot_sha256": snapshot_sha256,
            "next_round": next_round,
            "instruction_ids": [item.instruction_id for item in instructions],
        },
        expected_revision=expected_revision,
        state_update=state,
        event_type="round_completed",
    )
    state["revision"] = expected_revision + 1
    for instruction in instructions:
        result.issued_instruction_ids.append(instruction.instruction_id)
        if _round_lifecycle(state):
            _modern_instruction_event(workspace, state, instruction)
        else:
            _wake_existing(workspace, state, wake, result, instruction.agent_id, instruction)
    return state, next_round is not None


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
    updated = _append_section_text(content, title, sections)
    if updated != content:
        atomic_write_text(path, updated)


def _append_section_text(content: str, title: str, sections: list[tuple[str, str]]) -> str:
    """Pure counterpart of :func:`_append_section` for transactional writes."""
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
        return content
    replacement = content[:body_start] + "\n\n" + "\n\n".join(filter(None, (body, *additions))) + "\n\n" + content[body_end:].lstrip("\n")
    return replacement


def _completed_output(receipts: list[dict[str, Any]], agent_id: str, kinds: set[str]) -> dict[str, Any] | None:
    candidates = [record for record in receipts if record.get("agent_id") == agent_id and record.get("status") == "completed" and record.get("kind") in kinds]
    return candidates[-1] if candidates else None


def _participant_output_path(workspace: Path, agent_id: str, folder: str) -> Path:
    name = "%s-%s" % (agent_id, "提案文档.md" if folder == "proposals" else "交叉回应文档.md")
    return workspace / ".multiagent" / "views" / agent_id / "outputs" / name.replace(agent_id + "-", "", 1)


def _response_round(workspace: Path, receipt: dict[str, Any]) -> int:
    instruction = _find_instruction(workspace, str(receipt.get("agent_id", "")), str(receipt.get("instruction_id", "")))
    if instruction is None:
        return 0
    match = re.search(r"/outputs/round-([1-9][0-9]*)/交叉回应文档\.md$", instruction.output_path)
    return int(match.group(1)) if match else 1


def _round_lifecycle(state: dict[str, Any]) -> bool:
    """Return True for workspaces initialized with the event/round protocol.

    Older fixtures and workspaces remain readable so the migration can be
    performed without silently changing their confirmation contract.
    """
    return isinstance(state.get("coordinator_participant"), dict) and isinstance(state.get("round"), int)


def _valid_completed_outputs(
    workspace: Path,
    state: dict[str, Any],
    receipts: list[dict[str, Any]],
    participants: list[str],
    kinds: set[str],
    folder: str,
    round_number: int | None = None,
) -> bool:
    for agent in participants:
        candidates = [
            record for record in receipts
            if record.get("agent_id") == agent
            and record.get("status") == "completed"
            and record.get("kind") in kinds
            and (round_number is None or _response_round(workspace, record) == round_number)
        ]
        receipt = candidates[-1] if candidates else None
        if receipt is None:
            return False
        relative = receipt.get("output_path")
        if not isinstance(relative, str) or not relative:
            return False
        try:
            expected = path_in_workspace(workspace, relative)
        except Exception:
            return False
        instruction_id = receipt.get("instruction_id")
        instruction = _find_instruction(workspace, agent, str(instruction_id)) if instruction_id else None
        if instruction is None or instruction.kind not in kinds:
            return False
        if folder == "responses":
            expected_output = path_in_workspace(workspace, instruction.output_path)
        else:
            expected_output = _participant_output_path(workspace, agent, folder)
        if expected.resolve() != expected_output.resolve():
            return False
        if not expected.is_file() or not expected.read_text(encoding="utf-8").strip():
            return False
        output_hash = receipt.get("output_sha256")
        if not isinstance(output_hash, str) or output_hash != sha256_file(expected):
            return False
        try:
            binding = _participant_binding(state, agent)
            if instruction.platform_id != binding["platform_id"] or instruction.session_id != binding["session_id"]:
                return False
            validate_input_manifest(workspace, instruction)
        except Exception:
            return False
        if instruction.kind in {"propose", "repair"}:
            if instruction.access_scope.get("security_mode", "strict") == "strict":
                try:
                    evidence = load_isolation_evidence(workspace, instruction)
                except Exception:
                    return False
                if receipt.get("isolation_evidence") != evidence:
                    return False
            elif not isinstance(receipt.get("isolation_evidence"), dict):
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
        matches = [
            record for record in receipts
            if record.get("agent_id") == agent
            and record.get("kind") == "stop"
            and record.get("status") == "completed"
        ]
        if len(matches) != 1:
            return False
        if not validate_completed_stop_receipt(workspace, state, agent, instruction, matches[0]):
            return False
    return True


def _all_monitors_stopped(workspace: Path, state: dict[str, Any], participants: list[str]) -> bool:
    """Require each participant monitor to durably acknowledge its own stop."""
    expected = (state.get("monitoring") or {}).get("stop_instruction_ids")
    if not isinstance(expected, dict):
        return False
    for agent in participants:
        instruction_id = expected.get(agent)
        cursor_path = workspace / ".multiagent" / "monitors" / agent / "cursor.json"
        try:
            cursor = load_json(cursor_path)
        except Exception:
            return False
        if not isinstance(cursor, dict) or cursor.get("agent_id") != agent:
            return False
        if cursor.get("status") != "stopped" or cursor.get("stop_instruction_id") != instruction_id:
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
            if _round_lifecycle(state):
                _modern_instruction_event(workspace, state, instruction)
            else:
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


def _final_ack_instructions(workspace: Path, participants: list[str]) -> dict[str, Instruction]:
    """Load the unique final-decision acknowledgement instruction per agent."""
    found: dict[str, Instruction] = {}
    for agent in participants:
        directory = _instruction_root(workspace) / agent
        matches: list[Instruction] = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                instruction = Instruction.from_dict(load_json(path))
            except Exception:
                continue
            if instruction.kind == "final_ack" and instruction.agent_id == agent:
                matches.append(instruction)
        if len(matches) == 1:
            found[agent] = matches[0]
    return found


def _all_final_ack_receipts_completed(
    workspace: Path,
    receipts: list[dict[str, Any]],
    participants: list[str],
) -> bool:
    """Require one completed final_ack bound to the issued instruction per agent."""
    instructions = _final_ack_instructions(workspace, participants)
    if len(instructions) != len(participants):
        return False
    for agent in participants:
        instruction = instructions[agent]
        matches = [
            item for item in receipts
            if item.get("agent_id") == agent
            and item.get("instruction_id") == instruction.instruction_id
            and item.get("kind") == "final_ack"
            and item.get("status") == "completed"
        ]
        if len(matches) != 1:
            return False
        if instruction.access_scope.get("security_mode", "strict") == "strict":
            try:
                expected = load_isolation_evidence(workspace, instruction)
            except Exception:
                return False
            if matches[0].get("isolation_evidence") != expected:
                return False
    return True


def _ensure_final_ack_instructions(
    workspace: Path,
    state: dict[str, Any],
    wake: WakeAdapter,
    result: OrchestrationResult,
    participants: list[str],
) -> bool:
    """Publish final_decision acknowledgement instructions exactly once."""
    changed = False
    issued: dict[str, str] = dict(state.get("final_ack_instruction_ids") or {})
    for agent in participants:
        existing = _final_ack_instructions(workspace, [agent])
        if existing:
            issued[agent] = existing[agent].instruction_id
            if _round_lifecycle(state):
                _modern_instruction_event(workspace, state, existing[agent])
            else:
                _wake_existing(workspace, state, wake, result, agent, existing[agent])
            continue
        if _instruction_exists(workspace, agent, "final_ack"):
            continue
        before = len(result.issued_instruction_ids)
        _issue(workspace, state, wake, result, agent, "final_ack")
        if len(result.issued_instruction_ids) > before:
            issued[agent] = result.issued_instruction_ids[-1]
        changed = True
    if issued != state.get("final_ack_instruction_ids"):
        state["final_ack_instruction_ids"] = issued
        changed = True
    return changed


def _proposal_transition(
    workspace: Path,
    state: dict[str, Any],
    participants: list[str],
    wake: WakeAdapter,
    result: OrchestrationResult,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Atomically merge proposals and publish round-one responses.

    Modern workspaces must not expose a partially merged Markdown, a deleted
    proposal, or an instruction whose state transition was never committed.
    All visible files therefore go through one EventTransaction. Activation
    events are appended only after the transaction has committed.
    """
    document = _main_markdown(workspace)
    if document is None:
        raise WorkflowError("主讨论 Markdown 不存在", E_SCHEMA)
    content = document.read_text(encoding="utf-8")
    sections: list[tuple[str, str]] = []
    manifest_entries: list[dict[str, Any]] = []
    proposal_paths: list[str] = []
    for agent in participants:
        path = _participant_output_path(workspace, agent, "proposals")
        if not path.is_file():
            raise WorkflowError("proposal output missing", E_SCHEMA, agent_id=agent)
        data = path.read_bytes()
        text = data.decode("utf-8").strip()
        relative = path.relative_to(workspace).as_posix()
        sections.append((agent, "> 来源 SHA-256：`%s`\n\n%s" % (hashlib.sha256(data).hexdigest(), text)))
        proposal_paths.append(relative)
        manifest_entries.append({
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "merged_revision": expected_revision + 1,
            "disposed_at": _now(),
        })
    merged_content = _append_section_text(content, "## 二、独立提案", sections).encode("utf-8")

    disposition = state.get("proposal_disposition", "archive")
    manifest_path = ".multiagent/archive/proposals/manifest.json"
    manifest_file = path_in_workspace(workspace, manifest_path)
    existing: list[dict[str, Any]] = []
    if manifest_file.is_file():
        previous = load_json(manifest_file)
        if isinstance(previous, dict) and isinstance(previous.get("entries"), list):
            existing = list(previous["entries"])
    manifest_data = json.dumps({
        "discussion_id": state.get("discussion_id"),
        "disposition": disposition,
        "entries": existing + manifest_entries,
    }, ensure_ascii=False, indent=2).encode("utf-8")

    state = dict(state)
    state["submission_status"] = dict(state.get("submission_status") or {})
    for agent in participants:
        state["submission_status"][agent] = "submitted"
    state["round"] = 1
    state["stage"] = "cross_response"
    state["instruction_queue"] = {agent: [] for agent in participants}
    artifacts: dict[str, bytes] = {
        document.relative_to(workspace).as_posix(): merged_content,
        manifest_path: manifest_data,
    }
    deletions = proposal_paths if disposition == "delete" else []
    instructions: list[Instruction] = []
    discussion_relative = document.relative_to(workspace).as_posix()
    for agent in participants:
        prepared = prepare_inputs(
            workspace,
            agent,
            [(workspace / "project-context.md", "project-context.md"), (document, "discussion.md")],
            instruction_kind="respond",
            source_contents={discussion_relative: merged_content},
        )
        instruction = _new_instruction(
            state, workspace, agent, "respond", round_number=1, prepared_inputs=prepared,
        )
        instructions.append(instruction)
        artifacts.update(prepared.artifacts)
        artifacts.update(_instruction_artifacts(workspace, instruction))
        state["instruction_queue"][agent].append(instruction.instruction_id)
    state["response_status"] = {agent: "pending" for agent in participants}
    # The transaction publishes this Markdown bytestring; bind the snapshot
    # hash in the state that will be committed so the next pass does not see
    # a false content-authority conflict.
    authority = dict(state.get("content_authority") or {})
    authority["discussion_path"] = discussion_relative
    authority["sha256"] = hashlib.sha256(merged_content).hexdigest()
    state["content_authority"] = authority
    event_payload = {
        "stage_before": "independent_proposal",
        "stage_after": "cross_response",
        "round": 1,
        "instruction_ids": [item.instruction_id for item in instructions],
        "proposal_paths": proposal_paths,
        "proposal_disposition": disposition,
    }
    commit_transaction(
        workspace,
        event_type="proposal_merged",
        event_payload=event_payload,
        artifacts=artifacts,
        deletions=deletions,
        expected_revision=expected_revision,
        state_update=state,
    )
    state["revision"] = expected_revision + 1
    for instruction in instructions:
        result.issued_instruction_ids.append(instruction.instruction_id)
        _modern_instruction_event(workspace, state, instruction)
    return state


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
        if _round_lifecycle(state):
            path = workspace / ".multiagent" / "views" / agent / "outputs" / f"round-{int(state.get('round', 1))}" / "交叉回应文档.md"
        sections.append((agent, path.read_text(encoding="utf-8")))
    _append_section(document, "## 三、交叉回应", [
        (agent, "> 来源 SHA-256：`%s`\n\n%s" % (sha256_file(path), text))
        for agent, text in sections
    ])
    _append_section(document, "## 四、结构化决策包", [
        ("候选综合", "以下内容综合独立提案与交叉回应，仅形成候选供 Rainier 审阅；不构成已确认决策。请检查共识、分歧、风险和待选择事项。")
    ])
    _append_section(document, "## 五、候选决策与 Word 审阅", [
        ("候选审阅", "状态：候选、待 Rainier 审阅，不是正式决策。\n\n- 候选 ID：C-0001\n- 候选 Markdown：`.multiagent/deliverables/candidate.md`\n- 候选 Word：`候选决策.docx`\n- 候选 Word 打开后进入 human_review；Rainier 确认并发布正式结论后，才请求参与者停止监测。")
    ])
    if not state.get("candidate_decision_ids"):
        state["candidate_decision_ids"] = ["C-0001"]


def _candidate_transition(
    workspace: Path,
    state: dict[str, Any],
    participants: list[str],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Atomically materialize the modern candidate decision package.

    Candidate Markdown is authoritative content, so its mutation and the
    candidate stage/metadata must share one transaction. Word generation and
    opening remain outside this transaction because they are external side
    effects and are retried from the durable ``candidate_decision`` state.
    """
    document = _main_markdown(workspace)
    if document is None:
        raise WorkflowError("主讨论 Markdown 不存在", E_SCHEMA)
    content = document.read_text(encoding="utf-8")
    sections: list[tuple[str, str]] = []
    current_round = int(state.get("round", 1) or 1)
    for agent in participants:
        path = _participant_output_path(workspace, agent, "responses")
        if _round_lifecycle(state):
            path = workspace / ".multiagent" / "views" / agent / "outputs" / ("round-%d" % current_round) / "交叉回应文档.md"
        if not path.is_file():
            raise WorkflowError("response output missing for candidate", E_SCHEMA, agent_id=agent, round=current_round)
        text = path.read_text(encoding="utf-8").strip()
        sections.append((agent, "> 来源 SHA-256：`%s`\n\n%s" % (sha256_file(path), text)))
    updated = _append_section_text(content, "## 三、交叉回应", sections)
    updated = _append_section_text(updated, "## 四、结构化决策包", [
        ("候选综合", "以下内容综合独立提案与交叉回应，仅形成候选供 Rainier 审阅；不构成已确认决策。请检查共识、分歧、风险和待选择事项。"),
    ])
    updated = _append_section_text(updated, "## 五、候选决策与 Word 审阅", [
        ("候选审阅", "状态：候选、待 Rainier 审阅，不是正式决策。\n\n- 候选 ID：C-0001\n- 候选 Markdown：`.multiagent/deliverables/candidate.md`\n- 候选 Word：`候选决策.docx`\n- 候选 Word 打开后进入 human_review；Rainier 确认并发布正式结论后，才请求参与者停止监测。"),
    ])
    updated_data = updated.encode("utf-8")
    discussion_relative = document.relative_to(workspace).as_posix()
    state = dict(state)
    state["stage"] = "candidate_decision"
    state["candidate_decision_ids"] = list(state.get("candidate_decision_ids") or ["C-0001"])
    if "C-0001" not in state["candidate_decision_ids"]:
        state["candidate_decision_ids"].append("C-0001")
    state["candidate"] = {
        "candidate_id": "C-0001",
        "status": "candidate",
        "source_round": current_round,
        "discussion_path": discussion_relative,
        "discussion_sha256": hashlib.sha256(updated_data).hexdigest(),
        "created_at": _now(),
    }
    authority = dict(state.get("content_authority") or {})
    authority["discussion_path"] = discussion_relative
    authority["sha256"] = hashlib.sha256(updated_data).hexdigest()
    state["content_authority"] = authority
    commit_transaction(
        workspace,
        event_type="candidate_decision_created",
        event_payload={
            "candidate_id": "C-0001",
            "source_round": current_round,
            "discussion_path": discussion_relative,
            "discussion_sha256": hashlib.sha256(updated_data).hexdigest(),
        },
        artifacts={discussion_relative: updated_data},
        expected_revision=expected_revision,
        state_update=state,
    )
    state["revision"] = expected_revision + 1
    return state


def _markdown_section_items(text: str, titles: tuple[str, ...]) -> list[str]:
    """Extract bullet labels from a response section for convergence input."""
    title_pattern = "|".join(re.escape(title) for title in titles)
    match = re.search(r"(?ms)^###\s+(?:[一二三四五六七八九十]+、)?(?:" + title_pattern + r").*?\n(.*?)(?=^###\s+|\Z)", text)
    if not match:
        return []
    values: list[str] = []
    for line in match.group(1).splitlines():
        value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)", "", line).strip()
        if value and not value.startswith("<") and value not in values:
            values.append(value)
    return values


def _response_records_for_round(
    workspace: Path,
    receipts: list[dict[str, Any]],
    participants: list[str],
    round_number: int,
) -> list[AgentResponse]:
    records: list[AgentResponse] = []
    for agent in participants:
        candidates = [
            record for record in receipts
            if record.get("agent_id") == agent
            and record.get("status") == "completed"
            and record.get("kind") == "respond"
            and _response_round(workspace, record) == round_number
        ]
        if not candidates:
            continue
        record = candidates[-1]
        output = path_in_workspace(workspace, str(record.get("output_path", "")))
        if not output.is_file():
            continue
        text = output.read_text(encoding="utf-8")
        records.append(
            AgentResponse(
                agent,
                round_number,
                tuple(_markdown_section_items(text, ("共识点", "共识"))),
                tuple(_markdown_section_items(text, ("分歧点", "分歧"))),
                tuple(_markdown_section_items(text, ("新问题", "待确认问题", "待确认"))),
            )
        )
    return records


def _evaluate_convergence(
    workspace: Path,
    state: dict[str, Any],
    receipts: list[dict[str, Any]],
    participants: list[str],
    current_round: int,
) -> dict[str, Any] | None:
    """Load and schema-check the Coordinator's semantic assessment.

    Completeness is checked by the caller.  The modern workflow must never
    infer semantic convergence from response text or quiet-round counters.
    """
    path = workspace / ".multiagent" / "convergence" / ("round-%d.json" % current_round)
    if not path.is_file():
        return None
    try:
        payload = load_json(path)
        assessment = validate_convergence_assessment(payload, participant_ids=participants)
    except (WorkflowError, ConvergenceSchemaError, TypeError, ValueError) as exc:
        raise WorkflowError(
            "Coordinator 收敛评估不符合固定 JSON schema",
            E_SCHEMA,
            path=str(path),
            round=current_round,
        ) from exc
    if assessment.get("round") != current_round:
        raise WorkflowError(
            "Coordinator 收敛评估 round 与当前轮次不一致",
            E_SCHEMA,
            path=str(path),
            expected_round=current_round,
            actual_round=assessment.get("round"),
        )
    metadata = (state.get("rounds") or {}).get(str(current_round))
    if not isinstance(metadata, dict):
        raise WorkflowError("Coordinator 收敛评估缺少已发布快照元数据", E_SCHEMA, round=current_round)
    expected_path = metadata.get("snapshot_path")
    expected_hash = metadata.get("snapshot_sha256")
    published_revision = metadata.get("snapshot_published_revision")
    if (
        assessment.get("based_on_snapshot_path") != expected_path
        or assessment.get("based_on_snapshot_sha256") != expected_hash
        or not isinstance(published_revision, int)
        or assessment.get("based_on_revision", 0) < published_revision
    ):
        raise WorkflowError(
            "Coordinator 收敛评估未绑定当前已发布 round 快照",
            E_STATE_CONFLICT,
            path=str(path), round=current_round,
        )
    return assessment


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
        recover_transactions(workspace)
        state_path = _state_path(workspace)
        state = load_state(workspace)
        validate_state_shape(state)
        # A crash after a transaction's state/artifacts commit but before its
        # delivery signal must not strand an already-issued instruction.
        reconcile_instruction_events(workspace, state)
        validate_coordinator_execution(state, actor or "", platform_id or "", session_id or "")
        ensure_content_consistency(workspace, state)
        _validate_round_snapshots(workspace, state)
        if state.get("stage") not in {
            "initialized", "independent_proposal", "cross_response", "candidate_decision",
            "human_review", "finalizing", "completed", "user_confirmation",
            "confirmed_decision", "delivered", "monitoring_stopped",
        }:
            raise WorkflowError("旧阶段不能由新编排器推进", "E_PHASE", stage=state.get("stage"))
        result = OrchestrationResult(stage=str(state.get("stage", "unknown")))
        revision_before = int(state.get("revision", 0))
        participants = list(state.get("expected_participants") or [])
        modern_rounds = _round_lifecycle(state)
        for participant in participants:
            _participant_binding(state, participant)
        receipts = _read_receipts(workspace)
        changed = False
        stop_requested_pending = False
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
                            instruction = Instruction.from_dict(load_json(path))
                            if _round_lifecycle(state):
                                _modern_instruction_event(workspace, state, instruction)
                            else:
                                _wake_existing(workspace, state, wake, result, agent, instruction)
                    else:
                        _issue(workspace, state, wake, result, agent, "bootstrap")
                        changed = True

        if state.get("stage") in {"initialized", "independent_proposal"} and _valid_completed_outputs(workspace, state, receipts, participants, {"propose", "repair"}, "proposals"):
            if modern_rounds:
                revision_before = int(state.get("revision", revision_before))
                state = _proposal_transition(
                    workspace, state, participants, wake, result,
                    expected_revision=revision_before,
                )
                revision_before = int(state.get("revision", revision_before + 1))
                changed = False
            else:
                _merge_and_dispose(workspace, state, participants)
                for agent in participants:
                    if not _instruction_exists(workspace, agent, "respond"):
                        _issue(workspace, state, wake, result, agent, "respond")
                state["stage"] = "cross_response"
                changed = True

        if state.get("stage") == "cross_response":
            if not modern_rounds:
                if _valid_completed_outputs(workspace, state, receipts, participants, {"respond"}, "responses"):
                    _build_candidate(workspace, state, participants)
                    state["stage"] = "candidate_decision"
                    changed = True
            else:
                current_round = int(state.get("round", 1) or 1)
                if _valid_completed_outputs(
                    workspace, state, receipts, participants, {"respond"}, "responses", current_round
                ):
                    round_metadata = (state.get("rounds") or {}).get(str(current_round))
                    snapshot_published = isinstance(round_metadata, dict) and (
                        round_metadata.get("status") in {"snapshot_published", "completed"}
                    )
                    if not snapshot_published:
                        # Phase 1: make the complete response round visible as
                        # an immutable public snapshot.  No next-round input,
                        # state.round advance, or semantic decision occurs in
                        # this transaction.
                        state = _publish_round_snapshot(
                            workspace,
                            state,
                            receipts,
                            participants,
                            current_round,
                            expected_revision=revision_before,
                        )
                        revision_before = int(state.get("revision", revision_before + 1))
                        changed = False

                    # Phase 2 starts only after the coordinator has written a
                    # valid assessment against the now-published snapshot.
                    convergence = _evaluate_convergence(
                        workspace, state, receipts, participants, current_round
                    )
                    if convergence is not None:
                        state, has_next_round = _round_transition(
                            workspace,
                            state,
                            receipts,
                            participants,
                            current_round,
                            convergence,
                            wake,
                            result,
                            expected_revision=revision_before,
                        )
                        # The round transition has already committed its own
                        # state/event atomically. Any subsequent candidate
                        # materialization is a separate state transition.
                        revision_before = int(state.get("revision", revision_before + 1))
                        changed = False
                        if not has_next_round:
                            state = _candidate_transition(
                                workspace,
                                state,
                                participants,
                                expected_revision=revision_before,
                            )
                            revision_before = int(state.get("revision", revision_before + 1))
                            changed = False

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

        # Candidate delivery opens the human review window.  Participants keep
        # their monitors alive until the final decision is published.
        if state.get("stage") == "candidate_decision" and modern_rounds:
            delivery = state.get("candidate_delivery")
            if isinstance(delivery, dict) and delivery.get("opened") is True:
                monitoring = dict(state.get("monitoring") or {})
                monitoring.update({"enabled": True, "status": "active"})
                state["monitoring"] = monitoring
                state["stage"] = "human_review"
                changed = True

        # Legacy workspaces retain their original stop-before-confirmation
        # contract. Newly initialized workspaces use the modern human_review
        # -> finalizing -> stop order above.
        if state.get("stage") == "candidate_decision" and not modern_rounds:
            delivery = state.get("candidate_delivery")
            if isinstance(delivery, dict) and delivery.get("opened") is True:
                if _ensure_stop_instructions(workspace, state, wake, result, participants):
                    changed = True
                receipts = _read_receipts(workspace)
                if _all_stop_receipts_completed(workspace, state, receipts, participants):
                    stopped_at = _now()
                    monitoring = dict(state.get("monitoring") or {})
                    monitoring.update({"enabled": False, "status": "stopped", "stopped_at": stopped_at})
                    state["monitoring"] = monitoring
                    state["stage"] = "user_confirmation"
                    changed = True
                else:
                    missing = [agent for agent in participants if not any(
                        record.get("agent_id") == agent and record.get("kind") == "stop" and record.get("status") == "completed"
                        for record in receipts
                    )]
                    result.warnings.append("候选 Word 已打开；等待参与者 stop completed 回执%s。" % ("：" + ", ".join(missing) if missing else ""))

        # A published final decision is acknowledged by every participant
        # before any stop request is issued. Human review therefore keeps all
        # monitors active until Rainier has confirmed the candidate.
        if state.get("stage") == "finalizing" and modern_rounds:
            if _ensure_final_ack_instructions(workspace, state, wake, result, participants):
                changed = True
            receipts = _read_receipts(workspace)
            if _all_final_ack_receipts_completed(workspace, receipts, participants):
                if _ensure_stop_instructions(workspace, state, wake, result, participants):
                    changed = True
                if not state.get("stop_requested_event_id"):
                    state["stop_requested_event_id"] = "stop-requested-%s" % state.get("discussion_id", workspace.name)
                    stop_requested_pending = True
                    changed = True

        # Legacy workspaces retain the pre-round confirmation contract.
        if state.get("stage") == "finalizing" and not modern_rounds:
            if _ensure_stop_instructions(workspace, state, wake, result, participants):
                changed = True

        if state.get("stage") in {"finalizing", "confirmed_decision"}:
            receipts = _read_receipts(workspace)
            if _all_stop_receipts_completed(workspace, state, receipts, participants) and _all_monitors_stopped(workspace, state, participants):
                stopped_at = _now()
                monitoring = dict(state.get("monitoring") or {})
                monitoring.update({"enabled": False, "status": "stopped", "stopped_at": stopped_at})
                state["monitoring"] = monitoring
                automation = dict(state.get("automation") or {})
                automation.update({"enabled": False, "stopped_at": stopped_at})
                state["automation"] = automation
                state["stage"] = "confirmed_decision"
                changed = True

        if state.get("stage") == "confirmed_decision" and not isinstance(state.get("formal_delivery"), dict):
            document = _main_markdown(workspace)
            if document is not None:
                try:
                    code, delivered_state = export_docx.do_export(
                        _state_path(workspace),
                        state,
                        actor or "",
                        workspace,
                        str(document),
                        None,
                        False,
                        open_after=open_candidate,
                        opener=opener,
                        platform_id=platform_id,
                        session_id=session_id,
                        trusted_execution=True,
                    )
                except Exception as exc:
                    code, delivered_state = 1, state
                    result.warnings.append("正式 Word 生成异常：%s" % exc)
                if code == 0:
                    state = delivered_state
                    result.stage = str(state.get("stage", "unknown"))
                    return result
                result.warnings.append("正式 Word 尚未交付，保留 confirmed_decision 状态。")

        if _project_machine_indexes(state, workspace, receipts, participants):
            changed = True

        if changed:
            revision_before = int(state.get("revision", revision_before))
            update_content_hash(workspace, state)
            state["last_orchestrated_at"] = _now()
            transaction = EventTransaction(workspace)
            transaction.commit(
                event_type="stop_requested" if stop_requested_pending else "orchestration_state_changed",
                event_payload={
                    "stage_before": result.stage,
                    "stage_after": state.get("stage"),
                    "round": state.get("round", 0),
                    "issued_instruction_ids": list(result.issued_instruction_ids),
                    "blocking_error_codes": list(result.blocking_error_codes),
                    **({
                        "agent_ids": participants,
                        "instruction_ids": dict((state.get("monitoring") or {}).get("stop_instruction_ids") or {}),
                        "event_id": state.get("stop_requested_event_id"),
                    } if stop_requested_pending else {}),
                },
                expected_revision=revision_before,
                state_update=state,
            )
            state["revision"] = revision_before + 1
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
