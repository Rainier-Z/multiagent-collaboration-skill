# OpenClaw Participant Adapter

## 一、状态

本目录的 wake 契约处于“已设计、未验证”状态。`OpenClawWakeAdapter()` 在没有已验证 dispatcher 时返回 `unavailable`；不得因为存在工作区扫描脚本而声称能自动唤醒 OpenClaw 会话。

## 二、部署方式

完整 Skill 只在协调者侧安装。项目初始化时，协调者把轻量 Runtime 发布到共享工作区，并为 OpenClaw 参与者创建专属指令和回执目录。参与者只完成该项目的一次 Runtime 激活，不安装全局 Skill。

每条发给 OpenClaw 的不可变指令都必须包含 `operational_directive`。它要求 OpenClaw 将本项目的原生 Automation/cron 作为参与端义务运行：收到任意指令时创建或核验一个唯一任务，并把 `job_id`、任务名、创建时间及路由核验摘要保存到 `receipts/openclaw/automation-job.md`；每次触发时扫描自身指令、执行未终结动作、写入自身回执并在前台报告；停止时只读取该文件中的 `job_id` 并删除对应的本项目任务。任务名不能作为删除定位依据，重复 bootstrap 也不得创建重复任务。

## 三、唤醒语义

适配器只接收 `WakeRequest` 定位字段。任何真实 dispatcher 都必须经过独立端到端验证，且不得接收提案、回应或主讨论正文。`activate()` 返回 `activated` 也只能说明 handoff 已投递；完成仍由 Runtime 回执和输出哈希证明。没有 dispatcher 时返回 `manual_activation_required`，调用失败时返回 `activation_failed`。

未配置 dispatcher 时，协调者应记录 `E_PLATFORM_UNAVAILABLE` 并由 Rainier 显式启动参与者一次。传感器只能报告变化，不能完成唤醒。

OpenClaw 的原生 Automation 与本适配器的 direct wake 是两条独立链路：前者使已配置的 OpenClaw 参与者定期检查共享工作区，后者是协调者向一个既有会话投递最小 `WakeRequest` 的能力。前者存在、任务创建成功或后台扫描有输出，都不能把后者标记为 `activated` 或可用。默认状态继续是 `manual_activation_required`，直至保存端到端验证证据。

## 四、Automation 执行门槛

`operational_directive` 至少应要求下列可审计行为：

1. 每次触发先读取 `state.json`、`instructions/openclaw/` 与自己的回执；
2. 对自身的新 `bootstrap`、`propose`、`respond`、`repair` 或 `stop` 指令立即调用受限 Runtime；不得只汇报扫描结果；
3. 只写自己允许的提案、回应、回执与扫描证据；每轮前台报告扫描时间、阶段、指令 ID 和执行结果；
4. Automation 创建失败、路由不明确、运行失败或权限不足时写出 `BLOCKED`/失败证据并前台报告，不能宣称已自动化；
5. `stop`、`monitoring_stopped` 或 Rainier 明确停止时，按保存的 `job_id` 停止本项目任务并写 stop 回执。

`session=current` 可用于演习当前对话的投递，但必须验证实际路由和运行历史；它不是长期稳定身份或前台可见性的充分保证。验证通过前，不得把本目录标注为已支持无人值守自动化。

## 五、参与者边界

独立提案阶段，OpenClaw 只读自己的指令、项目上下文和 Runtime；只写自己的提案和回执。回应阶段需要主讨论 Markdown 时，必须由指令显式列出。不得写状态、主讨论 Markdown、他人目录、Runtime 或指令队列。

## 六、可信停止适配器

候选 Word 打开后，可信协调者侧可通过项目 Python 启动器调用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_runtime.ps1 openclaw_automation.py stop --workspace <工作区> --instruction-id <stop 指令 ID> --private-key <工作区外 PEM 路径>`。适配器会校验 stop 指令、OpenClaw 身份、项目保存的唯一 `job_id` 及指令内的 Ed25519 公钥 pin，然后按原生 `openclaw cron rm <job_id>` 执行移除，并以 `openclaw cron list --json` 核验目标任务已完全不存在。目标仍存在时，无论状态是 enabled、disabled、paused 或 stopped 都属于移除未完成；任一命令失败、JSON 结构含糊、目标仍存在或密钥 pin 不匹配都会 fail closed，不写成功的 removal proof 或签名 attestation。

成功的 removal proof 和签名 attestation 都包含已通过协议校验的 stop instruction 的 `instruction_sha256`，用于将停止证据绑定到确切的不可变指令内容。只有原生列表中找不到保存的目标 `job_id` 时才可生成成功证据。

成功时在 `.multiagent/audit/platform-evidence/openclaw/` 写入 `<instruction-id>-removal-proof.json` 和 `<instruction-id>-stop.json`。停止声明签入证明文件相对路径及 SHA-256；私钥仅从显式给定的工作区外路径读取，不写入工作区或命令输出。自动化 job ID 必须已保存在 `.multiagent/receipts/openclaw/automation-job.md`，可选 `--automation-job-id` 只用于一致性核对，不可覆盖已保存目标。
