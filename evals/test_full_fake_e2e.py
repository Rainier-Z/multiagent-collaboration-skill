"""Real-process acceptance test for the complete v0.2 lifecycle.

This is intentionally not an in-memory fixture.  The coordinator, three
participants, and three monitors are separate OS processes sharing only a
temporary workspace.  The test therefore covers the real event hash chain,
monitor cursors, dispatch markers, participant receipts, round snapshots, and
process shutdown behavior.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FAKES = Path(__file__).resolve().parent / "fakes"


class FullFakeProcessE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="multiagent-full-fake-e2e-")
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        participants = ["coordinator", "participant-b", "participant-c"]
        command = [
            sys.executable, str(SCRIPTS / "init_discussion.py"), str(self.workspace),
            "coordinator", *participants,
            "--security-mode", "normal",
            "--coordinator-platform", "codex",
            "--coordinator-session", "session-coordinator",
            "--participant-binding", "coordinator=codex:session-coordinator",
            "--participant-binding", "participant-b=codex:session-participant-b",
            "--participant-binding", "participant-c=codex:session-participant-c",
            "--min-response-rounds", "2", "--max-response-rounds", "2",
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.participants = participants
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.temp_dir.cleanup()

    def _spawn(self, script: Path, *args: str) -> None:
        self.processes.append(
            subprocess.Popen(
                [sys.executable, str(script), "--workspace", str(self.workspace), *args],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        )

    def _status(self) -> dict[str, object]:
        path = self.workspace / ".multiagent" / "fake-status" / "coordinator.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _receipts(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for path in sorted((self.workspace / ".multiagent" / "receipts").glob("*/*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _process_diagnostics(self) -> str:
        chunks: list[str] = []
        for index, process in enumerate(self.processes):
            stdout, stderr = ("", "")
            if process.poll() is not None:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                except (subprocess.TimeoutExpired, ValueError):
                    pass
            chunks.append("process[%d] rc=%r stdout=%r stderr=%r" % (index, process.poll(), stdout, stderr))
        status_root = self.workspace / ".multiagent" / "fake-status"
        if status_root.is_dir():
            chunks.append("fake-status=" + repr({path.name: path.read_text(encoding="utf-8") for path in status_root.glob("*.json")}))
        return " | ".join(chunks)

    def test_complete_lifecycle_uses_real_processes_and_files(self) -> None:
        for agent in self.participants:
            self._spawn(FAKES / "fake_monitor.py", "--agent-id", agent, "--timeout", "18")
        for agent in self.participants:
            self._spawn(FAKES / "fake_participant.py", "--agent-id", agent, "--timeout", "18")
        self._spawn(FAKES / "fake_coordinator.py", "--actor", "coordinator", "--timeout", "18")

        coordinator = self.processes[-1]
        try:
            coordinator.wait(timeout=22)
        except subprocess.TimeoutExpired:
            self.fail("coordinator process did not terminate; pid=%r rc=%r status=%r; %s" % (
                coordinator.pid, coordinator.poll(), self._status(), self._process_diagnostics()))

        status = self._status()
        self.assertEqual(status.get("status"), "delivered", "真实进程 E2E 未完成：%r; %s" % (status, self._process_diagnostics()))
        for process in self.processes[:-1]:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("participant/monitor process did not exit after stop: pid=%r" % process.pid)
        self.assertTrue(all(process.returncode == 0 for process in self.processes[:-1]), self._process_diagnostics())
        state = json.loads((self.workspace / ".multiagent" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("stage"), "delivered")
        self.assertEqual(state.get("round"), 2)

        internal = self.workspace / ".multiagent"
        self.assertTrue((internal / "rounds" / "round-1.md").is_file())
        self.assertTrue((internal / "rounds" / "round-2.md").is_file())
        events = [json.loads(line) for line in (internal / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event.get("event_type") == "round_completed" for event in events))
        # Modern lifecycle publishes the decision before final acknowledgements
        # and stop; the legacy ``decision_confirmed`` event is not emitted.
        self.assertTrue(any(event.get("event_type") == "final_decision_published" for event in events))
        for agent in self.participants:
            cursor = internal / "monitors" / agent / "cursor.json"
            self.assertTrue(cursor.is_file(), "缺少 %s monitor cursor" % agent)
            round_two = internal / "views" / agent / "outputs" / "round-2" / "交叉回应文档.md"
            self.assertIn("round-1.md:", round_two.read_text(encoding="utf-8"))
        records = self._receipts()
        self.assertEqual(sum(item.get("kind") == "final_ack" and item.get("status") == "completed" for item in records), 3)
        self.assertEqual(sum(item.get("kind") == "stop" and item.get("status") == "completed" for item in records), 3)
        self.assertTrue((self.workspace / "最终决策.docx").is_file())


if __name__ == "__main__":
    unittest.main()
