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
    summary_threshold: int = 40  # 历史消息超过该条数触发确定性压缩(0=关闭)
    summary_keep_recent: int = 12  # 压缩后保留的近期消息条数

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent name cannot be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.run_timeout is not None and self.run_timeout <= 0:
            raise ValueError("run_timeout must be positive when set")
        if self.summary_threshold < 0 or self.summary_keep_recent < 2:
            raise ValueError("summary thresholds must be non-negative (keep_recent >= 2)")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool_names cannot contain duplicates")
