"""Packaged backup and journaled full-profile restoration without a source checkout.

The native owner stops the backend first. A restored profile replaces a populated
profile only after explicit native confirmation; the preceding profile is retained.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import traceback
import uuid
from pathlib import Path

from backend import desktop_backup_archive as archive
from backend.config import settings
from backend.storage import private
from backend.storage.database import assert_schema_compatible


class BackupError(RuntimeError):
    """An actionable packaged backup failure."""


def _data_root() -> Path:
    root = settings.data_dir.absolute()
    if not root.is_absolute() or root.is_symlink():
        raise BackupError("Lyra's data directory is unavailable.")
    if settings.db_path.absolute() != root / "lyra.db":
        raise BackupError("Desktop backup requires the database inside Lyra's data folder.")
    return root


def _journal(root: Path) -> Path:
    return root.parent / f".{root.name}.restore-journal.json"


def _paths(root: Path, token: str) -> tuple[Path, Path]:
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise BackupError("The restore recovery record is invalid.")
    return (
        root.parent / f".{root.name}.restore-{token}",
        root.parent / f"{root.name}.before-restore-{token}",
    )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_database(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BackupError("The backup does not contain a regular Lyra database.")
    assert_schema_compatible(path)
    try:
        with contextlib.closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("pragma quick_check").fetchone()
            if result != ("ok",):
                raise BackupError("The backup database failed its integrity check.")
            tables = {row[0] for row in connection.execute("select name from sqlite_master")}
            if not {"classes", "documents"}.issubset(tables):
                raise BackupError("The archive does not contain a Lyra database.")
    except sqlite3.Error as exc:
        raise BackupError("The backup database could not be verified.") from exc


def _save_journal(root: Path, token: str, phase: str) -> None:
    private.publish_private_text(
        _journal(root), json.dumps({"version": 1, "token": token, "phase": phase})
    )
    descriptor = os.open(_journal(root), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(root.parent)


def recover_restore() -> None:
    """Finish or roll back an interrupted rename before any database is opened."""
    root = settings.data_dir.absolute()
    journal = _journal(root)
    # An older app must never move a newer live profile, even if a stale recovery
    # record exists. A missing live root still permits crash recovery from staging.
    assert_schema_compatible(settings.db_path)
    if not journal.exists():
        return
    root = _data_root()
    try:
        record = json.loads(private.read_private_text(journal))
        if not isinstance(record, dict) or record.get("version") != 1:
            raise BackupError("The restore recovery record is invalid.")
        token = record.get("token")
        if not isinstance(token, str):
            raise BackupError("The restore recovery record is invalid.")
        stage, previous = _paths(root, token)
        if any(path.is_symlink() for path in (root, stage, previous)):
            raise BackupError("The restore recovery folders are unsafe.")
        if record.get("phase") == "rolling_back":
            _rollback_restore(root, token, previous)
            return
        if record.get("phase") not in {"prepared", "backed_up", "published"}:
            raise BackupError("The restore recovery phase is invalid.")
        if stage.exists():
            _verify_database(stage / "lyra.db")
            if root.exists() and not previous.exists():
                root.rename(previous)
                _sync_directory(root.parent)
                _save_journal(root, token, "backed_up")
            if root.exists():
                raise BackupError("Restore recovery found conflicting profile folders.")
            stage.rename(root)
            _sync_directory(root.parent)
            _save_journal(root, token, "published")
        if not previous.exists() and record.get("phase") != "published":
            raise BackupError("The staged restore is missing; the current profile is unchanged.")
        _verify_database(root / "lyra.db")
        journal.unlink()
        _sync_directory(root.parent)
    except Exception:
        # Retain the failed restored profile too. Never delete the last known good
        # profile merely because publication or verification failed.
        if "previous" in locals() and previous.exists() and not previous.is_symlink():
            _save_journal(root, token, "rolling_back")
            _rollback_restore(root, token, previous)
        raise


def _rollback_restore(root: Path, token: str, previous: Path) -> None:
    if previous.exists():
        if root.exists():
            failed = root.parent / f".{root.name}.failed-restore-{token}"
            if failed.exists():
                raise BackupError("Restore needs recovery; the prior profile is retained.")
            root.rename(failed)
            _sync_directory(root.parent)
        previous.rename(root)
        _sync_directory(root.parent)
    # The previous rename may have completed before a power loss. The durable
    # rolling_back phase distinguishes that valid state from a missing stage.
    _verify_database(root / "lyra.db")
    _journal(root).unlink(missing_ok=True)
    _sync_directory(root.parent)


def create_backup(target: Path) -> dict[str, str]:
    root = _data_root()
    recover_restore()
    target = target.expanduser().absolute()
    if archive.path_is_within(target, root):
        raise BackupError("Choose a new backup filename outside Lyra's data folder.")
    _verify_database(root / "lyra.db")
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    partial = f".{target.name}.{uuid.uuid4().hex}.partial"
    owned_partial = False
    try:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BackupError("Choose a new backup filename; existing files are never replaced.")
        with tempfile.TemporaryDirectory(prefix="lyra-backup-") as temporary:
            stage = Path(temporary)
            archive.stage_backup_tree(stage, root, root / "lyra.db")
            descriptor = os.open(
                partial,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            owned_partial = True
            with os.fdopen(descriptor, "w+b") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle:
                    bundle.add(stage / archive.BACKUP_MANIFEST, arcname=archive.BACKUP_MANIFEST)
                    bundle.add(
                        stage / archive.BACKUP_DATA_PREFIX, arcname=archive.BACKUP_DATA_PREFIX
                    )
                raw.flush()
                os.fsync(raw.fileno())
                raw.seek(0)
                with tarfile.open(fileobj=raw, mode="r:gz") as bundle:
                    manifest = archive.read_backup_manifest(bundle)
                    archive.validate_backup_members(bundle, manifest)
                os.link(partial, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                # The verified archive is already fully published. A directory fsync
                # error cannot truthfully be reported as no saved backup.
                with contextlib.suppress(OSError):
                    os.fsync(parent_fd)
    finally:
        if owned_partial:
            os.unlink(partial, dir_fd=parent_fd)
        os.close(parent_fd)
    return {"status": "created", "label": target.name}


def _settings_snapshot(path: Path) -> dict[str, object] | None:
    with contextlib.closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if not connection.execute(
            "select 1 from sqlite_master where type='table' and name='settings'"
        ).fetchone():
            return None
        row = connection.execute("select * from settings where id=1").fetchone()
        return dict(row) if row else None


def _preserve_current_connections(root: Path, stage: Path) -> None:
    """Restore learning data, while current connection identity and Forget remain authoritative."""
    current = _settings_snapshot(root / "lyra.db")
    restored = _settings_snapshot(stage / "lyra.db")
    transferable = current is None and restored is None
    if current is not None and restored is not None:
        derived = {
            "id",
            "probe_revision",
            "tools_supported",
            "tools_message",
            "vision_supported",
            "vision_message",
        }
        transferred = {
            key: value for key, value in current.items() if key in restored and key not in derived
        }
        with contextlib.closing(sqlite3.connect(stage / "lyra.db")) as connection, connection:
            if transferred:
                assignments = ", ".join(
                    '"' + key.replace('"', '""') + '" = ?' for key in transferred
                )
                connection.execute(
                    f"update settings set {assignments} where id=1",  # noqa: S608
                    tuple(transferred.values()),
                )
        actual = _settings_snapshot(stage / "lyra.db") or {}
        if any(actual.get(key) != value for key, value in transferred.items()):
            raise BackupError("The restored connection settings could not be verified.")
        transferable = all(
            key in restored
            for key in ("endpoint_url", "tutor_credential_id", "legacy_credential_endpoint")
        )
    # Delete *all* archived authority before copying the live state. In particular,
    # an absent live secret plus a deletion tombstone must never revive an archive key.
    for name in (
        ".api_key",
        ".api_key.authority",
        ".exa_api_key",
        ".exa_api_key.authority",
        ".tutor_credential_generation",
        "credentials",
    ):
        destination = stage / name
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink(missing_ok=True)
        source = root / name
        if source.is_dir() and not source.is_symlink():
            archive.copy_tree_without_symlinks(source, destination, excluded=set())
        elif source.exists():
            payload = private.read_owned_bytes(source, root=root, max_bytes=65536)
            private.publish_private_bytes(destination, payload)
    if not transferable:
        # An older archive may lack the immutable reference fields. It cannot safely
        # associate any legacy current key with a restored endpoint; require reauthentication.
        (stage / ".api_key").unlink(missing_ok=True)
        private.publish_private_text(stage / ".api_key.authority", "deleted")
        private.publish_private_text(stage / ".tutor_credential_generation", uuid.uuid4().hex)
        if restored is not None:
            with contextlib.closing(sqlite3.connect(stage / "lyra.db")) as connection, connection:
                for key in ("tutor_credential_id", "legacy_credential_endpoint"):
                    if key in restored:
                        connection.execute(f"update settings set {key}=null where id=1")  # noqa: S608


def restore_backup(source: Path) -> dict[str, str]:
    root = _data_root()
    recover_restore()
    source = source.expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise BackupError("Choose a regular Lyra backup archive.")
    token = uuid.uuid4().hex
    stage, previous = _paths(root, token)
    private.secure_mkdir(stage, root=root.parent)
    try:
        initial = os.lstat(source)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as raw:
            opened = os.fstat(raw.fileno())
            if not stat.S_ISREG(opened.st_mode) or (initial.st_dev, initial.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise BackupError("The selected backup changed; choose it again.")
            with tarfile.open(fileobj=raw, mode="r:*") as bundle:
                manifest = archive.read_backup_manifest(bundle)
                archive.validate_backup_members(bundle, manifest)
                db = manifest["db"]
                if not isinstance(db, dict) or db.get("member") != "data/lyra.db":
                    raise BackupError("This archive uses a different database layout.")
                total = sum(member.size for member in bundle.getmembers())
                if shutil.disk_usage(root.parent).free < total + 64 * 1024 * 1024:
                    raise BackupError(
                        "There is not enough disk space to restore this backup safely."
                    )
                archive.extract_archive_prefix(bundle, prefix="data", destination=stage)
        _verify_database(stage / "lyra.db")
        from backend.desktop_import import _rewrite_document_paths

        _rewrite_document_paths(stage / "lyra.db", stage_data=stage)
        _preserve_current_connections(root, stage)
        for directory, _, filenames in os.walk(stage):
            for name in filenames:
                with (Path(directory) / name).open("rb") as handle:
                    os.fsync(handle.fileno())
            _sync_directory(Path(directory))
        _save_journal(root, token, "prepared")
        recover_restore()
    except Exception:
        if not _journal(root).exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"status": "restored", "label": previous.name}


def main(operation: str, selected_path: str, *, stream=None) -> int:
    output = stream or sys.stdout
    try:
        if operation == "create":
            result = create_backup(Path(selected_path))
        elif operation == "restore":
            result = restore_backup(Path(selected_path))
        else:
            raise BackupError("Unknown backup operation.")
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)[-4:]
        locations = ", ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in frames)
        print(f"Desktop backup failed: {type(error).__name__} at {locations}", file=sys.stderr)
        # Detailed arbitrary archive/path contents never enter native diagnostics.
        output.write(
            json.dumps(
                {
                    "status": "error",
                    "label": (
                        "Backup operation failed. Reopen Lyra to check recovery before retrying."
                    ),
                }
            )
            + "\n"
        )
        output.flush()
        return 1
    output.write(json.dumps(result) + "\n")
    output.flush()
    return 0
