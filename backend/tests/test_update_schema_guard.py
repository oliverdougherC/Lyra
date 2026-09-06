"""An older binary must refuse future data before startup writes."""

import sqlite3
from pathlib import Path

import pytest

from backend.storage import database


def test_future_schema_refused_before_connection_side_effects(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("create table student_work (body text)")
        conn.execute("insert into student_work values ('keep my words')")
        conn.execute("pragma user_version = 9999")
    before = path.read_bytes()
    entries_before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(RuntimeError, match="newer version of Lyra"):
        database.connect(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == entries_before


def test_migrate_itself_refuses_future_schema(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "future.db") as conn:
        conn.execute("pragma user_version = 9999")
        with pytest.raises(RuntimeError, match="newer version of Lyra"):
            database.migrate(conn)
        assert conn.execute("pragma user_version").fetchone()[0] == 9999


def test_forward_migration_preserves_verified_previous_database(tmp_path: Path) -> None:
    path = tmp_path / "student.db"
    with database.connect(path) as conn:
        first = sorted(database.MIGRATIONS_DIR.glob("*.sql"))[0]
        database._apply_migration(conn, 1, first)
        conn.execute("update settings set model = 'preserved-model'")
        conn.commit()
        database.migrate(conn)
    backups = list((tmp_path / "migration-backups").glob("*/lyra.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("pragma user_version").fetchone()[0] == 1
        assert backup.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert backup.execute("select model from settings").fetchone()[0] == "preserved-model"
    assert backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("packaged", [False, True])
def test_startup_refuses_future_before_recovery_or_directory_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packaged: bool,
) -> None:
    import asyncio

    from fastapi import FastAPI

    import backend.main as application

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("pragma user_version = 9999")
    monkeypatch.setattr(application.settings, "db_path", path)
    monkeypatch.setattr(application.settings, "packaged_mode", packaged)
    reached = []
    monkeypatch.setattr(
        application, "recover_desktop_import_publish", lambda: reached.append("recovery")
    )
    monkeypatch.setattr(
        type(application.settings), "ensure_directories", lambda self: reached.append("directories")
    )
    with pytest.raises(RuntimeError, match="newer version of Lyra"):
        asyncio.run(application.lifespan(FastAPI()).__aenter__())
    assert reached == []


def test_backup_failure_stops_before_forward_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "student.db"
    with database.connect(path) as conn:
        first = sorted(database.MIGRATIONS_DIR.glob("*.sql"))[0]
        database._apply_migration(conn, 1, first)

        def full_disk(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(database, "_backup_before_migration", full_disk)
        with pytest.raises(OSError, match="No space"):
            database.migrate(conn)
        assert conn.execute("pragma user_version").fetchone()[0] == 1


def test_profile_backup_copies_originals_and_text_with_hashes(db, monkeypatch):
    import hashlib
    import json

    from backend.config import settings

    original = settings.uploads_dir / "kept.pdf"
    original.write_bytes(b"original document")
    text = settings.text_dir / "kept.md"
    text.write_bytes(b"extracted words")
    database._backup_before_migration(db, database.latest_schema_version())
    snapshot = next((settings.db_path.parent / "migration-backups").iterdir())
    manifest = json.loads((snapshot / "backup-manifest.json").read_text())
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((snapshot / relative).read_bytes()).hexdigest() == digest
    original.unlink()
    assert (snapshot / "uploads/kept.pdf").read_bytes() == b"original document"
    assert (snapshot / "text/kept.md").read_bytes() == b"extracted words"
