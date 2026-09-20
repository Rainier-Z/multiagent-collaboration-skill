# 一、<当前讨论子项目名称>讨论记录

> 主讨论 Markdown 是内容权威；候选决策仅供 Rainier 审阅，不构成正式决策。

## 一、项目与身份

- 日期：<YYYY-MM-DD>
- discussion_id：<稳定 ID>
- 协调者：agent_id=<显式逻辑身份>；role=coordinator；platform_id=<平台>；session_id=<会话>
- 协调者参与身份：同一 agent_id；role=participant；platform_id/session_id 与协调者绑定一致；必须独立提交 proposal
- 参与者：<逐人列出 agent_id、role、platform_id、session_id>
- 主讨论 Markdown：<工作区根目录路径>
- 审计时间线：门禁、唤醒、执行、候选、停止、确认和交付事件追加于 `.multiagent/audit/`。

## 二、独立提案

每位参与者在独立阶段只能读取自己的密封输入视图；提示词或自我声明不构成隔离证明。提案源件位于 `.multiagent/views/<agent_id>/outputs/提案文档.md`，严格独立状态须有平台沙箱与访问证据。

<经门禁验证后按 agent_id 汇入提案及来源哈希；不得复制提案归档。>

提案合并处置记录：<默认逐个删除临时提案源件；仅在 `.multiagent/audit/` 写入路径、SHA-256、处置时间及结果；显式 archive 兼容模式只原位保留，不复制>

## 三、交叉回应

回应源件位于 `.multiagent/views/<agent_id>/outputs/round-<N>/交叉回应文档.md`，只在该阶段向参与者开放指令明确允许的公共材料；文件/receipt 只判断本轮是否齐全，是否继续下一轮由 Convergence Evaluator 判断。

<按 agent_id 汇入回应及来源哈希。>

## 四、结构化决策包

### 共识事项

<共识及依据>

### 分歧事项

<分歧及各方立场>

### 候选方案

<候选方案、支持理由、风险与代价>

### 待 Rainier 选择的问题

<如无则写“无”>

## 五、候选决策与 Word 审阅

**状态：候选、待 Rainier 审阅，不是正式决策。**

- 候选 ID：<ID>
- 候选 Markdown：`.multiagent/deliverables/candidate.md`
- 候选 Markdown SHA-256：<哈希>
- 候选 Word：`候选决策.docx`
- 候选 Word SHA-256：<哈希>
- 系统打开成功及时间：<opened / opened_at；失败时写阻塞>
- 候选审阅阶段：human_review；监测保持 active，尚未停止
- 正式结论发布阶段：finalizing；随后才向每位参与者发布 stop 指令
- 每位参与者 stop 指令 ID 与目标会话：<逐一列出，不得省略>
- 每位参与者唯一 completed stop 回执 ID、路径及哈希：<逐一列出>
- OpenClaw 本项目 Automation 移除核验：<job_id、移除时间、核验记录路径/哈希；未参与则写“不适用”>
- 全部 stop 回执核验完成时间：<时间；未收齐时阻断>
- 常规监测停止记录时间：<monitoring.stop_requested_at；正式结论发布且所有唯一 completed 回执通过后才可填写>
- 备注：候选 Word 仅供审阅；Rainier 的明确确认才代表用户确认。

## 六、确认固化记录

Rainier 查看候选 Word 后，确认会先发布正式结论并进入 finalizing；随后协调者执行 stop 门禁。只有每位参与者的 stop 回执均唯一且为 completed、OpenClaw Automation 移除证据有效、监测确已停止后，才进入 confirmed_decision。仅修改 state.json 不能证明实际停止。

- 候选 ID：<ID>
- 明确确认原文：<Rainier 原文>
- 确认时间：<YYYY-MM-DD HH:mm，Asia/Shanghai>
- 记录者：<协调者 agent_id>
- 确认固化的主讨论 Markdown 修订号：<revision>
- 正式决策与候选差异：<无 / 列明>

## 七、正式 Word 交付

- 正式 Word：`最终决策.docx`
- 源主讨论 Markdown SHA-256：<哈希>
- 正式 Word SHA-256：<哈希>
- 系统打开成功及时间：<opened / opened_at；失败时必须保持 confirmed_decision>
- 最终阶段：<delivered 或明确阻塞原因>
- 完成时间：<YYYY-MM-DD HH:mm>
