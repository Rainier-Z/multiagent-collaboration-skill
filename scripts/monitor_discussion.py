#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读一次讨论状态与产物的诊断工具。

本工具不运行循环、不等待、不派发指令、不唤醒 Agent，也不推进阶段。
真正的流程推进由协调者在新产物或回执事件后调用的门禁编排器负责。

用法：python monitor_discussion.py --workspace <讨论目录>
"""
import argparse
import json
import os
import sys


def _utf8():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_state(workspace):
    """读取 state.json；缺失或非法返回 None。"""
    path = os.path.join(workspace, ".multiagent", "state.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _file_ok(workspace, subdir, agent_id, suffix):
    """校验贡献文件存在且非空（双满足之一：Markdown 证据）。"""
    path = os.path.join(workspace, ".multiagent", "views", agent_id, "outputs", suffix)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def readiness(state, workspace):
    """返回 (proposals_ready, responses_ready, detail)。

    标准（双满足）：
      - 提案齐 = 名单中每个参与者 submission_status == submitted 且
        .multiagent/views/<id>/outputs/提案文档.md 文件存在且非空；
      - 回应齐 = 名单中每个参与者 response_status == submitted 且
        .multiagent/views/<id>/outputs/交叉回应文档.md 文件存在且非空。
    """
    if state is None:
        return False, False, {"error": "state.json 缺失或非法"}
    parts = state.get("expected_participants", []) or []
    ss = state.get("submission_status", {}) or {}
    rs = state.get("response_status", {}) or {}
    proposals_ready = (
        bool(parts)
        and all(ss.get(p) == "submitted" for p in parts)
        and all(_file_ok(workspace, "outputs", p, "提案文档.md") for p in parts)
    )
    responses_ready = (
        bool(parts)
        and all(rs.get(p) == "submitted" for p in parts)
        and all(_file_ok(workspace, "outputs", p, "交叉回应文档.md") for p in parts)
    )
    detail = {
        "phase": state.get("stage") or state.get("phase"),
        "revision": state.get("revision"),
        "participants": parts,
        "submission_status": ss,
        "response_status": rs,
        "response_rounds": state.get("response_rounds", {}) or {},
    }
    return proposals_ready, responses_ready, detail


def report(detail, proposals_ready, responses_ready):
    """打印一行状态（信息模式：含回应轮次）。"""
    phase = detail.get("phase", "?")
    ss = detail.get("submission_status", {})
    rounds = detail.get("response_rounds", {})
    parts = detail.get("participants", [])
    print("[monitor] 阶段=%s | 提案: %s | 回应轮次: %s | 提案齐=%s | 已回应=%s" % (
        phase,
        "/".join("%s=%s" % (p, "Y" if ss.get(p) == "submitted" else "N") for p in parts),
        "/".join("%s=r%d" % (p, rounds.get(p, 0)) for p in parts),
        "YES" if proposals_ready else "NO",
        "ALL" if responses_ready else "PARTIAL",
    ), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="只读检查一次当前门禁材料；不等待、不唤醒、不推进流程。"
    )
    parser.add_argument("--workspace", required=True, help="讨论工作区目录")
    args = parser.parse_args(argv)
    proposals_ready, responses_ready, detail = readiness(load_state(args.workspace), args.workspace)
    if detail.get("error"):
        print("[diagnostic] %s" % detail["error"], file=sys.stderr)
        return 1
    print("[diagnostic] stage=%s revision=%s proposals_ready=%s responses_ready=%s" % (
        detail.get("phase", "unknown"), detail.get("revision", "unknown"),
        str(proposals_ready).lower(), str(responses_ready).lower(),
    ))
    print("[diagnostic] 此结果只描述现有文件；流程门禁负责推进，扫描不能唤醒 Agent。")
    return 0 if proposals_ready or responses_ready else 1


if __name__ == "__main__":
    _utf8()
    sys.exit(main())
