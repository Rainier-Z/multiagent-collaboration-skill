#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
confirm_decision.py —— 候选决策登记 + 决策确认闸门（§13）

两种模式（二选一）：

  模式一：--new-candidate <ID> —— 登记候选决策并推进 cross_response → candidate_decision
    1. 先确保「三、交叉回应」区块完整：已有回应块优先用文档，缺失者从
       responses/<agent>-交叉回应文档.md 读取并合并写入该区块（幂等）。
    2. 从主讨论文档读取各方独立提案与交叉回应，组装 §13 结构化决策包
       （共识事项 / 分歧事项 / 候选方案 / 各方案支持者 / 主要依据 / 风险与代价 /
        各 Agent 推荐 / 需 Rainier 选择的问题），写入主文档「四、结构化决策包」。
    3. 登记 candidate_decision_ids。
    4. 推进 cross_response → candidate_decision（一次受控写入，revision +1）。
    5. 幂等：该 ID 已登记则空操作退出 0。

  模式二：--candidate-id + --confirm-text —— 混合确认闸门（§13（二））
    1. 单项且无歧义：自然语言可直接确认固化。
    2. 多项 / 修改性 / 存在歧义：先回显决策 ID 与完整内容，Rainier 二次确认后才固化。
    3. 固化前保存：候选决策 ID、确认原文、确认时间、记录者（协调者）。
    4. 只有确认通过才固化（推进 user_confirmation → confirmed_decision），否则拒绝。
    5. 幂等：该候选决策已确认则空操作退出 0。

用法（在运行时项目目录执行；脚本解析 state.json 所在目录）：
  python scripts/confirm_decision.py --state state.json --actor <协调者身份> --platform-id <平台> --session-id <会话> --new-candidate <候选决策ID>
  python scripts/confirm_decision.py --state state.json --actor <协调者身份> \
      --platform-id <平台> --session-id <会话> --candidate-id <候选决策ID> \
      --confirm-text <Rainier 确认原文> [--second-confirm <二次确认原文>]

二次确认：当确认涉及多项/修改/歧义时，若未提供 --second-confirm，
脚本会先回显决策 ID 与完整内容并交互式询问；非交互环境需显式提供 --second-confirm。

退出码：
  0 成功（登记 / 固化 / 幂等空操作）
  2 state.json 非法或与 schema 不一致
  3 前置条件不满足（非协调者 / 候选 ID 不存在 / 确认被拒绝 / 需二次确认但未提供 / 阶段不允许）
  1 其他 I/O 或内部错误
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workflow_core import (
    WorkflowError, atomic_write_json, atomic_write_text, sha256_file,
    validate_coordinator_execution,
)
from export_docx import _default_state_path, _workspace_root_for_state, do_export
from orchestrate_discussion import _all_stop_receipts_completed, _read_receipts
from transactions import EventTransaction


def _force_utf8_stdio():
    """统一 stdout/stderr 为 UTF-8，避免 Windows 控制台代码页导致输出编码崩溃。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

STAGES = [
    "initialized",
    "independent_proposal",
    "cross_response",
    "human_review",
    "finalizing",
    "human_review",
    "finalizing",
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


# ---------- 时间工具 ----------

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
    """在指定编号区块内、下一个编号章节标题之前插入区块内容。"""
    idx = md_text.find(heading)
    if idx == -1:
        return md_text.rstrip("\n") + "\n\n" + heading + "\n\n" + block.rstrip("\n") + "\n"
    nxt = idx + len(heading)
    m = NUM_HEAD_RE.search(md_text[nxt:])
    insert_at = nxt + m.start() if m else len(md_text)
    head = md_text[:insert_at]
    if not head.endswith("\n"):
        head += "\n"
    if not head.endswith("\n\n"):
        head += "\n"
    return head + block.rstrip("\n") + "\n\n" + md_text[insert_at:]


# ---------- 确认分析 ----------

AMBIG_SIGNALS = [
    "修改", "调整", "改成", "改为", "部分", "不确定", "可能", "或许",
    "待定", "再看", "暂缓", "暂不", "倾向", "有保留", "是否", "能否", "可否",
    "吗", "？", "?", "但",
]

AFFIRMATIVE_SIGNALS = ["确认", "同意", "通过", "接受", "认可", "没问题", "可以", "采纳", "批准", "ok", "OK", "Yes", "yes"]

# 否定 + 肯定结构（如「不同意」「未确认」「拒绝通过」）→ 拒绝语义，不得固化
NEGATED_AFFIRM_RE = re.compile(r"(?:不|没|未|无|拒绝|反对|否决)\s*(?:同意|确认|通过|接受|认可|可以|采纳|批准)")
# 显式拒绝/否决词（不含「否」以免「是否/可否」等疑问句被误判）
REJECT_WORDS = ("拒绝", "反对", "否决", "驳回", "不同意", "不支持", "不予确认", "不通过")


def _has_rejection(text):
    """检测拒绝/否决语义；命中则不得固化为正式决策（§13 只有明确确认才固化）。"""
    if NEGATED_AFFIRM_RE.search(text):
        return True
    for w in REJECT_WORDS:
        if w in text:
            return True
    return False


def analyze_confirmation(state, candidate_id, text):
    """判断确认性质。返回 (kind, reason)，kind ∈ {"direct", "echo_required", "rejected"}。"""
    # 确认原文中引用了哪些候选决策 ID
    ids_in_text = []
    for cid in state.get("candidate_decision_ids", []):
        if re.search(r"\b" + re.escape(cid) + r"\b", text):
            ids_in_text.append(cid)

    if candidate_id not in ids_in_text:
        return "echo_required", "确认原文未明确引用候选决策 %s" % candidate_id
    if len(ids_in_text) > 1:
        return "echo_required", "确认原文引用了多个候选决策: %s" % ids_in_text
    # 拒绝/否决检测必须先于肯定信号匹配，避免「不同意」被「同意」子串误判为肯定
    if _has_rejection(text):
        return "rejected", "确认原文含拒绝/否决语义，不得固化为正式决策"
    found = [w for w in AMBIG_SIGNALS if w in text]
    if found:
        return "echo_required", "确认原文含修改/歧义信号词: %s" % found
    if not any(w in text for w in AFFIRMATIVE_SIGNALS):
        return "echo_required", "确认原文缺少明确肯定语（确认/同意/通过等）"
    return "direct", "单项且无歧义，可直接确认"


def stop_receipts_gate_error(state, workspace):
    """Require fresh participant stop evidence independently of mutable state flags."""
    participants = state.get("expected_participants")
    if not isinstance(participants, list) or not participants:
        return "缺少预期参与者列表；禁止确认。"
    try:
        workspace_path = Path(workspace)
        receipts = _read_receipts(workspace_path)
        if not _all_stop_receipts_completed(workspace_path, state, receipts, participants):
            return "未能验证每位参与者唯一且绑定有效签名停止证明的完成回执；禁止确认。"
    except Exception:
        return "停止回执或签名证明读取失败；禁止确认。"
    return None


def _modern_lifecycle(state):
    """Whether this state was initialized with the round/event protocol."""
    return isinstance(state.get("coordinator_participant"), dict) and isinstance(state.get("round"), int)


def confirmation_gate_error(state, workspace):
    """Validate candidate delivery before human confirmation.

    Participants remain active during human review. Stop evidence is checked
    after the confirmed final decision is published.
    """
    modern = _modern_lifecycle(state)
    allowed = {"human_review"} if modern else {"user_confirmation", "human_review"}
    if state.get("stage") not in allowed:
        return "仅 human_review 阶段允许确认候选决策。"
    if not modern:
        legacy_stop_error = stop_receipts_gate_error(state, workspace)
        if legacy_stop_error:
            return legacy_stop_error

    delivery = state.get("candidate_delivery")
    if not isinstance(delivery, dict) or delivery.get("opened") is not True:
        return "候选 Word 未成功打开；禁止确认。"
    if not isinstance(delivery.get("opened_at"), str) or not delivery["opened_at"].strip():
        return "候选 Word 缺少打开时间证据；禁止确认。"

    relative_path = delivery.get("path")
    if not isinstance(relative_path, str) or not relative_path:
        return "候选 Word 路径缺失；禁止确认。"
    candidate_path = os.path.realpath(os.path.join(workspace, relative_path))
    workspace_path = os.path.realpath(workspace)
    try:
        if os.path.commonpath([candidate_path, workspace_path]) != workspace_path:
            return "候选 Word 路径越出工作区；禁止确认。"
    except ValueError:
        return "候选 Word 路径无效；禁止确认。"
    if not os.path.isfile(candidate_path):
        return "候选 Word 文件不存在；禁止确认。"
    expected_hash = delivery.get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(candidate_path) != expected_hash:
        return "候选 Word 哈希缺失或不匹配；禁止确认。"

    return None


def extract_decision_package(md_text):
    """提取「## 四、结构化决策包」区块内容，用于回显完整内容。"""
    m = re.search(r"^## 四、结构化决策包\s*(.*?)(?=^## |\Z)", md_text, flags=re.M | re.S)
    if m:
        return m.group(1).strip()
    return ""


def echo_full(candidate_id, package, reason):
    """回显决策 ID 与完整内容（二次确认前必须回显）。"""
    print("=" * 60)
    print("需要二次确认：%s" % reason)
    print("决策 ID: %s" % candidate_id)
    print("--- 完整内容（主讨论文档「四、结构化决策包」）---")
    print(package if package else "（未在主讨论文档中找到结构化决策包，仅回显决策 ID）")
    print("-" * 60)


def build_confirm_record(candidate_id, confirm_text, recorder, rev):
    """构建「六、确认固化记录」区块内容。"""
    now = local_hm()
    return (
        "- 候选决策 ID: %s\n"
        "- 确认原文: %s\n"
        "- 确认时间: %s (Asia/Shanghai)\n"
        "- 确认者: Rainier\n"
        "- 记录者: %s\n"
        "- 修订号: %d\n"
    ) % (candidate_id, confirm_text, now, recorder, rev)


# ---------- 结构化决策包组装（--new-candidate 模式） ----------

# 章节标题边界：只以「## 数字、」编号章节作为区块边界。
# 提案/回应正文内存在「## 独立性声明」「## 回应正文」等二级子标题，
# 若以任意「## 」为边界会把块内容截断，导致决策包解析不完整。
NUM_HEAD_RE = re.compile(r"^## [一二三四五六七八九十]+、", re.MULTILINE)


def section_end(md_text, start):
    """从 start 起找下一个编号章节标题（## N、…）作为区块结束；无则到文档末尾。"""
    m = NUM_HEAD_RE.search(md_text[start:])
    return start + m.start() if m else len(md_text)


def extract_section(md_text, heading):
    """提取某 ## 编号区块内容（不含标题），到下一个编号章节标题或文档末尾。"""
    idx = md_text.find(heading)
    if idx == -1:
        return ""
    nxt = idx + len(heading)
    return md_text[nxt:section_end(md_text, nxt)]


def parse_agent_blocks(seg):
    """解析区块内 '### <agent> 提案/交叉回应（来源: ...）' 分组，返回 {agent: 内容}。

    仅识别带「来源:」标记的块标题作为边界，避免把块内部的
    '### 一、…' 等子标题误判为新块而截断内容。
    """
    blocks = {}
    pat = r"^### (\S+) (?:提案|交叉回应)（来源: [^\n]*?）\s*\n(.*?)(?=^### \S+ (?:提案|交叉回应)（来源: |\Z)"
    for bm in re.finditer(pat, seg, flags=re.M | re.S):
        agent = bm.group(1).strip().lower()
        blocks[agent] = bm.group(2).strip()
    return blocks


def extract_subsection(content, heading):
    """从单份提案/回应内容中提取指定子标题下的文本。"""
    m = re.search(r"^#{1,6}\s*%s\s*\n(.*?)(?=^#{1,6}\s|\Z)" % re.escape(heading), content, flags=re.M | re.S)
    if m:
        return m.group(1).strip()
    return ""


def replace_section(md_text, heading, new_content):
    """用新内容替换 heading 到下一个编号章节标题之间的整段；无该 heading 则追加。"""
    idx = md_text.find(heading)
    if idx == -1:
        return md_text.rstrip("\n") + "\n\n" + heading + "\n\n" + new_content.rstrip("\n") + "\n"
    nxt = idx + len(heading)
    end = section_end(md_text, nxt)
    head = md_text[:idx]
    tail = md_text[end:]
    return head + heading + "\n\n" + new_content.rstrip("\n") + "\n\n" + tail.lstrip("\n")


def merge_responses_into_doc(md_text, base, participants):
    """把 responses/<agent>-交叉回应文档.md 合并进主文档「三、交叉回应」区块（幂等）。

    规则：区块内已有某参与者的回应块则保留文档内容（文档优先）；缺失者从
    responses/ 目录按命名读取并追加，使 Markdown 权威记录完整。
    全部参与者已有回应块或文件缺失时不做修改。
    """
    heading = "## 三、交叉回应"
    idx = md_text.find(heading)
    if idx == -1:
        return md_text  # 无该区块，保持幂等不处理
    nxt = idx + len(heading)
    end = section_end(md_text, nxt)
    seg = md_text[nxt:end]

    existing = parse_agent_blocks(seg)
    missing = [a for a in participants if a not in existing]
    if not missing:
        return md_text  # 区块已完整，跳过（幂等）

    # 保留区块前置说明（首个回应块标题之前的内容，通常是引用说明行）
    first = re.search(r"^### \S+ 交叉回应（来源:", seg, flags=re.M)
    head_part = seg[:first.start()] if first else seg

    blocks = []
    # 已存在块按原文顺序保留
    pat = r"^### \S+ 交叉回应（来源: [^\n]*?）\s*\n(.*?)(?=^### \S+ 交叉回应（来源:|\Z)"
    for bm in re.finditer(pat, seg, flags=re.M | re.S):
        blocks.append(bm.group(0).rstrip())
    # 缺失者从 responses/ 读取
    responses_dir = os.path.join(base, ".multiagent", "views")
    for a in participants:
        if a in existing:
            continue
        if os.path.isdir(responses_dir):
            fp = os.path.join(responses_dir, a, "outputs", "交叉回应文档.md")
            if os.path.isfile(fp):
                content = read_text(fp).strip()
                source = ".multiagent/views/%s/outputs/交叉回应文档.md" % a
                block = "### %s 交叉回应（来源: %s）\n\n%s" % (a, source, content)
                blocks.append(block)

    if not blocks:
        return md_text

    new_content = (head_part.rstrip() + "\n\n" if head_part.strip() else "") + "\n\n".join(blocks).rstrip() + "\n"
    return md_text[:idx] + heading + "\n\n" + new_content + "\n" + md_text[end:]


def assemble_decision_package(md_text):
    """从主文档「二、独立提案」「三、交叉回应」机械组装 §13 结构化决策包。

    返回 (决策包区块, 提案块映射, 回应块映射)。
    """
    proposals = parse_agent_blocks(extract_section(md_text, "## 二、独立提案"))
    responses = parse_agent_blocks(extract_section(md_text, "## 三、交叉回应"))

    def join(items, empty_msg):
        return "\n".join(items) if items else empty_msg

    consensus = []
    for a in sorted(responses):
        c = extract_subsection(responses[a], "一、共识点")
        if c:
            consensus.append("【%s】%s" % (a, c))

    dissent = []
    for a in sorted(responses):
        c = extract_subsection(responses[a], "二、分歧点")
        if c:
            dissent.append("【%s】%s" % (a, c))

    plans = []
    for a in sorted(proposals):
        c = extract_subsection(proposals[a], "二、提案内容")
        plans.append("#### 方案（%s）\n\n%s" % (a, c if c else "（提案内容缺失）"))

    support = []
    for a in sorted(responses):
        rec = extract_subsection(responses[a], "六、推荐")
        support.append("%s: %s" % (a, rec if rec else "未明确表态"))

    basis = []
    for a in sorted(proposals):
        c = extract_subsection(proposals[a], "三、主要依据")
        if c:
            basis.append("【%s 提案】%s" % (a, c))
    for a in sorted(responses):
        c = extract_subsection(responses[a], "四、主要依据")
        if c:
            basis.append("【%s 回应】%s" % (a, c))

    risks = []
    for a in sorted(proposals):
        c = extract_subsection(proposals[a], "四、风险与代价")
        if c:
            risks.append("【%s 提案】%s" % (a, c))
    for a in sorted(responses):
        c = extract_subsection(responses[a], "五、风险与代价")
        if c:
            risks.append("【%s 回应】%s" % (a, c))

    recs = []
    for a in sorted(responses):
        c = extract_subsection(responses[a], "六、推荐")
        recs.append("%s: %s" % (a, c if c else "未明确表态"))

    questions = []
    for a in sorted(proposals):
        c = extract_subsection(proposals[a], "五、待确认问题")
        if c:
            questions.append("【%s】%s" % (a, c))
    for a in sorted(responses):
        c = extract_subsection(responses[a], "二、分歧点")
        if c:
            questions.append("【%s 分歧待决】%s" % (a, c))

    block = "\n\n".join([
        "### 共识事项\n\n%s" % join(consensus, "（交叉回应中未记录明确共识）"),
        "### 分歧事项\n\n%s" % join(dissent, "（交叉回应中未记录分歧）"),
        "### 候选方案\n\n%s" % join(plans, "（无候选方案）"),
        "### 各方案支持者\n\n%s" % "\n".join(support),
        "### 主要依据\n\n%s" % join(basis, "（未归集到依据）"),
        "### 风险与代价\n\n%s" % join(risks, "（未归集到风险）"),
        "### 各 Agent 推荐\n\n%s" % "\n".join(recs),
        "### 待 Rainier 选择的问题\n\n%s" % join(questions, "（无待决问题）"),
    ])
    return block, proposals, responses


def run_new_candidate(
    state_path, state, actor, platform_id, session_id, new_id, discussion_arg, base,
):
    """登记候选决策 ID 并组装结构化决策包，推进 cross_response → candidate_decision。"""
    try:
        validate_coordinator_execution(state, actor or "", platform_id or "", session_id or "")
    except WorkflowError as error:
        print("协调者身份门禁拒绝：%s" % error, file=sys.stderr)
        return EXIT_PRECOND
    new_id = new_id.strip()
    # 幂等：该候选决策已登记
    if new_id in state.get("candidate_decision_ids", []):
        print("候选决策 %s 已登记，幂等空操作。" % new_id)
        return EXIT_OK

    stage = state["stage"]
    if stage not in ("cross_response", "candidate_decision"):
        print("阶段 %s 不允许登记候选决策（需 cross_response 或 candidate_decision）。" % stage, file=sys.stderr)
        return EXIT_PRECOND

    discussion = discussion_arg
    if discussion is None:
        discussion = find_discussion_md(base)
    if discussion is None or not os.path.isfile(discussion):
        print("未找到主讨论文档（需要顶层 *讨论文档*.md，或用 --discussion 指定）。", file=sys.stderr)
        return EXIT_ERR

    md_text = read_text(discussion)

    # 合并 responses/ 回应文件到「三、交叉回应」区块（幂等：区块已完整则跳过），
    # 使 Markdown 权威记录完整后再组装决策包
    md_text = merge_responses_into_doc(md_text, base, state["expected_participants"])

    # 组装结构化决策包并写入「四、结构化决策包」（整体替换，幂等）
    package, proposals, responses = assemble_decision_package(md_text)
    md_text = replace_section(md_text, "## 四、结构化决策包", package)

    # 登记候选决策到「五、候选决策 ID 登记」（幂等：该区块已含新 ID 则不追加）
    reg_heading = "## 五、候选决策 ID 登记"
    reg_section = extract_section(md_text, reg_heading)
    if new_id in reg_section:
        md_text_updated = md_text
    else:
        reg_line = "- 候选决策: %s（登记于 %s，来源=交叉回应组装）" % (new_id, local_hm())
        md_text_updated = append_to_section(md_text, reg_heading, reg_line)

    # 审计行（幂等：同内容不重复追加）
    new_rev = state["revision"] + 1
    if stage == "cross_response":
        audit = "[%s] 事件=阶段推进 | 参与者=%s | 来源=state.json | 处置=cross_response → candidate_decision, 登记候选决策 %s | revision=%d" % (
            local_hm(), actor, new_id, new_rev)
    else:
        audit = "[%s] 事件=候选决策登记 | 参与者=%s | 来源=state.json | 处置=登记候选决策 %s | revision=%d" % (
            local_hm(), actor, new_id, new_rev)
    if audit not in md_text_updated:
        md_text_updated = append_audit_line(md_text_updated, audit)

    # 受控编排：先写主文档，再更新 state.json
    try:
        write_text(discussion, md_text_updated)
    except OSError as e:
        print("主讨论文档写入失败：%s" % e, file=sys.stderr)
        return EXIT_ERR

    # 更新 state.json：登记候选 + 推进阶段（cross_response → candidate_decision）+ 协调者续期
    ids = list(state.get("candidate_decision_ids", []))
    if new_id not in ids:
        ids.append(new_id)
        state["candidate_decision_ids"] = ids
    if stage == "cross_response":
        state["stage"] = "candidate_decision"
    state["revision"] = new_rev
    if actor == state.get("coordinator"):
        state["coordination_lease_until"] = (
            datetime.now(tz_cn()) + timedelta(seconds=int(state["coordinator_timeout"]))
        ).isoformat(timespec="seconds")
    try:
        write_state(state_path, state)
    except OSError as e:
        print("state.json 写入失败：%s" % e, file=sys.stderr)
        print("恢复入口：主文档决策包与登记已写入，幂等重跑本脚本可完成登记与推进。", file=sys.stderr)
        return EXIT_ERR

    print("候选决策 %s 已登记（阶段=%s，revision=%d），结构化决策包已写入主文档。" % (
        new_id, state["stage"], state["revision"]))
    missing_resp = [a for a in state["expected_participants"] if a not in responses]
    if missing_resp:
        print("注意：以下参与者的交叉回应缺失（主文档区块与 responses/ 均无），决策包未纳入其回应：%s" % missing_resp, file=sys.stderr)
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="决策确认闸门：将候选决策固化为正式决策（§13 混合确认）")
    parser.add_argument("--state", default=None, help="state.json 路径（默认优先 .multiagent/state.json）")
    parser.add_argument("--actor", required=True, help="记录者（执行固化的协调者身份）")
    parser.add_argument("--platform-id", required=True, help="实际执行平台标识，必须匹配协调者绑定")
    parser.add_argument("--session-id", required=True, help="实际执行会话标识，必须精确匹配协调者绑定")
    parser.add_argument("--new-candidate", default=None,
                        help="登记新候选决策 ID 并组装结构化决策包，推进 cross_response → candidate_decision")
    parser.add_argument("--candidate-id", default=None,
                        help="候选决策 ID（须在 candidate_decision_ids 中；确认固化模式）")
    parser.add_argument("--confirm-text", default=None, help="Rainier 的确认原文（确认固化模式必需）")
    parser.add_argument("--second-confirm", default=None, help="二次确认原文（多项/修改/歧义时必需）")
    parser.add_argument("--discussion", default=None, help="主讨论文档路径（缺省自动发现）")
    parser.add_argument("--no-open", action="store_true", help="确认后生成正式 Word，但不调用系统程序打开")
    args = parser.parse_args()

    # 模式互斥：登记候选决策（--new-candidate）与确认固化（--candidate-id）二选一
    if bool(args.new_candidate) == bool(args.candidate_id):
        print("必须且只能提供 --new-candidate 或 --candidate-id 之一。", file=sys.stderr)
        return EXIT_PRECOND
    if args.candidate_id and not args.confirm_text:
        print("确认固化模式必须提供 --confirm-text。", file=sys.stderr)
        return EXIT_PRECOND

    state_path = os.path.abspath(args.state) if args.state else _default_state_path(os.getcwd())
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
    try:
        validate_coordinator_execution(state, actor, args.platform_id, args.session_id)
    except WorkflowError as error:
        print("协调者身份门禁拒绝：%s" % error, file=sys.stderr)
        return EXIT_PRECOND
    if actor != state["coordinator"]:
        print("仅当前协调者 %s 可以固化决策；%s 无权执行。" % (state["coordinator"], actor), file=sys.stderr)
        return EXIT_PRECOND

    base = _workspace_root_for_state(state_path)
    if args.new_candidate:
        return run_new_candidate(
            state_path, state, actor, args.platform_id, args.session_id,
            args.new_candidate, args.discussion, base,
        )

    candidate_id = args.candidate_id.strip()
    if candidate_id not in state.get("candidate_decision_ids", []):
        print("候选决策 %s 不在 candidate_decision_ids 中：%s" % (
            candidate_id, state.get("candidate_decision_ids", [])), file=sys.stderr)
        return EXIT_PRECOND

    # 幂等：该候选决策已确认 → 空操作
    if candidate_id in state.get("confirmed_decision_ids", []):
        if state["stage"] == "confirmed_decision":
            stop_error = stop_receipts_gate_error(state, base)
            if stop_error:
                print(stop_error, file=sys.stderr)
                return EXIT_PRECOND
            print("候选决策 %s 已确认；继续重试正式 Word 交付。" % candidate_id)
            return do_export(
                state_path,
                state,
                actor,
                base,
                args.discussion,
                None,
                False,
                open_after=not args.no_open,
                trusted_execution=True,
            )[0]
        if state["stage"] == "delivered":
            print("候选决策 %s 已确认且已交付，幂等空操作。" % candidate_id)
            return EXIT_OK
        print("候选决策 %s 已在确认列表中，但阶段为 %s；拒绝不一致状态。" % (
            candidate_id, state["stage"]), file=sys.stderr)
        return EXIT_PRECOND

    stage = state["stage"]
    if stage not in {"human_review", "user_confirmation"}:
        print("阶段 %s 不允许固化决策（需要 human_review）。" % stage, file=sys.stderr)
        return EXIT_PRECOND
    gate_error = confirmation_gate_error(state, base)
    if gate_error:
        print(gate_error, file=sys.stderr)
        return EXIT_PRECOND

    discussion = args.discussion
    if discussion is None:
        discussion = find_discussion_md(base)
    if discussion is None or not os.path.isfile(discussion):
        print("未找到主讨论文档（需要顶层 *讨论文档*.md，或用 --discussion 指定）。", file=sys.stderr)
        return EXIT_ERR
    md_text = read_text(discussion)

    # 分析确认：拒绝 → 拒绝固化；单项且无歧义 → 直接固化；否则回显 + 二次确认
    kind, reason = analyze_confirmation(state, candidate_id, args.confirm_text)
    if kind == "rejected":
        print("确认被拒绝：%s；候选决策 %s 未固化。" % (reason, candidate_id), file=sys.stderr)
        return EXIT_PRECOND
    second = args.second_confirm
    if kind == "echo_required":
        package = extract_decision_package(md_text)
        echo_full(candidate_id, package, reason)
        if second is None:
            if sys.stdin.isatty():
                try:
                    second = input("请 Rainier 再次输入确认原文（空回车 = 拒绝）: ").strip()
                except EOFError:
                    second = ""
            else:
                print("确认被拒绝：需二次确认，请提供 --second-confirm 参数；本次已回显决策 ID 与完整内容。",
                      file=sys.stderr)
                return EXIT_PRECOND
        if not second:
            print("未收到二次确认，确认被拒绝，候选决策 %s 未固化。" % candidate_id, file=sys.stderr)
            return EXIT_PRECOND
        # 二次确认路径同样复检否定：拒绝/否决语义永不固化
        if _has_rejection(second):
            print("二次确认含拒绝/否决语义，确认被拒绝，候选决策 %s 未固化。" % candidate_id, file=sys.stderr)
            return EXIT_PRECOND
        if second == args.confirm_text:
            # 二次确认与首次确认原文一致，视为明确的再次确认
            pass
        elif not any(w in second for w in AFFIRMATIVE_SIGNALS):
            print("二次确认未包含明确肯定语，确认被拒绝，候选决策 %s 未固化。" % candidate_id, file=sys.stderr)
            return EXIT_PRECOND

    # 预计算新修订号与审计/确认记录（固化写入完成后 revision 恰好落在 final_rev）
    base_rev = state["revision"]
    final_rev = base_rev + 1

    record = build_confirm_record(candidate_id, args.confirm_text, actor, final_rev)
    confirm_heading = "## 六、确认固化记录"

    # 幂等：若该候选决策的确认记录已存在于主文档，则跳过主文档追加
    if ("候选决策 ID: %s" % candidate_id) in md_text:
        md_text_updated = md_text
    else:
        md_text_updated = append_to_section(md_text, confirm_heading, record)

    audits = []
    rev_now = base_rev
    rev_now += 1
    audits.append("[%s] 事件=阶段推进 | 参与者=%s | 来源=state.json | 处置=user_confirmation → confirmed_decision | revision=%d" % (
        local_hm(), actor, rev_now))
    audits.append("[%s] 事件=决策确认 | 参与者=%s | 来源=state.json | 处置=候选决策 %s 固化为正式决策 | revision=%d" % (
        local_hm(), actor, candidate_id, rev_now))

    for line in audits:
        md_text_updated = append_audit_line(md_text_updated, line)

    # Confirmation text may have required an interactive second confirmation.
    # Re-read all stop artifacts immediately before the first durable write.
    gate_error = confirmation_gate_error(state, base)
    if gate_error:
        print(gate_error, file=sys.stderr)
        return EXIT_PRECOND

    # 受控编排：先写主讨论文档，再更新 state.json
    try:
        write_text(discussion, md_text_updated)
    except OSError as e:
        print("主讨论文档写入失败：%s" % e, file=sys.stderr)
        return EXIT_ERR

    # 更新 state.json：登记已确认 ID + 逐级推进阶段。每次成功写入 revision 恰好 +1，
    # 与 merge 逐级写入一致（state-schema §四.4）。已确认 ID 仅在最终写入时登记，
    # 避免中途失败时幂等重跑被提前短路。
    def _renew_lease():
        if actor == state.get("coordinator"):
            state["coordination_lease_until"] = (
                datetime.now(tz_cn()) + timedelta(seconds=int(state["coordinator_timeout"]))
            ).isoformat(timespec="seconds")

    def _add_confirmed():
        confirmed = list(state.get("confirmed_decision_ids", []))
        if candidate_id not in confirmed:
            confirmed.append(candidate_id)
            state["confirmed_decision_ids"] = confirmed

    # Publish the confirmed conclusion first. The coordinator orchestrator
    # issues stop instructions only after this durable finalization marker.
    modern = _modern_lifecycle(state)
    state["stage"] = "finalizing" if modern else "confirmed_decision"
    state["revision"] += 1
    _add_confirmed()
    _renew_lease()
    try:
        EventTransaction(base).commit(
            event_type="decision_confirmed",
            event_payload={
                "candidate_id": candidate_id,
                "stage_after": state["stage"],
                "discussion_path": Path(discussion).name,
                "discussion_sha256": sha256_file(discussion),
            },
            artifacts={},
            receipts={},
            expected_revision=base_rev,
            state_update=state,
        )
    except Exception as e:
        print("state.json / 事件日志写入失败：%s" % e, file=sys.stderr)
        print("恢复入口：主文档已记录确认（含候选决策 ID），幂等重跑本脚本可完成固化。", file=sys.stderr)
        return EXIT_ERR

    print("候选决策 %s 已固化（阶段=%s，revision=%d），记录者=%s。" % (
        candidate_id, state["stage"], state["revision"], actor))
    if modern:
        print("最终结论已发布；请运行一次协调器门禁，待参与者停止证明完成后再生成正式 Word。")
    else:
        print("候选决策已确认；继续生成正式 Word。")
        return do_export(
            state_path,
            state,
            actor,
            base,
            discussion,
            None,
            False,
            open_after=not args.no_open,
            trusted_execution=True,
        )[0]
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
