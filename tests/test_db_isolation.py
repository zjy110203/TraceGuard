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
import shutil
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


def _run_probe(body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as a real child pytest whose ``tests/conftest.py`` applies.

    The probe file lives next to this test on purpose: a file outside ``tests/``
    would not load the conftest under test, and the probe would then prove
    nothing about the guard.
    """
    PROBE_MODULE.write_text(
        "from __future__ import annotations\n\n" + textwrap.dedent(body), encoding="utf-8"
    )
    try:
        return subprocess.run(
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
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
        )
    finally:
        PROBE_MODULE.unlink(missing_ok=True)


def _isolation_env(**overrides: str) -> dict[str, str]:
    """A clean environment with only the given DB variables set."""
    env = os.environ.copy()
    for key in ("DATABASE_URL", "TRACEGUARD_DATABASE_URL", "TRACEGUARD_TEST_DB"):
        env.pop(key, None)
    env.update(overrides)
    env["PYTHONUTF8"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    return env


_PROBE_ASSERTS_CHOSEN_DB = """
    import json
    import os


    def test_probe_reports_chosen_db() -> None:
        db = os.environ["DATABASE_URL"]
        tg = os.environ["TRACEGUARD_DATABASE_URL"]
        print(json.dumps({"db": db, "tg": tg, "sentinel": "__SENTINEL__"}))
        assert db == tg, (db, tg)
""".replace("__SENTINEL__", SENTINEL)


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


# --------------------------------------------------------------------------- #
# R5-4.11: a marker cannot replace the location check
# --------------------------------------------------------------------------- #


def _probe_payload(stdout: str) -> dict[str, str]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{") and SENTINEL in line:
            return json.loads(line)
    raise AssertionError(f"probe produced no payload:\n{stdout}")


def test_marker_flag_alone_cannot_authorise_a_business_location() -> None:
    """``TRACEGUARD_TEST_DB=1`` must not turn a writable file into a test DB."""
    conftest = _conftest_module()
    outside = REPO_ROOT / ".isolation-probe-not-scratch"
    outside.mkdir(exist_ok=True)
    url = f"sqlite:///{(outside / 'business.db').as_posix()}"
    try:
        safe, _ = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)
        located, location_reason = conftest._test_db_location(url)
        assert safe, "precondition: passes the generic sqlite/data directory check"
        assert not located, "precondition: not a disposable location"
        assert "outside" in location_reason

        proc = _run_probe(
            _PROBE_ASSERTS_CHOSEN_DB,
            _isolation_env(
                DATABASE_URL=url,
                TRACEGUARD_DATABASE_URL=url,
                TRACEGUARD_TEST_DB="1",
            ),
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = _probe_payload(proc.stdout)
        assert payload["db"] == payload["tg"]
        assert payload["db"] != url, "the marker flag must not authorise this path"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_mismatched_db_urls_are_reported_and_neither_reused(tmp_path: Path) -> None:
    url_a = f"sqlite:///{(tmp_path / 'a.db').as_posix()}"
    url_b = f"sqlite:///{(tmp_path / 'b.db').as_posix()}"

    proc = _run_probe(
        _PROBE_ASSERTS_CHOSEN_DB,
        _isolation_env(DATABASE_URL=url_a, TRACEGUARD_DATABASE_URL=url_b),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _probe_payload(proc.stdout)
    assert payload["db"] == payload["tg"]
    assert payload["db"] not in {url_a, url_b}
    assert "different databases" in (proc.stdout + proc.stderr)


def test_drive_relative_path_is_rejected() -> None:
    conftest = _conftest_module()

    ok, reason = conftest.is_safe_test_db_url("sqlite:///C:business.db", repo_root=REPO_ROOT)

    assert not ok
    assert "drive-relative" in reason


def test_path_traversal_cannot_reach_the_formal_data_dir() -> None:
    conftest = _conftest_module()
    traversals = (
        "sqlite:///data/../data/traceguard.db",
        "sqlite:///./data/traceguard.db",
    )

    for url in traversals:
        ok, reason = conftest.is_safe_test_db_url(url, repo_root=REPO_ROOT)
        assert not ok, url
        assert "data" in reason.lower(), (url, reason)


def test_symlink_into_data_is_rejected(tmp_path: Path) -> None:
    conftest = _conftest_module()
    data_root = REPO_ROOT / "data"
    link = tmp_path / "innocent.db"
    try:
        link.symlink_to(data_root / "traceguard.db")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink in this environment: {type(exc).__name__}: {exc}")

    ok, reason = conftest.is_safe_test_db_url(f"sqlite:///{link.as_posix()}", repo_root=REPO_ROOT)

    assert not ok
    assert "data" in reason.lower()


@pytest.mark.skipif(not FORMAL_DB.exists(), reason=f"formal DB missing at {FORMAL_DB}")
def test_hardlink_to_formal_db_is_rejected() -> None:
    """A hard link has an innocent path but the formal DB's bytes.

    The link must live on the same volume as ``data/`` (cross-drive links are
    impossible), so it is created in the project scratch directory and removed
    afterwards.
    """
    conftest = _conftest_module()
    link_dir = REPO_ROOT / ".traceguard-tmp" / "hardlink-probe"
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / "looks-like-a-test.db"
    try:
        os.link(FORMAL_DB, link)
    except OSError as exc:
        pytest.skip(f"cannot hard-link in this environment: {type(exc).__name__}: {exc}")
    try:
        ok, reason = conftest.is_safe_test_db_url(
            f"sqlite:///{link.as_posix()}", repo_root=REPO_ROOT
        )
    finally:
        link.unlink(missing_ok=True)

    assert not ok, "a hard link to the formal DB must never be reused"
    assert "same file" in reason


@pytest.mark.skipif(not FORMAL_DB.exists(), reason=f"formal DB missing at {FORMAL_DB}")
def test_formal_database_survives_a_marked_child_run() -> None:
    """Fingerprint the real DB around a child suite that *claims* it is a test DB."""
    if not FORMAL_DB.exists():  # pragma: no cover - skipif already covers this
        pytest.skip(f"formal database not present at {FORMAL_DB}")

    before = _fingerprint(FORMAL_DB)
    url = f"sqlite:///{FORMAL_DB.as_posix()}"

    proc = _run_probe(
        _PROBE_ASSERTS_CHOSEN_DB,
        _isolation_env(
            DATABASE_URL=url,
            TRACEGUARD_DATABASE_URL=url,
            TRACEGUARD_TEST_DB="1",
        ),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _probe_payload(proc.stdout)
    assert payload["db"] != url
    assert _fingerprint(FORMAL_DB) == before, "formal DB changed during the child run"
