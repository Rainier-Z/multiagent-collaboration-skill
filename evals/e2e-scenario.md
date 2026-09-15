# Participant Runtime v2 端到端验收场景

## 目的与边界

本演习只验证本地工作流契约，不声称已经唤醒或隔离任意真实外部 Agent。`FakeWakeAdapter` 只代表调用已接受；隔离测试 attestation 是测试夹具，不能作为生产平台证据。生产场景必须由适配器针对精确 agent/platform/session/instruction/input-manifest 生成证据。

主讨论 Markdown 是唯一权威文本。JSON 仅保存运行状态、身份绑定与哈希索引；Word 是 Markdown 的审阅/交付快照。

## 阶段与硬门禁

```text
initialized
  → independent_proposal
  → cross_response
  → candidate_decision
  → user_confirmation
  → confirmed_decision
  → delivered
```

- 协调者每次状态变更必须给出精确 `actor + platform_id + session_id`，并与 state 中协调者绑定完全一致。
- 每位参与者必须在 `participant_bindings` 有显式平台和会话绑定；缺失即阻断，不从 agent 名称猜测。
- 提案/修复必须有匹配 instruction、密封输入 manifest 和 `.multiagent/audit/platform-evidence/<agent>/<instruction>.json` 的平台 attestation；提示词、自我声明及同用户 ACL 不算隔离证明。
- 修复输入只能包含自身失败回执与原输出/原输出缺失状态；交叉回应接收主文档的密封快照。
- 合并写入既有的 `## 二、独立提案`、`## 三、交叉回应`、`## 四、结构化决策包`、`## 五、候选决策与 Word 审阅`；不得创建重复标题或复制提案归档。
- 候选 Word 打开失败或指定 no-open 时停在 `candidate_decision`，监测保持运行；只有实际打开成功才进入 `user_confirmation` 并停止常规监测。该节点不下发参与者 stop 指令。
- 只有用户明确确认后生成并成功打开正式 Word，状态才进入 `delivered`。

## 演习命令

在技能根目录运行：

```powershell
py -3 -B -m unittest evals.test_orchestration evals.test_end_to_end_v2 -v
py -3 -B evals/run_scenarios.py
```

`run_scenarios.py` 会运行 v2 编排/交付测试，并确认旧的 `submit_contribution.py`、`merge_proposals.py`、`claim_coordination.py` 已明确退役且不再修改机器状态。旧的 30 项 v1 场景依赖独立写状态入口，故不再作为 v2 验收依据。

## 必须覆盖的结果

1. 非 Claude 协调者的正确绑定可执行；会话不匹配不改变任何状态。
2. 缺少参与者绑定、缺失或伪造隔离 attestation 均 fail-closed，不合并、不推进。
3. 具备有效绑定和 attestation 的提案可以合并；回应输入为公开讨论密封快照。
4. 候选 Word 打不开时留在候选阶段并继续监测；打开成功后停止监测、等待用户确认。
5. 最终 Word 必须包含全部提案和回应；正式 Word 打开失败不得标记 delivered。
6. 工作区根目录只保留 project context、权威讨论 Markdown、候选 Word 和最终 Word；运行文件限于 `.multiagent/`。
