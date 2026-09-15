# 一、项目级 Participant Runtime 启动指令

> 协调者为每位参与者分别填写本模板。它用于明确一次项目激活，不是完整 Skill 安装包；每条后续不可变指令也必须在 Runtime 中携带完整的 task_prompt。

## （一）双方身份绑定

- 我的逻辑身份：agent_id=<...>；role=participant
- 我的平台身份：platform_id=<明确的平台 ID>；session_id=<明确的会话 ID>
- 协调者逻辑身份：agent_id=<...>；role=coordinator
- 协调者平台身份：platform_id=<明确的平台 ID>；session_id=<明确的会话 ID>
- discussion_id：<稳定 ID>

agent_id 是逻辑身份，不可用平台名称或会话标识代替。协调者必须显式绑定身份；不得默认或推断为 Claude。

## （二）项目入口与工作区

- 共享工作区：`<workspace-absolute-path>`
- Runtime 版本：`<runtime-version>`
- 自己的指令目录：`<workspace>\.multiagent\instructions\<agent_id>\`
- 自己的回执目录：`<workspace>\.multiagent\receipts\<agent_id>\`
- Runtime 清单：`<workspace>\.multiagent\runtime\<runtime-version>\manifest.json`
- 自己的产物目录：`<workspace>\.multiagent\participants\<agent_id>\`
- 本次完整 task_prompt：<填写具体目标、必要背景、允许操作、产物要求与验收条件，不得只填 kind 或路径>

完整 Skill 仅由协调者使用。先核验项目 Runtime 清单，再执行写给自己且校验有效的指令。

## （三）输入隔离与允许路径

- 独立输入视图：`<sealed-view-id>`；清单 SHA-256：`<hash>`
- 平台沙箱配置：`<sandbox-profile>`；执行证明：`<evidence-path>`
- 只读白名单：`<精确路径列表>`
- 写入白名单：`<自己的产物与回执精确路径列表>`

提示词不是安全边界。必须由密封输入视图、平台沙箱和路径白名单限制实际访问。只有普通同一 Windows 用户 ACL、没有可核验的执行证据时，必须报告隔离未验证，不得标记为严格独立样本；无法建立隔离时先阻塞，不得继续宣称独立。

## （四）指令执行

每条不可变指令包含 `task_prompt`、指令类型、输入视图、允许读写路径、尝试次数和验收条件。`task_prompt` 必须说明目标、步骤、预期产物与验收方式；不得只列 kind 或文件路径。

1. 收到一次平台原生唤醒或 Rainier 的显式激活后，读取自己的原始指令与 Runtime 清单。
2. 核验指令、视图哈希、沙箱和路径白名单；仅按完整 task_prompt 执行一次。
3. 仅写入白名单内自己的产物和回执；产物写入后由协调者执行一次门禁校验。
4. 写入有效 `completed` 或 `failed` 回执。回执是执行证据，聊天回复不是。

平台支持本地命令时，可使用已发布 Runtime 的参与者入口；如果无法执行本地命令或核验隔离，写明限制并阻塞，不得伪造成功回执。

## （五）事件与唤醒约定

参与者无需启动通用常驻轮询脚本。协调者在回执或产物写入后执行一次门禁；经验证的平台原生唤醒可用于通知下一项任务。没有可用入口时，由 Rainier 显式激活一次，或诚实保持阻塞。监测只负责观察，不能代替门禁、唤醒或执行。

OpenClaw 例外：其每条指令必须包含完整 `operational_directive`，按该指令创建/核验项目级原生 Automation、读取自身指令、执行、写回执并在 stop 时停止 Automation。此规则仅适用于 OpenClaw，不构成其他平台的通用轮询要求。

## （六）产物格式

独立提案按项目提案模板写作，至少包含任务理解、提案内容、依据、风险与待确认问题，并注明“独立提案阶段未查看其他参与者提案”。交叉回应仅在指令允许读取对应候选或公开讨论材料时进行。
