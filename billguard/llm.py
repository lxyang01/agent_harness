from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Protocol

import httpx


class LLM(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str: ...


class OpenAICompatibleLLM:
    """Zero-dependency Chat Completions client for OpenAI and OpenRouter."""

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str = "https://api.openai.com/v1", timeout: float = 60,
                 temperature: float | None = None, max_retries: int = 2,
                 transport: httpx.BaseTransport | None = None,
                 proxy: str | None = None) -> None:
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_retries = max_retries
        self.proxy = proxy
        self._state = threading.local()
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY or OPENROUTER_API_KEY is required for a real LLM")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
            proxy=proxy,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        enriched = self._wire_messages(messages)
        schema_note = "可用工具 JSON Schema：\n" + json.dumps(tools, ensure_ascii=False)
        enriched.insert(1 if enriched and enriched[0]["role"] == "system" else 0,
                        {"role": "system", "content": schema_note})
        request_body: dict[str, Any] = {
            "model": self.model, "messages": enriched,
            "response_format": {"type": "json_object"},
        }
        if self.temperature is not None:
            request_body["temperature"] = self.temperature
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post("/chat/completions", json=request_body)
                if response.status_code in {408, 429} or response.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                response.raise_for_status()
                data = response.json()
                self._state.usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
                self._state.model = str(data.get("model", self.model))
                return data["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:4000]
                hint = ""
                if exc.response.status_code == 403 and "not available in your region" in detail.lower():
                    hint = " Choose a region-available OpenRouter model with --model."
                raise RuntimeError(
                    f"LLM request failed: HTTP {exc.response.status_code}: "
                    f"{detail or exc.response.reason_phrase}.{hint}"
                ) from exc
            except (httpx.RequestError, KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                if isinstance(exc, httpx.RequestError) and attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise RuntimeError(f"LLM request failed: {exc}") from exc
        raise RuntimeError("LLM request failed after retries")

    def close(self) -> None:
        self._client.close()

    @property
    def last_usage(self) -> dict[str, Any]:
        return dict(getattr(self._state, "usage", {}))

    @property
    def last_model(self) -> str:
        return str(getattr(self._state, "model", self.model))

    @staticmethod
    def _wire_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for message in messages:
            role = message["role"]
            content = str(message.get("content", ""))
            if role == "tool":
                result.append({"role": "user", "content": f"[工具 {message.get('name', 'unknown')} 的执行结果]\n{content}"})
            else:
                result.append({"role": role, "content": content})
        return result


class MockLLM:
    """Deterministic offline PlanningAgent model double."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            pending = self._latest_user(messages)
            used = [m.get("name") for m in messages if m["role"] == "tool"]
            if any(word in pending for word in ("拆分", "创建任务", "生成任务")):
                created_count = used.count("task_create")
                if last.get("name") == "read_doc" and created_count == 0:
                    return self._call("task_create", {
                        "title": "确认需求范围与验收标准", "priority": "high", "estimate_hours": 2,
                        "notes": "根据需求文档创建",
                    }, "已读取文档，开始创建项目任务")
                if last.get("name") == "task_create" and created_count == 1:
                    return self._call("task_create", {
                        "title": "实现并验证核心功能", "priority": "high", "estimate_hours": 8,
                        "notes": "完成后运行测试",
                    }, "继续创建实施任务")
            return json.dumps({"thought": "工具结果足以回答",
                               "final": self._format_result(last.get("name", "tool"), result)}, ensure_ascii=False)

        text = last["content"].strip()
        lower = text.lower()
        if any(word in text for word in ("刚才", "之前", "上面")):
            previous = next((m for m in reversed(messages[:-1]) if m["role"] == "tool"), None)
            if previous:
                return json.dumps({"thought": "从 Session 历史回答追问",
                                   "final": self._format_result(previous.get("name", "tool"), json.loads(previous["content"]))}, ensure_ascii=False)
        if "文档" in text and any(word in text for word in ("列出", "有哪些", "查看目录")):
            return self._call("list_docs", {}, "需要列出项目文档")
        if any(word in text for word in ("读取", "阅读")):
            match = re.search(r"([\w./\\-]+\.(?:md|txt|json))", text, re.IGNORECASE)
            if match:
                path = match.group(1).replace("\\", "/")
                if path.startswith("docs/"):
                    path = path[5:]
                return self._call("read_doc", {"path": path}, "需要读取项目文档")
        task_id = self._task_id(text)
        if task_id and any(word in text for word in ("完成", "标记完成")):
            return self._call("task_complete", {"task_id": task_id}, "需要完成指定任务")
        if task_id and any(word in text for word in ("删除", "移除")):
            return self._call("task_delete", {"task_id": task_id}, "需要删除指定任务")
        if "任务" in text and any(word in text for word in ("列出", "查看", "有哪些", "剩余")):
            arguments = {"status": "pending"} if "剩余" in text else {}
            return self._call("task_list", arguments, "需要读取项目任务")
        if "创建任务" in text or "添加任务" in text:
            title = re.sub(r"^.*?(?:创建|添加)任务[：:\s]*", "", text).strip() or text
            return self._call("task_create", {"title": title}, "需要创建项目任务")
        if any(word in text for word in ("计算", "算一下", "总工时")):
            return self._call("calculator", {"expression": self._expression(text)}, "需要精确计算")
        if any(word in text for word in ("搜索", "查资料", "search")):
            query = re.sub(r"^(请)?(帮我)?(搜索|查资料)", "", text).strip() or text
            return self._call("search", {"query": query}, "需要补充项目资料")
        if any(word in text for word in ("功能", "能做什么")):
            return json.dumps({"thought": "介绍 PlanningAgent 能力", "final":
                "我可以读取项目文档、搜索补充资料、计算工期或成本，并创建、查询、更新、完成和删除结构化项目任务。"}, ensure_ascii=False)
        return json.dumps({"thought": "无需工具", "final": f"我已记录你的项目信息：{text}"}, ensure_ascii=False)

    @staticmethod
    def _call(name: str, arguments: dict[str, Any], thought: str) -> str:
        return json.dumps({"thought": thought, "tool_call": {"name": name, "arguments": arguments}}, ensure_ascii=False)

    @staticmethod
    def _latest_user(messages: list[dict[str, Any]]) -> str:
        return next(m["content"] for m in reversed(messages) if m["role"] == "user")

    @staticmethod
    def _task_id(text: str) -> str | None:
        match = re.search(r"\bT\d{3,}\b", text, re.IGNORECASE)
        return match.group(0).upper() if match else None

    @staticmethod
    def _expression(text: str) -> str:
        matches = re.findall(r"[\d\s+\-*/().%]+", text)
        values = [value.strip() for value in matches if value.strip()]
        return max(values, key=len) if values else text

    @staticmethod
    def _format_result(name: str, result: dict[str, Any]) -> str:
        if "error" in result:
            return f"工具执行失败：{result['error']}"
        if name == "calculator":
            return f"计算结果是 {result['result']}。"
        if name == "search":
            return "搜索结果：" + "；".join(item["snippet"] for item in result["results"])
        if name == "list_docs":
            return "项目文档：" + ("、".join(result["documents"]) if result["documents"] else "暂无")
        if name == "read_doc":
            return f"已读取 {result['path']}：\n{result['content']}"
        if name == "task_create":
            task = result["created"]
            return f"已创建任务 {task['id']}：{task['title']}。"
        if name == "task_list":
            if not result["tasks"]:
                return "当前没有符合条件的项目任务。"
            return "项目任务：" + "；".join(
                f"{task['id']} [{task['status']}] {task['title']}" for task in result["tasks"])
        if name in {"task_update", "task_complete"}:
            task = result["updated"]
            return f"已更新任务 {task['id']}：状态为 {task['status']}。"
        if name == "task_delete":
            task = result["deleted"]
            return f"已删除任务 {task['id']}：{task['title']}。"
        return json.dumps(result, ensure_ascii=False)
