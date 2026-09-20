# Platform Adapters — 分发索引

本目录为协调者 Skill 提供“最小唤醒请求”的平台适配契约。它不分发完整 Skill 给参与者：完整 Skill 仅在协调者侧安装，参与者在每个项目中使用工作区内发布的轻量 Runtime。

| 平台 | 适配器 | 默认 handoff 状态 | 说明 |
|---|---|---|---|
| Claude Code | `claude/wake_adapter.py` | `unavailable` | 需要独立端到端证据和显式 dispatcher 才能接受请求 |
| Codex | `codex/wake_adapter.py` | `unavailable` | 协议已设计，未验证外部唤醒入口 |
| OpenClaw | `openclaw/wake_adapter.py` | `unavailable` | 协议已设计，未验证外部唤醒入口 |

适配器只接收 `WakeRequest` 的定位字段。新接口 `activate()` 只返回 `activated`、`manual_activation_required` 或 `activation_failed`；后台扫描不会返回 `activated`。兼容接口 `wake()` 仍映射为旧的 `accepted`、`unavailable`、`rejected`，实际执行必须由参与者 Runtime 写入的回执证明。详见 `references/platform-adapters.md`。
