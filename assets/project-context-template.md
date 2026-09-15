# 项目上下文

> 使用说明：初始化时由文档创建者（或初始化工具）填写，作为各参与 Agent 的公共输入。复制到讨论项目根目录并命名为 `project-context.md`。Markdown 是内容权威来源。

## 一、项目信息

- 项目名称：<项目名称>
- 讨论主题：<本次讨论的子项目主题>
- 创建日期：<YYYY-MM-DD>
- 创建者：<身份>

## 二、任务与目标

- 任务描述：<要解决的问题>
- 期望结果：<预期产物与完成形态>

## 三、权威输入

| 输入 | 路径 | 权威来源 / 版本 |
|---|---|---|
| 权威需求 | <路径> | <版本 / 哈希> |
| 项目上下文（本文件） | project-context.md | <版本> |
| 协作规范 | <路径> | <版本> |

## 四、协作约定

- 权威内容源：Markdown
- 权威流程状态：`.multiagent/state.json`
- 最终决策者：Rainier
- 讨论文档命名：<当前讨论子项目名称>讨论文档_<日期>.md
- 参与者名单：由初始化时 Rainier 指定（详见 `.multiagent/state.json` 的 `expected_participants`）
- 协调者：由初始化时 Rainier 指定，并绑定实际平台与会话（详见 `.multiagent/state.json` 的 `coordinator_binding`）
- 身份认领：每位 Agent 只使用初始化时绑定的 `agent_id`、`role`、`platform_id`、`session_id`；不得根据产品名称或当前宿主自行认领角色，也不得假定协调者属于特定平台。
- 提案处置：成功汇入后默认自动删除临时提案源件；审计区保留来源路径与 SHA-256，不复制归档。

## 五、边界

- 各参与者只写入自己的密封输出视图（`.multiagent/views/<agent-id>/outputs/提案文档.md`、`.multiagent/views/<agent-id>/outputs/交叉回应文档.md`）；具体任务由 `.multiagent/instructions/<agent-id>/` 的有效指令确定。
- 普通参与者不直接修改主讨论文档或 `.multiagent/state.json`。
- 不在项目文件中保存 API Key、Token、密码或内部配置。
- 隔离 attestation 私钥必须保存在工作区外；工作区最多包含初始化配置的公钥与 SHA-256 指纹。公钥配置不代表平台隔离证明已验证；验签/执行证据缺失时严格独立性门禁失败。
