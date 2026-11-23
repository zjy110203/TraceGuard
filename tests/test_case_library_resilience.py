"""One invalid case file must not take the platform down (R5-7 regression).

Found by system acceptance, not by unit tests: a stale compiled case carried
the redaction placeholder as a ``fill`` value, so ``load_cases_dir`` raised
during ``/overview`` — and because the same loader backs ``/cases`` and
``/intents``, three pages returned 500 while every other case was valid.

The fix has two halves, both asserted here:
  * the loader skips the broken file and keeps the valid ones,
  * the overview *names* the broken file instead of staying silent about it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings
from packages.execution.models import load_cases_dir, load_cases_dir_report

ROOT = Path(__file__).resolve().parents[1]
GOOD_CASE = ROOT / "benchmark" / "cases" / "TC-LOGIN-001.json"


def _broken_case(path: Path) -> None:
    """A case that looks like a case but carries a redaction placeholder value."""
    data = json.loads(GOOD_CASE.read_text(encoding="utf-8"))
    for step in data["steps"]:
        if step["action"] == "fill":
            step["value_ref"] = ""
            step["value"] = "«redacted»"
            break
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_loader_skips_broken_case_but_keeps_the_rest(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    shutil.copy2(GOOD_CASE, cases / "TC-LOGIN-001.json")
    _broken_case(cases / "TC-BROKEN-001.json")

    loaded = load_cases_dir(cases)
    assert [c.case_id for c in loaded] == ["TC-LOGIN-001"]

    loaded_2, invalid = load_cases_dir_report(cases)
    assert len(loaded_2) == 1
    assert [item["file"] for item in invalid] == ["TC-BROKEN-001.json"]
    assert "redaction placeholder" in invalid[0]["reason"]


def test_overview_survives_and_reports_the_invalid_case(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    shutil.copy2(GOOD_CASE, cases / "TC-LOGIN-001.json")
    _broken_case(cases / "TC-BROKEN-001.json")

    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        case_dir=str(cases),
        exploration_data_dir=str(tmp_path / "explorations"),
        candidate_data_dir=str(tmp_path / "candidates"),
        benchmark_data_dir=str(tmp_path / "bench"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    with TestClient(build_app(settings)) as client:
        overview = client.get("/api/v1/overview")
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["cases"] == 1
        assert [item["file"] for item in body["invalid_cases"]] == ["TC-BROKEN-001.json"]

        # Same loader behind these two pages: they used to 500 as well.
        assert client.get("/api/v1/cases").status_code == 200
        assert client.get("/api/v1/intents").status_code == 200
