#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recoverable state/event transactions.

The commit protocol is deliberately explicit:

1. validate the current state and caller preconditions;
2. write business artifacts and receipts;
3. append one event to :mod:`events`;
4. atomically publish the next state revision.

Each transaction has a durable journal under
``.multiagent/audit/transactions/<transaction_id>/``.  If a process dies
between these phases, ``recover_transactions`` either rolls back an
incomplete pre-event transaction, completes a state update for an already
appended event, or raises a fail-closed mismatch error.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from events import EventStream, EventStreamError
    from workflow_core import E_SCHEMA, E_STATE_CONFLICT, WorkflowError, path_in_workspace
except ImportError:  # pragma: no cover - package import fallback
    from .events import EventStream, EventStreamError
    from .workflow_core import E_SCHEMA, E_STATE_CONFLICT, WorkflowError, path_in_workspace


TRANSACTION_ROOT = ".multiagent/audit/transactions"
STATE_RELATIVE_PATH = ".multiagent/state.json"
_TX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TERMINAL = {"committed", "rolled_back"}


class TransactionError(WorkflowError):
    """A transaction could not be committed or safely recovered."""


class TransactionRecoveryError(TransactionError):
    """The event/state/artifact boundary is inconsistent and needs review."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value: Any) -> str:
    return _hash_bytes(_canonical(value))


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TransactionError("JSON 文件不存在", E_SCHEMA, path=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise TransactionError("JSON 文件无法解析", E_SCHEMA, path=str(path), line=exc.lineno) from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _content_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return _json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise TransactionError("业务产物不是可写入的 JSON/文本/字节", E_SCHEMA) from exc


def _relative(workspace: Path, value: str | Path) -> str:
    try:
        return path_in_workspace(workspace, value).relative_to(workspace).as_posix()
    except ValueError as exc:
        raise TransactionError("事务路径越出工作区", E_STATE_CONFLICT, path=str(value)) from exc


class EventTransaction:
    """One ordered, journaled event/state transition.

    ``artifacts`` and ``receipts`` are mappings of workspace-relative paths to
    text, bytes or JSON-compatible values. ``deletions`` is an iterable of
    workspace-relative files to remove in the same atomic transition.
    ``state_update`` receives a deep
    copy of the current state and may return a replacement state.  It is also
    valid to pass a mapping, which is merged into the current state.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_path: str | Path = STATE_RELATIVE_PATH,
        event_stream: EventStream | None = None,
        transaction_id: str | None = None,
        fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.state_path = path_in_workspace(self.workspace, state_path)
        self.event_stream = event_stream or EventStream(self.workspace)
        self.transaction_id = transaction_id or "tx-" + uuid.uuid4().hex
        if not _TX_ID_RE.fullmatch(self.transaction_id):
            raise TransactionError("transaction_id 非法", E_SCHEMA, transaction_id=self.transaction_id)
        self.root = path_in_workspace(self.workspace, f"{TRANSACTION_ROOT}/{self.transaction_id}")
        self.staged = self.root / "staged"
        self.backups = self.root / "backups"
        self.journal_path = self.root / "journal.json"
        self.fault_hook = fault_hook

    def _journal(self, value: Mapping[str, Any]) -> None:
        _atomic_bytes(self.journal_path, _json_bytes(dict(value)))

    def _fault(self, phase: str, journal: dict[str, Any]) -> None:
        if self.fault_hook is not None:
            self.fault_hook(phase, dict(journal))

    def _validate_inputs(
        self,
        *,
        event_type: str,
        event_payload: Mapping[str, Any],
        artifacts: Mapping[str, Any],
        receipts: Mapping[str, Any],
        deletions: Iterable[str | Path],
        expected_revision: int | None,
        precondition: Callable[[dict[str, Any]], Any] | None,
        state_update: Callable[[dict[str, Any]], Any] | Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise TransactionError("event_type 必须是非空字符串", E_SCHEMA)
        if not isinstance(event_payload, Mapping):
            raise TransactionError("event_payload 必须是对象", E_SCHEMA)
        try:
            event_payload_copy = json.loads(json.dumps(dict(event_payload), ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise TransactionError("event_payload 不是可序列化 JSON", E_SCHEMA) from exc
        state = _load_json(self.state_path)
        if not isinstance(state, dict):
            raise TransactionError("state.json 根节点必须是对象", E_SCHEMA)
        revision = state.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise TransactionError("state revision 必须是非负整数", E_SCHEMA, revision=revision)
        if expected_revision is not None and revision != expected_revision:
            raise TransactionError("state revision 与事务前置条件不匹配", E_STATE_CONFLICT,
                                   expected=expected_revision, actual=revision)
        # Reading here verifies the existing chain before any visible output.
        self.event_stream.read()
        if precondition is not None:
            try:
                result = precondition(copy.deepcopy(state))
            except WorkflowError:
                raise
            except Exception as exc:
                raise TransactionError("事务前置条件验证失败", E_STATE_CONFLICT, cause=str(exc)) from exc
            if result is False:
                raise TransactionError("事务前置条件未满足", E_STATE_CONFLICT)
        next_state = copy.deepcopy(state)
        if state_update is None:
            pass
        elif callable(state_update):
            try:
                candidate = state_update(copy.deepcopy(state))
            except WorkflowError:
                raise
            except Exception as exc:
                raise TransactionError("事务 state 更新计算失败", E_STATE_CONFLICT, cause=str(exc)) from exc
            if candidate is not None:
                if not isinstance(candidate, dict):
                    raise TransactionError("state_update 必须返回对象", E_SCHEMA)
                next_state = candidate
        elif isinstance(state_update, Mapping):
            next_state.update(copy.deepcopy(dict(state_update)))
        else:
            raise TransactionError("state_update 类型非法", E_SCHEMA)
        next_state["revision"] = revision + 1
        if not isinstance(artifacts, Mapping) or not isinstance(receipts, Mapping):
            raise TransactionError("artifacts/receipts 必须是路径到内容的对象", E_SCHEMA)
        operations: list[dict[str, Any]] = []
        occupied: set[str] = set()
        forbidden = {
            self.state_path.relative_to(self.workspace).as_posix(),
            self.event_stream.path.relative_to(self.workspace).as_posix()
            if self.event_stream.path.is_relative_to(self.workspace) else "",
        }
        for kind, values in (("artifact", artifacts), ("receipt", receipts)):
            for target, content in values.items():
                relative = _relative(self.workspace, target)
                if relative in forbidden:
                    raise TransactionError("业务写入不得覆盖 state 或事件流", E_STATE_CONFLICT, path=relative)
                if relative in occupied:
                    raise TransactionError("事务写入目标重复", E_STATE_CONFLICT, path=relative)
                occupied.add(relative)
                data = _content_bytes(content)
                operations.append({
                    "kind": kind,
                    "target": relative,
                    "data_sha256": _hash_bytes(data),
                    "data_size": len(data),
                })
        if isinstance(deletions, (str, bytes)):
            raise TransactionError("deletions 必须是路径序列，不能是字符串", E_SCHEMA)
        for target in deletions:
            relative = _relative(self.workspace, target)
            if relative in forbidden:
                raise TransactionError("业务删除不得删除 state 或事件流", E_STATE_CONFLICT, path=relative)
            if relative in occupied:
                raise TransactionError("事务写入/删除目标重复", E_STATE_CONFLICT, path=relative)
            occupied.add(relative)
            operations.append({"kind": "delete", "target": relative})
        return state, next_state, operations

    def commit(
        self,
        *,
        event_type: str,
        event_payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        artifacts: Mapping[str, Any] | None = None,
        receipts: Mapping[str, Any] | None = None,
        deletions: Iterable[str | Path] | None = None,
        precondition: Callable[[dict[str, Any]], Any] | None = None,
        expected_revision: int | None = None,
        state_update: Callable[[dict[str, Any]], Any] | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit a transaction in the documented four-phase order."""
        event_payload = event_payload or {}
        artifacts = artifacts or {}
        receipts = receipts or {}
        deletions = tuple(deletions or ())
        state, next_state, operations = self._validate_inputs(
            event_type=event_type, event_payload=event_payload, artifacts=artifacts,
            receipts=receipts, deletions=deletions, expected_revision=expected_revision,
            precondition=precondition, state_update=state_update)
        if self.journal_path.exists():
            existing = _load_json(self.journal_path)
            if existing.get("status") == "committed":
                return {"transaction_id": self.transaction_id, "status": "committed",
                        "event": existing.get("event"), "state_revision": next_state["revision"],
                        "idempotent": True}
            raise TransactionError("transaction_id 已存在且未完成", E_STATE_CONFLICT,
                                   transaction_id=self.transaction_id)
        self.staged.mkdir(parents=True, exist_ok=False)
        self.backups.mkdir(parents=True, exist_ok=False)
        before_hash = _hash_json(state)
        after_hash = _hash_json(next_state)
        journal: dict[str, Any] = {
            "schema_version": "1.0",
            "transaction_id": self.transaction_id,
            "status": "preconditions_validated",
            "workspace": str(self.workspace),
            "state_path": self.state_path.relative_to(self.workspace).as_posix(),
            "revision_before": state["revision"],
            "revision_after": next_state["revision"],
            "state_before_sha256": before_hash,
            "state_after_sha256": after_hash,
            "state_after": next_state,
            "event_type": event_type,
            "event_payload": dict(event_payload),
            "operations": operations,
            "applied": 0,
            "event": None,
        }
        self._journal(journal)
        self._fault("preconditions_validated", journal)
        try:
            # Stage bytes after precondition validation, before first visible output.
            values_by_target = {**dict(artifacts), **dict(receipts)}
            for operation in operations:
                if operation["kind"] == "delete":
                    continue
                target = operation["target"]
                staged = self.staged / target
                data = _content_bytes(values_by_target[target])
                _atomic_bytes(staged, data)
                operation["staged"] = target
            journal["status"] = "staging"
            self._journal(journal)
            self._fault("staging", journal)
            for index, operation in enumerate(operations, 1):
                target = path_in_workspace(self.workspace, operation["target"])
                backup = self.backups / operation["target"]
                if target.exists():
                    if not target.is_file():
                        raise TransactionError("事务目标不是普通文件", E_STATE_CONFLICT, path=operation["target"])
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    operation["before_exists"] = True
                    operation["before_sha256"] = _hash_bytes(target.read_bytes())
                else:
                    operation["before_exists"] = False
                    operation["before_sha256"] = None
                if operation["kind"] == "delete":
                    if target.exists():
                        if not target.is_file():
                            raise TransactionError("事务删除目标不是普通文件", E_STATE_CONFLICT, path=operation["target"])
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(self.staged / operation["staged"], target)
                operation["applied"] = True
                journal["applied"] = index
                journal["status"] = "artifacts_written"
                self._journal(journal)
                self._fault("artifacts_written", journal)
            event = self.event_stream.append(
                event_type,
                {**dict(event_payload), "transaction_id": self.transaction_id,
                 "artifact_paths": [item["target"] for item in operations if item["kind"] != "delete"],
                 "deleted_paths": [item["target"] for item in operations if item["kind"] == "delete"]},
                transaction_id=self.transaction_id,
                revision_before=state["revision"], revision_after=next_state["revision"],
                event_id=event_id,
            )
            journal["event"] = event
            journal["status"] = "event_appended"
            self._journal(journal)
            self._fault("event_appended", journal)
            _atomic_bytes(self.state_path, _json_bytes(next_state))
            journal["status"] = "state_updated"
            self._journal(journal)
            self._fault("state_updated", journal)
            journal["status"] = "committed"
            self._journal(journal)
            return {"transaction_id": self.transaction_id, "status": "committed",
                    "event": event, "state_revision": next_state["revision"], "idempotent": False}
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit deliberately leave the journal for recovery.
            if not isinstance(exc, Exception):
                raise
            phase = journal.get("status")
            if phase in {"event_appended", "state_updated"}:
                journal["status"] = "recovery_required"
                journal["error"] = str(exc)
                self._journal(journal)
                raise TransactionRecoveryError("事件已追加但 state 未完成，需恢复事务", E_STATE_CONFLICT,
                                                transaction_id=self.transaction_id, cause=str(exc)) from exc
            self._rollback(journal)
            raise TransactionError("事务提交失败并已回滚", E_STATE_CONFLICT,
                                   transaction_id=self.transaction_id, cause=str(exc)) from exc

    def _rollback(self, journal: dict[str, Any]) -> None:
        errors: list[str] = []
        for operation in reversed(journal.get("operations", [])[: int(journal.get("applied", 0))]):
            target = path_in_workspace(self.workspace, operation["target"])
            backup = self.backups / operation["target"]
            try:
                if operation.get("before_exists"):
                    _atomic_bytes(target, backup.read_bytes())
                else:
                    target.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"{operation['target']}: {exc}")
        journal["status"] = "rollback_required" if errors else "rolled_back"
        if errors:
            journal["rollback_errors"] = errors
        self._journal(journal)
        if errors:
            raise TransactionRecoveryError("事务回滚不完整", E_STATE_CONFLICT,
                                           transaction_id=self.transaction_id, errors=errors)


Transaction = EventTransaction
TransactionManager = EventTransaction


def _restore_artifacts(workspace: Path, journal: Mapping[str, Any], staged: Path, backups: Path) -> None:
    for operation in journal.get("operations", []):
        if not operation.get("applied"):
            continue
        target = path_in_workspace(workspace, operation["target"])
        backup = backups / operation["target"]
        if operation.get("before_exists"):
            _atomic_bytes(target, backup.read_bytes())
        else:
            target.unlink(missing_ok=True)


def recover_transactions(
    workspace: str | Path,
    *,
    state_path: str | Path = STATE_RELATIVE_PATH,
    event_stream: EventStream | None = None,
) -> list[dict[str, Any]]:
    """Recover unfinished transactions, failing closed on mixed state.

    A transaction with no event is rolled back.  A transaction with an event
    and the pre-transaction state is completed by publishing its recorded
    state snapshot.  Any other combination is a mismatch and raises
    ``TransactionRecoveryError``.
    """
    root = Path(workspace).resolve()
    stream = event_stream or EventStream(root)
    records = stream.read()
    by_tx: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        txid = record.get("transaction_id")
        if isinstance(txid, str):
            by_tx.setdefault(txid, []).append(record)
    transaction_root = path_in_workspace(root, TRANSACTION_ROOT)
    if not transaction_root.is_dir():
        return []
    state_file = path_in_workspace(root, state_path)
    recovered: list[dict[str, Any]] = []
    for journal_path in sorted(transaction_root.glob("*/journal.json")):
        journal = _load_json(journal_path)
        status = journal.get("status")
        if status in _TERMINAL:
            continue
        txid = journal.get("transaction_id")
        if not isinstance(txid, str):
            raise TransactionRecoveryError("事务日志缺少 transaction_id", E_SCHEMA, path=str(journal_path))
        state = _load_json(state_file)
        state_hash = _hash_json(state)
        before = journal.get("state_before_sha256")
        after = journal.get("state_after_sha256")
        matches = by_tx.get(txid, [])
        if len(matches) > 1:
            raise TransactionRecoveryError("事务出现多个事件", E_STATE_CONFLICT, transaction_id=txid)
        event = matches[0] if matches else None
        if event is None:
            if state_hash == after:
                raise TransactionRecoveryError("state 已更新但事件缺失", E_STATE_CONFLICT, transaction_id=txid)
            _restore_artifacts(root, journal, journal_path.parent / "staged", journal_path.parent / "backups")
            journal["status"] = "rolled_back"
            journal["recovered_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_bytes(journal_path, _json_bytes(journal))
            recovered.append({"transaction_id": txid, "action": "rolled_back"})
            continue
        if event.get("revision_before") != journal.get("revision_before") or event.get("revision_after") != journal.get("revision_after"):
            raise TransactionRecoveryError("事件 revision 与事务日志不匹配", E_STATE_CONFLICT, transaction_id=txid)
        if state_hash == after:
            journal["status"] = "committed"
            journal["recovered_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_bytes(journal_path, _json_bytes(journal))
            recovered.append({"transaction_id": txid, "action": "marked_committed"})
            continue
        if state_hash != before:
            raise TransactionRecoveryError("state 与事务前后快照均不匹配", E_STATE_CONFLICT,
                                           transaction_id=txid, actual=state_hash, expected_before=before, expected_after=after)
        # Ensure every visible output is present before publishing state.
        for operation in journal.get("operations", []):
            if not operation.get("applied"):
                staged = journal_path.parent / "staged" / operation["target"]
                target = path_in_workspace(root, operation["target"])
                if operation.get("kind") == "delete":
                    target.unlink(missing_ok=True)
                else:
                    if not staged.exists():
                        raise TransactionRecoveryError("事务缺少可恢复产物", E_STATE_CONFLICT, transaction_id=txid, path=operation["target"])
                    _atomic_bytes(target, staged.read_bytes())
            target = path_in_workspace(root, operation["target"])
            if operation.get("kind") == "delete":
                if target.exists():
                    raise TransactionRecoveryError("事务删除产物仍存在", E_STATE_CONFLICT, transaction_id=txid, path=operation["target"])
                continue
            if not target.is_file() or _hash_bytes(target.read_bytes()) != operation.get("data_sha256"):
                raise TransactionRecoveryError("事务产物哈希失配", E_STATE_CONFLICT, transaction_id=txid, path=operation["target"])
        _atomic_bytes(state_file, _json_bytes(journal["state_after"]))
        journal["status"] = "committed"
        journal["recovered_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_bytes(journal_path, _json_bytes(journal))
        recovered.append({"transaction_id": txid, "action": "completed_state_update"})
    return recovered


def commit_transaction(
    workspace: str | Path,
    *,
    event_type: str,
    event_payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    artifacts: Mapping[str, Any] | None = None,
    receipts: Mapping[str, Any] | None = None,
    deletions: Iterable[str | Path] | None = None,
    precondition: Callable[[dict[str, Any]], Any] | None = None,
    expected_revision: int | None = None,
    state_update: Callable[[dict[str, Any]], Any] | Mapping[str, Any] | None = None,
    transaction_id: str | None = None,
    fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Convenience function for the common one-shot transaction case."""
    tx = EventTransaction(workspace, transaction_id=transaction_id, fault_hook=fault_hook)
    return tx.commit(event_type=event_type, event_payload=event_payload, event_id=event_id,
                     artifacts=artifacts, receipts=receipts,
                     deletions=deletions,
                     precondition=precondition, expected_revision=expected_revision,
                     state_update=state_update)


def commit_round_transition(
    workspace: str | Path,
    *,
    round_number: int,
    snapshot_path: str | Path,
    snapshot: Any,
    next_round_instructions: Mapping[str | Path, Any] | None = None,
    receipts: Mapping[str | Path, Any] | None = None,
    deletions: Iterable[str | Path] | None = None,
    state_update: Callable[[dict[str, Any]], Any] | Mapping[str, Any] | None = None,
    expected_revision: int | None = None,
    transaction_id: str | None = None,
    fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
    event_type: str = "round.completed",
    event_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically publish a completed round and next-round instructions."""
    artifacts: dict[str | Path, Any] = {snapshot_path: snapshot}
    artifacts.update(next_round_instructions or {})
    return commit_transaction(
        workspace,
        event_type=event_type,
        event_payload={
            "round": round_number,
            "snapshot_path": str(snapshot_path),
            **dict(event_payload or {}),
        },
        artifacts=artifacts,
        receipts=receipts,
        deletions=deletions,
        state_update=state_update,
        expected_revision=expected_revision,
        transaction_id=transaction_id,
        fault_hook=fault_hook,
    )


__all__ = [
    "EventTransaction", "Transaction", "TransactionError", "TransactionManager",
    "TransactionRecoveryError", "commit_transaction", "commit_round_transition", "recover_transactions",
]
