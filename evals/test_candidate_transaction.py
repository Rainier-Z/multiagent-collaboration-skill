from __future__ import annotations

import json
import unittest
import sys
from unittest.mock import patch

from evals.test_round_loop import AtomicRoundLoopTests
from orchestrate_discussion import FakeWakeAdapter, orchestrate_once, _candidate_transition
import confirm_decision


class CandidateTransactionTests(AtomicRoundLoopTests):
    """Candidate Markdown and state/event must commit together."""

    def test_candidate_materialization_failure_does_not_partially_update(self) -> None:
        self._drive_proposals()
        for agent in self.participants:
            instruction = self._instructions(agent, "respond")[-1]
            self._write_receipt(instruction, "### 共识点\n- bounded\n### 分歧点\n### 新问题\n")
        discussion = next(self.workspace.glob("*讨论文档_*.md"))
        before_discussion = discussion.read_bytes()
        before_state = (self.internal / "state.json").read_bytes()

        with patch("orchestrate_discussion.commit_transaction", side_effect=RuntimeError("candidate crash")):
            with self.assertRaises(RuntimeError):
                _candidate_transition(
                    self.workspace,
                    self._read_state(),
                    self.participants,
                    expected_revision=self._read_state()["revision"],
                )

        self.assertEqual(discussion.read_bytes(), before_discussion)
        self.assertEqual((self.internal / "state.json").read_bytes(), before_state)
        events = [json.loads(line) for line in (self.internal / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any(event.get("event_type") == "candidate_decision_created" for event in events))

    def test_modern_confirmation_failure_keeps_markdown_and_state_aligned(self) -> None:
        self._drive_proposals()
        for agent in self.participants:
            instruction = self._instructions(agent, "respond")[-1]
            self._write_receipt(instruction, "### 共识点\n- bounded\n### 分歧点\n### 新问题\n")
        orchestrate_once(
            self.workspace, FakeWakeAdapter(), actor="coordinator",
            platform_id="codex", session_id="session-coordinator",
            open_candidate=False,
        )
        metadata = self._read_state()["rounds"]["1"]
        assessment = {
            "round": 1, "new_substantive_issues": [], "unanswered_arguments": [],
            "based_on_snapshot_path": metadata["snapshot_path"],
            "based_on_snapshot_sha256": metadata["snapshot_sha256"],
            "based_on_revision": metadata["snapshot_published_revision"],
            "new_evidence": [], "remaining_disagreements": [],
            "positions": {agent: ["bounded"] for agent in self.participants},
            "value_conflicts": [], "more_discussion": False,
            "requires_human_decision": True, "converged": True, "reason": "done",
        }
        path = self.internal / "convergence" / "round-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(assessment, ensure_ascii=False), encoding="utf-8")
        result = orchestrate_once(
            self.workspace, FakeWakeAdapter(), actor="coordinator",
            platform_id="codex", session_id="session-coordinator",
            open_candidate=True, opener=lambda _path: True,
        )
        self.assertEqual(result.stage, "human_review")
        discussion = next(self.workspace.glob("*讨论文档_*.md"))
        before_discussion = discussion.read_bytes()
        before_state = (self.internal / "state.json").read_bytes()
        argv = [
            "confirm_decision.py", "--state", str(self.internal / "state.json"),
            "--actor", "coordinator", "--platform-id", "codex",
            "--session-id", "session-coordinator", "--candidate-id", "C-0001",
            "--confirm-text", "确认 C-0001",
        ]
        with patch.object(sys, "argv", argv), patch("confirm_decision.EventTransaction.commit", side_effect=RuntimeError("publish crash")):
            self.assertEqual(confirm_decision.main(), confirm_decision.EXIT_ERR)
        self.assertEqual(discussion.read_bytes(), before_discussion)
        self.assertEqual((self.internal / "state.json").read_bytes(), before_state)


if __name__ == "__main__":
    unittest.main()
