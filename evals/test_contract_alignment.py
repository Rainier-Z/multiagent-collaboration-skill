#!/usr/bin/env python3
"""Tests for the stage, workspace-layout, and diagnostic-monitor contracts."""

from __future__ import annotations

import ast
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import monitor_discussion
import workspace_monitor
import validate_discussion
import init_discussion


class ContractAlignmentTests(unittest.TestCase):
    def test_state_schema_uses_implemented_identity_fields_and_seven_stages(self) -> None:
        schema = (REPO_ROOT / "references" / "state-schema.md").read_text(encoding="utf-8")
        self.assertIn("coordinator_binding", schema)
        self.assertIn("participant_bindings", schema)
        self.assertNotIn("coordinator_identity", schema)
        self.assertNotIn("participant_identities", schema)
        stage_lines = [line.strip() for line in schema.splitlines() if line.strip() in {
            "initialized", "→ independent_proposal", "→ cross_response", "→ candidate_decision",
            "→ user_confirmation", "→ confirmed_decision", "→ delivered",
        }]
        self.assertEqual(stage_lines, [
            "initialized", "→ independent_proposal", "→ cross_response", "→ candidate_decision",
            "→ user_confirmation", "→ confirmed_decision", "→ delivered",
        ])

    def test_disposition_and_attestation_trust_defaults_are_explicit(self) -> None:
        schema = (REPO_ROOT / "references" / "state-schema.md").read_text(encoding="utf-8")
        protocol = (REPO_ROOT / "references" / "collaboration-protocol.md").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_DISPOSITION = "delete"', (SCRIPTS_DIR / "init_discussion.py").read_text(encoding="utf-8"))
        self.assertIn("proposal_disposition", schema)
        self.assertIn("默认自动删除", protocol)
        self.assertIn("private_key_location", schema)
        self.assertIn("Ed25519", schema)
        self.assertIn("验签失败", schema)

    def test_initializer_requires_valid_public_verifier_configuration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="attestation-contract-") as raw:
            workspace = Path(raw) / "discussion"
            args = [
                str(workspace), "planner", "planner", "chatgpt",
                "--coordinator-platform", "codex", "--coordinator-session", "session-1",
                "--participant-binding", "planner=codex:session-1",
                "--participant-binding", "chatgpt=openai:session-2",
                "--attestation-key-id", "key-1",
            ]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    init_discussion.main(args)
                except SystemExit as exc:
                    missing_key = exc.code
                malformed_key = init_discussion.main(args + ["--attestation-public-key-b64", "not-base64"])
                wrong_size_key = init_discussion.main(args + ["--attestation-public-key-b64", "AQ=="])
                valid_key = init_discussion.main(args + [
                    "--attestation-public-key-b64", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                ])
            state = json.loads((workspace / ".multiagent" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(missing_key, 2)
        self.assertEqual(malformed_key, 3)
        self.assertEqual(wrong_size_key, 3)
        self.assertEqual(valid_key, 0)
        self.assertEqual(state["proposal_disposition"], "delete")
        self.assertEqual(state["isolation_trust"]["key_id"], "key-1")

    def test_candidate_stop_requires_each_participant_receipt_before_confirmation(self) -> None:
        protocol = (REPO_ROOT / "references" / "collaboration-protocol.md").read_text(encoding="utf-8")
        adapters = (REPO_ROOT / "references" / "platform-adapters.md").read_text(encoding="utf-8")
        template = (REPO_ROOT / "assets" / "discussion-template.md").read_text(encoding="utf-8")
        self.assertIn("每位参与者发出一条唯一 `stop` 指令", protocol)
        self.assertIn("收齐每个绑定会话的唯一 `completed` stop 回执", protocol)
        for required_stop_semantic in (
            "OpenClaw",
            "openclaw cron rm",
            "openclaw cron list --json",
            "`proof_reference`",
            "`proof_sha256`",
            "全部参与者的唯一、有效 completed 回执",
            "`user_confirmation`",
        ):
            with self.subTest(required_stop_semantic=required_stop_semantic):
                self.assertIn(required_stop_semantic, adapters)
        self.assertIn("stop 指令 ID", template)

    def test_document_template_uses_the_parser_section_headings(self) -> None:
        template = (REPO_ROOT / "assets" / "discussion-template.md").read_text(encoding="utf-8")
        for heading in (
            "## 二、独立提案",
            "## 三、交叉回应",
            "## 四、结构化决策包",
            "## 五、候选决策与 Word 审阅",
            "## 六、确认固化记录",
            "## 七、正式 Word 交付",
        ):
            self.assertIn(heading, template)
        self.assertNotIn("## （四）提案与交叉回应", template)

    def test_diagnostic_monitor_loads_internal_state_without_root_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="monitor-contract-") as raw:
            workspace = Path(raw)
            internal_state = workspace / ".multiagent" / "state.json"
            internal_state.parent.mkdir()
            internal_state.write_text('{"stage":"cross_response","revision":3}\n', encoding="utf-8")

            state = monitor_discussion.load_state(str(workspace))
            stage = workspace_monitor.read_stage(str(workspace))

        self.assertEqual(state["stage"], "cross_response")
        self.assertEqual(stage, "cross_response")

    def test_diagnostics_are_single_pass_and_do_not_claim_to_wake_agents(self) -> None:
        monitor_source = (REPO_ROOT / "scripts" / "monitor_discussion.py").read_text(encoding="utf-8")
        workspace_source = (REPO_ROOT / "scripts" / "workspace_monitor.py").read_text(encoding="utf-8")
        for source in (monitor_source, workspace_source):
            tree = ast.parse(source)
            main = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"][-1]
            self.assertFalse(any(isinstance(node, (ast.While, ast.For, ast.AsyncFor)) for node in ast.walk(main)))
            self.assertFalse(any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                for node in ast.walk(main)
            ))
        self.assertIn("不唤醒", monitor_source)
        self.assertIn("不驻留", workspace_source)

    def test_diagnostic_sources_have_no_legacy_polling_code_or_options(self) -> None:
        for name in ("monitor_discussion.py", "workspace_monitor.py"):
            source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("while True", source)
            self.assertNotIn("time.sleep", source)
            self.assertNotIn("--interval", source)
            self.assertNotIn("--watch-until", source)
            tree = ast.parse(source)
            self.assertFalse(any(isinstance(node, ast.While) for node in ast.walk(tree)))
            self.assertFalse(any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                for node in ast.walk(tree)
            ))

    def test_participant_templates_only_name_sealed_compact_paths(self) -> None:
        templates = (
            REPO_ROOT / "assets" / "project-context-template.md",
            REPO_ROOT / "assets" / "proposal-template.md",
            REPO_ROOT / "assets" / "response-template.md",
        )
        for path in templates:
            text = path.read_text(encoding="utf-8")
            self.assertIn(".multiagent/state.json", text)
            self.assertNotIn("proposals/<agent-id>", text)
            self.assertNotIn("responses/<agent-id>", text)
        response = templates[-1].read_text(encoding="utf-8")
        self.assertIn("指令密封提供的合并讨论副本", response)
        self.assertNotIn("合并主讨论文档 + 归档提案", response)
        self.assertIn("不得读取归档提案", response)

    def test_initializer_does_not_create_unused_flat_or_archive_dirs(self) -> None:
        source = (SCRIPTS_DIR / "init_discussion.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        subdirs = next(
            node.value for node in ast.walk(module)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "subdirs" for target in node.targets)
        )
        names = [node.value for node in ast.walk(subdirs) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        self.assertNotIn("proposals", names)
        self.assertNotIn("responses", names)
        self.assertNotIn("archive", names)
        self.assertIn('"views", participant, "outputs"', source)

    def test_skill_documents_require_explicit_coordinator_binding(self) -> None:
        skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for option in ("--actor", "--platform-id", "--session-id", ".multiagent/state.json"):
            self.assertIn(option, skill)
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("planner-ds=deepseek:session-001", readme)
        self.assertIn("orchestrate_discussion.py", readme)
        self.assertIn("--actor planner-ds --platform-id deepseek --session-id session-001", readme)

    def test_validator_uses_internal_state_and_only_reads(self) -> None:
        validator_source = (SCRIPTS_DIR / "validate_discussion.py").read_text(encoding="utf-8")
        self.assertNotIn('discussion_dir, "state.json"', validator_source)
        self.assertNotIn('discussion_dir, "proposals"', validator_source)
        self.assertNotIn('discussion_dir, "responses"', validator_source)
        with tempfile.TemporaryDirectory(prefix="validator-contract-") as raw:
            workspace = Path(raw) / "discussion"
            with redirect_stdout(io.StringIO()):
                init_result = init_discussion.main([
                    str(workspace), "planner-ds", "planner-ds", "chatgpt",
                    "--coordinator-platform", "deepseek", "--coordinator-session", "session-001",
                    "--attestation-public-key-b64", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "--attestation-key-id", "test-ed25519-key",
                    "--participant-binding", "planner-ds=deepseek:session-001",
                    "--participant-binding", "chatgpt=openai:session-002",
                ])
            self.assertEqual(init_result, 0)
            (workspace / "state.json").write_text('{"stage":"wrong-root"}', encoding="utf-8")
            before = {
                str(p.relative_to(workspace)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in workspace.rglob("*") if p.is_file()
            }
            with redirect_stdout(io.StringIO()):
                result = validate_discussion.main([str(workspace)])
            after = {
                str(p.relative_to(workspace)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in workspace.rglob("*") if p.is_file()
            }
        self.assertEqual(result, 0)
        self.assertEqual(before, after)

    def test_openclaw_template_contains_execution_loop_and_compact_paths(self) -> None:
        directive = (REPO_ROOT / "assets" / "openclaw-operational-directive-template.md").read_text(encoding="utf-8")
        self.assertIn(".multiagent/state.json", directive)
        self.assertIn(".multiagent/instructions/openclaw", directive)
        self.assertIn("扫描工作区", directive)
        self.assertIn("按规则执行", directive)
        self.assertIn("写入有效回执", directive)
        self.assertIn("前台报告", directive)
        self.assertIn("不得回复 HEARTBEAT_OK", directive)


if __name__ == "__main__":
    unittest.main()
