"""SQLite connection handling and forward-only migrations.

The stdlib driver is used directly with hand-written SQL. `vec0` virtual tables and
the extension load do not map onto an ORM, and the schema is small enough that models
would only add indirection.
"""

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import sqlite_vec

from backend.config import settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")
_MIGRATION_FK_OFF = re.compile(r"^pragma\s+foreign_keys\s*=\s*off\s*;?$", re.IGNORECASE)
_MIGRATION_FK_ON = re.compile(r"^pragma\s+foreign_keys\s*=\s*on\s*;?$", re.IGNORECASE)
_SQL_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQLITE_BUSY_TIMEOUT_MS = 5_000


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name, cascades on, WAL, and sqlite-vec loaded."""
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI submits a sync dependency generator and its sync
    # handler as two separate threadpool jobs, which are not guaranteed to land on the
    # same worker. A connection is never shared between concurrent requests, so the
    # accesses stay serial.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1_000)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("pragma foreign_keys = on")
    conn.execute(f"pragma busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("pragma journal_mode = wal")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration numbered above `pragma user_version`. Returns the new version."""
    version = conn.execute("pragma user_version").fetchone()[0]
    pending = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise RuntimeError(f"Migration file is not numbered: {path.name}")
        number = int(match.group(1))
        if number > version:
            pending.append((number, path))

    for number, path in pending:
        _apply_migration(conn, number, path)
        version = number

    return version


def _apply_migration(conn: sqlite3.Connection, number: int, path: Path) -> None:
    """Apply one migration atomically and only advance the version after it sticks."""
    statements, disables_foreign_keys = _load_migration_statements(path)

    if conn.in_transaction:
        conn.commit()

    if disables_foreign_keys:
        conn.execute("pragma foreign_keys = off")

    try:
        conn.execute("begin immediate")
        _execute_migration_statements(conn, statements)
        if disables_foreign_keys:
            foreign_key_errors = conn.execute("pragma foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(
                    f"Migration {path.name} left dangling foreign keys: {foreign_key_errors!r}"
                )
        # pragma does not accept a bound parameter, and `number` comes from a filename
        # matched against a digits-only regex above.
        conn.execute(f"pragma user_version = {number}")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if disables_foreign_keys:
            conn.execute("pragma foreign_keys = on")


def _load_migration_statements(path: Path) -> tuple[list[str], bool]:
    """Parse a migration script and strip only the pragma lines migrate() owns."""
    statements = _split_sql_script(path.read_text(encoding="utf-8"))
    filtered: list[str] = []
    saw_fk_off = False
    saw_fk_on = False

    for statement in statements:
        normalized = _normalize_sql(statement)
        if not normalized:
            continue
        if _MIGRATION_FK_OFF.fullmatch(normalized):
            saw_fk_off = True
            continue
        if _MIGRATION_FK_ON.fullmatch(normalized):
            saw_fk_on = True
            continue
        filtered.append(statement)

    if saw_fk_off != saw_fk_on:
        raise RuntimeError(
            f"Migration {path.name} must toggle foreign_keys off and back on in the same file"
        )

    return filtered, saw_fk_off


def _split_sql_script(script: str) -> list[str]:
    """Split SQL into execute()-ready statements without changing their contents."""
    statements: list[str] = []
    chunk: list[str] = []

    for line in script.splitlines(keepends=True):
        chunk.append(line)
        candidate = "".join(chunk)
        if candidate.strip() and sqlite3.complete_statement(candidate):
            if _normalize_sql(candidate):
                statements.append(candidate)
            chunk = []

    remainder = "".join(chunk)
    if _normalize_sql(remainder):
        raise RuntimeError("Migration script ended with an incomplete SQL statement")

    return statements


def _normalize_sql(statement: str) -> str:
    """Remove comments and collapse whitespace for pragma detection and empty checks."""
    without_block_comments = _SQL_BLOCK_COMMENT.sub("", statement)
    without_comments = _SQL_LINE_COMMENT.sub("", without_block_comments)
    return " ".join(without_comments.split()).strip()


def _execute_migration_statements(conn: sqlite3.Connection, statements: list[str]) -> None:
    """Run already-parsed migration statements inside the caller's transaction."""
    for statement in statements:
        conn.execute(statement)


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding a per-request connection."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
