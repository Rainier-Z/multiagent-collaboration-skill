#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Participant Runtime：清单、指令与回执的先行契约测试。"""

from __future__ import annotations

import hashlib
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from participant_runtime.participant_runner import (  # type: ignore[import-not-found]  # RED：Task 4 实现
    E_PATH_SCOPE,
    E_SCHEMA,
    run_instruction,
)
from participant_runtime.protocol import (  # type: ignore[import-not-found]  # RED：Task 3 实现
    Instruction,
    WorkflowError,
    publish_runtime,
    receipt_path,
    sha256_file,
    verify_manifest,
    write_instruction,
    write_receipt,
)
from participant_views import INDEPENDENCE_NOTE, access_scope, output_path, publish_inputs  # type: ignore[import-not-found]
from instruction_prompts import build_task_prompt  # type: ignore[import-not-found]


def _instruction_payload(
    agent_id: str,
    instruction_id: str,
    kind: str = "bootstrap",
    *,
    runtime_version: str = "1.0.0",
    sequence: int = 1,
) -> dict[str, object]:
    """构造设计文档规定的最小可验证指令；sha256 故意不包含自己。"""
    view_root = ".multiagent/views/%s" % agent_id
    receipt_root = ".multiagent/receipts/%s" % agent_id
    business_output = {
        "propose": view_root + "/outputs/提案文档.md",
        "repair": view_root + "/outputs/提案文档.md",
        "respond": view_root + "/outputs/交叉回应文档.md",
    }
    inputs = [view_root + "/inputs/project-context.md"]
    input_manifest_sha256 = "a" * 64
    output = business_output.get(kind, receipt_root)
    payload: dict[str, object] = {
        "instruction_id": instruction_id,
        "sequence": sequence,
        "kind": kind,
        "agent_id": agent_id,
        "discussion_id": "participant-runtime-test",
        "runtime_version": runtime_version,
        "state_revision": 1,
        "task_prompt": build_task_prompt(kind, agent_id, "coordinator", inputs, output),
        "input_paths": inputs,
        "output_path": output,
        "access_scope": {
            "mode": "sealed_view",
            "view_root": view_root,
            "allowed_read_roots": [view_root + "/inputs"],
            "allowed_write_roots": [view_root + "/outputs", receipt_root],
            "requires_platform_enforcement": True,
            "independence_claim_requires_enforcement_receipt": True,
            "security_note": INDEPENDENCE_NOTE,
            "input_manifest_path": "%s/inputs/.manifests/%s.json" % (view_root, input_manifest_sha256),
            "input_manifest_sha256": input_manifest_sha256,
            "scope_digest": "b" * 64,
        },
        "attempt": 1,
        "max_attempts": 3,
        "issued_at": "2026-08-14T00:00:00.000+08:00",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _rehash_instruction(payload: dict[str, object]) -> dict[str, object]:
    payload["sha256"] = hashlib.sha256(
        json.dumps({key: value for key, value in payload.items() if key != "sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


class ParticipantRuntimeProtocolTests(unittest.TestCase):
    """Runtime 发布必须可验证，终结回执必须幂等且不可冲突。"""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="participant-runtime-test-"))
        context = self.workspace / ".multiagent/views/claude-a/inputs/project-context.md"
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_text("# Context\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_publish_runtime_hashes_every_manifest_file(self) -> None:
        manifest = publish_runtime(self.workspace)

        self.assertTrue(manifest.files)
        self.assertTrue(
            all(item["sha256"] == sha256_file(self.workspace / item["path"]) for item in manifest.files)
        )

    def test_verify_manifest_rejects_changed_runner(self) -> None:
        manifest = publish_runtime(self.workspace)
        runner = self.workspace / manifest.entrypoint
        runner.write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(WorkflowError, "E_HASH"):
            verify_manifest(self.workspace, manifest)

    def test_instruction_sha256_excludes_only_sha256_field(self) -> None:
        instruction = Instruction.from_dict(_instruction_payload("claude-a", "I-0001"))

        self.assertTrue(instruction.verify_sha256())
        self.assertEqual(instruction.discussion_id, "participant-runtime-test")

        changed_discussion = _instruction_payload("claude-a", "I-discussion-bound")
        changed_discussion["discussion_id"] = "another-discussion"
        with self.assertRaises(WorkflowError) as caught:
            Instruction.from_dict(changed_discussion)
        self.assertEqual(caught.exception.code, "E_HASH")

        missing_discussion = _instruction_payload("claude-a", "I-discussion-required")
        missing_discussion.pop("discussion_id")
        _rehash_instruction(missing_discussion)
        with self.assertRaises(WorkflowError) as caught:
            Instruction.from_dict(missing_discussion)
        self.assertEqual(caught.exception.code, E_SCHEMA)

    def test_task_prompt_and_access_scope_are_required_and_hashed(self) -> None:
        payload = _instruction_payload("claude-a", "I-required-fields")
        for field in ("task_prompt", "access_scope"):
            invalid = dict(payload)
            invalid.pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(WorkflowError, "E_SCHEMA"):
                Instruction.from_dict(invalid)

        changed_prompt = dict(payload)
        changed_prompt["task_prompt"] = str(payload["task_prompt"]) + " altered"
        with self.assertRaisesRegex(WorkflowError, "E_HASH"):
            Instruction.from_dict(changed_prompt)

        changed_scope = dict(payload)
        changed_scope["access_scope"] = {**payload["access_scope"], "review_note": "altered"}
        with self.assertRaisesRegex(WorkflowError, "E_HASH"):
            Instruction.from_dict(changed_scope)

    def test_task_prompt_must_bind_to_identity_inputs_and_output(self) -> None:
        payload = _instruction_payload("claude-a", "I-incomplete-task-prompt")
        payload["task_prompt"] = "A long but unrelated task prompt. " * 5
        canonical = json.dumps(
            {key: value for key, value in payload.items() if key != "sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload["sha256"] = hashlib.sha256(canonical).hexdigest()

        with self.assertRaisesRegex(WorkflowError, "E_SCHEMA"):
            Instruction.from_dict(payload)

    def test_instruction_cannot_read_shared_discussion_or_another_participant_output(self) -> None:
        for path in ("discussion.md", ".multiagent/views/claude-b/outputs/提案文档.md"):
            payload = _instruction_payload("claude-a", "I-outside-view")
            payload["input_paths"] = [path]
            payload["task_prompt"] = build_task_prompt("propose", "claude-a", "coordinator", [path], payload["output_path"])
            canonical = json.dumps({key: value for key, value in payload.items() if key != "sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            payload["sha256"] = hashlib.sha256(canonical).hexdigest()
            with self.subTest(path=path), self.assertRaisesRegex(WorkflowError, "E_PATH_SCOPE"):
                Instruction.from_dict(payload)

    def test_view_publisher_returns_only_agent_input_paths(self) -> None:
        source = self.workspace / "project-context.md"
        source.write_text("# Context\n", encoding="utf-8")

        paths = publish_inputs(self.workspace, "claude-a", [(source, "project-context.md")])

        self.assertEqual(paths, [".multiagent/views/claude-a/inputs/project-context.md"])
        self.assertEqual((self.workspace / paths[0]).read_text(encoding="utf-8"), "# Context\n")
        self.assertEqual(access_scope(self.workspace, "claude-a")["mode"], "sealed_view")
        self.assertTrue(access_scope(self.workspace, "claude-a")["requires_platform_enforcement"])

    def test_view_publisher_keeps_changed_same_name_inputs_as_new_snapshots(self) -> None:
        source = self.workspace / "discussion.md"
        source.write_text("# Round one\n", encoding="utf-8")
        first = publish_inputs(self.workspace, "claude-a", [(source, "discussion.md")], instruction_kind="respond")[0]
        source.write_text("# Round two\n", encoding="utf-8")

        second = publish_inputs(self.workspace, "claude-a", [(source, "discussion.md")], instruction_kind="respond")[0]

        self.assertNotEqual(first, second)
        self.assertEqual((self.workspace / first).read_text(encoding="utf-8"), "# Round one\n")
        self.assertEqual((self.workspace / second).read_text(encoding="utf-8"), "# Round two\n")

    def test_independent_proposal_cannot_stage_shared_discussion_as_an_input(self) -> None:
        source = self.workspace / "discussion.md"
        source.write_text("# Shared discussion\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkflowError, "E_PATH_SCOPE"):
            publish_inputs(self.workspace, "claude-a", [(source, "discussion.md")])

    def test_openclaw_instruction_without_directive_is_rejected_at_every_protocol_boundary(self) -> None:
        payload = _instruction_payload("openclaw", "I-openclaw-missing-directive")

        with self.assertRaisesRegex(WorkflowError, "E_SCHEMA"):
            Instruction.from_dict(payload)

        direct_instruction = replace(
            Instruction.from_dict(_instruction_payload("claude-a", "I-direct-missing-directive")),
            agent_id="openclaw",
        )
        with self.assertRaisesRegex(WorkflowError, "E_SCHEMA"):
            write_instruction(self.workspace, direct_instruction)

    def test_terminal_receipt_conflict_is_state_conflict(self) -> None:
        completed = {
            "instruction_id": "I-0001",
            "kind": "respond",
            "agent_id": "claude-a",
            "runtime_version": "1.0.0",
            "state_revision": 1,
            "status": "completed",
            "at": "2026-08-14T00:00:01.000+08:00",
            "attempt": 1,
            "output_path": ".multiagent/views/claude-a/outputs/交叉回应文档.md",
            "output_sha256": "0" * 64,
            "isolation_evidence": {
                "mode": "restricted_view",
                "agent_id": "claude-a",
                "instruction_id": "I-0001",
                "view_root": ".multiagent/views/claude-a",
                "scope_digest": "b" * 64,
                "input_manifest_sha256": "a" * 64,
                "allowed_read_roots": [".multiagent/views/claude-a/inputs"],
                "allowed_write_roots": [".multiagent/views/claude-a/outputs", ".multiagent/receipts/claude-a"],
                "contract_validation": "passed",
                "authenticity": "path_and_hash_contract_only",
                "cryptographic_verification": "not_performed",
            },
        }
        failed = {
            **completed,
            "status": "failed",
            "error_code": "E_SCHEMA",
            "message": "invalid output",
            "recoverable": True,
        }
        self.assertEqual(write_receipt(self.workspace, "claude-a", completed), "written")

        with self.assertRaisesRegex(WorkflowError, "E_STATE_CONFLICT"):
            write_receipt(self.workspace, "claude-a", failed)

    def test_instruction_ids_cannot_escape_their_agent_receipt_scope(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "E_SCHEMA"):
            receipt_path(self.workspace, "claude-a", "../claude-b", "accepted")

    def test_prompt_builder_returns_a_complete_action_for_every_instruction_kind(self) -> None:
        for kind in ("bootstrap", "propose", "respond", "repair", "stop"):
            prompt = build_task_prompt(
                kind,
                "openclaw",
                "coordinator",
                [".multiagent/views/openclaw/inputs/project-context.md"],
                output_path(self.workspace, "openclaw", kind),
            )
            with self.subTest(kind=kind):
                self.assertGreater(len(prompt), 80)
                self.assertIn("不得声称独立性已证明", prompt)

    def test_init_bootstrap_embeds_the_mandatory_openclaw_automation_directive(self) -> None:
        workspace = self.workspace / "initialized"
        private_key = Ed25519PrivateKey.generate()
        public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).decode("ascii")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "init_discussion.py"),
                str(workspace),
                "claude",
                "claude",
                "openclaw",
                "--coordinator-platform",
                "claude-code",
                "--coordinator-session",
                "session-test",
                "--attestation-key-id",
                "test-ed25519-key",
                "--attestation-public-key-b64",
                public_key_b64,
                "--participant-binding",
                "claude=claude-code:session-test",
                "--participant-binding",
                "openclaw=openclaw:session-openclaw",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        instruction_path = next((workspace / ".multiagent" / "instructions" / "openclaw").glob("*-bootstrap-*.json"))
        payload = json.loads(instruction_path.read_text(encoding="utf-8"))
        state = json.loads((workspace / ".multiagent" / "state.json").read_text(encoding="utf-8"))
        instruction = Instruction.from_dict(payload)
        self.assertEqual(instruction.discussion_id, state["discussion_id"])
        self.assertTrue(instruction.verify_sha256())
        directive = payload["operational_directive"]
        for required_semantic in (
            "本项目 Automation 名称",
            "openclaw cron list --json",
            ".multiagent/state.json",
            "按规则执行",
            "写入产物与回执",
            "前台报告",
        ):
            with self.subTest(required_semantic=required_semantic):
                self.assertIn(required_semantic, directive)


class ParticipantRunnerTests(unittest.TestCase):
    """Runner 只能消费属于自身且路径受限的单条指令。"""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="participant-runner-test-"))
        context = self.workspace / ".multiagent/views/claude-a/inputs/project-context.md"
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_text("# Context\n", encoding="utf-8")

    def _runner_payload(
        self,
        instruction_id: str,
        *,
        runtime_version: str = "1.0.0",
        sequence: int = 1,
    ) -> dict[str, object]:
        source = self.workspace / "project-context.md"
        source.write_text("# Context\n", encoding="utf-8")
        publish_inputs(self.workspace, "claude-a", [(source, "project-context.md")])
        payload = _instruction_payload(
            "claude-a", instruction_id, runtime_version=runtime_version, sequence=sequence,
        )
        payload["access_scope"] = access_scope(self.workspace, "claude-a")
        return _rehash_instruction(payload)

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_runner_rejects_instruction_for_another_agent(self) -> None:
        # Task 4 的夹具接口：把 claude-b 指令放入 claude-a 队列，必须拒绝。
        instruction_dir = self.workspace / ".multiagent/instructions" / "claude-a"
        instruction_dir.mkdir(parents=True)
        path = instruction_dir / "001-I-0001.json"
        path.write_text(json.dumps(_instruction_payload("claude-b", "I-0001"), ensure_ascii=False), encoding="utf-8")

        self.assertEqual(run_instruction(self.workspace, "claude-a", "I-0001"), E_PATH_SCOPE)

    def test_runner_rejects_agent_path_traversal_before_reading_instruction_queue(self) -> None:
        instruction_dir = self.workspace / ".multiagent/instructions/claude-b"
        instruction_dir.mkdir(parents=True)
        instruction = Instruction.from_dict(_instruction_payload("claude-b", "I-forged"))
        write_instruction(self.workspace, instruction)

        self.assertEqual(
            run_instruction(self.workspace, "../instructions/claude-b", "I-forged"),
            E_SCHEMA,
        )

    def test_bootstrap_cli_without_instruction_id_is_repeatable_and_never_writes_failed(self) -> None:
        manifest = publish_runtime(self.workspace)
        instruction = Instruction.from_dict(self._runner_payload("I-bootstrap"))
        write_instruction(self.workspace, instruction)
        runner = self.workspace / manifest.entrypoint
        command = [
            sys.executable, str(runner), "--workspace", str(self.workspace),
            "--agent", "claude-a", "--once",
        ]

        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")

        receipts = self.workspace / ".multiagent/receipts" / "claude-a"
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertTrue((receipts / "I-bootstrap-accepted.json").is_file())
        self.assertFalse(any(receipts.glob("I-bootstrap-failed.json")))
        self.assertEqual(len(list(receipts.glob("I-bootstrap-accepted.json"))), 1)

    def test_instruction_selects_its_runtime_manifest_when_two_versions_coexist(self) -> None:
        v100 = publish_runtime(self.workspace, "1.0.0")
        publish_runtime(self.workspace, "1.0.1")
        v101_instruction = Instruction.from_dict(self._runner_payload(
            "I-v101", runtime_version="1.0.1", sequence=1,
        ))
        v100_instruction = Instruction.from_dict(self._runner_payload(
            "I-v100", runtime_version="1.0.0", sequence=2,
        ))
        write_instruction(self.workspace, v101_instruction)
        write_instruction(self.workspace, v100_instruction)
        # 只破坏 1.0.0；1.0.1 指令必须仍选择并验证自己的清单。
        old_runner = self.workspace / v100.entrypoint
        old_runner.write_text(old_runner.read_text(encoding="utf-8") + "\n# tampered old version\n", encoding="utf-8")

        self.assertEqual(run_instruction(self.workspace, "claude-a", "I-v101"), 0)
        self.assertEqual(run_instruction(self.workspace, "claude-a", "I-v100"), "E_HASH")
        self.assertTrue((self.workspace / ".multiagent/receipts/claude-a/I-v101-accepted.json").is_file())
        self.assertTrue((self.workspace / ".multiagent/receipts/claude-a/I-v100-failed.json").is_file())


if __name__ == "__main__":
    unittest.main()
