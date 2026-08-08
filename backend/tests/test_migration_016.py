"""Migration 016 rebuilds the three artifact tables with widened checks.

SQLite cannot alter a check constraint, so the migration drops and recreates
`artifacts`, `artifact_parts`, and `artifact_sources` under foreign keys off. The
contract under test: everything a solver artifact owns survives the rebuild, no foreign
key is left dangling, and both the old and the new kinds insert afterwards.
"""

import sqlite3
from pathlib import Path

from backend.storage import database
from backend.storage.database import MIGRATIONS_DIR, connect, migrate


def _migrate_through(conn: sqlite3.Connection, version: int) -> None:
    """Apply migrations up to `version`, the way `migrate()` applies all of them."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        number = int(path.name.split("_")[0])
        if number <= version:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"pragma user_version = {number}")
            conn.commit()


def _seed_solver_artifact(conn: sqlite3.Connection) -> int:
    """A solution set with one of every child row, written against the pre-016 schema."""
    class_id = int(conn.execute("insert into classes (name) values ('Signals')").lastrowid or 0)
    document_id = int(
        conn.execute(
            "insert into documents "
            "(class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'hw3.pdf', '/tmp/hw3.pdf', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    artifact_id = int(
        conn.execute(
            "insert into artifacts (class_id, kind, title, state, problems_total, "
            "problems_done) values (?, 'solution_set', 'Homework 3', 'ready', 1, 1)",
            (class_id,),
        ).lastrowid
        or 0
    )
    conn.execute(
        "insert into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, 'problem_set', 0)",
        (artifact_id, document_id),
    )
    problem_id = int(
        conn.execute(
            "insert into artifact_parts (artifact_id, kind, ordinal, label, content, "
            "status, origin, verdict) values (?, 'problem', 1, '1', 'Find x.', "
            "'complete', 'generated', 'verified')",
            (artifact_id,),
        ).lastrowid
        or 0
    )
    conn.execute(
        "insert into artifact_part_revisions (part_id, revision, content, origin, note) "
        "values (?, 1, 'Find x.', 'generated', NULL)",
        (problem_id,),
    )
    conn.execute(
        "insert into artifact_provenance (part_id, chunk_id, document_id, page_number, "
        "label) values (?, NULL, ?, 1, '1')",
        (problem_id, document_id),
    )
    conn.execute(
        "insert into artifact_checks (part_id, ordinal, tool, arguments, ok, result) "
        "values (?, 1, 'cas', '{}', 1, '{}')",
        (problem_id,),
    )
    conn.commit()
    return artifact_id


def test_016_preserves_every_child_row(tmp_path: Path) -> None:
    """Seed at 015, migrate, and count: the rebuild must be lossless."""
    conn = connect(tmp_path / "pre016.db")
    try:
        _migrate_through(conn, 15)
        artifact_id = _seed_solver_artifact(conn)

        version = migrate(conn)

        assert version >= 16
        artifact = conn.execute(
            "select kind, state, problems_total from artifacts where id = ?", (artifact_id,)
        ).fetchone()
        assert (artifact["kind"], artifact["state"], artifact["problems_total"]) == (
            "solution_set",
            "ready",
            1,
        )
        assert conn.execute("select count(*) from artifact_sources").fetchone()[0] == 1
        part = conn.execute("select id, verdict, solve_parts from artifact_parts").fetchone()
        assert part["verdict"] == "verified"
        # Columns later migrations added must survive the rebuild with their values.
        assert part["solve_parts"] == "together"
        assert conn.execute("select count(*) from artifact_part_revisions").fetchone()[0] == 1
        assert conn.execute("select count(*) from artifact_provenance").fetchone()[0] == 1
        assert conn.execute("select count(*) from artifact_checks").fetchone()[0] == 1
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_016_accepts_the_new_kinds_and_keeps_the_old(tmp_path: Path) -> None:
    conn = connect(tmp_path / "pre016.db")
    try:
        _migrate_through(conn, 15)
        _seed_solver_artifact(conn)
        class_id = int(conn.execute("select id from classes").fetchone()[0])
        migrate(conn)

        for kind in ("flashcard_deck", "quiz", "draft", "solution_set"):
            conn.execute(
                "insert into artifacts (class_id, kind, title, state) "
                "values (?, ?, 'probe', 'pending')",
                (class_id, kind),
            )
        # And the widened part kinds and content type.
        deck_id = int(
            conn.execute("select id from artifacts where kind = 'flashcard_deck'").fetchone()[0]
        )
        conn.execute(
            "insert into artifact_parts (artifact_id, kind, ordinal, content_type) "
            "values (?, 'card', 1, 'json')",
            (deck_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_016_rolls_back_cleanly_and_can_retry_after_fault_injection(
    tmp_path: Path, monkeypatch
) -> None:
    conn = connect(tmp_path / "pre016.db")
    try:
        _migrate_through(conn, 15)
        artifact_id = _seed_solver_artifact(conn)
        version_before = conn.execute("pragma user_version").fetchone()[0]
        original_schema = conn.execute(
            "select sql from sqlite_master where type = 'table' and name = 'artifacts'"
        ).fetchone()[0]
        original_execute = database._execute_migration_statements

        def fail_mid_rebuild(connection: sqlite3.Connection, statements: list[str]) -> None:
            for statement in statements[:4]:
                connection.execute(statement)
            raise sqlite3.OperationalError("fault injection during migration 016")

        monkeypatch.setattr(database, "_execute_migration_statements", fail_mid_rebuild)

        try:
            migrate(conn)
        except sqlite3.OperationalError as exc:
            assert "fault injection" in str(exc)
        else:
            raise AssertionError("migration 016 should have failed under fault injection")

        assert conn.execute("pragma user_version").fetchone()[0] == version_before == 15
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
        assert (
            conn.execute(
                "select name from sqlite_master where type = 'table' and name = 'artifacts_new'"
            ).fetchone()
            is None
        )
        artifact = conn.execute(
            "select kind, title from artifacts where id = ?", (artifact_id,)
        ).fetchone()
        assert (artifact["kind"], artifact["title"]) == ("solution_set", "Homework 3")
        rolled_back_schema = conn.execute(
            "select sql from sqlite_master where type = 'table' and name = 'artifacts'"
        ).fetchone()[0]
        assert rolled_back_schema == original_schema

        monkeypatch.setattr(database, "_execute_migration_statements", original_execute)
        version = migrate(conn)

        assert version >= 16
        migrated_artifact = conn.execute(
            "select kind, title from artifacts where id = ?", (artifact_id,)
        ).fetchone()
        assert (migrated_artifact["kind"], migrated_artifact["title"]) == (
            "solution_set",
            "Homework 3",
        )
    finally:
        conn.close()
