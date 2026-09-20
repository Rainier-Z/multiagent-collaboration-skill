#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append-only event stream primitives for the collaboration workspace.

The normal workflow state is intentionally kept in ``.multiagent/state.json``.
This module records the successful state transitions in a separate JSONL audit
stream.  A JSONL record is never edited in place: each record contains its
sequence number, the hash of the preceding record and a hash of itself.  The
chain makes a truncated, reordered or otherwise mismatched stream detectable
before a transaction is allowed to continue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # The scripts directory is also used as a flat import path by the CLI.
    from workflow_core import E_SCHEMA, E_STATE_CONFLICT, WorkflowError, path_in_workspace
except ImportError:  # pragma: no cover - package import fallback
    from .workflow_core import E_SCHEMA, E_STATE_CONFLICT, WorkflowError, path_in_workspace


EVENT_STREAM_RELATIVE_PATH = ".multiagent/audit/events.jsonl"
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class EventStreamError(WorkflowError):
    """Raised when an append-only stream cannot be trusted or extended."""


class EventStream:
    """A hash-chained, append-only JSONL event stream.

    ``EventStream(workspace)`` uses the project-standard
    ``.multiagent/audit/events.jsonl`` path.  Passing a path ending in
    ``.jsonl`` is also supported for small tools and tests.
    """

    def __init__(self, workspace_or_path: str | Path, event_path: str | Path | None = None) -> None:
        if event_path is None:
            candidate = Path(workspace_or_path)
            if candidate.suffix.lower() == ".jsonl":
                self.workspace = candidate.parent.resolve()
                self.path = candidate.resolve()
            else:
                self.workspace = candidate.resolve()
                self.path = self.workspace / EVENT_STREAM_RELATIVE_PATH
        else:
            self.workspace = Path(workspace_or_path).resolve()
            self.path = path_in_workspace(self.workspace, event_path)

    def _read_raw(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise EventStreamError("事件流无法读取", E_STATE_CONFLICT, path=str(self.path), cause=str(exc)) from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise EventStreamError("事件流最后一条记录未完整落盘", E_STATE_CONFLICT, path=str(self.path))
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EventStreamError("事件流包含损坏 JSON", E_SCHEMA, path=str(self.path), line=line_no) from exc
            if not isinstance(value, dict):
                raise EventStreamError("事件记录必须是对象", E_SCHEMA, path=str(self.path), line=line_no)
            records.append(value)
        return records

    @staticmethod
    def _verify_record(record: Mapping[str, Any], sequence: int, previous_hash: str) -> str:
        required = {"sequence", "event_id", "recorded_at", "event_type", "payload", "previous_hash", "event_hash"}
        missing = sorted(required - set(record))
        if missing:
            raise EventStreamError("事件记录缺少字段", E_SCHEMA, sequence=sequence, missing=missing)
        if record.get("sequence") != sequence:
            raise EventStreamError("事件序列不连续", E_STATE_CONFLICT, expected=sequence, actual=record.get("sequence"))
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
            raise EventStreamError("event_id 非法", E_SCHEMA, sequence=sequence)
        if not isinstance(record.get("event_type"), str) or not record["event_type"].strip():
            raise EventStreamError("event_type 必须是非空字符串", E_SCHEMA, sequence=sequence)
        if not isinstance(record.get("payload"), dict):
            raise EventStreamError("事件 payload 必须是对象", E_SCHEMA, sequence=sequence)
        if record.get("previous_hash") != previous_hash:
            raise EventStreamError("事件链 previous_hash 不匹配", E_STATE_CONFLICT, sequence=sequence)
        unsigned = {key: value for key, value in record.items() if key != "event_hash"}
        expected_hash = _digest(unsigned)
        if record.get("event_hash") != expected_hash:
            raise EventStreamError("事件 event_hash 不匹配", E_STATE_CONFLICT, sequence=sequence)
        return expected_hash

    def read(self) -> list[dict[str, Any]]:
        """Read and verify every record, returning independent dictionaries."""
        records = self._read_raw()
        previous = _GENESIS
        seen: set[str] = set()
        for sequence, record in enumerate(records, 1):
            previous = self._verify_record(record, sequence, previous)
            if record["event_id"] in seen:
                raise EventStreamError("事件 event_id 重复", E_STATE_CONFLICT, event_id=record["event_id"])
            seen.add(record["event_id"])
        return records

    def __iter__(self) -> Iterable[dict[str, Any]]:
        return iter(self.read())

    def last(self) -> dict[str, Any] | None:
        records = self.read()
        return records[-1] if records else None

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        transaction_id: str | None = None,
        revision_before: int | None = None,
        revision_after: int | None = None,
        event_id: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Append one event, or return an identical existing event by ID.

        The caller must hold the workflow's state lock when multiple processes
        can append concurrently.  The method still revalidates the full chain
        immediately before appending, so a pre-existing mismatch is never
        silently hidden.
        """
        if not isinstance(event_type, str) or not event_type.strip():
            raise EventStreamError("event_type 必须是非空字符串", E_SCHEMA)
        if not isinstance(payload, Mapping):
            raise EventStreamError("payload 必须是对象", E_SCHEMA)
        # Ensure the payload is JSON-safe before any filesystem mutation.
        try:
            payload_copy = json.loads(json.dumps(dict(payload), ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise EventStreamError("事件 payload 不是可序列化 JSON", E_SCHEMA) from exc
        records = self.read()
        if event_id is not None:
            if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
                raise EventStreamError("event_id 非法", E_SCHEMA, event_id=event_id)
            for existing in records:
                if existing["event_id"] == event_id:
                    candidate = {
                        "event_type": event_type,
                        "payload": payload_copy,
                        "transaction_id": transaction_id,
                        "revision_before": revision_before,
                        "revision_after": revision_after,
                    }
                    comparable = {key: existing.get(key) for key in candidate}
                    if comparable != candidate:
                        raise EventStreamError("event_id 已用于不同事件", E_STATE_CONFLICT, event_id=event_id)
                    return dict(existing)
        event_id = event_id or "evt-" + uuid.uuid4().hex
        previous_hash = records[-1]["event_hash"] if records else _GENESIS
        record: dict[str, Any] = {
            "sequence": len(records) + 1,
            "event_id": event_id,
            "recorded_at": recorded_at or _now(),
            "event_type": event_type,
            "payload": payload_copy,
            "previous_hash": previous_hash,
        }
        if transaction_id is not None:
            record["transaction_id"] = transaction_id
        if revision_before is not None:
            record["revision_before"] = revision_before
        if revision_after is not None:
            record["revision_after"] = revision_after
        record["event_hash"] = _digest(record)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise EventStreamError("事件追加失败", E_STATE_CONFLICT, path=str(self.path), cause=str(exc)) from exc
        return record

    append_event = append

    def verify(self) -> dict[str, Any]:
        records = self.read()
        last = records[-1] if records else None
        return {
            "path": str(self.path),
            "count": len(records),
            "last_event_id": last["event_id"] if last else None,
            "last_revision": last.get("revision_after") if last else None,
            "last_hash": last.get("event_hash") if last else _GENESIS,
        }


EventLog = EventStream


__all__ = ["EVENT_STREAM_RELATIVE_PATH", "EventLog", "EventStream", "EventStreamError"]
