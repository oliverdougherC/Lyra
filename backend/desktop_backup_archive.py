"""Shared standard-library backup archive format and validation (launcher and frozen app)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath

BACKUP_VERSION = 1


BACKUP_MANIFEST = "manifest.json"


BACKUP_DATA_PREFIX = "data"


BACKUP_EXTERNAL_DB = "database/lyra.db"


BACKUP_MAX_MEMBERS = 10_000


BACKUP_MAX_MEMBER_BYTES = 16 * 1024 * 1024 * 1024


BACKUP_MAX_TOTAL_BYTES = 128 * 1024 * 1024 * 1024


class LauncherError(RuntimeError):
    """An actionable setup or lifecycle failure."""


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    return (path.with_name(f"{path.name}-shm"), path.with_name(f"{path.name}-wal"))


def copy_tree_without_symlinks(source: Path, destination: Path, *, excluded: set[Path]) -> None:
    """Copy one directory tree without following symlinks or special files."""

    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if entry in excluded:
            continue
        if entry.is_symlink():
            raise LauncherError(f"backup refused symlink entry: {entry}")
        target = destination / entry.name
        if entry.is_dir():
            copy_tree_without_symlinks(entry, target, excluded=excluded)
            shutil.copystat(entry, target, follow_symlinks=False)
            continue
        if not entry.is_file():
            raise LauncherError(f"backup refused non-regular entry: {entry}")
        shutil.copy2(entry, target, follow_symlinks=False)


def snapshot_sqlite_database(source: Path, destination: Path) -> None:
    """Create a consistent SQLite file snapshot and fail if another writer is active."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    try:
        source_conn = sqlite3.connect(str(source), timeout=0, isolation_level=None)
    except sqlite3.Error as exc:
        raise LauncherError(f"could not open the source database {source}: {exc}") from exc

    try:
        source_conn.execute("pragma busy_timeout = 0")
        checkpoint = source_conn.execute("pragma wal_checkpoint(truncate)").fetchone()
        if not checkpoint or checkpoint[0] != 0:
            raise LauncherError(
                "Lyra's database is still busy. Stop any running Lyra process and retry."
            )
        source_conn.execute("begin immediate")
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.close(descriptor)
        # A commit may land after checkpoint and before BEGIN IMMEDIATE. SQLite's
        # backup API includes that committed WAL; copying only the main file cannot.
        # Read through a second connection while the owner above prevents new writers.
        with (
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as reader,
            sqlite3.connect(str(destination)) as snapshot,
        ):
            reader.backup(snapshot)
            result = snapshot.execute("pragma quick_check").fetchone()
            if not result or result[0] != "ok":
                raise LauncherError("the backup database snapshot failed SQLite quick_check")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "busy" in message or "locked" in message:
            raise LauncherError(
                "Lyra's database is still busy. Stop any running Lyra process and retry."
            ) from exc
        raise LauncherError(f"could not snapshot the database: {exc}") from exc
    except sqlite3.Error as exc:
        raise LauncherError(f"could not snapshot the database: {exc}") from exc
    finally:
        with suppress(sqlite3.Error):
            source_conn.rollback()
        source_conn.close()


def staged_backup_manifest(data_dir: Path, db_path: Path) -> dict[str, object]:
    inside_data_dir = path_is_within(db_path, data_dir)
    return {
        "version": BACKUP_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data_dir": BACKUP_DATA_PREFIX,
        "db": {
            "inside_data_dir": inside_data_dir,
            "relative_path": db_path.relative_to(data_dir).as_posix() if inside_data_dir else None,
            "member": (
                f"{BACKUP_DATA_PREFIX}/{db_path.relative_to(data_dir).as_posix()}"
                if inside_data_dir
                else BACKUP_EXTERNAL_DB
            ),
        },
    }


def stage_backup_tree(stage_root: Path, data_dir: Path, db_path: Path) -> dict[str, object]:
    """Build a self-contained backup tree from the configured data paths."""

    manifest = staged_backup_manifest(data_dir, db_path)
    excluded = set()
    if manifest["db"]["inside_data_dir"]:  # type: ignore[index]
        excluded.add(db_path)
        excluded.update(sqlite_sidecars(db_path))
    stage_data = stage_root / BACKUP_DATA_PREFIX
    copy_tree_without_symlinks(data_dir, stage_data, excluded=excluded)

    db_member = Path(str(manifest["db"]["member"]))  # type: ignore[index]
    snapshot_target = stage_root / db_member
    snapshot_sqlite_database(db_path, snapshot_target)
    (stage_root / BACKUP_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def safe_archive_member(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    normalized = member.as_posix()
    if not normalized or normalized == "." or member.is_absolute() or ".." in member.parts:
        raise LauncherError(f"backup archive contains an unsafe path: {name}")
    return member


def read_backup_manifest(bundle: tarfile.TarFile) -> dict[str, object]:
    manifest_member = None
    for index, member in enumerate(bundle):
        if index >= BACKUP_MAX_MEMBERS:
            raise LauncherError("backup archive contains too many entries")
        if member.name == BACKUP_MANIFEST:
            manifest_member = member
            break
    if manifest_member is None:
        raise LauncherError("backup archive does not contain a manifest.json")
    if manifest_member.size > 65536:
        raise LauncherError("backup archive manifest is too large")
    if not manifest_member.isfile():
        raise LauncherError("backup archive manifest is not a regular file")
    manifest_file = bundle.extractfile(manifest_member)
    if manifest_file is None:
        raise LauncherError("backup archive manifest could not be read")
    try:
        manifest = json.loads(manifest_file.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError(f"backup archive manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != BACKUP_VERSION:
        raise LauncherError("backup archive manifest uses an unsupported format")
    db = manifest.get("db")
    if not isinstance(db, dict) or not isinstance(db.get("inside_data_dir"), bool):
        raise LauncherError("backup archive manifest is missing database metadata")
    member = db.get("member")
    if not isinstance(member, str):
        raise LauncherError("backup archive manifest is missing the database member path")
    safe_archive_member(member)
    relative = db.get("relative_path")
    if relative is not None and not isinstance(relative, str):
        raise LauncherError("backup archive manifest has an invalid database relative path")
    inside_data_dir = bool(db["inside_data_dir"])
    if inside_data_dir:
        if not isinstance(relative, str):
            raise LauncherError("backup archive manifest is missing the database relative path")
        safe_relative = safe_archive_member(relative)
        expected_member = PurePosixPath(BACKUP_DATA_PREFIX) / safe_relative
        if PurePosixPath(member) != expected_member:
            raise LauncherError("backup archive manifest has inconsistent database paths")
    elif relative is not None or member != BACKUP_EXTERNAL_DB:
        raise LauncherError("backup archive manifest has inconsistent external database metadata")
    return manifest


def validate_backup_members(
    bundle: tarfile.TarFile,
    manifest: dict[str, object],
    *,
    max_members: int = BACKUP_MAX_MEMBERS,
    max_member_bytes: int = BACKUP_MAX_MEMBER_BYTES,
    max_total_bytes: int = BACKUP_MAX_TOTAL_BYTES,
) -> None:
    """Refuse unsafe or unexpected archive members before any restore write begins."""

    db = manifest.get("db")
    if not isinstance(db, dict):
        raise LauncherError("backup archive manifest is missing database metadata")
    inside_data_dir = bool(db.get("inside_data_dir"))
    db_member = str(db.get("member"))
    expected_db_member = safe_archive_member(db_member)

    seen_members: set[str] = set()
    saw_manifest = False
    saw_data = False
    saw_database = False
    total_bytes = 0
    for member in bundle:
        safe_name = safe_archive_member(member.name)
        normalized_name = safe_name.as_posix()
        if normalized_name in seen_members:
            raise LauncherError(f"backup archive contains a duplicate entry: {member.name}")
        seen_members.add(normalized_name)
        if len(seen_members) > max_members:
            raise LauncherError("backup archive contains too many entries")
        if safe_name == PurePosixPath(BACKUP_MANIFEST):
            if not member.isfile():
                raise LauncherError("backup archive manifest is not a regular file")
            saw_manifest = True
            continue
        if member.issym() or member.islnk():
            raise LauncherError(f"backup archive contains a link entry: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise LauncherError(f"backup archive contains an unsupported entry: {member.name}")
        if member.isfile():
            if member.size > max_member_bytes:
                raise LauncherError(f"backup archive entry is too large: {member.name}")
            total_bytes += member.size
            if total_bytes > max_total_bytes:
                raise LauncherError("backup archive is too large to restore safely")
        if safe_name.parts and safe_name.parts[0] == BACKUP_DATA_PREFIX:
            saw_data = True
            if inside_data_dir and safe_name == expected_db_member:
                saw_database = True
            continue
        if not inside_data_dir and safe_name == expected_db_member:
            saw_database = True
            continue
        raise LauncherError(f"backup archive contains an unexpected entry: {member.name}")

    if not saw_manifest:
        raise LauncherError("backup archive does not contain a manifest.json")
    if not saw_data:
        raise LauncherError("backup archive does not contain any data/ payload")
    if not saw_database:
        if inside_data_dir:
            raise LauncherError("backup archive is missing its database file inside data/")
        raise LauncherError("backup archive is missing its external database file")


def private_restore_mkdir(path: Path, *, root: Path) -> None:
    """Create every restore directory component explicitly owner-only.

    `Path.mkdir(parents=True, mode=...)` applies `mode` only to the leaf. An archive is not
    required to carry explicit directory members, so relying on it would let implicit
    intermediate directories inherit a permissive umask and later be published broad.
    """
    relative = path.relative_to(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    current = root
    for part in relative.parts:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        os.chmod(current, 0o700)


def extract_archive_prefix(
    bundle: tarfile.TarFile,
    *,
    prefix: str,
    destination: Path,
) -> None:
    """Extract one archive subtree as regular files only."""

    normalized_prefix = prefix.rstrip("/") + "/"
    extracted = False
    private_restore_mkdir(destination, root=destination)
    for member in bundle.getmembers():
        safe_name = safe_archive_member(member.name)
        if str(safe_name) == BACKUP_MANIFEST:
            continue
        if not str(safe_name).startswith(normalized_prefix):
            continue
        if member.issym() or member.islnk():
            raise LauncherError(f"backup archive contains a link entry: {member.name}")
        relative = PurePosixPath(*safe_name.parts[len(PurePosixPath(prefix).parts) :])
        if not relative.parts:
            continue
        target = destination.joinpath(*relative.parts)
        if member.isdir():
            private_restore_mkdir(target, root=destination)
            extracted = True
            continue
        if not member.isfile():
            raise LauncherError(f"backup archive contains an unsupported entry: {member.name}")
        private_restore_mkdir(target.parent, root=destination)
        if target.exists():
            raise LauncherError(f"restore would overwrite an existing file: {target}")
        payload = bundle.extractfile(member)
        if payload is None:
            raise LauncherError(f"backup archive entry could not be read: {member.name}")
        mode = 0o700 if relative.parts[0] == "models" and member.mode & 0o111 else 0o600
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, mode)
        owns_descriptor = False
        try:
            if os.name == "posix":
                os.fchmod(descriptor, mode)
            else:
                with suppress(OSError):
                    os.chmod(target, mode)
            with os.fdopen(descriptor, "wb") as handle:
                owns_descriptor = True
                shutil.copyfileobj(payload, handle)
        finally:
            if not owns_descriptor:
                os.close(descriptor)
        extracted = True
    if not extracted:
        raise LauncherError(f"backup archive does not contain the expected {prefix}/ payload")


def extract_archive_file(bundle: tarfile.TarFile, *, member_name: str, destination: Path) -> None:
    """Extract one regular archive member to one explicit file path."""

    safe_archive_member(member_name)
    try:
        member = bundle.getmember(member_name)
    except KeyError as exc:
        raise LauncherError(f"backup archive is missing {member_name}") from exc
    if member.issym() or member.islnk() or not member.isfile():
        raise LauncherError(f"backup archive entry is not a regular file: {member_name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise LauncherError(f"restore would overwrite an existing file: {destination}")
    payload = bundle.extractfile(member)
    if payload is None:
        raise LauncherError(f"backup archive entry could not be read: {member_name}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    owns_descriptor = False
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        else:
            with suppress(OSError):
                os.chmod(destination, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            owns_descriptor = True
            shutil.copyfileobj(payload, handle)
    finally:
        if not owns_descriptor:
            os.close(descriptor)
