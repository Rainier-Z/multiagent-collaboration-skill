"""Crash-consistency tests for modern coordinator instruction publication."""

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
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from events import EventStream  # noqa: E402
from orchestrate_discussion import FakeWakeAdapter, OrchestrationResult, _issue  # noqa: E402
from transactions import EventTransaction, recover_transactions  # noqa: E402
from workflow_core import load_state  # noqa: E402


class ProcessLoss(BaseException):
    pass


class InstructionTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="instruction-transaction-"))
        command = [
            sys.executable, str(SCRIPTS / "init_discussion.py"), str(self.workspace),
            "coordinator", "coordinator", "agent-b",
            "--security-mode", "normal",
            "--coordinator-platform", "codex", "--coordinator-session", "session-coordinator",
            "--participant-binding", "coordinator=codex:session-coordinator",
            "--participant-binding", "agent-b=claude:session-agent-b",
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_crash_before_instruction_event_rolls_back_instruction_and_sequence(self) -> None:
        def crash_after_artifact(phase: str, _journal: dict[str, object]) -> None:
            if phase == "artifacts_written":
                raise ProcessLoss()

        def faulting_transaction(*args: object, **kwargs: object) -> EventTransaction:
            return EventTransaction(*args, fault_hook=crash_after_artifact, **kwargs)

        state = load_state(self.workspace)
        with patch("orchestrate_discussion.EventTransaction", side_effect=faulting_transaction):
            with self.assertRaises(ProcessLoss):
                _issue(
                    self.workspace, state, FakeWakeAdapter(), OrchestrationResult(),
                    "coordinator", "final_ack",
                )

        recover_transactions(self.workspace)
        recovered = load_state(self.workspace)
        self.assertEqual(recovered.get("instruction_sequence", 0), 0)
        self.assertFalse(list((self.workspace / ".multiagent" / "instructions").rglob("*-final_ack-*.json")))

        result = OrchestrationResult()
        _issue(self.workspace, recovered, FakeWakeAdapter(), result, "agent-b", "final_ack")
        self.assertEqual(result.issued_instruction_ids, ["I-000001"])
        events = EventStream(self.workspace).read()
        self.assertEqual(
            [event["payload"]["agent_id"] for event in events if event["event_type"] == "instruction_issued"],
            ["agent-b"],
        )


if __name__ == "__main__":
    unittest.main()
