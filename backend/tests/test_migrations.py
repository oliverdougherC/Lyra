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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f"pragma table_info({table})").fetchall()  # noqa: S608
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone()
    return row is not None


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


def test_a_v34_database_upgrades_through_035_then_036_without_losing_existing_data(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v34-upgrade.db"
    conn = connect(db_path)
    try:
        _migrate_to(conn, 34)
        assert conn.execute("pragma user_version").fetchone()[0] == 34

        class_id = int(
            conn.execute("insert into classes (name, code) values ('Signals', 'EE 201')").lastrowid
            or 0
        )
        artifact_id = int(
            conn.execute(
                "insert into artifacts (class_id, kind, title, state) "
                "values (?, 'quiz', ?, 'ready')",
                (class_id, "Midterm review"),
            ).lastrowid
            or 0
        )
        card_part_id = int(
            conn.execute(
                "insert into artifact_parts (artifact_id, kind, ordinal, content) "
                "values (?, 'card', 1, 'Question')",
                (artifact_id,),
            ).lastrowid
            or 0
        )
        review_id = int(
            conn.execute(
                "insert into card_review_log (part_id, rating) values (?, 'good')",
                (card_part_id,),
            ).lastrowid
            or 0
        )
        older_attempt_id = int(
            conn.execute(
                "insert into quiz_attempts (artifact_id, started_at) "
                "values (?, datetime('now', '-2 minutes'))",
                (artifact_id,),
            ).lastrowid
            or 0
        )
        newer_attempt_id = int(
            conn.execute(
                "insert into quiz_attempts (artifact_id, started_at) "
                "values (?, datetime('now', '-1 minute'))",
                (artifact_id,),
            ).lastrowid
            or 0
        )
        session_id = int(
            conn.execute(
                "insert into chat_sessions (class_id, title) values (?, 'Agent session')",
                (class_id,),
            ).lastrowid
            or 0
        )
        user_message_id = int(
            conn.execute(
                "insert into messages (session_id, role, content) values (?, 'user', 'Need help')",
                (session_id,),
            ).lastrowid
            or 0
        )
        conn.execute(
            "insert into tool_audit_events ("
            "id, caller_kind, caller_id, class_id, session_id, artifact_id, tool, capability, "
            "effect, arguments_json, policy_decision, state, started_at"
            ") values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                "audit-before-036",
                "agent",
                "caller-1",
                class_id,
                session_id,
                artifact_id,
                "fetch_source",
                "web_research",
                "read",
                "{}",
                "allowed",
                "succeeded",
            ),
        )
        conn.commit()

        database._apply_migration(conn, 35, MIGRATIONS_DIR / "035_study_durability.sql")

        assert conn.execute("pragma user_version").fetchone()[0] == 35
        assert _table_exists(conn, "study_jobs")
        assert not _table_exists(conn, "agent_turn_attempts")
        assert {"op_id", "result_state"} <= _table_columns(conn, "card_review_log")
        assert {
            "question_count",
            "question_part_ids",
            "result",
            "abandoned",
        } <= _table_columns(conn, "quiz_attempts")
        assert conn.execute("select id from card_review_log").fetchone()["id"] == review_id
        attempts = conn.execute(
            "select id, finished_at, abandoned from quiz_attempts order by id"
        ).fetchall()
        assert [int(row["id"]) for row in attempts] == [older_attempt_id, newer_attempt_id]
        assert attempts[0]["finished_at"] is not None
        assert int(attempts[0]["abandoned"]) == 1
        assert attempts[1]["finished_at"] is None
        assert int(attempts[1]["abandoned"]) == 0

        database._apply_migration(conn, 36, MIGRATIONS_DIR / "036_agent_turn_attempts.sql")

        assert conn.execute("pragma user_version").fetchone()[0] == 36
        assert _table_exists(conn, "study_jobs")
        assert _table_exists(conn, "agent_turn_attempts")
        assert _table_exists(conn, "agent_attempt_targets")
        assert "attempt_id" in _table_columns(conn, "tool_audit_events")
        assert conn.execute("select id from messages where id = ?", (user_message_id,)).fetchone()
        audit_row = conn.execute(
            "select attempt_id from tool_audit_events where id = 'audit-before-036'"
        ).fetchone()
        assert audit_row is not None and audit_row["attempt_id"] is None
        assert conn.execute("pragma foreign_key_check").fetchall() == []

        assert migrate(conn) == 40
        assert _table_exists(conn, "writer_turn_attempts")
        assert _table_exists(conn, "writer_attempt_targets")
        assert _table_exists(conn, "tutor_turn_attempts")
        assert "operation_id" in _table_columns(conn, "tutor_turn_attempts")
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()

    reopened = connect(db_path)
    try:
        assert migrate(reopened) == 40
        assert reopened.execute("pragma user_version").fetchone()[0] == 40
        assert reopened.execute("pragma foreign_key_check").fetchall() == []
    finally:
        reopened.close()
