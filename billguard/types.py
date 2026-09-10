from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            result["name"] = self.name
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Message":
        return cls(
            role=value["role"], content=value["content"],
            name=value.get("name"), tool_call_id=value.get("tool_call_id")
        )


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class Decision:
    thought: str = ""
    tool_call: ToolCall | None = None
    final: str | None = None


@dataclass
class Session:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    owner: str | None = None

