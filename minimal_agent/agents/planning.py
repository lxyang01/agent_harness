from __future__ import annotations

from pathlib import Path

from ..harness import AgentSpec, HarnessEngine
from ..llm import LLM
from ..session import SessionStore
from ..tools import build_planning_registry


PLANNING_AGENT_SPEC = AgentSpec(
    name="PlanningAgent（项目规划助手）",
    instructions="""你的目标是帮助用户理解项目、补充背景资料、估算工期或成本，并维护可跟踪的项目任务。
任务必须使用稳定 ID。只有工具实际成功执行后，才能声称任务已创建、更新、完成或删除。
不要执行编码、文件修改、Shell 命令或 Git 操作；这些能力不属于本 Agent。
当需求不明确且会影响计划时，先向用户询问必要信息。""",
    tool_names=("list_docs", "read_doc", "search", "calculator", "task_create",
                "task_list", "task_update", "task_complete", "task_delete"),
    max_steps=10,
)


def create_planning_agent(llm: LLM, session_id: str, data_dir: str | Path = ".sessions",
                          docs_dir: str | Path = "docs") -> HarnessEngine:
    sessions = SessionStore(data_dir)
    tools = build_planning_registry(session_id, docs_dir, data_dir)
    return HarnessEngine(PLANNING_AGENT_SPEC, llm, tools, sessions)
