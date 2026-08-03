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


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name, cascades on, WAL, and sqlite-vec loaded."""
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI submits a sync dependency generator and its sync
    # handler as two separate threadpool jobs, which are not guaranteed to land on the
    # same worker. A connection is never shared between concurrent requests, so the
    # accesses stay serial.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("pragma foreign_keys = on")
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
        conn.executescript(path.read_text(encoding="utf-8"))
        # pragma does not accept a bound parameter, and `number` comes from a filename
        # matched against a digits-only regex above.
        conn.execute(f"pragma user_version = {number}")
        conn.commit()
        version = number

    return version


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency yielding a per-request connection."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
