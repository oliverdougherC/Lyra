"""Packaged first-launch migration from source data into platform paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import settings
from backend.desktop_migration import migrate_source_data_if_needed
from backend.storage.database import connect, migrate


def _seed_source_tree(root: Path) -> Path:
    source = root / "source-data"
    (source / "uploads" / "1").mkdir(parents=True)
    (source / "text").mkdir()
    (source / "models").mkdir()
    (source / "uploads" / "1" / "lecture.pdf").write_bytes(b"%PDF-1.7")
    (source / "text" / "1.txt").write_text("notes", encoding="utf-8")
    (source / "models" / "manifest.txt").write_text("runtime", encoding="utf-8")
    (source / ".api_key").write_text("secret", encoding="utf-8")
    conn = connect(source / "lyra.db")
    try:
        migrate(conn)
    finally:
        conn.close()
    return source


def test_packaged_migration_copies_source_data_and_verifies_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _seed_source_tree(tmp_path)
    target = tmp_path / "app-support"
    monkeypatch.setattr(settings, "packaged_mode", True)
    monkeypatch.setattr(settings, "data_dir", target)
    monkeypatch.setattr(settings, "db_path", target / "lyra.db")
    monkeypatch.setattr(settings, "source_data_dir", source)
    monkeypatch.setattr(settings, "source_db_path", source / "lyra.db")

    result = migrate_source_data_if_needed()

    assert result.status == "migrated"
    assert (target / "uploads" / "1" / "lecture.pdf").read_bytes() == b"%PDF-1.7"
    assert (target / "text" / "1.txt").read_text(encoding="utf-8") == "notes"
    assert (target / "models" / "manifest.txt").read_text(encoding="utf-8") == "runtime"
    assert (target / ".api_key").read_text(encoding="utf-8") == "secret"
    conn = connect(target / "lyra.db")
    try:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_packaged_migration_never_overwrites_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _seed_source_tree(tmp_path)
    target = tmp_path / "app-support"
    target.mkdir()
    monkeypatch.setattr(settings, "packaged_mode", True)
    monkeypatch.setattr(settings, "data_dir", target)
    monkeypatch.setattr(settings, "db_path", target / "lyra.db")
    monkeypatch.setattr(settings, "source_data_dir", source)
    monkeypatch.setattr(settings, "source_db_path", source / "lyra.db")

    result = migrate_source_data_if_needed()

    assert result.status == "skipped"
    assert result.reason == "target_exists"
