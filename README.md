# 一、Multiagent Collaboration Skill

这是一个面向独立意见协商的协调者 Skill。多个已启动的 Agent 通过共享工作区、事实事件流和多轮回应围绕同一议题协作；协调者同时以 participant 身份独立发言，以 coordinator 身份推进状态。系统只生成待审候选，Rainier 查看候选 Word 并明确确认后，才发布正式决策并停止监测。

## （一）使用流程

完整 Skill 只由协调者使用。协调者必须显式绑定自己的 agent_id、role、platform_id、session_id；各参与者分别绑定同样四项身份。每条不可变指令都包含完整 task_prompt。参与者无需安装完整 Skill，也不需要启动通用常驻轮询脚本。

```text
协调者 Skill
        │ 发布项目 Runtime、密封视图与完整指令
        ▼
共享工作区 ──► 平台原生唤醒或一次显式激活
        ▲                │
        └── 产物与回执 ◄─┘
```

每次有效产物或回执写入后，协调者单次运行门禁。Event Stream 是事实日志，文件是业务数据，state.json 是当前快照；三者不能互相替代。只读传感器 `monitor_discussion.py` 只观察文件，持久 `participant_monitor.py` 才消费事件、维护每个 Agent 的 cursor 并调用 Activation Bridge。Activation Bridge 只报告真实会话能否继续（`activated` / `manual_activation_required` / `activation_failed`），Runtime 执行指令；监测器不能替代协调者门禁或伪造执行完成。

## （二）Skill 目录

```text
multiagent-collaboration/
├─ SKILL.md                         协调者入口和边界
├─ scripts/                         初始化、编排、只读观察与 Runtime 源文件
├─ adapters/                        最小平台唤醒契约与适配器
├─ assets/                          讨论、提案、回应和参与者启动模板
├─ references/                      协议、状态字段与平台验证说明
├─ evals/                           回归与端到端场景
└─ examples/                        可复现实例
```

初始化后的项目工作区保持紧凑：根目录只含 project-context.md、主讨论 Markdown、候选 Word、正式 Word。机器数据、Runtime、密封视图、指令、回执、参与者提案/回应和审计证据放在 `.multiagent/`；不另复制提案归档。

## （三）职责分工

监测只观察，门禁单次决定，适配器尝试唤醒，Participant Runtime 执行完整指令。通用参与者不运行常驻轮询；OpenClaw 专属 Automation 规则见平台适配器文档。

## （四）快速开始

1. 从 SKILL.md 按流程启动讨论并显式绑定协调者与参与者身份。
2. 用 assets/ 中模板发布项目级指令；每条指令须含完整 task_prompt。
3. 每次产物或回执写入后，协调者单次运行门禁；完整候选审阅与正式交付顺序以 references/collaboration-protocol.md 为准。

### Fake Agent 验证边界

接入 Claude Code、Codex 或 OpenClaw 之前，必须先通过真实文件系统、Event Stream 和独立进程运行的 Fake E2E，验证“多轮回应 → 收敛评估 → 人工审阅 → 正式决策 → final_ack → 停机 → delivered”生命周期。仅通过内存夹具、组件单测或 `FakeWakeAdapter`，不能证明 cursor、并发、崩溃恢复和重复激活已经闭环：

```powershell
py -3 -B -m unittest discover -s evals -p 'test_*.py'
```

上述命令覆盖组件和协议回归；其中只有标明真实进程/文件系统的 Fake E2E 才构成联调前的闭环证据。该验证不声称唤醒了任何真实平台会话；真实平台必须另行取得端到端 handoff 证据。

### 安全初始化（PowerShell）

默认初始化为 `normal` 模式，不需要 Ed25519 参数；它仍使用密封视图、路径白名单、事件流和事务恢复。需要平台隔离证明时，追加 `--security-mode strict` 并提供工作区外的公钥/key ID。

以下命令是 strict 模式示例。strict 模式每个讨论使用一个 Ed25519 私钥；私钥保存在工作区外，只由可信平台适配器读取；项目工作区只保存公钥/key ID，参与者永远不接收私钥。先 provision，再把公开 key ID 与公钥传给初始化器；把公钥加入运行进程的公开信任表。信任表不是秘密，但必须在每个运行验签的进程中设置；缺失、key ID/公钥不匹配或验签失败均阻断，不降级。

```powershell
$workspace = (Join-Path (Get-Location) 'my-discussion')
$keyId = 'discussion-2026'
$privatePem = Join-Path $env:LOCALAPPDATA 'multiagent\keys\discussion-2026.pem' # 必须在工作区外
$key = py -3 .\scripts\attestation_keys.py provision --workspace $workspace --private-key-path $privatePem --key-id $keyId | ConvertFrom-Json
$publicKey = $key.public_key_b64
$env:MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON = (@{ $keyId = $publicKey } | ConvertTo-Json -Compress)

py -3 .\scripts\init_discussion.py $workspace planner-ds planner-ds chatgpt openclaw `
  --coordinator-platform deepseek --coordinator-session session-001 `
  --participant-binding planner-ds=deepseek:session-001 `
  --participant-binding openclaw=openclaw:agent:session=42 `
  --participant-binding chatgpt=chatgpt:session-002 `
  --security-mode strict `
  --attestation-public-key-b64 $publicKey --attestation-key-id $keyId
```

`provision` 输出 JSON 中的 `public_key_b64` 和 `key_id` 是公开资料；绝不输出私钥正文。不要把私钥放进工作区、指令、聊天提示词或参与者可读配置。严格独立性依赖工作区外可信适配器、受保护签名密钥和平台强制的沙箱/访问证据；普通文件系统协议无法防止工作区拥有者直接篡改 `state.json`。若适配器不能产生可信签名 attestation，或平台不能提供强制隔离证据，严格独立模式必须阻断，不得自动降级。

初始化时必须为 `participants` 名单中的每个身份（包括协调者）重复提供一次 `--participant-binding agent_id=platform_id:session_id`。身份、平台和会话是独立字段，平台不会由 agent_id 推断；协调者的绑定必须与 `--coordinator-platform`、`--coordinator-session` 完全一致。解析器只使用第一个 `=` 和第一个 `:` 作为分隔符，因此 session_id 可以包含后续的 `:` 或 `=`。

缺少、重复、多余或与协调者绑定不一致时，初始化会 fail-closed，不创建工作区状态。成功后 `.multiagent/state.json` 保存 `coordinator_binding`、完整 `participant_bindings` 及 `isolation_trust` 公钥/指纹，每条 bootstrap 指令也绑定相应目标平台与会话。逻辑身份 `planner-ds` 与真实平台 `deepseek` 分开记录。公钥配置本身不证明隔离已经通过平台验证。

每次有效产物或回执落盘后，由当前已绑定的协调者执行一次真实门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_runtime.ps1 orchestrate_discussion.py `
  --workspace .\my-discussion --once `
  --actor planner-ds --platform-id deepseek --session-id session-001
```

编排器验证四项身份与 `.multiagent/state.json` 中的协调者绑定完全一致；不得用逻辑身份推断平台或会话。

停止证明及引用的证据 JSON 都必须包含与原 stop 指令 `sha256` 一致的 `instruction_sha256`。证据须解析到工作区 `.multiagent/audit/platform-evidence/` 根目录内；拒绝 symlink/junction 逃逸，并核验原始字节哈希、内容和时间。回执 `at` 不得晚于当前验证时间；OpenClaw 的回执 `at` 必须严格晚于 `removal_checked_at`。OpenClaw 还要求 `automation_job_id`、`removal_verified=true`、`removal_checked_at`；可信适配器实际执行 `openclaw cron rm`，再以 `openclaw cron list --json` 确认目标完全不在列表中，才能签名；仍存在但 disabled 不算删除成功。参与者自身不能签名。协议未声称已在真实 OpenClaw 环境验证。完整门禁见 `references/collaboration-protocol.md` 与 `references/platform-adapters.md`。

## （五）运行环境

所有可执行 Python 示例均使用 `scripts/run_runtime.ps1`，而不直接依赖 PATH 中的 `python`。启动器按 `MULTIAGENT_PYTHON`、`py -3`、`python`、ProgramData Anaconda、用户目录下 Codex bundled Python 的顺序探测，要求 Python 3.11+，并对 Word 导出额外验证 `python-docx`。可以把一个已验证解释器的完整路径设置为 `MULTIAGENT_PYTHON`；真实依赖仅见 `requirements.txt`。若没有合格解释器，启动器输出 JSON 错误且不运行目标脚本。

## （六）平台验证状态

| 平台 | 默认唤醒状态 | 说明 |
|---|---|---|
| Claude Code | 未配置即 `unavailable` | 需要独立的项目级端到端 handoff 证据后才可配置投递器 |
| Codex | 未配置即 `unavailable` | 协议已设计，尚无本 Skill 的端到端 wake 证据 |
| OpenClaw | 未配置即 `unavailable` | 协议已设计，尚无本 Skill 的端到端 wake 证据 |

## （七）规范索引

同一规则不在 README 重复维护：流程及安全边界以 references/collaboration-protocol.md 为准；机器字段以 references/state-schema.md 为准；平台唤醒与 OpenClaw 规则以 references/platform-adapters.md 为准；讨论及参与者输入项以 assets/ 模板为准。
