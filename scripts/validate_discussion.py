#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_discussion.py —— 校验讨论一致性（只读，不修改任何文件）。

校验项（逐项输出 PASS / FAIL）：
    1. state.json 存在且为合法 JSON
    2. state.json 最小字段完整、类型与枚举合法（references/state-schema.md 第二、三、五节）
    3. 各参与者提交完整性：submission_status（提案）与 response_status（回应）
       键集均与名单一致、值 ∈ {pending, submitted}
    4. 时间字段为 Asia/Shanghai(+08:00) 的 ISO 8601
    5. Markdown 权威内容与 state.json 一致性（阶段推进双满足，§四.1）：
       - project-context.md 必须存在
       - 阶段特定证据：提案/回应文件、主讨论文档、决策 ID、Word 交付、监测停止状态
    6. --expect-phase 指定时校验当前阶段

用法：
    python validate_discussion.py [--expect-phase <阶段>] <讨论目录>

退出码：
    0  全部 PASS
    1  存在 FAIL
    2  用法/参数错误
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from workflow_core import WorkflowError, ensure_content_consistency, load_state

PROTOCOL_VERSION = "1.0"
SHANGHAI = "Asia/Shanghai"

# 封闭枚举（references/state-schema.md 第二、三、五节）
PHASES = [
    "initialized", "independent_proposal", "proposals_complete",
    "cross_response", "candidate_decision", "human_review", "finalizing", "user_confirmation",
    "confirmed_decision", "delivered", "monitoring_stopped",
]
SUBMISSION_VALUES = ("pending", "submitted")
DISPOSITIONS = ("archive", "delete")
MONITOR_MODES = ("reply_before", "periodic", "none")
MONITOR_STATUSES = ("active", "stopped", "expired")
REQUIRED_FIELDS = [
    "protocol_version", "discussion_id", "stage", "expected_participants",
    "submission_status", "response_status", "coordinator", "coordination_lease_until",
    "coordinator_timeout", "participant_timeout", "proposal_disposition",
    "monitoring", "candidate_decision_ids", "confirmed_decision_ids",
    "last_checked_at", "revision",
]

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# 讨论文档命名约定（模板：<当前讨论子项目名称>讨论文档_<日期>.md）
DISCUSSION_DOC_GLOB = "*讨论文档_*.md"


def shanghai_tz():
    """返回 Asia/Shanghai 时区；Windows 缺 IANA 时区库时降级为固定 +08:00（无夏令时，等价）。"""
    try:
        return ZoneInfo(SHANGHAI)
    except Exception:
        return timezone(timedelta(hours=8))


def _is_iso_plus8(value):
    """校验字符串为带 +08:00 时区的 ISO 8601 时间。"""
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return False
    if dt.tzinfo is None:
        return False
    return dt.utcoffset() == timedelta(hours=8)


def collect_state_errors(state):
    """校验最小字段、类型与枚举；返回错误列表，为空即合法。"""
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    check(isinstance(state, dict), "state.json 根节点必须是对象")
    if not isinstance(state, dict):
        return errors

    missing = [f for f in REQUIRED_FIELDS if f not in state]
    check(not missing, "缺少必填字段: " + ", ".join(missing))
    if missing:
        return errors

    check(state["protocol_version"] == PROTOCOL_VERSION,
          "protocol_version 必须为 " + PROTOCOL_VERSION)
    check(isinstance(state["discussion_id"], str) and state["discussion_id"],
          "discussion_id 必须是非空字符串")
    check(state["stage"] in PHASES, "stage 非法: " + str(state["stage"]))

    participants = state["expected_participants"]
    check(isinstance(participants, list) and participants,
          "expected_participants 必须是非空列表")
    if isinstance(participants, list):
        check(all(isinstance(p, str) and p for p in participants),
              "参与者身份必须是非空字符串")
        check(all(p == p.lower() for p in participants),
              "参与者身份必须为小写")
        check(len(set(participants)) == len(participants),
              "参与者身份存在重复")

    for field, label in (("submission_status", "提案提交状态"),
                         ("response_status", "回应提交状态")):
        field_status = state[field]
        check(isinstance(field_status, dict), field + " 必须是对象")
        if isinstance(participants, list) and isinstance(field_status, dict):
            check(set(field_status.keys()) == set(participants),
                  field + " 键集必须与 expected_participants 名单完全一致")
            check(all(v in SUBMISSION_VALUES for v in field_status.values()),
                  field + " 值必须 ∈ {pending, submitted}")

    check(state["coordinator"] in state["expected_participants"],
          "coordinator 必须 ∈ expected_participants")
    check(isinstance(state["coordinator_timeout"], int) and state["coordinator_timeout"] > 0,
          "coordinator_timeout 必须为正整数")
    check(isinstance(state["participant_timeout"], int) and state["participant_timeout"] > 0,
          "participant_timeout 必须为正整数")
    check(state["proposal_disposition"] in DISPOSITIONS,
          "proposal_disposition 必须 ∈ {archive, delete}")

    mon = state["monitoring"]
    check(isinstance(mon, dict) and set(mon.keys()) == {"enabled", "mode", "interval_seconds", "status"},
          "monitoring 必须含 enabled/mode/interval_seconds/status 四个字段")
    if isinstance(mon, dict):
        check(isinstance(mon["enabled"], bool), "monitoring.enabled 必须是布尔值")
        check(mon["mode"] in MONITOR_MODES, "monitoring.mode 非法")
        check(isinstance(mon["interval_seconds"], int) and mon["interval_seconds"] > 0,
              "monitoring.interval_seconds 必须为正整数")
        check(mon["status"] in MONITOR_STATUSES, "monitoring.status 非法")

    for key in ("candidate_decision_ids", "confirmed_decision_ids"):
        check(isinstance(state[key], list) and all(isinstance(x, str) for x in state[key]),
              key + " 必须是字符串列表")
    check(isinstance(state["revision"], int) and state["revision"] >= 1,
          "revision 必须为 ≥1 的整数")
    for key in ("coordination_lease_until", "last_checked_at"):
        check(_is_iso_plus8(state[key]),
              key + " 必须是 Asia/Shanghai(+08:00) 的 ISO 8601 时间")
    return errors


def collect_evidence_checks(state, discussion_dir):
    """校验 Markdown 权威内容与 state.json 一致性（阶段推进双满足，§四.1）。"""
    checks = []
    stage = state["stage"]
    participants = state["expected_participants"]
    # .get 防御字段缺失：缺失已在 collect_state_errors 中上报为 FAIL，此处不再崩溃
    status = state.get("submission_status", {})
    response_status = state.get("response_status", {})

    def exist(path):
        return os.path.isfile(path)

    def add(ok, msg):
        checks.append((ok, msg))

    # 公共证据：project-context.md 必须存在
    add(exist(os.path.join(discussion_dir, "project-context.md")),
        "project-context.md 存在（内容权威输入）")

    proposal_path = lambda pid: os.path.join(
        discussion_dir, ".multiagent", "views", pid, "outputs", "提案文档.md")
    def response_path(pid):
        output_root = os.path.join(discussion_dir, ".multiagent", "views", pid, "outputs")
        if isinstance(state.get("coordinator_participant"), dict) and int(state.get("round", 0) or 0) > 0:
            return os.path.join(output_root, "round-%d" % int(state["round"]), "交叉回应文档.md")
        return os.path.join(output_root, "交叉回应文档.md")
    discussion_doc_exists = bool(glob.glob(
        os.path.join(discussion_dir, DISCUSSION_DOC_GLOB)))

    # 阶段特定证据（按 state-schema.md 第三节状态流转语义）
    if stage == "independent_proposal":
        for pid in participants:
            if status.get(pid) == "submitted":
                add(exist(proposal_path(pid)),
                    "独立提案文件存在: " + os.path.basename(proposal_path(pid)))
    elif stage == "proposals_complete":
        for pid in participants:
            add(status.get(pid) == "submitted",
                "提案齐备：身份逐一匹配提交状态 - " + pid)
            add(exist(proposal_path(pid)),
                "提案文件存在: " + os.path.basename(proposal_path(pid)))
    elif stage == "cross_response":
        add(discussion_doc_exists, "主讨论文档存在（合并产物，推进双满足）")
        for pid in participants:
            add(response_status.get(pid) in ("pending", "submitted"),
                "回应提交完整性 - " + pid + " = " + str(response_status.get(pid)))
            if response_status.get(pid) == "submitted":
                add(exist(response_path(pid)),
                    "交叉回应文件存在: " + os.path.basename(response_path(pid)))
    elif stage in {"candidate_decision", "human_review", "finalizing"}:
        add(discussion_doc_exists, "主讨论文档存在（合并产物）")
        add(bool(state["candidate_decision_ids"]),
            "候选决策 ID 已登记（candidate_decision_ids 非空）")
    elif stage == "user_confirmation":
        add(discussion_doc_exists, "主讨论文档存在")
        add(bool(state["candidate_decision_ids"]),
            "候选决策 ID 已登记（candidate_decision_ids 非空）")
    elif stage == "confirmed_decision":
        add(discussion_doc_exists, "主讨论文档存在")
        add(bool(state["candidate_decision_ids"]),
            "候选决策 ID 已登记")
        add(bool(state["confirmed_decision_ids"]),
            "已确认决策 ID 已登记（confirmed_decision_ids 非空）")
    elif stage == "delivered":
        add(discussion_doc_exists, "主讨论文档存在")
        add(bool(state["confirmed_decision_ids"]),
            "已确认决策 ID 已登记")
        delivered = glob.glob(os.path.join(discussion_dir, ".multiagent", "deliverables", "*"))
        add(any(os.path.isfile(p) for p in delivered),
            "Word 交付快照存在于 .multiagent/deliverables/")
    elif stage == "monitoring_stopped":
        mon = state["monitoring"]
        add(mon["enabled"] is False and mon["status"] in ("stopped", "expired"),
            "监测已停止（monitoring.enabled=False 且 status ∈ {stopped, expired}）")
    return checks


def main(argv=None):
    """Read-only validation of the compact workspace contract."""
    parser = argparse.ArgumentParser(description="只读验证密封工作区及其 state.json。")
    parser.add_argument("discussion_dir", help="讨论工作区目录")
    parser.add_argument("--expect-phase", choices=PHASES, default=None)
    args = parser.parse_args(argv)
    workspace = os.path.abspath(args.discussion_dir)
    checks = []
    try:
        state = load_state(workspace)
        ensure_content_consistency(workspace, state)
    except (WorkflowError, OSError, ValueError) as exc:
        print("FAIL | .multiagent/state.json 或内容权威校验失败: %s" % exc)
        return EXIT_FAIL

    checks.append((True, "读取并验证 .multiagent/state.json"))
    participants = state["expected_participants"]
    checks.append((
        set(state["submission_status"]) == set(participants)
        and set(state["response_status"]) == set(participants),
        "参与者提案/回应状态键与名单一致",
    ))
    for agent_id in participants:
        outputs = os.path.join(workspace, ".multiagent", "views", agent_id, "outputs")
        receipts = os.path.join(workspace, ".multiagent", "receipts", agent_id)
        checks.append((os.path.isdir(outputs), "密封 outputs 目录存在: " + agent_id))
        checks.append((os.path.isdir(receipts), "密封 receipts 目录存在: " + agent_id))
        if state["submission_status"].get(agent_id) == "submitted":
            checks.append((os.path.isfile(os.path.join(outputs, "提案文档.md")), "提案产物存在: " + agent_id))
        if state["response_status"].get(agent_id) == "submitted":
            checks.append((os.path.isfile(os.path.join(outputs, "交叉回应文档.md")), "回应产物存在: " + agent_id))
    if args.expect_phase:
        checks.append((state["stage"] == args.expect_phase, "当前阶段 == 期望阶段(" + args.expect_phase + ")"))

    for ok, message in checks:
        print(("PASS" if ok else "FAIL") + " | " + message)
    failed = sum(not ok for ok, _ in checks)
    print("RESULT: %d PASS, %d FAIL" % (len(checks) - failed, failed))
    return EXIT_OK if failed == 0 else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
