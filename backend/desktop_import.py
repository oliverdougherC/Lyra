"""Safe packaged-data import from a user-picked Lyra checkout or data directory."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from backend import desktop_paths
from backend.api.routes_documents import _safe_filename
from backend.config import settings
from backend.core.app_settings import get_settings_row
from backend.core.errors import ConflictError, LyraError
from backend.desktop_migration import (
    SQLITE_SIDECARS,
    copy_regular_file,
    copy_tree_without_links,
    verify_sqlite,
)
from backend.storage import private
from backend.storage.database import MIGRATIONS_DIR, connect, migrate

STATE_VERSION = 2
MANIFEST_VERSION = 2
PUBLISH_RECOVERY_VERSION = 1
STATE_FILE = ".desktop-import-state.json"
STAGE_ROOT = ".desktop-import-stage"
SELECTIONS_DIR = ".desktop-import-selections"
MANIFEST_FILE = "manifest.json"
PUBLISH_RECOVERY_FILE = ".desktop-import-publish-recovery.json"
PREVIEW_SAMPLE_LIMIT = 8
CONFLICT_SAMPLE_LIMIT = 8
HASH_CHUNK_BYTES = 1024 * 1024
_RUNNING_STATES = {"queued", "running", "cancel_requested"}
_RESUMABLE_STATES = _RUNNING_STATES | {"cancelled", "failed"}
_SOURCE_IMPORT_DIRECTORIES = ("uploads", "text")
_PROFILE_PRESERVE_DIRECTORIES = ("models",)
_PROFILE_PRESERVE_FILES = (".api_key", ".exa_api_key", ".permissions-hardened")
_DESTINATION_AUXILIARY_PREFIXES = ("chunk_embeddings", "chunks_fts")
_STAGED_STATUS = "staged"
_AWAITING_PUBLISH_PHASE = "awaiting_publish"


@dataclass(frozen=True)
class ImportAssetSummary:
    selected_models: int
    selected_model_bytes: int
    selected_caches: int
    selected_cache_bytes: int
    preserved_models: int
    preserved_model_bytes: int
    preserved_caches: int
    preserved_cache_bytes: int


@dataclass(frozen=True)
class ImportPreview:
    source_name: str
    source_kind: str
    source_data_dir: Path
    class_count: int
    document_count: int
    total_entries: int
    total_bytes: int
    sample_entries: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: int | None = None
    database_identity: str | None = None
    conflicts: tuple[str, ...] = ()
    asset_summary: ImportAssetSummary | None = None
    old_runtime_active: bool | None = None
    source_lock: str | None = None


@dataclass(frozen=True)
class ImportStatus:
    available: bool
    destination_ready: bool
    status: str
    phase: str | None
    message: str | None
    source_name: str | None
    copied_entries: int
    total_entries: int
    copied_bytes: int
    total_bytes: int
    cancel_requested: bool
    can_resume: bool
    requires_restart: bool
    preview: ImportPreview | None
    schema_version: int | None = None
    database_identity: str | None = None
    conflicts: tuple[str, ...] = ()
    asset_summary: ImportAssetSummary | None = None
    old_runtime_active: bool | None = None
    source_lock: str | None = None


@dataclass(frozen=True)
class _SnapshotEntry:
    source: Path
    relative: str
    size: int


class _ImportCancelledError(Exception):
    pass


class DesktopImportManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def preview(self, selection_token: str) -> ImportPreview:
        _require_packaged_mode()
        return _build_preview(selection_token)

    def status(self) -> ImportStatus:
        with self._lock:
            state = self._load_state()
            self._resume_locked(state)
            return self._public_status(state)

    def start(self, selection_token: str, operation_id: str) -> ImportStatus:
        _require_packaged_mode()
        _assert_destination_ready()
        token = operation_id.strip()
        if not token:
            raise LyraError("The import request is missing its operation id.")
        preview = _build_preview(selection_token)
        with self._lock:
            state = self._load_state()
            if state is not None:
                status = str(state.get("status") or "idle")
                active_source = str(state.get("source_data_dir") or "")
                if status in _RUNNING_STATES and active_source != str(preview.source_data_dir):
                    raise ConflictError("Another import is already in progress.")
                if status in {_STAGED_STATUS, "completed"}:
                    return self._public_status(state)
                if status == "completed":
                    return self._public_status(state)
                if active_source != str(preview.source_data_dir):
                    self._clear_staging_locked()
                    state = None
            if state is None:
                state = {
                    "version": STATE_VERSION,
                    "operation_id": token,
                    "status": "queued",
                    "phase": "preparing",
                    "message": "Preparing the selected Lyra data for import.",
                    "selection_token": selection_token.strip(),
                    "source_name": preview.source_name,
                    "source_kind": preview.source_kind,
                    "source_data_dir": str(preview.source_data_dir),
                    "copied_entries": 0,
                    "total_entries": preview.total_entries,
                    "copied_bytes": 0,
                    "total_bytes": preview.total_bytes,
                    "cancel_requested": False,
                    "requires_restart": False,
                    "preview": _preview_payload(preview),
                }
            elif str(state.get("status") or "") in _RESUMABLE_STATES:
                state.update(
                    {
                        "operation_id": token,
                        "status": "queued",
                        "phase": "preparing",
                        "message": "Resuming the selected Lyra import.",
                        "selection_token": selection_token.strip(),
                        "cancel_requested": False,
                        "preview": _preview_payload(preview),
                    }
                )
            self._save_state_locked(state)
            self._start_worker_locked()
            return self._public_status(state)

    def cancel(self) -> ImportStatus:
        with self._lock:
            state = self._load_state()
            if state is None:
                return self._public_status(None)
            if str(state.get("status") or "") in {
                "completed",
                "cancelled",
                "failed",
                _STAGED_STATUS,
            }:
                return self._public_status(state)
            state["cancel_requested"] = True
            state["status"] = "cancel_requested"
            state["phase"] = "cancelling"
            state["message"] = "Cancelling after the current step."
            self._save_state_locked(state)
            return self._public_status(state)

    def _resume_locked(self, state: dict[str, object] | None) -> None:
        if state is None:
            return
        if str(state.get("status") or "") not in _RUNNING_STATES:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._start_worker_locked()

    def _start_worker_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        worker = threading.Thread(target=self._run_worker, name="lyra-desktop-import", daemon=True)
        self._thread = worker
        worker.start()

    def _run_worker(self) -> None:
        try:
            state = self._load_state()
            if state is None:
                return
            source_data_dir = Path(str(state["source_data_dir"]))
            preview = _build_preview(str(state.get("selection_token") or ""))
            self._patch_state(
                status="running",
                phase="locking_source",
                message="Checking the source database and locking it for a consistent snapshot.",
                total_entries=preview.total_entries,
                total_bytes=preview.total_bytes,
                preview=_preview_payload(preview),
            )
            _assert_destination_ready()
            private.secure_mkdir(_stage_root_path(), root=settings.data_dir.parent)
            manifest = self._stage_locked_snapshot(
                source_data_dir,
                source_name=preview.source_name,
                source_kind=preview.source_kind,
            )
            self._patch_state(
                status="running",
                phase="verifying",
                message="Verifying the staged import before publication.",
            )
            _verify_staged_import(_stage_data_path(), _stage_db_path())
            _assert_destination_ready()
            self._patch_state(
                status=_STAGED_STATUS,
                phase=_AWAITING_PUBLISH_PHASE,
                message="Import staged. Quit and relaunch Lyra to publish it safely.",
                copied_entries=int(manifest["total_entries"]),
                total_entries=int(manifest["total_entries"]),
                copied_bytes=int(manifest["total_bytes"]),
                total_bytes=int(manifest["total_bytes"]),
                cancel_requested=False,
                requires_restart=True,
            )
        except _ImportCancelledError:
            self._patch_state(
                status="cancelled",
                phase="cancelled",
                message="Import paused. Resume it when you are ready.",
            )
        except Exception as exc:  # pragma: no cover - exercised via route tests.
            message = str(exc).strip() or "The selected data could not be imported."
            self._patch_state(status="failed", phase="failed", message=message)

    def _stage_locked_snapshot(
        self, source_data_dir: Path, *, source_name: str, source_kind: str
    ) -> dict[str, object]:
        source_db = source_data_dir / "lyra.db"
        private.assert_not_symlink(source_data_dir, "the selected import folder")
        private.assert_not_symlink(source_db, "the selected import database")
        try:
            lock_conn = sqlite3.connect(str(source_db), timeout=0, isolation_level=None)
        except sqlite3.Error as exc:
            raise RuntimeError("The selected Lyra database could not be opened.") from exc

        source_conn: sqlite3.Connection | None = None
        try:
            lock_conn.execute("pragma busy_timeout = 0")
            try:
                lock_conn.execute("begin immediate")
            except sqlite3.OperationalError as exc:
                if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                    raise LyraError(
                        "The selected Lyra data is still busy. Close the old app and try again."
                    ) from exc
                raise
            source_conn = sqlite3.connect(str(source_db), timeout=0, isolation_level=None)
            source_conn.execute("pragma busy_timeout = 0")
            snapshot = _snapshot_entries(source_data_dir)
            metadata = _source_metadata(source_conn)
            existing_manifest = _read_stage_manifest()
            if existing_manifest is None and _stage_root_path().exists():
                shutil.rmtree(_stage_root_path(), ignore_errors=True)
            manifest = _prepare_stage_manifest(
                existing_manifest,
                source_data_dir,
                source_db,
                snapshot,
                source_name=source_name,
                source_kind=source_kind,
                metadata=metadata,
            )
            _copy_profile_into_stage()
            _write_stage_manifest(manifest)
            copied_entries, copied_bytes = _staged_progress(snapshot, manifest)
            self._patch_state(
                status="running",
                phase="copying_files",
                message="Copying imported files into a private staging area.",
                copied_entries=copied_entries,
                copied_bytes=copied_bytes,
                total_entries=int(manifest["total_entries"]),
                total_bytes=int(manifest["total_bytes"]),
            )
            manifest_entries = _manifest_entries_by_relative(manifest)
            for entry in snapshot:
                self._raise_if_cancelled()
                target = _stage_data_path() / entry.relative
                if _staged_match(manifest_entries.get(entry.relative), target):
                    continue
                copy_regular_file(entry.source, target)
                copied_entries += 1
                copied_bytes += entry.size
                self._patch_state(
                    copied_entries=copied_entries,
                    copied_bytes=copied_bytes,
                    message=f"Copied {entry.relative}.",
                )
            self._raise_if_cancelled()
            self._patch_state(
                phase="copying_database",
                message="Creating a verified SQLite snapshot from the selected data.",
            )
            if not _staged_database_matches(manifest):
                _backup_database(source_conn, _stage_db_path())
        finally:
            with contextlib.suppress(sqlite3.Error):
                lock_conn.rollback()
            lock_conn.close()
            if source_conn is not None:
                source_conn.close()

        if not _staged_database_matches(manifest):
            _rewrite_document_paths(_stage_db_path())
            _merge_destination_profile(_stage_db_path())
            with connect(_stage_db_path()) as staged:
                migrate(staged)
                issues = staged.execute("pragma foreign_key_check").fetchall()
                if issues:
                    raise RuntimeError("The imported database has broken references.")
                staged.commit()
            _truncate_sqlite_wal(_stage_db_path())
            verify_sqlite(_stage_db_path())
            manifest["staged_database"] = _staged_database_record(_stage_db_path())
            _write_stage_manifest(manifest)
        return manifest

    def _raise_if_cancelled(self) -> None:
        with self._lock:
            state = self._load_state()
            if state is not None and bool(state.get("cancel_requested")):
                raise _ImportCancelledError

    def _state_path(self) -> Path:
        return settings.data_dir.parent / STATE_FILE

    def _load_state(self) -> dict[str, object] | None:
        path = self._state_path()
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or int(payload.get("version") or 0) != STATE_VERSION:
            return None
        return payload

    def _save_state_locked(self, state: dict[str, object]) -> None:
        payload = dict(state)
        payload["updated_at"] = _utc_now()
        private.secure_mkdir(self._state_path().parent, root=settings.data_dir.parent)
        private.write_private_text(
            self._state_path(),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _patch_state(self, **updates: object) -> None:
        with self._lock:
            state = self._load_state()
            if state is None:
                return
            state.update(updates)
            self._save_state_locked(state)

    def _clear_staging_locked(self) -> None:
        shutil.rmtree(_stage_root_path(), ignore_errors=True)
        shutil.rmtree(_publish_recovery_root_path(), ignore_errors=True)
        _publish_recovery_record_path().unlink(missing_ok=True)
        self._state_path().unlink(missing_ok=True)

    def _public_status(self, state: dict[str, object] | None) -> ImportStatus:
        available = settings.packaged_mode
        conflicts = _destination_conflicts()
        destination_ready = not conflicts
        if state is None:
            return ImportStatus(
                available=available,
                destination_ready=destination_ready,
                status="idle",
                phase=None,
                message=(
                    None
                    if available and destination_ready
                    else "Import is only available before this installation has its own data."
                ),
                source_name=None,
                copied_entries=0,
                total_entries=0,
                copied_bytes=0,
                total_bytes=0,
                cancel_requested=False,
                can_resume=False,
                requires_restart=False,
                preview=None,
                conflicts=conflicts,
            )
        preview_data = state.get("preview")
        preview = _preview_from_payload(preview_data) if isinstance(preview_data, dict) else None
        status = str(state.get("status") or "idle")
        schema_version = preview.schema_version if preview is not None else None
        database_identity = preview.database_identity if preview is not None else None
        asset_summary = preview.asset_summary if preview is not None else None
        old_runtime_active = preview.old_runtime_active if preview is not None else None
        source_lock = preview.source_lock if preview is not None else None
        return ImportStatus(
            available=available,
            destination_ready=destination_ready,
            status=status,
            phase=str(state["phase"]) if state.get("phase") else None,
            message=str(state["message"]) if state.get("message") else None,
            source_name=str(state["source_name"]) if state.get("source_name") else None,
            copied_entries=int(state.get("copied_entries") or 0),
            total_entries=int(state.get("total_entries") or 0),
            copied_bytes=int(state.get("copied_bytes") or 0),
            total_bytes=int(state.get("total_bytes") or 0),
            cancel_requested=bool(state.get("cancel_requested")),
            can_resume=status in {"cancelled", "failed"},
            requires_restart=bool(state.get("requires_restart")),
            preview=preview,
            schema_version=schema_version,
            database_identity=database_identity,
            conflicts=conflicts,
            asset_summary=asset_summary,
            old_runtime_active=old_runtime_active,
            source_lock=source_lock,
        )


desktop_import_manager = DesktopImportManager()


def _preview_payload(preview: ImportPreview) -> dict[str, object]:
    payload = asdict(preview)
    payload["source_data_dir"] = str(preview.source_data_dir)
    payload["sample_entries"] = list(preview.sample_entries)
    payload["warnings"] = list(preview.warnings)
    return payload


def _preview_from_payload(payload: dict[str, object]) -> ImportPreview:
    asset_summary_payload = payload.get("asset_summary")
    return ImportPreview(
        source_name=str(payload["source_name"]),
        source_kind=str(payload["source_kind"]),
        source_data_dir=Path(str(payload["source_data_dir"])),
        class_count=int(payload["class_count"]),
        document_count=int(payload["document_count"]),
        total_entries=int(payload["total_entries"]),
        total_bytes=int(payload["total_bytes"]),
        sample_entries=tuple(str(value) for value in payload.get("sample_entries", [])),
        warnings=tuple(str(value) for value in payload.get("warnings", [])),
        schema_version=(
            int(payload["schema_version"]) if payload.get("schema_version") is not None else None
        ),
        database_identity=(
            str(payload["database_identity"])
            if payload.get("database_identity") is not None
            else None
        ),
        conflicts=tuple(str(value) for value in payload.get("conflicts", [])),
        asset_summary=(
            ImportAssetSummary(
                selected_models=int(asset_summary_payload.get("selected_models") or 0),
                selected_model_bytes=int(asset_summary_payload.get("selected_model_bytes") or 0),
                selected_caches=int(asset_summary_payload.get("selected_caches") or 0),
                selected_cache_bytes=int(asset_summary_payload.get("selected_cache_bytes") or 0),
                preserved_models=int(asset_summary_payload.get("preserved_models") or 0),
                preserved_model_bytes=int(asset_summary_payload.get("preserved_model_bytes") or 0),
                preserved_caches=int(asset_summary_payload.get("preserved_caches") or 0),
                preserved_cache_bytes=int(asset_summary_payload.get("preserved_cache_bytes") or 0),
            )
            if isinstance(asset_summary_payload, dict)
            else None
        ),
        old_runtime_active=(
            bool(payload["old_runtime_active"])
            if payload.get("old_runtime_active") is not None
            else None
        ),
        source_lock=str(payload["source_lock"]) if payload.get("source_lock") else None,
    )


def _require_packaged_mode() -> None:
    if not settings.packaged_mode:
        raise LyraError("Desktop import is only available in the packaged app.")


def _build_preview(selection_token: str) -> ImportPreview:
    record = _selection_record(selection_token)
    selected_root = Path(record["path"])
    source_data_dir = _resolve_selected_source_data_dir(selected_root)
    source_db = source_data_dir / "lyra.db"
    private.assert_not_symlink(source_data_dir, "the selected import folder")
    private.assert_not_symlink(source_db, "the selected import database")
    if not source_db.is_file():
        raise LyraError("That folder does not look like a Lyra data directory.")
    metadata = _source_metadata_from_path(source_db)
    snapshot = _snapshot_entries(source_data_dir)
    lock_state, old_runtime_active = _probe_source_lock(source_db)
    total_bytes = (
        sum(entry.size for entry in snapshot)
        + _database_family_size(source_db)
        + _profile_preserve_bytes()
    )
    _assert_headroom(total_bytes)
    return ImportPreview(
        source_name=str(record["label"]),
        source_kind=_classify_source_kind(source_data_dir, selected_root=selected_root),
        source_data_dir=source_data_dir,
        class_count=int(metadata["class_count"]),
        document_count=int(metadata["document_count"]),
        total_entries=len(snapshot),
        total_bytes=total_bytes,
        sample_entries=tuple(entry.relative for entry in snapshot[:PREVIEW_SAMPLE_LIMIT]),
        warnings=("Lyra preserves this installation's own settings, keys, and downloaded models.",),
        schema_version=int(metadata["schema_version"]),
        database_identity=_database_identity(source_db),
        conflicts=_destination_conflicts(),
        asset_summary=_asset_summary(source_data_dir),
        old_runtime_active=old_runtime_active,
        source_lock=lock_state,
    )


def _source_metadata_from_path(source_db: Path) -> dict[str, int]:
    try:
        conn = sqlite3.connect(f"{source_db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LyraError("The selected Lyra database could not be read.") from exc
    try:
        return _source_metadata(conn)
    finally:
        conn.close()


def _source_metadata(conn: sqlite3.Connection) -> dict[str, int]:
    schema_version = int(conn.execute("pragma user_version").fetchone()[0])
    latest = _latest_migration_version()
    if schema_version > latest:
        raise LyraError("This data was created by a newer Lyra and cannot be imported here.")
    interrupted = int(conn.execute("select count(*) from storage_intents").fetchone()[0])
    if interrupted:
        raise LyraError(
            "The selected data still has interrupted file operations. "
            "Open it once in the old Lyra and try again."
        )
    return {
        "schema_version": schema_version,
        "class_count": int(conn.execute("select count(*) from classes").fetchone()[0]),
        "document_count": int(conn.execute("select count(*) from documents").fetchone()[0]),
    }


def _resolve_selected_source_data_dir(selected_root: Path) -> Path:
    expanded = selected_root.expanduser()
    for candidate in desktop_paths.selected_source_data_candidates(expanded):
        if (candidate / "lyra.db").is_file():
            return candidate
    raise LyraError("That folder does not look like a Lyra data directory.")


def _selection_record(selection_token: str) -> dict[str, str]:
    token = selection_token.strip()
    if not token or "/" in token or "\\" in token or "." in token:
        raise LyraError("Pick a folder in the desktop app before starting the import.")
    path = _selections_dir_path() / f"{token}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LyraError("Pick a folder in the desktop app before starting the import.") from None
    except (OSError, ValueError):
        raise LyraError("The selected folder could not be reopened. Pick it again.") from None
    if not isinstance(payload, dict):
        raise LyraError("The selected folder could not be reopened. Pick it again.")
    selected_path = payload.get("path")
    label = payload.get("label")
    if not isinstance(selected_path, str) or not selected_path.strip():
        raise LyraError("The selected folder could not be reopened. Pick it again.")
    if not isinstance(label, str) or not label.strip():
        label = Path(selected_path).expanduser().name or "Selected folder"
    return {"path": selected_path.strip(), "label": label.strip()}


def _classify_source_kind(source_data_dir: Path, *, selected_root: Path | None = None) -> str:
    if (
        selected_root is not None
        and selected_root != source_data_dir
        and source_data_dir == selected_root / "data"
    ):
        return "checkout_root"
    parent = source_data_dir.parent
    if source_data_dir.name == "data" and parent.name and parent.joinpath("backend").exists():
        return "checkout_root"
    return "data_directory"


def _snapshot_entries(source_data_dir: Path) -> list[_SnapshotEntry]:
    entries: list[_SnapshotEntry] = []
    for directory in _SOURCE_IMPORT_DIRECTORIES:
        root = source_data_dir / directory
        if not root.exists():
            continue
        if not root.is_dir() or root.is_symlink():
            raise LyraError("The selected data contains an unsafe directory entry.")
        for current in sorted(root.rglob("*")):
            info = current.lstat()
            if current.is_symlink():
                raise LyraError("The selected data contains a symlinked file or folder.")
            if current.is_dir():
                continue
            if not current.is_file():
                raise LyraError("The selected data contains an unsupported file entry.")
            entries.append(
                _SnapshotEntry(
                    source=current,
                    relative=current.relative_to(source_data_dir).as_posix(),
                    size=info.st_size,
                )
            )
    return entries


def _assert_headroom(total_bytes: int) -> None:
    free = shutil.disk_usage(settings.data_dir.parent).free
    required = total_bytes + max(64 * 1024 * 1024, total_bytes // 5)
    if free < required:
        raise LyraError("There is not enough free disk space to import that Lyra data safely.")


def _destination_is_ready() -> bool:
    return not _destination_conflicts()


def _destination_conflicts() -> tuple[str, ...]:
    conflicts: list[str] = []
    if _directory_has_user_files(settings.uploads_dir):
        conflicts.append("Uploads already contain live Lyra files.")
    if _directory_has_user_files(settings.text_dir):
        conflicts.append("Extracted text already contains live Lyra files.")
    if not settings.db_path.exists():
        return tuple(conflicts)
    conn = connect()
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            ).fetchall()
            if (
                isinstance(row[0], str)
                and row[0] != "settings"
                and not any(
                    str(row[0]).startswith(prefix) for prefix in _DESTINATION_AUXILIARY_PREFIXES
                )
            )
        ]
        for table in tables:
            count = int(conn.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
            if count:
                conflicts.append(f"Database already contains {table.replace('_', ' ')} records.")
            if len(conflicts) >= CONFLICT_SAMPLE_LIMIT:
                break
    finally:
        conn.close()
    return tuple(conflicts)


def _profile_preserve_bytes() -> int:
    total = 0
    for directory in _PROFILE_PRESERVE_DIRECTORIES:
        root = settings.data_dir / directory
        if not root.exists():
            continue
        for current in sorted(root.rglob("*")):
            if current.is_symlink():
                raise LyraError("This installation's existing profile contains an unsafe file.")
            if current.is_file():
                total += current.stat().st_size
    for filename in _PROFILE_PRESERVE_FILES:
        current = settings.data_dir / filename
        if current.exists():
            total += current.stat().st_size
    return total


def _copy_profile_into_stage() -> None:
    for directory in _PROFILE_PRESERVE_DIRECTORIES:
        source = settings.data_dir / directory
        if not source.exists():
            continue
        destination = _stage_data_path() / directory
        if destination.exists():
            shutil.rmtree(destination)
        copy_tree_without_links(source, destination)
    for filename in _PROFILE_PRESERVE_FILES:
        source = settings.data_dir / filename
        if source.exists():
            copy_regular_file(source, _stage_data_path() / filename)


def _merge_destination_profile(stage_db: Path) -> None:
    if not settings.db_path.exists():
        return
    current = connect()
    try:
        row = get_settings_row(current)
        columns = [str(column) for column in row.keys() if column != "id"]  # noqa: SIM118
        payload = {column: row[column] for column in columns}
    finally:
        current.close()
    staged = sqlite3.connect(str(stage_db))
    try:
        assignments = ", ".join(f"{column} = ?" for column in payload)
        staged.execute(
            f"update settings set {assignments} where id = 1",  # noqa: S608
            tuple(payload.values()),
        )
        staged.commit()
    finally:
        staged.close()


def _asset_summary(source_data_dir: Path) -> ImportAssetSummary:
    selected_models, selected_model_bytes = _count_files_and_bytes(source_data_dir / "models")
    selected_caches, selected_cache_bytes = _count_files_and_bytes(source_data_dir / "pages")
    preserved_models, preserved_model_bytes = _count_files_and_bytes(settings.models_dir)
    preserve_cache = not private.is_within(settings.pages_dir, settings.data_dir)
    preserved_caches, preserved_cache_bytes = (
        _count_files_and_bytes(settings.pages_dir) if preserve_cache else (0, 0)
    )
    return ImportAssetSummary(
        selected_models=selected_models,
        selected_model_bytes=selected_model_bytes,
        selected_caches=selected_caches,
        selected_cache_bytes=selected_cache_bytes,
        preserved_models=preserved_models,
        preserved_model_bytes=preserved_model_bytes,
        preserved_caches=preserved_caches,
        preserved_cache_bytes=preserved_cache_bytes,
    )


def _count_files_and_bytes(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    if path.is_symlink():
        raise LyraError("The selected data contains a symlinked file or folder.")
    if path.is_file():
        return (1, path.stat().st_size)
    count = 0
    total = 0
    for current in sorted(path.rglob("*")):
        if current.is_symlink():
            raise LyraError("The selected data contains a symlinked file or folder.")
        if current.is_file():
            count += 1
            total += current.stat().st_size
    return (count, total)


def _probe_source_lock(source_db: Path) -> tuple[str, bool | None]:
    try:
        conn = sqlite3.connect(str(source_db), timeout=0, isolation_level=None)
    except sqlite3.Error:
        return ("unavailable", None)
    try:
        conn.execute("pragma busy_timeout = 0")
        try:
            conn.execute("begin immediate")
        except sqlite3.OperationalError as exc:
            if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                return ("busy", True)
            return ("unavailable", None)
        conn.rollback()
        return ("available", False)
    finally:
        conn.close()


def _database_identity(path: Path) -> str:
    record = json.dumps(_source_database_record(path), sort_keys=True).encode("utf-8")
    return f"lyra-db:{_database_family_size(path)}:{hashlib.sha256(record).hexdigest()[:12]}"


def _directory_has_user_files(path: Path) -> bool:
    if not path.exists():
        return False
    for entry in path.iterdir():
        if entry.name == ".permissions-hardened":
            continue
        return True
    return False


def _assert_destination_ready() -> None:
    if not _destination_is_ready():
        raise ConflictError("This installation already contains Lyra data, so import is disabled.")


def _selections_dir_path() -> Path:
    return settings.data_dir.parent / SELECTIONS_DIR


def _stage_root_path() -> Path:
    return settings.data_dir.parent / STAGE_ROOT


def _stage_data_path() -> Path:
    return _stage_root_path() / "data"


def _stage_db_path() -> Path:
    if private.is_within(settings.db_path, settings.data_dir):
        return _stage_data_path() / settings.db_path.relative_to(settings.data_dir)
    return _stage_root_path() / "database" / settings.db_path.name


def _write_stage_manifest(manifest: dict[str, object]) -> None:
    private.secure_mkdir(_stage_root_path(), root=settings.data_dir.parent)
    private.write_private_text(
        _stage_root_path() / MANIFEST_FILE,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _read_stage_manifest() -> dict[str, object] | None:
    path = _stage_root_path() / MANIFEST_FILE
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != MANIFEST_VERSION:
        return None
    return payload


def _build_stage_manifest(
    source_data_dir: Path,
    source_db: Path,
    snapshot: list[_SnapshotEntry],
    *,
    source_name: str,
    source_kind: str,
    metadata: dict[str, int],
) -> dict[str, object]:
    return {
        "version": MANIFEST_VERSION,
        "created_at": _utc_now(),
        "source_name": source_name,
        "source_kind": source_kind,
        "source_data_dir": str(source_data_dir),
        "source_layout": {
            "class_count": int(metadata["class_count"]),
            "document_count": int(metadata["document_count"]),
            "schema_version": int(metadata["schema_version"]),
        },
        "source_database": _source_database_record(source_db),
        "staged_database": None,
        "total_entries": len(snapshot),
        "total_bytes": _manifest_total_bytes(snapshot, source_db),
        "entries": [
            {
                "relative": entry.relative,
                "size": entry.size,
                "sha256": _hash_file(entry.source),
                "source_identity": _source_identity(entry.source),
            }
            for entry in snapshot
        ],
    }


def _prepare_stage_manifest(
    existing: dict[str, object] | None,
    source_data_dir: Path,
    source_db: Path,
    snapshot: list[_SnapshotEntry],
    *,
    source_name: str,
    source_kind: str,
    metadata: dict[str, int],
) -> dict[str, object]:
    if existing is None:
        return _build_stage_manifest(
            source_data_dir,
            source_db,
            snapshot,
            source_name=source_name,
            source_kind=source_kind,
            metadata=metadata,
        )
    _assert_manifest_matches_source(existing, source_db, snapshot, metadata)
    manifest = dict(existing)
    manifest.update(
        {
            "version": MANIFEST_VERSION,
            "source_name": source_name,
            "source_kind": source_kind,
            "source_data_dir": str(source_data_dir),
            "source_layout": {
                "class_count": int(metadata["class_count"]),
                "document_count": int(metadata["document_count"]),
                "schema_version": int(metadata["schema_version"]),
            },
            "total_entries": len(snapshot),
            "total_bytes": _manifest_total_bytes(snapshot, source_db),
        }
    )
    return manifest


def _assert_manifest_matches_source(
    manifest: dict[str, object],
    source_db: Path,
    snapshot: list[_SnapshotEntry],
    metadata: dict[str, int],
) -> None:
    layout = manifest.get("source_layout")
    if not isinstance(layout, dict):
        raise LyraError(
            "The selected Lyra data changed while it was being staged. Start the import again."
        )
    if (
        int(layout.get("class_count") or -1) != int(metadata["class_count"])
        or int(layout.get("document_count") or -1) != int(metadata["document_count"])
        or int(layout.get("schema_version") or -1) != int(metadata["schema_version"])
    ):
        raise LyraError(
            "The selected Lyra data changed while it was being staged. Start the import again."
        )
    expected_entries = _manifest_entries_by_relative(manifest)
    if len(expected_entries) != len(snapshot):
        raise LyraError(
            "The selected Lyra data changed while it was being staged. Start the import again."
        )
    for entry in snapshot:
        recorded = expected_entries.get(entry.relative)
        if recorded is None:
            raise LyraError(
                "The selected Lyra data changed while it was being staged. Start the import again."
            )
        if int(recorded.get("size") or -1) != entry.size:
            raise LyraError(
                "The selected Lyra data changed while it was being staged. Start the import again."
            )
        if recorded.get("source_identity") != _source_identity(entry.source):
            raise LyraError(
                "The selected Lyra data changed while it was being staged. Start the import again."
            )
        if str(recorded.get("sha256") or "") != _hash_file(entry.source):
            raise LyraError(
                "The selected Lyra data changed while it was being staged. Start the import again."
            )
    recorded_db = manifest.get("source_database")
    if not isinstance(recorded_db, dict):
        raise LyraError(
            "The selected Lyra data changed while it was being staged. Start the import again."
        )
    if recorded_db != _source_database_record(source_db):
        raise LyraError(
            "The selected Lyra data changed while it was being staged. Start the import again."
        )


def _manifest_entries_by_relative(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    payload = manifest.get("entries")
    if not isinstance(payload, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for entry in payload:
        if isinstance(entry, dict) and isinstance(entry.get("relative"), str):
            result[str(entry["relative"])] = entry
    return result


def _source_database_record(source_db: Path) -> dict[str, object]:
    identity = _source_identity(source_db)
    conn = connect(source_db)
    digest = hashlib.sha256()
    try:
        conn.execute("begin")
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        conn.close()
    return {
        "logical_sha256": digest.hexdigest(),
        "source_identity": {
            "device": identity["device"],
            "inode": identity["inode"],
        },
    }


def _staged_database_record(stage_db: Path) -> dict[str, object]:
    return {"size": stage_db.stat().st_size, "sha256": _hash_file(stage_db)}


def _source_identity(path: Path) -> dict[str, int]:
    info = path.stat()
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mtime_ns": int(info.st_mtime_ns),
    }


def _manifest_total_bytes(snapshot: list[_SnapshotEntry], source_db: Path) -> int:
    return (
        sum(entry.size for entry in snapshot)
        + _database_family_size(source_db)
        + _profile_preserve_bytes()
    )


def _database_family_size(source_db: Path) -> int:
    total = 0
    for suffix in ("", "-wal"):
        current = source_db.with_name(source_db.name + suffix)
        if current.is_file():
            total += current.stat().st_size
    return total


def _staged_progress(
    snapshot: list[_SnapshotEntry], manifest: dict[str, object]
) -> tuple[int, int]:
    copied_entries = 0
    copied_bytes = 0
    manifest_entries = _manifest_entries_by_relative(manifest)
    for entry in snapshot:
        if _staged_match(manifest_entries.get(entry.relative), _stage_data_path() / entry.relative):
            copied_entries += 1
            copied_bytes += entry.size
    if _staged_database_matches(manifest):
        copied_bytes += _stage_db_path().stat().st_size
    return copied_entries, copied_bytes


def _staged_match(record: dict[str, object] | None, target: Path) -> bool:
    if record is None:
        return False
    try:
        return (
            target.is_file()
            and target.stat().st_size == int(record.get("size") or -1)
            and _hash_file(target) == str(record.get("sha256") or "")
        )
    except OSError:
        return False


def _staged_database_matches(manifest: dict[str, object]) -> bool:
    recorded = manifest.get("staged_database")
    if not isinstance(recorded, dict):
        return False
    target = _stage_db_path()
    return target.is_file() and recorded == _staged_database_record(target)


def _backup_database(source_conn: sqlite3.Connection, destination: Path) -> None:
    private.secure_mkdir(destination.parent, root=settings.data_dir.parent)
    destination.unlink(missing_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        source_conn.backup(target)
        target.commit()
        result = target.execute("pragma quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("The imported database snapshot failed SQLite quick_check.")
    finally:
        target.close()


def _rewrite_document_paths(stage_db: Path) -> None:
    conn = sqlite3.connect(str(stage_db))
    try:
        rows = conn.execute(
            "select id, class_id, filename, stored_path from documents order by id"
        ).fetchall()
        for document_id, class_id, filename, stored_path in rows:
            basename = _expected_upload_basename(
                int(document_id),
                int(class_id),
                str(filename),
                str(stored_path or ""),
            )
            rewritten = settings.uploads_dir / str(class_id) / basename
            conn.execute(
                "update documents set stored_path = ? where id = ?",
                (str(rewritten), int(document_id)),
            )
        conn.commit()
    finally:
        conn.close()


def _expected_upload_basename(
    document_id: int, class_id: int, filename: str, stored_path: str
) -> str:
    canonical = f"{document_id}-{_safe_filename(filename)}"
    expected = _stage_data_path() / "uploads" / str(class_id) / canonical
    if expected.is_file():
        return canonical
    legacy = Path(stored_path).name
    legacy_path = _stage_data_path() / "uploads" / str(class_id) / legacy
    if legacy and legacy_path.is_file():
        return legacy
    raise RuntimeError("The selected data is missing one or more uploaded source files.")


def _verify_staged_import(stage_data: Path, stage_db: Path) -> None:
    manifest = _read_stage_manifest()
    if manifest is None:
        raise RuntimeError("The staged desktop import manifest is missing.")
    for relative, record in _manifest_entries_by_relative(manifest).items():
        target = stage_data / relative
        if not private.is_within(target, stage_data) or not _staged_match(record, target):
            raise RuntimeError("The staged desktop import files did not match the snapshot.")
    verify_sqlite(stage_db)
    conn = sqlite3.connect(str(stage_db))
    try:
        rows = conn.execute("select class_id, stored_path from documents").fetchall()
        for _class_id, stored_path in rows:
            path = Path(str(stored_path))
            if not private.is_within(path, settings.uploads_dir):
                raise RuntimeError(
                    "The imported database references a file outside Lyra's uploads."
                )
            relative = path.relative_to(settings.data_dir)
            staged = stage_data / relative
            if not staged.is_file():
                raise RuntimeError("The imported database references a missing uploaded file.")
    finally:
        conn.close()


def _verify_live_import() -> None:
    verify_sqlite(settings.db_path)
    conn = sqlite3.connect(str(settings.db_path))
    try:
        rows = conn.execute("select stored_path from documents").fetchall()
        for (stored_path,) in rows:
            path = Path(str(stored_path))
            if not private.is_within(path, settings.uploads_dir):
                raise RuntimeError(
                    "The imported database references a file outside Lyra's uploads."
                )
            if not path.is_file():
                raise RuntimeError("The imported database references a missing uploaded file.")
    finally:
        conn.close()


def _truncate_sqlite_wal(db_path: Path) -> None:
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True) as conn:
        result = conn.execute("pragma wal_checkpoint(truncate)").fetchone()
        if result and result[0] != 0:
            raise RuntimeError("The staged database could not checkpoint its WAL.")


def _publish_recovery_record_path() -> Path:
    return settings.data_dir.parent / PUBLISH_RECOVERY_FILE


def _publish_recovery_root_path() -> Path:
    return settings.data_dir.parent / ".desktop-import-publish-recovery"


def _publish_backup_data_path() -> Path:
    return _publish_recovery_root_path() / "live-data"


def _publish_backup_db_path() -> Path:
    return _publish_recovery_root_path() / "live-db" / settings.db_path.name


def _write_publish_recovery_record(record: dict[str, object]) -> None:
    private.secure_mkdir(_publish_recovery_root_path(), root=settings.data_dir.parent)
    payload = dict(record)
    payload["updated_at"] = _utc_now()
    private.write_private_text(
        _publish_recovery_record_path(),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _load_publish_recovery_record() -> dict[str, object] | None:
    path = _publish_recovery_record_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or int(payload.get("version") or 0) != PUBLISH_RECOVERY_VERSION
    ):
        raise RuntimeError("The desktop import recovery record is unreadable.")
    return payload


def _patch_publish_recovery_record(**updates: object) -> dict[str, object]:
    record = _load_publish_recovery_record()
    if record is None:
        raise RuntimeError("The desktop import recovery record is missing.")
    record.update(updates)
    _write_publish_recovery_record(record)
    return record


def _db_moves_separately() -> bool:
    return not private.is_within(settings.db_path, settings.data_dir)


def _db_family_exists(path: Path) -> bool:
    return any(path.with_name(path.name + suffix).exists() for suffix in SQLITE_SIDECARS)


def _remove_db_family(path: Path) -> None:
    for suffix in SQLITE_SIDECARS:
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _move_db_family(source: Path, destination: Path) -> None:
    private.secure_mkdir(destination.parent, root=destination.parent)
    for suffix in SQLITE_SIDECARS:
        current = source.with_name(source.name + suffix)
        if current.exists():
            current.replace(destination.with_name(destination.name + suffix))


def _read_state_raw() -> dict[str, object] | None:
    with desktop_import_manager._lock:
        return desktop_import_manager._load_state()


def _patch_state_raw(**updates: object) -> None:
    with desktop_import_manager._lock:
        state = desktop_import_manager._load_state()
        if state is None:
            return
        state.update(updates)
        desktop_import_manager._save_state_locked(state)


def _set_staged_state(message: str) -> None:
    manifest = _read_stage_manifest()
    total_entries = int(manifest["total_entries"]) if manifest is not None else 0
    total_bytes = int(manifest["total_bytes"]) if manifest is not None else 0
    _patch_state_raw(
        status=_STAGED_STATUS,
        phase=_AWAITING_PUBLISH_PHASE,
        message=message,
        copied_entries=total_entries,
        total_entries=total_entries,
        copied_bytes=total_bytes,
        total_bytes=total_bytes,
        cancel_requested=False,
        requires_restart=True,
    )


def _set_failed_state(message: str) -> None:
    _patch_state_raw(
        status="failed",
        phase="failed",
        message=message,
        cancel_requested=False,
    )


def _set_completed_state(manifest: dict[str, object], message: str) -> None:
    _patch_state_raw(
        status="completed",
        phase="completed",
        message=message,
        copied_entries=int(manifest["total_entries"]),
        total_entries=int(manifest["total_entries"]),
        copied_bytes=int(manifest["total_bytes"]),
        total_bytes=int(manifest["total_bytes"]),
        cancel_requested=False,
        requires_restart=False,
    )


def _stage_ready_for_publish() -> bool:
    manifest = _read_stage_manifest()
    if manifest is None:
        return False
    if not _stage_data_path().exists():
        return False
    return _stage_db_path().exists()


def _initialize_publish_recovery() -> dict[str, object]:
    record = {
        "version": PUBLISH_RECOVERY_VERSION,
        "created_at": _utc_now(),
        "phase": "prepared",
        "db_moves_separately": _db_moves_separately(),
    }
    _write_publish_recovery_record(record)
    return record


def _sync_destination_profile_into_stage(manifest: dict[str, object]) -> dict[str, object]:
    _copy_profile_into_stage()
    _merge_destination_profile(_stage_db_path())
    _truncate_sqlite_wal(_stage_db_path())
    manifest = dict(manifest)
    manifest["total_bytes"] = _manifest_total_bytes(
        _snapshot_entries(Path(str(manifest["source_data_dir"]))),
        Path(str(manifest["source_data_dir"])) / "lyra.db",
    )
    manifest["staged_database"] = _staged_database_record(_stage_db_path())
    _write_stage_manifest(manifest)
    return manifest


def _complete_publish_from_record() -> dict[str, object]:
    record = _load_publish_recovery_record()
    if record is None:
        raise RuntimeError("The desktop import recovery record is missing.")
    manifest = _read_stage_manifest()
    if manifest is None:
        raise RuntimeError("The staged desktop import manifest is missing.")
    backup_data = _publish_backup_data_path()
    backup_db = _publish_backup_db_path()
    if settings.data_dir.exists() and _stage_data_path().exists() and not backup_data.exists():
        settings.data_dir.replace(backup_data)
        _patch_publish_recovery_record(phase="live_data_backed_up")
    if (
        _db_moves_separately()
        and settings.db_path.exists()
        and _stage_db_path().exists()
        and not _db_family_exists(backup_db)
    ):
        _move_db_family(settings.db_path, backup_db)
        _patch_publish_recovery_record(phase="live_db_backed_up")
    if not settings.data_dir.exists():
        if not _stage_data_path().exists():
            raise RuntimeError("The staged desktop import payload is missing.")
        _stage_data_path().replace(settings.data_dir)
        _patch_publish_recovery_record(phase="data_published")
    if _db_moves_separately() and not settings.db_path.exists():
        if not _db_family_exists(_stage_db_path()):
            raise RuntimeError("The staged desktop import database is missing.")
        _move_db_family(_stage_db_path(), settings.db_path)
        _patch_publish_recovery_record(phase="database_published")
    _verify_live_import()
    _set_completed_state(manifest, "Import complete. Lyra reopened the imported data safely.")
    shutil.rmtree(_publish_recovery_root_path(), ignore_errors=True)
    _publish_recovery_record_path().unlink(missing_ok=True)
    shutil.rmtree(_stage_root_path(), ignore_errors=True)
    return {
        "status": "ok",
        "phase": "completed",
        "message": "Desktop import published.",
    }


def _rollback_publish_from_record(message: str) -> None:
    backup_data = _publish_backup_data_path()
    backup_db = _publish_backup_db_path()
    private.secure_mkdir(_stage_root_path(), root=settings.data_dir.parent)
    if settings.data_dir.exists() and not _stage_data_path().exists():
        settings.data_dir.replace(_stage_data_path())
    if (
        _db_moves_separately()
        and settings.db_path.exists()
        and not _db_family_exists(_stage_db_path())
    ):
        _move_db_family(settings.db_path, _stage_db_path())
    if backup_data.exists():
        if settings.data_dir.exists():
            shutil.rmtree(settings.data_dir)
        backup_data.replace(settings.data_dir)
    if _db_moves_separately() and _db_family_exists(backup_db):
        _remove_db_family(settings.db_path)
        _move_db_family(backup_db, settings.db_path)
    shutil.rmtree(_publish_recovery_root_path(), ignore_errors=True)
    _publish_recovery_record_path().unlink(missing_ok=True)
    if _stage_ready_for_publish():
        _set_staged_state(
            f"{message} The staged import is still available; relaunch to retry publish."
        )
    else:
        _set_failed_state(message)


def publish_staged_import(*, stream: TextIO | None = None) -> int:
    _require_packaged_mode()
    output = stream or sys.stdout
    payload: dict[str, object]
    code = 0
    try:
        record = _load_publish_recovery_record()
        if record is not None:
            payload = _complete_publish_from_record()
        else:
            state = _read_state_raw()
            if state is None or str(state.get("status") or "") not in {_STAGED_STATUS, "completed"}:
                payload = {
                    "status": "noop",
                    "phase": "idle",
                    "message": "No staged desktop import is waiting to publish.",
                }
            elif str(state.get("status") or "") == "completed":
                payload = {
                    "status": "ok",
                    "phase": "completed",
                    "message": "Desktop import was already published.",
                }
            else:
                manifest = _read_stage_manifest()
                if manifest is None:
                    raise RuntimeError("The staged desktop import manifest is missing.")
                _assert_destination_ready()
                manifest = _sync_destination_profile_into_stage(manifest)
                _verify_staged_import(_stage_data_path(), _stage_db_path())
                _initialize_publish_recovery()
                payload = _complete_publish_from_record()
    except Exception as exc:
        code = 1
        message = str(exc).strip() or "The staged desktop import could not be published."
        if _load_publish_recovery_record() is not None:
            _rollback_publish_from_record(message)
        else:
            _set_failed_state(message)
        payload = {"status": "error", "phase": "failed", "message": message}
    output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    output.flush()
    return code


def recover_desktop_import_publish() -> dict[str, object]:
    record = _load_publish_recovery_record()
    if record is None:
        return {"status": "skipped", "message": "No desktop import publish recovery needed."}
    try:
        return _complete_publish_from_record()
    except Exception as exc:
        message = str(exc).strip() or "Desktop import publish recovery failed."
        _rollback_publish_from_record(message)
        return {
            "status": "rolled_back",
            "phase": "failed",
            "message": message,
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _latest_migration_version() -> int:
    numbers: list[int] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        prefix, separator, _ = path.name.partition("_")
        if separator and prefix.isdigit():
            numbers.append(int(prefix))
    if not numbers:
        raise RuntimeError("No database migrations are available.")
    return max(numbers)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
