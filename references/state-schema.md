# 一、state.json 机器流程账本

## （一）权威边界

主讨论 Markdown 是讨论与正式决策内容权威；state.json 是阶段及机器证据账本；候选/正式 Word 是对应 Markdown 的只读快照。参与者不得写状态文件。协调者每次只在有效输入事件后调用一次门禁，并以原子写入记录守卫结果。

本文件记录当前初始化器与 Participant Runtime 使用的字段契约。实现尚未完成或平台能力尚未通过端到端验证的项目必须明确标为未验证；不得以本文描述替代运行证据。

## （二）核心字段建议

| 字段 | 类型 | 说明 |
|---|---|---|
| protocol_version | string | 协议版本 |
| discussion_id | string | 稳定讨论 ID |
| stage | enum | 当前阶段，按下节顺序推进 |
| revision | integer | 成功状态写入的修订号 |
| coordinator_binding | object | 协调者实际绑定，含 agent_id、role、platform_id、session_id |
| expected_participants | string[] | 冻结后的逻辑 agent_id 集合 |
| participant_bindings | object | 以 agent_id 为键的实际参与者绑定 |
| isolation_trust | object | Ed25519 公钥信任锚：key_id、公钥 Base64、指纹；私钥仅在工作区外 |
| submission_status / response_status | object | 每位参与者提案/回应的产物和回执索引 |
| proposal_disposition | enum | 合并成功后默认 `delete`；`archive` 仅兼容且原位保留、不复制 |
| content_authority | object | 主讨论 Markdown 路径、SHA-256、更新时间 |
| runtime_distribution | object | Runtime 版本、清单路径与哈希 |
| instruction_sequence / instruction_queue | integer / object | 不可变指令序号与待处理指令索引 |
| receipt_index | object | 指令 ID 到回执路径、状态、哈希的索引 |
| input_views | object | 密封视图清单、沙箱配置、白名单和独立性证据 |
| gate | object | 上次门禁事件/运行 ID、结果、幂等记录 |
| watchdog | object | 可选超时/恢复看门狗；不得成为主流程依赖 |
| candidate_delivery | object | 候选 Word 路径、哈希、opened/opened_at；候选 Markdown 与 manifest 位于 `.multiagent/deliverables/` |
| monitoring | object | enabled/status、stop_requested_at 与每位参与者的 stop 指令/回执证据 |
| user_confirmation | object | Rainier 查看和明确确认的原文、时间、结果 |
| formal_delivery | object | 固化后的 Markdown、正式 Word 与自动打开结果 |
| retry_policy | object | 最大修复次数和可修复错误码 |
| convergence | object | 收敛门槛与结果摘要 |
| blocking_items | array | 身份、隔离、唤醒、验证等未解决阻塞 |

每个身份绑定对象必须包含 agent_id、role、platform_id、session_id。`agent_id` 表示逻辑身份；不得从 platform_id、账号或默认值推导它。协调者的四项绑定必须在启动时显式写入；缺项时不得启动流程。初始化时只向项目传 `public_key_b64` 与 `key_id`，项目保存验签公钥/指纹，签名私钥必须位于工作区外，严禁写入项目。

运行验签的进程必须设置公开环境变量 `MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON`，其值是 `{ "key-id": "public-key-b64" }` JSON 对象；该信任表不是秘密。私钥通过 `scripts/attestation_keys.py provision --workspace <工作区> --private-key-path <工作区外绝对 PEM 路径> --key-id <id>` 配置/生成，只能供可信平台适配器读取，绝不交给参与者。环境变量缺失、JSON 无效、key ID 未受信任/公钥不匹配、签名缺失或验签失败一律 fail-closed。严格独立门禁不得自动降级；平台适配器无法产出签名 attestation 时阻断是预期安全结果。

## （三）不可变指令字段

每条指令文件建议包含以下字段；所有指令都必须有完整 task_prompt，而不是只有 kind 与路径：

| 字段 | 说明 |
|---|---|
| instruction_id、sequence、kind | 指令标识、顺序、阶段类型 |
| agent_id、role、platform_id、session_id | 明确的逻辑和投递身份 |
| task_prompt | 可直接执行的完整任务：目标、背景、允许动作、预期产物、验收条件 |
| input_view_id、input_manifest_sha256 | 本指令使用的密封输入视图与清单哈希 |
| sandbox_profile、read_allowlist、write_allowlist | 平台强制的隔离配置和精确路径白名单 |
| isolation_evidence | 可复核的沙箱/访问执行证据引用；缺失则独立性未验证 |
| output_path、attempt、max_attempts | 产物路径及修复限制 |
| runtime_version、state_revision、issued_at、sha256 | 版本、签发修订、时间与不可变校验 |

task_prompt 存在于工作区原始指令中。平台唤醒消息可以仅携带工作区、agent_id、platform_id、session_id 和指令 ID 等定位字段，不得以短唤醒消息取代完整指令。重复投递必须幂等。

## （四）阶段与守卫顺序

唯一正常阶段顺序如下；不为文档生成或打开增加子阶段：

~~~text
initialized
→ independent_proposal
→ cross_response
→ candidate_decision
→ user_confirmation
→ confirmed_decision
→ delivered
~~~

`candidate_decision` 包含候选 Markdown/Word 生成。候选 Word 成功打开后，协调者必须向每位参与者各下发一条唯一 `stop` 指令，并等待所有目标身份各自返回唯一 `completed` stop 回执；回执必须携带与项目受信任密钥匹配的停止证明。停止证明 payload 与引用的证据 JSON 均必须含 `instruction_sha256`，且等于该 stop 指令 `sha256` 字段。payload 的统一必需字段为 `discussion_id`、`instruction_id`、`instruction_sha256`、`agent_id`、`platform_id`、`session_id`、`stopped_at`、`mechanism`、`target`、`action_verified`、`proof_reference`、`proof_sha256`。成功证明要求 `action_verified=true`，时间带时区。`proof_reference` 必须指向工作区 `.multiagent/audit/platform-evidence/` 下真实 JSON；验证器把证据根目录与文件路径解析为规范路径，确认最终路径仍位于解析后的证据根目录内，并拒绝任何解析后逃逸出该根目录的 symlink/junction；另检查文件存在且有效、SHA-256 与原始文件字节一致、证据内容与动作/目标/指令哈希匹配，并校验讨论、唯一 stop 指令、会话身份和事件时序。时间顺序为 stop 指令签发 ≤ 实际操作开始 ≤ 实际操作完成（`stopped_at`）；OpenClaw 还须满足 `stopped_at` ≤ `removal_checked_at` < `receipt.at`，其他平台须满足 `stopped_at` ≤ `receipt.at`；所有回执时间均不得晚于当前验证时间。证明和签名均绑定当前讨论，禁止跨讨论重放。

OpenClaw 停止证明还必须包含 `automation_job_id`、`removal_verified: true`、带时区的 `removal_checked_at`。可信 OpenClaw 适配器实际执行 `openclaw cron rm <automation_job_id>`，然后以 `openclaw cron list --json` 核验目标 `automation_job_id` 完全不在列表中；目标仍存在但 disabled 也不算删除成功。记录实际命令、退出码、JSON 结果和时间到 `proof_reference` 指向的证据文件。OpenClaw stop 回执的 `receipt.at` 必须严格晚于 `removal_checked_at`，且不晚于当前验证时间。适配器完成路径、哈希、内容及时间顺序核验后，才使用工作区外可信适配器的受保护私钥签名；参与者自身不得签名或接触私钥。Ed25519 签名元数据为 `key_id`、`algorithm`、`signature_b64`，签名覆盖完整契约 payload。只有全部 stop 回执和证明验证通过后，协调者才可记录 `monitoring.enabled=false`、`monitoring.status=stopped`、`monitoring.stop_requested_at`，并进入 `user_confirmation`。仅把 state 字段改成 stopped 不代表实际停止，也不能通过门禁。普通文件系统协议无法防止工作区拥有者直接篡改 `state.json`；严格独立性依赖工作区外可信适配器、签名密钥与平台强制的沙箱/访问证据，平台无法提供时必须阻断而不得降级。确认仅能从 `user_confirmation` 推进至 `confirmed_decision`。正式 Word 生成并成功打开后才进入 `delivered`；打开失败或未请求打开时保留 `confirmed_decision`，不能伪报完成。`monitoring_stopped` 仅为显式停止或旧工作区兼容状态，不是正常交付阶段。拒绝、退回、隔离未验证、身份不匹配或缺少确认均不能推进。

## （五）字段对象示例

以下示例使用实现中的真实身份字段名；阶段值属于唯一七阶段链：

~~~json
{
  "stage": "initialized",
  "coordinator_binding": {
    "agent_id": "coordinator-1",
    "role": "coordinator",
    "platform_id": "codex",
    "session_id": "explicit-session-id"
  },
  "participant_bindings": {
    "reviewer-a": {
      "agent_id": "reviewer-a",
      "role": "participant",
      "platform_id": "claude_code",
      "session_id": "explicit-session-id"
    }
  },
  "proposal_disposition": "delete",
  "isolation_trust": {
    "algorithm": "Ed25519",
    "key_id": "platform-attestation-key-2026-01",
    "public_key_b64": "<32-byte public key, Base64>",
    "public_key_sha256": "<SHA-256 fingerprint>",
    "private_key_location": "external_to_workspace"
  },
  "monitoring": {
    "enabled": true,
    "status": "active",
    "stop_requests": {
      "reviewer-a": {
        "instruction_id": null,
        "receipt_id": null,
        "status": "pending",
        "stop_attestation": {
          "discussion_id": "discussion-2026-09-13",
          "instruction_id": "stop-openclaw-001",
          "instruction_sha256": "<sha256-of-original-stop-instruction>",
          "agent_id": "reviewer-a",
          "platform_id": "openclaw",
          "session_id": "explicit-session-id",
          "stopped_at": "2026-09-13T12:29:57+08:00",
          "mechanism": "openclaw cron rm followed by cron list --json verification",
          "target": "project automation openclaw-job-123",
          "action_verified": true,
          "proof_reference": ".multiagent/audit/platform-evidence/openclaw-stop-stop-openclaw-001.json",
          "proof_sha256": "<sha256-of-exact-evidence-json-bytes>",
          "automation_job_id": "openclaw-job-123",
          "removal_verified": true,
          "removal_checked_at": "2026-09-13T12:29:58+08:00",
          "signature": {
            "key_id": "platform-attestation-key-2026-01",
            "algorithm": "ed25519",
            "signature_b64": "<base64-signature>"
          }
        }
      }
    }
  },
  "gate": {
    "event_id": "receipt-or-artifact-id",
    "run_id": "one-shot-run-id",
    "status": "completed"
  },
  "watchdog": {
    "enabled": false,
    "purpose": "timeout_or_recovery_only"
  }
}
~~~

## （六）独立性与完成判定

只有在每位参与者各自的密封视图哈希、平台沙箱、精确白名单及可复核执行证据齐备时，才可记录严格独立。平台 attestation 必须由工作区外可信适配器使用工作区外的签名私钥签名，并使用 `isolation_trust` 中受信任公钥验签；签名缺失、密钥 ID 不匹配或验签失败均阻断。项目目录只存公钥/指纹，不存私钥。威胁模型不覆盖工作区拥有者直接篡改 `state.json`：普通文件系统协议无法保护工作区拥有者对本地账本的写权限。提示词、隔离声明或同一 Windows 用户下的普通 ACL 均不是执行证明；严格独立性依赖外置可信适配器、签名和平台强制隔离证据，平台不能提供时必须阻断不得降级。证据缺失时 independence_status 必须为 unverified，且独立性门禁失败。当前平台是否真实提供了满足条件的 attestation，必须单独端到端验证，不得由文档描述推定已验证。

候选 Word 打开与常规监测停止均记录在 `candidate_delivery`/`monitoring` 证据字段，不增加流程阶段。候选打开、停止请求、用户确认、正式 Word 打开和最终交付都须分别保留时间与证据。

## （七）代码组字段确认清单

请实现侧确认并统一字段名、类型与路径：

1. 身份：coordinator_binding、participant_bindings 及 agent_id、role、platform_id、session_id 的对象结构。
2. 指令与隔离：task_prompt、input_view_id、input_manifest_sha256、sandbox_profile、read_allowlist、write_allowlist、isolation_evidence、independence_status。
3. 门禁和看门狗：gate.event_id、gate.run_id、gate.status、watchdog.enabled 及幂等/恢复语义。
4. 候选产物：candidate_delivery.path、markdown_path、sha256、markdown_sha256、opened、opened_at、open_attempted_at。
5. 监测停止与用户确认：candidate Word 打开后向全部参与者下发 stop；仅收齐唯一 completed 回执，且统一停止证明字段（discussion_id、instruction_id、instruction_sha256、agent_id、platform_id、session_id、stopped_at、mechanism、target、action_verified、proof_reference、proof_sha256）和证据 JSON 的解析路径、哈希、内容、身份绑定及时序均验证通过后，才设置 monitoring.enabled/status/stop_requested_at。`receipt.at` 不得晚于当前验证时间；OpenClaw 还要求 `automation_job_id`、`removal_verified=true`、`removal_checked_at`，列表中目标任务必须完全不存在且 stop 回执 `at` 严格晚于核验时间；用户确认另记原文和时间。
6. 正式交付：formal_delivery.path、sha256、source_markdown、source_sha256、opened、opened_at、open_attempted_at。
7. 阶段枚举：严格使用本文唯一七阶段顺序；`monitoring_stopped` 只用于显式停止或旧工作区兼容。
8. .multiagent/ 下 Runtime、视图、产物、指令、回执、审计的实际目录名。

## （八）运行时字段示例

```json
{
  "runtime_distribution": {
    "version": "1.0.0",
    "manifest_path": ".multiagent/runtime/participant/1.0.0/manifest.json",
    "manifest_sha256": "..."
  },
  "retry_policy": {
    "max_attempts": 3,
    "recoverable_error_codes": ["E_SCHEMA", "E_OUTPUT_FORMAT"]
  },
  "gate": {
    "status": "idle",
    "last_event_id": null,
    "last_run_id": null
  },
  "watchdog": {
    "enabled": false,
    "purpose": "timeout_or_recovery_only"
  },
  "convergence": {
    "min_response_rounds": 1,
    "max_response_rounds": 3,
    "no_new_issue_rounds": 1
  }
}
```

平台返回 accepted 只能说明最小 handoff 请求被接收，不证明参与者已执行；执行以有效回执和产物哈希为准。unavailable、rejected、隔离证据缺失、身份不匹配、哈希不一致、路径越界或状态冲突均不得推进阶段。
