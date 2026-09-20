# 一、平台唤醒契约

## （一）职责边界

本层定义协调者如何把一次最小唤醒请求投递给显式绑定的平台会话。请求必须包含逻辑 agent_id、platform_id 和 session_id；不得根据默认值推断协调者或目标参与者平台。唤醒只通知会话读取工作区中的不可变指令，不承载完整讨论正文，也不证明参与者已完成。

监测器只观察变化；门禁在每次有效产物或回执写入后由协调者单次运行；适配器负责可能的会话唤醒；Participant Runtime 负责读取完整 task_prompt 并执行。可选常驻看门狗仅处理超时或恢复，不能成为主流程推进依赖。参与者不需要启动通用轮询脚本。

## （二）运行入口

适配器和协调者调用项目脚本时必须使用 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_runtime.ps1 <脚本相对路径> [参数...]`，不得假设 PATH 中存在可用的 `python`。启动器只接受 `scripts/` 范围内的相对路径，强制 Python 3.11+ 与 UTF-8，并在 `export_docx.py` 前验证 `python-docx`；探测失败时返回结构化错误且不会执行目标脚本。解释器探测顺序和显式 `MULTIAGENT_PYTHON` 覆盖规则见根目录 `SKILL.md` 与 `README.md`。

适配器和协调者调用项目脚本时，门禁只能对一个输入事件执行一次。文件监测器是传感器；适配器是可能的唤醒动作层；Participant Runtime 是参与者的受限执行器；协调编排器是流程推进者。这四者不可互相冒充。

## （三）统一契约

公共定义位于 `adapters/common/wake_protocol.py`：

```text
WakeRequest(workspace, agent_id, platform_id, session_id, instruction_id, runtime_version)
→ ActivationResult(status, detail, evidence)
```

WakeRequest 只携带定位字段，不允许携带提案、回应、讨论正文、task_prompt、私密推理、凭据或其他参与者输入。完整 task_prompt 必须写在工作区不可变指令中；收到唤醒的参与者需要读取原始指令后执行。ActivationResult.status 只能是以下三态：

| 值 | 含义 | 后续动作 |
|---|---|---|
| `activated` | 已验证的外部平台 dispatcher 接受最小 handoff 请求 | 等待参与者工作区回执；不可据此认定完成 |
| `manual_activation_required` | 没有已验证且已配置的外部激活入口 | 记录 `E_PLATFORM_UNAVAILABLE`，要求 Rainier 显式启动 |
| `activation_failed` | 已尝试激活但平台明确拒绝或调用失败 | 记录失败证据，不假装重试成功 |

兼容旧编排器的 `WakeAdapter.wake()` 仍可返回 `WakeResult(accepted/unavailable/rejected)`；其映射为 `activated/manual_activation_required/activation_failed`。新调用方应使用平台适配器的 `activate()`，而不是把兼容状态当作执行完成。后台文件扫描永远不会生成 `activated`。

适配器默认不能调用未配置的 API、终端或会话。它们不保存 API Key、Token、密码或内部配置。

## （四）平台状态

| 平台 | 默认实现 | 当前 wake 验证状态 |
|---|---|---|
| Claude Code | `ClaudeCodeWakeAdapter()` | 未配置即 `unavailable`；尚无本协议的独立 handoff 证据 |
| Codex | `CodexWakeAdapter()` | 未配置即 `unavailable`；已设计、未验证 |
| OpenClaw | `OpenClawWakeAdapter()` | 未配置即 `unavailable`；已设计、未验证 |

平台身份与会话路由由初始化时显式绑定，不能从平台名称、账号或当前进程推断。`activated` 只表示平台接受了定位请求；实际执行以目标 agent_id 对应会话写入的有效回执和产物为准。没有经过验证的原生入口时，返回 `manual_activation_required`，或在投递失败时返回 `activation_failed`。

可以向任一适配器显式注入经验证的 dispatcher。只有在保存了独立端到端证据后，才允许 dispatcher 返回 `activated`（旧 `WakeResult("accepted")` 会被兼容映射）。该状态仅表示投递成功，仍必须由 Runtime 的 `accepted`、`completed` 或 `failed` 回执证明实际执行。

### 1. OpenClaw 的原生 Automation 义务

OpenClaw 参与者支持以其原生 Automation/cron 进行项目级轮询，但这属于**参与端的执行义务**，不是协调者的 direct wake 能力。协调者发布给 OpenClaw 的每一条不可变指令（`bootstrap`、`propose`、`respond`、`repair`、`stop`）都必须含完整的 `operational_directive`：

1. 指定项目工作区、`agent_id=openclaw`、指令 ID、允许读写范围和停止条件；
2. 要求收到任意 OpenClaw 指令时创建或核验唯一的项目级 Automation，并将其 `automation_job_id`、任务名、创建时间及核验摘要持久写入 `.multiagent/receipts/openclaw/automation-job.md`；已存在的有效任务不得重复创建；
3. 要求每次 Automation 触发时读取 `.multiagent/state.json`、`.multiagent/instructions/openclaw/` 与 `.multiagent/receipts/openclaw/`，发现自身未终结指令即按 Runtime 执行，而不是只报告扫描状态；
4. 要求执行后只写自己的产物、回执和允许的扫描证据，并在前台报告扫描时间、阶段、指令 ID 与执行结果；
5. 要求收到自己的 `stop` 指令、Rainier 明确停止，或进入 `monitoring_stopped`（仅显式停止/旧工作区兼容态，不是正常交付阶段）时，仅由可信 OpenClaw 适配器按已保存的 `automation_job_id` 停止本项目任务；参与者不得签名。适配器执行下述停止验证并保存证据，参与者只写自己的 stop 回执。

**候选交付后的 stop 门禁**：候选 Word 成功打开后，协调者必须向每个参与者身份各发布唯一 stop 指令。停止证明 payload 与引用的证据 JSON 都必须包含 `instruction_sha256`，且等于原 stop 指令的 `sha256` 字段；payload 还必须完整包含统一字段：`discussion_id`、`instruction_id`、`instruction_sha256`、`agent_id`、`platform_id`、`session_id`、`stopped_at`、`mechanism`、`target`、`action_verified`、`proof_reference`、`proof_sha256`。OpenClaw 另含 `automation_job_id`、`removal_verified: true`、带时区的 `removal_checked_at`。可信 OpenClaw 适配器必须实际运行 `openclaw cron rm <automation_job_id>`，再运行 `openclaw cron list --json`；只有目标 `automation_job_id` 完全不在列表中才算删除成功，仍存在但 disabled 也失败。把实际命令结果、退出码、目标及时间写入工作区 `.multiagent/audit/platform-evidence/` 下真实 JSON。`proof_reference` 指向该 JSON，`proof_sha256` 是其原始字节的 SHA-256。验签前须验证文件解析后的最终路径仍位于解析后的证据根目录内，拒绝 symlink/junction 越界，并验证文件存在且为有效 JSON，哈希、内容、目标、指令哈希、讨论/指令/会话绑定和时序均正确；禁止跨讨论重放。`receipt.at` 不得晚于当前验证时间；OpenClaw stop 回执时间必须严格晚于 `removal_checked_at`。全部验证通过后，可信适配器才可从工作区外读取受保护的项目私钥生成 Ed25519 签名（`key_id`、`algorithm="ed25519"`、`signature_b64`），签名覆盖契约字段与证据哈希。OpenClaw 参与者不得读取私钥、自行签名或以自述替代实际核验。唯一 `completed` 回执必须引用签名证明。只有全部参与者的唯一、有效 completed 回执均通过校验后，协调者才能把项目监测标记为 stopped 并进入 `user_confirmation`。state.json 的 stopped 标志不是 Automation 已停止的证据；OpenClaw 未能移除任务、缺少签名/字段/证明引用或无法核验时回执必须 failed/blocked，流程不得进入用户确认阶段。

`operational_directive` 只能解释或执行已下发的指令；不得自行生成下一条指令、修改 `state.json`、争夺协调权或把后台扫描视为完成证据。使用 `session=current` 的任务仅适合端到端演习：它绑定创建时会话，不能单独证明长期稳定的身份或投递路由。长期运行须显式验证会话/频道路由与运行历史。

即使 OpenClaw 已成功建立原生 Automation，`OpenClawWakeAdapter().activate()` 仍默认返回 `manual_activation_required`，直到独立验证“协调者最小 handoff → 正确 OpenClaw 会话 → 读取工作区原始指令 → 有效回执 → 可见运行记录”的端到端链路。原生轮询不能被表述为协调者已具备 direct wake，也不能把扫描结果当成 `activated`。

> 新协议覆盖说明：上述 stop 证明门禁适用于正式结论发布后的 `finalizing` 阶段；候选 Word 审阅期间保持监测，旧工作区才使用 `user_confirmation` 兼容路径。

## （五）验证门槛

某平台的 wake 能力要标记为已验证，必须有可复现证据证明：

1. 平台接收的内容只有最小 `WakeRequest` 定位字段。
2. 被唤醒会话读取共享工作区的指定指令，而不是聊天消息中的正文。
3. 会话写出了属于自身目录、时序正确且哈希有效的回执。
4. 未配置或失败时产生 `manual_activation_required`/`activation_failed`，不会制造成功回执或推进阶段。
5. 重复投递同一指令不造成重复产物或额外状态修订。
6. 独立阶段使用密封输入视图、平台强制沙箱与精确读写白名单，并保留访问执行证据；普通同一 Windows 用户 ACL 或提示词声明不足以证明严格独立。

对于 OpenClaw，另外必须保存原生 Automation 的任务 ID、计划触发时间、实际运行历史、前台投递路由以及对应 Runtime 回执；仅有任务创建成功、后台日志或名称相同均不构成验证证据。

在上述证据出现前，本文件和适配器代码都不得宣称自动唤醒已经可用。

本文规定的是适配器契约，不是实测报告；不得声称已在任何真实 OpenClaw 环境验证。

## （六）隔离 Attestation 信任锚

使用 `scripts/attestation_keys.py provision --workspace <工作区> --private-key-path <工作区外绝对 PEM 路径> --key-id <id>` 生成 key material，再把 JSON 输出的 `public_key_b64` 和 `key_id` 传给初始化器。工作区仅保存公钥/指纹，私钥始终在工作区外。每个验签进程必须设置非秘密环境变量 `MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON`，其值是 `{"key-id":"public-key-b64"}` 形式的公开 JSON 对象；缺失、无效、未 pin 的 key 或签名不匹配均 fail-closed。平台 attestation 应签署输入视图清单哈希、执行会话身份、沙箱与读写白名单摘要、实际读取/拒绝访问证据及执行时间；验证器检查签名、公钥 key ID、讨论/指令绑定及新鲜时序。私钥只交由可信平台适配器，绝不交给参与者。OpenClaw 具体停止签名字段与门禁见本节上一段。配置公钥不代表相应平台已实现或验证了 attestation。
使用 `scripts/attestation_keys.py provision --workspace <工作区> --private-key-path <工作区外绝对 PEM 路径> --key-id <id>` 生成 key material，再把 JSON 输出的 `public_key_b64` 和 `key_id` 传给初始化器。工作区仅保存公钥/指纹，私钥始终在工作区外。每个验签进程必须设置非秘密环境变量 `MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON`，其值是 `{"key-id":"public-key-b64"}` 形式的公开 JSON 对象；缺失、无效、未 pin 的 key 或签名不匹配均 fail-closed。平台 attestation 应签署输入视图清单哈希、执行会话身份、沙箱与读写白名单摘要、实际读取/拒绝访问证据及执行时间；验证器检查签名、公钥 key ID、讨论/指令绑定及新鲜时序。私钥只交由工作区外可信平台适配器，绝不交给参与者。普通文件系统协议无法防止工作区拥有者直接篡改 `state.json`；严格独立性依赖工作区外可信适配器、受保护的签名密钥与平台强制的沙箱/访问证据。平台无法提供这些独立证据时必须阻断严格门禁，不得降级。OpenClaw 具体停止签名字段与门禁见本节上一段。配置公钥不代表相应平台已实现或验证了 attestation。
