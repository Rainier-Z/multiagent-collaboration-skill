#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent event consumer for one collaboration participant.

``monitor_discussion.py`` remains a deliberately read-only, one-shot file
sensor.  This module is the small runtime loop that turns the append-only
event stream into activation attempts for an already-running participant.
It does not advance the workflow or create instructions; it only consumes
events addressed to one agent, calls an injected :class:`ActivationBridge`,
and persists a cursor plus an activation outcome.

The cursor is advanced only after an event has a durable activation record.
An event that already has a durable record is never activated again.  This
is intentionally at-least-once for an external platform, with a durable
idempotency fence: a process crash during the external call leaves the event
in ``pending`` and the restarted monitor reports ``activation_failed`` instead
of guessing that it is safe to send a duplicate wake request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:  # CLI execution: scripts is on sys.path.
    from events import EventStream, EventStreamError
    from workflow_core import WorkflowError, atomic_write_json, path_in_workspace
except ImportError:  # pragma: no cover - package import fallback.
    from .events import EventStream, EventStreamError
    from .workflow_core import WorkflowError, atomic_write_json, path_in_workspace

from adapters.common.wake_protocol import (
    ActivationBridge,
    ActivationResult,
    WakeRequest,
    manual_activation_required,
)


CURSOR_RELATIVE = ".multiagent/monitors/{agent_id}/cursor.json"
RESULTS_RELATIVE = ".multiagent/monitors/{agent_id}/activation-results.json"
INTERESTING_EVENTS = frozenset({
    "instruction_issued",
    "final_decision_published",
    "stop_requested",
})
_TERMINAL_RESULTS = frozenset({"activated", "manual_activation_required", "activation_failed"})


class MonitorError(RuntimeError):
    """A monitor cannot safely continue without losing event evidence."""


@dataclass(frozen=True)
class Cursor:
    last_sequence: int = 0
    last_event_id: str | None = None
    last_event_hash: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "Cursor":
        if value is None:
            return cls()
        sequence = value.get("last_sequence", 0)
        if not isinstance(sequence, int) or sequence < 0:
            raise MonitorError("cursor.last_sequence must be a non-negative integer")
        event_id = value.get("last_event_id")
        event_hash = value.get("last_event_hash")
        if sequence == 0 and (event_id is not None or event_hash is not None):
            raise MonitorError("empty cursor cannot contain an event identity")
        if sequence > 0 and (not isinstance(event_id, str) or not isinstance(event_hash, str)):
            raise MonitorError("non-empty cursor must contain event identity")
        return cls(sequence, event_id, event_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_sequence": self.last_sequence,
            "last_event_id": self.last_event_id,
            "last_event_hash": self.last_event_hash,
        }


@dataclass(frozen=True)
class ActivationRecord:
    event_id: str
    sequence: int
    event_type: str
    status: str
    detail: str
    evidence: str
    recorded_at: str
    instruction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
            "recorded_at": self.recorded_at,
            "instruction_id": self.instruction_id,
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"invalid monitor JSON: {path}") from exc
    if not isinstance(value, dict):
        raise MonitorError(f"monitor JSON must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(path, dict(value))


class ParticipantMonitor:
    """Durable event consumer for exactly one participant identity."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        agent_id: str,
        bridge: ActivationBridge,
        *,
        clock: Callable[[], str] = _now,
    ) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise MonitorError("agent_id is required")
        self.workspace = Path(workspace).resolve()
        self.agent_id = agent_id
        self.bridge = bridge
        self.clock = clock
        self.cursor_path = path_in_workspace(
            self.workspace, CURSOR_RELATIVE.format(agent_id=agent_id)
        )
        self.results_path = path_in_workspace(
            self.workspace, RESULTS_RELATIVE.format(agent_id=agent_id)
        )
        self._cursor = Cursor.from_dict(_load_object(self.cursor_path))
        self._results = self._load_results()

    @property
    def cursor(self) -> Cursor:
        return self._cursor

    @property
    def activation_results(self) -> tuple[ActivationRecord, ...]:
        return tuple(self._results.values())

    def _load_results(self) -> dict[str, ActivationRecord]:
        payload = _load_object(self.results_path)
        if payload is None:
            return {}
        raw = payload.get("results", {})
        if not isinstance(raw, dict):
            raise MonitorError("activation-results.results must be an object")
        results: dict[str, ActivationRecord] = {}
        for event_id, value in raw.items():
            if not isinstance(event_id, str) or not isinstance(value, dict):
                raise MonitorError("activation result record is malformed")
            record = ActivationRecord(
                event_id=event_id,
                sequence=int(value.get("sequence", 0)),
                event_type=str(value.get("event_type", "")),
                status=str(value.get("status", "")),
                detail=str(value.get("detail", "")),
                evidence=str(value.get("evidence", "")),
                recorded_at=str(value.get("recorded_at", "")),
                instruction_id=value.get("instruction_id") if isinstance(value.get("instruction_id"), str) else None,
            )
            if record.status not in _TERMINAL_RESULTS:
                raise MonitorError("activation result status is not terminal")
            results[event_id] = record
        return results

    def _persist_results(self) -> None:
        _write_json(
            self.results_path,
            {
                "schema_version": "1.0",
                "agent_id": self.agent_id,
                "results": {key: value.to_dict() for key, value in self._results.items()},
            },
        )

    def _persist_cursor(self) -> None:
        _write_json(
            self.cursor_path,
            {
                "schema_version": "1.0",
                "agent_id": self.agent_id,
                **self._cursor.to_dict(),
                "updated_at": self.clock(),
            },
        )

    def _validate_cursor(self, records: list[dict[str, Any]]) -> None:
        if self._cursor.last_sequence == 0:
            return
        if self._cursor.last_sequence > len(records):
            raise MonitorError("event stream was truncated after cursor")
        record = records[self._cursor.last_sequence - 1]
        if (
            record.get("event_id") != self._cursor.last_event_id
            or record.get("event_hash") != self._cursor.last_event_hash
        ):
            raise MonitorError("cursor does not match the event stream")

    def _is_targeted(self, event: Mapping[str, Any]) -> bool:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return False
        event_type = event.get("event_type")
        if event_type == "instruction_issued":
            return payload.get("agent_id") == self.agent_id
        target = payload.get("agent_id")
        if target is None:
            targets = payload.get("agent_ids") or payload.get("targets")
            if isinstance(targets, list):
                return self.agent_id in targets
            # Final decision is a broadcast unless explicitly restricted.
            return event_type in {"final_decision_published", "stop_requested"}
        return target == self.agent_id

    def _instruction_for(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None
        instruction_id = payload.get("instruction_id")
        if not isinstance(instruction_id, str):
            return None
        root = self.workspace / ".multiagent" / "instructions" / self.agent_id
        if not root.is_dir():
            return None
        for path in root.glob("*.json"):
            value = _load_object(path)
            if isinstance(value, dict) and value.get("instruction_id") == instruction_id:
                return value
        return None

    def _request_for(self, event: Mapping[str, Any]) -> WakeRequest:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        instruction = self._instruction_for(event)
        state = _load_object(self.workspace / ".multiagent" / "state.json") or {}
        bindings = state.get("participant_bindings")
        binding = bindings.get(self.agent_id) if isinstance(bindings, Mapping) else None
        if not isinstance(binding, Mapping):
            binding = {}
        source = instruction or payload
        event_id = str(event.get("event_id", "event"))
        instruction_id = str(source.get("instruction_id") or f"event-{event_id}")
        runtime_version = str(source.get("runtime_version") or state.get("runtime_version") or "1.0.0")
        platform_id = str(source.get("platform_id") or binding.get("platform_id") or "unknown")
        session_id = str(source.get("session_id") or binding.get("session_id") or "unknown")
        return WakeRequest(
            self.workspace,
            self.agent_id,
            instruction_id,
            runtime_version,
            platform_id,
            session_id,
        )

    def _activate(self, event: Mapping[str, Any]) -> ActivationRecord:
        event_id = str(event.get("event_id"))
        try:
            result = self.bridge.activate(self._request_for(event))
        except Exception as exc:  # An adapter failure is an observable terminal result.
            result = ActivationResult("activation_failed", f"bridge raised {type(exc).__name__}")
        record = ActivationRecord(
            event_id=event_id,
            sequence=int(event.get("sequence", 0)),
            event_type=str(event.get("event_type", "")),
            status=result.status,
            detail=result.detail,
            evidence=result.evidence,
            recorded_at=self.clock(),
            instruction_id=self._request_for(event).instruction_id,
        )
        self._results[event_id] = record
        self._persist_results()
        return record

    def poll(self) -> list[ActivationRecord]:
        """Consume newly appended targeted events and return new outcomes."""
        try:
            records = EventStream(self.workspace).read()
        except EventStreamError as exc:
            raise MonitorError(str(exc)) from exc
        self._validate_cursor(records)
        fresh: list[ActivationRecord] = []
        for event in records[self._cursor.last_sequence:]:
            sequence = int(event.get("sequence", 0))
            if event.get("event_type") in INTERESTING_EVENTS and self._is_targeted(event):
                event_id = str(event.get("event_id"))
                if event_id not in self._results:
                    fresh.append(self._activate(event))
            self._cursor = Cursor(sequence, str(event.get("event_id")), str(event.get("event_hash")))
            self._persist_cursor()
        return fresh

    def run_forever(self, interval_seconds: float = 2.0, *, stop_when_requested: bool = False) -> None:
        """Run until interrupted, optionally ending after a targeted stop event."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            results = self.poll()
            if stop_when_requested and any(
                item.event_type == "stop_requested" and item.status == "activated"
                for item in results
            ):
                return
            time.sleep(interval_seconds)


def _adapter_for(platform: str) -> ActivationBridge:
    if platform == "claude-code":
        from adapters.claude.wake_adapter import ClaudeCodeWakeAdapter
        return ClaudeCodeWakeAdapter()
    if platform == "codex":
        from adapters.codex.wake_adapter import CodexWakeAdapter
        return CodexWakeAdapter()
    if platform == "openclaw":
        from adapters.openclaw.wake_adapter import OpenClawWakeAdapter
        return OpenClawWakeAdapter()
    raise ValueError(f"unsupported platform: {platform}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one persistent participant event-monitor pass")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--platform", choices=("claude-code", "codex", "openclaw"), required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop-when-requested", action="store_true")
    args = parser.parse_args(argv)
    try:
        monitor = ParticipantMonitor(args.workspace, args.agent_id, _adapter_for(args.platform))
        results = monitor.poll() if args.once else (monitor.run_forever(args.interval, stop_when_requested=args.stop_when_requested) or [])
        for result in results:
            print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, MonitorError, WorkflowError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
