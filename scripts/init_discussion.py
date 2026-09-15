#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""init_discussion.py —— 初始化一个多 Agent 协作讨论工作区。

严格遵循 references/state-schema.md 的最小字段与默认值生成 state.json
（initialized 阶段、revision=1、辅助监测默认 off）。

生成内容：
    project-context.md        从 assets/project-context-template.md 拷贝
    <讨论ID>讨论文档_<日期>.md   顶层主讨论文档，以 assets/discussion-template.md 为骨架实例化
    .multiagent/               state、指令、Runtime、密封视图、回执、审计与交付物

幂等：已存在 state.json 时拒绝重复初始化（除非 --force）。

用法：
    python init_discussion.py [选项] <讨论目录> <默认协调者> <参与者...>

选项：
    --force                        已存在 state.json 时强制重新初始化
    --discussion-id ID             讨论 ID（默认取讨论目录名）
    --coordinator-platform ID      协调者实际平台标识（必填）
    --coordinator-session ID       协调者实际会话标识（必填）
    --participant-binding SPEC     每位参与者的实际绑定，重复指定，格式 agent_id=platform_id:session_id
    --attestation-public-key-b64   Ed25519 公钥信任锚（必填，私钥不得进入工作区）
    --attestation-key-id           attestation 签名密钥标识（必填）
    --coordinator-timeout 秒       协调者超时，默认 300（5 分钟）
    --participant-timeout 秒       普通参与者等待，默认 900（15 分钟）
    --disposition archive|delete   提案处置策略，默认 delete；archive 仅原位保留
    --template 路径                项目上下文模板路径（默认脚本上级目录 assets/ 下）

退出码：
    0  成功
    2  用法/参数错误
    3  数据校验失败（参与者名单、协调者、超时、处置策略非法）
    4  已存在 state.json 且未加 --force（重复初始化被拒绝）
    5  模板缺失或文件写入失败
"""

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from workflow_core import atomic_write_json, sha256_file
from participant_runtime.protocol import Instruction, publish_runtime, write_instruction
from openclaw_automation import render_openclaw_operational_directive
from participant_views import access_scope, output_path, publish_inputs
from instruction_prompts import build_task_prompt

PROTOCOL_VERSION = "1.0"
SHANGHAI = "Asia/Shanghai"
# state.json 最小字段（references/state-schema.md 第二节，全部必填）
REQUIRED_FIELDS = [
    "protocol_version",
    "discussion_id",
    "stage",
    "expected_participants",
    "submission_status",
    "response_status",
    "coordinator",
    "coordinator_binding",
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
DEFAULT_COORDINATOR_TIMEOUT = 300  # 协调者超时默认 5 分钟（§8）
DEFAULT_PARTICIPANT_TIMEOUT = 900  # 普通参与者等待默认 15 分钟（§9）
DEFAULT_DISPOSITION = "delete"     # 合并成功后自动删除临时提案源件

# 退出码
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_ALREADY_INIT = 4
EXIT_IO = 5


def shanghai_tz():
    """返回 Asia/Shanghai 时区；Windows 缺 IANA 时区库时降级为固定 +08:00（无夏令时，等价）。"""
    try:
        return ZoneInfo(SHANGHAI)
    except Exception:
        return timezone(timedelta(hours=8))


def now_iso():
    """返回 Asia/Shanghai 时区的 ISO 8601 时间字符串（如 2026-08-09T20:00:00+08:00）。"""
    return datetime.now(shanghai_tz()).isoformat(timespec="seconds")


def write_state_atomic(state_path, state):
    """原子写入 state.json：写临时文件后 os.replace；失败清理临时文件并保留旧状态（单动作编排）。"""
    atomic_write_json(state_path, state)


def normalize_identity_list(participants, coordinator):
    """规范化身份为全小写；返回 (participants, coordinator, error)。error 为 None 表示合法。"""
    participants = [p.strip().lower() for p in participants if p.strip()]
    coordinator = coordinator.strip().lower()
    if not participants:
        return None, None, "参与者名单为空"
    if any(not p for p in participants):
        return None, None, "参与者身份不得为空字符串"
    if len(set(participants)) != len(participants):
        return None, None, "参与者身份存在重复"
    if coordinator not in participants:
        return None, None, "默认协调者不在参与者名单内"
    return participants, coordinator, None


def build_state(discussion_id, participants, coordinator, now,
                coordinator_platform_id, coordinator_session_id,
                participant_bindings, coordinator_timeout, participant_timeout, disposition,
                isolation_trust):
    """按最小字段构建初始 state.json。"""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "discussion_id": discussion_id,
        "stage": "initialized",
        "expected_participants": participants,
        "submission_status": {p: "pending" for p in participants},
        "response_status": {p: "pending" for p in participants},
        "coordinator": coordinator,
        "coordinator_binding": {
            "agent_id": coordinator,
            "role": "coordinator",
            "platform_id": coordinator_platform_id,
            "session_id": coordinator_session_id,
        },
        "participant_bindings": participant_bindings,
        "isolation_trust": isolation_trust,
        # 协调权租约：初始化时 = 当前时间 + 协调者超时（§8）
        "coordination_lease_until": (
            datetime.fromisoformat(now) + timedelta(seconds=coordinator_timeout)
        ).isoformat(timespec="seconds"),
        "coordinator_timeout": coordinator_timeout,
        "participant_timeout": participant_timeout,
        "proposal_disposition": disposition,
        # 辅助监测默认关闭，未明确同意不启动（§14）
        "monitoring": {
            "enabled": False,
            "mode": "reply_before",
            "interval_seconds": 120,
            "status": "active",
        },
        "candidate_decision_ids": [],
        "confirmed_decision_ids": [],
        "last_checked_at": now,
        "revision": 1,
        # Runtime/queue fields are machine process authority. Participants never write them.
        "runtime_distribution": {"version": "1.0.0", "status": "pending"},
        "instruction_queue": {p: [] for p in participants},
        "receipt_index": {p: [] for p in participants},
        "retry_policy": {"max_attempts": 3},
        "automation": {"enabled": True, "last_orchestrated_at": None},
        "convergence": {"min_response_rounds": 1, "max_response_rounds": 2, "no_new_issue_rounds": 1},
        "content_authority": {},
    }


def parse_participant_bindings(specs, participants, coordinator,
                               coordinator_platform_id, coordinator_session_id):
    """解析重复 --participant-binding 参数；返回 (映射, error)。

    使用首个 '=' 和首个 ':' 分隔，因此 session_id 可包含 ':'、'=' 等安全字符。
    """
    bindings = {}
    for raw_spec in specs:
        if not isinstance(raw_spec, str) or raw_spec.count("=") < 1:
            return None, "参与者绑定格式非法，应为 agent_id=platform_id:session_id"
        raw_agent_id, separator, remainder = raw_spec.partition("=")
        raw_platform_id, separator, session_id = remainder.partition(":")
        agent_id = raw_agent_id.strip().lower()
        platform_id = raw_platform_id.strip()
        if not separator or not agent_id or not platform_id or not session_id:
            return None, "参与者绑定格式非法，应为 agent_id=platform_id:session_id"
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in platform_id + session_id):
            return None, "平台与会话标识不得含空白或控制字符"
        if ":" in platform_id or "=" in platform_id:
            return None, "平台标识不得含 ':' 或 '=' 分隔符"
        if agent_id in bindings:
            return None, "参与者绑定身份重复: " + agent_id
        bindings[agent_id] = {
            "agent_id": agent_id,
            "role": "coordinator" if agent_id == coordinator else "participant",
            "platform_id": platform_id,
            "session_id": session_id,
        }

    expected = set(participants)
    supplied = set(bindings)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        details = []
        if missing:
            details.append("缺少: " + ", ".join(missing))
        if extra:
            details.append("多余: " + ", ".join(extra))
        return None, "参与者绑定必须恰好覆盖参与者名单（" + "；".join(details) + ")"

    coordinator_entry = bindings.get(coordinator)
    if coordinator_entry is None:
        return None, "协调者缺少参与者绑定"
    if (coordinator_entry["platform_id"] != coordinator_platform_id
            or coordinator_entry["session_id"] != coordinator_session_id
            or coordinator_entry["role"] != "coordinator"):
        return None, "协调者参与者绑定必须与 --coordinator-platform/--coordinator-session 完全一致"
    return bindings, None


def parse_attestation_trust(public_key_b64, key_id):
    """验证项目外 Ed25519 签名者的公钥配置；绝不接受或保存私钥。"""
    key_id = key_id.strip()
    if not key_id or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key_id):
        return None, "attestation-key-id 不得为空或包含空白/控制字符"
    try:
        public_key = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, ValueError):
        return None, "attestation-public-key-b64 必须是合法 Base64"
    if len(public_key) != 32:
        return None, "Ed25519 attestation 公钥解码后必须恰为 32 字节"
    return {
        "algorithm": "Ed25519",
        "key_id": key_id,
        "public_key_b64": base64.b64encode(public_key).decode("ascii"),
        "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        "private_key_location": "external_to_workspace",
    }, None


def main(argv=None):
    # Windows 控制台输出 UTF-8 中文
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="初始化一个多 Agent 协作讨论工作区（state.json 初始化为 initialized）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("discussion_dir", help="讨论工作区目录路径")
    parser.add_argument("coordinator", help="默认协调者身份")
    parser.add_argument("participants", nargs="+", help="参与者身份名单（至少 1 个）")
    parser.add_argument("--coordinator-platform", required=True,
                        help="协调者实际平台标识，不会由逻辑身份推断")
    parser.add_argument("--coordinator-session", required=True,
                        help="协调者实际会话标识，不会由逻辑身份推断")
    parser.add_argument("--participant-binding", action="append", default=[], metavar="SPEC",
                        help="实际参与方绑定 agent_id=platform_id:session_id；必须对每个参与者（含协调者）恰好指定一次")
    parser.add_argument("--attestation-public-key-b64", required=True,
                        help="平台隔离证明验签用 Ed25519 公钥（Base64）；私钥必须位于项目工作区之外")
    parser.add_argument("--attestation-key-id", required=True,
                        help="平台隔离证明验签公钥的稳定标识")
    parser.add_argument("--force", action="store_true",
                        help="已存在 state.json 时强制重新初始化")
    parser.add_argument("--discussion-id", default=None,
                        help="讨论 ID（默认取讨论目录名，全讨论唯一且不变）")
    parser.add_argument("--coordinator-timeout", type=int,
                        default=DEFAULT_COORDINATOR_TIMEOUT,
                        help="协调者超时（秒）")
    parser.add_argument("--participant-timeout", type=int,
                        default=DEFAULT_PARTICIPANT_TIMEOUT,
                        help="普通参与者等待时间（秒）")
    parser.add_argument("--disposition", "--proposal-disposition", dest="disposition", choices=("archive", "delete"),
                        default=DEFAULT_DISPOSITION,
                        help="提案处置策略")
    parser.add_argument("--template", default=None,
                        help="项目上下文模板路径（默认脚本上级目录 assets/ 下）")
    parser.add_argument("--min-response-rounds", type=int, default=1,
                        help="自动交叉回应最少轮数")
    parser.add_argument("--max-response-rounds", type=int, default=2,
                        help="自动交叉回应最多轮数")
    parser.add_argument("--max-repair-attempts", type=int, default=3,
                        help="可自动修复的最多尝试次数")
    args = parser.parse_args(argv)

    # ---- 校验参数 ----
    participants, coordinator, err = normalize_identity_list(
        args.participants, args.coordinator)
    if err:
        print("FAIL: " + err, file=sys.stderr)
        return EXIT_VALIDATION
    coordinator_platform = args.coordinator_platform.strip()
    coordinator_session = args.coordinator_session.strip()
    if not coordinator_platform or not coordinator_session:
        print("FAIL: 协调者平台标识与会话标识不得为空", file=sys.stderr)
        return EXIT_VALIDATION
    participant_bindings, binding_error = parse_participant_bindings(
        args.participant_binding, participants, coordinator,
        coordinator_platform, coordinator_session,
    )
    if binding_error:
        print("FAIL: " + binding_error, file=sys.stderr)
        return EXIT_VALIDATION
    isolation_trust, trust_error = parse_attestation_trust(
        args.attestation_public_key_b64, args.attestation_key_id)
    if trust_error:
        print("FAIL: " + trust_error, file=sys.stderr)
        return EXIT_VALIDATION
    if args.coordinator_timeout <= 0 or args.participant_timeout <= 0:
        print("FAIL: 超时时间必须为正整数", file=sys.stderr)
        return EXIT_VALIDATION
    if args.min_response_rounds < 1 or args.max_response_rounds < args.min_response_rounds or args.max_repair_attempts < 1:
        print("FAIL: 回应轮数或修复尝试次数非法", file=sys.stderr)
        return EXIT_VALIDATION

    discussion_dir = os.path.abspath(args.discussion_dir)
    internal_dir = os.path.join(discussion_dir, ".multiagent")
    state_path = os.path.join(internal_dir, "state.json")

    legacy_layout = (
        "state.json", "instructions", "receipts", "runtime", "proposals",
        "responses", "archive", "audit", "views", "deliverables",
        ".workflow-transactions", ".state.lock",
    )
    existing_legacy = [name for name in legacy_layout if os.path.exists(os.path.join(discussion_dir, name))]
    if existing_legacy:
        print(
            "FAIL: 检测到不受支持的根目录旧布局；请先由维护者安全处理这些路径: "
            + ", ".join(existing_legacy),
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    # ---- 幂等：已存在内部 state.json 时拒绝重复初始化 ----
    if os.path.exists(state_path):
        if not args.force:
            print("FAIL: 已存在 state.json（重复初始化被拒绝），如需覆盖请加 --force", file=sys.stderr)
            return EXIT_ALREADY_INIT
        print("INFO: 检测到已有 state.json，--force 强制重新初始化")

    # ---- 模板路径 ----
    template_path = args.template
    if template_path is None:
        template_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "assets",
            "project-context-template.md")
    template_path = os.path.abspath(template_path)
    if not os.path.isfile(template_path):
        print("FAIL: 项目上下文模板不存在: " + template_path, file=sys.stderr)
        return EXIT_IO

    # ---- 创建内部运行目录；项目根仅保留上下文与主讨论 Markdown ----
    subdirs = [
        "deliverables", "instructions", "receipts", "audit", "views",
        os.path.join("runtime", "participant"),
    ]
    for sub in subdirs:
        os.makedirs(os.path.join(internal_dir, sub), exist_ok=True)
    for participant in participants:
        os.makedirs(os.path.join(internal_dir, "instructions", participant), exist_ok=True)
        os.makedirs(os.path.join(internal_dir, "receipts", participant), exist_ok=True)
        os.makedirs(os.path.join(internal_dir, "views", participant, "outputs"), exist_ok=True)

    # ---- 拷贝项目上下文模板为 project-context.md ----
    try:
        shutil.copyfile(template_path, os.path.join(discussion_dir, "project-context.md"))
    except OSError as e:
        print("FAIL: 拷贝 project-context.md 失败: " + str(e), file=sys.stderr)
        return EXIT_IO

    # ---- 创建顶层主讨论文档（以 assets/discussion-template.md 为骨架实例化） ----
    discussion_id = args.discussion_id if args.discussion_id else os.path.basename(discussion_dir)
    now = now_iso()
    discussion_template_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "discussion-template.md")
    discussion_template_path = os.path.abspath(discussion_template_path)
    if not os.path.isfile(discussion_template_path):
        print("FAIL: 讨论文档模板不存在: " + discussion_template_path, file=sys.stderr)
        return EXIT_IO
    date_str = datetime.fromisoformat(now).strftime("%Y-%m-%d")
    discussion_doc_name = discussion_id + "讨论文档_" + date_str + ".md"
    try:
        with open(discussion_template_path, "r", encoding="utf-8") as f:
            doc_content = f.read()
        # 仅实例化标题与头部元数据，正文占位符保留给后续合并/审计过程填写
        doc_content = doc_content.replace(
            "# <当前讨论子项目名称>讨论文档_<日期>",
            "# " + discussion_id + "讨论文档_" + date_str)
        doc_content = doc_content.replace("- 日期：<YYYY-MM-DD>", "- 日期：" + date_str)
        doc_content = doc_content.replace("<expected_participants 身份列表>", ", ".join(participants))
        doc_content = doc_content.replace("<coordinator 身份>", coordinator)
        with open(os.path.join(discussion_dir, discussion_doc_name), "w", encoding="utf-8") as f:
            f.write(doc_content)
    except OSError as e:
        print("FAIL: 写入主讨论文档失败: " + str(e), file=sys.stderr)
        return EXIT_IO

    # ---- 构建并写入 state.json ----
    state = build_state(
        discussion_id, participants, coordinator, now,
        coordinator_platform, coordinator_session,
        participant_bindings,
        args.coordinator_timeout, args.participant_timeout, args.disposition,
        isolation_trust)
    state["retry_policy"] = {"max_attempts": args.max_repair_attempts}
    state["convergence"] = {
        "min_response_rounds": args.min_response_rounds,
        "max_response_rounds": args.max_response_rounds,
        "no_new_issue_rounds": 1,
    }
    state["content_authority"] = {
        "discussion_path": discussion_doc_name,
        "sha256": sha256_file(os.path.join(discussion_dir, discussion_doc_name)),
        "updated_at": now,
    }
    # Publish the project-scoped Runtime once, then issue one immutable bootstrap
    # instruction per participant. This is distribution only; a platform adapter
    # (or Rainier's one-time activation) is still required to wake a real session.
    try:
        manifest = publish_runtime(Path(discussion_dir))
        state["runtime_distribution"] = {
            "version": manifest.runtime_version,
            "protocol_version": manifest.protocol_version,
            "manifest_path": ".multiagent/runtime/participant/%s/manifest.json" % manifest.runtime_version,
            "status": "published",
        }
        for sequence, participant in enumerate(participants, start=1):
            instruction_id = "I-%04d-bootstrap-%s" % (sequence, participant)
            manifest_source = Path(discussion_dir) / state["runtime_distribution"]["manifest_path"]
            input_paths = publish_inputs(
                Path(discussion_dir),
                participant,
                [
                    (Path(discussion_dir) / "project-context.md", "project-context.md"),
                    (manifest_source, "runtime-manifest.json"),
                ],
                instruction_kind="bootstrap",
            )
            participant_output = output_path(Path(discussion_dir), participant, "bootstrap")
            participant_scope = access_scope(Path(discussion_dir), participant)
            participant_scope["attestation_verifier"] = {
                "key_id": isolation_trust["key_id"],
                "algorithm": "ed25519",
                "public_key_b64": isolation_trust["public_key_b64"],
            }
            payload = {
                "instruction_id": instruction_id,
                "discussion_id": state["discussion_id"],
                "sequence": 1,
                "kind": "bootstrap",
                "agent_id": participant,
                "platform_id": participant_bindings[participant]["platform_id"],
                "session_id": participant_bindings[participant]["session_id"],
                "runtime_version": manifest.runtime_version,
                "state_revision": state["revision"],
                "task_prompt": build_task_prompt(
                    "bootstrap", participant, coordinator, input_paths, participant_output
                ),
                "input_paths": input_paths,
                "output_path": participant_output,
                "access_scope": participant_scope,
                "attempt": 1,
                "max_attempts": args.max_repair_attempts,
                "issued_at": now,
            }
            if participant == "openclaw":
                payload["operational_directive"] = render_openclaw_operational_directive(
                    Path(discussion_dir), discussion_id, coordinator
                )
            payload["sha256"] = __import__("hashlib").sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            write_instruction(Path(discussion_dir), Instruction.from_dict(payload))
            state["instruction_queue"][participant].append(instruction_id)
    except Exception as e:
        print("FAIL: 发布项目级 Participant Runtime 或 bootstrap 指令失败: " + str(e), file=sys.stderr)
        return EXIT_IO
    try:
        write_state_atomic(state_path, state)
    except OSError as e:
        print("FAIL: 写入 state.json 失败: " + str(e), file=sys.stderr)
        return EXIT_IO

    print("OK: 讨论工作区初始化完成: " + discussion_dir)
    print("    discussion_id: " + state["discussion_id"])
    print("    阶段: " + state["stage"])
    print("    参与者: " + ", ".join(state["expected_participants"]))
    print("    协调者: " + state["coordinator"])
    print("    协调者绑定: %s / %s" % (coordinator_platform, coordinator_session))
    print("    参与者绑定数: " + str(len(participant_bindings)))
    print("    提案处置: " + state["proposal_disposition"])
    print("    主讨论文档: " + discussion_doc_name)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
