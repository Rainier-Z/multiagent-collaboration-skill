#!/usr/bin/env python3
"""为每条不可变指令生成可独立执行的完整提示词。"""

from __future__ import annotations


def build_task_prompt(kind: str, agent_id: str, coordinator_id: str, inputs: list[str], output: str) -> str:
    actions = {
        "bootstrap": (
            "校验这条不可变指令、指定 Runtime 清单、身份字段与访问范围；本步骤只做激活校验并写 accepted 回执，不开始提案。"
        ),
        "propose": (
            "只依据密封视图中的项目上下文独立完成提案，回答其中的完整任务、约束与验收要求；列出依据、备选方案、风险和建议。"
            "提交前不得读取主讨论 Markdown、他方提案、回应或共享目录内容。"
        ),
        "respond": (
            "依据本指令输入视图中已经发布的讨论快照，逐项回应共识、分歧、依据、风险和待确认问题；不得改写他方产物。"
        ),
        "repair": (
            "仅根据本指令可见的失败说明修复你自己的原产物，满足原任务和验收条件；不得扩展任务、读取或改写他方文件。"
        ),
        "upgrade": "仅验证指令指定的 Runtime 版本与哈希；不要修改其他参与者的文件或状态机。",
        "stop": "停止你为本项目启动的原生 Automation 或监测；记录停止结果并写 stop 回执，不再处理业务内容。",
    }
    action = actions.get(kind)
    if action is None:
        raise ValueError("unsupported instruction kind: %s" % kind)
    allowed_inputs = "\n".join("- %s" % path for path in inputs) if inputs else "- 无"
    return (
        "你是参与者 agent_id=%s；协调者逻辑身份为 agent_id=%s。逻辑身份、平台身份和会话身份是不同字段，不得推断或替代。\n\n"
        "任务：%s\n\n"
        "执行边界：只读取下面列出的 input_paths，只写本指令 output_path 与 .multiagent/receipts/%s/ 下自己的回执；"
        "不得读取共享主讨论、未列出的工作区文件、其他参与者视图或产物，不得修改 state.json。\n\n"
        "安全说明：密封视图是路径范围的能力边界，本身不证明操作系统级硬隔离。必须由平台沙箱/读写白名单实际执行并留存执行证据；"
        "同一用户下的普通 ACL 不算硬隔离。缺少该证据时不得声称独立性已证明，应明确记录为未验证。\n\n"
        "允许输入：\n%s\n\n"
        "唯一业务输出：%s\n"
        "验收：产物须完整、可读且符合输入任务的要求；完成后由 Participant Runtime 校验并记录回执。"
    ) % (agent_id, coordinator_id, action, agent_id, allowed_inputs, output)
