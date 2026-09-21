# 一、项目级自动协商协议

## （一）身份与职责

每个协作身份由四个独立字段标识：`agent_id`（逻辑身份）、`role`（coordinator 或 participant）、`platform_id`（平台身份）、`session_id`（明确会话）。协调者必须在启动时显式绑定四项；不得从默认平台、进程或会话推断身份。状态字段按 `references/state-schema.md` 与初始化器实现的 `coordinator_binding`、`participant_bindings` 契约使用。

协调者同时拥有两个逻辑身份：`coordinator:<id>` 负责推进状态，`participant:<id>` 负责独立发言。它必须和其他参与者一样先提交自己的 proposal，在全部 proposal 完成前不得读取其他提案。参与者只处理分配给自己的完整指令并写自己的产物和回执；只有协调者更新状态，只有 Rainier 可确认候选决策。

## （二）事件、门禁、唤醒与执行

五类职责彼此独立：

1. `monitor_discussion.py` 只读观察文件变化，是低层传感器；`participant_monitor.py` 是每个参与 Agent 的持久 Event Consumer，负责读取事件、恢复 cursor、按 event_id 去重并触发激活，但不是流程推进器。
2. 门禁编排器在有效产物或回执写入后由协调者单次调用，校验证据并选择唯一可执行的下一步。可选看门狗只处理超时和恢复。
3. Activation Bridge 把最小定位请求投递到显式绑定的平台会话，并返回 `activated`、`manual_activation_required` 或 `activation_failed`；平台无可用唤醒时不得假装成功。
4. Participant Runtime 读取自己的原始指令，在约束内执行并写入产物/回执。

因此主链路是“产物/回执 → append event → 更新 state 快照 → 单次门禁 → 发布指令/event → Participant Monitor 消费 → Activation Bridge → 参与者执行 → 新事件”。没有已验证 dispatcher 时必须记录 `manual_activation_required`，不能假装成功。扫描日志、`accepted` handoff 或聊天文本均不是执行完成证据。

Round transition 是首个必须事务化的业务转换：`round-N.md`、下一轮指令（如有）、`state.round`/`revision` 和 `round_completed` event 必须作为一个可恢复提交；后续再把 proposal merge、临时源件删除、manifest 和 finalization 纳入同一事务模型。事件流损坏、cursor 断裂、事务日志与 state 不匹配时统一 fail-closed。

## （三）收敛与交付顺序

状态机使用 `initialized → independent_proposal → cross_response(round=N) → candidate_decision → human_review → finalizing → confirmed_decision → delivered`。`state.json` 是快照，`.multiagent/audit/events.jsonl` 是事实流；事务日志缺失或不一致时拒绝推进。门禁只推进确定性且证据充分的转换，交付顺序为：

1. 完成独立提案、交叉回应并收敛意见；阶段输入须满足隔离验证要求。提案合并成功后默认自动删除每位参与者的临时提案源件，不询问用户；仅在 `.multiagent/audit/` 保留源路径、SHA-256、处置时间和处置结果。不得创建或复制 archive。显式兼容选项 `archive` 只允许原位保留源件，不得复制到归档目录。
2. 每一轮只用文件/receipt 判断回应是否齐全；是否继续下一轮由协调者承担的 Semantic Convergence 职责判断，不创建新的独立 Agent。协调者读取完整 round snapshot，写入 `.multiagent/convergence/round-N.json`；Python 只校验 schema、轮次和参与者引用。
3. Round 1 完成后生成不可变 `round-1.md`；Round N+1 的密封输入必须显式包含上一轮 snapshot，不能只复用初始 `discussion.md`。
4. 生成候选 Markdown，标记 `candidate`，保留共识、分歧、依据、风险和待选择问题。
5. 从该候选 Markdown 生成候选 Word，记录源哈希。
6. 候选 Word 自动打开成功后进入 `human_review`，监测继续运行；候选不是正式决策。
7. Rainier 确认后先将正式决策固化到主讨论 Markdown，记录确认原文、时间和修订号，阶段进入 `finalizing`。
8. 进入 `finalizing` 后先向每位参与者发布唯一 `final_ack` 指令；收齐 ACK 后，正式结论发布后协调者必须向每位参与者发出一条唯一 `stop` 指令。收齐每个绑定会话的唯一 `completed` stop 回执和有效证明后才停止监测并进入 `confirmed_decision`。
9. 从固化后的 Markdown 生成正式 Word 并自动打开；只有打开成功才进入 `delivered`，打开失败时保留 `confirmed_decision`。

候选 Markdown 与 manifest 位于 `.multiagent/deliverables/`；工作区根目录只保留 `project-context.md`、主讨论 Markdown、`候选决策.docx`、`最终决策.docx`。提案合并后的临时提案源件按默认策略删除，审计区只保留路径与哈希处置清单；回应源件留在 `.multiagent/views/<agent_id>/outputs/`，必要内容合并至主讨论 Markdown。不得复制提案归档；显式 `archive` 兼容策略仅原位保留。`monitoring_stopped` 仅用于显式停止或旧工作区兼容，不属于上述正常阶段链。

Rainier 拒绝或退回候选时，不得固化为正式决策；等待明确的新指示再重新开启收敛。候选 Word 打开失败、确认记录缺失或正式 Word 打开失败时，不得伪报完成。

## （四）不可变指令与密封输入

指令位于 `.multiagent/instructions/<agent_id>/`，发布后不可修改。除 ID、阶段和路径等机器字段外，每条指令必须含完整 `task_prompt`，明确目标、背景、允许操作、产物要求和验收条件；`kind` 与路径仅为索引，不能代替任务文本。唤醒消息可以仅包含最小定位信息，但被唤醒的参与者必须读取并执行工作区内完整指令。

指令为每位参与者绑定密封、只读且有哈希清单的输入视图、平台沙箱配置和精确读写路径白名单。提示词不是安全边界。平台沙箱须实际强制输入/输出限制；同一 Windows 用户下的普通文件 ACL 不足以证明隔离。必须保存可复核的输入视图清单、沙箱配置及读取/拒绝访问等执行证据。

**签名信任契约**：使用 `scripts/attestation_keys.py provision --workspace <工作区> --private-key-path <工作区外绝对 PEM 路径> --key-id <id>` 生成密钥资料。协调者初始化时只传 `public_key_b64` 和 `key_id`；项目只保存公钥与指纹。执行验签的每个进程均须配置公开环境变量 `MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON`，值为 JSON 对象 `{"key-id":"public-key-b64"}`。该值不是秘密；缺失、JSON 无效、key ID/公钥不匹配、签名缺失或验签失败均阻断，不得静默降级。私钥只交由可信平台适配器从工作区外受保护位置读取，绝不写入指令、共享目录或交给参与者。适配器需用该密钥签署绑定讨论、指令、agent/platform/session、输入 manifest、白名单和执行时间的真实平台证据；签名本身不能把自我声明变成平台执行证据。

停止也属于需要证明的实际操作：每个参与者的唯一 `stop` completed 回执须带由同一项目受信平台密钥签署的停止证明。证明 payload 与其引用的证据 JSON 都必须包含 `instruction_sha256`，其值必须等于原 stop 指令的 `sha256` 字段；统一必需字段为 `discussion_id`、`instruction_id`、`instruction_sha256`、`agent_id`、`platform_id`、`session_id`、`stopped_at`、`mechanism`、`target`、`action_verified`、`proof_reference`、`proof_sha256`。时间采用带时区的 ISO 8601；成功证明要求 `action_verified=true`。`proof_reference` 必须是工作区相对路径，指向 `.multiagent/audit/platform-evidence/` 下实际存在、可解析的 JSON 文件。验证器须把工作区与证据根目录解析为规范路径，确认该文件的最终解析路径仍是证据根目录的后代；拒绝任何解析后逃逸到证据根目录外的 symlink/junction、目录逃逸或伪引用，并核对文件内容、其原始字节 SHA-256 是否等于 `proof_sha256`、证据中的目标/动作/指令哈希是否匹配。证据应记录实际命令、结果及带时区时间；验证时须确认 stop 指令签发时间 ≤ 实际操作开始时间 ≤ 实际操作完成时间（`stopped_at`）；OpenClaw 还须满足 `stopped_at` ≤ `removal_checked_at` < `receipt.at`；其他平台须满足 `stopped_at` ≤ `receipt.at`；所有回执时间均不得晚于当前验证时间，且证明不得超过新鲜度策略。适配器仅在完成这些检查后签名。证明必须绑定当前 `discussion_id`、该讨论中唯一有效的 stop `instruction_id` 及对应 agent/platform/session；签名覆盖全部契约字段和 `proof_sha256`。任何跨讨论重放、跨指令复用、身份不符、过期或哈希/内容/时序不符均 fail-closed。

OpenClaw 的证明还必须包含 `automation_job_id`、`removal_verified=true`、带时区的 `removal_checked_at`。可信 OpenClaw 适配器必须针对该项目目标实际执行 `openclaw cron rm <automation_job_id>`，随后执行 `openclaw cron list --json`；只有命令成功且 JSON 列表中完全不存在该 `automation_job_id` 才算移除成功。目标仍在列表中（包括 disabled 状态）一律失败。适配器把实际命令、退出码、目标和带时区的输出/检查时间保存为上述目录中的证据 JSON，完成路径、哈希、内容及时序自检后，才从工作区外读取受保护的项目私钥进行签名。`receipt.at` 必须晚于 `removal_checked_at` 且不得晚于验证当前时间；等于核验时间或晚于当前时间均拒绝。OpenClaw 参与者自身不得签名、读取私钥或以自述替代适配器证据。缺少任一字段、受信签名、completed 回执或有效证明时，门禁不得从 `finalizing` 进入 `confirmed_decision`。平台无法产出签名证明时严格独立模式阻断，这是安全结果，不是自动降级。

威胁模型不覆盖工作区拥有者直接篡改 `state.json` 或其他本地账本；普通文件系统协议无法阻止有权写入工作区的拥有者伪造、回滚或重写状态。严格独立性必须依赖工作区外的可信适配器与签名密钥，以及平台强制执行的沙箱/访问证据；签名验证器须在信任边界之外保护私钥并验证适配器提交的证据。若平台无法提供所需执行证据，严格独立性门禁必须阻断，不得降级到提示词声明、同用户 ACL 或自签名证明。当前平台是否可提供此类 attestation 必须另行端到端验证，文档不得声称已经验证。缺少证据时，状态必须标记隔离未验证，不得称为严格独立样本，也不得以该标签通过独立性门禁。

参与者在独立提案阶段只能读取自己的密封视图、通用项目上下文和白名单内模板；不得接触其他参与者材料或主讨论 Markdown。交叉回应阶段只可读取指令明确纳入的新视图。阶段切换由一次门禁决定。

> 新协议覆盖说明：候选 Word 打开后只进入 `human_review`，不会停止监测；Rainier 确认并发布正式结论后进入 `finalizing`，再执行 stop 门禁并进入 `confirmed_decision`。`user_confirmation` 仅是旧工作区兼容名称。

## （五）紧凑工作区与回执

工作区根目录仅保留项目上下文、主讨论 Markdown、候选 Word、正式 Word。其余机器数据统一置于 `.multiagent/`：

~~~text
.multiagent/
├─ state.json
├─ runtime/participant/<version>/
├─ instructions/<agent_id>/
├─ receipts/<agent_id>/
├─ views/<agent_id>/
├─ deliverables/
└─ audit/
~~~

提案与回应源件只写入 `.multiagent/views/<agent_id>/outputs/`，不得另建或写入 `participants/`、根级 `proposals/`、`responses/` 或提案归档目录。回应阶段只读取本次指令密封提供的合并讨论副本，不回读归档提案。

每个完成回执记录指令 ID、四项身份、状态、时间（`receipt.at`）、产物路径与 SHA-256；失败回执记录错误码、说明和是否可修复。`receipt.at` 必须是含时区的 ISO 8601 时间且不得晚于当前验证时间；stop 回执还须不早于签名的 `stopped_at`，OpenClaw stop 回执必须严格晚于 `removal_checked_at`。路径解析后必须位于对应白名单范围。回执和产物写入后协调者只运行一次门禁，不要求独立守护脚本常驻。

## （六）异常与恢复

格式或模板类可修复问题可由门禁对同一参与者发布有次数上限的修复指令；修复仍必须具有完整 `task_prompt` 和不扩大的白名单。哈希错误、路径逃逸、隔离证据不足、身份不匹配、状态冲突、重试耗尽、平台唤醒不可用或语义缺口均阻塞推进。不得为消除阻塞而伪造身份、独立性、确认、打开结果或完成状态。

失败的门禁可以安全重试，但同一产物/回执事件不得重复发放非幂等动作。状态名、证据字段和门禁幂等键须与 `references/state-schema.md` 及当前实现一致；未实现/未验证的字段或平台能力必须阻断并如实标示，不得作为可用契约。
