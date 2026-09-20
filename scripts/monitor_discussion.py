#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot sensor for events belonging to one participant.

The monitor is intentionally not a workflow engine: it never evaluates
proposal/response readiness, issues instructions, calls an activation
adapter, or changes discussion state. It only compares files in the selected
agent's instruction, receipt, and output directories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping


MonitorLifecycle = Literal["active", "stopped"]
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT_ROOTS = (
    ".multiagent/instructions/{agent_id}",
    ".multiagent/receipts/{agent_id}",
    ".multiagent/views/{agent_id}/outputs",
)


@dataclass(frozen=True)
class MonitorEvent:
    """A file change observed for the selected agent."""

    path: str
    change: Literal["added", "modified", "removed"]
    fingerprint: tuple[int, int] | None

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "change": self.change, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class MonitorScan:
    """Read-only result from one scan; no workflow decision is included."""

    lifecycle: MonitorLifecycle
    agent_id: str
    events: tuple[MonitorEvent, ...]
    snapshot: dict[str, tuple[int, int]]

    @property
    def has_events(self) -> bool:
        return bool(self.events)

    def to_dict(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle,
            "agent_id": self.agent_id,
            "events": [event.to_dict() for event in self.events],
        }


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _validate_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError("agent_id must be a path-safe non-empty identifier")
    return agent_id


def load_state(workspace: str | os.PathLike[str]) -> dict[str, object] | None:
    """Read internal state for compatibility diagnostics; never infer readiness."""
    path = Path(workspace) / ".multiagent" / "state.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def snapshot_agent_events(
    workspace: str | os.PathLike[str], agent_id: str,
) -> dict[str, tuple[int, int]]:
    """Return fingerprints only for files owned by ``agent_id``.

    Global state, other agents' directories, discussion Markdown, and audit
    files are deliberately outside this sensor's input set.
    """
    agent_id = _validate_agent_id(agent_id)
    root = Path(workspace)
    current: dict[str, tuple[int, int]] = {}
    for template in _EVENT_ROOTS:
        directory = root / template.format(agent_id=agent_id)
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            current[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return current


def _normalise_snapshot(value: Mapping[str, object] | None) -> dict[str, tuple[int, int]]:
    if value is None:
        return {}
    normalised: dict[str, tuple[int, int]] = {}
    for path, fingerprint in value.items():
        if (
            isinstance(path, str)
            and isinstance(fingerprint, (list, tuple))
            and len(fingerprint) == 2
            and all(isinstance(item, int) for item in fingerprint)
        ):
            normalised[path] = (fingerprint[0], fingerprint[1])
    return normalised


def diff_events(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> tuple[MonitorEvent, ...]:
    """Compare two selected-agent snapshots without making a business decision."""
    old = _normalise_snapshot(previous)
    new = _normalise_snapshot(current)
    events: list[MonitorEvent] = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            events.append(MonitorEvent(path, "added", new[path]))
        elif path not in new:
            events.append(MonitorEvent(path, "removed", None))
        elif old[path] != new[path]:
            events.append(MonitorEvent(path, "modified", new[path]))
    return tuple(events)


def scan_agent_events(
    workspace: str | os.PathLike[str],
    agent_id: str,
    previous_snapshot: Mapping[str, object] | None = None,
) -> MonitorScan:
    """Perform one active scan; omitted baseline treats current files as new."""
    current = snapshot_agent_events(workspace, agent_id)
    return MonitorScan("active", agent_id, diff_events(previous_snapshot, current), current)


class DiscussionMonitor:
    """Lifecycle wrapper for one agent's read-only event sensor.

    Construction establishes a baseline, so pre-existing files are not
    replayed. ``stop`` suppresses scans without deleting the baseline;
    ``resume`` therefore reports files written while monitoring was stopped.
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        agent_id: str,
        baseline: Mapping[str, object] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.agent_id = _validate_agent_id(agent_id)
        self._baseline = (
            _normalise_snapshot(baseline)
            if baseline is not None
            else snapshot_agent_events(self.workspace, self.agent_id)
        )
        self._lifecycle: MonitorLifecycle = "active"

    @property
    def lifecycle(self) -> MonitorLifecycle:
        return self._lifecycle

    def stop(self) -> MonitorLifecycle:
        self._lifecycle = "stopped"
        return self._lifecycle

    def resume(self) -> MonitorLifecycle:
        self._lifecycle = "active"
        return self._lifecycle

    def scan(self) -> MonitorScan:
        current = snapshot_agent_events(self.workspace, self.agent_id)
        if self._lifecycle == "stopped":
            return MonitorScan("stopped", self.agent_id, (), current)
        events = diff_events(self._baseline, current)
        self._baseline = current
        return MonitorScan("active", self.agent_id, events, current)


def _print_scan(scan: MonitorScan) -> None:
    print(
        "[monitor] agent=%s lifecycle=%s events=%d" %
        (scan.agent_id, scan.lifecycle, len(scan.events)),
        flush=True,
    )
    for event in scan.events:
        print("  %s %s" % (event.change, event.path), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读检测一个 agent 的新指令/回执/产物；不推进业务、不唤醒会话。"
    )
    parser.add_argument("--workspace", required=True, help="讨论工作区目录")
    parser.add_argument("--agent-id", required=True, help="要观察的当前 agent 身份")
    parser.add_argument("--baseline", default=None, help="可选：先前 agent 事件快照 JSON")
    args = parser.parse_args(argv)
    try:
        previous = None
        if args.baseline:
            previous = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise ValueError("baseline must be a JSON object")
        scan = scan_agent_events(args.workspace, args.agent_id, previous)
    except (OSError, ValueError, TypeError) as exc:
        print("[diagnostic] scan failed: %s" % exc, file=sys.stderr)
        return 2
    _print_scan(scan)
    print("[diagnostic] 监测只报告当前 agent 的文件事件；不代表激活成功或流程已推进。")
    return 0


if __name__ == "__main__":
    _utf8()
    sys.exit(main())
