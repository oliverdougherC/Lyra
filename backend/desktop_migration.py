"""Private file-copy helpers shared by the explicit desktop import pipeline.

Old checkout data is never migrated automatically. The user-driven importer owns source
selection, SQLite snapshotting, staging, verification, publication, and recovery.
"""

from __future__ import annotations

import shutil
import sqlite3
import stat
from pathlib import Path

SQLITE_SIDECARS = ("", "-wal", "-shm")


def copy_tree_without_links(source: Path, destination: Path) -> None:
    info = source.lstat()
    if not source.is_dir() or stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"desktop import refused non-directory source tree: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for current in sorted(source.rglob("*")):
        relative = current.relative_to(source)
        target = destination / relative
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"desktop import refused symlinked source entry: {current}")
        if current.is_dir():
            target.mkdir(exist_ok=False)
            continue
        if not current.is_file():
            raise RuntimeError(f"desktop import refused non-regular source entry: {current}")
        shutil.copy2(current, target)


def copy_regular_file(source: Path, destination: Path) -> None:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not source.is_file():
        raise RuntimeError(f"desktop import refused non-regular source entry: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def verify_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True) as conn:
            result = conn.execute("pragma integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("desktop import database failed integrity_check")
    except sqlite3.Error as exc:
        raise RuntimeError(f"desktop import could not verify the database: {exc}") from exc
