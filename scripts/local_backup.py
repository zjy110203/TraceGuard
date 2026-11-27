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
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.execution.candidate_transaction import atomic_write, json_bytes

DIRECTORY_ROLES = {"cases", "candidates", "explorations", "chains", "blobs", "benchmark"}
SHARED_DATABASE_ROLES = {"database"}
SEPARATE_DATABASE_ROLES = {"platform_database", "teamflow_database"}
DATABASE_ROLES = SHARED_DATABASE_ROLES | SEPARATE_DATABASE_ROLES
SHARED_ROLES = SHARED_DATABASE_ROLES | DIRECTORY_ROLES
SEPARATE_ROLES = SEPARATE_DATABASE_ROLES | DIRECTORY_ROLES
LEGACY_ROLES = SHARED_ROLES


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
    format_version = manifest.get("format")
    topology = "shared" if format_version == "traceguard-offline-v1" else manifest.get("topology")
    database_roles = (
        SHARED_DATABASE_ROLES
        if topology == "shared"
        else SEPARATE_DATABASE_ROLES
        if topology == "separate"
        else set()
    )
    expected_roles = (
        SHARED_ROLES
        if format_version == "traceguard-offline-v1" and topology == "shared"
        else SHARED_ROLES
        if format_version == "traceguard-offline-v2" and topology == "shared"
        else SEPARATE_ROLES
        if format_version == "traceguard-offline-v2" and topology == "separate"
        else set()
    )
    if set(manifest.get("roles", {})) != expected_roles:
        raise ValueError("invalid/incomplete deployment manifest")
    if format_version == "traceguard-offline-v2":
        if (
            not isinstance(manifest.get("backup_id"), str)
            or not manifest["backup_id"].startswith("bk_")
            or not manifest.get("created_at")
            or manifest.get("sqlite_database_count") != len(database_roles)
        ):
            raise ValueError("invalid backup batch metadata")
    actual = inventory(root / "payload")
    if actual != manifest["files"]:
        raise ValueError("backup file inventory/digest mismatch")
    sqlite_status: dict[str, str] = {}
    for role, row in manifest["roles"].items():
        expected_kind = "file" if role in database_roles else "directory"
        if row.get("kind") != expected_kind or row.get("source_role", role) != role:
            raise ValueError("invalid role kind")
        base = root / "payload" / role
        if not base.is_dir():
            raise ValueError(f"missing role: {role}")
        if role in database_roles:
            children = list(base.iterdir())
            if len(children) != 1 or not children[0].is_file():
                raise ValueError(f"exactly one SQLite file required for role: {role}")
            # sqlite3.Connection.__exit__ commits/rolls back but does not close
            # the handle. Close explicitly so Windows can safely copy or remove
            # a verified backup immediately afterward.
            with closing(
                sqlite3.connect(children[0].resolve().as_uri() + "?mode=ro", uri=True)
            ) as conn:
                if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError(f"SQLite integrity check failed: {role}")
            sqlite_status[role] = "ok"
            continue
        for path in base.rglob("*.json"):
            json.loads(path.read_bytes())
        for path in base.rglob("*.jsonl"):
            for line in path.read_bytes().splitlines():
                if line.strip():
                    json.loads(line)
    return {
        "status": "VERIFIED",
        "files": len(actual),
        "sqlite": "ok" if format_version == "traceguard-offline-v1" else sqlite_status,
        "topology": topology,
    }


def create(plan: dict[str, Any], destination: Path, *, offline: bool) -> dict[str, Any]:
    topology = plan.get("topology")
    database_roles = (
        SHARED_DATABASE_ROLES
        if topology == "shared"
        else SEPARATE_DATABASE_ROLES
        if topology == "separate"
        else set()
    )
    expected_roles = (
        SHARED_ROLES
        if topology == "shared"
        else SEPARATE_ROLES
        if topology == "separate"
        else set()
    )
    if not offline or not expected_roles or set(plan) != expected_roles | {"topology"}:
        raise ValueError("offline mode and a complete, explicit shared/separate plan are required")
    destination = destination.resolve()
    paths = {role: Path(plan[role]).resolve() for role in expected_roles}
    if any(destination == path or path in destination.parents for path in paths.values()):
        raise ValueError("backup destination must be outside every source")
    if destination.exists():
        raise ValueError("backup destination must be new; existing data is protected")
    for role in database_roles:
        database_path = paths.get(role)
        if (
            database_path is None
            or not database_path.is_file()
            or any(Path(str(database_path) + suffix).exists() for suffix in ("-wal", "-journal"))
        ):
            raise ValueError(
                f"{role} must exist and have no WAL/journal; stop writers/checkpoint first"
            )
    roles: dict[str, Any] = {}
    before: dict[str, dict[str, str]] = {}
    for role, path in paths.items():
        is_database = role in DATABASE_ROLES
        if (
            not path.exists()
            or (is_database and not path.is_file())
            or (not is_database and not path.is_dir())
        ):
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
    if any(
        Path(str(paths[role]) + suffix).exists()
        for role in database_roles
        for suffix in ("-wal", "-journal")
    ):
        raise ValueError("database writer appeared during offline backup; copy is incomplete")
    expected = {
        f"{role}/{name}": sha for role, files in before.items() for name, sha in files.items()
    }
    manifest = {
        "format": "traceguard-offline-v2",
        "topology": topology,
        "backup_id": f"bk_{uuid4().hex}",
        "created_at": datetime.now(UTC).isoformat(),
        "sqlite_database_count": len(database_roles),
        "roles": {role: {**row, "source_role": role} for role, row in roles.items()},
        "files": expected,
    }
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
