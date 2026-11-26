"""Read persisted scenario runs and recompute path verdicts, without changing Oracles."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from packages.agents.exploration import verify_trace_record
from packages.agents.goal_assertions import evaluate_assertions, evaluate_test_case_verdict
from packages.agents.scenarios import default_registry


def audit(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(root.rglob("exp_*.json")):
        record = json.loads(path.read_bytes())
        spec = record.get("path_spec")
        if not isinstance(spec, dict):
            continue
        observations = list(record.get("observations") or [])
        report = evaluate_assertions(
            spec["expect"], observations[-1] if observations else {}, observations=observations
        )
        verdict = evaluate_test_case_verdict(
            goal_status=record["goal_status"],
            expected_goal_status=spec["expected_goal_status"],
            required_expect_verdicts=[r.verdict for r in report.results if r.required],
        ).value
        scenario = default_registry().get(record["scenario_id"], record["scenario_version"])
        integrity = (
            verify_trace_record(record)
            and scenario is not None
            and scenario.digest == record["scenario_digest"]
            and scenario.path_spec_for_path(record["scenario_path"]) == spec
            and verdict == record["test_case_verdict"]
        )
        rows.append(
            {
                "source": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "exploration_id": record["exploration_id"],
                "scenario_id": record["scenario_id"],
                "scenario_version": record["scenario_version"],
                "path": record["scenario_path"],
                "goal_status": record["goal_status"],
                "expected_goal_status": spec["expected_goal_status"],
                "test_case_verdict": verdict,
                "path_assertions": [r.to_dict() for r in report.results],
                "integrity_verified": integrity,
            }
        )
    return {
        "status": "PASS"
        if rows
        and all(r["integrity_verified"] and r["test_case_verdict"] == "PASSED" for r in rows)
        else "FAIL"
        if rows
        else "NOT_RUN",
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result["status"], len(result["rows"]), "persisted paths")
    raise SystemExit(0 if result["status"] == "PASS" else 1)
