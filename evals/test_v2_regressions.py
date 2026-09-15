#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实演练暴露的七项回归契约。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from export_docx import open_document  # type: ignore[import-not-found]
from participant_runtime.protocol import Instruction  # type: ignore[import-not-found]


class V2RegressionTests(unittest.TestCase):
    def _init_workspace(
        self, root: Path, bindings: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        binding_args = []
        for binding in bindings or [
            "dsh=deepseek:session-001",
            "chatgpt=openai:session:chatgpt=alpha",
        ]:
            binding_args.extend(["--participant-binding", binding])
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "init_discussion.py"),
                str(root),
                "dsh",
                "dsh",
                "chatgpt",
                "--discussion-id",
                "identity-and-layout",
                "--coordinator-platform",
                "deepseek",
                "--coordinator-session",
                "session-001",
                "--attestation-public-key-b64",
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "--attestation-key-id",
                "test-ed25519-key",
                *binding_args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_initializer_requires_explicit_coordinator_platform_and_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiagent-v2-") as temp:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "init_discussion.py"), str(Path(temp) / "讨论"), "dsh", "dsh"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("--coordinator-platform", completed.stderr)

    def test_initializer_uses_compact_internal_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiagent-v2-") as temp:
            workspace = Path(temp) / "讨论"
            completed = self._init_workspace(workspace)
            self.assertEqual(completed.returncode, 0, completed.stderr)

            self.assertTrue((workspace / ".multiagent" / "state.json").is_file())
            self.assertFalse((workspace / "state.json").exists())
            self.assertFalse((workspace / "instructions").exists())
            self.assertFalse((workspace / "receipts").exists())
            self.assertFalse((workspace / "runtime").exists())
            internal = workspace / ".multiagent"
            self.assertFalse((internal / "proposals").exists())
            self.assertFalse((internal / "responses").exists())
            self.assertFalse((internal / "archive" / "proposals").exists())
            for agent_id in ("dsh", "chatgpt"):
                self.assertTrue((internal / "views" / agent_id / "outputs").is_dir())
                self.assertTrue((internal / "receipts" / agent_id).is_dir())
            reported_docs = [
                line.split("主讨论文档:", 1)[1].strip()
                for line in completed.stdout.splitlines()
                if "主讨论文档:" in line
            ]
            self.assertEqual(len(reported_docs), 1, completed.stdout)
            self.assertRegex(
                reported_docs[0], r"^identity-and-layout讨论文档_\d{4}-\d{2}-\d{2}\.md$"
            )
            self.assertEqual(
                sorted(path.name for path in workspace.iterdir() if not path.name.startswith(".")),
                sorted([reported_docs[0], "project-context.md"]),
            )

    def test_initializer_binds_logical_role_to_real_platform_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiagent-v2-") as temp:
            workspace = Path(temp) / "讨论"
            completed = self._init_workspace(workspace)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads((workspace / ".multiagent" / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(state["coordinator"], "dsh")
            self.assertEqual(state["coordinator_binding"], {
                "agent_id": "dsh",
                "role": "coordinator",
                "platform_id": "deepseek",
                "session_id": "session-001",
            })
            self.assertEqual(state["participant_bindings"], {
                "dsh": {
                    "agent_id": "dsh",
                    "role": "coordinator",
                    "platform_id": "deepseek",
                    "session_id": "session-001",
                },
                "chatgpt": {
                    "agent_id": "chatgpt",
                    "role": "participant",
                    "platform_id": "openai",
                    "session_id": "session:chatgpt=alpha",
                },
            })
            self.assertEqual(state["proposal_disposition"], "delete")
            self.assertEqual(state["isolation_trust"]["algorithm"], "Ed25519")
            self.assertEqual(state["isolation_trust"]["key_id"], "test-ed25519-key")
            self.assertEqual(state["isolation_trust"]["private_key_location"], "external_to_workspace")
            self.assertNotIn('"private_key":', json.dumps(state).lower())

    def test_initializer_rejects_missing_duplicate_extra_and_mismatched_bindings(self) -> None:
        invalid_bindings = {
            "malformed binding": ["dsh=deepseek:session-001", "chatgpt-without-separator"],
            "missing participant": ["dsh=deepseek:session-001"],
            "duplicate participant": [
                "dsh=deepseek:session-001",
                "chatgpt=openai:first",
                "chatgpt=openai:second",
            ],
            "unexpected participant": [
                "dsh=deepseek:session-001",
                "chatgpt=openai:session-002",
                "other=provider:session-003",
            ],
            "coordinator mismatch": [
                "dsh=another-platform:session-001",
                "chatgpt=openai:session-002",
            ],
        }

        for case, bindings in invalid_bindings.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="multiagent-v2-") as temp:
                workspace = Path(temp) / "讨论"
                completed = self._init_workspace(workspace, bindings)
                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertFalse((workspace / ".multiagent" / "state.json").exists())
                self.assertFalse((workspace / ".multiagent").exists())

    def test_every_instruction_contains_a_complete_task_prompt_and_scoped_view(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiagent-v2-") as temp:
            workspace = Path(temp) / "讨论"
            completed = self._init_workspace(workspace)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            instruction_path = next(
                (workspace / ".multiagent" / "instructions" / "chatgpt").glob("*.json")
            )
            payload = json.loads(instruction_path.read_text(encoding="utf-8"))
            instruction = Instruction.from_dict(payload)

            self.assertGreater(len(instruction.task_prompt), 80)
            self.assertIn("chatgpt", instruction.task_prompt)
            self.assertEqual(instruction.access_scope["mode"], "sealed_view")
            self.assertTrue(instruction.access_scope["requires_platform_enforcement"])
            self.assertEqual(instruction.access_scope["attestation_verifier"]["key_id"], "test-ed25519-key")
            self.assertEqual(instruction.access_scope["attestation_verifier"]["algorithm"], "ed25519")
            self.assertTrue(
                all(path.startswith(".multiagent/views/chatgpt/") for path in instruction.input_paths)
            )
            self.assertEqual(instruction.platform_id, "openai")
            self.assertEqual(instruction.session_id, "session:chatgpt=alpha")

    def test_open_document_uses_the_supplied_frontend_opener(self) -> None:
        opened: list[Path] = []
        target = Path("candidate.docx")

        def opener(path: Path) -> bool:
            opened.append(Path(path))
            return True

        result = open_document(target, opener=opener)

        self.assertTrue(result)
        self.assertEqual(opened, [target])

    def test_discussion_template_records_identity_isolation_candidate_word_and_shutdown(self) -> None:
        template = (REPO_ROOT / "assets" / "discussion-template.md").read_text(encoding="utf-8")

        for marker in (
            "platform_id",
            "session_id",
            "密封输入视图",
            "候选 Word",
            "系统打开成功及时间",
            "常规监测停止记录时间",
            "Rainier 的明确确认才代表用户确认",
        ):
            self.assertIn(marker, template)

    def test_skill_declares_event_driven_gates_instead_of_process_polling(self) -> None:
        skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("事件驱动", skill)
        self.assertIn("门禁不得依赖常驻流程监测脚本", skill)
        self.assertIn("候选 Word", skill)
        self.assertIn("自动打开", skill)
        self.assertIn("密封、只读的输入视图", skill)


if __name__ == "__main__":
    unittest.main()
