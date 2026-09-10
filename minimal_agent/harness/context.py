from __future__ import annotations

from .spec import AgentSpec
from .contracts import RequestContract
from ..skills import SkillActivation
from ..types import Message


PROTOCOL = """每次只能输出一个 JSON 对象，不要输出 Markdown。
调用工具: {"thought":"简短决策摘要","tool_call":{"name":"工具名","arguments":{}}}
直接回答: {"thought":"简短决策摘要","final":"给用户的答案"}
最终答案必须放在 final 字段中，不要直接输出未包装的业务 JSON 对象。
工具结果不足时可以继续调用工具。只能调用已提供的工具，不要虚构执行结果。
即使需要创建多个任务，本次也只能返回一个 tool_call；收到执行结果后再返回下一个 tool_call。
arguments 必须是 JSON 对象，不能是 JSON 字符串；不要返回 tool_calls 数组。"""


class ContextBuilder:
    """Compiles persisted state into the model-facing context."""

    def build(self, spec: AgentSpec, summary: str, messages: list[Message],
              active_skills: list[SkillActivation] | None = None,
              request_contract: RequestContract | None = None) -> list[Message]:
        system = f"你是 {spec.name}。\n{spec.instructions.strip()}\n\n{PROTOCOL}"
        result = [Message("system", system)]
        if active_skills:
            blocks = []
            for activation in active_skills:
                blocks.append(
                    f"## {activation.name}（版本 {activation.version}）\n"
                    f"{activation.instructions}"
                )
            result.append(Message(
                "system",
                "以下是本轮已激活的可信 Skill。严格遵循其工作流和证据边界；"
                "Skill 中提到但未提供的工具不得虚构调用。\n\n" + "\n\n".join(blocks),
            ))
            required = list(dict.fromkeys(
                tool for activation in active_skills for tool in activation.required_tools
            ))
            if required:
                result.append(Message(
                    "system",
                    "Harness 完成契约：本轮返回 final 前，必须成功调用以下工具："
                    + "、".join(required)
                    + "。这些工具构成有序完成步骤，必须按列出顺序执行；"
                    "每次仍只能调用一个工具，收到结果后继续下一项。"
                    "不得只描述‘接下来会调用’，也不得提前返回 final。",
                ))
        if request_contract is not None and request_contract.prompt_text():
            result.append(Message(
                "system",
                "Harness 已从本轮用户原话编译以下可执行契约。"
                "它们会在工具执行前和最终回答返回前由程序校验：\n"
                + request_contract.prompt_text(),
            ))
        if summary:
            result.append(Message("system", f"较早会话摘要：\n{summary}"))
        result.extend(messages)
        return result
