from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adversarial_evaluation import AdversarialEvaluator, save_adversarial_report


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Run deterministic offline adversarial evaluations",
    )
    parser.add_argument(
        "--output-dir",
        default=str(project_root / ".sessions" / "evaluations"),
    )
    parser.add_argument(
        "--report",
        default=str(project_root / "docs" / "adversarial_evaluation_report.md"),
        help="Canonical Markdown failure report path; use an empty value to disable",
    )
    args = parser.parse_args()

    report = AdversarialEvaluator().run()
    paths = save_adversarial_report(
        report, args.output_dir, args.report or None,
    )
    print(json.dumps({
        "metrics": report["metrics"],
        "categories": report["categories"],
        "reports": paths,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
