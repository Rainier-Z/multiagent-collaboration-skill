#!/usr/bin/env python3
"""可审计的本地 Participant Runtime 演习，不调用也不伪造外部平台唤醒。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from orchestrate_discussion import FakeWakeAdapter, orchestrate_once
from participant_runtime.participant_runner import run_instruction


def instruction_id(workspace: Path, agent: str, kind: str) -> str:
    matches = sorted((workspace / "instructions" / agent).glob("*-%s-*.json" % kind))
    if len(matches) != 1:
        raise RuntimeError("%s 的 %s 指令数量异常: %d" % (agent, kind, len(matches)))
    return json.loads(matches[0].read_text(encoding="utf-8"))["instruction_id"]


def run(workspace: Path) -> dict[str, object]:
    participants = ("claude-demo", "codex-demo")
    init = subprocess.run(
        [sys.executable, str(SCRIPTS / "init_discussion.py"), str(workspace), participants[0], *participants,
         "--disposition", "delete"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if init.returncode:
        raise RuntimeError(init.stdout + init.stderr)

    wake = FakeWakeAdapter()
    for agent in participants:
        if run_instruction(workspace, agent, instruction_id(workspace, agent, "bootstrap")) != 0:
            raise RuntimeError("bootstrap failed: %s" % agent)
    orchestrate_once(workspace, wake)

    for agent in participants:
        proposal = workspace / "proposals" / (agent + "-提案文档.md")
        proposal.write_text("# %s 的独立提案\n\n演习内容。\n" % agent, encoding="utf-8")
        if run_instruction(workspace, agent, instruction_id(workspace, agent, "propose")) != 0:
            raise RuntimeError("proposal failed: %s" % agent)
    orchestrate_once(workspace, wake)

    for agent in participants:
        response = workspace / "responses" / (agent + "-交叉回应文档.md")
        response.write_text("# %s 的交叉回应\n\n基于已合并内容。\n" % agent, encoding="utf-8")
        if run_instruction(workspace, agent, instruction_id(workspace, agent, "respond")) != 0:
            raise RuntimeError("response failed: %s" % agent)
    result = orchestrate_once(workspace, wake)
    state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
    return {
        "workspace": str(workspace),
        "stage": result.stage,
        "instructions_issued": [request.instruction_id for request in wake.requests],
        "platform_wake_claimed": False,
        "candidate_requires_rainier_confirmation": state.get("stage") == "candidate_decision",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, help="必须是空白或不存在的演习目录")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    if workspace.exists() and any(workspace.iterdir()):
        parser.error("--workspace 必须为空，避免覆盖现有项目")
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        print(json.dumps(run(workspace), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "message": str(exc), "platform_wake_claimed": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
