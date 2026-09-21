"""A real OS-process monitor for the fake multi-agent E2E test.

The bridge writes a dispatch marker after ParticipantMonitor consumes an
instruction_issued event.  Fake participants wait for those markers, which
keeps the E2E causal chain as Event Stream -> Monitor -> Activation Bridge ->
participant instead of letting participants poll instructions directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from adapters.common.wake_protocol import ActivationResult, WakeRequest  # noqa: E402
from participant_monitor import MonitorError, ParticipantMonitor  # noqa: E402


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%s" % os.getpid())
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class DispatchBridge:
    def __init__(self, workspace: Path, agent_id: str) -> None:
        self.workspace = workspace
        self.agent_id = agent_id

    def activate(self, request: WakeRequest) -> ActivationResult:
        marker = (
            self.workspace / ".multiagent" / "fake-dispatch" / self.agent_id
            / (request.instruction_id + ".json")
        )
        _atomic_json(marker, request.to_dict())
        return ActivationResult("activated", evidence="fake-process-dispatch-marker")


def _instruction_kind(workspace: Path, agent_id: str, instruction_id: str) -> str | None:
    root = workspace / ".multiagent" / "instructions" / agent_id
    for path in root.glob("*.json") if root.is_dir() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("instruction_id") == instruction_id:
            return value.get("kind") if isinstance(value.get("kind"), str) else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    monitor = ParticipantMonitor(workspace, args.agent_id, DispatchBridge(workspace, args.agent_id))
    deadline = time.monotonic() + args.timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            results = monitor.poll()
            for result in results:
                if result.status == "activated" and _instruction_kind(workspace, args.agent_id, result.instruction_id or "") == "stop":
                    return 0
            last_error = ""
        except (MonitorError, OSError, ValueError) as error:
            # A concurrent append can be observed between write and newline
            # visibility. Retry briefly; persistent corruption still fails.
            last_error = str(error)
        time.sleep(0.02)
    _atomic_json(
        workspace / ".multiagent" / "fake-status" / ("monitor-%s.json" % args.agent_id),
        {"status": "timeout", "error": last_error},
    )
    return 124


if __name__ == "__main__":
    raise SystemExit(main())
