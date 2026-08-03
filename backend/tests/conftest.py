"""Shared fixtures. Every test runs against a temporary database and data directory."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.config import settings
from backend.storage.database import connect, migrate


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path-derived setting at a per-test directory.

    Autouse so no test can reach the developer's `data/lyra.db`.
    """
    root = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", root)
    monkeypatch.setattr(settings, "db_path", root / "lyra.db")
    settings.ensure_directories()
    return root


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """A migrated connection to the temporary database."""
    conn = connect()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def class_id(db: sqlite3.Connection) -> int:
    """A single class row, since almost everything is class-scoped."""
    cursor = db.execute("insert into classes (name, code) values ('Calculus II', 'MATH 201')")
    db.commit()
    return int(cursor.lastrowid or 0)
