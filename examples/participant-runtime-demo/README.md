# Participant Runtime 本地演习

该示例从空白目录初始化一个真实的项目级 Runtime 工作区，并在不调用外部平台或模型的前提下演示：Runtime 发布、bootstrap 回执、提案指令、参与者受限输出校验、合并、预授权删除、自动回应与候选阶段。

运行：

```powershell
<python-absolute-path> examples/participant-runtime-demo/run_demo.py --workspace C:\Temp\participant-runtime-demo
```

该脚本只使用 `FakeWakeAdapter`，输出中明确标注 `platform_wake_claimed: false`。它不等于任何平台已实际唤醒；真实唤醒必须由经验证的适配器另行提供证据。

演习结束后请查看：

- `state.json`：机器流程状态；
- `instructions/<agent>/`：不可变指令；
- `receipts/<agent>/`：参与端回执；
- `audit/wake-events.json`：投递尝试；
- `archive/proposals/deletion-manifest.json`：预授权删除审计；
- 顶层讨论 Markdown：合并和候选内容。
