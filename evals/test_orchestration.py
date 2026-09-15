#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Participant Runtime：传感器无副作用与编排自动推进的先行契约测试。"""

from __future__ import annotations

import json
import base64
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from orchestrate_discussion import (  # type: ignore[import-not-found]  # RED：Task 5 实现
    E_PLATFORM_UNAVAILABLE,
    FakeWakeAdapter,
    UnavailableWakeAdapter,
    _new_instruction,
    monitor_once,
    orchestrate_once,
)
from participant_runtime.protocol import Instruction, isolation_evidence_path, load_isolation_evidence, load_stop_attestation, stop_attestation_path
from workflow_core import WorkflowError, sha256_file
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from attestation_keys import sign_payload


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _workspace_state() -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "discussion_id": "red-contract",
        "stage": "initialized",
        "expected_participants": ["claude-a", "codex-a"],
        "submission_status": {"claude-a": "pending", "codex-a": "pending"},
        "response_status": {"claude-a": "pending", "codex-a": "pending"},
        "coordinator": "claude-a",
        "coordinator_binding": {
            "agent_id": "claude-a", "role": "coordinator",
            "platform_id": "claude-code", "session_id": "session-claude-a",
        },
        "participant_bindings": {
            "claude-a": {"platform_id": "claude-code", "session_id": "session-claude-a"},
            "codex-a": {"platform_id": "codex", "session_id": "session-codex-a"},
            "openclaw": {"platform_id": "openclaw", "session_id": "session-openclaw"},
        },
        "candidate_decision_ids": [],
        "confirmed_decision_ids": [],
        "monitoring": {"enabled": True, "status": "active"},
        "proposal_disposition": "archive",
        "revision": 1,
        "runtime_distribution": {"version": "1.0.0"},
        "retry_policy": {"max_attempts": 3},
    }


class OrchestrationContractTests(unittest.TestCase):
    """传感器不得改状态；编排器必须以回执推进并产生可审计结果。"""

    def setUp(self) -> None:
        self.key_dir = tempfile.TemporaryDirectory(prefix="orchestration-test-key-")
        self.key_id = "orchestration-test-key"
        self.private_key_path = Path(self.key_dir.name) / "private.pem"
        private_key = Ed25519PrivateKey.generate()
        self.private_key_path.write_bytes(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        self.public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).decode("ascii")
        self.attestation_env = patch.dict(os.environ, {
            "MULTIAGENT_ATTESTATION_KEY_ID": self.key_id,
            "MULTIAGENT_ATTESTATION_PUBLIC_KEY_B64": self.public_key_b64,
            "MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON": json.dumps({self.key_id: self.public_key_b64}),
        })
        self.attestation_env.start()
        self.workspace = Path(tempfile.mkdtemp(prefix="orchestration-test-"))
        self.internal = self.workspace / ".multiagent"
        self.internal.mkdir()
        document = self.workspace / "contract讨论文档_2026-08-14.md"
        document.write_text(
            "# Contract discussion\n\n## 一、项目与身份\n\n## 二、独立提案\n\n"
            "## 三、交叉回应\n\n## 四、结构化决策包\n\n## 五、候选决策与 Word 审阅\n\n"
            "## 六、确认固化记录\n\n## 七、正式 Word 交付\n", encoding="utf-8")
        state = _workspace_state()
        state["content_authority"] = {
            "discussion_path": document.name,
            "sha256": sha256_file(document),
        }
        _write_json(self.internal / "state.json", state)
        (self.workspace / "project-context.md").write_text("# Context\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.attestation_env.stop()
        self.key_dir.cleanup()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _revision(self) -> int:
        return int(json.loads((self.internal / "state.json").read_text(encoding="utf-8"))["revision"])

    def _orchestrate(self, wake=None, *, open_candidate=True):
        return orchestrate_once(
            self.workspace, wake, open_candidate=open_candidate,
            actor="claude-a", platform_id="claude-code", session_id="session-claude-a",
            opener=lambda _path: True,
        )

    def _attest(self, instruction: Instruction) -> dict:
        scope = instruction.access_scope
        value = {
            "issuer": "platform-attestation", "source": "platform-attestation",
            "evidence_type": "platform_sandbox", "enforcement_type": "platform_sandbox",
            "issued_at": "2026-09-13T12:00:00+08:00",
            "agent_id": instruction.agent_id, "instruction_id": instruction.instruction_id,
            "platform_id": instruction.platform_id, "session_id": instruction.session_id,
            "view_root": scope["view_root"], "scope_digest": scope["scope_digest"],
            "input_manifest_sha256": scope["input_manifest_sha256"],
            "allowed_read_roots": scope["allowed_read_roots"],
            "allowed_write_roots": scope["allowed_write_roots"],
            "trust": {"trust_root_id": "test-platform-fixture"},
        }
        value = sign_payload(
            value, private_key_path=self.private_key_path, key_id=self.key_id,
        )
        path = isolation_evidence_path(self.workspace, instruction)
        _write_json(path, value)
        return load_isolation_evidence(self.workspace, instruction)

    def test_monitor_only_reports_and_leaves_state_revision_unchanged(self) -> None:
        before = self._revision()

        observation = monitor_once(self.workspace)

        self.assertIsNotNone(observation)
        self.assertEqual(self._revision(), before)

    def test_completed_bootstraps_enqueue_one_propose_per_agent(self) -> None:
        _write_json(self.internal / "receipts/claude-a/I-0001-completed.json", {
            "instruction_id": "I-0001", "agent_id": "claude-a", "status": "completed", "kind": "bootstrap"
        })
        _write_json(self.internal / "receipts/codex-a/I-0002-completed.json", {
            "instruction_id": "I-0002", "agent_id": "codex-a", "status": "completed", "kind": "bootstrap"
        })

        result = self._orchestrate(FakeWakeAdapter())

        self.assertFalse(result.blocking_error_codes)
        pending = sorted(self.internal.glob("instructions/*/*-propose-*.json"))
        self.assertEqual(len(pending), 2)

    def test_invalid_output_enqueues_repair_for_same_agent(self) -> None:
        _write_json(self.internal / "receipts/claude-a/I-0003-failed.json", {
            "instruction_id": "I-0003", "agent_id": "claude-a", "status": "failed", "kind": "propose",
            "error_code": "E_OUTPUT_FORMAT", "recoverable": True, "attempt": 1
        })

        self._orchestrate(FakeWakeAdapter())

        repair = list((self.internal / "instructions" / "claude-a").glob("*-repair-*.json"))
        self.assertEqual(len(repair), 1)

    def test_third_invalid_output_escalates_retry_exhausted(self) -> None:
        _write_json(self.internal / "receipts/claude-a/I-0003-failed.json", {
            "instruction_id": "I-0003", "agent_id": "claude-a", "status": "failed", "kind": "propose",
            "error_code": "E_OUTPUT_FORMAT", "recoverable": True, "attempt": 3
        })

        result = self._orchestrate(FakeWakeAdapter())

        self.assertIn("E_RETRY_EXHAUSTED", result.blocking_error_codes)

    def test_unconfigured_adapter_returns_platform_unavailable(self) -> None:
        result = self._orchestrate(UnavailableWakeAdapter())

        self.assertIn(E_PLATFORM_UNAVAILABLE, result.blocking_error_codes)

    def test_unbound_responses_cannot_create_candidate(self) -> None:
        state = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        state["stage"] = "cross_response"
        _write_json(self.internal / "state.json", state)
        for sequence, agent in enumerate(("claude-a", "codex-a"), start=1):
            response = self.internal / "views" / agent / "outputs" / "交叉回应文档.md"
            response.parent.mkdir(parents=True, exist_ok=True)
            response.write_text("# %s response\n" % agent, encoding="utf-8")
            _write_json(self.internal / "receipts" / agent / ("I-%04d-completed.json" % sequence), {
                "instruction_id": "I-%04d" % sequence,
                "agent_id": agent,
                "status": "completed",
                "kind": "respond",
                "output_path": response.relative_to(self.workspace).as_posix(),
                "output_sha256": sha256_file(response),
            })

        result = self._orchestrate(FakeWakeAdapter(), open_candidate=False)
        updated = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        document = self.workspace / updated["content_authority"]["discussion_path"]

        self.assertEqual(result.stage, "cross_response")
        self.assertFalse(updated["candidate_decision_ids"])
        self.assertFalse(updated["confirmed_decision_ids"])
        self.assertTrue(updated["monitoring"]["enabled"])
        self.assertEqual(updated["monitoring"]["status"], "active")
        self.assertNotIn("candidate_delivery", updated)
        self.assertEqual(updated["response_status"], {"claude-a": "pending", "codex-a": "pending"})
        self.assertEqual(updated["content_authority"]["sha256"], sha256_file(document))

    def test_open_candidate_requests_each_stop_and_waits_for_bound_receipts(self) -> None:
        state = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        participants = ["claude-a", "codex-a", "openclaw"]
        state["expected_participants"] = participants
        state["submission_status"] = {agent: "submitted" for agent in participants}
        state["response_status"] = {agent: "submitted" for agent in participants}
        state["stage"] = "candidate_decision"
        state["candidate_decision_ids"] = ["C-0001"]
        state["monitoring"] = {"enabled": True, "status": "active"}
        _write_json(self.internal / "state.json", state)
        candidate = self.workspace / "候选决策.docx"
        candidate.write_bytes(b"opened-candidate")
        state["candidate_delivery"] = {
            "path": candidate.name,
            "sha256": sha256_file(candidate),
            "opened": True,
            "opened_at": "2026-09-13T12:00:00+08:00",
        }
        _write_json(self.internal / "state.json", state)

        wake = FakeWakeAdapter()
        first = self._orchestrate(wake)
        state = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        stop_paths = {
            agent: sorted((self.internal / "instructions" / agent).glob("*-stop-*.json"))
            for agent in participants
        }
        self.assertEqual(first.stage, "candidate_decision")
        self.assertTrue(state["monitoring"]["enabled"])
        self.assertEqual(state["monitoring"]["status"], "stopping")
        self.assertTrue(all(len(paths) == 1 for paths in stop_paths.values()))
        stop_instructions = {
            agent: Instruction.from_dict(json.loads(paths[0].read_text(encoding="utf-8")))
            for agent, paths in stop_paths.items()
        }
        self.assertEqual(stop_instructions["openclaw"].platform_id, "openclaw")
        self.assertEqual(stop_instructions["openclaw"].session_id, "session-openclaw")
        self.assertTrue(all(item.to_dict().get("discussion_id") == state["discussion_id"] for item in stop_instructions.values()))
        self.assertEqual({request.agent_id for request in wake.requests}, set(participants))

        def complete_stop(agent: str, *, signed: bool = True, removal_verified: bool = True) -> None:
            instruction = stop_instructions[agent]
            now = datetime.now(timezone.utc).astimezone()
            issued_at = datetime.fromisoformat(instruction.issued_at)
            stopped_at = max(now - timedelta(seconds=10), issued_at + timedelta(milliseconds=1))
            marker = self.internal / "receipts" / agent / (instruction.instruction_id + "-stop-marker.txt")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("stopped\n", encoding="utf-8")
            if signed:
                proof = {
                    "discussion_id": "red-contract",
                    "agent_id": agent,
                    "instruction_id": instruction.instruction_id,
                    "instruction_sha256": instruction.sha256,
                    "platform_id": instruction.platform_id,
                    "session_id": instruction.session_id,
                    "stopped_at": stopped_at.isoformat(timespec="milliseconds"),
                    "mechanism": "session_monitor_stop",
                    "action_verified": True,
                    "target": "current session monitor instance",
                    "proof_reference": ".multiagent/audit/platform-evidence/proofs/%s/%s.json" % (
                        agent, instruction.instruction_id,
                    ),
                }
                if instruction.platform_id == "openclaw":
                    proof.update({
                        "automation_job_id": "openclaw-job-1",
                        "removal_verified": removal_verified,
                        "removal_checked_at": (stopped_at + timedelta(milliseconds=1)).isoformat(timespec="milliseconds"),
                    })
                proof_artifact = {
                    field: proof[field]
                    for field in (
                        "discussion_id", "instruction_id", "instruction_sha256", "agent_id", "platform_id", "session_id",
                        "stopped_at", "mechanism", "target", "action_verified",
                    )
                }
                if instruction.platform_id == "openclaw":
                    proof_artifact.update({field: proof[field] for field in (
                        "automation_job_id", "removal_verified", "removal_checked_at",
                    )})
                proof_path = self.workspace / proof["proof_reference"]
                _write_json(proof_path, proof_artifact)
                proof["proof_sha256"] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
                signed_proof = sign_payload(proof, private_key_path=self.private_key_path, key_id=self.key_id)
                _write_json(stop_attestation_path(self.workspace, instruction), signed_proof)
                evidence = load_stop_attestation(self.workspace, instruction)
            else:
                evidence = {"mode": "restricted_view", "contract_validation": "passed"}
            _write_json(self.internal / "receipts" / agent / (instruction.instruction_id + "-completed.json"), {
                "instruction_id": instruction.instruction_id,
                "agent_id": agent,
                "kind": "stop",
                "status": "completed",
                "runtime_version": instruction.runtime_version,
                "state_revision": instruction.state_revision,
                "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
                "attempt": instruction.attempt,
                "output_path": marker.relative_to(self.workspace).as_posix(),
                "output_sha256": sha256_file(marker),
                "isolation_evidence": evidence,
            })

        # A marker and self-asserted receipt cannot move the stop gate.
        complete_stop("claude-a", signed=False)
        self.assertEqual(self._orchestrate(wake).stage, "candidate_decision")
        self.assertTrue(json.loads((self.internal / "state.json").read_text(encoding="utf-8"))["monitoring"]["enabled"])
        complete_stop("claude-a")
        complete_stop("codex-a")
        with self.assertRaises(WorkflowError) as rejected:
            complete_stop("openclaw", removal_verified=False)
        self.assertEqual(rejected.exception.code, "E_ISOLATION_UNVERIFIED")
        self.assertEqual(self._orchestrate(wake).stage, "candidate_decision")
        complete_stop("openclaw")
        self.assertEqual(self._orchestrate(wake).stage, "user_confirmation")
        stopped = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        self.assertFalse(stopped["monitoring"]["enabled"])
        self.assertEqual(stopped["monitoring"]["status"], "stopped")
        self.assertTrue(stopped["monitoring"]["stop_requested_at"])
        self.assertTrue(stopped["monitoring"]["stopped_at"])
        self.assertEqual(self._orchestrate(wake).stage, "user_confirmation")
        self.assertEqual(len(list((self.internal / "instructions").rglob("*-stop-*.json"))), len(participants))

    def test_four_wake_events_are_preserved_and_completed_instructions_are_not_rewoken(self) -> None:
        wake = FakeWakeAdapter()
        for sequence, agent in enumerate(("claude-a", "codex-a"), start=1):
            _write_json(self.internal / "receipts" / agent / ("B-%d-accepted.json" % sequence), {
                "instruction_id": "B-%d" % sequence,
                "agent_id": agent,
                "status": "accepted",
                "kind": "bootstrap",
            })

        self._orchestrate(wake)
        for agent in ("claude-a", "codex-a"):
            proposal = self.internal / "views" / agent / "outputs" / "提案文档.md"
            proposal.parent.mkdir(parents=True, exist_ok=True)
            proposal.write_text("# %s proposal\n" % agent, encoding="utf-8")
            instruction_path = next((self.internal / "instructions" / agent).glob("*-propose-*.json"))
            instruction = Instruction.from_dict(json.loads(instruction_path.read_text(encoding="utf-8")))
            evidence = self._attest(instruction)
            _write_json(self.internal / "receipts" / agent / (instruction.instruction_id + "-completed.json"), {
                "instruction_id": instruction.instruction_id,
                "agent_id": agent,
                "status": "completed",
                "kind": "propose",
                "output_path": instruction.output_path,
                "output_sha256": sha256_file(proposal),
                "isolation_evidence": evidence,
            })

        self._orchestrate(wake)
        self._orchestrate(wake)

        audit = json.loads((self.internal / "audit/wake-events.json").read_text(encoding="utf-8"))
        events = audit["events"]
        identities = [(event["agent_id"], event["instruction_id"]) for event in events]
        self.assertEqual(len(wake.requests), 4)
        self.assertEqual(len(events), 4)
        self.assertEqual(len(set(identities)), 4)
        self.assertEqual({event["status"] for event in events}, {"accepted"})
        self.assertTrue((self.internal / "views" / "claude-a" / "outputs" / "提案文档.md").is_file())
        self.assertFalse((self.internal / "archive" / "proposals" / "claude-a-提案文档.md").exists())

    def test_issued_instruction_carries_and_hashes_state_discussion_id(self) -> None:
        state = json.loads((self.internal / "state.json").read_text(encoding="utf-8"))
        instruction = _new_instruction(state, self.workspace, "claude-a", "propose")
        self.assertEqual(instruction.to_dict().get("discussion_id"), state["discussion_id"])
        self.assertTrue(instruction.verify_sha256())
        changed = instruction.to_dict()
        changed["discussion_id"] = "another-discussion"
        with self.assertRaises(WorkflowError) as caught:
            Instruction.from_dict(changed)
        self.assertEqual(caught.exception.code, "E_HASH")

    def test_orchestrator_embeds_openclaw_directive_for_every_instruction_kind_and_hashes_it(self) -> None:
        """Every coordinator-issued OpenClaw action carries an immutable directive."""
        state = _workspace_state()
        state.update({"coordinator": "claude-a", "runtime_distribution": {"version": "1.0.0"}})
        required_markers = (
            "OpenClaw 参与者操作指令模板",
            "openclaw cron list --json",
            "不得只报告状态",
            "无变化也如实报告",
            "不得修改 .multiagent/state.json",
            "openclaw cron rm",
        )

        for kind in ("bootstrap", "propose", "respond", "repair", "stop"):
            kwargs = {}
            if kind == "repair":
                failed_receipt_path = self.internal / "receipts" / "openclaw" / "failed-receipt.json"
                _write_json(failed_receipt_path, {"instruction_id": "I-000001", "status": "invalid"})
                kwargs["failure_receipt"] = {
                    "_path": failed_receipt_path,
                    "instruction_id": "I-000001",
                    "status": "invalid",
                }
            instruction = _new_instruction(state, self.workspace, "openclaw", kind, **kwargs)
            self.assertIsInstance(instruction, Instruction)
            self.assertGreater(len(instruction.task_prompt), 40)
            for prompt_section in ("任务：", "执行边界：", "安全说明：", "允许输入：", "唯一业务输出：", "验收："):
                self.assertIn(prompt_section, instruction.task_prompt)
            self.assertEqual({key: instruction.access_scope[key] for key in (
                "mode", "view_root", "allowed_read_roots", "allowed_write_roots",
                "requires_platform_enforcement", "independence_claim_requires_enforcement_receipt",
            )}, {
                "mode": "sealed_view",
                "view_root": ".multiagent/views/openclaw",
                "allowed_read_roots": [".multiagent/views/openclaw/inputs"],
                "allowed_write_roots": [
                    ".multiagent/views/openclaw/outputs",
                    ".multiagent/receipts/openclaw",
                ],
                "requires_platform_enforcement": True,
                "independence_claim_requires_enforcement_receipt": True,
            })
            self.assertTrue(instruction.access_scope["security_note"])
            self.assertIsNotNone(instruction.operational_directive)
            for marker in required_markers:
                self.assertIn(marker, instruction.operational_directive)
            self.assertTrue(instruction.verify_sha256())

            tampered = instruction.to_dict()
            tampered["operational_directive"] += "\nTAMPERED"
            with self.assertRaises(WorkflowError):
                Instruction.from_dict(tampered)

        # The directive is scoped to OpenClaw; unrelated participants do not inherit it.
        other = _new_instruction(state, self.workspace, "claude-a", "propose")
        self.assertIsNone(other.operational_directive)
        self.assertTrue(other.verify_sha256())

    def test_init_publishes_hashed_bootstrap_directive_only_for_openclaw(self) -> None:
        """The real initializer publishes the same immutable contract at bootstrap."""
        init_workspace = self.workspace / "init-openclaw"
        command = [
            sys.executable,
            str(SCRIPTS_DIR / "init_discussion.py"),
            str(init_workspace),
            "claude",
            "claude",
            "openclaw",
            "--coordinator-platform",
            "deepseek",
            "--coordinator-session",
            "session-001",
            "--participant-binding", "claude=deepseek:session-001",
            "--participant-binding", "openclaw=openclaw:openclaw-session",
            "--attestation-public-key-b64", self.public_key_b64,
            "--attestation-key-id", self.key_id,
            "--discussion-id",
            "init-openclaw-test",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", env=os.environ.copy(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        openclaw_path = next((init_workspace / ".multiagent" / "instructions" / "openclaw").glob("*-bootstrap-*.json"))
        claude_path = next((init_workspace / ".multiagent" / "instructions" / "claude").glob("*-bootstrap-*.json"))
        openclaw = Instruction.from_dict(json.loads(openclaw_path.read_text(encoding="utf-8")))
        claude = Instruction.from_dict(json.loads(claude_path.read_text(encoding="utf-8")))
        self.assertIsNotNone(openclaw.operational_directive)
        self.assertIn("openclaw cron list --json", openclaw.operational_directive)
        self.assertTrue(openclaw.verify_sha256())
        self.assertIsNone(claude.operational_directive)
        self.assertTrue(claude.verify_sha256())


if __name__ == "__main__":
    unittest.main()
