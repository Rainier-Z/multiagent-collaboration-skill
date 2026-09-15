# §21 十六个验收情景 —— 测试覆盖说明

> 依据：`结构化需求.md` v0.7（§5 状态闭环 / §8 协调接管 / §9 参与者超时 / §11 提案处置 / §13 决策确认 / §14 监测 / §15 Word 交付 / §21 验收情景）与 `references/state-schema.md`（9 阶段流转、15 最小字段、一致性拒绝规则）。
> 运行器：`evals/run_scenarios.py`（每个情景独立 tempfile 目录，真实调用 scripts/ 下 CLI，断言退出码 / stdout / state.json 阶段与字段）。
> 标注说明：凡标注「人工/协议级」的情景，核心行为由 Skill 约定或人工决策承载，脚本层无对应命令，本套件以「最近的脚本可断言行为」作为替代断言方式，并如实说明缺口。

---

## 情景 1：正常三方独立提案与合并 —— 自动可测（完整）

- **构造**：init 三方（claude/codex/openclaw，协调者 claude）→ 三方各写提案并 submit → 建主讨论文档 → merge。
- **断言**：init 后 stage=initialized、revision=1；提交后 stage=independent_proposal 且三方 submitted；validate `--expect-phase independent_proposal` 退出 0；merge 退出 0、输出"共合并 3 份提案"、stage=cross_response、revision=6；主文档合并 3 个提案区块且带 sha256；归档目录保留 3 份提案 + manifest.json、proposals/ 清空；补齐交叉回应后 validate `--expect-phase cross_response` 退出 0。
- **覆盖**：§5 最小闭环的前半段（initialized→independent_proposal→proposals_complete→cross_response）。

## 情景 2：参与者数量大于三个 —— 自动可测（完整）

- **构造**：四参与者（claude/codex/openclaw/gemini）走完整流水线。
- **断言**：merge 退出 0、stage=cross_response；submission_status 键集覆盖 4 身份；主文档合并 4 个提案区块。
- **覆盖**：§7「参与者数量不限于三个，以身份逐一匹配而非数量字段」。

## 情景 3：同一 Agent 重复提交 —— 自动可测（完整）

- **构造**：init 后 claude 对同一提案文件 submit 两次。
- **断言**：首次退出 0、claude=submitted、revision+1；第二次退出 0（幂等）、revision 不变、expected_participants 与 submission_status 均仍为 3 条（不产生第二个参与者条目）、输出标注"幂等"。
- **覆盖**：§7.3「同身份不重复计数、更新保留同一身份」。

## 情景 4：提案阶段意外读取其他方案（独立性污染→降级声明） —— 部分自动（声明被记录保留），隔离本身为协议级

- **构造**：claude 的提案内含「独立性受限声明」文本，与其他方案一起 submit 并 merge。
- **断言**：含降级声明的提案仍可合并（退出 0）；主文档合并后声明文本原样保留（不抹除）；claude 提案区块已合并。
- **替代断言说明**：§10.3 要求「如实声明独立性受限，由 Rainier 决定保留/重做/仅作回应输入」。**独立性本身（各 Agent 只拿到自己的提案路径）是 Skill/协议级约束，脚本层无强制手段**，本测试只验证「降级声明被如实记录并随合并保留」这一脚本可断言侧面。隔离是否真实生效需人工/Skill 审查验证。

## 情景 5：两个 Agent 同时认领协调权（原子性） —— 自动可测（完整，多进程模拟）

- **构造**：init 默认协调者 codex，将租约回拨到已过期；`multiprocessing.Barrier(2)` 同步启动 claude 与 openclaw 两个进程同时跑 `claim_coordination.py`。
- **断言**：两个进程均返回；恰好一个退出码 0；失败方退出码为 3（租约未到期）或 4（原子锁/CAS 失败）且不继续执行协调操作；state.coordinator 为唯一胜者；revision 恰好 +1。
- **覆盖**：§8.3 原子认领（O_EXCL 锁比较并交换）与「认领失败必须重新读取 state.json」。

## 情景 6：协调者超时后成功接管 —— 自动可测（完整）

- **构造**：init 协调者 codex → 回拨租约 → claude 认领。
- **断言**：认领退出 0、输出"协调权接管成功"；coordinator 变为 claude；revision+1；主文档审计时间线记录"协调接管"与原协调者 codex。
- **覆盖**：§8.2 超时接管（原协调者、接管者、认领时间、修订号落审计）。

## 情景 7：普通参与者超时后暂停并请求 Rainier 决策 —— 部分自动（暂停/不自动移除可测），「向 Rainier 报告」为人工/协议级

- **构造**：init 配置 participant_timeout=60（断言字段记录）；openclaw 模拟超时未提交，仅 claude/codex 提交后尝试 merge。
- **断言**：merge 退出 3、输出"提案缺失"；阶段保持 independent_proposal（暂停推进）、revision 不变；openclaw 仍在 expected_participants（未自动移除）；validate 暂停态仍通过（一致性保持）。
- **替代断言说明**：§9 的「暂停当前阶段推进并向 Rainier 报告」「Rainier 选择继续等待/移除/缩减名单」是人工决策承载，无对应脚本命令。本测试以「存在未提交参与者时推进被拒绝 + 系统不自动移除」验证暂停与不自动移除语义；向 Rainier 报告与名单调整决策需人工/Skill 验证。

## 情景 8：名单冻结后新增或移除参与者 —— 自动可测（schema 一致性门禁）

- **构造**：init 三方 → 首份提案进入 independent_proposal（冻结）；随后三种越权路径各测一次。
- **断言**：名单外身份 gemini 提交被拒（退出 3、"不在参与者名单内"）；未经 Rainier 确认直接把 gemini 塞进 expected_participants（不同步 submission_status）→ validate 退出 1；未经确认移除 openclaw（submission_status 仍含该身份）→ validate 退出 1。
- **覆盖**：§7.2 名单冻结 + schema「submission_status 键集与名单完全一致」约束。**Rainier 授权的名单变更及其审计记录是协议级动作**，脚本无 add/remove 命令，此部分需人工/Skill 验证。

## 情景 9：Markdown 与 state.json 不一致时拒绝推进 —— 自动可测（完整）

- **构造**：三方全部 submit（state 声称 claude 已提交）后删除 claude 的提案文件（Markdown 证据缺失），再尝试 merge。
- **断言**：merge 退出 3、输出"提案缺失"与恢复入口；阶段保持 independent_proposal；revision 不变（不一致未修复前不推进）。
- **覆盖**：§12.3 / state-schema §四「双满足推进，不一致拒绝推进并生成恢复信息，失败不改变 revision」。

## 情景 10：多项自然语言确认存在歧义 —— 自动可测（完整）

- **构造**：流水线到 cross_response 后，夹具置 stage=candidate_decision 并登记 D-D1、D-D2 两个候选 ID、主文档追加结构化决策包；首次确认原文「确认 D-D1 并调整 D-D2」（引用多项 + 含"调整"歧义词）。
- **断言**：未提供二次确认时退出 3、先回显"需要二次确认"与决策 ID；stage 保持 candidate_decision、confirmed_decision_ids 为空、revision 不变；提供 `--second-confirm 确认` 后退出 0、stage=confirmed_decision、confirmed_decision_ids=["D-D1"]；主文档保存确认固化记录（候选 ID/确认原文/记录者）。
- **覆盖**：§13.2 混合确认（单项无歧义直接确认；多项/歧义须先回显再二次确认）。

## 情景 11：分歧无法收敛并生成结构化决策包 —— 部分自动（决策包结构可测），决策包「生成动作」为协议级

- **构造**：流水线到 cross_response 后，夹具登记候选 ID、主文档追加完整结构化决策包，再 validate。
- **断言**：决策包含全部 8 个要素（共识/分歧/候选方案/支持者/主要依据/风险与代价/各 Agent 推荐/待 Rainier 选择的问题）；标注"计数仅为展示，不构成裁决依据"（不用多数票抹平）；candidate_decision_ids 已登记；validate `--expect-phase candidate_decision` 退出 0。
- **替代断言说明**：§13.1 的「生成结构化决策包」是协调者的协议/Skill 职责，无独立生成脚本。本测试验证「已形成的决策包结构完整 + 候选决策 ID 登记 + 阶段一致性」，并以此作为协议行为的证据代理。

## 情景 12：选择归档后的提案保留 —— 自动可测（完整）

- **构造**：init `--disposition archive` → 完整流水线 merge。
- **断言**：merge 退出 0；proposal_disposition 记录为 archive；archive/proposals/ 保留 3 份提案且内容与提交原文逐字节一致；manifest.json 记录 3 条 entries（含文件名与 sha256）。
- **覆盖**：§11.2 归档（保留原文件名、内容、时间与可验证哈希）。

## 情景 13：选择删除但未获得再次确认（不删除） —— 自动可测（完整）

- **构造**：init `--disposition delete` → 完整流水线 merge（仅生成删除清单，不执行删除）。
- **断言**：merge 退出 0；proposal_disposition 记录为 delete；proposals/ 下 3 份提案文件仍在（未被删除）；deliverables/删除清单_<discussion_id>.json 列出 3 个文件的路径与 sha256；清单注明"未确认前不删除任何文件"。
- **覆盖**：§11.3 删除必须先列精确删除清单并取得 Rainier 再次确认，未确认前不得删除。

## 情景 14：两分钟辅助监测未启用或中途失效 —— 自动可测（完整）

- **构造**：init 后断言监测默认字段；再把手动把 monitoring.status 改为 expired 继续跑核心流程。
- **断言**：enabled=False（未明确同意不启用）；mode=reply_before、interval_seconds=120、status=active（schema 合法）；监测关闭时提案提交正常；status=expired 时提交仍正常；validate 仍通过（失效不影响核心流程）。
- **覆盖**：§14「未明确同意不启动；辅助监测为会话级能力可能失效；失效不影响核心协作流程」。

## 情景 15：Word 转换成功并记录源 Markdown 哈希 —— 自动可测（完整）

- **构造**：流水线到 cross_response → 夹具置 candidate_decision → 单项无歧义确认固化（confirm-text「确认 D-D1」）→ export_docx → 再次 export 验证幂等。
- **断言**：确认退出 0、stage=confirmed_decision；首次 export 退出 0、stage=delivered、revision+1、快照数=1；deliverables/word-snapshots.json 记录快照（source_markdown、generated_at、docx 路径）；**按 V2 语义，`source_sha256` 等于「最终落盘 md 的原始字节哈希」**（export 先把交付记录+审计行幂等追加进主文档，再对最终 md 算哈希，故断言用按字节哈希与落盘文件比对，而非对追加前内存文本算哈希）；docx 文件存在；主文档出现「## 七、Word 交付记录」并记录源路径与哈希；**重复 export 退出 0、输出标注幂等、快照数不递增**。
- **覆盖**：§15.1-5（Word 为只读快照，记录源路径/哈希/时间；正式结论确认后自动生成；幂等重跑）。

## 情景 16：Word 转换失败但 Markdown 正式决策保持有效 —— 自动可测（完整）

- **构造**：同情景 15 固化为 confirmed_decision；export 时 `--out` 指向不存在的输出目录使 `doc.save` 抛 OSError。
- **断言**：export 退出 1、输出"Markdown 仍是唯一权威内容源"；stage 保持 confirmed_decision、revision 不变、confirmed_decision_ids 仍含 D-D1；**按 V2 语义，失败发生在 convert 之前，交付记录（§15 记录）可能已幂等追加进主文档，因此断言放宽为「正式决策/确认固化区块内容未变」**（用区块提取比对 `## 六、确认固化记录`，而非整文档逐字节相同）；确认固化记录仍保留；主文档含「## 七、Word 交付记录」区块；validate `--expect-phase confirmed_decision` 仍通过。
- **覆盖**：§15.3/§16「Word 转换失败不影响 Markdown 正式决策有效性；Markdown 始终是唯一权威内容源」。

---

## 汇总

| 自动可测程度 | 情景 |
|---|---|
| 完整自动（纯脚本断言） | 1, 2, 3, 5, 6, 9, 10, 12, 13, 14, 15, 16 |
| 部分自动（需人工/协议级验证） | 4（隔离本身协议级）、7（向 Rainier 报告人工级）、8（Rainier 名单变更人工级）、11（决策包生成协议级） |

当前脚本状态下运行 `python evals/run_scenarios.py` 结果为 **16/16 PASS**（M02/M03 已满足本套件全部断言；协调者后续仍按交付流程独立复核）。
