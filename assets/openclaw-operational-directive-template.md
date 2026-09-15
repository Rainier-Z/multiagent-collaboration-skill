# OpenClaw 参与者操作指令模板

## 一、身份与边界

- `agent_id`、`role`、`platform_id`、`session_id` 必须由协调者在项目指令中显式指定；不得因运行在 OpenClaw 而自行认领协调者，也不得假定协调者是 Claude。
- 工作区：`{{workspace}}`。
- 本项目 Automation 名称：`{{monitor_job_name}}`；只能管理这个项目的任务。
- 协调者身份只以 `.multiagent/state.json` 的显式绑定为准。

## 二、启动或核验原生 Automation

收到属于自己的非 `stop` 项目指令（`bootstrap`、`propose`、`respond` 或 `repair`）后，先核验唯一项目 Automation。使用当前 OpenClaw 版本真实支持的命令；若无法确认命令、路由或前台投递能力，报告 `BLOCKED`，不得声称监测已启动。`stop` 必须由可信 OpenClaw 适配器执行实际移除及签名流程，参与者不可代签。

1. 使用 `openclaw cron list --json`，寻找名称精确匹配 `{{monitor_job_name}}` 的任务。
2. 若已存在，只核验并复用一个任务；将 `automation_job_id`、名称、创建时间及路由核验摘要写入 `.multiagent/receipts/openclaw/automation-job.md`。
3. 若不存在，只有具备必要权限且 Gateway 可连接时才创建任务。将新建的 `automation_job_id`、名称、创建时间及路由核验摘要写入上述回执文件。
4. `--session current` 只用于验证当前会话可见性，不证明长期稳定；长期运行的会话和投递路由必须单独验证。

示例任务提示词必须保持以下闭环语义（实际命令选项以当前 OpenClaw 版本为准）：

```text
每 2 分钟扫描 {{workspace}}。先读 .multiagent/state.json、.multiagent/instructions/openclaw/ 和 .multiagent/receipts/openclaw/。发现属于 openclaw 的未终结指令时，读取完整原始指令及其 task_prompt，按当前阶段、输入范围、写入白名单和验收条件执行；不得只报告状态。仅写 .multiagent/views/openclaw/outputs/ 中获准的提案/回应、自己的回执以及获准的运行证据。执行后写入有效回执，并在当前会话向 Rainier 报告扫描时间、阶段、指令 ID、变化数量与实际执行结果。不得回复 HEARTBEAT_OK，不得修改 .multiagent/state.json、主讨论 Markdown、其他参与者文件或 instructions。
```

任务创建和配置检查成功只证明任务已配置；只有 Automation 实际运行记录、正确前台投递和有效 Runtime 回执共同存在，才能证明端到端执行。

## 三、每轮扫描后的执行闭环

1. **扫描工作区**：读取 `.multiagent/state.json`，再检查 `.multiagent/instructions/openclaw/` 和 `.multiagent/receipts/openclaw/`。
2. **识别指令**：核对指令 ID、状态、哈希、身份、完整 `task_prompt`、输入视图、读写白名单和阶段。
3. **按规则执行**：只执行属于自己的当前有效指令；独立提案阶段不得读取其他提案或主讨论正文。
4. **写入产物与回执**：提案/回应只写获准的 `.multiagent/views/openclaw/outputs/` 路径；回执只写 `.multiagent/receipts/openclaw/`。
5. **前台报告**：每轮扫描后都在当前会话向 Rainier 报告扫描时间、阶段、指令 ID、变化数量和执行结果；无变化也如实报告。

如果扫描只产生后台日志而没有前台投递能力，不得声称它能唤醒对话或完成自动推进；将其报告为能力边界并请求协调者处理。

## 四、停止与禁止事项

- 收到自己的 `stop` 指令后，停止本项目 Automation 的操作由工作区外可信 OpenClaw 适配器实际完成：它必须运行 `openclaw cron rm <automation_job_id>`，随后运行 `openclaw cron list --json` 并确认该 `automation_job_id` 完全不在列表中；目标仍存在但 disabled 也不能视为删除成功。参与者只报告适配器返回的结果，不得自行签署停止证明、声称未核实的移除成功或接触私钥。
- 可信适配器将实际命令、退出码、目标、JSON 输出及带时区的时间保存为工作区 `.multiagent/audit/platform-evidence/` 下真实 JSON，并验证解析后的文件路径仍在解析后的证据根目录内、无 symlink/junction 越界、文件内容和 SHA-256 正确后，用工作区外受保护私钥签名。停止证明及证据 JSON 必须都含与原 stop 指令 `sha256` 一致的 `instruction_sha256`。停止证明还必须含 `discussion_id`、`instruction_id`、`instruction_sha256`、`agent_id`、`platform_id`、`session_id`、`stopped_at`、`mechanism`、`target`、`action_verified`、`proof_reference`、`proof_sha256`，以及 OpenClaw 专有字段 `automation_job_id`、`removal_verified=true`、`removal_checked_at`。证明绑定原 stop 指令与当前讨论，禁止跨讨论重放。回执 `at` 不得晚于当前时间，且必须严格晚于 `removal_checked_at`。
- 若适配器没有返回可验证的签名证明，stop 回执必须报告 `BLOCKED`/`FAILED`，不得把 state 中的 stopped 标志当作实际移除证据。
- 工作区拥有者可以直接篡改 `state.json`；普通文件系统协议不能防御此行为。严格独立依赖工作区外可信适配器及签名和平台强制的隔离证据；平台不能提供时必须阻断，不得降级。本文是操作契约，不代表已在真实 OpenClaw 环境验证。
- 不得停止全局 heartbeat 或其他项目 Automation。
- 不得修改 `.multiagent/state.json`、主讨论 Markdown、其他参与者文件、其他参与者指令或 `runtime/`。
- 不得自行下发/伪造指令、争夺协调权，或把任务创建成功误报为执行完成。
