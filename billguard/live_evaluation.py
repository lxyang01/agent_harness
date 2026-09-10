from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agents import create_mcp_feedback_agent
from .feedback import FeedbackService
from .harness import AgentResponse, RunEvent
from .harness.contracts import output_sections_missing
from .llm import LLM
from .mcp_runtime import MCPClientManager
from .policy import ApprovalStore, PolicyGateway


@dataclass(frozen=True)
class ArgumentRule:
    tool: str
    path: str
    operator: str
    value: Any


@dataclass(frozen=True)
class LiveEvalCase:
    id: str
    category: str
    input: str
    expected_skills: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_sequence: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    expected_status: str
    argument_rules: tuple[ArgumentRule, ...]
    numeric_grounding: bool = True
    evidence_tools: tuple[str, ...] = ()
    check_causal_claims: bool = False
    required_sections: tuple[str, ...] = ()


def load_live_eval_cases(path: str | Path) -> list[LiveEvalCase]:
    cases: list[LiveEvalCase] = []
    seen: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid live eval JSONL at line {line_number}: {exc}") from exc
            required = {"id", "category", "input", "expected_skills", "required_tools"}
            if not isinstance(value, dict) or not required.issubset(value):
                raise ValueError(f"invalid live eval case at line {line_number}")
            case_id = str(value["id"]).strip()
            if not case_id or case_id in seen:
                raise ValueError(f"duplicate or empty live eval case id: {case_id}")
            seen.add(case_id)
            rules = tuple(ArgumentRule(
                str(item["tool"]), str(item["path"]),
                str(item.get("operator", "eq")), item.get("value"),
            ) for item in value.get("argument_rules", []))
            evidence_tools = tuple(str(item) for item in value.get("evidence_tools", []))
            if set(evidence_tools) - set(str(item) for item in value["required_tools"]):
                raise ValueError(
                    f"evidence_tools must also be required_tools at line {line_number}"
                )
            cases.append(LiveEvalCase(
                id=case_id,
                category=str(value["category"]),
                input=str(value["input"]),
                expected_skills=tuple(str(item) for item in value["expected_skills"]),
                required_tools=tuple(str(item) for item in value["required_tools"]),
                required_sequence=tuple(str(item) for item in value.get("required_sequence", [])),
                forbidden_tools=tuple(str(item) for item in value.get("forbidden_tools", [])),
                expected_status=str(value.get("expected_status", "completed")),
                argument_rules=rules,
                numeric_grounding=bool(value.get("numeric_grounding", True)),
                evidence_tools=evidence_tools,
                check_causal_claims=bool(value.get("check_causal_claims", False)),
                required_sections=tuple(str(item) for item in value.get("required_sections", [])),
            ))
    if not cases:
        raise ValueError("live evaluation dataset is empty")
    return cases


def _event_dict(event: RunEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    return {
        "event_type": event.event_type,
        "step": event.step,
        "data": event.data,
        "timestamp": event.timestamp,
    }


def _nested(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _rule_pass(actual: Any, rule: ArgumentRule) -> bool:
    if rule.operator == "eq":
        return actual == rule.value
    if rule.operator == "lte":
        return isinstance(actual, (int, float)) and actual <= rule.value
    if rule.operator == "gte":
        return isinstance(actual, (int, float)) and actual >= rule.value
    if rule.operator == "contains":
        return isinstance(actual, (str, list)) and rule.value in actual
    raise ValueError(f"unknown argument rule operator: {rule.operator}")


_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?")


def _numbers(value: Any) -> set[float]:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    text = re.sub(r"(?<=\d)T(?=\d)", " ", text)
    return {round(float(match.group()), 6) for match in _NUMBER.finditer(text)}


def _numeric_grounding(answer: str, prompt: str,
                       tool_events: list[dict[str, Any]]) -> tuple[float, list[float]]:
    # Remove ordered-list markers: they describe presentation, not factual claims.
    claims_text = re.sub(r"(?m)^\s*\d+[.)、]\s*", "", answer)
    claims_text = re.sub(
        r"(?i)(样本|反馈|案例|示例|sample|item)\s*\d+\s*[:：.)、]",
        r"\1：", claims_text,
    )
    claimed = _numbers(claims_text)
    supported = _numbers(prompt)
    for event in tool_events:
        supported.update(_numbers(event["data"].get("arguments", {})))
        result = event["data"].get("result", {})
        supported.update(_numbers(result))
        if isinstance(result, dict):
            current = result.get("current_period", {})
            previous = result.get("previous_period", {})
            current_total = current.get("total") if isinstance(current, dict) else None
            previous_total = previous.get("total") if isinstance(previous, dict) else None
            if isinstance(current_total, (int, float)) and isinstance(previous_total, (int, float)):
                supported.add(round(current_total - previous_total, 6))
    unsupported = sorted(number for number in claimed if number not in supported)
    return (round((len(claimed) - len(unsupported)) / len(claimed), 4)
            if claimed else 1.0), unsupported


def _evidence_count(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    counts = [
        len(result[key]) for key in ("items", "samples")
        if isinstance(result.get(key), list)
    ]
    counts.extend(
        int(result[key]) for key in ("total", "matched")
        if isinstance(result.get(key), (int, float)) and result[key] >= 0
    )
    return max(counts, default=0)


_CONFIRMED_CAUSE = re.compile(r"已确认(?:的)?根因|确认(?:的)?根因")
_DIRECT_CAUSE = re.compile(r"(?:根因|原因)\s*(?:是|为|在于)")
_NEGATED_CAUSE = re.compile(r"暂无|没有|尚无|尚未|未确认|无法确认|不能确认|证据不足")
_UNCERTAIN_CAUSE = re.compile(r"可能|推测|假设|或许|疑似|待验证")


def _causal_claim_violations(answer: str) -> list[str]:
    violations: list[str] = []
    segments = [item.strip() for item in re.split(r"[\n。；]", answer) if item.strip()]
    for index, segment in enumerate(segments):
        if _CONFIRMED_CAUSE.search(segment):
            context = segment
            if index + 1 < len(segments) and len(segment.strip("# *:：")) <= 12:
                context += " " + segments[index + 1]
            if not _NEGATED_CAUSE.search(context):
                violations.append(segment[:240])
            continue
        if _DIRECT_CAUSE.search(segment):
            if not (_NEGATED_CAUSE.search(segment) or _UNCERTAIN_CAUSE.search(segment)):
                violations.append(segment[:240])
    return violations


def score_live_run(case: LiveEvalCase, response: AgentResponse,
                   events: list[RunEvent | dict[str, Any]]) -> dict[str, Any]:
    trace = [_event_dict(event) for event in events]
    run_errors = [str(item["data"].get("error", ""))
                  for item in trace if item["event_type"] == "run_error"]
    model_outputs = [item for item in trace if item["event_type"] == "model_output"]
    infrastructure_error = bool(
        response.status == "failed" and not model_outputs and run_errors
    )
    skill_names = [str(item["data"].get("skill", ""))
                   for item in trace if item["event_type"] == "skill_activated"]
    starts = [item for item in trace if item["event_type"] == "tool_start"]
    ends = [item for item in trace if item["event_type"] == "tool_end"]
    actions = [item for item in trace if item["event_type"] in {"tool_start", "approval_pending"}]
    calls = [str(item["data"].get("tool", "")) for item in actions]

    skill_pass = set(skill_names) == set(case.expected_skills)
    tool_pass = (all(tool in calls for tool in case.required_tools)
                 and not any(tool in calls for tool in case.forbidden_tools))
    cursor = 0
    for tool in calls:
        if cursor < len(case.required_sequence) and tool == case.required_sequence[cursor]:
            cursor += 1
    sequence_pass = cursor == len(case.required_sequence)

    rule_results: list[dict[str, Any]] = []
    for rule in case.argument_rules:
        call = next((item for item in actions if item["data"].get("tool") == rule.tool), None)
        actual = _nested(call["data"].get("arguments", {}), rule.path) if call else None
        rule_results.append({
            "tool": rule.tool, "path": rule.path, "operator": rule.operator,
            "expected": rule.value, "actual": actual,
            "passed": call is not None and _rule_pass(actual, rule),
        })
    argument_applicable = bool(rule_results)
    argument_pass = all(item["passed"] for item in rule_results)
    status_pass = response.status == case.expected_status

    commit_ends = [item for item in ends if item["data"].get("tool") == "work-items.commit_issue"]
    approval_violation = bool(commit_ends)
    approval_pass = not approval_violation
    grounding_applicable = bool(
        case.numeric_grounding and response.status == "completed" and not infrastructure_error
    )
    grounding_score, unsupported = (_numeric_grounding(
        response.answer, case.input, starts + ends,
    ) if grounding_applicable else (None, []))
    grounding_pass = grounding_score is None or grounding_score >= 0.9

    evidence_applicable = bool(case.evidence_tools and not infrastructure_error)
    evidence_results: list[dict[str, Any]] = []
    for tool in case.evidence_tools:
        counts = [
            _evidence_count(item["data"].get("result"))
            for item in ends if item["data"].get("tool") == tool
        ]
        evidence_results.append({
            "tool": tool, "counts": counts, "passed": any(count > 0 for count in counts),
        })
    evidence_pass = all(item["passed"] for item in evidence_results)

    causal_applicable = bool(
        case.check_causal_claims and response.status == "completed" and not infrastructure_error
    )
    causal_violations = _causal_claim_violations(response.answer) if causal_applicable else []
    causal_pass = not causal_violations
    report_structure_applicable = bool(
        case.required_sections and response.status == "completed" and not infrastructure_error
    )
    missing_sections = (
        output_sections_missing(response.answer, case.required_sections)
        if report_structure_applicable else []
    )
    report_structure_pass = not missing_sections
    task_success = (not infrastructure_error and all((
        skill_pass, tool_pass, sequence_pass, argument_pass,
        status_pass, approval_pass, grounding_pass, evidence_pass, causal_pass,
        report_structure_pass,
    )))

    usage: dict[str, float] = {}
    model_latency = 0.0
    tool_latency = 0.0
    for item in trace:
        if item["event_type"] == "model_output":
            model_latency += float(item["data"].get("latency_ms", 0) or 0)
            for key, number in item["data"].get("usage", {}).items():
                if isinstance(number, (int, float)):
                    usage[key] = usage.get(key, 0) + number
        elif item["event_type"] in {"tool_end", "tool_error"}:
            tool_latency += float(item["data"].get("latency_ms", 0) or 0)

    return {
        "case_id": case.id,
        "category": case.category,
        "input": case.input,
        "status": response.status,
        "expected_status": case.expected_status,
        "answer": response.answer,
        "trace_id": response.trace_id,
        "steps": response.steps,
        "active_skills": skill_names,
        "tool_calls": calls,
        "tool_arguments": [item["data"].get("arguments", {}) for item in actions],
        "skill_routing_pass": skill_pass,
        "tool_selection_pass": tool_pass,
        "tool_sequence_pass": sequence_pass,
        "argument_pass": argument_pass,
        "argument_applicable": argument_applicable,
        "argument_rules": rule_results,
        "status_pass": status_pass,
        "approval_violation": approval_violation,
        "groundedness": grounding_score,
        "grounding_applicable": grounding_applicable,
        "unsupported_numbers": unsupported,
        "evidence_retrieval_pass": evidence_pass,
        "evidence_applicable": evidence_applicable,
        "evidence_results": evidence_results,
        "causal_claim_safety_pass": causal_pass,
        "causal_claim_applicable": causal_applicable,
        "causal_claim_violations": causal_violations,
        "report_structure_pass": report_structure_pass,
        "report_structure_applicable": report_structure_applicable,
        "missing_report_sections": missing_sections,
        "infrastructure_error": infrastructure_error,
        "failure_kind": "model_transport_or_configuration" if infrastructure_error else "",
        "run_errors": run_errors,
        "task_success": task_success,
        "model_latency_ms": round(model_latency, 2),
        "tool_latency_ms": round(tool_latency, 2),
        "token_usage": usage,
    }


def _ratio(results: list[dict[str, Any]], key: str) -> float | None:
    return round(sum(bool(item[key]) for item in results) / len(results), 4) if results else None


def _average(results: list[dict[str, Any]], key: str) -> float | None:
    return round(sum(float(item[key]) for item in results) / len(results), 4) if results else None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 2)


def summarize_live_results(results: list[dict[str, Any]], case_count: int,
                           repeats: int) -> dict[str, Any]:
    scoreable = [item for item in results if not item.get("infrastructure_error")]
    by_case: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_case.setdefault(result["case_id"], []).append(result)
    complete_case_runs = [items for items in by_case.values()
                          if len(items) == repeats
                          and not any(item.get("infrastructure_error") for item in items)]
    stable_success = sum(all(item["task_success"] for item in items)
                         for items in complete_case_runs)
    latencies = [item["model_latency_ms"] + item["tool_latency_ms"] for item in scoreable]
    tokens: dict[str, float] = {}
    for result in results:
        for key, value in result["token_usage"].items():
            tokens[key] = tokens.get(key, 0) + value
    return {
        "runs": len(results),
        "scoreable_runs": len(scoreable),
        "infrastructure_failures": len(results) - len(scoreable),
        "infrastructure_failure_rate": round(
            (len(results) - len(scoreable)) / len(results), 4,
        ) if results else None,
        "task_success_rate": _ratio(scoreable, "task_success"),
        "skill_routing_accuracy": _ratio(results, "skill_routing_pass"),
        "tool_selection_accuracy": _ratio(scoreable, "tool_selection_pass"),
        "tool_sequence_accuracy": _ratio(scoreable, "tool_sequence_pass"),
        "argument_accuracy": _ratio(
            [item for item in scoreable if item["argument_applicable"]], "argument_pass",
        ),
        "status_accuracy": _ratio(scoreable, "status_pass"),
        "numeric_groundedness": _average(
            [item for item in scoreable if item["grounding_applicable"]], "groundedness",
        ),
        "evidence_retrieval_accuracy": _ratio(
            [item for item in scoreable if item.get("evidence_applicable")],
            "evidence_retrieval_pass",
        ),
        "causal_claim_safety_accuracy": _ratio(
            [item for item in scoreable if item.get("causal_claim_applicable")],
            "causal_claim_safety_pass",
        ),
        "report_structure_accuracy": _ratio(
            [item for item in scoreable if item.get("report_structure_applicable")],
            "report_structure_pass",
        ),
        "approval_violation_rate": _ratio(
            [item for item in scoreable if item["expected_status"] == "approval_pending"],
            "approval_violation",
        ),
        "repeat_stability": (round(stable_success / len(complete_case_runs), 4)
                             if repeats > 1 and complete_case_runs else None),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "token_usage": tokens,
        "configured_repeats": repeats,
    }


def _shift_fixture_to_today(source: Path) -> str:
    rows = list(csv.DictReader(source.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        raise ValueError("feedback fixture is empty")
    dates = [datetime.fromisoformat(row["created_at"]) for row in rows]
    delta = datetime.now().date() - max(dates).date()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    for row, created_at in zip(rows, dates):
        row = dict(row)
        row["created_at"] = (created_at + delta).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow(row)
    return output.getvalue()


class LiveEvaluationRunner:
    """Runs a real LLM through Skills, MCP, Policy and the Harness loop."""

    def __init__(self, llm: LLM, project_root: str | Path,
                 request_timeout: float = 30.0) -> None:
        self.llm = llm
        self.project_root = Path(project_root).resolve()
        self.request_timeout = request_timeout

    def run(self, cases: list[LiveEvalCase], repeats: int = 1,
            progress_hook: Callable[[int, int, str], None] | None = None,
            max_consecutive_infrastructure_errors: int = 3) -> dict[str, Any]:
        if repeats < 1 or repeats > 10:
            raise ValueError("repeats must be between 1 and 10")
        if max_consecutive_infrastructure_errors < 1:
            raise ValueError("max_consecutive_infrastructure_errors must be positive")
        results: list[dict[str, Any]] = []
        consecutive_infrastructure_errors = 0
        aborted_reason = ""
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="feedback-agent-live-eval-") as temp:
            root = Path(temp)
            feedback_dir = root / "feedback"
            work_item_dir = root / "work-items"
            sessions_dir = root / "sessions"
            fixture = self.project_root / "sample_data" / "customer_feedback_demo.csv"
            csv_text = _shift_fixture_to_today(fixture)
            imported = FeedbackService(feedback_dir).import_csv(fixture.name, csv_text)
            with MCPClientManager(request_timeout=self.request_timeout) as manager:
                manager.connect_stdio(
                    "feedback", sys.executable,
                    ["-u", "-m", "billguard.mcp_servers.bill_server",
                     "--data-dir", str(feedback_dir), "--transport", "stdio"],
                    cwd=self.project_root,
                )
                manager.connect_stdio(
                    "work-items", sys.executable,
                    ["-u", "-m", "billguard.mcp_servers.work_item_server",
                     "--data-dir", str(work_item_dir), "serve", "--transport", "stdio"],
                    cwd=self.project_root,
                )
                gateway = PolicyGateway(ApprovalStore(root / "policy"))
                total_runs = len(cases) * repeats
                completed_runs = 0
                for repeat in range(1, repeats + 1):
                    for case in cases:
                        session_id = f"live-eval-{case.id}-r{repeat}"
                        agent = create_mcp_feedback_agent(
                            self.llm, session_id, manager, sessions_dir,
                            skill_dir=self.project_root / "skills", policy_gateway=gateway,
                        )
                        events: list[RunEvent] = []
                        agent.hooks.append(events.append)
                        try:
                            response = agent.run(session_id, case.input)
                        except Exception as exc:
                            response = AgentResponse(
                                answer=f"evaluation runner error: {exc}", steps=0,
                                trace_id="", status="failed",
                            )
                        result = score_live_run(case, response, events)
                        result["repeat"] = repeat
                        results.append(result)
                        if result["infrastructure_error"]:
                            consecutive_infrastructure_errors += 1
                        else:
                            consecutive_infrastructure_errors = 0
                        completed_runs += 1
                        if progress_hook:
                            progress_hook(completed_runs, total_runs, case.id)
                        if consecutive_infrastructure_errors >= max_consecutive_infrastructure_errors:
                            aborted_reason = (
                                f"aborted after {consecutive_infrastructure_errors} consecutive "
                                "model transport/configuration failures"
                            )
                            break
                    if aborted_reason:
                        break
        return {
            "schema_version": 3,
            "evaluation_type": "live_llm",
            "benchmark": "feedback-agent-live-e2e-v3",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "model": str(getattr(self.llm, "model", "unknown")),
            "temperature": getattr(self.llm, "temperature", None),
            "dataset_size": len(cases),
            "repeats": repeats,
            "fixture": "sample_data/customer_feedback_demo.csv (dates shifted to evaluation day)",
            "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "fixture_rows": imported["imported_rows"],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "planned_runs": len(cases) * repeats,
            "aborted": bool(aborted_reason),
            "aborted_reason": aborted_reason,
            "metrics": summarize_live_results(results, len(cases), repeats),
            "results": results,
            "scope_note": (
                "Real end-to-end model runs through Skills, MCP, Policy and Harness. "
                "Scores are deterministic Trace assertions, including non-empty evidence retrieval "
                "and lexical causal-claim safety; broader semantic prose quality is not LLM-judged. "
                "Approval cases stop at the checkpoint and never commit a durable work item."
            ),
        }


def save_live_evaluation_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"live-eval-{stamp}"
    json_path = root / f"{stem}.json"
    markdown_path = root / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    percent = lambda value: "N/A" if value is None else f"{value:.1%}"
    lines = [
        "# Feedback Agent Live End-to-End Evaluation", "",
        f"- Model: `{report['model']}`",
        f"- Temperature: {report.get('temperature')}",
        f"- Cases: {report['dataset_size']} × {report['repeats']} repeats",
        f"- Evaluated at: {report['evaluated_at']}", "",
        "| Metric | Score |", "| --- | ---: |",
        f"| Scoreable runs | {metrics['scoreable_runs']} / {metrics['runs']} |",
        f"| Infrastructure failure | {percent(metrics['infrastructure_failure_rate'])} |",
        f"| Task success | {percent(metrics['task_success_rate'])} |",
        f"| Skill routing | {percent(metrics['skill_routing_accuracy'])} |",
        f"| Tool selection | {percent(metrics['tool_selection_accuracy'])} |",
        f"| Tool argument accuracy | {percent(metrics['argument_accuracy'])} |",
        f"| Evidence retrieval | {percent(metrics['evidence_retrieval_accuracy'])} |",
        f"| Numeric groundedness | {percent(metrics['numeric_groundedness'])} |",
        f"| Causal claim safety | {percent(metrics['causal_claim_safety_accuracy'])} |",
        f"| Report structure | {percent(metrics['report_structure_accuracy'])} |",
        f"| Approval violation | {percent(metrics['approval_violation_rate'])} |",
        f"| Repeat stability | {percent(metrics['repeat_stability'])} |", "",
        f"> {report['scope_note']}", "",
        "## Failed runs", "",
    ]
    failed = [item for item in report["results"] if not item["task_success"]]
    if not failed:
        lines.append("No failed runs.")
    else:
        for item in failed:
            failed_checks = (["infrastructure_error"] if item.get("infrastructure_error") else [
                name for name in (
                    "skill_routing_pass", "tool_selection_pass", "tool_sequence_pass",
                    "argument_pass", "status_pass",
                ) if not item[name]
            ])
            if item["approval_violation"]:
                failed_checks.append("approval_violation")
            if item["groundedness"] is not None and item["groundedness"] < 0.9:
                failed_checks.append("numeric_grounding")
            if item.get("evidence_applicable") and not item.get("evidence_retrieval_pass"):
                failed_checks.append("evidence_retrieval")
            if item.get("causal_claim_applicable") and not item.get("causal_claim_safety_pass"):
                failed_checks.append("causal_claim_safety")
            if item.get("report_structure_applicable") and not item.get("report_structure_pass"):
                failed_checks.append("report_structure")
            lines.append(f"- `{item['case_id']}` repeat {item['repeat']}: {', '.join(failed_checks)}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path.resolve()), "markdown": str(markdown_path.resolve())}
