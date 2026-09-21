"""One real coordinator OS process for the fake E2E acceptance test."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from convergence import validate_convergence_assessment  # noqa: E402
from events import EventStream  # noqa: E402
from orchestrate_discussion import FakeWakeAdapter, orchestrate_once  # noqa: E402
import confirm_decision  # noqa: E402
import export_docx  # noqa: E402


def _state(workspace: Path) -> dict[str, object]:
    return json.loads((workspace / ".multiagent" / "state.json").read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%s" % os.getpid())
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _receipts(workspace: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted((workspace / ".multiagent" / "receipts").glob("*/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _round_for_output(output_path: object) -> int:
    text = str(output_path)
    marker = "/round-"
    return int(text.split(marker, 1)[1].split("/", 1)[0]) if marker in text else 0


def _responses_complete(workspace: Path, participants: list[str], round_number: int) -> bool:
    records = _receipts(workspace)
    return all(
        any(
            item.get("agent_id") == agent
            and item.get("kind") == "respond"
            and item.get("status") == "completed"
            and _round_for_output(item.get("output_path")) == round_number
            for item in records
        )
        for agent in participants
    )


def _write_assessment(workspace: Path, participants: list[str], round_number: int) -> None:
    path = workspace / ".multiagent" / "convergence" / ("round-%d.json" % round_number)
    if path.is_file():
        return
    converged = round_number >= 2
    payload = {
        "round": round_number,
        "new_substantive_issues": [] if converged else ["需要第二轮验证"],
        "unanswered_arguments": [] if converged else ["Round 1 的证据需要回应"],
        "new_evidence": ["三个真实 participant 进程已写入回执"],
        "remaining_disagreements": [] if converged else ["需要继续交叉回应"],
        "positions": {agent: ["共享文件事实源"] for agent in participants},
        "value_conflicts": [],
        "more_discussion": not converged,
        "requires_human_decision": converged,
        "converged": converged,
        "reason": "真实进程 round-%d 语义评估" % round_number,
    }
    validate_convergence_assessment(payload, participant_ids=participants)
    _atomic_json(path, payload)


def _confirm(workspace: Path, actor: str) -> int:
    argv = [
        "confirm_decision.py", "--state", str(workspace / ".multiagent" / "state.json"),
        "--actor", actor, "--platform-id", "codex", "--session-id", "session-%s" % actor,
        "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001", "--no-open",
    ]
    with patch.object(sys, "argv", argv), patch.object(export_docx, "open_document", return_value=True):
        return confirm_decision.main()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    deadline = time.monotonic() + args.timeout
    last_error = ""
    confirmed = False
    while time.monotonic() < deadline:
        try:
            state = _state(workspace)
            participants = list(state["expected_participants"])
            if state.get("stage") == "cross_response":
                current_round = int(state.get("round", 1) or 1)
                if _responses_complete(workspace, participants, current_round):
                    _write_assessment(workspace, participants, current_round)
            if state.get("stage") == "human_review" and not confirmed:
                code = _confirm(workspace, args.actor)
                if code != 0:
                    raise RuntimeError("confirm_decision returned %d" % code)
                confirmed = True
            result = orchestrate_once(
                workspace,
                FakeWakeAdapter(),
                actor=args.actor,
                platform_id="codex",
                session_id="session-%s" % args.actor,
                open_candidate=True,
                opener=lambda _path: True,
            )
            state = _state(workspace)
            if state.get("stage") == "delivered":
                _atomic_json(workspace / ".multiagent" / "fake-status" / "coordinator.json", {
                    "status": "delivered", "stage": "delivered", "last_result": result.stage,
                })
                return 0
            last_error = "stage=%s" % state.get("stage")
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(0.02)
        time.sleep(0.02)
    _atomic_json(workspace / ".multiagent" / "fake-status" / "coordinator.json", {
        "status": "timeout", "stage": _state(workspace).get("stage"), "error": last_error,
        "event_count": len(EventStream(workspace).read()),
    })
    return 124


if __name__ == "__main__":
    raise SystemExit(main())
