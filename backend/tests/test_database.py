"""SQLite connection and migration reliability regressions."""

import threading
import time
from pathlib import Path

from backend.storage.database import _SQLITE_BUSY_TIMEOUT_MS, connect, migrate


def test_connect_sets_a_bounded_busy_timeout(tmp_path: Path) -> None:
    conn = connect(tmp_path / "busy-timeout.db")
    try:
        assert conn.execute("pragma busy_timeout").fetchone()[0] == _SQLITE_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_schema_49_document_upgrade_keeps_original_and_backs_up_before_nickname(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pre-nickname.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        class_id = int(conn.execute("insert into classes (name) values ('Math')").lastrowid)
        document_id = int(
            conn.execute(
                "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
                "values (?, 'LADW_2026_08-31.pdf', 'unchanged-source', 'application/pdf', "
                "123, 'ready')",
                (class_id,),
            ).lastrowid
        )
        conn.commit()
        # Recreate the immediately preceding schema from this otherwise current fixture.
        conn.execute("alter table documents drop column nickname")
        conn.execute("pragma user_version = 49")
        conn.commit()

        assert migrate(conn) == 50
        assert tuple(
            conn.execute(
                "select filename, stored_path, byte_size, nickname from documents where id = ?",
                (document_id,),
            ).fetchone()
        ) == ("LADW_2026_08-31.pdf", "unchanged-source", 123, None)
        backups = list((tmp_path / "migration-backups").glob("schema-49-*/lyra.db"))
        assert len(backups) == 1
    finally:
        conn.close()


def test_busy_timeout_allows_a_blocked_writer_to_complete_after_lock_release(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "writer-contention.db"
    lock_holder = connect(db_path)
    blocked_writer = connect(db_path)
    try:
        migrate(lock_holder)
        migrate(blocked_writer)

        lock_holder.execute("begin immediate")
        lock_holder.execute("insert into classes (name, code) values ('Locked Class', 'LOCK-1')")

        started = threading.Event()
        writer_done = threading.Event()
        outcome: dict[str, float | int | str] = {}

        def write_while_blocked() -> None:
            started.set()
            began_at = time.perf_counter()
            try:
                cursor = blocked_writer.execute(
                    "insert into classes (name, code) values ('Waiting Class', 'WAIT-1')"
                )
                blocked_writer.commit()
                outcome["row_id"] = int(cursor.lastrowid or 0)
            except Exception as exc:  # pragma: no cover - the assertion below unwraps this.
                outcome["error"] = repr(exc)
            finally:
                outcome["elapsed"] = time.perf_counter() - began_at
                writer_done.set()

        worker = threading.Thread(target=write_while_blocked)
        worker.start()
        assert started.wait(timeout=1)

        time.sleep(0.2)
        lock_holder.commit()

        assert writer_done.wait(timeout=2)
        worker.join(timeout=1)
        assert "error" not in outcome
        assert outcome["elapsed"] >= 0.2
        count = blocked_writer.execute(
            "select count(*) from classes where code in ('LOCK-1', 'WAIT-1')"
        ).fetchone()[0]
        assert count == 2
    finally:
        blocked_writer.close()
        lock_holder.close()
