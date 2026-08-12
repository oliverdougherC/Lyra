"""Upgrades reach head from every released version, not only from a fresh database.

`migrate()` is forward-only and, in production, always runs to head. What a released
install actually does is sit at some intermediate `user_version` and then upgrade on the
next launch. These tests reproduce that: they reach each old version through the production
per-file apply - the same foreign-key-pragma stripping and atomic commit a real install
used - and then migrate to head, so the state being upgraded from is the state the app
truly produced rather than one `executescript` happens to leave behind.
"""

import sqlite3
from pathlib import Path

import pytest

from backend.storage import database
from backend.storage.database import MIGRATIONS_DIR, connect, migrate


def _migration_numbers() -> list[int]:
    numbers = sorted(int(path.name.split("_")[0]) for path in MIGRATIONS_DIR.glob("*.sql"))
    if not numbers:
        raise RuntimeError("No migrations were found.")
    return numbers


NUMBERS = _migration_numbers()
LATEST = NUMBERS[-1]


def _migrate_to(conn: sqlite3.Connection, target: int) -> None:
    """Reach `target` through the real per-file apply, as a released install reached it."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        number = int(path.name.split("_")[0])
        if number <= target:
            database._apply_migration(conn, number, path)


def test_a_fresh_database_migrates_to_the_latest_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    try:
        assert migrate(conn) == LATEST
        assert conn.execute("pragma user_version").fetchone()[0] == LATEST
        # Migration 001 inserts the singleton settings row; a full migrate must keep it.
        assert conn.execute("select count(*) from settings").fetchone()[0] == 1
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """A second launch finds nothing pending and leaves the version where it was."""
    conn = connect(tmp_path / "twice.db")
    try:
        first = migrate(conn)
        second = migrate(conn)
        assert first == second == LATEST
    finally:
        conn.close()


@pytest.mark.parametrize("start", NUMBERS[:-1])
def test_an_install_at_any_released_version_upgrades_to_head(tmp_path: Path, start: int) -> None:
    """From every intermediate version, the real upgrade path reaches head with FKs intact."""
    conn = connect(tmp_path / f"v{start}.db")
    try:
        _migrate_to(conn, start)
        assert conn.execute("pragma user_version").fetchone()[0] == start

        assert migrate(conn) == LATEST
        assert conn.execute("pragma user_version").fetchone()[0] == LATEST
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_data_written_against_an_early_schema_survives_the_upgrade(tmp_path: Path) -> None:
    """A row written at the v1 schema is still there, unchanged, at head."""
    conn = connect(tmp_path / "early.db")
    try:
        _migrate_to(conn, 1)
        class_id = int(conn.execute("insert into classes (name) values ('Signals')").lastrowid or 0)
        conn.commit()

        migrate(conn)

        row = conn.execute("select name from classes where id = ?", (class_id,)).fetchone()
        assert row["name"] == "Signals"
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_the_per_file_helper_reaches_the_same_schema_as_migrate(tmp_path: Path) -> None:
    """Guard the tests' own premise: reaching head file-by-file must land where migrate()
    does. If `_migrate_to` and `migrate()` ever diverged, the upgrade tests above would be
    starting from a state migrate() never produces, and would prove nothing.
    """
    helper = connect(tmp_path / "helper.db")
    whole = connect(tmp_path / "whole.db")
    try:
        _migrate_to(helper, LATEST)
        migrate(whole)

        def schema(conn: sqlite3.Connection) -> list[str]:
            return [
                str(row[0])
                for row in conn.execute(
                    "select sql from sqlite_master where sql is not null order by name"
                )
            ]

        assert helper.execute("pragma user_version").fetchone()[0] == LATEST
        assert schema(helper) == schema(whole)
    finally:
        helper.close()
        whole.close()
