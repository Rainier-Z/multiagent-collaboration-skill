#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_docx.py —— 生成与交付正式 Word 快照

职责：
  1. 将主讨论 Markdown 文档转换为 Word（python-docx），中文字体微软雅黑，
     覆盖标题 / 表格 / 列表 / 代码块 / 引用 / 加粗等基本排版。
  2. Markdown 始终是唯一权威内容源；Word 是只读交付快照，不反向覆盖 Markdown。
  3. 将正式 Word 放在工作区根目录，清单和审计元数据放在 .multiagent/deliverables/。
  4. 只有正式 Word 已成功交给系统默认程序打开，才推进 confirmed_decision → delivered。
     打开失败或 --no-open 时保留 confirmed_decision 并记录失败。
  5. 幂等：清单中已有同源快照时复用；--force 可强制重新生成。

用法（在运行时项目目录执行；脚本解析 state.json 所在目录）：
  python scripts/export_docx.py --state .multiagent/state.json --actor <协调者身份> --platform-id <平台> --session-id <会话> [--discussion <主讨论文档>] [--out <输出docx路径>] [--force]

依赖：python-docx（第三方库，允许使用）。未安装时给出安装提示并以非 0 退出，不改变任何状态。

退出码：
  0 成功（快照生成 / 监测停止 / 幂等空操作）
  2 state.json 非法或与 schema 不一致
  3 前置条件不满足（阶段未确认 / actor 非协调者）
  1 其他 I/O 或内部错误（含 python-docx 未安装 / 转换失败）
"""

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from workflow_core import (
    WorkflowError, atomic_write_json, atomic_write_text,
    sha256_file as core_sha256_file, validate_coordinator_execution,
)


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
    idx = md_text.find(heading)
    if idx == -1:
        return md_text.rstrip("\n") + "\n\n" + heading + "\n\n" + block.rstrip("\n") + "\n"
    nxt = idx + len(heading)
    m = re.search(r"^#{1,2} ", md_text[nxt:], flags=re.M)
    insert_at = nxt + m.start() if m else len(md_text)
    head = md_text[:insert_at]
    if not head.endswith("\n"):
        head += "\n"
    if not head.endswith("\n\n"):
        head += "\n"
    return head + block.rstrip("\n") + "\n\n" + md_text[insert_at:]


def sha256_file(path):
    """对文件原始字节计算 sha256。

    注：read_text/write_text 在 Windows 走通用换行模式（\n <-> \r\n），
    内存字符串哈希与落盘文件字节哈希不一致；为保证「记录哈希 == 落盘 md」，
    对落盘 md 一律按原始字节哈希。
    """
    return core_sha256_file(path)


# ---------- Markdown → docx ----------

def set_ea_font(run, name):
    """设置 run 的西文与东亚字体（东亚设为微软雅黑）。"""
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)


def add_rich(par, text):
    """处理行内 **加粗** 与 `行内代码`，其余按普通文本。"""
    tokens = re.split(r"(\*\*.*?\*\*|`[^`]*`)", text)
    for t in tokens:
        if not t:
            continue
        if t.startswith("**") and t.endswith("**") and len(t) >= 4:
            r = par.add_run(t[2:-2])
            r.bold = True
            set_ea_font(r, "微软雅黑")
        elif t.startswith("`") and t.endswith("`") and len(t) >= 2:
            r = par.add_run(t[1:-1])
            r.font.name = "Consolas"
            set_ea_font(r, "微软雅黑")
            r.font.size = Pt(9)
        else:
            r = par.add_run(t)
            set_ea_font(r, "微软雅黑")


def parse_table_row(line):
    """解析一行表格 '| a | b |' → ['a', 'b']。"""
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def md_to_docx(md_text, doc):
    lines = md_text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].rstrip()

        # 围栏代码块
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].rstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过闭围栏
            p = doc.add_paragraph()
            r = p.add_run("\n".join(buf))
            r.font.name = "Consolas"
            set_ea_font(r, "微软雅黑")
            r.font.size = Pt(9)
            p.paragraph_format.left_indent = Cm(0.5)
            continue

        # 表格（表头 + 分隔行 |---| ）
        if stripped.startswith("|") and i + 1 < n and re.match(
                r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1].rstrip()):
            header = parse_table_row(stripped)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(parse_table_row(lines[i]))
                i += 1
            table = doc.add_table(rows=1, cols=max(len(header), 1))
            table.style = "Table Grid"
            for j in range(len(header)):
                cell = table.rows[0].cells[j]
                p = cell.paragraphs[0]
                add_rich(p, header[j])
            for row in rows:
                cells = table.add_row().cells
                for j in range(len(header)):
                    p = cells[j].paragraphs[0]
                    add_rich(p, row[j] if j < len(row) else "")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = min(len(m.group(1)), 9)
            h = doc.add_heading(level=level)
            add_rich(h, m.group(2))
            for r in h.runs:
                set_ea_font(r, "微软雅黑")
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*-{3,}\s*$", stripped):
            p = doc.add_paragraph()
            r = p.add_run("—" * 24)
            set_ea_font(r, "微软雅黑")
            p.alignment = 1  # 居中
            i += 1
            continue

        # 无序列表（缩进层级以前导空格近似）
        if re.match(r"^\s*[-*+]\s+", stripped):
            indent = len(stripped) - len(stripped.lstrip())
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5 * (indent + 1))
            p.style = doc.styles["List Bullet"]
            add_rich(p, re.sub(r"^\s*[-*+]\s+", "", stripped))
            i += 1
            continue

        # 有序列表
        m2 = re.match(r"^\s*\d+[.、]\s+(.*)", stripped)
        if m2:
            p = doc.add_paragraph()
            p.style = doc.styles["List Number"]
            add_rich(p, m2.group(1))
            i += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            add_rich(p, re.sub(r"^>\s?", "", stripped))
            for r in p.runs:
                r.italic = True
            i += 1
            continue

        # 空行
        if stripped == "":
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        add_rich(p, stripped)
        i += 1


def convert_md(md_path, docx_path):
    """转换主讨论文档为 docx；失败抛异常，不影响 Markdown 权威性。"""
    from docx import Document
    from docx.shared import Cm, Pt
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    globals()["qn"] = qn
    globals()["OxmlElement"] = OxmlElement
    globals()["Cm"] = Cm
    globals()["Pt"] = Pt

    md_text = read_text(md_path)
    doc = Document()
    # 默认样式：字体微软雅黑（含东亚字体）
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), "微软雅黑")

    md_to_docx(md_text, doc)
    doc.save(docx_path)
    return docx_path


def open_document(path, opener=None):
    """用系统默认程序打开 Word；生成成功与前台打开结果分别记录。"""
    target = os.fspath(path)
    try:
        if opener is not None:
            return opener(path) is True
        elif os.name == "nt":
            os.startfile(os.path.abspath(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", os.path.abspath(target)])
        else:
            subprocess.Popen(["xdg-open", os.path.abspath(target)])
        return True
    except Exception:
        return False


def _safe_filename(value):
    return re.sub(r'[<>:"/\\|?*]+', "_", str(value)).strip(" .") or "discussion"


def create_candidate_delivery(workspace, state, discussion, open_after=True, opener=None):
    """Create a review-only Word at the workspace root and internal Markdown/manifest."""
    workspace = os.path.abspath(os.fspath(workspace))
    discussion = os.path.abspath(os.fspath(discussion))
    source_text = read_text(discussion)
    source_hash = sha256_file(discussion)
    candidate_id = (state.get("candidate_decision_ids") or ["C-0001"])[0]
    deliverables_dir = os.path.join(workspace, ".multiagent", "deliverables")
    os.makedirs(deliverables_dir, exist_ok=True)
    candidate_markdown = os.path.join(deliverables_dir, "candidate.md")
    candidate_docx = os.path.join(workspace, "候选决策.docx")
    review_text = (
        "# 候选稿（待 Rainier 确认）\n\n"
        "> 状态：未确认。此 Markdown/Word 仅供审阅，不构成正式决策。\n\n"
        "---\n\n%s" % source_text.lstrip()
    )
    write_text(candidate_markdown, review_text)
    try:
        convert_md(candidate_markdown, candidate_docx)
    except ImportError:
        raise RuntimeError("未安装 python-docx，无法生成候选 Word")

    attempted_at = now_iso() if open_after else None
    opened = bool(open_document(candidate_docx, opener=opener)) if open_after else False
    record = {
        "path": os.path.relpath(candidate_docx, workspace).replace(os.sep, "/"),
        "sha256": sha256_file(candidate_docx),
        "markdown_path": os.path.relpath(candidate_markdown, workspace).replace(os.sep, "/"),
        "markdown_sha256": sha256_file(candidate_markdown),
        "opened": opened,
        "opened_at": attempted_at if opened else None,
        "open_attempted_at": attempted_at,
    }
    if open_after and not opened:
        record["open_error"] = "系统程序未能打开候选 Word；文件已保留，可手动打开。"
    manifest_path = os.path.join(deliverables_dir, "candidate-manifest.json")
    write_text(manifest_path, json.dumps({
        "discussion_id": state.get("discussion_id"),
        "candidate_id": candidate_id,
        "source_markdown": os.path.relpath(discussion, workspace).replace(os.sep, "/"),
        "source_sha256": source_hash,
        "delivery": record,
    }, ensure_ascii=False, indent=2) + "\n")
    return record


def _workspace_root_for_state(state_path):
    parent = os.path.dirname(os.path.abspath(state_path))
    return os.path.dirname(parent) if os.path.basename(parent) == ".multiagent" else parent


def _default_state_path(workspace):
    compact = os.path.join(workspace, ".multiagent", "state.json")
    legacy = os.path.join(workspace, "state.json")
    return compact if os.path.isfile(compact) else legacy


# ---------- 主流程 ----------

def load_manifest(manifest_path):
    try:
        data = json.loads(read_text(manifest_path))
        if isinstance(data, dict):
            return data
    except (ValueError, OSError):
        pass
    return {"snapshots": []}


def do_export(
    state_path, state, actor, base, discussion_arg, out_arg, force,
    open_after=True, opener=None, *, platform_id=None, session_id=None,
    trusted_execution=False,
):
    """生成 Word 只读快照并记录到 deliverables，推进 confirmed_decision → delivered。

    幂等设计：先把交付记录 + 审计行幂等追加进主文档，再对「最终 md 落盘文本」计算
    source_sha256，据此生成 docx 并写 manifest，使 manifest.source_sha256 == 落盘 md 哈希；
    重跑命中同源哈希即真幂等（不再每次生成新快照）。

    返回 (exit_code, state)；state 在内存中同步更新，只有推进阶段时写盘。
    """
    if not trusted_execution:
        try:
            validate_coordinator_execution(state, actor or "", platform_id or "", session_id or "")
        except WorkflowError as error:
            print("协调者身份门禁拒绝：%s" % error, file=sys.stderr)
            return EXIT_PRECOND, state

    stage = state["stage"]
    if stage not in ("confirmed_decision", "delivered"):
        print("阶段 %s 未确认正式决策，不能生成 Word 交付快照。" % stage, file=sys.stderr)
        return EXIT_PRECOND, state

    discussion = discussion_arg
    if discussion is None:
        discussion = find_discussion_md(base)
    if discussion is None or not os.path.isfile(discussion):
        print("未找到主讨论文档（需要顶层 *讨论文档*.md，或用 --discussion 指定）。", file=sys.stderr)
        return EXIT_ERR, state

    md_on_disk = read_text(discussion)
    on_disk_sha = sha256_file(discussion)  # 落盘 md 的原始字节哈希（记录/幂等键）
    rel_disc = os.path.relpath(discussion, base)
    deliverables_dir = os.path.join(base, ".multiagent", "deliverables")
    os.makedirs(deliverables_dir, exist_ok=True)
    manifest_path = os.path.join(deliverables_dir, "word-snapshots.json")
    manifest = load_manifest(manifest_path)
    snapshots = manifest.get("snapshots", [])

    # 幂等预检：以「当前落盘 md 哈希」为键（与 manifest 记录一致）
    match = None
    for s in snapshots:
        if s.get("source_sha256") == on_disk_sha and s.get("docx"):
            match = s
            break

    if not force and match and os.path.isfile(os.path.join(base, match["docx"])):
        if stage == "delivered":
            # Existing formal Word remains reusable and can still be opened on request.
            docx_path = os.path.join(base, match["docx"])
            opened = bool(open_document(docx_path, opener=opener)) if open_after else False
            print("正式 Word 已存在：%s（源 sha256 一致）。" % match["docx"])
            if open_after and not opened:
                print("系统程序未能打开正式 Word；文件已保留，可手动打开。", file=sys.stderr)
            if not force:
                state["formal_delivery"] = {
                    "path": os.path.relpath(docx_path, base).replace(os.sep, "/"),
                    "sha256": sha256_file(docx_path),
                    "source_markdown": rel_disc.replace(os.sep, "/"),
                    "source_sha256": on_disk_sha,
                    "opened": opened,
                    "opened_at": now_iso() if opened else None,
                    "open_attempted_at": now_iso() if open_after else None,
                }
                if not opened:
                    state["formal_delivery"]["open_error"] = (
                        "未请求打开正式 Word。" if not open_after else "系统程序未能打开正式 Word；文件已保留，可手动打开。"
                    )
                    if stage == "confirmed_decision":
                        state["revision"] += 1
                    try:
                        write_state(state_path, state)
                    except OSError as e:
                        print("state.json 写入失败：%s" % e, file=sys.stderr)
                        return EXIT_ERR, state
                    return EXIT_ERR, state
                if stage == "confirmed_decision":
                    state["stage"] = "delivered"
                    state["revision"] += 1
                    try:
                        write_state(state_path, state)
                    except OSError as e:
                        print("state.json 写入失败：%s" % e, file=sys.stderr)
                        return EXIT_ERR, state
                return EXIT_OK, state
        # 上次流程中途失败（状态未推进）：复用已有快照文件，补齐主文档记录与状态
        docx_path = os.path.join(base, match["docx"])
        snapshot_rel = match["docx"]
        generated_at = match.get("generated_at", now_iso())
        final_sha = on_disk_sha
        print("发现未完成的同源快照：%s，复用并补齐主文档记录与状态。" % snapshot_rel)
    else:
        # 生成新快照（python-docx）。失败不影响 Markdown 正式决策有效性与状态。
        if out_arg:
            docx_path = os.path.abspath(out_arg)
        else:
            docx_path = os.path.join(base, "最终决策.docx")
        snapshot_rel = os.path.relpath(docx_path, base)

        # 1) 幂等追加交付记录 + 审计行（记录内嵌追加前的源哈希；以「源 Markdown 路径」去重）
        heading = "## 七、正式 Word 交付"
        record_marker = "- 源 Markdown 路径: %s" % rel_disc
        if record_marker not in md_on_disk:
            new_rev = state["revision"] + 1 if stage == "confirmed_decision" else state["revision"]
            word_record = (
                "- 源 Markdown 路径: %s\n"
                "- 源文件哈希: %s\n"
                "- 生成时间: %s (Asia/Shanghai)\n"
                "- 快照路径: %s\n"
            ) % (rel_disc, on_disk_sha, local_hm(), snapshot_rel)
            audit = "[%s] 事件=Word生成 | 参与者=%s | 来源=state.json | 处置=源=%s, sha256=%s | revision=%d" % (
                local_hm(), actor, rel_disc, on_disk_sha, new_rev)
            md_text = append_to_section(md_on_disk, heading, word_record)
            if audit not in md_text:
                md_text = append_audit_line(md_text, audit)
            try:
                write_text(discussion, md_text)
            except OSError as e:
                print("主讨论文档记录写入失败：%s" % e, file=sys.stderr)
                return EXIT_ERR, state

        # 2) 对「最终 md 落盘文件」计算 source_sha256（记录哈希 == 落盘 md == docx 来源）
        final_sha = sha256_file(discussion)

        # 3) 生成 docx（基于最终 md 落盘内容）
        try:
            convert_md(discussion, docx_path)
        except ImportError:
            print("未安装 python-docx，无法生成 Word 快照。", file=sys.stderr)
            print("安装命令：pip install python-docx", file=sys.stderr)
            print("提示：Markdown 仍是唯一权威内容源，本次失败不影响其有效性。", file=sys.stderr)
            return EXIT_ERR, state
        except Exception as e:
            print("Markdown → Word 转换失败：%s" % e, file=sys.stderr)
            print("提示：Markdown 仍是唯一权威内容源，本次失败不影响其有效性。", file=sys.stderr)
            return EXIT_ERR, state
        generated_at = now_iso()
        snapshots.append({
            "generated_at": generated_at,
            "source_markdown": rel_disc,
            "source_sha256": final_sha,
            "docx": snapshot_rel,
        })

    try:
        write_text(manifest_path, json.dumps({
            "discussion_id": state["discussion_id"],
            "snapshots": snapshots,
        }, ensure_ascii=False, indent=2))
    except OSError as e:
        print("快照记录写入失败：%s" % e, file=sys.stderr)
        return EXIT_ERR, state

    attempted_at = now_iso() if open_after else None
    opened = bool(open_document(docx_path, opener=opener)) if open_after else False
    state["formal_delivery"] = {
        "path": os.path.relpath(docx_path, base).replace(os.sep, "/"),
        "sha256": sha256_file(docx_path),
        "source_markdown": rel_disc.replace(os.sep, "/"),
        "source_sha256": final_sha,
        "opened": opened,
        "opened_at": attempted_at if opened else None,
        "open_attempted_at": attempted_at,
    }
    if open_after and not opened:
        state["formal_delivery"]["open_error"] = "系统程序未能打开正式 Word；文件已保留，可手动打开。"

    # 只有前台打开成功时才允许进入 delivered。no-open/失败都只记录尝试。
    if not opened:
        state["formal_delivery"]["open_error"] = (
            "未请求打开正式 Word。" if not open_after else "系统程序未能打开正式 Word；文件已保留，可手动打开。"
        )
        if stage == "confirmed_decision":
            state["revision"] += 1
        try:
            write_state(state_path, state)
        except OSError as e:
            print("state.json 写入失败：%s" % e, file=sys.stderr)
            return EXIT_ERR, state
        print(state["formal_delivery"]["open_error"], file=sys.stderr)
        return EXIT_ERR, state

    # 状态推进：confirmed_decision → delivered（revision +1）；终态重新打开不改阶段
    if stage == "confirmed_decision":
        state["stage"] = "delivered"
        state["revision"] += 1
        state["coordination_lease_until"] = (
            datetime.now(tz_cn()) + timedelta(seconds=int(state["coordinator_timeout"]))
        ).isoformat(timespec="seconds")
        try:
            write_state(state_path, state)
        except OSError as e:
            print("state.json 写入失败：%s" % e, file=sys.stderr)
            print("恢复入口：docx 与 deliverables 记录已生成，幂等重跑本脚本可推进状态。", file=sys.stderr)
            return EXIT_ERR, state
        print("Word 快照已生成并推进到 delivered：%s（revision=%d）" % (docx_path, state["revision"]))
    else:
        print("Word 快照已（重新）生成：%s（状态保持 %s，不改变 revision）" % (docx_path, stage))
    print("源 Markdown：%s | sha256: %s | 生成时间：%s" % (rel_disc, final_sha, generated_at))
    print("快照记录：%s" % manifest_path)
    return EXIT_OK, state


def request_stop(state_path, state, actor, *, platform_id=None, session_id=None, trusted_execution=False):
    """Request stop instructions without bypassing participant receipts."""
    if not trusted_execution:
        try:
            validate_coordinator_execution(state, actor or "", platform_id or "", session_id or "")
        except WorkflowError as error:
            print("协调者身份门禁拒绝：%s" % error, file=sys.stderr)
            return EXIT_PRECOND
    if state["stage"] != "delivered":
        print("--stop-monitoring 需在 delivered 阶段（当前 %s）。" % state["stage"], file=sys.stderr)
        return EXIT_PRECOND
    automation = dict(state.get("automation") or {})
    if automation.get("stop_requested"):
        print("stop 请求已登记；等待编排器核验全部 stop completed 回执（阶段保持 delivered）。")
        return EXIT_OK
    automation["stop_requested"] = True
    automation["stop_requested_at"] = now_iso()
    automation["stop_requested_by"] = actor
    state["automation"] = automation
    state["revision"] += 1
    try:
        write_state(state_path, state)
    except OSError as e:
        print("state.json 写入失败：%s" % e, file=sys.stderr)
        return EXIT_ERR
    print("stop 请求已登记（revision=%d）；阶段保持 delivered，等待全部 stop completed 回执。" %
          state["revision"])
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="生成主讨论文档的 Word 只读快照并记录哈希")
    parser.add_argument("--state", default=None, help="state.json 路径（默认优先 .multiagent/state.json）")
    parser.add_argument("--actor", required=True, help="state.json 中登记的 coordinator agent_id")
    parser.add_argument("--platform-id", required=True, help="实际执行平台标识，必须匹配协调者绑定")
    parser.add_argument("--session-id", required=True, help="实际执行会话标识，必须精确匹配协调者绑定")
    parser.add_argument("--discussion", default=None, help="主讨论文档路径（缺省自动发现）")
    parser.add_argument("--out", default=None, help="输出 docx 路径（缺省讨论根目录下正式决策.docx）")
    parser.add_argument("--force", action="store_true", help="同源哈希快照已存在时强制重新生成")
    parser.add_argument("--no-open", action="store_true", help="生成 Word 但不调用系统程序打开")
    parser.add_argument("--stop-monitoring", action="store_true",
                        help="Word 成功后请求编排器下发 stop；本命令不能直接推进 monitoring_stopped")
    args = parser.parse_args()

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
        print("仅当前协调者 %s 可以执行交付/停止操作；%s 无权执行。" % (state["coordinator"], actor), file=sys.stderr)
        return EXIT_PRECOND

    base = _workspace_root_for_state(state_path)
    stage = state["stage"]

    if args.stop_monitoring:
        print("停止监测由候选 Word 打开后的编排器流程负责；请运行 orchestrate_discussion.py。", file=sys.stderr)
        return EXIT_PRECOND

    # 普通 Word 快照导出
    rc, _state = do_export(
        state_path, state, actor, base, args.discussion, args.out, args.force,
        not args.no_open, platform_id=args.platform_id,
        session_id=args.session_id, trusted_execution=True,
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
