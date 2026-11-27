from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.local_backup as local_backup
from scripts.local_backup import (
    DIRECTORY_ROLES,
    SEPARATE_DATABASE_ROLES,
    SEPARATE_ROLES,
    SHARED_DATABASE_ROLES,
    SHARED_ROLES,
    create,
    restore,
    validate,
)


def plan(tmp_path: Path, topology: str = "separate") -> dict[str, str]:
    root = tmp_path / "source"
    root.mkdir()
    paths: dict[str, str] = {"topology": topology}
    for role in DIRECTORY_ROLES:
        directory = root / role
        directory.mkdir()
        (directory / "sample.json").write_text(json.dumps({"role": role}), encoding="utf-8")
        paths[role] = str(directory)
    database_roles = SEPARATE_DATABASE_ROLES if topology == "separate" else SHARED_DATABASE_ROLES
    for role in database_roles:
        db = root / f"{role}.db"
        with sqlite3.connect(db) as conn:
            conn.execute("create table audit (record text)")
            conn.execute("insert into audit values ('preserved')")
        paths[role] = str(db)
    return paths


@pytest.mark.parametrize("topology", ["shared", "separate"])
def test_complete_backup_restore_and_existing_data_protection(
    tmp_path: Path, topology: str
) -> None:
    paths = plan(tmp_path, topology)
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    assert create(paths, backup, offline=True)["status"] == "VERIFIED"
    assert restore(backup, restored, offline=True)["activated"] is False
    database_roles = SHARED_DATABASE_ROLES if topology == "shared" else SEPARATE_DATABASE_ROLES
    role_set = SHARED_ROLES if topology == "shared" else SEPARATE_ROLES
    assert validate(restored)["sqlite"] == {role: "ok" for role in database_roles}
    manifest = json.loads((backup / "manifest.json").read_bytes())
    assert set(manifest["roles"]) == role_set
    assert manifest["format"] == "traceguard-offline-v2"
    assert manifest["topology"] == topology
    assert manifest["sqlite_database_count"] == len(database_roles)
    assert manifest["backup_id"].startswith("bk_") and manifest["created_at"]
    with pytest.raises(ValueError):
        restore(backup, Path(paths["cases"]), offline=True)
    with pytest.raises(ValueError):
        create(paths, backup, offline=True)
    (backup / "payload" / "candidates" / "sample.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        restore(backup, tmp_path / "corrupt-restore", offline=True)
    assert not (tmp_path / "corrupt-restore").exists()


@pytest.mark.parametrize("damage", ["online", "missing", "missing_db", "wal", "journal", "nested"])
def test_backup_refuses_incomplete_or_live_plan(tmp_path: Path, damage: str) -> None:
    paths = plan(tmp_path)
    destination = tmp_path / "backup"
    if damage == "missing":
        paths.pop("candidates")
    if damage == "missing_db":
        paths["teamflow_database"] = str(tmp_path / "missing-teamflow.db")
    if damage == "wal":
        Path(paths["teamflow_database"] + "-wal").write_bytes(b"wal")
    if damage == "journal":
        Path(paths["teamflow_database"] + "-journal").write_bytes(b"journal")
    if damage == "nested":
        destination = Path(paths["cases"]) / "backup"
    with pytest.raises(ValueError):
        create(paths, destination, offline=damage != "online")
    assert not destination.exists()


def test_source_mutation_during_copy_is_detected(tmp_path: Path, monkeypatch) -> None:
    paths = plan(tmp_path)
    destination = tmp_path / "backup"
    original_copytree = local_backup.shutil.copytree

    def racing_copy(source, target, *args, **kwargs):
        result = original_copytree(source, target, *args, **kwargs)
        if Path(source) == Path(paths["cases"]):
            (Path(source) / "sample.json").write_text('{"changed":true}', encoding="utf-8")
        return result

    monkeypatch.setattr(local_backup.shutil, "copytree", racing_copy)
    with pytest.raises(ValueError, match="source changed during offline backup"):
        create(paths, destination, offline=True)
    assert not (destination / "manifest.json").exists()


def test_manifest_tampering_and_missing_payload_are_rejected(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    create(plan(tmp_path), backup, offline=True)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    first_file = next(iter(manifest["files"]))
    manifest["files"][first_file] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory/digest mismatch"):
        validate(backup)

    manifest["files"][first_file] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    payload_file = backup / "payload" / first_file
    payload_file.unlink()
    with pytest.raises(ValueError, match="inventory/digest mismatch"):
        validate(backup)


def test_restore_refuses_backup_subdirectory(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    create(plan(tmp_path), backup, offline=True)
    with pytest.raises(ValueError):
        restore(backup, backup / "recursive", offline=True)
    assert not (backup / "recursive").exists()


def test_v1_shared_backup_remains_readable(tmp_path: Path) -> None:
    backup = tmp_path / "backup-v2"
    create(plan(tmp_path, "shared"), backup, offline=True)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["format"] = "traceguard-offline-v1"
    manifest.pop("topology")
    manifest.pop("backup_id")
    manifest.pop("created_at")
    manifest.pop("sqlite_database_count")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert validate(backup)["topology"] == "shared"
