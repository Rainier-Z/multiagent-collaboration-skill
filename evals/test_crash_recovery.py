#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real-filesystem crash and recovery tests for the v0.2 runtime loop.

These tests deliberately raise ``BaseException`` at transaction boundaries to
model process loss.  The transaction code intentionally leaves a journal for
that class of interruption; the next process must either finish the recorded
event/state transition or roll back the visible artifacts.  The monitor tests
exercise the separate activation-result fence when cursor persistence fails
after an external activation has already succeeded.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from adapters.common.wake_protocol import ActivationResult, WakeRequest  # noqa: E402
from events import EventStream, EventStreamError  # noqa: E402
from participant_monitor import ParticipantMonitor  # noqa: E402
from transactions import (  # noqa: E402
    TransactionRecoveryError,
    commit_round_transition,
    recover_transactions,
)


class SimulatedProcessLoss(BaseException):
    """A non-Exception crash that leaves the durable transaction journal."""


class FakeBridge:
    def __init__(self) -> None:
        self.requests: list[WakeRequest] = []

    def activate(self, request: WakeRequest) -> ActivationResult:
        self.requests.append(request)
        return ActivationResult("activated", evidence="fake-dispatch")


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="crash-recovery-test-"))
        state_path = self.workspace / ".multiagent" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"revision": 1, "stage": "cross_response", "round": 0}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _state(self) -> dict[str, object]:
        return json.loads(
            (self.workspace / ".multiagent" / "state.json").read_text(encoding="utf-8")
        )

    def _round_transition(self, *, fault_hook=None):
        return commit_round_transition(
            self.workspace,
            round_number=1,
            snapshot_path=".multiagent/rounds/round-1.md",
            snapshot="# Round 1\n",
            next_round_instructions={
                ".multiagent/instructions/claude/round-1.json": {
                    "instruction_id": "round-1-claude",
                    "round": 1,
                }
            },
            state_update={"round": 1, "stage": "cross_response"},
            expected_revision=1,
            fault_hook=fault_hook,
        )

    def test_round_transition_crash_during_artifacts_rolls_back_everything(self) -> None:
        """A round cannot leave a snapshot without its next-round instruction."""

        applied = 0

        def crash_after_first_artifact(phase: str, _journal: dict[str, object]) -> None:
            nonlocal applied
            if phase == "artifacts_written":
                applied += 1
                if applied == 1:
                    raise SimulatedProcessLoss("process disappeared mid-round transition")

        with self.assertRaises(SimulatedProcessLoss):
            self._round_transition(fault_hook=crash_after_first_artifact)

        # The fault is injected after one real os.replace(), so this checks
        # recovery from a genuinely half-written filesystem, not an in-memory
        # transaction mock.
        self.assertTrue(
            (self.workspace / ".multiagent/rounds/round-1.md").exists()
            or (self.workspace / ".multiagent/instructions/claude/round-1.json").exists()
        )
        self.assertEqual(self._state()["revision"], 1)
        recovered = recover_transactions(self.workspace)
        self.assertEqual(recovered[0]["action"], "rolled_back")
        self.assertFalse((self.workspace / ".multiagent/rounds/round-1.md").exists())
        self.assertFalse(
            (self.workspace / ".multiagent/instructions/claude/round-1.json").exists()
        )
        self.assertEqual(self._state()["revision"], 1)
        self.assertEqual(EventStream(self.workspace).read(), [])

    def test_round_transition_crash_after_event_finishes_state_atomically(self) -> None:
        """Once the event exists, recovery publishes the recorded next state."""

        def crash_after_event(phase: str, _journal: dict[str, object]) -> None:
            if phase == "event_appended":
                raise SimulatedProcessLoss("process disappeared after event append")

        with self.assertRaises(SimulatedProcessLoss):
            self._round_transition(fault_hook=crash_after_event)

        self.assertEqual(self._state()["revision"], 1)
        self.assertTrue((self.workspace / ".multiagent/rounds/round-1.md").is_file())
        self.assertTrue(
            (self.workspace / ".multiagent/instructions/claude/round-1.json").is_file()
        )
        self.assertEqual(recover_transactions(self.workspace)[0]["action"], "completed_state_update")
        self.assertEqual(self._state()["revision"], 2)
        self.assertEqual(self._state()["round"], 1)
        self.assertEqual(len(EventStream(self.workspace).read()), 1)

    def test_round_transition_crash_after_state_write_is_marked_committed(self) -> None:
        """A crash after atomic state replacement must not replay business work."""

        def crash_after_state(phase: str, _journal: dict[str, object]) -> None:
            if phase == "state_updated":
                raise SimulatedProcessLoss("process disappeared after state replace")

        with self.assertRaises(SimulatedProcessLoss):
            self._round_transition(fault_hook=crash_after_state)

        self.assertEqual(self._state()["revision"], 2)
        recovered = recover_transactions(self.workspace)
        self.assertEqual(recovered[0]["action"], "marked_committed")
        self.assertEqual(len(EventStream(self.workspace).read()), 1)

    def test_event_and_instruction_disconnect_fails_closed(self) -> None:
        """An event with a missing referenced instruction cannot be accepted."""

        def crash_after_event(phase: str, _journal: dict[str, object]) -> None:
            if phase == "event_appended":
                raise SimulatedProcessLoss("process disappeared after event append")

        with self.assertRaises(SimulatedProcessLoss):
            self._round_transition(fault_hook=crash_after_event)

        instruction = self.workspace / ".multiagent/instructions/claude/round-1.json"
        instruction.unlink()
        with self.assertRaises(TransactionRecoveryError):
            recover_transactions(self.workspace)
        self.assertEqual(self._state()["revision"], 1)
        self.assertEqual(len(EventStream(self.workspace).read()), 1)

    def test_activation_success_before_cursor_crash_is_idempotent_on_restart(self) -> None:
        """A durable activation result fences a replay after cursor loss."""

        (self.workspace / ".multiagent/instructions/claude").mkdir(parents=True)
        (self.workspace / ".multiagent" / "state.json").write_text(
            json.dumps({
                "revision": 1,
                "participant_bindings": {
                    "claude": {"platform_id": "claude-code", "session_id": "s-1"}
                },
            }),
            encoding="utf-8",
        )
        (self.workspace / ".multiagent/instructions/claude/I-1.json").write_text(
            json.dumps({
                "instruction_id": "I-1",
                "runtime_version": "1.0.0",
                "platform_id": "claude-code",
                "session_id": "s-1",
            }),
            encoding="utf-8",
        )
        event = EventStream(self.workspace).append(
            "instruction_issued",
            {"agent_id": "claude", "instruction_id": "I-1"},
            event_id="evt-1",
        )
        first_bridge = FakeBridge()
        first = ParticipantMonitor(self.workspace, "claude", first_bridge)
        original_persist_cursor = first._persist_cursor
        failed_once = True

        def fail_cursor_once() -> None:
            nonlocal failed_once
            if failed_once:
                failed_once = False
                raise SimulatedProcessLoss("cursor write interrupted")
            original_persist_cursor()

        first._persist_cursor = fail_cursor_once  # type: ignore[method-assign]
        with self.assertRaises(SimulatedProcessLoss):
            first.poll()
        self.assertEqual(len(first_bridge.requests), 1)
        self.assertTrue(
            (self.workspace / ".multiagent/monitors/claude/activation-results.json").is_file()
        )
        self.assertEqual(event["event_id"], "evt-1")

        restarted_bridge = FakeBridge()
        restarted = ParticipantMonitor(self.workspace, "claude", restarted_bridge)
        self.assertEqual(restarted.poll(), [])
        self.assertEqual(len(restarted_bridge.requests), 0)
        self.assertEqual(restarted.cursor.last_sequence, 1)
        self.assertEqual(restarted.activation_results[0].status, "activated")

    def test_partial_jsonl_tail_is_rejected_fail_closed(self) -> None:
        """A process-truncated JSONL tail is detected, not silently discarded."""

        stream = EventStream(self.workspace)
        stream.append("before.crash", {"ok": True}, event_id="evt-1")
        with stream.path.open("ab") as handle:
            handle.write(b'{"sequence":2,"event_id":"evt-2"')
            handle.flush()

        with self.assertRaises(EventStreamError):
            stream.read()
        with self.assertRaises(EventStreamError):
            stream.append("after.crash", {"ok": True}, event_id="evt-3")
        # Current behavior is intentionally fail-closed; it does not claim to
        # recover an incomplete JSONL record automatically.
        self.assertFalse(stream.path.read_bytes().endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
