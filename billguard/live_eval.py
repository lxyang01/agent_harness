from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .live_evaluation import (
    LiveEvaluationRunner,
    load_live_eval_cases,
    save_live_evaluation_report,
)
from .llm import OpenAICompatibleLLM


def parse_case_selector(selector: str) -> list[str]:
    """Expand a compact selector such as ``7-10,12,15`` into live case IDs."""
    case_ids: list[str] = []
    for item in selector.split(","):
        item = item.strip()
        if not item:
            raise ValueError("case selector contains an empty item")
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid case range: {item}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"case range must be ascending: {item}")
            numbers = range(start, end + 1)
        else:
            number_text = item.removeprefix("live-")
            if not number_text.isdigit():
                raise ValueError(f"invalid case number: {item}")
            numbers = (int(number_text),)
        case_ids.extend(f"live-{number:03d}" for number in numbers)
    return list(dict.fromkeys(case_ids))


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Run paid, real-model end-to-end Feedback Agent evaluations",
    )
    parser.add_argument("--dataset", default=str(project_root / "evals" / "live_agent_cases.jsonl"))
    parser.add_argument("--output-dir", default=str(project_root / ".sessions" / "evaluations"))
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--model", default="openai/gpt-4o-mini")
    parser.add_argument("--proxy", default=None,
                        help="Optional HTTP proxy, for example http://127.0.0.1:7897")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0,
                        help="Run only the first N cases; 0 means all cases")
    parser.add_argument("--case", dest="case_ids", action="append", default=[],
                        help="Run one case ID; repeat this option to select multiple cases")
    parser.add_argument("--cases", default="",
                        help='Run compact case numbers, for example "7-10,12-15"')
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-infrastructure-errors", type=int, default=3,
                        help="Abort after this many consecutive model transport/configuration failures")
    parser.add_argument("--confirm-live", action="store_true",
                        help="Required acknowledgement that this command makes paid API calls")
    args = parser.parse_args()

    cases = load_live_eval_cases(args.dataset)
    if args.case_ids and args.cases:
        parser.error("--case and --cases cannot be used together")
    if (args.case_ids or args.cases) and args.limit:
        parser.error("--case/--cases and --limit cannot be used together")
    if args.cases:
        try:
            args.case_ids = parse_case_selector(args.cases)
        except ValueError as exc:
            parser.error(str(exc))
    if args.case_ids:
        requested = set(args.case_ids)
        known = {case.id for case in cases}
        unknown = sorted(requested - known)
        if unknown:
            parser.error("unknown case IDs: " + ", ".join(unknown))
        cases = [case for case in cases if case.id in requested]
    if args.limit:
        if args.limit < 1:
            parser.error("--limit must be positive")
        cases = cases[:args.limit]
    planned_runs = len(cases) * args.repeats
    if planned_runs > 60:
        parser.error(f"planned runs ({planned_runs}) exceed the safety cap of 60")
    if not args.confirm_live:
        print(json.dumps({
            "status": "dry_run",
            "message": "No API call was made. Add --confirm-live to execute paid model requests.",
            "model": args.model,
            "temperature": args.temperature,
            "proxy": "configured" if args.proxy else "environment/default",
            "cases": len(cases),
            "repeats": args.repeats,
            "planned_runs": planned_runs,
        }, ensure_ascii=False, indent=2))
        return

    llm = OpenAICompatibleLLM(
        model=args.model, base_url=args.base_url, timeout=args.timeout,
        temperature=args.temperature, proxy=args.proxy,
    )
    try:
        report = LiveEvaluationRunner(llm, project_root, request_timeout=args.timeout).run(
            cases, args.repeats,
            progress_hook=lambda done, total, case_id: print(
                f"[{done}/{total}] {case_id}", file=sys.stderr, flush=True,
            ) if done else None,
            max_consecutive_infrastructure_errors=args.max_infrastructure_errors,
        )
        paths = save_live_evaluation_report(report, args.output_dir)
    finally:
        llm.close()
    print(json.dumps({
        "metrics": report["metrics"],
        "aborted": report["aborted"],
        "aborted_reason": report["aborted_reason"],
        "reports": paths,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
