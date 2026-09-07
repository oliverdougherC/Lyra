"""Shared fixtures. Every test runs against a temporary database and data directory."""

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.errors
import pytest

from backend.config import settings
from backend.storage.database import connect, migrate


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Never let a unit/integration test touch the developer's OS credential store.

    Module-local fake backends may still override the application's adapter. Patch
    the public keyring API as a second boundary and force spawned Python processes
    onto NullKeyring. Real Keychain acceptance belongs in a separately authorized
    harness, never the ordinary pytest suite.
    """
    values: dict[tuple[str, str], str] = {}

    def read(service: str, username: str) -> str | None:
        return values.get((service, username))

    def write(service: str, username: str, value: str) -> None:
        values[(service, username)] = value

    def delete(service: str, username: str) -> None:
        if (service, username) not in values:
            raise keyring.errors.PasswordDeleteError("No test credential exists.")
        del values[(service, username)]

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setattr(keyring, "get_password", read)
    monkeypatch.setattr(keyring, "set_password", write)
    monkeypatch.setattr(keyring, "delete_password", delete)
    return values


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


@pytest.fixture(autouse=True)
def isolated_helper_quit_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test represents a fresh backend lifetime; quit stays permanent within it."""
    from backend.llm.embed_server import embedding_server
    from backend.llm.ocr_server import ocr_server
    from backend.llm.rerank_server import rerank_server

    for helper in (embedding_server, ocr_server, rerank_server):
        monkeypatch.setattr(helper, "_quitting", threading.Event())
