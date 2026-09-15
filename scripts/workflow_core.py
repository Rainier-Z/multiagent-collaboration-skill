#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可靠的多 Agent 协同工作流共享内核。

Markdown 是内容权威，``state.json`` 是流程权威。本模块只提供确定性、
可审计的文件与状态原语；它不会调用模型，也不会把文件监测误称为唤醒机制。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable


PROTOCOL_VERSION = "1.0"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
PARTICIPANT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
INSTRUCTION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_BUSY = 4
EXIT_CONFIRMATION_REQUIRED = 5
EXIT_BLOCKED = 6

E_SCHEMA = "E_SCHEMA"
E_PATH_SCOPE = "E_PATH_SCOPE"
E_HASH = "E_HASH"
E_PHASE = "E_PHASE"
E_STATE_CONFLICT = "E_STATE_CONFLICT"
E_RUNTIME_VERSION = "E_RUNTIME_VERSION"
E_OUTPUT_FORMAT = "E_OUTPUT_FORMAT"
E_RETRY_EXHAUSTED = "E_RETRY_EXHAUSTED"
E_PLATFORM_UNAVAILABLE = "E_PLATFORM_UNAVAILABLE"
E_SEMANTIC_DECISION = "E_SEMANTIC_DECISION"
E_COORDINATOR_BINDING = "E_COORDINATOR_BINDING"
E_ISOLATION_UNVERIFIED = "E_ISOLATION_UNVERIFIED"

VALID_STAGES = {
    "initialized", "independent_proposal", "proposals_complete",
    "cross_response", "candidate_decision", "user_confirmation",
    "confirmed_decision", "delivered", "monitoring_stopped",
}


class WorkflowError(RuntimeError):
    """带稳定机器错误码、可序列化细节的工作流异常。"""

    def __init__(self, message: str, code: str | int = E_STATE_CONFLICT, **details: Any) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")


def iso_now() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="milliseconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def emit(status: str, **payload: Any) -> None:
    print(json.dumps({"status": status, **payload}, ensure_ascii=False, sort_keys=True))


def fail(error: Exception) -> int:
    if isinstance(error, WorkflowError):
        emit("ERROR", error_code=error.code, message=str(error), details=error.details)
        return _exit_code(error.code)
    emit("ERROR", error_code="E_RUNTIME", message=str(error), details={})
    return EXIT_RUNTIME


def _exit_code(code: str | int) -> int:
    if isinstance(code, int):
        return code
    return {
        E_PATH_SCOPE: EXIT_USAGE,
        E_SCHEMA: EXIT_CONFLICT,
        E_HASH: EXIT_CONFLICT,
        E_PHASE: EXIT_CONFLICT,
        E_STATE_CONFLICT: EXIT_CONFLICT,
        E_RUNTIME_VERSION: EXIT_CONFLICT,
        E_OUTPUT_FORMAT: EXIT_CONFLICT,
        E_RETRY_EXHAUSTED: EXIT_BLOCKED,
        E_PLATFORM_UNAVAILABLE: EXIT_BLOCKED,
        E_SEMANTIC_DECISION: EXIT_CONFIRMATION_REQUIRED,
        E_COORDINATOR_BINDING: EXIT_BLOCKED,
        E_ISOLATION_UNVERIFIED: EXIT_BLOCKED,
    }.get(code, EXIT_RUNTIME)


def load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkflowError("JSON 文件不存在", E_SCHEMA, path=str(target)) from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError("JSON 文件无法解析", E_SCHEMA, path=str(target), line=exc.lineno) from exc
    if not isinstance(value, dict):
        raise WorkflowError("JSON 顶层必须是对象", E_SCHEMA, path=str(target))
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    _atomic_write(Path(path), text.encode("utf-8"))


def atomic_write_json(path: str | Path, value: dict[str, Any]) -> None:
    atomic_write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def path_in_workspace(workspace: str | Path, relative: str | Path) -> Path:
    """将相对路径收敛到 workspace，拒绝绝对路径、.. 逃逸与符号链接逃逸。"""
    root = Path(workspace).resolve()
    candidate_input = Path(relative)
    if candidate_input.is_absolute():
        raise WorkflowError("路径必须相对工作区", E_PATH_SCOPE, path=str(relative))
    target = (root / candidate_input).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkflowError("路径越出工作区", E_PATH_SCOPE, path=str(relative), workspace=str(root)) from exc
    return target


def safe_relative_path(workspace: str | Path, path: str | Path) -> str:
    root = Path(workspace).resolve()
    target = Path(path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkflowError("路径越出工作区", E_PATH_SCOPE, path=str(path), workspace=str(root)) from exc


def validate_participant_id(participant: str) -> str:
    value = participant.strip()
    if not PARTICIPANT_RE.fullmatch(value):
        raise WorkflowError("参与者 ID 非法", E_SCHEMA, participant=participant)
    return value


def normalize_participants(values: Iterable[str], *, lowercase: bool = False) -> list[str]:
    participants = [validate_participant_id(v.lower() if lowercase else v) for v in values if v.strip()]
    if not participants:
        raise WorkflowError("参与者名单不能为空", E_SCHEMA)
    if len(participants) != len(set(participants)):
        raise WorkflowError("参与者名单存在重复身份", E_SCHEMA)
    return participants


class StateLock(AbstractContextManager["StateLock"]):
    """基于原子目录创建的跨进程锁，退出时只清理自己创建的锁。"""

    def __init__(self, workspace: str | Path, timeout_seconds: float = 5.0) -> None:
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / ".multiagent" / ".state.lock"
        self.timeout_seconds = timeout_seconds
        self.acquired = False

    def __enter__(self) -> "StateLock":
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                os.mkdir(self.path)
                self.acquired = True
                atomic_write_json(self.path / "owner.json", {"pid": os.getpid(), "acquired_at": iso_now()})
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise WorkflowError("状态正被另一动作占用", E_STATE_CONFLICT, lock_path=str(self.path))
                time.sleep(0.05)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.acquired:
            try:
                (self.path / "owner.json").unlink(missing_ok=True)
                self.path.rmdir()
            finally:
                self.acquired = False
        return None


def validate_state_shape(state: dict[str, Any]) -> None:
    required = {
        "protocol_version", "discussion_id", "stage", "expected_participants",
        "submission_status", "response_status", "coordinator", "revision",
    }
    missing = sorted(required - set(state))
    if missing:
        raise WorkflowError("state.json 缺少必填字段", E_SCHEMA, missing=missing)
    if state["protocol_version"] != PROTOCOL_VERSION:
        raise WorkflowError("protocol_version 不兼容", E_SCHEMA, actual=state["protocol_version"], expected=PROTOCOL_VERSION)
    if state["stage"] not in VALID_STAGES:
        raise WorkflowError("stage 非法", E_PHASE, stage=state["stage"])
    participants = state["expected_participants"]
    if not isinstance(participants, list):
        raise WorkflowError("expected_participants 必须是列表", E_SCHEMA)
    normalized = normalize_participants(participants)
    if set(state["submission_status"]) != set(normalized) or set(state["response_status"]) != set(normalized):
        raise WorkflowError("提交状态键集必须与参与者名单一致", E_SCHEMA)
    if state["coordinator"] not in normalized:
        raise WorkflowError("coordinator 不在参与者名单内", E_SCHEMA)
    binding = state.get("coordinator_binding")
    if not isinstance(binding, dict):
        raise WorkflowError("state.json 缺少 coordinator_binding", E_SCHEMA)
    if (
        binding.get("agent_id") != state["coordinator"]
        or binding.get("role") != "coordinator"
        or not isinstance(binding.get("platform_id"), str)
        or not binding["platform_id"].strip()
        or not isinstance(binding.get("session_id"), str)
        or not binding["session_id"].strip()
    ):
        raise WorkflowError("coordinator_binding 身份字段非法", E_SCHEMA)
    if not isinstance(state["revision"], int) or state["revision"] < 1:
        raise WorkflowError("revision 必须为正整数", E_SCHEMA)


def validate_coordinator_execution(
    state: dict[str, Any],
    agent_id: str,
    platform_id: str,
    session_id: str,
) -> None:
    """Allow coordinator-only execution only for the exact bound agent/session."""
    binding = state.get("coordinator_binding")
    if (
        not isinstance(binding, dict)
        or state.get("coordinator") != agent_id
        or binding.get("agent_id") != agent_id
        or binding.get("role") != "coordinator"
        or binding.get("platform_id") != platform_id
        or binding.get("session_id") != session_id
        or not isinstance(platform_id, str)
        or not platform_id.strip()
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise WorkflowError(
            "执行身份与 state.json coordinator_binding 不匹配",
            E_COORDINATOR_BINDING,
            agent_id=agent_id,
            platform_id=platform_id,
            session_id=session_id,
        )


def load_state(workspace: str | Path) -> dict[str, Any]:
    state = load_json(path_in_workspace(workspace, ".multiagent/state.json"))
    validate_state_shape(state)
    return state


def update_state_metadata(state: dict[str, Any]) -> None:
    state["revision"] = int(state["revision"]) + 1
    state["last_checked_at"] = iso_now()


def main_markdown_path(workspace: str | Path, state: dict[str, Any]) -> Path:
    authority = state.get("content_authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("discussion_path"), str):
        raise WorkflowError("缺少内容权威路径", E_SCHEMA)
    return path_in_workspace(workspace, authority["discussion_path"])


def ensure_content_consistency(workspace: str | Path, state: dict[str, Any]) -> None:
    authority = state.get("content_authority")
    if not authority:
        return  # 兼容旧 CC state；迁移后的 state 由初始化器补全。
    markdown = main_markdown_path(workspace, state)
    if not markdown.is_file():
        raise WorkflowError("权威讨论 Markdown 不存在", E_HASH, path=str(markdown))
    actual = sha256_file(markdown)
    expected = authority.get("sha256")
    if expected and actual != expected:
        raise WorkflowError("Markdown 与 state.json 哈希不一致", E_HASH, expected=expected, actual=actual)


def update_content_hash(workspace: str | Path, state: dict[str, Any]) -> None:
    authority = state.get("content_authority")
    if not authority:
        return
    markdown = main_markdown_path(workspace, state)
    authority["sha256"] = sha256_file(markdown)
    authority["updated_at"] = iso_now()


def instruction_digest(payload: dict[str, Any]) -> str:
    value = {key: item for key, item in payload.items() if key != "sha256"}
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def validate_instruction(payload: dict[str, Any]) -> None:
    required = {"instruction_id", "discussion_id", "sequence", "kind", "agent_id", "runtime_version", "state_revision", "task_prompt", "input_paths", "output_path", "access_scope", "attempt", "max_attempts", "issued_at", "sha256"}
    missing = sorted(required - set(payload))
    if missing:
        raise WorkflowError("指令缺少必填字段", E_SCHEMA, missing=missing)
    agent_id = validate_participant_id(str(payload["agent_id"]))
    if not isinstance(payload["discussion_id"], str) or not payload["discussion_id"].strip():
        raise WorkflowError("discussion_id 必须是非空字符串", E_SCHEMA, field="discussion_id")
    instruction_id = payload["instruction_id"]
    if not isinstance(instruction_id, str) or not INSTRUCTION_ID_RE.fullmatch(instruction_id):
        raise WorkflowError("instruction_id 非法", E_SCHEMA, instruction_id=instruction_id)
    task_prompt = payload["task_prompt"]
    if not isinstance(task_prompt, str) or len(task_prompt.strip()) < 80:
        raise WorkflowError("task_prompt 必须是完整可执行指令", E_SCHEMA)
    output_path = payload["output_path"]
    if not isinstance(output_path, str) or not output_path:
        raise WorkflowError("output_path 必须是非空字符串", E_SCHEMA)
    access_scope = payload["access_scope"]
    view_root = f".multiagent/views/{agent_id}"
    receipt_root = f".multiagent/receipts/{agent_id}"
    if not isinstance(access_scope, dict):
        raise WorkflowError("access_scope 必须是对象", E_SCHEMA)
    if (
        access_scope.get("mode") != "sealed_view"
        or access_scope.get("view_root") != view_root
        or access_scope.get("allowed_read_roots") != [f"{view_root}/inputs"]
        or access_scope.get("allowed_write_roots") != [f"{view_root}/outputs", receipt_root]
        or access_scope.get("requires_platform_enforcement") is not True
        or access_scope.get("independence_claim_requires_enforcement_receipt") is not True
        or not isinstance(access_scope.get("security_note"), str)
        or not access_scope["security_note"].strip()
        or not isinstance(access_scope.get("input_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", access_scope["input_manifest_sha256"])
        or access_scope.get("input_manifest_path")
        != f"{view_root}/inputs/.manifests/{access_scope.get('input_manifest_sha256')}.json"
        or not isinstance(access_scope.get("scope_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", access_scope["scope_digest"])
    ):
        raise WorkflowError("access_scope 不符合参与者密封视图契约", E_SCHEMA)
    if (payload.get("platform_id") is None) != (payload.get("session_id") is None):
        raise WorkflowError("platform_id 与 session_id 必须同时提供", E_SCHEMA)
    if payload.get("platform_id") is not None and (
        not isinstance(payload["platform_id"], str)
        or not payload["platform_id"].strip()
        or not isinstance(payload["session_id"], str)
        or not payload["session_id"].strip()
    ):
        raise WorkflowError("platform_id 与 session_id 必须为非空字符串", E_SCHEMA)
    input_paths = payload["input_paths"]
    if not isinstance(input_paths, list) or not input_paths or not all(isinstance(item, str) and item for item in input_paths):
        raise WorkflowError("input_paths 必须是非空字符串列表", E_SCHEMA)
    for item in input_paths:
        posix_path = PurePosixPath(item)
        windows_path = PureWindowsPath(item)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
            or "\\" in item
            or not item.startswith(f"{view_root}/inputs/")
        ):
            raise WorkflowError("input_path 超出参与者密封视图", E_PATH_SCOPE, path=item)
    if agent_id not in task_prompt or output_path not in task_prompt or any(item not in task_prompt for item in input_paths):
        raise WorkflowError("task_prompt 必须绑定身份、全部输入与输出", E_SCHEMA)
    expected_outputs = {
        "propose": f"{view_root}/outputs/提案文档.md",
        "repair": f"{view_root}/outputs/提案文档.md",
        "respond": f"{view_root}/outputs/交叉回应文档.md",
    }
    if output_path.rstrip("/") != expected_outputs.get(str(payload["kind"]), receipt_root):
        raise WorkflowError("output_path 超出参与者写入范围", E_PATH_SCOPE, path=payload["output_path"])
    if payload["sha256"] != instruction_digest(payload):
        raise WorkflowError("指令哈希不匹配", E_HASH, instruction_id=payload.get("instruction_id"))


def enqueue_instruction(workspace: str | Path, agent_id: str, payload: dict[str, Any]) -> Path:
    validate_instruction(payload)
    agent_id = validate_participant_id(agent_id)
    if payload["agent_id"] != agent_id:
        raise WorkflowError("指令 agent_id 与队列不一致", E_PATH_SCOPE, agent_id=agent_id)
    target = path_in_workspace(workspace, f".multiagent/instructions/{agent_id}/{int(payload['sequence']):06d}-{payload['kind']}-{payload['instruction_id']}.json")
    if target.exists():
        existing = load_json(target)
        if existing == payload:
            return target
        raise WorkflowError("不可变指令发生冲突", E_STATE_CONFLICT, path=str(target))
    atomic_write_json(target, payload)
    return target


def write_deletion_manifest(workspace: str | Path, entries: list[dict[str, Any]], revision: int) -> Path:
    target = path_in_workspace(workspace, ".multiagent/archive/proposals/deletion-manifest.json")
    payload = {"created_at": iso_now(), "merge_revision": revision, "entries": entries}
    atomic_write_json(target, payload)
    return target


class WorkspaceTransaction:
    """Workspace-local recoverable transaction for a bounded set of files.

    Every replacement is staged before the first visible mutation. Existing
    targets are backed up before replacement/deletion. An exception rolls all
    applied operations back; a process crash leaves a journal that the next
    invocation can recover before starting a new transaction.
    """

    def __init__(self, workspace: str | Path, label: str,
                 fault_hook: Callable[[dict[str, Any], int], None] | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.transaction_id = "%s-%s" % (label, uuid.uuid4().hex)
        self.root = self.workspace / ".multiagent" / "audit" / "workflow-transactions" / self.transaction_id
        self.staged = self.root / "staged"
        self.backups = self.root / "backups"
        self.journal_path = self.root / "journal.json"
        self.operations: list[dict[str, Any]] = []
        self.committed = False
        self.fault_hook = fault_hook
        self.staged.mkdir(parents=True, exist_ok=False)
        self.backups.mkdir(parents=True, exist_ok=False)
        self._write_journal("staging")

    def _relative(self, path: str | Path) -> str:
        value = Path(path)
        if value.is_absolute():
            return safe_relative_path(self.workspace, value)
        return safe_relative_path(self.workspace, path_in_workspace(self.workspace, value))

    def _write_journal(self, status: str, **extra: Any) -> None:
        atomic_write_json(self.journal_path, {
            "transaction_id": self.transaction_id,
            "status": status,
            "operations": self.operations,
            "updated_at": iso_now(),
            **extra,
        })

    def stage_bytes(self, target: str | Path, data: bytes) -> None:
        relative = self._relative(target)
        staged = self.staged / relative
        _atomic_write(staged, data)
        self.operations.append({"kind": "replace", "target": relative, "staged": relative})
        self._write_journal("staging")

    def stage_text(self, target: str | Path, text: str) -> None:
        self.stage_bytes(target, text.encode("utf-8"))

    def stage_json(self, target: str | Path, value: dict[str, Any]) -> None:
        self.stage_text(target, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def stage_copy(self, source: str | Path, target: str | Path) -> None:
        source_path = path_in_workspace(self.workspace, self._relative(source))
        if not source_path.is_file():
            raise WorkflowError("事务复制源文件不存在", E_STATE_CONFLICT, path=str(source_path))
        self.stage_bytes(target, source_path.read_bytes())

    def stage_delete(self, target: str | Path) -> None:
        relative = self._relative(target)
        self.operations.append({"kind": "delete", "target": relative})
        self._write_journal("staging")

    def commit(self) -> None:
        self._write_journal("committing", applied=0)
        applied = 0
        try:
            for index, operation in enumerate(self.operations):
                target = path_in_workspace(self.workspace, operation["target"])
                backup = self.backups / operation["target"]
                operation["original_exists"] = target.exists()
                if target.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                if operation["kind"] == "replace":
                    source = self.staged / operation["staged"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                elif operation["kind"] == "delete":
                    target.unlink(missing_ok=True)
                else:
                    raise WorkflowError("未知事务操作", E_SCHEMA, operation=operation)
                applied = index + 1
                self._write_journal("committing", applied=applied)
                if self.fault_hook is not None:
                    self.fault_hook(operation, applied)
            self.committed = True
            self._write_journal("committed", applied=applied)
        except Exception as exc:
            self._rollback(applied)
            raise WorkflowError("工作区事务提交失败并已回滚", E_STATE_CONFLICT,
                                transaction_id=self.transaction_id, cause=str(exc)) from exc

    def _rollback(self, applied: int) -> None:
        rollback_errors: list[str] = []
        for operation in reversed(self.operations[:applied]):
            target = path_in_workspace(self.workspace, operation["target"])
            backup = self.backups / operation["target"]
            try:
                if operation.get("original_exists"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as exc:  # journal remains for deterministic recovery
                rollback_errors.append("%s: %s" % (operation["target"], exc))
        self._write_journal("rolled_back" if not rollback_errors else "rollback_required",
                            applied=applied, rollback_errors=rollback_errors)
        if rollback_errors:
            raise WorkflowError("工作区事务回滚不完整", E_STATE_CONFLICT,
                                transaction_id=self.transaction_id, errors=rollback_errors)

    def close(self) -> None:
        if self.committed:
            shutil.rmtree(self.root, ignore_errors=True)


def recover_workspace_transactions(workspace: str | Path) -> list[str]:
    """Rollback incomplete transaction journals before a new mutation starts."""
    root = Path(workspace).resolve()
    recovered: list[str] = []
    transaction_root = root / ".multiagent" / "audit" / "workflow-transactions"
    if not transaction_root.is_dir():
        return recovered
    for journal_path in sorted(transaction_root.glob("*/journal.json")):
        journal = load_json(journal_path)
        if journal.get("status") in {"committed", "rolled_back"}:
            shutil.rmtree(journal_path.parent, ignore_errors=True)
            continue
        operations = journal.get("operations", [])
        applied = int(journal.get("applied", 0))
        backups = journal_path.parent / "backups"
        errors: list[str] = []
        for operation in reversed(operations[:applied]):
            target = path_in_workspace(root, operation["target"])
            backup = backups / operation["target"]
            try:
                if operation.get("original_exists"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as exc:
                errors.append("%s: %s" % (operation["target"], exc))
        if errors:
            atomic_write_json(journal_path, {**journal, "status": "rollback_required", "rollback_errors": errors})
            raise WorkflowError("检测到无法自动恢复的工作区事务", E_STATE_CONFLICT,
                                transaction_id=journal.get("transaction_id"), errors=errors)
        shutil.rmtree(journal_path.parent, ignore_errors=True)
        recovered.append(str(journal.get("transaction_id")))
    return recovered


def run_main(main: Any) -> None:
    try:
        result = main()
        raise SystemExit(EXIT_OK if result is None else int(result))
    except SystemExit:
        raise
    except Exception as error:
        raise SystemExit(fail(error))
