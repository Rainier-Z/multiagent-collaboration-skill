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

from events import EventStream, reconcile_instruction_events  # noqa: E402
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

    def test_crash_before_instruction_event_allows_same_agent_retry(self) -> None:
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
        _issue(self.workspace, recovered, FakeWakeAdapter(), result, "coordinator", "final_ack")
        self.assertEqual(result.issued_instruction_ids, ["I-000001"])
        events = [event for event in EventStream(self.workspace).read() if event["event_type"] == "instruction_issued"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "instruction-I-000001")
        self.assertEqual(events[0]["payload"]["agent_id"], "coordinator")
        self.assertEqual(load_state(self.workspace)["instruction_sequence"], 1)

    def test_crash_after_instruction_event_recovers_without_reissuing(self) -> None:
        def crash_after_event(phase: str, _journal: dict[str, object]) -> None:
            if phase == "event_appended":
                raise ProcessLoss()

        def faulting_transaction(*args: object, **kwargs: object) -> EventTransaction:
            return EventTransaction(*args, fault_hook=crash_after_event, **kwargs)

        state = load_state(self.workspace)
        with patch("orchestrate_discussion.EventTransaction", side_effect=faulting_transaction):
            with self.assertRaises(ProcessLoss):
                _issue(
                    self.workspace, state, FakeWakeAdapter(), OrchestrationResult(),
                    "coordinator", "final_ack",
                )

        self.assertEqual(load_state(self.workspace).get("instruction_sequence", 0), 0)
        events_before_recovery = [
            event for event in EventStream(self.workspace).read()
            if event["event_type"] == "instruction_issued"
        ]
        self.assertEqual([event["event_id"] for event in events_before_recovery], ["instruction-I-000001"])

        recover_transactions(self.workspace)
        recovered = load_state(self.workspace)
        self.assertEqual(recovered["instruction_sequence"], 1)
        instructions = [
            path for path in (self.workspace / ".multiagent" / "instructions").rglob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("instruction_id") == "I-000001"
        ]
        self.assertEqual(len(instructions), 1)

        reconcile_instruction_events(self.workspace, recovered)
        final_events = [
            event for event in EventStream(self.workspace).read()
            if event["event_type"] == "instruction_issued"
        ]
        self.assertEqual(
            [event["event_id"] for event in final_events if event["event_id"] == "instruction-I-000001"],
            ["instruction-I-000001"],
        )
        self.assertEqual(json.loads(instructions[0].read_text(encoding="utf-8"))["instruction_id"], "I-000001")


if __name__ == "__main__":
    unittest.main()
