"""SQLite connection handling and forward-only migrations.

The stdlib driver is used directly with hand-written SQL. `vec0` virtual tables and
the extension load do not map onto an ORM, and the schema is small enough that models
would only add indirection.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import sqlite_vec

from backend.config import settings
from backend.storage import private

# WAL leaves two predictable sidecars beside the database. They are secured before SQLite
# opens the database, because post-open hardening is too late for both permissive umasks and
# a planted symlink at either pathname.
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")
_MIGRATION_FK_OFF = re.compile(r"^pragma\s+foreign_keys\s*=\s*off\s*;?$", re.IGNORECASE)
_MIGRATION_FK_ON = re.compile(r"^pragma\s+foreign_keys\s*=\s*on\s*;?$", re.IGNORECASE)
_SQL_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQLITE_BUSY_TIMEOUT_MS = 5_000


def latest_schema_version() -> int:
    return max(int(path.name.split("_", 1)[0]) for path in MIGRATIONS_DIR.glob("*.sql"))


def _refuse_future_schema(version: int) -> None:
    if version > latest_schema_version():
        raise RuntimeError(
            "This data was saved by a newer version of Lyra. Reopen the newer app; "
            "this app has not migrated or recovered your data. To use an older app, "
            "restore a verified pre-migration backup into a separate data location."
        )


def assert_schema_compatible(path: Path | None = None) -> None:
    """Read the committed schema before directory hardening, recovery, or WAL setup."""
    path = path or settings.db_path
    private.assert_not_symlink(path, "the database path")
    if not path.exists() or path.stat().st_size == 0:
        return
    for suffix in _DB_SIDECAR_SUFFIXES:
        private.assert_not_symlink(path.with_name(path.name + suffix), "a database sidecar")
    # Read-only SQLite includes committed WAL state. immutable=1 would miss a newer
    # schema still in WAL after a crash, so it must not be used for this guard.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        _refuse_future_schema(int(conn.execute("pragma user_version").fetchone()[0]))
    finally:
        conn.close()


def _backup_before_migration(conn: sqlite3.Connection, version: int) -> None:
    """Keep a verified, private SQLite snapshot; never replace a previous backup."""
    database_path = conn.execute("pragma database_list").fetchone()[2]
    if not database_path:  # In-memory databases are tests, not persistent student data.
        return
    directory = Path(database_path).parent / "migration-backups"
    private.secure_mkdir(directory, root=directory.parent)
    snapshot = directory / f"schema-{version}-{uuid.uuid4().hex}"
    private.secure_mkdir(snapshot, root=directory)
    path = snapshot / "lyra.db"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    backup = sqlite3.connect(path)
    try:
        if conn.in_transaction:
            conn.commit()
        conn.backup(backup)
        backup.enable_load_extension(True)
        sqlite_vec.load(backup)
        backup.enable_load_extension(False)
        if backup.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(
                "Pre-migration backup failed integrity verification; upgrade stopped."
            )
        if backup.execute("pragma user_version").fetchone()[0] != version:
            raise RuntimeError("Pre-migration backup schema verification failed; upgrade stopped.")
        backup.close()
        files = {"lyra.db": hashlib.sha256(path.read_bytes()).hexdigest()}
        if Path(database_path) == settings.db_path:
            # These are durable originals and extracted text. Models/caches can be
            # downloaded again; keys remain in the existing Keychain/profile, never
            # copied into an update archive or a shareable backup receipt.
            for tree in (settings.uploads_dir, settings.text_dir):
                if not tree.exists():
                    continue
                private.assert_not_symlink(tree, "a backup source directory")
                for parent, directories, names in os.walk(tree, followlinks=False):
                    for name in directories:
                        private.assert_not_symlink(Path(parent) / name, "a backup source directory")
                    relative_parent = Path(parent).relative_to(settings.data_dir)
                    private.secure_mkdir(snapshot / relative_parent, root=snapshot)
                    for name in names:
                        original = Path(parent) / name
                        relative = original.relative_to(settings.data_dir)
                        content = private.read_owned_bytes(
                            original, root=settings.data_dir, max_bytes=512 * 1024 * 1024
                        )
                        destination = snapshot / relative
                        private.write_private_bytes(destination, content)
                        digest = hashlib.sha256(content).hexdigest()
                        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                            raise RuntimeError("Backup file verification failed; upgrade stopped.")
                        with destination.open("rb") as saved_file:
                            os.fsync(saved_file.fileno())
                        files[str(relative)] = digest
        private.write_private_text(
            snapshot / "backup-manifest.json",
            json.dumps({"schema": version, "files": files}, sort_keys=True),
        )
        with (snapshot / "backup-manifest.json").open("rb") as receipt:
            os.fsync(receipt.fileno())
        with path.open("rb") as saved:
            os.fsync(saved.fileno())
        for parent, _, _ in os.walk(snapshot, topdown=False):
            snapshot_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(snapshot_fd)
            finally:
                os.close(snapshot_fd)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        backup.close()
        shutil.rmtree(snapshot)
        raise


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name, cascades on, WAL, and sqlite-vec loaded."""
    path = db_path or settings.db_path
    # A symlinked database path is refused before anything opens it: sqlite would follow the
    # link to create or open the target, and the hardening below would chmod that outside
    # file while reporting the configured path as secured. An explicit LYRA_DB_PATH should
    # name a real file (or a location under a real directory), not a link out of the tree.
    private.assert_not_symlink(path, "the database path")
    assert_schema_compatible(path)
    # Inside the data tree, the database's parent chain is owned by Lyra and created with the
    # same no-symlink-beneath-root guarantee as the rest of it. An explicit LYRA_DB_PATH may
    # point outside that tree, into a location the user chose; there Lyra owns only the
    # immediate directory it creates for the database (created `0o700`, like any Lyra
    # directory), and treats that as its own root so it neither re-permissions a pre-existing
    # external directory nor follows a symlink where that directory belongs.
    owned_root = (
        settings.data_dir if private.is_within(path.parent, settings.data_dir) else path.parent
    )
    private.secure_mkdir(path.parent, root=owned_root)
    # SQLite must reopen these names itself, so keep the no-follow preparation below from
    # becoming a check/use race in an explicit external directory where another OS user
    # could otherwise unlink and replace an entry. The directory remains at the user's
    # chosen mode (0755 is fine); only group/world write access is unsafe and rejected.
    private.assert_safe_external_writer_parent(path.parent)
    # SQLite cannot be the first process to create or touch any of these predictable names:
    # its open flags follow symlinks and its creation mode is affected by the process umask.
    # Publish absent files exclusively at 0o600, or harden existing current-user files
    # without opening a descriptor that would discard this process's SQLite locks.
    # Existing databases are never truncated.
    private.secure_sqlite_file(path)
    for suffix in _DB_SIDECAR_SUFFIXES:
        private.secure_sqlite_file(path.with_name(path.name + suffix))
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
    # The database and its WAL sidecars hold the same private state as the rest of the data
    # tree. The parent directory is `0o700`, which is what actually keeps other users out;
    # tightening the files as well is defence in depth for a database placed, by an explicit
    # `LYRA_DB_PATH`, somewhere the directory contract does not otherwise reach.
    private.secure_sqlite_file(path, create=False)
    for suffix in _DB_SIDECAR_SUFFIXES:
        private.secure_sqlite_file(path.with_name(path.name + suffix), create=False)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration numbered above `pragma user_version`. Returns the new version."""
    version = conn.execute("pragma user_version").fetchone()[0]
    _refuse_future_schema(version)
    pending = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise RuntimeError(f"Migration file is not numbered: {path.name}")
        number = int(match.group(1))
        if number > version:
            pending.append((number, path))

    if pending and version > 0:
        _backup_before_migration(conn, version)

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
