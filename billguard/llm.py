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


def _metrics() -> Any:
    """全局指标实例(billguard.metrics.METRICS)。

    惰性导入 + 全量异常吞没:本模块支持脱离包环境单独复用,埋点失败
    (无包环境/指标模块缺失)时返回 None,业务路径完全不受影响。"""
    try:
        from .metrics import METRICS
        return METRICS
    except Exception:
        return None


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
        # 埋点外壳:一次 complete() = 一次逻辑调用(calls/seconds 在 finally 记,
        # 成败都算);失败额外计 failures;重试在 _complete 内部计数
        metrics = _metrics()
        started = time.perf_counter()
        try:
            return self._complete(messages, tools, metrics)
        except Exception:
            if metrics is not None:
                metrics.inc("llm_failures_total")
            raise
        finally:
            if metrics is not None:
                metrics.inc("llm_calls_total")
                metrics.observe("llm_call_seconds", time.perf_counter() - started)

    def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                  metrics: Any = None) -> str:
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
                        if metrics is not None:
                            metrics.inc("llm_retries_total")
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
                    if metrics is not None:
                        metrics.inc("llm_retries_total")
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
