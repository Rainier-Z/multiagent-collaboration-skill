# Claude Code Participant Adapter

## 一、状态

本目录的 wake 契约处于“已设计、未验证”状态。`ClaudeCodeWakeAdapter()` 没有显式 dispatcher 时必定返回 `unavailable`。此前的文件协作演示不能证明本协议可以从文件变化自动唤醒 Claude Code 会话，因此不能将其标记为自动 handoff 可用。

## 二、部署方式

完整 Skill 仅在协调者侧安装。协调者初始化项目时，将轻量 Runtime 发布到共享工作区的 `runtime/participant/<version>/`，并为 Claude Code 参与者建立其专属 `instructions/<agent-id>/` 和 `receipts/<agent-id>/`。参与者首次被显式启动后，只验证项目 Runtime 并消费自己的指令；不安装全局 Skill。

## 三、唤醒语义

适配器只能投递最小 `WakeRequest`。消息不得包含提案、回应、主讨论正文或凭据。若宿主环境提供了已验证的 dispatcher，可注入该 dispatcher；`activate()` 返回 `activated` 仅表示请求已投递，必须继续等待项目目录中的 Runtime 回执。没有 dispatcher 时返回 `manual_activation_required`。

没有经验证 dispatcher 时，协调者记录 `E_PLATFORM_UNAVAILABLE`，由 Rainier 显式启动一次 Claude Code 参与者。文件监测器只能发现指令，不能代替这一启动动作。

## 四、参与者边界

独立提案阶段，Claude Code 参与者只读项目上下文、自己的指令和 Runtime；只能写自己的提案与回执。进入回应阶段后，只有指令明确允许时才可读取合并后的主讨论 Markdown。它永远不得写 `state.json`、主讨论 Markdown、其他参与者路径、Runtime 或指令队列。
