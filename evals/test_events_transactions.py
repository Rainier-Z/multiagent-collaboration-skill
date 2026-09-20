#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contract tests for the append-only event and state transaction layer."""

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

from events import EventStream, EventStreamError  # type: ignore[import-not-found]
from transactions import (  # type: ignore[import-not-found]
    TransactionRecoveryError,
    commit_transaction,
    recover_transactions,
)


class Crash(BaseException):
    pass


class EventTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="event-transaction-test-"))
        state_path = self.workspace / ".multiagent" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"revision": 1, "stage": "initialized"}), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_append_only_stream_is_hash_chained(self) -> None:
        stream = EventStream(self.workspace)
        first = stream.append("one", {"value": 1}, revision_before=1, revision_after=2)
        second = stream.append("two", {"value": 2}, revision_before=2, revision_after=3)
        records = stream.read()
        self.assertEqual([item["sequence"] for item in records], [1, 2])
        self.assertEqual(second["previous_hash"], first["event_hash"])
        self.assertEqual(stream.verify()["count"], 2)
        path = stream.path
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace('"value":1', '"value":9')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(EventStreamError):
            stream.read()

    def test_commit_order_writes_artifact_then_event_then_revision(self) -> None:
        phases: list[str] = []

        def hook(phase: str, _journal: dict[str, object]) -> None:
            phases.append(phase)

        result = commit_transaction(
            self.workspace,
            event_type="proposal.written",
            event_payload={"agent_id": "claude-a"},
            artifacts={".multiagent/views/claude-a/outputs/proposal.md": "proposal"},
            receipts={".multiagent/receipts/claude-a/receipt.json": {"status": "completed"}},
            state_update={"stage": "independent_proposal"},
            expected_revision=1,
            fault_hook=hook,
        )
        self.assertEqual(result["status"], "committed")
        self.assertEqual(phases, ["preconditions_validated", "staging", "artifacts_written", "artifacts_written", "event_appended", "state_updated"])
        state = json.loads((self.workspace / ".multiagent/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["revision"], 2)
        self.assertEqual(len(EventStream(self.workspace).read()), 1)

    def test_crash_after_event_is_completed_from_journal(self) -> None:
        def hook(phase: str, _journal: dict[str, object]) -> None:
            if phase == "event_appended":
                raise Crash("simulated process loss")

        with self.assertRaises(Crash):
            commit_transaction(self.workspace, event_type="receipt.completed", artifacts={"receipt.json": "ok"}, fault_hook=hook)
        state_before = json.loads((self.workspace / ".multiagent/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state_before["revision"], 1)
        self.assertEqual(recover_transactions(self.workspace)[0]["action"], "completed_state_update")
        state_after = json.loads((self.workspace / ".multiagent/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state_after["revision"], 2)
        self.assertTrue((self.workspace / "receipt.json").is_file())

    def test_crash_before_event_rolls_back_and_does_not_emit_event(self) -> None:
        def hook(phase: str, _journal: dict[str, object]) -> None:
            if phase == "artifacts_written":
                raise Crash("simulated process loss")

        with self.assertRaises(Crash):
            commit_transaction(self.workspace, event_type="never.emitted", artifacts={"output.txt": "x"}, fault_hook=hook)
        self.assertTrue((self.workspace / "output.txt").is_file())
        self.assertEqual(recover_transactions(self.workspace)[0]["action"], "rolled_back")
        self.assertFalse((self.workspace / "output.txt").exists())
        self.assertEqual(EventStream(self.workspace).read(), [])

    def test_state_ahead_without_event_is_fail_closed(self) -> None:
        def hook(phase: str, _journal: dict[str, object]) -> None:
            if phase == "event_appended":
                raise Crash("simulated process loss")

        with self.assertRaises(Crash):
            commit_transaction(self.workspace, event_type="state.ahead", artifacts={"output.txt": "x"}, fault_hook=hook)
        state_path = self.workspace / ".multiagent/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["revision"] = 2
        state_path.write_text(json.dumps(state), encoding="utf-8")
        # Remove the event to model a state update that was published alone.
        EventStream(self.workspace).path.unlink()
        with self.assertRaises(TransactionRecoveryError):
            recover_transactions(self.workspace)


if __name__ == "__main__":
    unittest.main()
