#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claim_coordination.py —— 原子认领协调权

职责：
  1. 依据 state.json 的当前协调者与 coordination_lease_until 判断协调权是否可认领；
     协调者租约到期（coordinator_timeout，默认 300 秒 = 5 分钟）后，其他参与者可尝试认领。
  2. 以「原子文件创建（os.open O_CREAT|O_EXCL）锁文件」实现比较并交换（CAS）：
     并发认领时只有一个 Agent 能创建锁文件成功，从而只有一个 Agent 能更新 state.json 的协调者字段。
  3. 认领成功后记录：原协调者、接管者、原租约到期时间、认领时间、新修订号；
     更新 state.json 的 coordinator 与 coordination_lease_until，revision 恰好 +1。
  4. 认领失败：重新读取 state.json 后以非 0 退出，不执行任何协调者操作。

用法（在运行时项目目录执行；脚本解析 state.json 所在目录，不写死平台路径）：
  python scripts/claim_coordination.py --state state.json --agent <接管者身份>

退出码：
  0 成功（认领成功，或当前协调者续期/无需认领）
  2 state.json 非法或与 schema 不一致
  3 前置条件不满足（租约未到期 / 身份不在 expected_participants）
  4 原子认领失败（锁已被其他 Agent 持有，已重新读取 state.json）
  1 其他 I/O 或内部错误
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from workflow_core import atomic_write_json, atomic_write_text


def _force_utf8_stdio():
    """统一 stdout/stderr 为 UTF-8，避免 Windows 控制台代码页导致输出编码崩溃。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

# 权威 schema：九阶段封闭枚举（references/state-schema.md 第三节）
STAGES = [
    "initialized",
    "independent_proposal",
    "proposals_complete",
    "cross_response",
    "candidate_decision",
    "user_confirmation",
    "confirmed_decision",
    "delivered",
    "monitoring_stopped",
]

# 权威 schema：15 个最小必填字段（references/state-schema.md 第二节）
REQUIRED_FIELDS = [
    "protocol_version",
    "discussion_id",
    "stage",
    "expected_participants",
    "submission_status",
    "coordinator",
    "coordination_lease_until",
    "coordinator_timeout",
    "participant_timeout",
    "proposal_disposition",
    "monitoring",
    "candidate_decision_ids",
    "confirmed_decision_ids",
    "last_checked_at",
    "revision",
]

EXIT_OK = 0
EXIT_ERR = 1
EXIT_STATE = 2
EXIT_PRECOND = 3
EXIT_LOST = 4


# ---------- 时间工具：Asia/Shanghai，ISO 8601 ----------

def tz_cn():
    """Asia/Shanghai 时区（zoneinfo 优先，回退固定 UTC+8，无夏令时差异）。"""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


def now_iso():
    """当前时间，Asia/Shanghai，ISO 8601（如 2026-08-09T20:04:00+08:00）。"""
    return datetime.now(tz_cn()).isoformat(timespec="seconds")


def parse_iso(s):
    """解析 ISO 8601 时间；无时区则按 Asia/Shanghai 解释；失败返回 None。"""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_cn())
    return dt


def local_hm():
    """审计时间线使用的人类可读时间：%Y-%m-%d %H:%M。"""
    return datetime.now(tz_cn()).strftime("%Y-%m-%d %H:%M")


# ---------- state.json 校验与读写 ----------

def validate_state(s):
    """按权威 schema 校验 state.json，返回 (是否合法, 错误列表)。"""
    errs = []
    if not isinstance(s, dict):
        return False, ["state 必须是 JSON 对象"]
    for f in REQUIRED_FIELDS:
        if f not in s:
            errs.append("缺少必填字段: %s" % f)
    if errs:
        return False, errs

    if not isinstance(s["protocol_version"], str):
        errs.append("protocol_version 必须为字符串")
    if not isinstance(s["discussion_id"], str):
        errs.append("discussion_id 必须为字符串")
    if s["stage"] not in STAGES:
        errs.append("stage 非法: %s" % s["stage"])

    parts = s["expected_participants"]
    if not isinstance(parts, list) or not parts:
        errs.append("expected_participants 必须为非空数组")
    elif len(parts) != len(set(parts)):
        errs.append("expected_participants 不允许重复")
    elif any(not isinstance(p, str) or p != p.lower() or not p for p in parts):
        errs.append("expected_participants 必须为小写非空字符串")

    ss = s.get("submission_status", {})
    if not isinstance(ss, dict):
        errs.append("submission_status 必须为对象")
    elif set(ss.keys()) != set(parts):
        errs.append("submission_status 键集必须与 expected_participants 一致")
    elif any(v not in ("pending", "submitted") for v in ss.values()):
        errs.append("submission_status 值必须是 pending/submitted")

    if s.get("coordinator") not in parts:
        errs.append("coordinator 必须属于 expected_participants")

    if not isinstance(s.get("coordinator_timeout"), int) or s["coordinator_timeout"] <= 0:
        errs.append("coordinator_timeout 必须为正整数")
    if not isinstance(s.get("participant_timeout"), int) or s["participant_timeout"] <= 0:
        errs.append("participant_timeout 必须为正整数")

    if s.get("proposal_disposition") not in ("archive", "delete"):
        errs.append("proposal_disposition 必须是 archive/delete")

    m = s.get("monitoring", {})
    if not isinstance(m, dict):
        errs.append("monitoring 必须为对象")
    else:
        if m.get("enabled") not in (True, False):
            errs.append("monitoring.enabled 必须为布尔")
        if m.get("mode") not in ("reply_before", "periodic", "none"):
            errs.append("monitoring.mode 非法")
        if m.get("status") not in ("active", "stopped", "expired"):
            errs.append("monitoring.status 非法")

    for f in ("candidate_decision_ids", "confirmed_decision_ids"):
        v = s.get(f)
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            errs.append("%s 必须为字符串数组" % f)

    if not isinstance(s.get("revision"), int) or s["revision"] < 0:
        errs.append("revision 必须为非负整数")

    for f in ("coordination_lease_until", "last_checked_at"):
        if parse_iso(s.get(f)) is None:
            errs.append("%s 必须是 ISO8601 时间" % f)

    return len(errs) == 0, errs


def load_state(path):
    """读取并解析 state.json；解析失败抛 IOError。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_state(path, state):
    """以「临时文件 + 原子重命名」写入 state.json，避免半写。"""
    atomic_write_json(path, state)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    atomic_write_text(path, text)


def find_discussion_md(base):
    """定位主讨论文档：顶层 *讨论文档*.md，多个时取最新修改的。"""
    pattern = os.path.join(base, "*讨论文档*.md")
    cands = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def append_audit_line(md_text, line):
    """在审计时间线代码块内追加一行；找不到代码块则追加到文件末尾。"""
    idx = md_text.find("## 一、审计时间线")
    if idx == -1:
        return md_text.rstrip("\n") + "\n\n## 审计记录（追加）\n\n```\n" + line + "\n```\n"
    rest = md_text[idx:]
    f1 = rest.find("```")
    if f1 == -1:
        return md_text.rstrip("\n") + "\n" + line + "\n"
    f2 = rest.find("```", f1 + 3)
    if f2 == -1:
        return md_text.rstrip("\n") + "\n" + line + "\n"
    ins_at = idx + f2
    return md_text[:ins_at] + line.rstrip("\n") + "\n" + md_text[ins_at:]


# ---------- 原子认领核心 ----------

def _lock_stale(lock_path, timeout):
    """锁文件存在超过 timeout 秒视为过期（崩溃残留），允许清理重试。"""
    try:
        return (datetime.now().timestamp() - os.path.getmtime(lock_path)) > timeout
    except OSError:
        return False


def _try_acquire_lock(lock_path):
    """原子创建锁文件（O_CREAT|O_EXCL），成功返回 fd，被占用返回 None，其他错误抛 OSError。"""
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def _release_lock(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass


def do_claim(state_path, state0, agent, timeout):
    """租约已到期场景：原子认领协调权。"""
    lock_path = state_path + ".claim"
    for attempt in range(3):
        fd = None
        try:
            fd = _try_acquire_lock(lock_path)
            if fd is None:
                if _lock_stale(lock_path, timeout):
                    # 过期锁：清理后重试一次
                    _release_lock(lock_path)
                    continue
                # 原子认领失败：重新读取 state.json 后退出非 0
                print("原子认领失败：协调权已被其他 Agent 认领（锁文件存在）。")
                try:
                    fresh = load_state(state_path)
                    ok, errs = validate_state(fresh)
                    if ok:
                        print("重新读取 state.json：协调者=%s，租约到期=%s，revision=%s" % (
                            fresh.get("coordinator"),
                            fresh.get("coordination_lease_until"),
                            fresh.get("revision")))
                    else:
                        print("重新读取的 state.json 非法：%s" % "; ".join(errs))
                except OSError as e:
                    print("重新读取 state.json 失败：%s" % e)
                return EXIT_LOST

            # 持锁成功：写入认领者信息
            os.write(fd, json.dumps({
                "agent": agent,
                "pid": os.getpid(),
                "created_at": now_iso(),
            }).encode("utf-8"))
            os.close(fd)
            fd = None
        except OSError as e:
            print("创建认领锁失败：%s" % e, file=sys.stderr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return EXIT_ERR

        try:
            # 持锁后重新读取 state.json，执行比较并交换：原协调者必须未续期
            state = load_state(state_path)
            ok, errs = validate_state(state)
            if not ok:
                print("重新读取的 state.json 非法：%s" % "; ".join(errs), file=sys.stderr)
                return EXIT_STATE
            now = datetime.now(tz_cn())
            lease = parse_iso(state.get("coordination_lease_until"))
            if lease is not None and now < lease:
                # 持锁期间原协调者续期：放弃认领
                print("比较失败：持锁期间原协调者已续期（租约到期 %s），认领放弃。" % state["coordination_lease_until"])
                return EXIT_LOST
            if state["coordinator"] == agent:
                print("当前协调者已是本身份，无需认领。")
                return EXIT_OK

            old_coord = state["coordinator"]
            old_lease = state["coordination_lease_until"]
            new_lease = (now + timedelta(seconds=timeout)).isoformat(timespec="seconds")
            state["coordinator"] = agent
            state["coordination_lease_until"] = new_lease
            state["revision"] += 1
            new_rev = state["revision"]

            # 审计条目（追加式，不覆盖历史）
            audit = "[%s] 事件=协调接管 | 参与者=%s | 来源=state.json | 处置=原协调者=%s, 原租约到期=%s, 接管时间=%s | revision=%d" % (
                local_hm(), agent, old_coord, old_lease, new_lease, new_rev)

            md_ok = True
            md = find_discussion_md(os.path.dirname(state_path))
            if md is None:
                md_ok = False
                print("未找到主讨论文档，审计条目未落盘；state.json 写入仍有效。", file=sys.stderr)
            else:
                try:
                    text = read_text(md)
                    text2 = append_audit_line(text, audit)
                    if text2 != text:
                        write_text(md, text2)
                except OSError as e:
                    md_ok = False
                    print("审计追加失败（%s）：%s" % (md, e), file=sys.stderr)

            # 提交点：更新 state.json（revision 已 +1）
            write_state(state_path, state)
            print("协调权接管成功：%s → %s，revision=%d，租约至 %s。" % (old_coord, agent, new_rev, new_lease))
            if not md_ok:
                print("恢复入口：请手动将以下审计行追加到主讨论文档审计时间线：")
                print("  %s" % audit)
            return EXIT_OK
        except OSError as e:
            print("接管过程中写盘失败：%s" % e, file=sys.stderr)
            return EXIT_ERR
        finally:
            _release_lock(lock_path)

    print("认领锁反复被占用，放弃认领。", file=sys.stderr)
    return EXIT_LOST


def do_renew(state_path, state, agent, timeout):
    """当前协调者租约已过期：续期租约（revision +1，写审计）。"""
    now = datetime.now(tz_cn())
    new_lease = (now + timedelta(seconds=timeout)).isoformat(timespec="seconds")
    state["coordination_lease_until"] = new_lease
    state["revision"] += 1
    new_rev = state["revision"]

    audit = "[%s] 事件=协调续期 | 参与者=%s | 来源=state.json | 处置=续期至 %s | revision=%d" % (
        local_hm(), agent, new_lease, new_rev)

    md = find_discussion_md(os.path.dirname(state_path))
    md_ok = True
    if md is None:
        md_ok = False
        print("未找到主讨论文档，审计条目未落盘。", file=sys.stderr)
    else:
        try:
            text = read_text(md)
            text2 = append_audit_line(text, audit)
            if text2 != text:
                write_text(md, text2)
        except OSError as e:
            md_ok = False
            print("审计追加失败（%s）：%s" % (md, e), file=sys.stderr)

    write_state(state_path, state)
    print("协调者租约已续期至 %s，revision=%d。" % (new_lease, new_rev))
    if not md_ok:
        print("恢复入口：请手动将以下审计行追加到主讨论文档审计时间线：")
        print("  %s" % audit)
    return EXIT_OK


def main():
    print("RETIRED: claim_coordination.py 属于旧租约认领流程，不再允许独立修改协调者状态；协调者绑定由项目初始化和受控编排器管理。", file=sys.stderr)
    return EXIT_PRECOND

    parser = argparse.ArgumentParser(
        description="原子认领协调权（基于 state.json 租约，O_EXCL 锁实现比较并交换）")
    parser.add_argument("--state", default="state.json", help="state.json 路径（默认当前目录 state.json）")
    parser.add_argument("--agent", required=True, help="尝试认领协调权的参与者身份")
    args = parser.parse_args()

    state_path = os.path.abspath(args.state)
    if not os.path.isfile(state_path):
        print("state.json 不存在：%s" % state_path, file=sys.stderr)
        return EXIT_STATE

    try:
        state = load_state(state_path)
    except OSError as e:
        print("读取 state.json 失败：%s" % e, file=sys.stderr)
        return EXIT_ERR

    ok, errs = validate_state(state)
    if not ok:
        print("state.json 非法，拒绝操作：", file=sys.stderr)
        for e in errs:
            print("  - %s" % e, file=sys.stderr)
        print("恢复入口：修复 state.json 使其符合 references/state-schema.md 后重试。", file=sys.stderr)
        return EXIT_STATE

    agent = args.agent.strip().lower()
    parts = state["expected_participants"]
    if agent not in parts:
        print("身份 %s 不在 expected_participants 中：%s" % (agent, parts), file=sys.stderr)
        return EXIT_PRECOND

    timeout = int(state["coordinator_timeout"])
    now = datetime.now(tz_cn())
    lease = parse_iso(state.get("coordination_lease_until"))
    cur = state["coordinator"]

    if agent == cur:
        # 当前协调者调用：租约未到期则无需操作；到期则续期
        if lease is not None and now < lease:
            print("已是当前协调者 %s，租约未到期（%s），无需认领。" % (cur, state["coordination_lease_until"]))
            return EXIT_OK
        return do_renew(state_path, state, agent, timeout)

    if lease is not None and now < lease:
        # 租约未到期：不可认领
        print("协调权仍由 %s 持有，租约到期时间 %s，尚未超时，不可认领。" % (
            cur, state["coordination_lease_until"]))
        return EXIT_PRECOND

    # 租约已到期：尝试原子认领
    return do_claim(state_path, state, agent, timeout)


if __name__ == "__main__":
    sys.exit(main())
