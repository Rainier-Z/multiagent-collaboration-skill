"""A real OS-process participant used by the fake E2E acceptance test."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from participant_runtime.participant_runner import run_instruction  # noqa: E402
from participant_runtime.protocol import Instruction  # noqa: E402


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%s" % os.getpid())
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _find_instruction(workspace: Path, agent_id: str, instruction_id: str) -> Instruction | None:
    root = workspace / ".multiagent" / "instructions" / agent_id
    for path in root.glob("*.json") if root.is_dir() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            instruction = Instruction.from_dict(value)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if instruction.instruction_id == instruction_id:
            return instruction
    return None


def _terminal(workspace: Path, agent_id: str, instruction_id: str) -> bool:
    root = workspace / ".multiagent" / "receipts" / agent_id
    return any((root / (instruction_id + "-" + status + ".json")).is_file() for status in ("accepted", "completed", "failed"))


def _write_business_output(workspace: Path, instruction: Instruction) -> None:
    output = workspace / instruction.output_path
    output.parent.mkdir(parents=True, exist_ok=True)
    if instruction.kind == "propose":
        text = (
            "# %s 独立提案\n\n"
            "### 一、方案\n- 共享工作区驱动的可验证协作。\n\n"
            "### 二、依据\n- 仅使用本指令列出的项目上下文。\n"
        ) % instruction.agent_id
    else:
        observed: list[str] = []
        for relative in instruction.input_paths:
            path = workspace / relative
            if path.is_file():
                content = path.read_bytes()
                observed.append("%s:%s" % (Path(relative).name, hashlib.sha256(content).hexdigest()))
        round_one = [item for item in observed if "round-1.md" in item]
        text = (
            "# %s 第%s轮回应\n\n"
            "### 共识点\n- 共享工作区和事件日志可验证。\n\n"
            "### 分歧点\n- 本轮没有未解决的结构性分歧。\n\n"
            "### 新问题\n- 无\n\n"
            "### 输入证据\n- %s\n"
        ) % (instruction.agent_id, _round_from_path(instruction.output_path), "; ".join(observed) or "无")
        if instruction.output_path.find("round-2") >= 0 and not round_one:
            raise RuntimeError("round-2 instruction did not expose round-1 snapshot")
    output.write_text(text, encoding="utf-8")


def _round_from_path(path: str) -> str:
    marker = "/round-"
    if marker in path:
        return path.split(marker, 1)[1].split("/", 1)[0]
    return "1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    dispatch_root = workspace / ".multiagent" / "fake-dispatch" / args.agent_id
    deadline = time.monotonic() + args.timeout
    processed: set[str] = set()
    error = ""
    while time.monotonic() < deadline:
        for marker in sorted(dispatch_root.glob("*.json")) if dispatch_root.is_dir() else ():
            try:
                request = json.loads(marker.read_text(encoding="utf-8"))
                instruction_id = str(request["instruction_id"])
            except (OSError, json.JSONDecodeError, KeyError):
                continue
            if instruction_id in processed or _terminal(workspace, args.agent_id, instruction_id):
                processed.add(instruction_id)
                continue
            instruction = _find_instruction(workspace, args.agent_id, instruction_id)
            if instruction is None:
                # Broadcast event activation has no instruction file; it is
                # intentionally not a participant business instruction.
                processed.add(instruction_id)
                continue
            try:
                if instruction.kind in {"propose", "respond"}:
                    _write_business_output(workspace, instruction)
                code = run_instruction(workspace, args.agent_id, instruction_id)
                if code != 0:
                    raise RuntimeError("runtime returned %s for %s" % (code, instruction_id))
                processed.add(instruction_id)
                if instruction.kind == "stop":
                    return 0
            except Exception as exc:
                error = str(exc)
                _atomic_json(
                    workspace / ".multiagent" / "fake-status" / ("participant-%s.json" % args.agent_id),
                    {"status": "failed", "error": error, "instruction_id": instruction_id},
                )
                return 2
        time.sleep(0.02)
    _atomic_json(
        workspace / ".multiagent" / "fake-status" / ("participant-%s.json" % args.agent_id),
        {"status": "timeout", "error": error},
    )
    return 124


if __name__ == "__main__":
    raise SystemExit(main())
