from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from orchestrate_discussion import FakeWakeAdapter, orchestrate_once
from participant_runtime.protocol import Instruction
from workflow_core import sha256_file, WorkflowError


class ProposalTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="multiagent-proposal-tx-"))
        participants = ["coordinator", "agent-b"]
        args = [
            sys.executable, str(SCRIPTS / "init_discussion.py"), str(self.workspace),
            "coordinator", *participants, "--security-mode", "normal",
            "--coordinator-platform", "codex", "--coordinator-session", "session-coordinator",
            "--participant-binding", "coordinator=codex:session-coordinator",
            "--participant-binding", "agent-b=claude:session-b",
        ]
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.participants = participants
        self.internal = self.workspace / ".multiagent"
        for index, agent in enumerate(participants, 1):
            path = self.internal / "receipts" / agent / ("bootstrap-%d-completed.json" % index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "instruction_id": "bootstrap-%d" % index, "agent_id": agent,
                "kind": "bootstrap", "status": "completed",
            }), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def state(self) -> dict:
        return json.loads((self.internal / "state.json").read_text(encoding="utf-8"))

    def instructions(self, agent: str, kind: str) -> list[Instruction]:
        return [
            Instruction.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((self.internal / "instructions" / agent).glob("*-%s-*.json" % kind))
        ]

    def write_proposals(self) -> None:
        orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)
        for agent in self.participants:
            instruction = self.instructions(agent, "propose")[-1]
            output = self.workspace / instruction.output_path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("# %s\nproposal\n" % agent, encoding="utf-8")
            receipt = {
                "instruction_id": instruction.instruction_id, "agent_id": agent,
                "kind": "propose", "status": "completed", "output_path": instruction.output_path,
                "output_sha256": sha256_file(output),
                "isolation_evidence": {"mode": "normal", "contract_validation": "passed"},
            }
            path = self.internal / "receipts" / agent / (instruction.instruction_id + "-completed.json")
            path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_proposal_merge_failure_leaves_no_partial_transition(self) -> None:
        self.write_proposals()
        discussion = next(self.workspace.glob("*讨论文档_*.md"))
        before_discussion = discussion.read_bytes()
        before_state = (self.internal / "state.json").read_bytes()

        def fail_commit(*_args, **_kwargs):
            raise RuntimeError("simulated proposal transaction crash")

        with patch("orchestrate_discussion.commit_transaction", side_effect=fail_commit):
            with self.assertRaises(RuntimeError):
                orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)

        self.assertEqual(discussion.read_bytes(), before_discussion)
        self.assertEqual((self.internal / "state.json").read_bytes(), before_state)
        self.assertFalse(list(self.internal.glob("archive/proposals/manifest.json")))
        self.assertFalse(list(self.internal.rglob("*-respond-*.json")))
        for agent in self.participants:
            self.assertTrue((self.workspace / self.instructions(agent, "propose")[-1].output_path).is_file())

    def test_proposal_merge_commits_manifest_deletion_and_round_one_instructions(self) -> None:
        self.write_proposals()
        result = orchestrate_once(self.workspace, FakeWakeAdapter(), actor="coordinator", platform_id="codex", session_id="session-coordinator", open_candidate=False)
        self.assertEqual(result.stage, "cross_response")
        self.assertEqual(self.state()["round"], 1)
        self.assertFalse(list((self.internal / "views").rglob("*/outputs/提案文档.md")))
        self.assertTrue((self.internal / "archive/proposals/manifest.json").is_file())
        self.assertEqual(len(list(self.internal.rglob("*-respond-*.json"))), len(self.participants))
        events = [json.loads(line) for line in (self.internal / "audit/events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event.get("event_type") == "proposal_merged" for event in events))


if __name__ == "__main__":
    unittest.main()
