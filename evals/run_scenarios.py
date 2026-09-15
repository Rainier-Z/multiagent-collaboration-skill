#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_scenarios.py —— 执行 v2 编排、停止门禁、Word 生命周期与旧入口退役检查。

旧的 30 个情景依赖已退役的 submit/merge/claim 独立写状态入口，不再作为 v2 验收。
当前只运行 test_orchestration、test_end_to_end_v2 与三个旧入口不修改状态的检查。

运行：
    python evals/run_scenarios.py
退出码：0 全部 PASS；1 存在 FAIL（供协调者运行验证）。
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from multiprocessing import Barrier, Process, Queue
from pathlib import Path
from queue import Empty

# ---------- 路径常量 ----------
_EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_EVALS_DIR)
SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
ASSETS_DIR = os.path.join(_REPO_ROOT, "assets")
PYTHON = sys.executable

# Runtime 场景直接调用协调端与参与端的公开接口；既有 1--16 场景仍以
# subprocess 验证原有 CLI。这里显式收敛导入路径，避免测试解释器的 cwd
# 决定接口是否可见。
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from orchestrate_discussion import FakeWakeAdapter, UnavailableWakeAdapter, orchestrate_once
from participant_runtime.participant_runner import run_instruction
from participant_runtime.protocol import (
    Instruction,
    publish_runtime,
    verify_manifest,
    write_instruction,
)
from workflow_core import E_HASH, E_OUTPUT_FORMAT, E_PATH_SCOPE, E_RETRY_EXHAUSTED, WorkflowError

_TZ8 = timezone(timedelta(hours=8))  # Asia/Shanghai 固定 +08:00（无夏令时）


def _script(name):
    """返回 scripts/ 下脚本的绝对路径。"""
    return os.path.join(SCRIPTS_DIR, name)


def _run_script(name, args, cwd=None, timeout=120, env=None):
    """运行脚本，返回 (退出码, stdout, stderr)。"""
    cmd = [PYTHON, _script(name)] + [str(a) for a in args]
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd, timeout=timeout, env=env)
    return r.returncode, r.stdout, r.stderr


def _state_path(disc_dir):
    """state.json 绝对路径。"""
    return os.path.join(disc_dir, "state.json")


def _load_state(disc_dir):
    """读取 state.json 为 dict。"""
    with open(_state_path(disc_dir), "r", encoding="utf-8") as f:
        return json.load(f)


def _write_state(disc_dir, state):
    """覆盖写入 state.json（测试夹具专用，非受控编排动作）。"""
    with open(_state_path(disc_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _now_plus8():
    """当前时间，Asia/Shanghai +08:00。"""
    return datetime.now(_TZ8)


def _init_discussion(disc_dir, coordinator, participants, **opts):
    """调用 init_discussion.py，返回 (退出码, stdout, stderr)。"""
    args = [disc_dir, coordinator] + list(participants)
    for key, val in opts.items():
        args += ["--" + key.replace("_", "-"), str(val)]
    return _run_script("init_discussion.py", args)


def _write_proposal(disc_dir, agent, content):
    """写入 proposals/<agent>-提案文档.md，返回绝对路径。"""
    p = os.path.join(disc_dir, "proposals", "%s-提案文档.md" % agent)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def _submit(disc_dir, agent, filepath, kind="proposal", actor=None):
    """由 state 当前协调者显式登记参与者产物；参与者本身无权改机器状态。"""
    if actor is None:
        actor = _load_state(disc_dir)["coordinator"]
    return _run_script(
        "submit_contribution.py",
        [disc_dir, agent, filepath, kind, "--actor", actor],
    )


def _create_discussion_doc(disc_dir):
    """从 assets/discussion-template.md 拷贝主讨论文档到讨论目录根。"""
    src = os.path.join(ASSETS_DIR, "discussion-template.md")
    dst = os.path.join(disc_dir, "测试讨论文档_20260809.md")
    shutil.copyfile(src, dst)
    return dst


def _find_discussion_md(disc_dir):
    """定位顶层 *讨论文档*.md，多个时取最新。"""
    cands = [p for p in glob.glob(os.path.join(disc_dir, "*讨论文档*.md")) if os.path.isfile(p)]
    if not cands:
        return None
    cands.sort(key=os.path.getmtime, reverse=True)
    return cands[0]


def _backdate_lease(disc_dir, seconds=120):
    """测试夹具：把协调租约改为已过期，模拟协调者超时。不改 revision。"""
    st = _load_state(disc_dir)
    st["coordination_lease_until"] = (_now_plus8() - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    _write_state(disc_dir, st)


def _set_candidate_decision(disc_dir, candidate_ids, package_text):
    """测试夹具：把 stage 置为 candidate_decision 并登记候选 ID，同时追加结构化决策包。"""
    st = _load_state(disc_dir)
    st["stage"] = "candidate_decision"
    st["candidate_decision_ids"] = list(candidate_ids)
    _write_state(disc_dir, st)
    md = _find_discussion_md(disc_dir)
    with open(md, "r", encoding="utf-8") as f:
        text = f.read()
    if "## 四、结构化决策包" not in text:
        text = text.rstrip("\n") + "\n\n## 四、结构化决策包\n\n" + package_text + "\n"
        with open(md, "w", encoding="utf-8") as f:
            f.write(text)


def _pipeline_to_merge(disc_dir, coordinator, participants, disposition="archive", do_responses=False):
    """标准流水线：init → 全员提案 → 建主文档 → merge。返回 (merge_rc, out, err, state)。"""
    rc, out, err = _init_discussion(disc_dir, coordinator, participants, disposition=disposition)
    if rc != 0:
        return rc, out, err, None
    for p in participants:
        prop = _write_proposal(disc_dir, p, "提案：%s 的独立提案\n" % p)
        rc2, out2, err2 = _submit(disc_dir, p, prop, "proposal")
        if rc2 != 0:
            return rc2, out2, err2, None
    _create_discussion_doc(disc_dir)
    rc3, out3, err3 = _run_script("merge_proposals.py",
                                  ["--state", _state_path(disc_dir), "--actor", coordinator])
    st = _load_state(disc_dir) if os.path.isfile(_state_path(disc_dir)) else None
    if rc3 == 0 and do_responses:
        for p in participants:
            rp = os.path.join(disc_dir, "responses", "%s-交叉回应文档.md" % p)
            with open(rp, "w", encoding="utf-8") as f:
                f.write("回应：%s 的交叉回应\n" % p)
            _submit(disc_dir, p, rp, "response")
        st = _load_state(disc_dir)
    return rc3, out3, err3, st


def _confirm_direct(disc_dir, coordinator, candidate_id, confirm_text):
    """直接固化（单项无歧义）：调用 confirm_decision.py。"""
    return _run_script("confirm_decision.py", [
        "--state", _state_path(disc_dir), "--actor", coordinator,
        "--candidate-id", candidate_id, "--confirm-text", confirm_text])


def _decision_package_text():
    """结构化决策包文本（§13.1 要求的全部 8 个要素）。"""
    return (
        "### 共识事项\n\n- 双方认同问题确实存在\n\n"
        "### 分歧事项\n\n- 方案 A 与方案 B 无法收敛\n\n"
        "### 候选方案\n\n- 方案 A / 方案 B\n\n"
        "### 各方案支持者\n\n- 方案 A: claude ｜ 方案 B: codex（计数仅为展示，不构成裁决依据）\n\n"
        "### 主要依据\n\n- A 依据……\n- B 依据……\n\n"
        "### 风险与代价\n\n- A 风险……\n- B 风险……\n\n"
        "### 各 Agent 推荐\n\n- claude: 方案 A\n- codex: 方案 B\n- openclaw: 弃权待 Rainier 决定\n\n"
        "### 待 Rainier 选择的问题\n\n- Q1: 方案 A 还是方案 B？\n")


def _sha256_file(path):
    """对文件原始字节计算 sha256（与 export_docx 的 sha256_file 一致）。

    Windows 下文本模式读写会做 \\n <-> \\r\\n 转换，内存字符串哈希与落盘字节哈希
    不一致；断言「manifest.source_sha256 == 落盘 md」必须按原始字节哈希。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_section(md_text, heading):
    """提取从 heading 起到下一个 ## 顶级标题前的区块文本；找不到返回空串。"""
    idx = md_text.find(heading)
    if idx == -1:
        return ""
    nxt = idx + len(heading)
    m = re.search(r"^## ", md_text[nxt:], flags=re.M)
    if m:
        return md_text[idx:nxt + m.start()]
    return md_text[idx:]


def check(ok, msg):
    """记录一条断言并打印 PASS/FAIL。"""
    print("  %s | %s" % ("PASS" if ok else "FAIL", msg))
    return bool(ok)

# ============ 情景 1：正常三方独立提案与合并 ============
def test_scenario_01():
    d = tempfile.mkdtemp(prefix="sc01-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        st = _load_state(d)
        ok &= check(st["stage"] == "initialized" and st["revision"] == 1,
                    "初始化后 stage=initialized, revision=1")
        for p in ["claude", "codex", "openclaw"]:
            prop = _write_proposal(d, p, "提案：%s 的独立提案\n" % p)
            rc2, out2, _ = _submit(d, p, prop, "proposal")
            ok &= check(rc2 == 0, "提交提案 %s 退出码 0" % p)
        st = _load_state(d)
        ok &= check(st["stage"] == "independent_proposal", "全部提案后 stage=independent_proposal")
        ok &= check(all(v == "submitted" for v in st["submission_status"].values()),
                    "三方 submission_status 全部 submitted")
        rc3, out3, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "independent_proposal", d])
        ok &= check(rc3 == 0, "validate --expect-phase independent_proposal 退出码 0")
        _create_discussion_doc(d)
        rc4, out4, _ = _run_script("merge_proposals.py",
                                   ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc4 == 0, "merge 退出码 0")
        ok &= check("共合并 3 份提案" in out4, "merge 输出合并 3 份提案")
        st = _load_state(d)
        ok &= check(st["stage"] == "cross_response", "merge 后 stage=cross_response")
        ok &= check(st["revision"] == 6, "revision=6（init1 + 3 提案 + merge 两段推进）")
        md = _find_discussion_md(d)
        md_text = open(md, encoding="utf-8").read()
        ok &= check(len(re.findall(r"### .* 提案（来源", md_text)) == 3,
                    "主文档合并 3 个提案区块")
        ok &= check("sha256:" in md_text, "合并区块带 sha256 标记")
        archived = glob.glob(os.path.join(d, "archive", "proposals", "*.md"))
        ok &= check(len(archived) == 3 and os.path.isfile(
            os.path.join(d, "archive", "proposals", "manifest.json")),
            "归档目录保留 3 份提案 + manifest.json")
        ok &= check(not glob.glob(os.path.join(d, "proposals", "*.md")),
                    "proposals/ 下提案已移走")
        # 交叉回应后 validate 应全绿
        for p in ["claude", "codex", "openclaw"]:
            rp = os.path.join(d, "responses", "%s-交叉回应文档.md" % p)
            with open(rp, "w", encoding="utf-8") as f:
                f.write("回应：%s 的交叉回应\n" % p)
            _submit(d, p, rp, "response")
        rc5, out5, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "cross_response", d])
        ok &= check(rc5 == 0, "validate --expect-phase cross_response 退出码 0")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 2：参与者数量大于三个 ============
def test_scenario_02():
    d = tempfile.mkdtemp(prefix="sc02-")
    try:
        parts = ["claude", "codex", "openclaw", "gemini"]
        rc, out, err, st = _pipeline_to_merge(d, "claude", parts)
        ok = check(rc == 0, "四参与者 merge 退出码 0")
        ok &= check(st is not None and st["stage"] == "cross_response", "stage=cross_response")
        ok &= check(set(st["submission_status"].keys()) == set(parts),
                    "submission_status 键集覆盖 4 个身份")
        md_text = open(_find_discussion_md(d), encoding="utf-8").read()
        ok &= check(len(re.findall(r"### .* 提案（来源", md_text)) == 4,
                    "主文档合并 4 个提案区块")
        ok &= check("共合并 4 份提案" in out, "merge 输出合并 4 份提案")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 3：同一 Agent 重复提交 ============
def test_scenario_03():
    d = tempfile.mkdtemp(prefix="sc03-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        prop = _write_proposal(d, "claude", "提案：claude 第一次提交\n")
        rc1, out1, _ = _submit(d, "claude", prop, "proposal")
        ok &= check(rc1 == 0, "首次提交退出码 0")
        st1 = _load_state(d)
        rev1 = st1["revision"]
        ok &= check(st1["submission_status"]["claude"] == "submitted", "首次提交后 claude=submitted")
        rc2, out2, _ = _submit(d, "claude", prop, "proposal")
        st2 = _load_state(d)
        ok &= check(rc2 == 0, "重复提交退出码 0（幂等成功）")
        ok &= check(st2["revision"] == rev1, "重复提交 revision 不变（不重复计数）")
        ok &= check(len(st2["expected_participants"]) == 3
                    and len(st2["submission_status"]) == 3,
                    "未创建第二个参与者条目（名单与状态仍各 3 条）")
        ok &= check("幂等" in out2, "重复提交输出标注幂等")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 4：提案阶段意外读取其他方案（独立性污染→降级声明） ============
def test_scenario_04():
    d = tempfile.mkdtemp(prefix="sc04-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        # claude 声明独立性受限（§10.3 降级声明）
        decl = ("> 独立性受限声明：本提案在形成过程中读取了其他参与方的方案内容，"
                "独立性受限，特此如实声明。\n\n提案正文……\n")
        _write_proposal(d, "claude", decl)
        _write_proposal(d, "codex", "提案：codex 独立提案\n")
        _write_proposal(d, "openclaw", "提案：openclaw 独立提案\n")
        for p in ["claude", "codex", "openclaw"]:
            prop = os.path.join(d, "proposals", "%s-提案文档.md" % p)
            rc2, _, _ = _submit(d, p, prop, "proposal")
            ok &= check(rc2 == 0, "提交 %s 退出码 0" % p)
        _create_discussion_doc(d)
        rc3, out3, _ = _run_script("merge_proposals.py",
                                   ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc3 == 0, "含降级声明的提案仍可合并（退出码 0）")
        md_text = open(_find_discussion_md(d), encoding="utf-8").read()
        ok &= check("独立性受限声明" in md_text, "降级声明如实保留到主文档（不抹除）")
        ok &= check("### claude 提案" in md_text, "claude 提案区块已合并")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 5：两个 Agent 同时认领协调权（原子性） ============
def _claim_worker(state_path, agent, barrier, q):
    """子进程：等待 Barrier 后尝试原子认领，结果放入队列。"""
    try:
        barrier.wait(timeout=30)
    except Exception:
        pass
    rc, out, err = _run_script("claim_coordination.py",
                               ["--state", state_path, "--agent", agent])
    q.put((agent, rc, out, err))


def test_scenario_05():
    d = tempfile.mkdtemp(prefix="sc05-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "codex", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0（默认协调者 codex）")
        _create_discussion_doc(d)
        _backdate_lease(d, 300)  # 让租约到期，两个 Agent 均可尝试认领
        rev_before = _load_state(d)["revision"]
        state_path = _state_path(d)
        barrier = Barrier(2)
        q = Queue()
        p1 = Process(target=_claim_worker, args=(state_path, "claude", barrier, q))
        p2 = Process(target=_claim_worker, args=(state_path, "openclaw", barrier, q))
        p1.start(); p2.start()
        p1.join(60); p2.join(60)
        results = []
        for _ in range(2):
            try:
                results.append(q.get(timeout=15))
            except Empty:
                break
        ok &= check(len(results) == 2, "两个认领进程均返回")
        wins = [r for r in results if r[1] == 0]
        ok &= check(len(wins) == 1, "只有一个 Agent 认领成功（退出码 0）")
        losers = [r for r in results if r[1] != 0]
        ok &= check(all(r[1] in (3, 4) for r in losers),
                    "认领失败方退出码为 3（租约未到期）或 4（原子锁/CAS 失败），未继续执行协调操作")
        st = _load_state(d)
        winner = wins[0][0] if wins else None
        ok &= check(winner is not None and st["coordinator"] == winner,
                    "state.coordinator 为唯一胜者：%s" % winner)
        ok &= check(st["revision"] == rev_before + 1, "协调权变更恰好 revision+1")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)

# ============ 情景 6：协调者超时后成功接管 ============
def test_scenario_06():
    d = tempfile.mkdtemp(prefix="sc06-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "codex", ["claude", "codex", "openclaw"],
                                        coordinator_timeout=300)
        ok &= check(rc == 0, "init 退出码 0（默认协调者 codex）")
        _create_discussion_doc(d)
        _backdate_lease(d, 300)  # 模拟协调者超时
        rev_before = _load_state(d)["revision"]
        rc2, out2, err2 = _run_script("claim_coordination.py",
                                      ["--state", _state_path(d), "--agent", "claude"])
        ok &= check(rc2 == 0, "接管认领退出码 0")
        ok &= check("协调权接管成功" in out2, "输出标注接管成功")
        st = _load_state(d)
        ok &= check(st["coordinator"] == "claude", "coordinator 变为 claude")
        ok &= check(st["revision"] == rev_before + 1, "接管后 revision 恰好 +1")
        md_text = open(_find_discussion_md(d), encoding="utf-8").read()
        ok &= check("协调接管" in md_text, "审计时间线记录协调接管事件")
        ok &= check("codex" in md_text, "审计记录原协调者 codex")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 7：普通参与者超时后暂停并请求 Rainier 决策 ============
def test_scenario_07():
    d = tempfile.mkdtemp(prefix="sc07-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"],
                                        participant_timeout=60)
        ok &= check(rc == 0, "init 退出码 0")
        st = _load_state(d)
        ok &= check(st["participant_timeout"] == 60, "participant_timeout 按配置记录（60 秒）")
        # openclaw 模拟超时未提交，其余两人提交
        for p in ["claude", "codex"]:
            prop = _write_proposal(d, p, "提案：%s 的独立提案\n" % p)
            rc2, _, _ = _submit(d, p, prop, "proposal")
            ok &= check(rc2 == 0, "提交 %s 退出码 0" % p)
        _create_discussion_doc(d)
        rev_before = _load_state(d)["revision"]
        rc3, out3, err3 = _run_script("merge_proposals.py",
                                      ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc3 == 3, "存在未提交参与者时 merge 拒绝推进（退出码 3）")
        ok &= check("提案缺失" in (out3 + err3), "输出标注缺失参与者")
        st = _load_state(d)
        ok &= check(st["stage"] == "independent_proposal", "阶段保持 independent_proposal（暂停推进）")
        ok &= check(st["revision"] == rev_before, "revision 不变（拒绝推进不 +1）")
        ok &= check("openclaw" in st["expected_participants"], "未自动移除超时参与者（openclaw 仍在名单）")
        rc4, out4, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "independent_proposal", d])
        ok &= check(rc4 == 0, "暂停状态 validate 通过（一致性保持）")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 8：名单冻结后新增或移除参与者 ============
def test_scenario_08():
    d = tempfile.mkdtemp(prefix="sc08-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        prop = _write_proposal(d, "claude", "提案：claude 的独立提案\n")
        rc1, _, _ = _submit(d, "claude", prop, "proposal")
        ok &= check(rc1 == 0, "首份提案后名单进入冻结阶段（independent_proposal）")
        # 1) 名单外的新身份提交被拒绝
        prop_x = _write_proposal(d, "gemini", "提案：gemini 越权提案\n")
        rc2, out2, err2 = _submit(d, "gemini", prop_x, "proposal")
        ok &= check(rc2 == 3 and "不在参与者名单内" in (out2 + err2),
                    "名单外身份提交被拒绝（退出码 3）")
        # 2) 未经 Rainier 确认偷偷新增名单 → schema 一致性校验失败
        st = _load_state(d)
        st["expected_participants"].append("gemini")
        _write_state(d, st)
        rc3, out3, _ = _run_script("validate_discussion.py", [d])
        ok &= check(rc3 == 1, "新增参与者但未同步 submission_status 时 validate 拒绝（退出码 1）")
        # 3) 未经 Rainier 确认偷偷移除名单 → schema 一致性校验失败
        st = _load_state(d)
        st["expected_participants"].remove("gemini")  # 还原
        st["expected_participants"].remove("openclaw")  # 模拟未授权移除
        _write_state(d, st)
        rc4, out4, _ = _run_script("validate_discussion.py", [d])
        ok &= check(rc4 == 1, "移除参与者但 submission_status 仍含该身份时 validate 拒绝（退出码 1）")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 9：Markdown 与 state.json 不一致时拒绝推进 ============
def test_scenario_09():
    d = tempfile.mkdtemp(prefix="sc09-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        for p in ["claude", "codex", "openclaw"]:
            prop = _write_proposal(d, p, "提案：%s 的独立提案\n" % p)
            rc2, _, _ = _submit(d, p, prop, "proposal")
            ok &= check(rc2 == 0, "提交 %s 退出码 0" % p)
        _create_discussion_doc(d)
        # 制造不一致：state 声称 claude 已提交，但 claude 提案文件被删
        os.remove(os.path.join(d, "proposals", "claude-提案文档.md"))
        rev_before = _load_state(d)["revision"]
        rc3, out3, err3 = _run_script("merge_proposals.py",
                                      ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc3 == 3, "state 与 Markdown 证据不一致时 merge 拒绝推进（退出码 3）")
        ok &= check("提案缺失" in (out3 + err3), "输出缺失证据与恢复入口")
        st = _load_state(d)
        ok &= check(st["stage"] == "independent_proposal", "阶段未推进（保持 independent_proposal）")
        ok &= check(st["revision"] == rev_before, "revision 不变（不一致未修复前不推进）")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 10：多项自然语言确认存在歧义 ============
def test_scenario_10():
    d = tempfile.mkdtemp(prefix="sc10-")
    try:
        ok = True
        rc, out, err, st = _pipeline_to_merge(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "流水线推进到 cross_response")
        # 夹具：登记两个候选决策并生成结构化决策包
        _set_candidate_decision(d, ["D-D1", "D-D2"], _decision_package_text())
        rev_before = _load_state(d)["revision"]
        # 1) 多项确认（引用 2 个候选 ID）且无二次确认 → 拒绝固化
        rc2, out2, err2 = _run_script("confirm_decision.py", [
            "--state", _state_path(d), "--actor", "claude",
            "--candidate-id", "D-D1", "--confirm-text", "确认 D-D1 并调整 D-D2"])
        ok &= check(rc2 == 3, "多项/歧义确认未二次确认时拒绝固化（退出码 3）")
        ok &= check("需要二次确认" in out2, "先回显决策 ID 与完整内容")
        st = _load_state(d)
        ok &= check(st["stage"] == "candidate_decision", "候选决策保持 candidate 状态（未固化）")
        ok &= check(st["confirmed_decision_ids"] == [], "confirmed_decision_ids 仍为空")
        ok &= check(st["revision"] == rev_before, "拒绝固化 revision 不变")
        # 2) 提供明确二次确认 → 才固化为正式决策
        rc3, out3, err3 = _run_script("confirm_decision.py", [
            "--state", _state_path(d), "--actor", "claude",
            "--candidate-id", "D-D1", "--confirm-text", "确认 D-D1 并调整 D-D2",
            "--second-confirm", "确认"])
        ok &= check(rc3 == 0, "提供二次确认后固化成功（退出码 0）")
        st = _load_state(d)
        ok &= check(st["stage"] == "confirmed_decision", "二次确认后 stage=confirmed_decision")
        ok &= check(st["confirmed_decision_ids"] == ["D-D1"], "确认闸门保存候选决策 ID")
        md_text = open(_find_discussion_md(d), encoding="utf-8").read()
        ok &= check("候选决策 ID: D-D1" in md_text, "确认固化记录保存候选 ID/原文/记录者")
        ok &= check("确认原文" in md_text, "确认固化记录保存确认原文")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)

# ============ 情景 11：分歧无法收敛并生成结构化决策包 ============
def test_scenario_11():
    d = tempfile.mkdtemp(prefix="sc11-")
    try:
        ok = True
        rc, out, err, st = _pipeline_to_merge(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "流水线推进到 cross_response")
        _set_candidate_decision(d, ["D-D1"], _decision_package_text())
        md_text = open(_find_discussion_md(d), encoding="utf-8").read()
        required = ["### 共识事项", "### 分歧事项", "### 候选方案", "### 各方案支持者",
                    "### 主要依据", "### 风险与代价", "### 各 Agent 推荐", "### 待 Rainier 选择的问题"]
        ok &= check(all(s in md_text for s in required), "结构化决策包包含全部 8 个要素")
        ok &= check("计数仅为展示，不构成裁决依据" in md_text,
                    "标注支持者计数仅为展示（不采用多数票裁决）")
        st = _load_state(d)
        ok &= check(st["candidate_decision_ids"] == ["D-D1"], "候选决策 ID 已登记")
        rc2, out2, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "candidate_decision", d])
        ok &= check(rc2 == 0, "validate --expect-phase candidate_decision 退出码 0")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 12：选择归档后的提案保留 ============
def test_scenario_12():
    d = tempfile.mkdtemp(prefix="sc12-")
    try:
        parts = ["claude", "codex", "openclaw"]
        rc, out, err, st = _pipeline_to_merge(d, "claude", parts, disposition="archive")
        ok = check(rc == 0, "disposition=archive 时 merge 退出码 0")
        ok &= check(st is not None and st["proposal_disposition"] == "archive",
                    "proposal_disposition 记录为 archive")
        archived = glob.glob(os.path.join(d, "archive", "proposals", "*.md"))
        ok &= check(len(archived) == 3, "归档目录保留 3 份提案文件")
        # 内容与哈希可验证
        for p in parts:
            af = os.path.join(d, "archive", "proposals", "%s-提案文档.md" % p)
            content = open(af, encoding="utf-8").read()
            ok &= check(content == "提案：%s 的独立提案\n" % p, "归档保留 %s 提案原文" % p)
        manifest_path = os.path.join(d, "archive", "proposals", "manifest.json")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        ok &= check(len(manifest.get("entries", [])) == 3 and
                    all("sha256" in e and "filename" in e for e in manifest["entries"]),
                    "归档 manifest 记录文件名与可验证哈希")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 13：选择删除但未获得再次确认（不删除） ============
def test_scenario_13():
    d = tempfile.mkdtemp(prefix="sc13-")
    try:
        parts = ["claude", "codex", "openclaw"]
        rc, out, err, st = _pipeline_to_merge(d, "claude", parts, disposition="delete")
        ok = check(rc == 0, "disposition=delete 时 merge 退出码 0")
        ok &= check(st is not None and st["proposal_disposition"] == "delete",
                    "proposal_disposition 记录为 delete")
        ok &= check(len(glob.glob(os.path.join(d, "proposals", "*.md"))) == 3,
                    "未获再次确认前提案文件未被删除（仍存于 proposals/）")
        del_lists = glob.glob(os.path.join(d, "deliverables", "删除清单_*.json"))
        ok &= check(len(del_lists) == 1, "生成精确删除清单")
        dl = json.load(open(del_lists[0], encoding="utf-8"))
        ok &= check(len(dl.get("files", [])) == 3 and
                    all("path" in f and "sha256" in f for f in dl["files"]),
                    "删除清单列出 3 个文件及路径/哈希")
        ok &= check("未确认前不删除任何文件" in open(del_lists[0], encoding="utf-8").read(),
                    "清单标注未确认前不删除")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 14：两分钟辅助监测未启用或中途失效 ============
def test_scenario_14():
    d = tempfile.mkdtemp(prefix="sc14-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "init 退出码 0")
        st = _load_state(d)
        mon = st["monitoring"]
        ok &= check(mon["enabled"] is False, "未明确同意时监测默认不启用（enabled=False）")
        ok &= check(mon["mode"] == "reply_before" and mon["interval_seconds"] == 120
                    and mon["status"] == "active",
                    "监测字段符合 schema（mode=reply_before, interval=120, status=active）")
        # 监测未启用不影响核心流程
        prop = _write_proposal(d, "claude", "提案：claude 的独立提案\n")
        rc1, _, _ = _submit(d, "claude", prop, "proposal")
        ok &= check(rc1 == 0, "监测关闭时提案提交正常")
        # 模拟中途失效（status=expired）：核心流程仍可运行
        st = _load_state(d)
        st["monitoring"]["status"] = "expired"
        _write_state(d, st)
        prop2 = _write_proposal(d, "codex", "提案：codex 的独立提案\n")
        rc2, _, _ = _submit(d, "codex", prop2, "proposal")
        ok &= check(rc2 == 0, "监测失效（status=expired）时核心流程仍可运行")
        rc3, out3, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "independent_proposal", d])
        ok &= check(rc3 == 0, "监测失效时 validate 仍通过（不影响一致性校验）")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 15：Word 转换成功并记录源 Markdown 哈希 ============
def test_scenario_15():
    d = tempfile.mkdtemp(prefix="sc15-")
    try:
        ok = True
        rc, out, err, st = _pipeline_to_merge(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "流水线推进到 cross_response")
        _set_candidate_decision(d, ["D-D1"], _decision_package_text())
        rc1, out1, err1 = _confirm_direct(d, "claude", "D-D1", "确认 D-D1")
        ok &= check(rc1 == 0, "单项无歧义确认直接固化（退出码 0）")
        st = _load_state(d)
        ok &= check(st["stage"] == "confirmed_decision", "确认后 stage=confirmed_decision")
        rev_before = st["revision"]
        md = _find_discussion_md(d)
        manifest_path = os.path.join(d, "deliverables", "word-snapshots.json")
        rc2, out2, err2 = _run_script("export_docx.py",
                                      ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc2 == 0, "export_docx 退出码 0")
        st = _load_state(d)
        ok &= check(st["stage"] == "delivered", "导出成功后 stage=delivered")
        ok &= check(st["revision"] == rev_before + 1, "导出推进恰好 revision+1")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        snaps = manifest.get("snapshots", [])
        ok &= check(len(snaps) == 1, "首次导出快照数 = 1")
        # V2：先追加交付记录再算哈希，manifest.source_sha256 == 最终落盘 md 字节哈希
        final_sha = _sha256_file(md)
        ok &= check(snaps[0]["source_sha256"] == final_sha,
                    "快照记录 source_sha256 == 落盘 md 字节哈希（按最终文本）")
        ok &= check("source_markdown" in snaps[0] and "generated_at" in snaps[0] and "docx" in snaps[0],
                    "快照记录源路径/生成时间/快照路径")
        ok &= check(os.path.isfile(os.path.join(d, snaps[0]["docx"])),
                    "deliverables 下 docx 文件存在")
        md_text = open(md, encoding="utf-8").read()
        ok &= check("## 七、Word 交付记录" in md_text, "主文档出现 Word 交付记录")
        ok &= check("源 Markdown 路径:" in md_text and "源文件哈希:" in md_text,
                    "主文档记录源 Markdown 路径与哈希")
        # 重复 export 幂等：快照数不递增
        rc3, out3, err3 = _run_script("export_docx.py",
                                      ["--state", _state_path(d), "--actor", "claude"])
        ok &= check(rc3 == 0, "重复 export 退出码 0（幂等空操作）")
        ok &= check("幂等" in out3, "重复 export 输出标注幂等")
        manifest2 = json.load(open(manifest_path, encoding="utf-8"))
        ok &= check(len(manifest2.get("snapshots", [])) == 1, "重复 export 快照数不递增（仍为 1）")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ 情景 16：Word 转换失败但 Markdown 正式决策保持有效 ============
def test_scenario_16():
    d = tempfile.mkdtemp(prefix="sc16-")
    try:
        ok = True
        rc, out, err, st = _pipeline_to_merge(d, "claude", ["claude", "codex", "openclaw"])
        ok &= check(rc == 0, "流水线推进到 cross_response")
        _set_candidate_decision(d, ["D-D1"], _decision_package_text())
        rc1, out1, err1 = _confirm_direct(d, "claude", "D-D1", "确认 D-D1")
        ok &= check(rc1 == 0, "单项无歧义确认直接固化（退出码 0）")
        st = _load_state(d)
        ok &= check(st["stage"] == "confirmed_decision", "确认后 stage=confirmed_decision")
        rev_before = st["revision"]
        md = _find_discussion_md(d)
        md_before = open(md, encoding="utf-8").read()
        confirm_section_before = _extract_section(md_before, "## 六、确认固化记录")
        # 制造转换失败：输出目录不存在 → doc.save 抛 OSError
        bad_out = os.path.join(d, "deliverables", "no_such_sub", "x.docx")
        rc2, out2, err2 = _run_script("export_docx.py", [
            "--state", _state_path(d), "--actor", "claude", "--out", bad_out])
        ok &= check(rc2 == 1, "Word 转换失败退出码 1")
        ok &= check("Markdown 仍是唯一权威内容源" in (out2 + err2),
                    "失败输出明确 Markdown 仍为唯一权威内容源")
        st = _load_state(d)
        ok &= check(st["stage"] == "confirmed_decision", "转换失败不推进阶段（保持 confirmed_decision）")
        ok &= check(st["revision"] == rev_before, "转换失败 revision 不变")
        ok &= check(st["confirmed_decision_ids"] == ["D-D1"], "正式决策保持有效")
        md_after = open(md, encoding="utf-8").read()
        # V2：失败发生在 convert 之前，交付记录（§15 记录）可能已幂等追加，
        # 但不得改写正式决策与确认固化区块——断言「决策区块内容未变」。
        ok &= check(_extract_section(md_after, "## 六、确认固化记录") == confirm_section_before,
                    "转换失败不改写正式决策与确认固化区块")
        ok &= check("候选决策 ID: D-D1" in md_after, "确认固化记录仍保留在 Markdown")
        ok &= check("## 七、Word 交付记录" in md_after, "主文档含 Word 交付记录区块")
        rc3, out3, _ = _run_script("validate_discussion.py",
                                   ["--expect-phase", "confirmed_decision", d])
        ok &= check(rc3 == 0, "失败后 validate --expect-phase confirmed_decision 仍通过")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============ Participant Runtime：真实项目级 E2E 场景 ============
# 每个情景均创建真实项目工作区，使用 init_discussion 发布 Runtime，再以
# runner/receipt/orchestrate 的公开接口推进。FakeWakeAdapter 仅记录已接受
# 的投递请求，绝不伪造外部平台已经唤醒或模型已经写作。


def _runtime_init(disc_dir, participants=("claude", "codex"), disposition="delete"):
    """初始化一个带项目级 Runtime、指令队列和回执目录的真实工作区。"""
    rc, out, err = _init_discussion(
        disc_dir, participants[0], list(participants), disposition=disposition,
        max_repair_attempts=3, min_response_rounds=1, max_response_rounds=2,
    )
    if rc != 0:
        raise RuntimeError("Runtime 工作区初始化失败: %s%s" % (out, err))
    return Path(disc_dir), FakeWakeAdapter()


def _instruction_id(workspace, agent, kind):
    matches = sorted((Path(workspace) / "instructions" / agent).glob("*-%s-*.json" % kind))
    if len(matches) != 1:
        raise AssertionError("%s 应恰有一条 %s 指令，实际 %d 条" % (agent, kind, len(matches)))
    return json.loads(matches[0].read_text(encoding="utf-8"))["instruction_id"]


def _bootstrap_all(workspace, participants):
    """真实消费 bootstrap；accepted 回执表示 Runtime 清单已被参与端核验。"""
    for agent in participants:
        instruction_id = _instruction_id(workspace, agent, "bootstrap")
        rc = run_instruction(Path(workspace), agent, instruction_id)
        if rc != 0:
            raise AssertionError("%s bootstrap 失败: %s" % (agent, rc))


def _start_proposals(workspace, wake, participants):
    _bootstrap_all(workspace, participants)
    result = orchestrate_once(Path(workspace), wake)
    if result.blocking_error_codes:
        raise AssertionError("bootstrap 后不应阻塞: %s" % result.blocking_error_codes)
    for agent in participants:
        _instruction_id(workspace, agent, "propose")
    return result


def _complete_proposal(workspace, agent, text=None, kind="propose"):
    """模拟参与者仅写自己的 Markdown，再由 Runtime 校验并写 completed 回执。"""
    root = Path(workspace)
    output = root / "proposals" / (agent + "-提案文档.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text or "# %s 的独立提案\n\n内容完整。\n" % agent, encoding="utf-8")
    return run_instruction(root, agent, _instruction_id(root, agent, kind))


def _complete_response(workspace, agent, text=None):
    root = Path(workspace)
    output = root / "responses" / (agent + "-交叉回应文档.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text or "# %s 的交叉回应\n\n只回应已合并内容。\n" % agent, encoding="utf-8")
    return run_instruction(root, agent, _instruction_id(root, agent, "respond"))


def _make_instruction(
        workspace, agent, instruction_id, kind, output_path,
        sequence=99, attempt=1, runtime_version=None):
    """用正式 Instruction 对象生成不可伪造哈希的测试指令。"""
    state = _load_state(str(workspace))
    payload = {
        "instruction_id": instruction_id,
        "sequence": sequence,
        "kind": kind,
        "agent_id": agent,
        "runtime_version": runtime_version or state["runtime_distribution"]["version"],
        "state_revision": state["revision"],
        "input_paths": ["project-context.md"],
        "output_path": output_path,
        "attempt": attempt,
        "max_attempts": 3,
        "issued_at": _now_plus8().isoformat(timespec="milliseconds"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["sha256"] = hashlib.sha256(canonical).hexdigest()
    instruction = Instruction.from_dict(payload)
    write_instruction(Path(workspace), instruction)
    return instruction


def test_scenario_17():
    """发布 Runtime 后篡改其中一个清单文件，参与端必须拒绝消费。"""
    d = tempfile.mkdtemp(prefix="sc17-runtime-manifest-")
    try:
        workspace, _wake = _runtime_init(d)
        manifest_path = next((workspace / "runtime" / "participant").glob("*/manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runner = workspace / manifest["entrypoint"]
        runner.write_text(runner.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        ok = False
        try:
            verify_manifest(workspace)
        except WorkflowError as exc:
            ok = exc.code == E_HASH
        return check(ok, "清单篡改被 E_HASH 拒绝，未把篡改 Runtime 视为可执行")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_18():
    """合法签名但越界 output_path 的指令仍必须被 Runner 拒绝。"""
    d = tempfile.mkdtemp(prefix="sc18-runtime-path-")
    try:
        workspace, _wake = _runtime_init(d, participants=("claude",))
        bad = _make_instruction(workspace, "claude", "I-009999", "propose", "../state.json")
        rc = run_instruction(workspace, "claude", bad.instruction_id)
        failed = workspace / "receipts" / "claude" / (bad.instruction_id + "-failed.json")
        payload = json.loads(failed.read_text(encoding="utf-8")) if failed.exists() else {}
        return check(rc == E_PATH_SCOPE and payload.get("error_code") == E_PATH_SCOPE,
                     "越界指令被拒绝并留下 E_PATH_SCOPE 失败回执")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_19():
    """同一完成指令重复运行只能得到一个终结回执，不重复产物或状态推进。"""
    d = tempfile.mkdtemp(prefix="sc19-runtime-idempotent-")
    try:
        participants = ("claude",)
        workspace, wake = _runtime_init(d, participants=participants)
        _start_proposals(workspace, wake, participants)
        first = _complete_proposal(workspace, "claude")
        revision_before = _load_state(str(workspace))["revision"]
        second = _complete_proposal(workspace, "claude")
        completed = list((workspace / "receipts" / "claude").glob("*-completed.json"))
        result = orchestrate_once(workspace, wake)
        return check(first == 0 and second == 0, "重复消费终结指令返回幂等成功") and \
            check(len(completed) == 1, "同一指令仅有一个 completed 回执") and \
            check(result.stage == "cross_response", "首次有效完成后自动推进，不因重复回执重复推进") and \
            check(_load_state(str(workspace))["revision"] > revision_before, "协调端仅在有效状态转换时递增 revision")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_20():
    """缺失 Markdown 触发同一参与者 repair，修复后可完成且不替换参与者。"""
    d = tempfile.mkdtemp(prefix="sc20-runtime-repair-")
    try:
        participants = ("claude",)
        workspace, wake = _runtime_init(d, participants=participants)
        _start_proposals(workspace, wake, participants)
        initial = _instruction_id(workspace, "claude", "propose")
        rc = run_instruction(workspace, "claude", initial)
        first = orchestrate_once(workspace, wake)
        repair_id = _instruction_id(workspace, "claude", "repair")
        repaired = _complete_proposal(workspace, "claude", kind="repair")
        second = orchestrate_once(workspace, wake)
        return check(rc == E_OUTPUT_FORMAT, "格式/缺失输出被 Runtime 识别为 E_OUTPUT_FORMAT") and \
            check(not first.blocking_error_codes and repair_id != initial, "自动向原参与者下发唯一 repair 指令") and \
            check(repaired == 0 and second.stage == "cross_response", "同一参与者修复后自动进入交叉回应")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_21():
    """修复必须封顶；本断言刻意保留，防止历史失败回执被重复消费后无限重派。"""
    d = tempfile.mkdtemp(prefix="sc21-runtime-retry-cap-")
    try:
        participants = ("claude",)
        workspace, wake = _runtime_init(d, participants=participants)
        _start_proposals(workspace, wake, participants)
        # 第一次失败 → repair attempt=2；第二次失败 → repair attempt=3；第三次失败后必须阻塞。
        run_instruction(workspace, "claude", _instruction_id(workspace, "claude", "propose"))
        orchestrate_once(workspace, wake)
        run_instruction(workspace, "claude", _instruction_id(workspace, "claude", "repair"))
        orchestrate_once(workspace, wake)
        repairs = sorted((workspace / "instructions" / "claude").glob("*-repair-*.json"))
        repair_attempts = [json.loads(path.read_text(encoding="utf-8"))["attempt"] for path in repairs]
        if len(repairs) != 2 or repair_attempts != [2, 3]:
            return check(False, "失败回执不得被重复消费：应仅有 attempt=2 与 attempt=3 两条 repair")
        final = json.loads(repairs[-1].read_text(encoding="utf-8"))["instruction_id"]
        run_instruction(workspace, "claude", final)
        result = orchestrate_once(workspace, wake)
        return check(E_RETRY_EXHAUSTED in result.blocking_error_codes,
                     "第 3 次可修复失败后必须 E_RETRY_EXHAUSTED 阻塞，不得再发 repair")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_22():
    """delete 仅在项目初始化已预授权时发生，先写可核验删除清单再移除原件。"""
    d = tempfile.mkdtemp(prefix="sc22-runtime-delete-")
    try:
        participants = ("claude", "codex")
        workspace, wake = _runtime_init(d, participants=participants, disposition="delete")
        _start_proposals(workspace, wake, participants)
        for agent in participants:
            if _complete_proposal(workspace, agent) != 0:
                return check(False, "%s 的真实提案无法完成" % agent)
        result = orchestrate_once(workspace, wake)
        manifest_path = workspace / "archive" / "proposals" / "deletion-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
        entries = manifest.get("entries", []) if isinstance(manifest, dict) else manifest
        hashes_ok = all(isinstance(item.get("sha256"), str) and len(item["sha256"]) == 64 for item in entries)
        originals_removed = all(not (workspace / "proposals" / (agent + "-提案文档.md")).exists() for agent in participants)
        state = _load_state(str(workspace))
        content = workspace / state["content_authority"]["discussion_path"]
        state_hash_matches = state["content_authority"]["sha256"] == _sha256_file(str(content))
        return check(result.stage == "cross_response", "合并后自动推进到交叉回应") and \
            check(len(entries) == len(participants) and hashes_ok, "删除清单在删除前保留每份提案的路径与哈希") and \
            check(originals_removed, "仅在 delete 预授权下删除已合并提案原件") and \
            check(all(state["submission_status"].get(agent) == "submitted" for agent in participants),
                  "完成回执同步为 submitted，旧校验器与 Runtime 状态不漂移") and \
            check(all(len(state["instruction_queue"].get(agent, [])) == 1 for agent in participants),
                  "未完成的 respond 指令精确投影到各自队列") and \
            check(state_hash_matches, "合并写 Markdown 后同步 content_authority 哈希")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_23():
    """所有有效提案完成后，协调端无需人工通知即下发每位参与者的回应指令。"""
    d = tempfile.mkdtemp(prefix="sc23-runtime-cross-response-")
    try:
        participants = ("claude", "codex")
        workspace, wake = _runtime_init(d, participants=participants, disposition="archive")
        _start_proposals(workspace, wake, participants)
        for agent in participants:
            _complete_proposal(workspace, agent)
        result = orchestrate_once(workspace, wake)
        responses = [_instruction_id(workspace, agent, "respond") for agent in participants]
        document = _find_discussion_md(str(workspace))
        text = Path(document).read_text(encoding="utf-8") if document else ""
        for agent in participants:
            if _complete_response(workspace, agent) != 0:
                return check(False, "%s 的交叉回应未能生成完成回执" % agent)
        candidate = orchestrate_once(workspace, wake)
        state = _load_state(str(workspace))
        content = workspace / state["content_authority"]["discussion_path"]
        state_hash_matches = state["content_authority"]["sha256"] == _sha256_file(str(content))
        validate_rc, _validate_out, _validate_err = _run_script(
            "validate_discussion.py", ["--expect-phase", "candidate_decision", str(workspace)]
        )
        return check(result.stage == "cross_response", "有效提案齐备后自动进入 cross_response") and \
            check(len(set(responses)) == len(participants), "每个参与者获得自己的一条回应指令") and \
            check("## 三、已合并独立提案" in text, "回应开始前已写入合并后的公共讨论内容") and \
            check(candidate.stage == "candidate_decision" and bool(state["candidate_decision_ids"]),
                  "回应完成后自动生成非空候选决策标识") and \
            check(all(state["response_status"].get(agent) == "submitted" for agent in participants),
                  "回应完成回执同步为 submitted") and \
            check(all(not state["instruction_queue"].get(agent, []) for agent in participants),
                  "所有终结指令从队列投影移除") and \
            check(all(len(state["receipt_index"].get(agent, [])) >= 5 for agent in participants),
                  "每位参与者的 bootstrap/propose/respond 回执均投影到 receipt_index") and \
            check(state_hash_matches, "候选内容写入后同步 content_authority 哈希") and \
            check(validate_rc == 0, "Runtime 候选阶段与既有 validate_discussion 兼容")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_24():
    """精确验证 Runtime 的交付后停止职责：只在 delivered 证据存在后发 stop。"""
    d = tempfile.mkdtemp(prefix="sc24-runtime-stop-")
    try:
        participants = ("claude", "codex")
        workspace, wake = _runtime_init(d, participants=participants)
        state = _load_state(str(workspace))
        # Word 生成由既有 export_docx.py（场景 15）验真；这里不伪造 Word，
        # 仅以 delivered 这一 Word 成功后的机器状态验证 Runtime 自动停机。
        state["stage"] = "delivered"
        _write_state(str(workspace), state)
        first = orchestrate_once(workspace, wake)
        issued = [_instruction_id(workspace, agent, "stop") for agent in participants]
        for agent, instruction_id in zip(participants, issued):
            if run_instruction(workspace, agent, instruction_id) != 0:
                return check(False, "%s stop 指令未能真实生成终结回执" % agent)
        second = orchestrate_once(workspace, wake)
        final = _load_state(str(workspace))
        return check(first.stage == "delivered" and len(issued) == len(participants),
                     "仅在 delivered 后自动为每位参与者下发 stop") and \
            check(second.stage == "monitoring_stopped" and final["stage"] == "monitoring_stopped",
                  "全部 stop 回执完成后自动进入 monitoring_stopped")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_25():
    """bootstrap CLI 缺省选择下一条指令；重复运行必须幂等且不制造 failed。"""
    d = tempfile.mkdtemp(prefix="sc25-bootstrap-cli-")
    try:
        workspace, _wake = _runtime_init(d, participants=("claude",))
        state = _load_state(str(workspace))
        manifest = json.loads((workspace / state["runtime_distribution"]["manifest_path"]).read_text(encoding="utf-8"))
        runner = workspace / manifest["entrypoint"]
        command = [PYTHON, str(runner), "--workspace", str(workspace), "--agent", "claude", "--once"]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        receipts = workspace / "receipts" / "claude"
        return check((first.returncode, second.returncode) == (0, 0), "无 instruction-id 的 bootstrap CLI 可重复运行") and \
            check(len(list(receipts.glob("*-accepted.json"))) == 1, "bootstrap 只保留一个 accepted 回执") and \
            check(not list(receipts.glob("*-failed.json")), "重复 bootstrap 不写 failed 回执")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_26():
    """提交登记默认拒绝：缺 actor 和参与者 actor 都不能修改机器权威。"""
    d = tempfile.mkdtemp(prefix="sc26-submit-actor-")
    try:
        ok = True
        rc, out, err = _init_discussion(d, "claude", ["claude", "codex"])
        ok &= check(rc == 0, "actor 门禁工作区初始化成功")
        proposal = _write_proposal(d, "codex", "# codex 独立提案\n")
        state_path = _state_path(d)
        before_bytes = Path(state_path).read_bytes()
        before = _load_state(d)
        before_hash = _sha256_file(state_path)
        content_hash = before["content_authority"]["sha256"]

        missing_rc, _out, _err = _run_script(
            "submit_contribution.py", [d, "codex", proposal, "proposal"]
        )
        ok &= check(missing_rc == 2, "缺少显式 --actor 时 argparse fail-closed")
        ok &= check(Path(state_path).read_bytes() == before_bytes, "缺 actor 不改 state 字节、revision 或哈希")

        participant_rc, _out, _err = _submit(d, "codex", proposal, actor="codex")
        ok &= check(participant_rc == 3, "参与者 actor 不能登记贡献")
        ok &= check(_sha256_file(state_path) == before_hash and _load_state(d)["revision"] == before["revision"],
                    "参与者 actor 被拒后 state/revision 不变")
        ok &= check(_load_state(d)["content_authority"]["sha256"] == content_hash,
                    "参与者 actor 被拒后内容权威哈希不变")

        coordinator_rc, _out, _err = _submit(d, "codex", proposal, actor="claude")
        after = _load_state(d)
        ok &= check(coordinator_rc == 0, "与 state.coordinator 匹配的显式 actor 可登记")
        ok &= check(after["revision"] == before["revision"] + 1 and after["submission_status"]["codex"] == "submitted",
                    "合法协调者登记恰好推进一次 revision")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_27():
    """--stop-monitoring 只能登记请求，不能绕过参与者 stop completed 回执。"""
    d = tempfile.mkdtemp(prefix="sc27-stop-receipts-")
    try:
        participants = ["claude", "codex"]
        rc, out, err, _state = _pipeline_to_merge(d, "claude", participants)
        if rc != 0:
            return check(False, "stop 门禁流水线未能推进到 cross_response")
        _set_candidate_decision(d, ["D-STOP"], _decision_package_text())
        rc, _out, _err = _confirm_direct(d, "claude", "D-STOP", "确认 D-STOP")
        if rc != 0:
            return check(False, "stop 门禁无法建立 confirmed_decision")
        rc, out, err = _run_script("export_docx.py", [
            "--state", _state_path(d), "--actor", "claude", "--stop-monitoring",
        ])
        state = _load_state(d)
        no_stop_receipts = not list(Path(d).glob("receipts/*/*-completed.json"))
        ok = check(rc == 0, "Word 成功后 stop 请求登记成功")
        ok &= check(state["stage"] == "delivered" and state["automation"]["stop_requested"],
                    "--stop-monitoring 保持 delivered，仅记录 stop_requested")
        ok &= check(no_stop_receipts, "请求阶段尚无伪造的 stop completed 回执")

        wake = FakeWakeAdapter()
        first = orchestrate_once(Path(d), wake)
        second = orchestrate_once(Path(d), wake)
        ok &= check(first.stage == "delivered" and second.stage == "delivered",
                    "没有 stop completed 回执时编排器不能进入 monitoring_stopped")
        ok &= check(len(list(Path(d).glob("instructions/*/*-stop-*.json"))) == len(participants),
                    "编排器只下发每位参与者的一条 stop 指令")
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_28():
    """四次独立 wake 审计全部保留；已完成/已下发指令不能被重复唤醒。"""
    d = tempfile.mkdtemp(prefix="sc28-wake-audit-")
    try:
        participants = ("claude", "codex")
        workspace, wake = _runtime_init(d, participants=participants, disposition="archive")
        _start_proposals(workspace, wake, participants)
        for agent in participants:
            _complete_proposal(workspace, agent)
        orchestrate_once(workspace, wake)
        orchestrate_once(workspace, wake)
        store = json.loads((workspace / "audit/wake-events.json").read_text(encoding="utf-8"))
        events = store["events"]
        identities = [(event["agent_id"], event["instruction_id"]) for event in events]
        return check(len(events) == 4 and len(wake.requests) == 4, "2 个 propose + 2 个 respond wake 均保留") and \
            check(len(set(identities)) == 4, "wake 审计按 agent/instruction 去重且不覆盖") and \
            check({event["status"] for event in events} == {"accepted"}, "四次 wake 结果均可审计")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_29():
    """多个 Runtime 版本并存时，Runner 必须按 instruction.runtime_version 选择清单。"""
    d = tempfile.mkdtemp(prefix="sc29-runtime-version-")
    try:
        workspace, _wake = _runtime_init(d, participants=("claude",))
        v100 = publish_runtime(workspace, "1.0.0")
        publish_runtime(workspace, "1.0.1")
        instruction = _make_instruction(
            workspace, "claude", "I-v101", "bootstrap", "receipts/claude/",
            sequence=99, runtime_version="1.0.1",
        )
        old_runner = workspace / v100.entrypoint
        old_runner.write_text(old_runner.read_text(encoding="utf-8") + "\n# tampered 1.0.0\n", encoding="utf-8")
        rc = run_instruction(workspace, "claude", instruction.instruction_id)
        return check(rc == 0, "1.0.1 指令不受已损坏 1.0.0 清单影响") and \
            check((workspace / "receipts/claude/I-v101-accepted.json").is_file(),
                  "按指令版本验证成功并写 accepted 回执")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scenario_30():
    """legacy merge 两个故障点均须完全回滚，下次无故障调用可恢复完成。"""
    root = tempfile.mkdtemp(prefix="sc30-merge-transaction-")
    try:
        ok = True
        for checkpoint in ("after_markdown", "after_manifest"):
            d = os.path.join(root, checkpoint)
            participants = ["claude", "codex"]
            rc, out, err = _init_discussion(d, "claude", participants, disposition="archive")
            ok &= check(rc == 0, "%s 工作区初始化成功" % checkpoint)
            for agent in participants:
                proposal = _write_proposal(d, agent, "# %s proposal\n" % agent)
                submit_rc, _out, _err = _submit(d, agent, proposal, actor="claude")
                ok &= check(submit_rc == 0, "%s 提案登记成功" % agent)

            state_path = Path(_state_path(d))
            discussion = Path(_find_discussion_md(d))
            proposals = {path.name: (path.read_bytes(), _sha256_file(str(path))) for path in Path(d, "proposals").glob("*.md")}
            state_before = state_path.read_bytes()
            markdown_before = discussion.read_bytes()
            state_value = _load_state(d)
            revision_before = state_value["revision"]
            authority_before = state_value["content_authority"]["sha256"]
            fault_env = os.environ.copy()
            fault_env["MULTIAGENT_TEST_FAIL_MERGE_AT"] = checkpoint
            fault_rc, fault_out, fault_err = _run_script(
                "merge_proposals.py",
                ["--state", str(state_path), "--actor", "claude"],
                env=fault_env,
            )
            ok &= check(fault_rc == 1 and "E_STATE_CONFLICT: 工作区事务提交失败并已回滚" in (fault_out + fault_err),
                        "%s 故障注入返回稳定回滚错误" % checkpoint)
            after_fault = _load_state(d)
            ok &= check(state_path.read_bytes() == state_before and after_fault["revision"] == revision_before,
                        "%s 故障后 state 字节与 revision 完全不变" % checkpoint)
            ok &= check(after_fault["content_authority"]["sha256"] == authority_before and discussion.read_bytes() == markdown_before,
                        "%s 故障后 Markdown 与权威哈希完全不变" % checkpoint)
            ok &= check(all(path.is_file() and path.read_bytes() == payload[0] and _sha256_file(str(path)) == payload[1]
                            for name, payload in proposals.items() for path in [Path(d, "proposals", name)]),
                        "%s 故障后所有源提案及哈希保持不变" % checkpoint)
            ok &= check(not list(Path(d, "archive", "proposals").glob("*")),
                        "%s 故障后 archive/manifest 无残留" % checkpoint)

            recover_rc, recover_out, recover_err = _run_script(
                "merge_proposals.py", ["--state", str(state_path), "--actor", "claude"]
            )
            recovered = _load_state(d)
            ok &= check(recover_rc == 0 and recovered["stage"] == "cross_response",
                        "%s 下次无故障调用自动恢复并完成 merge" % checkpoint)
        return ok
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------- 汇总 ----------
SCENARIOS = []  # v1 情景不适用于已退役的直接写状态入口。


def test_legacy_mutators_are_retired():
    """旧入口必须拒绝工作且不改动机器状态。"""
    root = Path(tempfile.mkdtemp(prefix="legacy-mutator-retirement-"))
    try:
        state_path = root / ".multiagent" / "state.json"
        state_path.parent.mkdir(parents=True)
        original = b'{"sentinel":"unchanged"}\n'
        state_path.write_bytes(original)
        calls = (
            ("submit_contribution.py", [str(root), "agent", "missing.md", "proposal"]),
            ("merge_proposals.py", ["--state", str(state_path), "--actor", "agent"]),
            ("claim_coordination.py", ["--state", str(state_path), "--agent", "agent"]),
        )
        for script, args in calls:
            rc, out, err = _run_script(script, args)
            if rc == 0 or "RETIRED" not in (out + err) or state_path.read_bytes() != original:
                return check(False, "%s 未以显式退役结果拒绝状态修改" % script)
        return check(True, "submit/merge/claim 三个旧入口均退役且 state 字节不变")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    # Windows 控制台输出 UTF-8 中文
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("run_scenarios.py —— v2 编排/停止门禁、Word 生命周期及旧入口退役检查")
    print("旧 30 情景不再执行：它们依赖已退役的独立写状态入口。")
    suite = subprocess.run(
        [PYTHON, "-B", "-m", "unittest", "evals.test_orchestration", "evals.test_candidate_delivery", "evals.test_end_to_end_v2", "-v"],
        cwd=_REPO_ROOT, text=True, encoding="utf-8", errors="replace",
    )
    retired_ok = test_legacy_mutators_are_retired()
    print("旧入口状态不变门禁：%s" % ("PASS" if retired_ok else "FAIL"))
    return 0 if suite.returncode == 0 and retired_ok else 1


if __name__ == "__main__":
    sys.exit(main())
