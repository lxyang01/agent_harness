from __future__ import annotations

import ast
import hashlib
import json
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .policy import ToolPolicy


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    result_formatter: Callable[[Any], str] | None = None
    policy: ToolPolicy = ToolPolicy()

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        return tool

    def schemas(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        selected = tuple(names) if names is not None else self.names()
        return [self._tools[name].schema() for name in selected]

    def execute(self, name: str, arguments: dict[str, Any], allowed: Iterable[str] | None = None) -> Any:
        if allowed is not None and name not in set(allowed):
            raise ToolError(f"tool is not enabled for this agent: {name}")
        tool = self._tools.get(name)
        if not tool:
            raise ToolError(f"unknown tool: {name}")
        self._validate(tool.parameters, arguments)
        try:
            return tool.handler(**arguments)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{name} failed: {exc}") from exc

    def format_result(self, name: str, result: Any) -> str | None:
        tool = self._tools.get(name)
        if not tool or not tool.result_formatter:
            return None
        return tool.result_formatter(result)

    @staticmethod
    def _validate(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolError("tool arguments must be an object")
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in arguments:
                raise ToolError(f"missing required argument: {key}")
        if schema.get("additionalProperties") is False:
            extra = set(arguments) - set(properties)
            if extra:
                raise ToolError(f"unexpected argument(s): {', '.join(sorted(extra))}")
        python_types = {"string": str, "number": (int, float), "integer": int,
                        "boolean": bool, "array": list, "object": dict}
        for key, value in arguments.items():
            rule = properties.get(key, {})
            expected = rule.get("type")
            if expected in python_types:
                valid = isinstance(value, python_types[expected])
                if expected in {"integer", "number"} and isinstance(value, bool):
                    valid = False
                if not valid:
                    raise ToolError(f"argument {key} must be {expected}")
            if "enum" in rule and value not in rule["enum"]:
                raise ToolError(f"argument {key} must be one of {rule['enum']}")
            if "minimum" in rule and value < rule["minimum"]:
                raise ToolError(f"argument {key} must be >= {rule['minimum']}")


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_OPS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.UAdd: operator.pos, ast.USub: operator.neg,
}


def calculator(expression: str) -> dict[str, Any]:
    if len(expression) > 200:
        raise ToolError("expression is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError("invalid arithmetic expression") from exc

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ToolError("exponent is too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](visit(node.operand))
        raise ToolError("only arithmetic operators and numbers are allowed")

    return {"expression": expression, "result": visit(tree)}


def project_search(query: str) -> dict[str, Any]:
    """Deterministic offline search backend suitable for tests and demos."""
    corpus = {
        "agent": "Agent Runtime 通常包含模型适配、工具执行、状态管理、循环控制与可观测性。",
        "harness": "Harness 将模型决策约束在受控循环内，并负责预算、工具权限、状态和 Trace。",
        "context": "Context 应优先保留目标、关键决定、近期消息和相关工具结果。",
        "测试": "Agent 测试应覆盖直接回答、多工具循环、异常、最大步数、Session 隔离和状态恢复。",
        "python": "Python 标准库可用于构建轻量 CLI、JSON 持久化和 HTTP 客户端。",
    }
    hits = [{"title": key, "snippet": value, "url": f"mock://project-search/{key}"}
            for key, value in corpus.items() if key.lower() in query.lower()]
    if not hits:
        hits = [{"title": query, "snippet": "未命中本地语料，这是离线 Mock 搜索结果。",
                 "url": "mock://project-search/no-hit"}]
    return {"query": query, "results": hits, "source": "mock"}


class DocumentService:
    ALLOWED_SUFFIXES = {".md", ".txt", ".json"}

    def __init__(self, root: str | Path, max_chars: int = 20_000) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_chars = max_chars

    def list_docs(self) -> dict[str, Any]:
        files = [str(path.relative_to(self.root)).replace("\\", "/")
                 for path in self.root.rglob("*")
                 if path.is_file() and path.suffix.lower() in self.ALLOWED_SUFFIXES]
        return {"documents": sorted(files), "root": str(self.root)}

    def read_doc(self, path: str) -> dict[str, Any]:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError("document path escapes the configured docs directory")
        if target.suffix.lower() not in self.ALLOWED_SUFFIXES:
            raise ToolError("unsupported document type; allowed: .md, .txt, .json")
        if not target.is_file():
            raise ToolError(f"document not found: {path}")
        content = target.read_text(encoding="utf-8")
        truncated = len(content) > self.max_chars
        return {"path": str(target.relative_to(self.root)).replace("\\", "/"),
                "content": content[:self.max_chars], "truncated": truncated}


class TaskService:
    PRIORITIES = {"low", "medium", "high"}
    STATUSES = {"pending", "in_progress", "completed"}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    def _load(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise TypeError("task file must contain a list")
            return value
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ToolError(f"cannot load tasks: {exc}") from exc

    def _save(self, session_id: str, tasks: list[dict[str, Any]]) -> None:
        self._path(session_id).write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _find(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
        task = next((item for item in tasks if item["id"].lower() == task_id.lower()), None)
        if not task:
            raise ToolError(f"task not found: {task_id}")
        return task

    def create(self, session_id: str, title: str, priority: str = "medium",
               estimate_hours: float | None = None, notes: str = "") -> dict[str, Any]:
        tasks = self._load(session_id)
        next_number = max([int(task["id"][1:]) for task in tasks] or [0]) + 1
        task = {"id": f"T{next_number:03d}", "title": title, "status": "pending",
                "priority": priority, "estimate_hours": estimate_hours, "notes": notes}
        tasks.append(task)
        self._save(session_id, tasks)
        return {"created": task, "count": len(tasks), "storage_path": str(self._path(session_id))}

    def list(self, session_id: str, status: str | None = None) -> dict[str, Any]:
        tasks = self._load(session_id)
        selected = [task for task in tasks if status is None or task["status"] == status]
        return {"tasks": selected, "count": len(selected), "filter": status,
                "storage_path": str(self._path(session_id))}

    def update(self, session_id: str, task_id: str, title: str | None = None,
               priority: str | None = None, estimate_hours: float | None = None,
               notes: str | None = None, status: str | None = None) -> dict[str, Any]:
        tasks = self._load(session_id)
        task = self._find(tasks, task_id)
        updates = {key: value for key, value in {
            "title": title, "priority": priority, "estimate_hours": estimate_hours,
            "notes": notes, "status": status,
        }.items() if value is not None}
        if not updates:
            raise ToolError("at least one task field must be updated")
        task.update(updates)
        self._save(session_id, tasks)
        return {"updated": task, "storage_path": str(self._path(session_id))}

    def complete(self, session_id: str, task_id: str) -> dict[str, Any]:
        return self.update(session_id, task_id, status="completed")

    def delete(self, session_id: str, task_id: str) -> dict[str, Any]:
        tasks = self._load(session_id)
        task = self._find(tasks, task_id)
        tasks.remove(task)
        self._save(session_id, tasks)
        return {"deleted": task, "remaining_count": len(tasks),
                "storage_path": str(self._path(session_id))}


def _task_line(task: dict[str, Any]) -> str:
    hours = f", {task['estimate_hours']}h" if task.get("estimate_hours") is not None else ""
    return f"{task['id']} [{task['status']}/{task['priority']}{hours}] {task['title']}"


def _format_task_create(result: dict[str, Any]) -> str:
    return "已创建 " + _task_line(result["created"])


def _format_task_list(result: dict[str, Any]) -> str:
    tasks = result["tasks"]
    if not tasks:
        return "当前没有符合条件的项目任务"
    total = sum(float(task["estimate_hours"]) for task in tasks if task.get("estimate_hours") is not None)
    lines = [f"共 {len(tasks)} 条任务，已估算工时合计 {total:g}h："]
    lines.extend(_task_line(task) for task in tasks)
    return "\n".join(lines)


def _format_task_update(result: dict[str, Any]) -> str:
    return "已更新 " + _task_line(result["updated"])


def _format_task_delete(result: dict[str, Any]) -> str:
    return f"已删除 {result['deleted']['id']} {result['deleted']['title']}"


def build_planning_registry(session_id: str, docs_root: str | Path,
                            state_root: str | Path) -> ToolRegistry:
    docs = DocumentService(docs_root)
    tasks = TaskService(Path(state_root) / "tasks")
    registry = ToolRegistry()
    registry.register(Tool("calculator", "精确计算项目工期、成本、比例等数学表达式",
                           _object({"expression": {"type": "string"}}, ["expression"]), calculator))
    registry.register(Tool("search", "搜索项目规划和技术背景信息（当前为离线 Mock）",
                           _object({"query": {"type": "string"}}, ["query"]), project_search))
    registry.register(Tool("list_docs", "列出项目文档目录中的可读文档", _object({}, []), docs.list_docs))
    registry.register(Tool("read_doc", "读取项目文档；路径相对于受限 docs 目录",
                           _object({"path": {"type": "string"}}, ["path"]), docs.read_doc))
    priority = {"type": "string", "enum": ["low", "medium", "high"]}
    status = {"type": "string", "enum": ["pending", "in_progress", "completed"]}
    registry.register(Tool("task_create", "创建结构化项目任务",
        _object({"title": {"type": "string"}, "priority": priority,
                 "estimate_hours": {"type": "number", "minimum": 0}, "notes": {"type": "string"}}, ["title"]),
        lambda **kwargs: tasks.create(session_id, **kwargs), _format_task_create))
    registry.register(Tool("task_list", "列出当前项目任务，可按状态过滤",
        _object({"status": status}, []), lambda **kwargs: tasks.list(session_id, **kwargs), _format_task_list))
    registry.register(Tool("task_update", "按稳定任务 ID 更新任务字段",
        _object({"task_id": {"type": "string"}, "title": {"type": "string"}, "priority": priority,
                 "estimate_hours": {"type": "number", "minimum": 0}, "notes": {"type": "string"}, "status": status}, ["task_id"]),
        lambda **kwargs: tasks.update(session_id, **kwargs), _format_task_update))
    registry.register(Tool("task_complete", "将指定任务标记为已完成",
        _object({"task_id": {"type": "string"}}, ["task_id"]),
        lambda task_id: tasks.complete(session_id, task_id), _format_task_update))
    registry.register(Tool("task_delete", "按稳定任务 ID 删除项目任务",
        _object({"task_id": {"type": "string"}}, ["task_id"]),
        lambda task_id: tasks.delete(session_id, task_id), _format_task_delete))
    return registry
