from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .skills import SkillRuntime


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    input: str
    expected_skills: tuple[str, ...]
    expected_required_tools: tuple[str, ...] = ()


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid eval JSONL at line {line_number}: {exc}") from exc
            required = {"id", "category", "input", "expected_skills"}
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError(f"invalid eval case at line {line_number}")
            case_id = str(item["id"]).strip()
            if not case_id or case_id in seen:
                raise ValueError(f"duplicate or empty eval case id: {case_id}")
            skills = item["expected_skills"]
            tools = item.get("expected_required_tools", [])
            if not isinstance(skills, list) or not all(isinstance(value, str) for value in skills):
                raise ValueError(f"expected_skills must be an array: {case_id}")
            if not isinstance(tools, list) or not all(isinstance(value, str) for value in tools):
                raise ValueError(f"expected_required_tools must be an array: {case_id}")
            seen.add(case_id)
            cases.append(EvalCase(
                case_id, str(item["category"]), str(item["input"]),
                tuple(skills), tuple(tools),
            ))
    if not cases:
        raise ValueError("evaluation dataset is empty")
    return cases


class RoutingEvaluator:
    """Deterministic ablation benchmark for routing and completion contracts."""

    VARIANTS = ("baseline", "skills", "full")

    def __init__(self, skills: SkillRuntime) -> None:
        self.skills = skills

    def run(self, cases: list[EvalCase], variant: str = "full") -> dict[str, Any]:
        if variant not in self.VARIANTS:
            raise ValueError(f"unknown evaluation variant: {variant}")
        results: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            if variant == "baseline":
                predicted_skills = ("bill-triage",)
                predicted_required: tuple[str, ...] = ()
            else:
                activations = self.skills.activate(case.input)
                predicted_skills = tuple(item.name for item in activations)
                predicted_required = (() if variant == "skills" else tuple(dict.fromkeys(
                    tool for item in activations for tool in item.required_tools
                )))
            latency_ms = round((time.perf_counter() - started) * 1000, 4)
            latencies.append(latency_ms)
            skill_pass = set(predicted_skills) == set(case.expected_skills)
            contract_pass = set(predicted_required) == set(case.expected_required_tools)
            results.append({
                "id": case.id,
                "category": case.category,
                "input": case.input,
                "expected_skills": list(case.expected_skills),
                "predicted_skills": list(predicted_skills),
                "expected_required_tools": list(case.expected_required_tools),
                "predicted_required_tools": list(predicted_required),
                "skill_pass": skill_pass,
                "contract_pass": contract_pass,
                "passed": skill_pass and contract_pass,
                "latency_ms": latency_ms,
            })
        category_metrics: dict[str, dict[str, Any]] = {}
        for category in sorted({case.category for case in cases}):
            selected = [item for item in results if item["category"] == category]
            category_metrics[category] = {
                "total": len(selected),
                "passed": sum(item["passed"] for item in selected),
                "accuracy": self._ratio(sum(item["passed"] for item in selected), len(selected)),
            }
        contract_cases = [item for item in results if item["expected_required_tools"]]
        return {
            "schema_version": 1,
            "evaluation_type": "routing",
            "benchmark": "billguard-routing-v1",
            "variant": variant,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {
                "total": len(results),
                "passed": sum(item["passed"] for item in results),
                "overall_accuracy": self._ratio(sum(item["passed"] for item in results), len(results)),
                "skill_routing_accuracy": self._ratio(sum(item["skill_pass"] for item in results), len(results)),
                "completion_contract_accuracy": self._ratio(
                    sum(item["contract_pass"] for item in contract_cases), len(contract_cases),
                ) if contract_cases else None,
                "average_latency_ms": round(sum(latencies) / len(latencies), 4),
                "p95_latency_ms": self._percentile(latencies, 0.95),
            },
            "categories": category_metrics,
            "results": results,
            "scope_note": (
                "Deterministic routing/contract benchmark only. It does not measure LLM groundedness, "
                "answer quality, tool argument accuracy, or end-to-end task success."
            ),
        }

    def compare(self, cases: list[EvalCase]) -> dict[str, Any]:
        reports = [self.run(cases, variant) for variant in self.VARIANTS]
        return {
            "schema_version": 1,
            "evaluation_type": "routing",
            "benchmark": "billguard-routing-v1-ablation",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "dataset_size": len(cases),
            "variants": [{"variant": report["variant"], "metrics": report["metrics"]}
                         for report in reports],
            "reports": reports,
            "scope_note": reports[-1]["scope_note"],
        }

    @staticmethod
    def _ratio(value: int, total: int) -> float:
        return round(value / total, 4) if total else 0.0

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
        return round(ordered[index], 4)


def save_evaluation_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"routing-eval-{stamp}"
    json_path = root / f"{stem}.json"
    markdown_path = root / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    variants = report.get("variants", [])
    lines = [
        "# BillGuard Routing Evaluation",
        "",
        f"- Dataset size: {report.get('dataset_size', 0)}",
        f"- Evaluated at: {report.get('evaluated_at', '')}",
        "",
        "| Variant | Overall | Skill routing | Completion contract | Avg latency | P95 latency |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in variants:
        metrics = item["metrics"]
        contract = metrics.get("completion_contract_accuracy")
        contract_text = "N/A" if contract is None else f"{contract:.1%}"
        lines.append(
            f"| {item['variant']} | {metrics['overall_accuracy']:.1%} | "
            f"{metrics['skill_routing_accuracy']:.1%} | "
            f"{contract_text} | {metrics['average_latency_ms']:.4f} ms | "
            f"{metrics['p95_latency_ms']:.4f} ms |"
        )
    lines.extend(["", f"> {report.get('scope_note', '')}", ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path.resolve()), "markdown": str(markdown_path.resolve())}


class EvaluationReportStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        reports: list[dict[str, Any]] = []
        for path in self.root.glob("*-eval-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                summary = {
                    "filename": path.name,
                    "evaluation_type": value.get("evaluation_type", "routing"),
                    "benchmark": value.get("benchmark", ""),
                    "evaluated_at": value.get("evaluated_at", ""),
                    "dataset_size": value.get("dataset_size", 0),
                    "variants": value.get("variants", []),
                    "model": value.get("model", ""),
                    "temperature": value.get("temperature"),
                    "repeats": value.get("repeats", 1),
                    "metrics": value.get("metrics", {}),
                    "scope_note": value.get("scope_note", ""),
                }
                reports.append(summary)
            except (OSError, json.JSONDecodeError):
                continue
        reports.sort(key=lambda item: item.get("evaluated_at", ""), reverse=True)
        return reports[:limit]
