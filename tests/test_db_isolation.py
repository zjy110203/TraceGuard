"""Isolation tests for the pytest DB guard in ``tests/conftest.py``.

These are behavioural, not just helper unit tests: they spawn real child
``pytest``/``python`` processes with a deliberately polluted environment
(``DATABASE_URL`` pointing at the formal ``data/traceguard.db``) and assert the
suite cannot reach the formal database.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FORMAL_DB = REPO_ROOT / "data" / "traceguard.db"
PROBE_MODULE = Path(__file__).with_name("_tmp_pollution_probe.py")
SENTINEL = "TRACEGUARD_PROBE_OK"
TIMEOUT_S = 30


def _conftest_module() -> ModuleType:
    """Return the already-imported ``tests/conftest.py`` module."""
    module = sys.modules.get("conftest") or sys.modules.get("tests.conftest")
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        "traceguard_tests_conftest_fallback", Path(__file__).with_name("conftest.py")
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load tests/conftest.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _polluted_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{FORMAL_DB.as_posix()}"
    env["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{FORMAL_DB.as_posix()}"
    env["PYTHONUTF8"] = "1"
    env.pop("TRACEGUARD_TEST_DB", None)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, _sha256(path)


def test_inherited_formal_db_is_overridden_by_conftest() -> None:
    """A polluted env must not let the child suite reach the formal DB."""
    PROBE_MODULE.write_text(
        textwrap.dedent(
            f'''
            from __future__ import annotations

            import os
            from pathlib import Path

            FORMAL = Path({FORMAL_DB.as_posix()!r})


            def test_inherited_urls_were_overridden() -> None:
                db = os.environ["DATABASE_URL"]
                tg = os.environ["TRACEGUARD_DATABASE_URL"]
                print(f"probe DATABASE_URL={{db}}")
                print(f"probe TRACEGUARD_DATABASE_URL={{tg}}")
                assert db == tg, (db, tg)
                assert db.startswith("sqlite:///"), db
                db_path = Path(db[len("sqlite:///") :]).resolve()
                tg_path = Path(tg[len("sqlite:///") :]).resolve()
                assert db_path != FORMAL.resolve(), db_path
                assert tg_path != FORMAL.resolve(), tg_path
                print("{SENTINEL}")
            '''
        ),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(PROBE_MODULE),
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
            ],
            cwd=str(REPO_ROOT),
            env=_polluted_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
        )
    finally:
        PROBE_MODULE.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert SENTINEL in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.skipif(not FORMAL_DB.exists(), reason=f"formal DB missing at {FORMAL_DB}")
def test_formal_database_is_not_modified(tmp_path: Path) -> None:
    """Importing the app must not mutate ``data/traceguard.db``."""
    if not FORMAL_DB.exists():  # pragma: no cover - skipif already covers this
        pytest.skip(f"formal database not present at {FORMAL_DB}")

    before = _fingerprint(FORMAL_DB)
    probe_db = tmp_path / "import-probe.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{probe_db.as_posix()}"
    env["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{probe_db.as_posix()}"
    env["PYTHONUTF8"] = "1"
    env.pop("TRACEGUARD_TEST_DB", None)

    proc = subprocess.run(
        [sys.executable, "-c", "import apps.api.main"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = _fingerprint(FORMAL_DB)
    assert after == before, (
        f"formal DB changed: before={before} after={after} (size, mtime_ns, sha256)"
    )


def test_cleanup_success_removes_suite_dir(tmp_path: Path) -> None:
    conftest = _conftest_module()
    target = tmp_path / "suite-db-dir"
    target.mkdir()
    (target / "suite.db").write_text("payload", encoding="utf-8")

    conftest._cleanup_suite_db_dir(target)

    assert not target.exists()


def test_cleanup_failure_is_recorded_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest = _conftest_module()
    diag_dir = conftest._DIAG_DIR
    target = tmp_path / "stuck-dir"
    target.mkdir()
    (target / "suite.db").write_text("payload", encoding="utf-8")
    before = set(diag_dir.glob("*.json")) if diag_dir.exists() else set()

    def _deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated rmtree denial")

    monkeypatch.setattr(conftest.shutil, "rmtree", _deny)

    with pytest.warns(RuntimeWarning, match="cleanup failed"):
        conftest._cleanup_suite_db_dir(target)

    assert target.exists(), "failure branch must not delete the directory"
    new_records = (set(diag_dir.glob("*.json")) - before) if diag_dir.exists() else set()
    assert new_records, "a cleanup failure must leave a JSON record"
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in new_records]
    assert any(p.get("directory") == str(target) for p in payloads)
    assert any(p.get("exception_type") == "PermissionError" for p in payloads)


def test_session_diagnostics_are_recorded() -> None:
    conftest = _conftest_module()
    record = conftest._DIAG_DIR / f"session-{os.getpid()}.json"
    assert record.exists(), f"expected session diagnostics at {record}"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert "overridden" in payload
    assert payload["DATABASE_URL"] == payload["TRACEGUARD_DATABASE_URL"]
    assert payload["DATABASE_URL"] == conftest._ACTIVE_DB_URL


def test_diagnostics_redact_credentials() -> None:
    conftest = _conftest_module()

    assert (
        conftest._redact_db_url("postgresql://user:secret@localhost/db")
        == "postgresql://<redacted>"
    )
    assert conftest._redact_db_url("sqlite:///tmp/suite.db") == "sqlite:///tmp/suite.db"


def test_safety_rejects_formal_database() -> None:
    conftest = _conftest_module()
    url = f"sqlite:///{FORMAL_DB.as_posix()}"

    ok, reason = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)

    assert not ok
    assert "formal" in reason.lower()


def test_safety_rejects_data_subdirectory() -> None:
    conftest = _conftest_module()
    url = f"sqlite:///{(REPO_ROOT / 'data' / 'runs' / 'isolated.db').as_posix()}"

    ok, reason = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)

    assert not ok
    assert "data" in reason.lower()


def test_safety_allows_system_temp_dir(tmp_path: Path) -> None:
    conftest = _conftest_module()
    url = f"sqlite:///{(tmp_path / 'suite.db').as_posix()}"

    ok, reason = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)

    assert ok, reason


def test_safety_allows_traceguard_tmp_dir(tmp_path: Path) -> None:
    conftest = _conftest_module()
    repo = tmp_path / "repo"
    db = repo / ".traceguard-tmp" / "r5" / "verify" / "20260101-000000" / "isolated.db"
    url = f"sqlite:///{db.as_posix()}"

    ok, reason = conftest.is_safe_test_db_url(url, repo_root=repo)

    assert ok, reason


def test_safety_rejects_non_sqlite_backend() -> None:
    conftest = _conftest_module()
    url = "postgresql://user:secret@localhost/traceguard"

    ok, reason = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)

    assert not ok
    assert "sqlite" in reason.lower()
    assert "user:secret" not in reason, "rejection reason must not echo credentials"


def test_safety_rejects_empty_and_pathless_urls() -> None:
    conftest = _conftest_module()

    empty_ok, empty_reason = conftest.is_safe_test_db_url("", repo_root=REPO_ROOT)
    pathless_ok, pathless_reason = conftest.is_safe_test_db_url("sqlite://", repo_root=REPO_ROOT)

    assert not empty_ok
    assert empty_reason
    assert not pathless_ok
    assert pathless_reason
