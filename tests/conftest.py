"""Shared test fixtures — each test gets an isolated temp SQLite DB.

Using a fresh engine per test guarantees repeatability (stage 1 acceptance:
可重复初始化和清理) and prevents fault state leaking between tests.

Isolation note (R5-4.9): ``apps.api.main`` builds a module-level app on import,
and that app creates tables on whatever ``DATABASE_URL`` points at. A plain
``pytest`` run therefore used to write to the FORMAL ``data/traceguard.db``
whenever the developer shell / IDE run config / leftover CI var already had a
``DATABASE_URL`` — the old ``os.environ.setdefault`` kept that value verbatim.
The suite now *validates and overrides*: an inherited URL is reused only when it
is provably a throwaway test DB (under the system temp dir or under this repo's
``.traceguard-tmp/``). Anything else — the formal DB, a ``data/`` subdirectory,
a business file somewhere else on disk, a drive-relative path or a non-sqlite
backend — is replaced with this session's temp DB, and both env vars are forced
to agree.

R5-4.11: ``TRACEGUARD_TEST_DB=1`` no longer widens the location check. The flag
used to make *any* writable sqlite file acceptable, which meant one inherited
variable could point the suite at a real business database. A marker can only
confirm a location that is already provably disposable; it can never replace the
path check. The flag is still read and reported in diagnostics so an operator
can see it was set, and the override explains that it was not enough.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import warnings
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

# Module-level ASGI objects are imported during collection. Give them an
# explicit isolated test configuration so production/development startup still
# requires the official launch capability.
os.environ["APP_ENV"] = "test"

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine

    from packages.core.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUITE_DB_DIR = Path(tempfile.mkdtemp(prefix="traceguard-pytest-"))
_SUITE_DB_URL = f"sqlite:///{(_SUITE_DB_DIR / 'suite.db').as_posix()}"

# Diagnostics and cleanup-failure records live together (gitignored, outside
# ``data/``). The directory may not exist yet; writers create it on demand.
_DIAG_DIR = _REPO_ROOT / ".traceguard-tmp" / "pytest-cleanup-failures"

_SQLITE_PREFIX = "sqlite:///"

# ``C:foo.db`` is drive-*relative* on Windows: it means "the current directory
# on drive C", not "C:\foo.db". Resolving it against the repo silently invents a
# location, so it is rejected outright instead of being guessed at.
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![\\/])")


def _sqlite_file_path(url: str) -> Path | None:
    """Return the file a ``sqlite:///`` URL points at, or None.

    None means "no file": non-sqlite URLs and ``:memory:``. Relative paths
    resolve against the repo root, the only cwd the suite supports.
    """
    if not url.startswith(_SQLITE_PREFIX):
        return None
    raw = url[len(_SQLITE_PREFIX) :]
    if not raw or raw == ":memory:":
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path


def _is_under(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    root_resolved = root.resolve()
    return resolved == root_resolved or root_resolved in resolved.parents


def is_safe_test_db_url(url: str, *, repo_root: Path) -> tuple[bool, str]:
    """Decide whether ``url`` is a safe database for the test suite.

    Safe means: sqlite-backed, resolvable to a file that is NOT the formal
    database and NOT anywhere under ``<repo_root>/data/``, and whose parent
    directory exists or can be created and is writable (verified by an actual
    create-then-remove probe). Returns ``(ok, reason)``; ``reason`` always
    explains the verdict, so a rejected inherited URL can be logged verbatim.
    """
    if not url or not url.strip():
        return False, "empty database URL"

    if url in ("sqlite://", "sqlite:///"):
        return False, f"sqlite URL without a file path: {url!r}"

    if url == "sqlite:///:memory:":
        return True, "in-memory sqlite database"

    if not url.startswith(_SQLITE_PREFIX):
        scheme = url.split(":", 1)[0] or url
        return False, f"unsupported database backend {scheme!r}; only sqlite:/// is allowed"

    raw = url[len(_SQLITE_PREFIX) :]
    if _DRIVE_RELATIVE_RE.match(raw):
        return False, f"drive-relative sqlite path has no real location: {raw!r}"
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = repo_root / db_path
    resolved = db_path.resolve()

    data_root = (repo_root / "data").resolve()
    formal_db = (data_root / "traceguard.db").resolve()
    if resolved == formal_db:
        return False, f"points at the formal database {formal_db}"
    if resolved == data_root or data_root in resolved.parents:
        return False, f"lives under the formal data directory {data_root}"
    if resolved.exists() and formal_db.exists():
        # A hard link looks like an unrelated path but shares the formal DB's
        # bytes: writing through it corrupts real data, so compare identity
        # rather than trusting the location.
        try:
            if os.path.samefile(resolved, formal_db):
                return False, f"is the same file as the formal database {formal_db}"
        except OSError:  # pragma: no cover - racing deletion
            pass

    parent = resolved.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".tg-write-probe-", dir=parent)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return False, (f"parent directory {parent} is not usable ({type(exc).__name__}: {exc})")
    return True, f"safe sqlite test database at {resolved}"


def _test_db_location(url: str) -> tuple[bool, str]:
    """Can a throwaway test DB live here at all?

    Only two locations qualify: the system temp directory and this repo's
    ``.traceguard-tmp/``. That is a *location* fact, independent of any flag —
    ``TRACEGUARD_TEST_DB=1`` describes intent, not where the bytes are, so it
    must never widen this check. ``resolve()`` is used so a symlink pointing at
    ``data/`` cannot smuggle a formal database through an innocent-looking path.
    """
    if url == "sqlite:///:memory:":
        return True, "in-memory sqlite database"
    path = _sqlite_file_path(url)
    if path is None:
        return False, "not a file-backed sqlite URL"
    temp_root = Path(tempfile.gettempdir())
    if _is_under(path, temp_root):
        return True, f"under the system temp directory {temp_root}"
    scratch = _REPO_ROOT / ".traceguard-tmp"
    if _is_under(path, scratch):
        return True, f"under the project scratch directory {scratch}"
    return False, (
        f"{path.resolve()} is outside both the system temp directory {temp_root} "
        f"and {scratch}; only those are accepted as test database locations"
    )


def _redact_db_url(url: str | None) -> str | None:
    """Keep sqlite paths; reduce any other backend to ``scheme://<redacted>``.

    Non-sqlite URLs can embed a username/password, and diagnostics are written
    to disk — never copy a credential into a log just to explain a rejection.
    """
    if not url:
        return url
    if url.startswith(_SQLITE_PREFIX) or url == "sqlite://":
        return url
    scheme = url.split(":", 1)[0]
    return f"{scheme or 'unknown'}://<redacted>"


def _decide_db_url() -> tuple[str, dict[str, Any]]:
    """Pick the session DB URL, validate/override the inherited one, record why."""
    inherited_db = (os.environ.get("DATABASE_URL") or "").strip() or None
    inherited_tg = (os.environ.get("TRACEGUARD_DATABASE_URL") or "").strip() or None
    marker = os.environ.get("TRACEGUARD_TEST_DB")
    candidate = inherited_db or inherited_tg

    mismatch = (
        inherited_db is not None and inherited_tg is not None and inherited_db != inherited_tg
    )
    diag: dict[str, Any] = {
        "event": "pytest_session_db",
        "pid": os.getpid(),
        "timestamp": datetime.now().isoformat(),
        "inherited": {
            "DATABASE_URL": _redact_db_url(inherited_db),
            "TRACEGUARD_DATABASE_URL": _redact_db_url(inherited_tg),
        },
        # Reported, never trusted: the flag expresses intent, not location.
        "test_db_marker": marker,
        "mismatch": mismatch,
        "suite_db_url": _SUITE_DB_URL,
    }

    keep = False
    reason = "no inherited DATABASE_URL/TRACEGUARD_DATABASE_URL"
    if mismatch:
        # Two different databases mean the environment is polluted or one of the
        # two is stale. Picking either would be a guess about real data, so both
        # are dropped and the mismatch is recorded verbatim.
        reason = (
            "DATABASE_URL and TRACEGUARD_DATABASE_URL point at different databases; "
            "neither is reused"
        )
        warnings.warn(
            f"pytest DB isolation: {reason} (DATABASE_URL="
            f"{_redact_db_url(inherited_db)}, TRACEGUARD_DATABASE_URL="
            f"{_redact_db_url(inherited_tg)}); using {_SUITE_DB_URL}",
            RuntimeWarning,
            stacklevel=1,
        )
    elif candidate:
        safe, safety_reason = is_safe_test_db_url(candidate, repo_root=_REPO_ROOT)
        located, location_reason = _test_db_location(candidate)
        if safe and located:
            keep = True
            reason = f"kept inherited test DB: {location_reason}"
        elif safe and marker == "1":
            reason = (
                f"TRACEGUARD_TEST_DB=1 was set but cannot stand in for the location "
                f"check: {location_reason}"
            )
        elif safe:
            reason = f"safe but not in a disposable location: {location_reason}"
        else:
            reason = safety_reason

    chosen = _SUITE_DB_URL
    if keep and candidate is not None:
        chosen = candidate
    diag["overridden"] = not keep
    diag["chosen_url"] = chosen
    diag["reason"] = reason
    diag["location_reason"] = _test_db_location(candidate)[1] if candidate else "no inherited URL"

    # Settings reads DATABASE_URL; alembic env.py reads TRACEGUARD_DATABASE_URL.
    # Force both so no code path can fall back to a different database.
    os.environ["DATABASE_URL"] = chosen
    os.environ["TRACEGUARD_DATABASE_URL"] = chosen
    diag["DATABASE_URL"] = chosen
    diag["TRACEGUARD_DATABASE_URL"] = chosen
    return chosen, diag


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _record_cleanup_failure(target: Path, exc: BaseException) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    record = _DIAG_DIR / f"{stamp}-{os.getpid()}.json"
    _write_json(
        record,
        {
            "event": "pytest_suite_db_cleanup_failure",
            "pid": os.getpid(),
            "timestamp": datetime.now().isoformat(),
            "directory": str(target),
            "exception_type": type(exc).__name__,
            "message": str(exc),
        },
    )
    return record


def _dispose_imported_engines() -> list[str]:
    """Dispose engines that module-level apps still hold open on the suite DB.

    Importing ``apps.api.main`` builds an app — and an engine — at import time.
    That app's lifespan disposes it, but a test session never runs the lifespan,
    so on Windows the sqlite file stays locked and the temp directory cannot be
    removed (``WinError 32``). Disposing here is real cleanup, not a way to hide
    a failure: if removal still fails afterwards, it is recorded as before.
    """
    disposed: list[str] = []
    for module_name in ("apps.api.main", "apps.teamflow.main"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        engine = getattr(getattr(getattr(module, "app", None), "state", None), "engine", None)
        if engine is None:
            continue
        try:
            engine.dispose()
            disposed.append(module_name)
        except Exception:  # noqa: BLE001 - the remaining names still get a chance
            continue
    return disposed


def _cleanup_suite_db_dir(target: Path) -> None:
    """Remove the session DB dir; never raise, but never fail silently either."""
    # Windows releases a sqlite handle asynchronously after dispose(), so a
    # single attempt can fail spuriously. Three bounded attempts keep the
    # common case clean without turning a real leak into a silent pass.
    _dispose_imported_engines()
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - cleanup must not abort the session
            last_error = exc
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
    assert last_error is not None
    record: Path | None = None
    try:
        record = _record_cleanup_failure(target, last_error)
    except OSError as write_exc:
        warnings.warn(
            f"could not record pytest cleanup failure for {target}: "
            f"{type(write_exc).__name__}: {write_exc}",
            RuntimeWarning,
            stacklevel=2,
        )
    detail = f"; record: {record}" if record is not None else ""
    warnings.warn(
        f"pytest suite DB cleanup failed for {target} "
        f"({type(last_error).__name__}: {last_error}){detail}",
        RuntimeWarning,
        stacklevel=2,
    )


_ACTIVE_DB_URL, _SESSION_DIAG = _decide_db_url()
try:
    _write_json(_DIAG_DIR / f"session-{os.getpid()}.json", _SESSION_DIAG)
except OSError as _diag_exc:  # diagnostics must never break collection
    warnings.warn(
        f"could not write pytest session DB diagnostics: {type(_diag_exc).__name__}: {_diag_exc}",
        RuntimeWarning,
        stacklevel=1,
    )

EVAL_TOKEN = "test-eval-token"


@pytest.fixture(scope="session", autouse=True)
def _drop_suite_db() -> Iterator[None]:
    """Remove the throwaway suite DB directory once the session ends."""
    yield
    _cleanup_suite_db_dir(_SUITE_DB_DIR)


@pytest.fixture()
def settings() -> Settings:
    from packages.core.config import AppEnv, Settings

    return Settings(
        app_env=AppEnv.TEST,
        database_url="sqlite:///:memory:",
        eval_control_token=EVAL_TOKEN,
        teamflow_test_user="tester",
        teamflow_test_password="test-pass-123",
    )


@pytest.fixture()
def app(settings: Settings) -> FastAPI:
    from apps.teamflow.main import build_app

    return build_app(settings)


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def eval_headers() -> dict[str, str]:
    return {"X-Eval-Token": EVAL_TOKEN}


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Engine]:
    from sqlalchemy import create_engine

    from apps.teamflow.eval_models import Base as EvalBase
    from apps.teamflow.models import Base as BusinessBase

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    BusinessBase.metadata.create_all(eng)
    EvalBase.metadata.create_all(eng)
    yield eng
    eng.dispose()
