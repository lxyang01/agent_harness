from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation import RoutingEvaluator, load_eval_cases, save_evaluation_report
from .skills import SkillRuntime


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Run deterministic Feedback Agent evaluations")
    parser.add_argument("--dataset", default=str(project_root / "evals" / "feedback_agent_cases.jsonl"))
    parser.add_argument("--skill-dir", default=str(project_root / "skills"))
    parser.add_argument("--output-dir", default=".sessions/evaluations")
    parser.add_argument("--variant", choices=("baseline", "skills", "full", "compare"), default="compare")
    args = parser.parse_args()

    cases = load_eval_cases(args.dataset)
    evaluator = RoutingEvaluator(SkillRuntime(args.skill_dir))
    report = evaluator.compare(cases) if args.variant == "compare" else evaluator.run(cases, args.variant)
    if args.variant == "compare":
        paths = save_evaluation_report(report, args.output_dir)
        print(json.dumps({"metrics": report["variants"], "reports": paths}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
