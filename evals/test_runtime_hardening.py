"""Fault-oriented acceptance tests for the v0.2.1 runtime hardening pass."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from adapters.common.wake_protocol import ActivationResult, WakeRequest  # noqa: E402
from events import EventStream, reconcile_instruction_events  # noqa: E402
from participant_monitor import ParticipantMonitor  # noqa: E402
from workflow_core import StateLock, atomic_write_json  # noqa: E402


class ProcessLoss(BaseException):
    pass


class RecordingBridge:
    def __init__(self) -> None:
        self.requests: list[tuple[WakeRequest, str | None]] = []

    def activate(self, request: WakeRequest, *, idempotency_key: str | None = None) -> ActivationResult:
        self.requests.append((request, idempotency_key))
        return ActivationResult("activated", evidence="test-dispatch")


class RuntimeHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="runtime-hardening-"))
        (self.root / ".multiagent" / "audit").mkdir(parents=True)
        (self.root / ".multiagent" / "instructions" / "claude").mkdir(parents=True)
        atomic_write_json(self.root / ".multiagent" / "state.json", {
            "revision": 9,
            "participant_bindings": {"claude": {"platform_id": "claude-code", "session_id": "s-1"}},
        })

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _instruction(self, instruction_id: str, kind: str = "respond") -> Path:
        path = self.root / ".multiagent" / "instructions" / "claude" / (instruction_id + ".json")
        payload = {
            "instruction_id": instruction_id, "agent_id": "claude", "kind": kind,
            "runtime_version": "1.0", "platform_id": "claude-code", "session_id": "s-1",
            "sha256": "a" * 64,
        }
        atomic_write_json(path, payload)
        return path

    def test_reconciliation_repairs_each_durable_instruction_signal_idempotently(self) -> None:
        # Covers proposal/round/final-ack/stop kinds: state+instruction survive
        # a crash, while the event signal is rebuilt exactly once on restart.
        for name, kind in (("I-propose", "propose"), ("I-round", "respond"), ("I-final", "final_ack"), ("I-stop", "stop")):
            self._instruction(name, kind)
        state = json.loads((self.root / ".multiagent" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(reconcile_instruction_events(self.root, state), ["I-final", "I-propose", "I-round", "I-stop"])
        self.assertEqual(reconcile_instruction_events(self.root, state), [])
        events = EventStream(self.root).read()
        self.assertEqual({item["event_id"] for item in events}, {"instruction-I-propose", "instruction-I-round", "instruction-I-final", "instruction-I-stop"})

    def test_crash_after_external_activation_never_retries_unknown_request(self) -> None:
        self._instruction("I-activate")
        EventStream(self.root).append("instruction_issued", {"agent_id": "claude", "instruction_id": "I-activate", "kind": "respond"}, event_id="instruction-I-activate")
        bridge = RecordingBridge()
        monitor = ParticipantMonitor(self.root, "claude", bridge, before_terminal_persist=lambda: (_ for _ in ()).throw(ProcessLoss()))
        with self.assertRaises(ProcessLoss):
            monitor.poll()
        self.assertEqual(len(bridge.requests), 1)
        restarted_bridge = RecordingBridge()
        restarted = ParticipantMonitor(self.root, "claude", restarted_bridge)
        self.assertEqual(restarted.poll(), [])
        self.assertEqual(restarted_bridge.requests, [])
        self.assertEqual(restarted.activation_results[0].status, "activation_unknown")

    def test_stop_receipt_is_required_before_monitor_stops(self) -> None:
        self._instruction("I-stop", "stop")
        EventStream(self.root).append("stop_requested", {"agent_ids": ["claude"], "instruction_ids": {"claude": "I-stop"}}, event_id="evt-stop")
        bridge = RecordingBridge()
        monitor = ParticipantMonitor(self.root, "claude", bridge)
        monitor.poll()
        self.assertEqual(monitor.cursor.status, "stopping")
        receipt = self.root / ".multiagent" / "receipts" / "claude" / "I-stop-completed.json"
        atomic_write_json(receipt, {"instruction_id": "I-stop", "instruction_sha256": "a" * 64, "agent_id": "claude", "kind": "stop", "status": "completed"})
        monitor.poll()
        self.assertEqual(monitor.cursor.status, "stopped")

    def test_pid_reuse_reclaims_a_live_but_different_process_owner(self) -> None:
        lock_dir = self.root / ".multiagent" / ".state.lock"
        lock_dir.mkdir(parents=True)
        atomic_write_json(lock_dir / "owner.json", {"pid": os.getpid(), "owner_token": "old", "process_start_time": "old-start"})
        with patch.object(StateLock, "_process_start_time", return_value="new-start"):
            with StateLock(self.root, timeout_seconds=0.2) as lock:
                self.assertTrue(lock.acquired)


if __name__ == "__main__":
    unittest.main()
