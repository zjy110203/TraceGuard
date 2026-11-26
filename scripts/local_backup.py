"""Offline local deployment backup/restore into a NEW directory.

Stop API, SUT and all writers before invoking with --offline. Hash comparisons
detect changes but cannot replace that operational precondition. No live backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any

from packages.execution.candidate_transaction import atomic_write, json_bytes

ROLES = {"database", "cases", "candidates", "explorations", "chains", "blobs", "benchmark"}


def inventory(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"links/reparse points are unsupported: {path.name}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def validate(root: Path) -> dict[str, Any]:
    """Check copied bytes, parseable state and SQLite without modifying source."""
    manifest = json.loads((root / "manifest.json").read_bytes())
    if manifest.get("format") != "traceguard-offline-v1" or set(manifest["roles"]) != ROLES:
        raise ValueError("invalid/incomplete deployment manifest")
    actual = inventory(root / "payload")
    if actual != manifest["files"]:
        raise ValueError("backup file inventory/digest mismatch")
    for role, row in manifest["roles"].items():
        if row["kind"] not in {"file", "directory"}:
            raise ValueError("invalid role kind")
        base = root / "payload" / role
        if not base.is_dir():
            raise ValueError(f"missing role: {role}")
        for path in base.rglob("*.json"):
            json.loads(path.read_bytes())
        for path in base.rglob("*.jsonl"):
            for line in path.read_bytes().splitlines():
                if line.strip():
                    json.loads(line)
    dbs = list((root / "payload" / "database").glob("*"))
    if len(dbs) != 1 or not dbs[0].is_file():
        raise ValueError("exactly one SQLite database required")
    with sqlite3.connect(dbs[0].resolve().as_uri() + "?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
    return {"status": "VERIFIED", "files": len(actual), "sqlite": "ok"}


def create(plan: dict[str, str], destination: Path, *, offline: bool) -> dict[str, Any]:
    if not offline or set(plan) != ROLES:
        raise ValueError("offline mode and all seven deployment roles are required")
    destination = destination.resolve()
    paths = {role: Path(value).resolve() for role, value in plan.items()}
    if any(destination == path or path in destination.parents for path in paths.values()):
        raise ValueError("backup destination must be outside every source")
    if destination.exists():
        raise ValueError("backup destination must be new; existing data is protected")
    if not paths["database"].is_file() or any(
        Path(str(paths["database"]) + suffix).exists() for suffix in ("-wal", "-journal")
    ):
        raise ValueError(
            "database must exist and have no WAL/journal; stop writers/checkpoint first"
        )
    roles: dict[str, Any] = {}
    before: dict[str, dict[str, str]] = {}
    for role, path in paths.items():
        if not path.exists() or (role != "database" and not path.is_dir()):
            raise ValueError(f"missing deployment source: {role}")
        roles[role] = {"kind": "file" if path.is_file() else "directory"}
        before[role] = (
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
            if path.is_file()
            else inventory(path)
        )
    destination.mkdir(parents=True)
    for role, path in paths.items():
        target = destination / "payload" / role
        if path.is_file():
            target.mkdir(parents=True)
            shutil.copy2(path, target / path.name)
        else:
            shutil.copytree(path, target)
    for role, path in paths.items():
        after = (
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
            if path.is_file()
            else inventory(path)
        )
        if after != before[role]:
            raise ValueError("source changed during offline backup; incomplete copy retained")
    if any(Path(str(paths["database"]) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("database writer appeared during offline backup; copy is incomplete")
    expected = {
        f"{role}/{name}": sha for role, files in before.items() for name, sha in files.items()
    }
    manifest = {"format": "traceguard-offline-v1", "roles": roles, "files": expected}
    atomic_write(destination / "manifest.json", json_bytes(manifest))
    return validate(destination)


def restore(backup: Path, destination: Path, *, offline: bool) -> dict[str, Any]:
    if not offline:
        raise ValueError("stop all writers and select offline mode")
    if backup.resolve() in destination.resolve().parents:
        raise ValueError("restore destination must be outside the backup")
    validate(backup)
    if destination.exists():
        raise ValueError("restore destination must be new; existing data is protected")
    shutil.copytree(backup, destination)
    result = validate(destination)
    atomic_write(destination / "restore-result.json", json_bytes(result))
    # Operator points deployment settings at these role directories after review.
    return {**result, "restored_root": str(destination.resolve()), "activated": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify", "restore"))
    parser.add_argument(
        "--source", type=Path, required=True, help="plan JSON for create, backup otherwise"
    )
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.operation == "verify":
        result = validate(args.source)
    elif args.destination is None:
        parser.error("--destination is required")
    elif args.operation == "create":
        result = create(
            json.loads(args.source.read_bytes()), args.destination, offline=args.offline
        )
    else:
        result = restore(args.source, args.destination, offline=args.offline)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
