#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读一次工作区文件快照的诊断工具。

本工具默认只扫描一次；不驻留、不轮询、不唤醒前台会话，也不推进工作流。
没有既有快照基线时只能报告当前清单规模，不能声称发现了自上次运行以来的变化。

用法：python workspace_monitor.py --workspace <讨论目录> [--baseline <snapshot.json>]
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


def snapshot(workspace):
    """返回 {相对路径: (mtime_ns, size)} 快照。跳过隐藏目录与 __pycache__。"""
    snap = {}
    if not os.path.isdir(workspace):
        return snap
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.startswith("~$"):  # 跳过 Office 临时锁文件
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, workspace).replace("\\", "/")
            try:
                st = os.stat(p)
                snap[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return snap


def diff(prev, cur):
    """比较两快照，返回 (新增, 修改, 删除)。"""
    added = [k for k in cur if k not in prev]
    removed = [k for k in prev if k not in cur]
    modified = [k for k in cur if k in prev and cur[k] != prev[k]]
    return sorted(added), sorted(modified), sorted(removed)


def report_scan(scan_no, added, modified, removed):
    """打印一次扫描结果（检测到变化时逐条报告）。"""
    if not added and not modified and not removed:
        print("[scan #%d] 无变化" % scan_no, flush=True)
        return
    print("[scan #%d] 检测到变化：新增 %d / 修改 %d / 删除 %d" % (
        scan_no, len(added), len(modified), len(removed)), flush=True)
    for f in added:
        print("  + 新增: %s" % f, flush=True)
    for f in modified:
        print("  ~ 修改: %s" % f, flush=True)
    for f in removed:
        print("  - 删除: %s" % f, flush=True)


def read_stage(workspace):
    """读取工作区 state.json 的 stage（或 phase）字段；缺失时返回 'unknown'。"""
    p = os.path.join(workspace, ".multiagent", "state.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "unknown"
    if isinstance(data, dict):
        for key in ("stage", "phase"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="对工作区做一次只读文件快照；不等待、不唤醒、不推进流程。"
    )
    parser.add_argument("--workspace", required=True, help="讨论工作区目录")
    parser.add_argument("--baseline", default=None, help="可选：先前 snapshot JSON，仅用于比较")
    args = parser.parse_args(argv)

    current = snapshot(args.workspace)
    stage = read_stage(args.workspace)
    print("[diagnostic] stage=%s | 本次扫描文件数=%d | 工作区=%s" % (
        stage, len(current), os.path.abspath(args.workspace)), flush=True)
    if args.baseline:
        try:
            with open(args.baseline, encoding="utf-8") as stream:
                previous = json.load(stream)
            added, modified, removed = diff(previous, current)
            report_scan(1, added, modified, removed)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            print("[diagnostic] baseline 读取失败：%s" % exc, file=sys.stderr)
            return 2
    else:
        print("[diagnostic] 未提供对比基线，本次只报告快照；不会建立后台轮询。", flush=True)
    return 0


if __name__ == "__main__":
    _utf8()
    sys.exit(main())
