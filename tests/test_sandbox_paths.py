"""R4-0 path-safety negative tests for RepairSandbox.close().

Covers the R4 audit requirements that R3 tests did not:
  * Windows junction replacing the sandbox directory (victim stays inside the
    project temp root — never personal/system directories);
  * junction pointing outside SANDBOX_ROOT (containment refusal);
  * directory replaced after open() with the marker removed (keep, don't guess);
  * Windows case-insensitive path handling resolves to the same directory;
  * evidence delivery failure is recorded on the result, not silent;
  * cleanup failure is surfaced via result.cleanup_error.

Safety: every link target lives inside ``.traceguard-tmp`` of the project.
No personal or system directory is ever referenced.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from packages.execution import sandbox as sb
from packages.execution.models import TestCaseVersion, load_case
from packages.execution.sandbox import MARKER_NAME, SANDBOX_ROOT, RepairSandbox


def _case_pair() -> tuple[TestCaseVersion, TestCaseVersion]:
    case_file = Path(__file__).resolve().parents[1] / "benchmark" / "cases" / "TC-CREATE-002.json"
    base = load_case(case_file)
    candidate = TestCaseVersion(**{**base.__dict__, "version": 2})
    return base, candidate


def _sandbox(tmp_name: str, **kwargs: object) -> RepairSandbox:
    base, candidate = _case_pair()
    defaults: dict[str, object] = {
        "proposal_id": f"rp_r4_path_{tmp_name}",
        "base_case": base,
        "candidate": candidate,
        "on_evidence": lambda payload: None,
    }
    defaults.update(kwargs)
    return RepairSandbox(**defaults)  # type: ignore[arg-type]


def test_directory_replaced_by_markerless_copy_is_kept() -> None:
    """If the sandbox dir is swapped for one without our marker, close() keeps it."""
    sandbox = _sandbox("nomarker")
    sandbox.open()
    marker = sandbox._dir / MARKER_NAME
    assert marker.exists()
    marker.unlink()  # simulate directory replacement / tampering
    sandbox.close()
    assert sandbox._dir.exists(), "directory without a valid marker must never be deleted"
    shutil.rmtree(sandbox._dir, ignore_errors=True)


def test_case_insensitive_resolution_still_scoped() -> None:
    """Windows is case-insensitive: a case-variant _dir must resolve to the
    same directory so the marker check (not the raw string) governs deletion."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific path semantics")
    sandbox = _sandbox("casefold")
    opened: Path = sandbox.open()
    # Path equality on Windows ignores case, so compare raw strings.
    variant_str = str(opened).swapcase()
    if variant_str == str(opened):
        pytest.skip("path contains no case-significant characters")
    sandbox._dir = Path(variant_str)
    sandbox.close()
    assert not opened.exists(), "case-variant resolution must still delete our own dir"


def test_junction_to_victim_inside_root_is_not_deleted() -> None:
    """Sandbox dir replaced by a junction to a victim inside SANDBOX_ROOT:
    containment passes, but the victim has no marker -> nothing is deleted."""
    if sys.platform != "win32":
        pytest.skip("Windows junctions")
    import _winapi

    victim = SANDBOX_ROOT / "r4_junction_victim_inside"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "keep_me.txt").write_text("do not delete", encoding="utf-8")

    sandbox = _sandbox("junc_inside")
    sandbox.open()
    shutil.rmtree(sandbox._dir)  # replace real dir with a junction
    try:
        _winapi.CreateJunction(str(victim), str(sandbox._dir))
    except OSError:
        shutil.rmtree(victim, ignore_errors=True)
        pytest.skip("junction creation unavailable on this host")

    try:
        sandbox.close()
        assert victim.exists(), "junction target without our marker must survive"
        assert (victim / "keep_me.txt").read_text(encoding="utf-8") == "do not delete"
    finally:
        if sandbox._dir.is_symlink() or sandbox._dir.exists():
            try:
                os.remove(str(sandbox._dir))  # remove the junction itself only
            except OSError:
                pass
        shutil.rmtree(victim, ignore_errors=True)


def test_junction_pointing_outside_root_is_refused() -> None:
    """Junction target outside SANDBOX_ROOT (still inside the project temp
    root for safety): containment check must refuse deletion entirely."""
    if sys.platform != "win32":
        pytest.skip("Windows junctions")
    import _winapi

    outside_root = SANDBOX_ROOT.parent / "r4_junction_victim_outside"
    outside_root.mkdir(parents=True, exist_ok=True)
    (outside_root / "keep_me.txt").write_text("do not delete", encoding="utf-8")

    sandbox = _sandbox("junc_outside")
    sandbox.open()
    shutil.rmtree(sandbox._dir)
    try:
        _winapi.CreateJunction(str(outside_root), str(sandbox._dir))
    except OSError:
        shutil.rmtree(outside_root, ignore_errors=True)
        pytest.skip("junction creation unavailable on this host")

    try:
        sandbox.close()
        assert outside_root.exists(), "target outside SANDBOX_ROOT must never be touched"
        assert (outside_root / "keep_me.txt").read_text(encoding="utf-8") == "do not delete"
    finally:
        try:
            os.remove(str(sandbox._dir))
        except OSError:
            pass
        shutil.rmtree(outside_root, ignore_errors=True)


def test_evidence_delivery_failure_is_recorded() -> None:
    """A raising on_evidence callback must yield an explicit delivery error on
    the result instead of a silent loss or an uncaught exception."""

    def bad_callback(payload: dict[str, object]) -> None:
        raise RuntimeError("platform evidence sink unavailable")

    sandbox = _sandbox("deliver", on_evidence=bad_callback)
    result = sandbox.run()
    assert result.verdict in {
        "VALIDATED",
        "NOT_VALIDATED",
        "NO_BEHAVIORAL_CHANGE",
        "SANDBOX_FAILED",
    }
    assert "RuntimeError" in result.evidence_delivery_error
    assert "platform evidence sink unavailable" in result.evidence_delivery_error
    assert result.evidence, "evidence payload must remain available to the caller"


def test_cleanup_failure_is_surfaced_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """When rmtree fails, result.cleanup_error must say so — cleanup failure
    must not masquerade as success."""
    sandbox = _sandbox("cleanup")
    sandbox.open()
    sandbox._process = None
    sandbox._stderr_handle = None

    real_rmtree = shutil.rmtree

    def failing_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)) == sandbox._dir.resolve():
            raise PermissionError(13, "simulated locked directory")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sb.shutil, "rmtree", failing_rmtree)
    sandbox.close()
    assert sandbox._cleanup_error, "close() must record the failure"
    assert "simulated locked directory" in sandbox._cleanup_error
    assert sandbox._dir.exists()
    monkeypatch.undo()
    try:
        shutil.rmtree(sandbox._dir, ignore_errors=True)
    except SystemExit:
        # The real host delete-guard shim may also abort our test cleanup once
        # the per-turn threshold is crossed; leaving the dir in .traceguard-tmp
        # is acceptable — it is project-scoped temp space.
        pass


def test_systemexit_from_delete_guard_is_recorded_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some host environments inject rmtree delete-guard shims that abort with
    ``SystemExit`` (a BaseException) once a per-turn delete threshold is hit.
    A blocked teardown must be recorded as cleanup_error — never leak out of
    close() and never masquerade as a successful cleanup."""
    sandbox = _sandbox("guard")
    sandbox.open()
    sandbox._process = None
    sandbox._stderr_handle = None

    real_rmtree = shutil.rmtree

    def guard_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)) == sandbox._dir.resolve():
            raise SystemExit(1)  # simulate SAFE_DELETE_BULK_CONFIRM_REQUIRED
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sb.shutil, "rmtree", guard_rmtree)
    sandbox.close()  # must not raise SystemExit
    assert sandbox._cleanup_error, "close() must record the blocked cleanup"
    assert "cleanup blocked" in sandbox._cleanup_error
    assert "SystemExit" in sandbox._cleanup_error
    assert sandbox._dir.exists(), "blocked cleanup must not claim the dir was deleted"
    monkeypatch.undo()
    try:
        shutil.rmtree(sandbox._dir, ignore_errors=True)
    except SystemExit:
        pass  # host delete-guard threshold crossed; dir stays in project temp


def test_marker_content_is_ownership_scoped() -> None:
    """The ownership marker must scope deletion to THIS sandbox id only."""
    sandbox = _sandbox("marker")
    opened: Path = sandbox.open()
    marker = json.loads((opened / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["sandbox_id"] == sandbox.sandbox_id
    assert marker["proposal_id"] == sandbox.proposal_id
    assert marker["owner"] == "RepairSandbox"
    assert marker["project"] == "traceguard"
    sandbox.close()
