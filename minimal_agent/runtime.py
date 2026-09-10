"""Backward-compatible imports for the renamed Harness runtime."""

from .harness.engine import AgentResponse, HarnessEngine

AgentRuntime = HarnessEngine

__all__ = ["AgentResponse", "AgentRuntime", "HarnessEngine"]
