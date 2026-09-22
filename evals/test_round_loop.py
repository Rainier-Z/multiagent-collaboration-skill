from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from orchestrate_discussion import FakeWakeAdapter, orchestrate_once
from participant_runtime.protocol import Instruction
from workflow_core import sha256_file


class AtomicRoundLoopTests(unittest.TestCase):
    """PR1 contract: real sealed files carry a complete round transition."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="multiagent-round-loop-"))
        participants = ["coordinator", "agent-b", "agent-c"]
        command = [
            sys.executable, str(SCRIPTS / "init_discussion.py"), str(self.workspace),
            "coordinator", *participants,
            "--security-mode", "normal",
            "--coordinator-platform", "codex",
            "--coordinator-session", "session-coordinator",
            "--participant-binding", "coordinator=codex:session-coordinator",
            "--participant-binding", "agent-b=claude:session-b",
            "--participant-binding", "agent-c=openclaw:session-c",
            "--max-response-rounds", "3",
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.internal = self.workspace / ".multiagent"
        self.participants = participants
        self._write_bootstrap_receipts()

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _read_state(self) -> dict:
        return json.loads((self.internal / "state.json").read_text(encoding="utf-8"))

    def _instructions(self, agent: str, kind: str) -> list[Instruction]:
        paths = sorted((self.internal / "instructions" / agent).glob(f"*-{kind}-*.json"))
        return [Instruction.from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in paths]

    def _write_receipt(self, instruction: Instruction, text: str, *, evidence: bool = False) -> None:
        output = self.workspace / instruction.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        payload = {
            "instruction_id": instruction.instruction_id,
            "agent_id": instruction.agent_id,
            "kind": instruction.kind,
            "status": "completed",
            "output_path": instruction.output_path,
            "output_sha256": sha256_file(output),
            "isolation_evidence": {"mode": "restricted_view", "contract_validation": "passed"},
        }
        if evidence:
            payload["isolation_evidence"] = {"mode": "normal", "contract_validation": "passed"}
        receipt_path = self.internal / "receipts" / instruction.agent_id / f"{instruction.instruction_id}-completed.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _write_bootstrap_receipts(self) -> None:
        for index, agent in enumerate(self.participants, 1):
            path = self.internal / "receipts" / agent / f"I-bootstrap-{index}-completed.json"
            path.write_text(json.dumps({
                "instruction_id": f"I-{index:04d}-bootstrap-{agent}",
                "agent_id": agent, "kind": "bootstrap", "status": "completed",
            }), encoding="utf-8")

    def _write_assessment(self, round_number: int, *, converged: bool = False) -> None:
        path = self.internal / "convergence" / f"round-{round_number}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = self._read_state()["rounds"][str(round_number)]
        path.write_text(json.dumps({
            "round": round_number,
            "based_on_snapshot_path": metadata["snapshot_path"],
            "based_on_snapshot_sha256": metadata["snapshot_sha256"],
            "based_on_revision": metadata["snapshot_published_revision"],
            "new_substantive_issues": [] if converged else ["待验证"],
            "unanswered_arguments": [] if converged else ["待回应"],
            "new_evidence": ["round snapshot"],
            "remaining_disagreements": [] if converged else ["待回应"],
            "positions": {agent: ["公共快照"] for agent in self.participants},
            "value_conflicts": [],
            "more_discussion": not converged,
            "requires_human_decision": converged,
            "converged": converged,
            "reason": "测试收敛评估",
        }, ensure_ascii=False), encoding="utf-8")

    def _drive_proposals(self) -> None:
        wake = FakeWakeAdapter()
        orchestrate_once(
            self.workspace, wake, actor="coordinator",
            platform_id="codex", session_id="session-coordinator", open_candidate=False,
        )
        # Modern orchestration publishes instructions/events only.  Activation
        # belongs to participant_monitor, so the coordinator must not invoke a
        # legacy WakeAdapter even when one is supplied by the caller.
        self.assertEqual(wake.requests, [])
        for agent in self.participants:
            self._write_receipt(self._instructions(agent, "propose")[-1], f"# {agent}\n独立提案。\n", evidence=True)
        result = orchestrate_once(
            self.workspace, FakeWakeAdapter(), actor="coordinator",
            platform_id="codex", session_id="session-coordinator", open_candidate=False,
        )
        self.assertEqual(result.stage, "cross_response")

    def test_round_one_to_round_two_is_atomic_and_sealed(self) -> None:
        self._drive_proposals()
        for agent in self.participants:
            instruction = self._instructions(agent, "respond")[-1]
            self.assertTrue(any(path.endswith("/discussion.md") for path in instruction.input_paths))
            self._write_receipt(instruction, "### 共识点\n- 基础方案\n### 分歧点\n- 成本与速度\n### 新问题\n- 需要验证\n")

        result = orchestrate_once(
            self.workspace, FakeWakeAdapter(), actor="coordinator",
            platform_id="codex", session_id="session-coordinator", open_candidate=False,
        )
        self.assertEqual(result.stage, "cross_response")
        state = self._read_state()
        snapshot = self.internal / "rounds" / "round-1.md"
        self.assertTrue(snapshot.is_file())
        self.assertEqual(state["round"], 1)
        self.assertEqual(state["rounds"]["1"]["snapshot_path"], ".multiagent/rounds/round-1.md")
        self.assertEqual(state["rounds"]["1"]["snapshot_sha256"], sha256_file(snapshot))
        self.assertEqual(state["rounds"]["1"]["status"], "snapshot_published")

        # The coordinator now reads the sealed snapshot before writing its
        # semantic assessment.  Only the next call may advance the round.
        self._write_assessment(1)
        result = orchestrate_once(
            self.workspace, FakeWakeAdapter(), actor="coordinator",
            platform_id="codex", session_id="session-coordinator", open_candidate=False,
        )
        self.assertEqual(result.stage, "cross_response")
        state = self._read_state()
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["rounds"]["1"]["status"], "completed")

        events = [json.loads(line) for line in (self.internal / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        published = [event for event in events if event.get("event_type") == "round_snapshot_published"]
        self.assertEqual(len(published), 1)
        completed = [event for event in events if event.get("event_type") == "round_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"]["next_round"], 2)
        self.assertEqual(set(completed[0]["payload"]["instruction_ids"]), set(result.issued_instruction_ids))

        for agent in self.participants:
            instruction = self._instructions(agent, "respond")[-1]
            self.assertIn("round-1.md", " ".join(instruction.input_paths))
            self.assertNotIn("outputs/round-1", " ".join(instruction.input_paths))
            self._write_receipt(instruction, "### 共识点\n- 基础方案\n### 分歧点\n### 新问题\n")

    def test_completed_round_snapshot_is_immutable(self) -> None:
        self._drive_proposals()
        for agent in self.participants:
            self._write_receipt(self._instructions(agent, "respond")[-1], "### 共识点\n- x\n### 分歧点\n- unresolved\n### 新问题\n- issue\n")
        orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)
        snapshot = self.internal / "rounds" / "round-1.md"
        original = snapshot.read_bytes()
        snapshot.write_bytes(original + b"tampered\n")
        with self.assertRaises(Exception):
            orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)

    def test_preseeded_assessment_with_wrong_snapshot_binding_is_rejected(self) -> None:
        self._drive_proposals()
        for agent in self.participants:
            self._write_receipt(self._instructions(agent, "respond")[-1], "### 共识点\n- x\n### 分歧点\n### 新问题\n")
        # This assessment was prepared before the immutable round snapshot
        # existed, therefore it cannot authorize the next transition.
        path = self.internal / "convergence" / "round-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "round": 1, "based_on_snapshot_path": ".multiagent/rounds/round-1.md",
            "based_on_snapshot_sha256": "0" * 64, "based_on_revision": 1,
            "new_substantive_issues": [], "unanswered_arguments": [], "new_evidence": [],
            "remaining_disagreements": [], "positions": {}, "value_conflicts": [],
            "more_discussion": False, "requires_human_decision": True,
            "converged": True, "reason": "preseeded",
        }), encoding="utf-8")
        with self.assertRaises(Exception):
            orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)


if __name__ == "__main__":
    unittest.main()
