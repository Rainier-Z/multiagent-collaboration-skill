# <agent-id> 交叉回应文档

> 使用说明：交叉回应阶段，各参与者只能基于本次指令密封提供的合并讨论副本与项目上下文写作，不得读取归档提案或其他参与者私有文件。写入 `.multiagent/views/<agent-id>/outputs/round-<round>/交叉回应文档.md`，`<agent-id>` 与 `.multiagent/state.json` 的 `expected_participants` 身份键完全一致。UTF-8 编码。共识与分歧须如实保留，不以多数票消解分歧。

## 交叉回应追溯块

- responder_id: <expected_participants 身份>
- round: <轮次，正整数，默认 1>
- 回应对象: <被回应的合并区块来源身份，可多项>
- 被回应提案版本: <对应区块被合并时的 .multiagent/state.json revision / 提案文件版本号>
- 被回应原文（摘录）: <逐字摘录 + 引用位置>
- 回应时间: <YYYY-MM-DD HH:mm> (Asia/Shanghai)
- 锚定 revision: <回应文件落盘时 .multiagent/state.json revision>
- 所涉候选决策 ID: <如已分配则填写，否则留空>

## 回应正文

### 一、共识点

<与对方一致、可达成共识的内容>

### 二、分歧点

<与对方不一致的内容，保留双方原文立场>

### 三、候选方案

<在分歧上提出的候选方案>

### 四、主要依据

<支持本方立场的依据>

### 五、风险与代价

<对方方案或本方方案的风险与代价>

### 六、推荐

<本 Agent 的推荐意见>

### 七、新问题与待确认事项

<本轮新出现、尚未解决或需要下一轮回应的问题；没有则填写“无”>
