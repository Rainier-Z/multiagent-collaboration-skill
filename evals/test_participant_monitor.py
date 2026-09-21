"""Persistent event-consumer tests for the participant monitor."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from adapters.common.wake_protocol import ActivationResult, WakeRequest  # noqa: E402
from events import EventStream  # noqa: E402
from participant_monitor import MonitorError, ParticipantMonitor  # noqa: E402


class FakeBridge:
    def __init__(self, result: ActivationResult | None = None) -> None:
        self.result = result or ActivationResult("activated", evidence="fake-dispatch")
        self.requests: list[WakeRequest] = []

    def activate(self, request: WakeRequest) -> ActivationResult:
        self.requests.append(request)
        return self.result


class ParticipantMonitorTests(unittest.TestCase):
    def _workspace(self) -> Path:
        raw = tempfile.TemporaryDirectory(prefix="participant-monitor-")
        self.addCleanup(raw.cleanup)
        root = Path(raw.name)
        (root / ".multiagent" / "audit").mkdir(parents=True)
        (root / ".multiagent" / "instructions" / "claude").mkdir(parents=True)
        (root / ".multiagent" / "state.json").write_text(
            json.dumps({
                "participant_bindings": {
                    "claude": {"platform_id": "claude-code", "session_id": "session-1"}
                }
            }), encoding="utf-8"
        )
        return root

    def test_cursor_restarts_and_duplicate_event_does_not_reactivate(self) -> None:
        workspace = self._workspace()
        instruction = workspace / ".multiagent" / "instructions" / "claude" / "I-1.json"
        instruction.write_text(json.dumps({
            "instruction_id": "I-1",
            "runtime_version": "1.2.0",
            "platform_id": "claude-code",
            "session_id": "session-1",
        }), encoding="utf-8")
        event = EventStream(workspace).append(
            "instruction_issued",
            {"agent_id": "claude", "instruction_id": "I-1", "kind": "propose"},
            event_id="evt-1",
        )
        bridge = FakeBridge()
        monitor = ParticipantMonitor(workspace, "claude", bridge)
        first = monitor.poll()
        self.assertEqual([item.status for item in first], ["activated"])
        self.assertEqual(len(bridge.requests), 1)
        self.assertEqual(monitor.cursor.last_sequence, event["sequence"])

        restarted_bridge = FakeBridge()
        restarted = ParticipantMonitor(workspace, "claude", restarted_bridge)
        self.assertEqual(restarted.poll(), [])
        self.assertEqual(restarted_bridge.requests, [])
        saved = json.loads((workspace / ".multiagent" / "monitors" / "claude" / "cursor.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["last_event_id"], "evt-1")

    def test_broadcast_final_and_targeted_stop_are_consumed(self) -> None:
        workspace = self._workspace()
        stream = EventStream(workspace)
        stream.append("final_decision_published", {"decision_id": "D-1"}, event_id="evt-final")
        stream.append("stop_requested", {"agent_ids": ["claude"]}, event_id="evt-stop")
        bridge = FakeBridge()
        monitor = ParticipantMonitor(workspace, "claude", bridge)
        results = monitor.poll()
        self.assertEqual([result.event_type for result in results], [
            "final_decision_published", "stop_requested"
        ])
        self.assertEqual(len(bridge.requests), 2)
        self.assertEqual([request.instruction_id for request in bridge.requests], [
            "event-evt-final", "event-evt-stop"
        ])

    def test_unconfigured_bridge_reports_manual_activation_required(self) -> None:
        workspace = self._workspace()
        EventStream(workspace).append(
            "instruction_issued",
            {"agent_id": "claude", "instruction_id": "I-manual"},
            event_id="evt-manual",
        )
        from adapters.claude.wake_adapter import ClaudeCodeWakeAdapter
        results = ParticipantMonitor(workspace, "claude", ClaudeCodeWakeAdapter()).poll()
        self.assertEqual(results[0].status, "manual_activation_required")

    def test_cursor_mismatch_fails_closed(self) -> None:
        workspace = self._workspace()
        EventStream(workspace).append("instruction_issued", {"agent_id": "claude"}, event_id="evt-1")
        monitor_dir = workspace / ".multiagent" / "monitors" / "claude"
        monitor_dir.mkdir(parents=True)
        (monitor_dir / "cursor.json").write_text(json.dumps({
            "last_sequence": 1,
            "last_event_id": "wrong",
            "last_event_hash": "wrong",
        }), encoding="utf-8")
        with self.assertRaises(MonitorError):
            ParticipantMonitor(workspace, "claude", FakeBridge()).poll()


if __name__ == "__main__":
    unittest.main()
