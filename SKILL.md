---
name: multiagent-collaboration
description: 多个平等、独立的 AI 参与者需要围绕同一议题形成独立提案、交叉回应与候选决策时使用。协调者在共享项目工作区发布轻量 Participant Runtime 和指令队列；参与者不安装完整 Skill。用于独立意见协商，不用于工程实施流水线。
---

# 一、Multiagent Collaboration Skill

## （一）适用范围

本 Skill 用于围绕同一议题收集独立提案、交叉回应和候选决策，并由 Rainier 作最终确认。完整 Skill 只由协调者安装；参与者通过项目级 Runtime 与不可变指令工作，不安装完整 Skill。

## （二）权威与角色

- 主讨论 Markdown 是讨论与决策内容的权威；`.multiagent/state.json` 是机器流程账本；候选与正式 Word 都是各自阶段的只读快照。
- 协调者只能在启动时显式绑定自己的 `agent_id`、`role`、`platform_id`、`session_id`；不得把默认平台、账号或进程推断为协调者身份。
- 每位参与者分别绑定 `agent_id`、`role`、`platform_id`、`session_id`。`agent_id` 是逻辑身份，平台和会话是投递身份，不可互相替代。
- 每条不可变指令都必须包含可直接执行的完整 `task_prompt`、输入视图及其校验信息、读写白名单和验收条件；`kind` 与路径只是索引，不能代替任务说明。

## （三）事件驱动流程

共享工作区由 Event Stream 记录不可变事实，业务文件保存可读数据，`state.json` 只保存当前快照；三者不可互相替代。每次状态变化遵循“验证前置条件 → 写业务产物/回执 → append event → 原子更新 state.revision”的事务顺序，崩溃后由事件日志和事务日志恢复，无法证明一致时 fail-closed。协调者单次运行门禁；门禁不依赖监测器自动推进，也即门禁不得依赖常驻流程监测脚本。

`monitor_discussion.py` 是只读文件传感器；`participant_monitor.py` 是每个参与 Agent 可独立运行的持久 Event Consumer。后者读取并校验哈希链，从 `.multiagent/monitors/<agent_id>/cursor.json` 恢复位置，按 event_id 幂等处理相关事件，并把激活结果持久化；cursor 与事件流不一致时 fail-closed。Activation Bridge 才回答“已有会话能否继续执行”，结果只能是 `activated`、`manual_activation_required` 或 `activation_failed`。扫描不能冒充唤醒成功。Participant Runtime 负责读取完整指令、执行任务并回写产物与回执。

讨论状态只使用以下正常阶段：

~~~text
initialized → independent_proposal → cross_response(round=N) → candidate_decision
→ human_review → finalizing → confirmed_decision → delivered
~~~

讨论交付顺序固定为：

1. 协调者也以 `participant:<coordinator>` 身份独立提交 proposal；所有 proposal 完成前不得读取其他提案。
2. 每一轮只用文件/receipt 判断回应是否齐全；只有“是否继续下一轮”交给协调者承担的受约束 Semantic Convergence 职责。它读取完整 round snapshot，输出固定 JSON；这不是新增的独立 Agent。Python 只做 schema、轮次和参与者引用校验。
3. 本轮完成必须通过一个原子 Round transition：不可变 `round-N.md`、下一轮 instructions（若需要）、`state.round`/`state.revision` 和 `round_completed` event 一并提交；事件、产物或 state 无法一致恢复时阻断。
4. 收敛后生成候选 Markdown 和候选 Word，并自动打开候选 Word；候选 Word 打开成功后进入 `human_review`，参与者监测保持运行。
5. Rainier 查看候选 Word 并确认；确认脚本先把正式结论发布到 Markdown，阶段进入 `finalizing`。
6. 仅在正式结论发布后下发 stop；收齐唯一、绑定、有效的 stop 证明后停止监测，进入 `confirmed_decision`，再生成正式 Word。
7. 正式 Word 打开成功才进入 `delivered`；失败时保留 `confirmed_decision`。旧工作区仍兼容 `user_confirmation` 和旧停机门禁。

候选内容与正式决策必须分别标识、分别留痕；候选 Word 不能冒充正式交付物。确认、修改或拒绝候选的具体状态转换见 `references/collaboration-protocol.md`。

## （四）独立性与工作区

独立阶段为每位参与者提供密封、只读的输入视图，并由路径白名单限制实际读取和写入。新讨论默认 `security_mode=normal`：共享文件协议和事件审计可用，但不宣称硬隔离或严格独立；需要严格证明时显式使用 `--security-mode strict`，再启用工作区外 Ed25519 attestation、平台沙箱和 stop proof。严格模式的验签失败或证据缺失必须阻断，不得降级。提示词不是安全边界；同一 Windows 用户下的普通文件 ACL 不足以证明隔离。

项目根目录保持紧凑，只放 `project-context.md`、主讨论 Markdown、`候选决策.docx` 和 `最终决策.docx`。候选 Markdown 与所有清单放在 `.multiagent/deliverables/`；状态、指令、回执、隔离输入视图、提案/回应源件、日志及证据均置于 `.multiagent/`。提案合并成功后默认自动删除临时提案源件，不询问用户；仅在审计区保留路径、SHA-256、处置时间和结果，不创建/复制 archive。显式 archive 兼容选项只原位保留，不复制。`monitoring_stopped` 仅保留给显式停止或旧工作区兼容，不是正常交付阶段。

## （五）平台唤醒

优先使用已经验证的平台原生唤醒；没有可验证入口或投递失败时，诚实记录阻塞，或由 Rainier 做一次显式激活，不得以文件扫描冒充唤醒。参与者不需要启动通用常驻轮询脚本。

OpenClaw 保持专有规则：它收到项目指令后按 `operational_directive` 创建/核验项目级原生 Automation 并执行自身指令。stop 时由工作区外可信 OpenClaw 适配器实际运行 `openclaw cron rm <automation_job_id>`，再运行 `openclaw cron list --json` 并确认目标任务不在列表中；只变为 disabled 不算删除成功。签名 attestation 与证据 JSON 都须绑定 `instruction_sha256`，回执时间须晚于 `removal_checked_at`；参与者自身不得签名或接触私钥。此协议文档不是实测报告，不表示已在真实 OpenClaw 环境验证。这是 OpenClaw 参与端机制，不是通用 Participant Runtime 要求，也不证明协调者 direct wake 可用。详见 `references/platform-adapters.md`。

## （六）操作入口与详细规范

协调者对每次已写入的产物或回执调用一次门禁：

~~~text
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_runtime.ps1 orchestrate_discussion.py `
  --workspace <目录> --once --actor <agent_id> `
  --platform-id <platform_id> --session-id <session_id>
~~~

协调者身份必须由当前真实执行会话显式传入；命令格式为 `--actor <agent_id> --platform-id <platform_id> --session-id <session_id>`，与工作区 `.multiagent/state.json` 中的 `coordinator_binding` 不一致时必须拒绝执行。

初始化前先用 `scripts/attestation_keys.py provision` 生成一次性 Ed25519 密钥资料：私钥写到工作区外的显式 PEM 路径；项目只接收公钥和 key ID。每个运行验签的进程都必须设置公开信任表 `MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON`，格式为 `{"key-id":"public-key-b64"}`；它不是秘密。缺失、JSON 无效、key ID 不匹配或验签失败都必须 fail-closed。私钥只交给可信平台适配器，绝不交给参与者。最短 PowerShell 命令见 README 的“安全初始化”。

可选看门狗不能成为主流程推进依赖。Runtime 字段、状态机、隔离证据与平台契约分别见 `references/collaboration-protocol.md`、`references/state-schema.md` 和 `references/platform-adapters.md`；讨论及启动模板见 `assets/`。
