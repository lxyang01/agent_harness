from __future__ import annotations

import json
import re
from typing import Any

from .types import Decision, ToolCall


class DecisionParseError(ValueError):
    pass


def parse_decision(text: str) -> Decision:
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value: Any = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
        protocol_keys = {"thought", "tool_call", "tool_calls", "final", "answer"}
        # Some models return a JSON array when they intend several calls. The
        # Harness executes one action per loop, so consume the first protocol
        # action. A plain array of result objects is a structured final answer.
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if protocol_keys.intersection(value[0]):
                value = value[0]
            else:
                return Decision(final=json.dumps(value, ensure_ascii=False, indent=2))
        if not isinstance(value, dict):
            continue
        thought = value.get("thought", "")
        has_final = "final" in value or "answer" in value
        final = value.get("final", value.get("answer"))
        call = value.get("tool_call")
        if not call and isinstance(value.get("tool_calls"), list) and value["tool_calls"]:
            call = value["tool_calls"][0]
        if isinstance(call, dict) and isinstance(call.get("function"), dict):
            call = call["function"]
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = None
            if isinstance(arguments, dict):
                # A tool action takes precedence over a premature final field.
                return Decision(thought=str(thought), tool_call=ToolCall(call["name"], arguments))
        if has_final and final is not None:
            if not isinstance(final, str):
                final = json.dumps(final, ensure_ascii=False, indent=2)
            return Decision(thought=str(thought), final=final)
        # JSON-only modes sometimes follow the requested answer structure but
        # omit the outer {"final": ...} envelope. Treat a non-empty object
        # without any protocol control fields as a structured final answer.
        # Objects such as {"thought": "..."} remain invalid.
        if value and not protocol_keys.intersection(value):
            return Decision(final=json.dumps(value, ensure_ascii=False, indent=2))
    raise DecisionParseError("LLM output must contain exactly one valid tool_call or final answer")
