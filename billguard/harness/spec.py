from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    """Declarative definition consumed by the generic Harness."""

    name: str
    instructions: str
    tool_names: tuple[str, ...]
    max_steps: int = 8
    run_timeout: float | None = None  # 单次 run 的总执行时间预算(秒),None=不限

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent name cannot be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.run_timeout is not None and self.run_timeout <= 0:
            raise ValueError("run_timeout must be positive when set")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool_names cannot contain duplicates")
