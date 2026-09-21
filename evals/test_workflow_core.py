#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Participant Runtime：工作流内核的先行契约测试。

本文件在 Runtime 实现前有意失败：`scripts/workflow_core.py` 尚不存在。
失败必须来自缺失的产品接口，不能被解释器或导入路径问题掩盖。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from workflow_core import (  # type: ignore[import-not-found]  # RED：Task 2 实现
    E_HASH,
    E_PATH_SCOPE,
    E_SCHEMA,
    StateLock,
    WorkflowError,
    atomic_write_json,
    instruction_digest,
    load_state,
    load_json,
    path_in_workspace,
    validate_instruction,
)
from instruction_prompts import build_task_prompt  # type: ignore[import-not-found]
from participant_views import access_scope, output_path, publish_inputs  # type: ignore[import-not-found]


class WorkflowCoreContractTests(unittest.TestCase):
    """唯一内核必须提供原子写入、路径收敛与锁的最小契约。"""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="runtime-core-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_atomic_json_replaces_whole_document(self) -> None:
        target = self.temp_dir / "state.json"
        atomic_write_json(target, {"revision": 1, "obsolete": True})
        atomic_write_json(target, {"revision": 2})

        self.assertEqual(load_json(target), {"revision": 2})
        self.assertFalse(any(target.parent.glob("*.tmp")))

    def test_path_in_workspace_rejects_parent_escape(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "E_PATH_SCOPE") as caught:
            path_in_workspace(self.temp_dir, "../state.json")

        self.assertEqual(caught.exception.code, E_PATH_SCOPE)

    def test_path_in_workspace_resolves_only_inside_workspace(self) -> None:
        resolved = path_in_workspace(self.temp_dir, "proposals/claude-a-提案文档.md")

        self.assertEqual(resolved, self.temp_dir / "proposals" / "claude-a-提案文档.md")

    def test_state_lock_releases_after_context_exit(self) -> None:
        with StateLock(self.temp_dir):
            self.assertTrue((self.temp_dir / ".multiagent" / ".state.lock").exists())

        self.assertFalse((self.temp_dir / ".multiagent" / ".state.lock").exists())

    def test_state_lock_reclaims_dead_owner(self) -> None:
        lock = self.temp_dir / ".multiagent" / ".state.lock"
        lock.mkdir(parents=True)
        (lock / "owner.json").write_text(json.dumps({
            "pid": 99999999,
            "owner_token": "dead-owner",
            "acquired_at": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")

        with StateLock(self.temp_dir, timeout_seconds=0.2) as acquired:
            owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["pid"], os.getpid())
            self.assertNotEqual(owner["owner_token"], "dead-owner")
            self.assertEqual(owner["owner_token"], acquired.owner_token)

    def test_load_state_reads_only_the_compact_internal_path(self) -> None:
        state = {
            "protocol_version": "1.0",
            "discussion_id": "test",
            "stage": "initialized",
            "expected_participants": ["claude-a"],
            "submission_status": {"claude-a": "pending"},
            "response_status": {"claude-a": "pending"},
            "coordinator": "claude-a",
            "coordinator_binding": {
                "agent_id": "claude-a",
                "role": "coordinator",
                "platform_id": "claude-code",
                "session_id": "session-1",
            },
            "revision": 1,
        }
        atomic_write_json(self.temp_dir / ".multiagent" / "state.json", state)

        self.assertEqual(load_state(self.temp_dir), state)
        self.assertFalse((self.temp_dir / "state.json").exists())

    def test_instruction_validator_requires_and_hashes_complete_prompt_and_scope(self) -> None:
        source = self.temp_dir / "project-context.md"
        source.write_text("# Context\n", encoding="utf-8")
        inputs = publish_inputs(self.temp_dir, "claude-a", [(source, "context.md")])
        output = output_path(self.temp_dir, "claude-a", "propose")
        payload = {
            "instruction_id": "I-0001",
            "sequence": 1,
            "kind": "propose",
            "agent_id": "claude-a",
            "discussion_id": "workflow-core-test",
            "runtime_version": "1.0.0",
            "state_revision": 1,
            "task_prompt": build_task_prompt("propose", "claude-a", "coordinator", inputs, output),
            "input_paths": inputs,
            "output_path": output,
            "access_scope": access_scope(self.temp_dir, "claude-a"),
            "attempt": 1,
            "max_attempts": 3,
            "issued_at": "2026-08-14T00:00:00+08:00",
        }
        payload["sha256"] = instruction_digest(payload)
        validate_instruction(payload)

        missing_discussion = dict(payload)
        missing_discussion.pop("discussion_id")
        missing_discussion["sha256"] = instruction_digest(missing_discussion)
        with self.assertRaisesRegex(WorkflowError, E_SCHEMA):
            validate_instruction(missing_discussion)

        blank_discussion = dict(payload)
        blank_discussion["discussion_id"] = "  "
        blank_discussion["sha256"] = instruction_digest(blank_discussion)
        with self.assertRaisesRegex(WorkflowError, E_SCHEMA):
            validate_instruction(blank_discussion)

        for field in ("task_prompt", "access_scope"):
            invalid = dict(payload)
            invalid.pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(WorkflowError, E_SCHEMA):
                validate_instruction(invalid)

        changed_prompt = dict(payload)
        changed_prompt["task_prompt"] = str(payload["task_prompt"]) + " changed"
        with self.assertRaisesRegex(WorkflowError, E_HASH):
            validate_instruction(changed_prompt)


if __name__ == "__main__":
    unittest.main()
