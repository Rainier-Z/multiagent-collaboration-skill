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

产物或回执写入后，由协调者执行一次门禁编排：校验、决定可执行的下一步并在需要时请求一次平台唤醒。门禁不得依赖常驻流程监测脚本。文件监测只负责报告变化；可选看门狗只用于超时检测或恢复。平台唤醒负责激活指定会话；Participant Runtime 负责读取完整指令、执行任务并回写产物与回执。四者职责不可混同。

讨论状态只使用以下唯一正常阶段：

~~~text
initialized → independent_proposal → cross_response → candidate_decision
→ user_confirmation → confirmed_decision → delivered
~~~

讨论交付顺序固定为：

1. 收敛意见。
2. 生成候选 Markdown，并明确标注为待审候选，不能作为正式决策。
3. 从候选 Markdown 生成候选 Word。
4. 只有候选 Word 自动打开成功，才逐一向所有参与者发布唯一 `stop` 指令；收齐每个绑定会话的唯一 `completed` 回执和有效、已签名的停止证明后，才记录常规监测停止并进入 `user_confirmation`。停止证明及其引用的证据 JSON 都必须含 `instruction_sha256`，且与原 stop 指令的 `sha256` 一致；`proof_reference` 解析后的最终路径必须仍位于工作区 `.multiagent/audit/platform-evidence/` 内，拒绝 symlink/junction 逃逸，并验证哈希、内容与时序。回执时间不得晚于当前验证时间；OpenClaw 的 stop 回执时间必须晚于 `removal_checked_at`。OpenClaw 还须证明原生 Automation 已从 `openclaw cron list --json` 结果中完全消失；仍存在但 disabled 不算删除成功。单改 state 字段不等于实际停止；任何回执缺失或无效时停留在 `candidate_decision`。
5. 等待 Rainier 查看候选 Word 并作明确确认；`confirm_decision.py` 必须校验候选打开证据和监测停止状态。
6. 仅在确认后把决策固化到主讨论 Markdown，并进入 `confirmed_decision`。
7. 生成并自动打开正式 Word；只有打开成功才进入 `delivered`，失败时停留在 `confirmed_decision`。

候选内容与正式决策必须分别标识、分别留痕；候选 Word 不能冒充正式交付物。确认、修改或拒绝候选的具体状态转换见 `references/collaboration-protocol.md`。

## （四）独立性与工作区

独立阶段为每位参与者提供密封、只读的输入视图，并由平台沙箱及路径白名单限制实际读取和写入。平台 attestation 必须由工作区外可信适配器使用工作区外私钥签名，并使用初始化配置的项目公钥验签；工作区不得保存私钥。威胁模型不包含工作区拥有者直接篡改 `state.json` 的情形：普通文件系统协议无法阻止拥有者改写账本，签名只证明可信适配器提交的证据，不能单独防止拥有者伪造/回滚工作区状态。严格独立性依赖工作区外可信适配器、受保护签名密钥与平台强制的沙箱/访问证据；平台不能提供时必须阻断，不得降级。提示词不是安全边界；同一 Windows 用户下的普通文件 ACL 不足以证明隔离。验签失败或无法取得沙箱和访问执行证据时，必须阻断严格独立性门禁。公钥配置不代表任一平台已经支持或通过端到端验证。

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
