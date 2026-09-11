from __future__ import annotations

import json
import re
from typing import Any, Iterable


MAX_USER_INPUT_CHARS = 32_000
MAX_MODEL_OUTPUT_CHARS = 64_000
MAX_HTTP_REQUEST_BYTES = 16 * 1024 * 1024


class GuardrailError(ValueError):
    pass


def validate_user_input(value: str) -> None:
    if len(value) > MAX_USER_INPUT_CHARS:
        raise GuardrailError(
            f"user input exceeds the {MAX_USER_INPUT_CHARS:,} character limit"
        )


def validate_model_output(value: str) -> None:
    if len(value) > MAX_MODEL_OUTPUT_CHARS:
        raise GuardrailError(
            f"model output exceeds the {MAX_MODEL_OUTPUT_CHARS:,} character limit"
        )


_PII_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "[邮箱]",
    ),
    (
        "order_id",
        re.compile(r"\b(?:ORD|ORDER|NO)[-_]?[A-Za-z0-9-]{5,}\b", re.I),
        "[订单号]",
    ),
)


def redact_pii(value: str) -> tuple[str, dict[str, int]]:
    text = value
    counts: dict[str, int] = {}
    for name, pattern, replacement in _PII_PATTERNS:
        text, count = pattern.subn(replacement, text)
        if count:
            counts[name] = count
    return text, counts


_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?")


def _numbers(value: Any) -> set[float]:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str,
    )
    text = re.sub(r"(?m)^\s*\d+[.)、]\s*", "", text)
    text = re.sub(
        r"(?i)(样本|反馈|案例|示例|sample|item)\s*\d+\s*[:：.)、]",
        r"\1：", text,
    )
    return {round(float(match.group()), 6) for match in _NUMBER.finditer(text)}


def unsupported_numeric_claims(answer: str,
                               evidence_values: Iterable[Any]) -> list[float]:
    """Return numeric claims absent from executed tool evidence.

    User text is intentionally not accepted as evidence. Tool arguments are
    accepted because they prove the runtime actually executed the requested
    bound or period; tool results support returned business metrics.
    """
    claimed = _numbers(answer)
    if not claimed:
        return []
    supported: set[float] = set()
    for value in evidence_values:
        supported.update(_numbers(value))
    return sorted(number for number in claimed if number not in supported)
