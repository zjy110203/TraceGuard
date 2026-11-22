"""Run a stage-2 TestCase file against TeamFlow without any Agent.

Usage (from repo root, with TeamFlow already running):
    # PowerShell:
    .\\.venv\\Scripts\\python.exe -m scripts.run_case benchmark/cases/TC_E2E_001.json
    # Git Bash / Linux:
    .venv/Scripts/python.exe -m scripts.run_case benchmark/cases/TC_E2E_001.json

Case files are TestCaseVersion.to_dict() JSON. The runner appends a Run/Step
report to --report-dir. This is the "TestCase can execute independently of
the Agent / async platform" acceptance criterion for stage 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from packages.execution.models import _version_from_dict
from packages.execution.runner import LocalRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a TestCaseVersion locally")
    parser.add_argument("case_file", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--report-dir", type=Path, default=Path("data/runs"))
    parser.add_argument("--reset", action="store_true", help="reset + clear faults first")
    parser.add_argument("--eval-token", default="local-dev-eval-token-change-me")
    args = parser.parse_args()

    case = _version_from_dict(json.loads(args.case_file.read_text(encoding="utf-8")))
    case.validate()

    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        if client.get("/health").status_code != 200:
            print("ERROR: TeamFlow not healthy at", args.base_url, file=sys.stderr)
            return 2
        if args.reset:
            client.post("/__eval__/faults/clear", headers={"X-Eval-Token": args.eval_token})
            client.post("/__eval__/data/reset", headers={"X-Eval-Token": args.eval_token})

    runner = LocalRunner(args.base_url, report_dir=args.report_dir, eval_token=args.eval_token)
    report = runner.run(case)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.test_verdict == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
