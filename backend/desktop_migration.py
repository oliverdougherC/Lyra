"""First-launch migration from source-checkout data paths into packaged locations."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend import desktop_paths
from backend.config import settings
from backend.storage import private

_COPY_DIRECTORIES = ("uploads", "text", "models")
_COPY_FILES = (".api_key", ".exa_api_key", ".permissions-hardened")
_SQLITE_SIDECARS = ("", "-wal", "-shm")


@dataclass(frozen=True)
class SourceMigrationResult:
    status: str
    reason: str
    source: Path | None = None
    target: Path | None = None


def migrate_source_data_if_needed() -> SourceMigrationResult:
    if not settings.packaged_mode:
        return SourceMigrationResult(status="skipped", reason="not_packaged")
    source = _source_data_dir()
    if source is None:
        return SourceMigrationResult(status="skipped", reason="no_source_data")
    if _same_location(source, settings.data_dir):
        return SourceMigrationResult(status="skipped", reason="source_is_target", source=source)
    if _target_exists():
        return SourceMigrationResult(status="skipped", reason="target_exists", source=source)
    source_db = _source_db_path(source)
    if not source_db.is_file():
        return SourceMigrationResult(
            status="skipped", reason="source_database_missing", source=source
        )
    private.assert_not_symlink(source, "LYRA_SOURCE_DATA_DIR")
    private.assert_not_symlink(source_db, "LYRA_SOURCE_DB_PATH")
    _verify_sqlite(source_db)

    private.secure_mkdir(settings.data_dir.parent, root=settings.data_dir.parent)
    stage_data = Path(
        tempfile.mkdtemp(
            prefix=f".{settings.data_dir.name}.migrate-",
            dir=str(settings.data_dir.parent),
        )
    )
    stage_db = (
        settings.db_path.with_name(f".{settings.db_path.name}.migrate-{os.getpid()}")
        if not private.is_within(settings.db_path, settings.data_dir)
        else None
    )
    try:
        _stage_payload(source, source_db, stage_data, stage_db)
        staged_db = _staged_db_path(stage_data, stage_db)
        _verify_sqlite(staged_db)
        stage_data.replace(settings.data_dir)
        if stage_db is not None:
            private.secure_mkdir(settings.db_path.parent, root=settings.db_path.parent)
            try:
                stage_db.replace(settings.db_path)
            except OSError as exc:
                settings.data_dir.replace(stage_data)
                raise RuntimeError(
                    "desktop source-data migration could not finalize the database path"
                ) from exc
        return SourceMigrationResult(
            status="migrated",
            reason="migrated",
            source=source,
            target=settings.data_dir,
        )
    except Exception:
        shutil.rmtree(stage_data, ignore_errors=True)
        if stage_db is not None:
            _cleanup_db_family(stage_db)
        raise


def _source_data_dir() -> Path | None:
    if settings.source_data_dir is not None:
        return settings.source_data_dir.expanduser()
    resource_root = settings.resource_root
    if resource_root is None:
        raise RuntimeError("LYRA_RESOURCE_ROOT is not configured.")
    for candidate in desktop_paths.source_data_candidates(resource_root):
        if candidate.exists():
            return candidate
    return None


def _source_db_path(source_data_dir: Path) -> Path:
    if settings.source_db_path is not None:
        return settings.source_db_path.expanduser()
    return source_data_dir / "lyra.db"


def _same_location(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def _target_exists() -> bool:
    if settings.data_dir.exists():
        return True
    return settings.db_path.exists()


def _stage_payload(source: Path, source_db: Path, stage_data: Path, stage_db: Path | None) -> None:
    for name in _COPY_DIRECTORIES:
        current = source / name
        if current.exists():
            _copy_tree_without_links(current, stage_data / name)
    for name in _COPY_FILES:
        current = source / name
        if current.exists():
            _copy_regular_file(current, stage_data / name)
    target_db = _staged_db_path(stage_data, stage_db)
    _copy_db_family(source_db, target_db)


def _staged_db_path(stage_data: Path, stage_db: Path | None) -> Path:
    if stage_db is not None:
        return stage_db
    return stage_data / settings.db_path.relative_to(settings.data_dir)


def _copy_tree_without_links(source: Path, destination: Path) -> None:
    info = source.lstat()
    if not source.is_dir() or stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"desktop migration refused non-directory source tree: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for current in sorted(source.rglob("*")):
        relative = current.relative_to(source)
        target = destination / relative
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"desktop migration refused symlinked source entry: {current}")
        if current.is_dir():
            target.mkdir(exist_ok=False)
            continue
        if not current.is_file():
            raise RuntimeError(f"desktop migration refused non-regular source entry: {current}")
        shutil.copy2(current, target)


def _copy_regular_file(source: Path, destination: Path) -> None:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not source.is_file():
        raise RuntimeError(f"desktop migration refused non-regular source entry: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_db_family(source_db: Path, staged_db: Path) -> None:
    staged_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in _SQLITE_SIDECARS:
        current = source_db.with_name(source_db.name + suffix)
        if not current.exists():
            continue
        _copy_regular_file(current, staged_db.with_name(staged_db.name + suffix))


def _cleanup_db_family(path: Path) -> None:
    for suffix in _SQLITE_SIDECARS:
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _verify_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True) as conn:
            result = conn.execute("pragma integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("desktop source-data migration database failed integrity_check")
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"desktop source-data migration could not verify the database: {exc}"
        ) from exc
