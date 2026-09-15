#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_proposals.py —— 合并提案 + 推进阶段

职责：
  1. 从 proposals/ 读取全部已提交提案（命名 <agent-id>-提案文档.md，agent-id 与
     expected_participants 身份键完全一致），按模板合并进主讨论文档「二、独立提案」区块，
     保留原文并标注来源身份与 sha256。
  2. 独立提案阶段结束后由合法协调者执行：先校验全部身份已提交（double-satisfaction），
     再将阶段推进到 cross_response（若仍在 independent_proposal 则先推进到
     proposals_complete，逐级流转、每级 revision +1）。
  3. 按 state.json 的 proposal_disposition 处置提案文件：
       archive → 移动到 archive/proposals/（保留原文件名/内容/时间/可验证哈希）；
       delete  → 生成精确删除清单到 deliverables/（不在本脚本删除，需后续 Rainier 确认）。
  4. Markdown 与 state.json 双写（同一受控编排动作）；任一步失败保留旧状态并给出恢复入口。
  5. 幂等：已合并区块以 sha256 标记去重，重跑不会重复追加；阶段已越过 proposals_complete 时直接空操作。

用法（在运行时项目目录执行；脚本解析 state.json 所在目录）：
  python scripts/merge_proposals.py --state state.json --actor <协调者身份> [--discussion <主讨论文档>]

退出码：
  0 成功（合并并推进，或已合并的空操作）
  2 state.json 非法或与 schema 不一致
  3 前置条件不满足（非协调者 / 存在未提交参与者 / 阶段不允许合并）
  1 其他 I/O 或内部错误
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from workflow_core import (
    WorkspaceTransaction,
    atomic_write_json,
    atomic_write_text,
    iso_now as core_iso_now,
    recover_workspace_transactions,
    sha256_file as core_sha256_file,
    sha256_text,
)


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


# ---------- 时间工具：Asia/Shanghai，ISO 8601 ----------

def tz_cn():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(tz_cn()).isoformat(timespec="seconds")


def parse_iso(s):
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
    return datetime.now(tz_cn()).strftime("%Y-%m-%d %H:%M")


# ---------- state.json 校验与读写 ----------

def validate_state(s):
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
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_state(path, state):
    atomic_write_json(path, state)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    atomic_write_text(path, text)


def find_discussion_md(base):
    pattern = os.path.join(base, "*讨论文档*.md")
    cands = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def append_audit_line(md_text, line):
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


def append_to_section(md_text, heading, block):
    """在指定编号区块内、下一个编号章节标题（## N、…）之前插入区块内容。

    不以任意「## 」为边界：提案正文含「## 独立性声明」「## 提案正文」等二级子标题，
    若按任意二级标题截断会把后续提案块错误插入前一块内部。
    """
    idx = md_text.find(heading)
    if idx == -1:
        return md_text.rstrip("\n") + "\n\n" + heading + "\n\n" + block.rstrip("\n") + "\n"
    nxt = idx + len(heading)
    m = re.search(r"^## [一二三四五六七八九十]+、", md_text[nxt:], flags=re.M)
    insert_at = nxt + m.start() if m else len(md_text)
    head = md_text[:insert_at]
    if not head.endswith("\n"):
        head += "\n"
    if not head.endswith("\n\n"):
        head += "\n"
    return head + block.rstrip("\n") + "\n\n" + md_text[insert_at:]


def sha256_file(path):
    return core_sha256_file(path)


# ---------- 状态推进 ----------

def stage_advance_path(cur, target):
    """返回从 cur 逐级推进到 target 的合法路径（含 cur 与 target）；非法返回 None。"""
    if cur == target:
        return [cur]
    if cur not in STAGES or target not in STAGES:
        return None
    idx = STAGES.index(cur)
    tidx = STAGES.index(target)
    if tidx < idx:
        # 唯一回退例外：user_confirmation → cross_response（受用户授权重做），本脚本不使用
        return None
    if cur in ("delivered", "monitoring_stopped"):
        # 终端阶段无出边
        return None
    return STAGES[idx:tidx + 1]


def renew_lease_in_place(state, actor):
    """协调者每次有效状态写入都续期租约。"""
    if actor == state.get("coordinator"):
        state["coordination_lease_until"] = (
            datetime.now(tz_cn()) + timedelta(seconds=int(state["coordinator_timeout"]))
        ).isoformat(timespec="seconds")


def build_proposal_block(agent, filename, content, sha):
    """按讨论模板合并格式生成单份提案区块。"""
    head = "### %s 提案（来源: proposals/%s）\n\n" % (agent, filename)
    meta = "> sha256: %s\n\n" % sha
    return head + meta + content.rstrip("\n") + "\n"


def collect_proposals(proposals_dir):
    """读取 proposals/ 下的提案文件，返回 {agent: (相对文件名, 绝对路径, 内容, sha256)}。"""
    found = {}
    pattern = os.path.join(proposals_dir, "*提案文档*.md")
    for p in sorted(glob.glob(pattern)):
        if not os.path.isfile(p):
            continue
        filename = os.path.basename(p)
        stem = filename[:-3] if filename.endswith(".md") else filename
        m = re.match(r"^(.*)-提案文档$", stem)
        if not m:
            continue
        agent = m.group(1).strip().lower()
        found[agent] = (filename, p, read_text(p), sha256_file(p))
    return found


def main():
    print("RETIRED: merge_proposals.py 是旧流程入口，不再合并提案或修改状态；请由 orchestrate_discussion.py 单点完成门禁、合并与阶段推进。", file=sys.stderr)
    return EXIT_PRECOND

    parser = argparse.ArgumentParser(description="合并独立提案并推进阶段到 cross_response")
    parser.add_argument("--state", default="state.json", help="state.json 路径（默认当前目录 state.json）")
    parser.add_argument("--actor", required=True, help="执行合并的协调者身份")
    parser.add_argument("--discussion", default=None, help="主讨论文档路径（缺省自动发现）")
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

    actor = args.actor.strip().lower()
    if actor != state["coordinator"]:
        print("仅当前协调者 %s 可以合并提案；%s 无权执行。" % (state["coordinator"], actor), file=sys.stderr)
        return EXIT_PRECOND

    base = os.path.dirname(state_path)
    try:
        recover_workspace_transactions(base)
    except Exception as e:
        print("检测到未完成事务且自动恢复失败：%s" % e, file=sys.stderr)
        return EXIT_ERR
    discussion = args.discussion
    if discussion is None:
        discussion = find_discussion_md(base)
    if discussion is None or not os.path.isfile(discussion):
        print("未找到主讨论文档（需要顶层 *讨论文档*.md，或用 --discussion 指定）。", file=sys.stderr)
        return EXIT_ERR

    stage = state["stage"]
    # 幂等：阶段已越过 proposals_complete → 视为已合并，空操作
    after_merge = STAGES[STAGES.index("cross_response"):]
    if stage in after_merge:
        print("阶段已是 %s，提案此前已合并，空操作。" % stage)
        return EXIT_OK
    if stage not in ("independent_proposal", "proposals_complete"):
        print("阶段 %s 不允许合并提案（需 independent_proposal 或 proposals_complete）。" % stage, file=sys.stderr)
        return EXIT_PRECOND

    # 收集提案并核对是否全部身份已提交（double-satisfaction 证据）
    proposals_dir = os.path.join(base, "proposals")
    if not os.path.isdir(proposals_dir):
        print("proposals/ 目录不存在：%s" % proposals_dir, file=sys.stderr)
        return EXIT_PRECOND
    found = collect_proposals(proposals_dir)

    pending = []
    for p in state["expected_participants"]:
        if p not in found:
            pending.append(p)
    if pending:
        print("以下参与者的提案缺失，不能合并：%s" % pending, file=sys.stderr)
        print("提交状态：%s" % state["submission_status"], file=sys.stderr)
        print("恢复入口：补齐提案或修正提交状态后再重跑；state 未变化。", file=sys.stderr)
        return EXIT_PRECOND

    # 额外出现的不在名单中的提案文件：跳过并在摘要中提示（不写入主文档，防污染）
    extra = [a for a in found if a not in state["expected_participants"]]
    for a in extra:
        del found[a]

    md_text = read_text(discussion)

    # 幂等：按 sha256 标记去重，已合并过的区块不再追加
    merged_agents = []
    for agent in state["expected_participants"]:
        filename, _p, content, sha = found[agent]
        if ("sha256: %s" % sha) in md_text:
            merged_agents.append(agent)  # 此前已合并（可能上次部分失败），跳过
            continue
        block = build_proposal_block(agent, filename, content, sha)
        md_text = append_to_section(md_text, "## 二、独立提案", block)
        merged_agents.append(agent)

    # 预计算修订号与审计行
    base_rev = state["revision"]
    audits = []
    if stage == "independent_proposal":
        # 先推进到 proposals_complete（revision +1）
        r1 = base_rev + 1
        audits.append("[%s] 事件=阶段推进 | 参与者=%s | 来源=state.json | 处置=independent_proposal → proposals_complete | revision=%d" % (
            local_hm(), actor, r1))
        # 再推进到 cross_response（revision +1）
        r2 = r1 + 1
        audits.append("[%s] 事件=合并提案 | 参与者=%s | 来源=proposals/ | 处置=合并 %d 份提案, proposals_complete → cross_response | revision=%d" % (
            local_hm(), actor, len(merged_agents), r2))
        final_rev = r2
    else:
        r1 = base_rev + 1
        audits.append("[%s] 事件=合并提案 | 参与者=%s | 来源=proposals/ | 处置=合并 %d 份提案, proposals_complete → cross_response | revision=%d" % (
            local_hm(), actor, len(merged_agents), r1))
        final_rev = r1

    for line in audits:
        md_text = append_audit_line(md_text, line)

    # 先在内存中形成唯一最终状态；Markdown、提案处置清单、归档文件和
    # state.json 将由同一可恢复事务一次提交，任一失败全部回滚。
    path = stage_advance_path(stage, "cross_response")
    for i in range(1, len(path)):
        state["stage"] = path[i]
        state["revision"] += 1
        renew_lease_in_place(state, actor)
    if isinstance(state.get("content_authority"), dict):
        state["content_authority"]["discussion_path"] = os.path.relpath(discussion, base).replace("\\", "/")
        state["content_authority"]["sha256"] = sha256_text(md_text)
        state["content_authority"]["updated_at"] = core_iso_now()

    disposition = state["proposal_disposition"]
    fail_at = os.environ.get("MULTIAGENT_TEST_FAIL_MERGE_AT", "").strip()
    if fail_at not in {"", "after_markdown", "after_manifest"}:
        print("非法测试故障点 MULTIAGENT_TEST_FAIL_MERGE_AT=%s" % fail_at, file=sys.stderr)
        return EXIT_PRECOND
    discussion_relative = os.path.relpath(discussion, base).replace("\\", "/")
    manifest_targets: set[str] = set()

    def merge_fault_hook(operation, _applied):
        target = operation.get("target", "")
        if fail_at == "after_markdown" and target == discussion_relative:
            raise OSError("injected merge failure after_markdown")
        if fail_at == "after_manifest" and target in manifest_targets:
            raise OSError("injected merge failure after_manifest")

    transaction = WorkspaceTransaction(base, "merge-proposals", fault_hook=merge_fault_hook)
    try:
        transaction.stage_text(discussion, md_text)
        if disposition == "archive":
            archive_entries = []
            archive_dir = os.path.join(base, "archive", "proposals")
            manifest_path = os.path.join(archive_dir, "manifest.json")
            manifest_targets.add(os.path.relpath(manifest_path, base).replace("\\", "/"))
            old_entries = []
            if os.path.isfile(manifest_path):
                existing = json.loads(read_text(manifest_path))
                old_entries = existing.get("entries", []) if isinstance(existing, dict) else []
            for agent in sorted(found):
                filename, src, _content, sha = found[agent]
                st = os.stat(src)
                dst = os.path.join(archive_dir, filename)
                transaction.stage_copy(src, dst)
                transaction.stage_delete(src)
                archive_entries.append({
                    "agent": agent,
                    "filename": filename,
                    "sha256": sha,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz_cn()).isoformat(timespec="seconds"),
                })
            transaction.stage_json(manifest_path, {
                "discussion_id": state["discussion_id"],
                "recorded_at": now_iso(),
                "entries": old_entries + archive_entries,
            })
            disp_report = "已归档 %d 份提案到 archive/proposals/（manifest: %s）" % (
                len(archive_entries), manifest_path)
        else:
            # legacy delete remains a precise, non-destructive preauthorization list;
            # actual project-preauthorized deletion is only performed by orchestrator
            # against archive/proposals/deletion-manifest.json.
            listed = []
            for agent in sorted(found):
                filename, src, _content, sha = found[agent]
                st = os.stat(src)
                listed.append({
                    "agent": agent,
                    "path": os.path.relpath(src, base),
                    "sha256": sha,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz_cn()).isoformat(timespec="seconds"),
                })
            list_path = os.path.join(base, "deliverables", "删除清单_%s.json" % state["discussion_id"])
            manifest_targets.add(os.path.relpath(list_path, base).replace("\\", "/"))
            transaction.stage_json(list_path, {
                "discussion_id": state["discussion_id"],
                "listed_at": now_iso(),
                "policy": "delete",
                "confirmed": False,
                "note": "未确认前不删除任何文件；仅可处置本清单中的精确路径与哈希。",
                "files": listed,
            })
            disp_report = "已生成删除清单（未删除提案）：%s" % list_path
        transaction.stage_json(state_path, state)
        transaction.commit()
        transaction.close()
    except Exception as e:
        print("合并事务失败；Markdown、提案处置与 state 已回滚：%s" % e, file=sys.stderr)
        return EXIT_ERR

    print("提案合并完成：共合并 %d 份提案，阶段推进至 %s，revision=%d。" % (
        len(merged_agents), state["stage"], final_rev))
    print("提案处置（%s）：%s" % (disposition, disp_report))
    if extra:
        print("注意：跳过不在 expected_participants 中的提案文件：%s" % extra)
    return EXIT_OK
if __name__ == "__main__":
    sys.exit(main())
