from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.local_backup import ROLES, create, restore, validate


def plan(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "source"
    root.mkdir()
    paths = {}
    for role in ROLES - {"database"}:
        directory = root / role
        directory.mkdir()
        (directory / "sample.json").write_text(json.dumps({"role": role}), encoding="utf-8")
        paths[role] = str(directory)
    db = root / "platform.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table audit (record text)")
        conn.execute("insert into audit values ('preserved')")
    paths["database"] = str(db)
    return paths


def test_complete_backup_restore_and_existing_data_protection(tmp_path: Path) -> None:
    paths = plan(tmp_path)
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    assert create(paths, backup, offline=True)["status"] == "VERIFIED"
    assert restore(backup, restored, offline=True)["activated"] is False
    assert validate(restored)["sqlite"] == "ok"
    assert set(json.loads((backup / "manifest.json").read_bytes())["roles"]) == ROLES
    with pytest.raises(ValueError):
        restore(backup, Path(paths["cases"]), offline=True)
    with pytest.raises(ValueError):
        create(paths, backup, offline=True)
    (backup / "payload" / "candidates" / "sample.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        restore(backup, tmp_path / "corrupt-restore", offline=True)
    assert not (tmp_path / "corrupt-restore").exists()


@pytest.mark.parametrize("damage", ["online", "missing", "wal", "nested"])
def test_backup_refuses_incomplete_or_live_plan(tmp_path: Path, damage: str) -> None:
    paths = plan(tmp_path)
    destination = tmp_path / "backup"
    if damage == "missing":
        paths.pop("candidates")
    if damage == "wal":
        Path(paths["database"] + "-wal").write_bytes(b"wal")
    if damage == "nested":
        destination = Path(paths["cases"]) / "backup"
    with pytest.raises(ValueError):
        create(paths, destination, offline=damage != "online")
    assert not destination.exists()


def test_restore_refuses_backup_subdirectory(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    create(plan(tmp_path), backup, offline=True)
    with pytest.raises(ValueError):
        restore(backup, backup / "recursive", offline=True)
    assert not (backup / "recursive").exists()
