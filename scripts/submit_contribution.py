#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""submit_contribution.py —— 参与者提交提案/回应，更新 state.json。

校验（顺序执行，任一失败即拒绝并返回非零退出码）：
    1. state.json 存在、为合法 JSON，且通过最小字段/枚举/键集校验（§四.5 先验证后推进）
    2. agent-id 在 expected_participants 名单内（§7 身份逐一匹配，否则拒绝）
    3. 提交文件存在且可读，且符合 §10 命名规范：
       - proposal  必须为 <discussion_dir>/proposals/<agent-id>-提案文档.md
       - response 必须为 <discussion_dir>/responses/<agent-id>-交叉回应文档.md
    4. 当前阶段允许该类型提交（§四 状态流转守卫）：
       - proposal 在 initialized 阶段收到首个合法提案时，原子推进到
         independent_proposal 并记录该提交（一次受控状态写入，revision 恰好 +1）；
         此后 proposal 仅在 independent_proposal 阶段允许
       - response 仅在 cross_response 阶段允许
    5. 同身份不重复计数（§7.3）：已提交时视为幂等成功，不再次 +1 revision、
       不创建第二个参与者条目；未提交时更新同一条目为 submitted 并 revision+1。
    6. 提交状态按类型独立跟踪（§12 至少记录允许扩展）：
       - proposal  → submission_status（提案提交状态）
       - response → response_status（回应提交状态，键集与名单一致）

成功：每次状态写入使 revision 恰好 +1（§四.4），原子写入、失败保留旧状态。

用法：
    python submit_contribution.py <讨论目录> <agent-id> <文件路径> <proposal|response>

退出码：
    0  成功（含重复提交的幂等成功）
    1  运行时错误（state.json 缺失/JSON 损坏、写入失败）
    2  用法/参数错误
    3  校验拒绝（身份不在名单、阶段不允许该类型、文件不存在、state 结构非法）
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from workflow_core import atomic_write_json

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
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_VALIDATION = 3


def shanghai_tz():
    """返回 Asia/Shanghai 时区；Windows 缺 IANA 时区库时降级为固定 +08:00（无夏令时，等价）。"""
    try:
        return ZoneInfo(SHANGHAI)
    except Exception:
        return timezone(timedelta(hours=8))


def now_iso():
    """返回 Asia/Shanghai 时区的 ISO 8601 时间字符串。"""
    return datetime.now(shanghai_tz()).isoformat(timespec="seconds")


def write_state_atomic(state_path, state):
    """原子写入 state.json；失败清理临时文件并保留旧状态（单动作编排失败路径）。"""
    atomic_write_json(state_path, state)


def validate_state(state):
    """校验 state.json 最小字段、类型与枚举；返回错误列表，为空即合法（§四.5 校验顺序）。"""
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


def main(argv=None):
    print("RETIRED: submit_contribution.py 是旧流程入口，不再允许直接修改机器状态；请由 orchestrate_discussion.py 单点消费 Runtime 回执。", file=sys.stderr)
    return EXIT_VALIDATION

    # Windows 控制台输出 UTF-8 中文
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="由当前协调者登记参与者已写入的提案/回应；参与者不得直接修改 state.json。")
    parser.add_argument("discussion_dir", help="讨论工作区目录路径")
    parser.add_argument("agent_id", help="提交者身份（须在 expected_participants 名单内）")
    parser.add_argument("file_path", help="提交的提案/回应文件路径")
    parser.add_argument("type", choices=("proposal", "response"),
                        help="提交类型：proposal 或 response")
    parser.add_argument("--actor", required=True,
                        help="执行登记的当前协调者身份；必须显式提供，参与者身份将被拒绝")
    args = parser.parse_args(argv)

    # ---- 读取并校验 state.json ----
    state_path = os.path.join(os.path.abspath(args.discussion_dir), "state.json")
    if not os.path.isfile(state_path):
        print("FAIL: 未找到 state.json，请先运行 init_discussion.py", file=sys.stderr)
        return EXIT_RUNTIME
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print("FAIL: 读取/解析 state.json 失败: " + str(e), file=sys.stderr)
        return EXIT_RUNTIME

    errors = validate_state(state)
    if errors:
        print("FAIL: state.json 结构非法（拒绝推进）:", file=sys.stderr)
        for e in errors:
            print("    - " + e, file=sys.stderr)
        return EXIT_VALIDATION

    # state.json is coordinator-owned machine authority. The participant only
    # writes its scoped artifact/receipt through Participant Runtime; this CLI
    # merely lets the coordinator register that already-written artifact.
    actor = args.actor.strip().lower()
    if actor != state["coordinator"]:
        print("FAIL: 仅当前协调者 %s 可登记贡献并修改 state.json；参与者必须通过项目级 Runtime 写产物/回执" %
              state["coordinator"], file=sys.stderr)
        return EXIT_VALIDATION

    # ---- 身份校验（§7） ----
    agent_id = args.agent_id.strip().lower()
    if agent_id not in state["expected_participants"]:
        print("FAIL: 身份不在参与者名单内: " + agent_id, file=sys.stderr)
        return EXIT_VALIDATION

    # ---- 文件存在性 ----
    file_path = os.path.abspath(args.file_path)
    if not os.path.isfile(file_path):
        print("FAIL: 提交文件不存在: " + file_path, file=sys.stderr)
        return EXIT_VALIDATION

    # ---- 文件命名规范（§10）：proposals/<agent-id>-提案文档.md / responses/<agent-id>-交叉回应文档.md ----
    # 统一为与 validate_discussion 推导路径一致的约定，避免"已提交但校验时找不到文件"的不一致。
    expected_rel = os.path.join(
        "proposals" if args.type == "proposal" else "responses",
        agent_id + ("-提案文档.md" if args.type == "proposal" else "-交叉回应文档.md"))
    expected_path = os.path.abspath(os.path.join(os.path.abspath(args.discussion_dir), expected_rel))
    if file_path != expected_path:
        print("FAIL: 文件名不符合 §10 命名规范，期望路径: " + expected_rel, file=sys.stderr)
        return EXIT_VALIDATION

    # ---- 阶段守卫（§四 状态流转） ----
    stage = state["stage"]
    # initialized → independent_proposal 是合法下一步（守卫只禁越级/逆向）；
    # 收到首个合法提案时在同一受控写入中推进阶段并记录提交（单动作编排，revision 恰好 +1）。
    if args.type == "proposal" and stage not in ("initialized", "independent_proposal"):
        print("FAIL: proposal 提交仅在 initialized→independent_proposal 阶段允许，当前阶段: " + stage,
              file=sys.stderr)
        return EXIT_VALIDATION
    if args.type == "response" and stage != "cross_response":
        print("FAIL: response 提交仅在 cross_response 阶段允许，当前阶段: " + stage,
              file=sys.stderr)
        return EXIT_VALIDATION

    # ---- 按类型选择提交状态字段（§12 至少记录；response 独立跟踪） ----
    # proposal → submission_status（提案提交状态）；response → response_status（回应提交状态）。
    status_field = "submission_status" if args.type == "proposal" else "response_status"

    # ---- proposal：同身份不重复计数（§7.3），幂等成功 ----
    if args.type == "proposal" and state[status_field].get(agent_id) == "submitted":
        print("OK: " + agent_id + " 已提交过 proposal，幂等成功（不重复计数，revision 不变）")
        return EXIT_OK

    # ---- response：支持多轮（讨论无上限，收敛由 Rainier 决定，不存在"回应齐"门禁）----
    # 每次提交回应都视为新一轮：response_status 保持 submitted（已回应），response_rounds[agent] 递增。
    response_round = 0
    if args.type == "response":
        rounds = state.get("response_rounds", {}) or {}
        response_round = int(rounds.get(agent_id, 0)) + 1

    # ---- 更新同一条目、必要时推进阶段，revision+1（单次原子写入） ----
    new_state = dict(state)
    new_state[status_field] = dict(state[status_field])
    new_state[status_field][agent_id] = "submitted"
    if args.type == "response":
        new_state["response_rounds"] = dict(state.get("response_rounds", {}) or {})
        new_state["response_rounds"][agent_id] = response_round
    if args.type == "proposal" and stage == "initialized":
        new_state["stage"] = "independent_proposal"
    new_state["revision"] = state["revision"] + 1
    try:
        write_state_atomic(state_path, new_state)
    except OSError as e:
        print("FAIL: 写入 state.json 失败: " + str(e), file=sys.stderr)
        return EXIT_RUNTIME

    if args.type == "response":
        print("OK: " + agent_id + " 第 %d 轮回应已提交, revision=%d" % (response_round, new_state["revision"]))
    else:
        print("OK: " + agent_id + " proposal 提交成功, revision=" + str(new_state["revision"])
              + (", 阶段推进: initialized → independent_proposal" if stage == "initialized" else ""))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
