"""Regression tests for the dependency-free local application launcher."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import threading
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from backend.storage import private
from backend.storage.database import connect, migrate


def load_launcher() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "lyra_launcher.py"
    spec = importlib.util.spec_from_file_location("lyra_launcher_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher() -> ModuleType:
    return load_launcher()


@pytest.fixture(autouse=True)
def isolate_launcher_state(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Never let launcher tests read or write the checkout's live ownership state."""

    runtime_dir = tmp_path / ".lyra"
    monkeypatch.setattr(launcher, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(launcher, "RUNTIME_FILE", runtime_dir / "runtime.json")
    monkeypatch.setattr(launcher, "INSTALL_FILE", runtime_dir / "install.json")
    monkeypatch.setattr(launcher, "LOCK_FILE", runtime_dir / "launcher.lock")


def owned_record() -> dict[str, object]:
    return {
        "pid": 417,
        "pgid": 417,
        "start_token": "proc:12345",
        "command": ["python", "-m", "uvicorn"],
    }


def seed_workspace(root: Path, *, external_db: bool = False) -> tuple[Path, Path]:
    data_dir = root / "workspace-data"
    uploads = data_dir / "uploads" / "1"
    text_dir = data_dir / "text"
    pages = data_dir / "pages" / "1"
    models = data_dir / "models"
    uploads.mkdir(parents=True)
    text_dir.mkdir(parents=True)
    pages.mkdir(parents=True)
    models.mkdir(parents=True)

    db_path = root / "backup-db" / "lyra.db" if external_db else data_dir / "lyra.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)

    conn.execute(
        "update settings set endpoint_url = ?, model = ?, context_window = ?, "
        "allow_web_research = ?, parallel_requests = ?, parallel_concurrency = ? where id = 1",
        (
            "http://127.0.0.1:8080/v1",
            "qwen-local",
            16384,
            1,
            1,
            3,
        ),
    )
    class_id = int(
        conn.execute(
            "insert into classes (name, code, semester, archived) values (?, ?, ?, ?)",
            ("Physics", "PHY 201", "Fall 2026", 0),
        ).lastrowid
        or 0
    )
    document_id = int(
        conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state, "
            "stage_detail, pages_total, pages_done, pages_skipped, recognize) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                class_id,
                "lecture.pdf",
                f"uploads/{class_id}/1-lecture.pdf",
                "application/pdf",
                17,
                "ready",
                "indexed",
                2,
                2,
                0,
                1,
            ),
        ).lastrowid
        or 0
    )
    conn.execute(
        "insert into document_pages (document_id, page_number, state, text, error_message) "
        "values (?, ?, ?, ?, ?)",
        (document_id, 1, "recognized", "Magnetic flux notes", None),
    )
    chunk_id = int(
        conn.execute(
            "insert into chunks (document_id, class_id, content, token_count, page_number, "
            "section_title, problem_number, part_index, doc_type, embedding_model, embedding_dim) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                class_id,
                "Faraday's law summary",
                4,
                1,
                "Lecture 1",
                None,
                None,
                "notes",
                "nomic",
                768,
            ),
        ).lastrowid
        or 0
    )
    artifact_id = int(
        conn.execute(
            "insert into artifacts (class_id, kind, title, state, stage_detail, problems_total, "
            "problems_done, error_message) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                class_id,
                "solution_set",
                "Worksheet 1",
                "ready",
                "complete",
                1,
                1,
                None,
            ),
        ).lastrowid
        or 0
    )
    conn.execute(
        "insert into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, ?, ?)",
        (artifact_id, document_id, "problem_set", 0),
    )
    part_id = int(
        conn.execute(
            "insert into artifact_parts (artifact_id, parent_part_id, kind, ordinal, label, "
            "content, status, origin, verdict, content_version) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                None,
                "problem",
                0,
                "1",
                "Apply Faraday's law.",
                "complete",
                "generated",
                "verified",
                # A non-zero version so restore preserving it is a real assertion, not a
                # coincidence with the column default (PLA-289).
                4,
            ),
        ).lastrowid
        or 0
    )
    conn.execute(
        "insert into artifact_part_revisions (part_id, revision, content, origin, note) "
        "values (?, ?, ?, ?, ?)",
        (part_id, 1, "Apply Faraday's law.", "generated", "Initial draft"),
    )
    conn.execute(
        "insert into artifact_provenance (part_id, chunk_id, document_id, page_number, label) "
        "values (?, ?, ?, ?, ?)",
        (part_id, chunk_id, document_id, 1, "p.1"),
    )
    conn.commit()
    conn.close()

    (uploads / "1-lecture.pdf").write_bytes(b"%PDF-1.7 lecture\n")
    (text_dir / f"{document_id}.txt").write_text("Magnetic flux notes", encoding="utf-8")
    (pages / "1.png").write_bytes(b"png-page-1")
    (models / "manifest.txt").write_text("local model marker", encoding="utf-8")
    runtime = models / "llama-server"
    runtime.write_bytes(b"local runtime")
    if os.name == "posix":
        os.chmod(runtime, 0o755)  # noqa: S103 - executable preservation fixture
    return data_dir, db_path


def assert_restored_workspace(data_dir: Path, db_path: Path) -> None:
    assert (data_dir / "uploads" / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 lecture\n"
    assert (data_dir / "text" / "1.txt").read_text(encoding="utf-8") == "Magnetic flux notes"
    assert (data_dir / "pages" / "1" / "1.png").read_bytes() == b"png-page-1"
    assert (data_dir / "models" / "manifest.txt").read_text(
        encoding="utf-8"
    ) == "local model marker"
    assert (data_dir / "models" / "llama-server").read_bytes() == b"local runtime"
    if os.name == "posix":
        assert stat.S_IMODE((data_dir / "models" / "llama-server").stat().st_mode) & 0o100

    conn = sqlite3.connect(db_path)
    try:
        settings = conn.execute(
            "select endpoint_url, model, context_window, allow_web_research, "
            "parallel_requests, parallel_concurrency from settings where id = 1"
        ).fetchone()
        assert settings == (
            "http://127.0.0.1:8080/v1",
            "qwen-local",
            16384,
            1,
            1,
            3,
        )
        document = conn.execute(
            "select filename, stored_path, state, pages_total, pages_done, recognize from documents"
        ).fetchone()
        assert document == (
            "lecture.pdf",
            "uploads/1/1-lecture.pdf",
            "ready",
            2,
            2,
            1,
        )
        artifact = conn.execute(
            "select title, state, problems_total, problems_done from artifacts"
        ).fetchone()
        assert artifact == ("Worksheet 1", "ready", 1, 1)
        # The body and its optimistic-concurrency version both survive the round-trip, so a
        # restored draft cannot report the wrong version and refuse the student's edits.
        part = conn.execute("select content, content_version from artifact_parts").fetchone()
        assert part == ("Apply Faraday's law.", 4)
        revision = conn.execute("select revision, note from artifact_part_revisions").fetchone()
        assert revision == (1, "Initial draft")
    finally:
        conn.close()


def write_backup_archive(
    archive: Path,
    *,
    manifest: dict[str, object],
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    with tarfile.open(archive, mode="w:gz") as bundle:
        payload = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
        for info, payload in members:
            bundle.addfile(info, io.BytesIO(payload) if payload is not None else None)


def internal_manifest(launcher: ModuleType) -> dict[str, object]:
    return {
        "version": launcher.BACKUP_VERSION,
        "created_at": "2026-08-07T00:00:00Z",
        "data_dir": launcher.BACKUP_DATA_PREFIX,
        "db": {
            "inside_data_dir": True,
            "relative_path": "lyra.db",
            "member": "data/lyra.db",
        },
    }


def test_parse_args_supports_commands_and_safe_legacy_flags(launcher: ModuleType) -> None:
    assert launcher.parse_args([]).command == "start"
    assert launcher.parse_args(["status"]).command == "status"
    assert launcher.parse_args(["--stop"]).command == "stop"
    assert launcher.parse_args(["--prod"]).command == "start"
    assert launcher.parse_args(["--dev", "--no-browser"]).dev is True
    assert launcher.parse_args(["backup", "--archive", "lyra-backup.tgz"]).command == "backup"
    restore = launcher.parse_args(
        ["restore", "--archive", "lyra-backup.tgz", "--data-dir", "restored-data"]
    )
    assert restore.command == "restore"
    assert restore.data_dir == Path("restored-data")


def test_parse_args_rejects_lifecycle_only_flags_on_other_commands(
    launcher: ModuleType,
) -> None:
    with pytest.raises(SystemExit):
        launcher.parse_args(["status", "--clean"])
    with pytest.raises(SystemExit):
        launcher.parse_args(["doctor", "--dev"])
    with pytest.raises(SystemExit):
        launcher.parse_args(["backup"])
    with pytest.raises(SystemExit):
        launcher.parse_args(["restore", "--archive", "lyra-backup.tgz"])
    with pytest.raises(SystemExit):
        launcher.parse_args(["status", "--archive", "lyra-backup.tgz"])


def test_backup_and_restore_roundtrip_preserves_workspace_data(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, db_path = seed_workspace(tmp_path)
    archive = tmp_path / "lyra-backup.tgz"
    restored_data = tmp_path / "restored-data"
    stops: list[str] = []

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(
        launcher,
        "stop_supervised_stack",
        lambda _runtime: stops.append("stop") or True,
    )

    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    assert archive.exists()
    assert (
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
        == 0
    )

    assert stops == ["stop", "stop"]
    assert_restored_workspace(restored_data, restored_data / db_path.name)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_backup_archive_is_created_private_regardless_of_umask(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive carries the whole data tree, so it must be owner-only from creation.

    Written under a wide-open umask on purpose: the archive lives outside the data
    directory and so has no owner-only parent to hide behind, and its mode must not depend
    on the umask the backup happened to run under.
    """
    data_dir, _db_path = seed_workspace(tmp_path)
    archive = tmp_path / "lyra-backup.tgz"
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    previous_umask = os.umask(0)
    try:
        assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    finally:
        os.umask(previous_umask)

    assert archive.exists()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_backup_refuses_a_dangling_symlink_archive_target_without_creating_through_it(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, _db_path = seed_workspace(tmp_path)
    external = tmp_path / "outside" / "stolen-backup.tgz"
    archive_link = tmp_path / "lyra-backup.tgz"
    archive_link.symlink_to(external)
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="target already exists"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive_link)]))

    assert archive_link.is_symlink()
    assert not external.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
@pytest.mark.parametrize("external_db", [False, True])
def test_restore_creates_private_data_and_database_regardless_of_umask(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    external_db: bool,
) -> None:
    data_dir, db_path = seed_workspace(tmp_path, external_db=external_db)
    archive = tmp_path / "lyra-backup.tgz"
    restored_data = tmp_path / "restored-data"
    restored_db = tmp_path / "restored-db" / "lyra.db"
    restored_db.parent.mkdir(mode=0o755)
    os.chmod(restored_db.parent, 0o755)  # noqa: S103 - pre-existing user-chosen parent
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    if external_db:
        monkeypatch.setenv("LYRA_DB_PATH", str(db_path))
    else:
        monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)
    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0

    arguments = ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
    if external_db:
        arguments.extend(["--db-path", str(restored_db)])
    previous_umask = os.umask(0)
    try:
        assert launcher.restore(launcher.parse_args(arguments)) == 0
    finally:
        os.umask(previous_umask)

    final_db = restored_db if external_db else restored_data / "lyra.db"
    for directory in (
        restored_data,
        restored_data / "uploads",
        restored_data / "text",
        restored_data / "pages",
        restored_data / "models",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in (
        restored_data / "uploads" / "1" / "1-lecture.pdf",
        restored_data / "text" / "1.txt",
        restored_data / "pages" / "1" / "1.png",
        restored_data / "models" / "manifest.txt",
        final_db,
    ):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE((restored_data / "models" / "llama-server").stat().st_mode) == 0o700
    if external_db:
        assert stat.S_IMODE(restored_db.parent.stat().st_mode) == 0o755
        # Successful launcher output must be accepted by the exact parent predicate the
        # backend applies before its next normal SQLite open.
        private.assert_safe_external_writer_parent(restored_db.parent)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
@pytest.mark.parametrize("unsafe_mode", [0o777, 0o775])
def test_restore_refuses_an_external_database_parent_writable_by_other_users(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    data_dir, db_path = seed_workspace(tmp_path, external_db=True)
    archive = tmp_path / "lyra-backup.tgz"
    restored_data = tmp_path / "restored-data"
    restored_db = tmp_path / "unsafe-restored-db" / "lyra.db"
    restored_db.parent.mkdir()
    os.chmod(restored_db.parent, unsafe_mode)  # noqa: S103 - unsafe boundary under test
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LYRA_DB_PATH", str(db_path))
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)
    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0

    with pytest.raises(launcher.LauncherError, match="writable by other users"):
        launcher.restore(
            launcher.parse_args(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-dir",
                    str(restored_data),
                    "--db-path",
                    str(restored_db),
                ]
            )
        )

    assert stat.S_IMODE(restored_db.parent.stat().st_mode) == unsafe_mode
    assert not restored_db.exists()
    assert not restored_data.exists()
    assert list(restored_db.parent.glob(f".{restored_db.name}.restore-*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")
def test_restore_refuses_an_external_database_parent_not_owned_by_the_current_user(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "someone-elses-db-dir"
    parent.mkdir(mode=0o755)
    target = parent / "lyra.db"
    real_fstat = os.fstat

    def report_different_owner(descriptor: int) -> SimpleNamespace:
        info = real_fstat(descriptor)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)

    monkeypatch.setattr(launcher.os, "fstat", report_different_owner)

    with pytest.raises(launcher.LauncherError, match="owned by the current user"):
        launcher.restore_target_file(target)

    assert not target.exists()


def test_backup_and_restore_roundtrip_supports_external_database_path(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, db_path = seed_workspace(tmp_path, external_db=True)
    archive = tmp_path / "lyra-external-db.tgz"
    restored_data = tmp_path / "restored-data"
    restored_db = tmp_path / "restored-db" / "lyra.db"
    restored_db.parent.mkdir()

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LYRA_DB_PATH", str(db_path))
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    assert (
        launcher.restore(
            launcher.parse_args(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-dir",
                    str(restored_data),
                    "--db-path",
                    str(restored_db),
                ]
            )
        )
        == 0
    )

    assert_restored_workspace(restored_data, restored_db)


def test_restore_refuses_to_overwrite_a_non_empty_directory(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, _db_path = seed_workspace(tmp_path)
    archive = tmp_path / "lyra-backup.tgz"
    restored_data = tmp_path / "restored-data"
    restored_data.mkdir()
    (restored_data / "keep.txt").write_text("leave me alone", encoding="utf-8")

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    with pytest.raises(launcher.LauncherError, match="must not already exist"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )


def test_backup_refuses_symlinks_inside_the_data_directory(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, _db_path = seed_workspace(tmp_path)
    archive = tmp_path / "lyra-backup.tgz"
    (data_dir / "uploads" / "1" / "outside-link").symlink_to(tmp_path / "elsewhere.txt")

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="symlink entry"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))


def test_restore_rejects_unexpected_archive_members_without_creating_the_target(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "bad-backup.tgz"
    restored_data = tmp_path / "restored-data"
    db = tarfile.TarInfo("data/lyra.db")
    db_payload = b"not a database"
    db.size = len(db_payload)
    stray = tarfile.TarInfo("unexpected.txt")
    stray_payload = b"bad member"
    stray.size = len(stray_payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(db, db_payload), (stray, stray_payload)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="unexpected entry"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_missing_internal_database_member(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "missing-db.tgz"
    restored_data = tmp_path / "restored-data"
    text = tarfile.TarInfo("data/text/1.txt")
    text_payload = b"class notes"
    text.size = len(text_payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(text, text_payload)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="missing its database file inside data/"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rolls_back_requested_targets_when_external_db_finalize_fails(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, db_path = seed_workspace(tmp_path, external_db=True)
    archive = tmp_path / "lyra-external-db.tgz"
    restored_data = tmp_path / "restored-data"
    restored_db = tmp_path / "restored-db" / "lyra.db"
    restored_db.parent.mkdir()

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("LYRA_DB_PATH", str(db_path))
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)
    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0

    original_replace = type(restored_db).replace
    stage_db = restored_db.with_name(f".{restored_db.name}.restore-{os.getpid()}")

    def fail_final_db_replace(self: Path, target: Path) -> Path:
        if self == stage_db and Path(target) == restored_db:
            raise OSError("disk full")
        return original_replace(self, target)

    monkeypatch.setattr(type(restored_db), "replace", fail_final_db_replace)

    with pytest.raises(launcher.LauncherError, match="targets were rolled back"):
        launcher.restore(
            launcher.parse_args(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--data-dir",
                    str(restored_data),
                    "--db-path",
                    str(restored_db),
                ]
            )
        )
    assert not restored_data.exists()
    assert not restored_db.exists()


@pytest.mark.parametrize(
    ("member_name", "match"),
    [
        ("../evil.txt", "unsafe path"),
        ("data/../evil.txt", "unsafe path"),
    ],
)
def test_restore_rejects_traversal_members(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    member_name: str,
    match: str,
) -> None:
    archive = tmp_path / "traversal.tgz"
    restored_data = tmp_path / "restored-data"
    info = tarfile.TarInfo(member_name)
    payload = b"bad"
    info.size = len(payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(info, payload)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match=match):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_duplicate_normalized_members(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "duplicate.tgz"
    restored_data = tmp_path / "restored-data"
    one = tarfile.TarInfo("data/./lyra.db")
    first_payload = b"first"
    one.size = len(first_payload)
    two = tarfile.TarInfo("data/lyra.db")
    second_payload = b"second"
    two.size = len(second_payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(one, first_payload), (two, second_payload)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="duplicate entry"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_link_members(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "link.tgz"
    restored_data = tmp_path / "restored-data"
    info = tarfile.TarInfo("data/lyra.db")
    info.type = tarfile.SYMTYPE
    info.linkname = "elsewhere.db"
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(info, None)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="link entry"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_special_members(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "special.tgz"
    restored_data = tmp_path / "restored-data"
    info = tarfile.TarInfo("data/pipe")
    info.type = tarfile.FIFOTYPE
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(info, None)],
    )

    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="unsupported entry"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_oversized_archive_members(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "oversized.tgz"
    restored_data = tmp_path / "restored-data"
    info = tarfile.TarInfo("data/lyra.db")
    payload = b"12345"
    info.size = len(payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(info, payload)],
    )

    monkeypatch.setattr(launcher, "BACKUP_MAX_MEMBER_BYTES", 4)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="too large"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_archives_with_too_many_members(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "too-many.tgz"
    restored_data = tmp_path / "restored-data"
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    for index in range(3):
        info = tarfile.TarInfo(f"data/file-{index}.txt")
        payload = f"{index}".encode()
        info.size = len(payload)
        members.append((info, payload))
    db = tarfile.TarInfo("data/lyra.db")
    db_payload = b"db"
    db.size = len(db_payload)
    members.append((db, db_payload))
    write_backup_archive(archive, manifest=internal_manifest(launcher), members=members)

    monkeypatch.setattr(launcher, "BACKUP_MAX_MEMBERS", 3)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="too many entries"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_restore_rejects_archives_exceeding_total_size(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "too-large-total.tgz"
    restored_data = tmp_path / "restored-data"
    first = tarfile.TarInfo("data/lyra.db")
    first_payload = b"1234"
    first.size = len(first_payload)
    second = tarfile.TarInfo("data/text/1.txt")
    second_payload = b"5678"
    second.size = len(second_payload)
    write_backup_archive(
        archive,
        manifest=internal_manifest(launcher),
        members=[(first, first_payload), (second, second_payload)],
    )

    monkeypatch.setattr(launcher, "BACKUP_MAX_TOTAL_BYTES", 7)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    with pytest.raises(launcher.LauncherError, match="too large to restore safely"):
        launcher.restore(
            launcher.parse_args(
                ["restore", "--archive", str(archive), "--data-dir", str(restored_data)]
            )
        )
    assert not restored_data.exists()


def test_backup_refuses_when_an_external_writer_holds_the_database(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir, db_path = seed_workspace(tmp_path)
    archive = tmp_path / "lyra-backup.tgz"
    writer = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
    writer.execute("begin immediate")

    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)

    try:
        with pytest.raises(launcher.LauncherError, match="database is still busy"):
            launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))
    finally:
        writer.rollback()
        writer.close()


# ---------------------------------------------------------------------------
# PLA-307: Atomic backup publication — failure-injection tests
# ---------------------------------------------------------------------------


def _backup_monkeypatch(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    archive_name: str = "lyra-backup.tgz",
) -> tuple[Path, Path]:
    """Common setup for PLA-307 backup-failure tests."""
    data_dir, _db_path = seed_workspace(tmp_path)
    archive = tmp_path / archive_name
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("LYRA_DB_PATH", raising=False)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "stop_supervised_stack", lambda _runtime: True)
    return data_dir, archive


def test_backup_cleans_staging_after_tar_write_failure(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exception during archive compression must not leave staging or final artifacts."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_add = tarfile.TarFile.add

    def exploding_add(self: tarfile.TarFile, name: str, arcname: str = "", **kw: object) -> None:
        if arcname == launcher.BACKUP_DATA_PREFIX:
            raise OSError("simulated write failure")
        original_add(self, name, arcname=arcname, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", exploding_add)

    with pytest.raises(OSError, match="simulated write failure"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists(), "final archive must not exist after write failure"
    staging_leftovers = list(archive.parent.glob(".lyra-backup-*.tmp"))
    assert staging_leftovers == [], f"staging file left behind: {staging_leftovers}"


def test_backup_cleans_staging_after_keyboard_interrupt(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A KeyboardInterrupt mid-write must clean up staging without leaving partial artifacts."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_add = tarfile.TarFile.add

    def interrupting_add(self: tarfile.TarFile, name: str, arcname: str = "", **kw: object) -> None:
        if arcname == launcher.BACKUP_DATA_PREFIX:
            raise KeyboardInterrupt
        original_add(self, name, arcname=arcname, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", interrupting_add)

    with pytest.raises(KeyboardInterrupt):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists()
    assert list(archive.parent.glob(".lyra-backup-*.tmp")) == []


def test_backup_cleans_staging_after_disk_full_simulation(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A simulated disk-full error during write must clean up completely."""
    import errno as _errno

    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_add = tarfile.TarFile.add

    def disk_full_add(self: tarfile.TarFile, name: str, arcname: str = "", **kw: object) -> None:
        if arcname == launcher.BACKUP_DATA_PREFIX:
            raise OSError(_errno.ENOSPC, "No space left on device")
        original_add(self, name, arcname=arcname, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", disk_full_add)

    with pytest.raises(OSError, match="No space left on device"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists()
    assert list(archive.parent.glob(".lyra-backup-*.tmp")) == []


def test_backup_refuses_pre_existing_destination(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """backup_archive_target refuses when the archive already exists."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)
    archive.write_bytes(b"existing content")

    with pytest.raises(launcher.LauncherError, match="target already exists"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))


def test_backup_retry_succeeds_after_failed_attempt(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retry to the same destination after a failed attempt must succeed."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_add = tarfile.TarFile.add
    attempt = [0]

    def fail_first_add(self: tarfile.TarFile, name: str, arcname: str = "", **kw: object) -> None:
        if arcname == launcher.BACKUP_DATA_PREFIX and attempt[0] == 0:
            attempt[0] = 1
            raise OSError("first attempt fails")
        original_add(self, name, arcname=arcname, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", fail_first_add)

    with pytest.raises(OSError, match="first attempt fails"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists()

    monkeypatch.setattr(tarfile.TarFile, "add", original_add)
    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    assert archive.exists()


def test_backup_publication_race_does_not_delete_competing_file(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If another process creates the archive before our link, their file must survive."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_link = os.link

    def racing_link(src: object, dst: object) -> None:
        Path(str(dst)).write_bytes(b"raced-into-place")
        original_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(FileExistsError):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert archive.exists(), "competing file must survive the race"
    assert archive.read_bytes() == b"raced-into-place", "competing file content must be intact"
    assert list(archive.parent.glob(".lyra-backup-*.tmp")) == [], "staging must be cleaned"
    staging_candidates = list(archive.parent.glob(".lyra-backup-*"))
    assert staging_candidates == [], "no staging artifacts should remain"
    assert archive.stat().st_size == len(b"raced-into-place"), "file size must match"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_backup_staging_file_is_private(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The staging file must be mode 0o600 from creation, even under a wide-open umask."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    observed_modes: list[int] = []
    original_link = os.link

    def capturing_link(src: object, dst: object) -> None:
        observed_modes.append(stat.S_IMODE(os.stat(src).st_mode))
        original_link(src, dst)

    monkeypatch.setattr(os, "link", capturing_link)

    previous_umask = os.umask(0)
    try:
        assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0
    finally:
        os.umask(previous_umask)

    assert observed_modes == [0o600]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_backup_structurally_validates_archive_before_publication(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive is validated after writing. A corrupt archive must not be published."""
    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_open = tarfile.open

    def corrupt_validation(name: object = None, mode: str = "r", **kw: object) -> tarfile.TarFile:
        if mode == "r:gz":
            raise tarfile.ReadError("simulated corrupt archive")
        return original_open(name, mode=mode, **kw)

    monkeypatch.setattr(tarfile, "open", corrupt_validation)

    with pytest.raises(tarfile.ReadError, match="simulated corrupt archive"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists()
    assert list(archive.parent.glob(".lyra-backup-*.tmp")) == []


def test_backup_normal_roundtrip_still_works(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sanity check: the atomic publication path produces a valid, restorable archive."""
    data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)
    assert launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)])) == 0

    assert archive.exists()
    with tarfile.open(archive, mode="r:gz") as check:
        manifest = launcher.read_backup_manifest(check)
        assert manifest["version"] == launcher.BACKUP_VERSION

    restored = tmp_path / "restored"
    assert (
        launcher.restore(
            launcher.parse_args(["restore", "--archive", str(archive), "--data-dir", str(restored)])
        )
        == 0
    )
    assert_restored_workspace(restored, restored / "lyra.db")


# ---------------------------------------------------------------------------
# PLA-307: ownership-based publication cleanup (inode identity)
# ---------------------------------------------------------------------------


def test_backup_cleanup_removes_own_hard_link_on_fsync_failure(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When _fsync_directory raises after os.link succeeds, the archive is removed
    because inode identity proves it belongs to this attempt.
    """
    import errno

    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    def exploding_fsync(path: Path) -> None:
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(launcher, "_fsync_directory", exploding_fsync)

    with pytest.raises(OSError, match="I/O error"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert not archive.exists(), "our own hard link must be cleaned up after fsync failure"
    assert list(archive.parent.glob(".lyra-backup-*.tmp")) == [], "staging must be cleaned"


def test_backup_cleanup_preserves_competitor_file_after_link_and_fsync_failure(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If a competitor replaces the archive between os.link and _fsync_directory,
    the cleanup must not remove the competitor's file.
    """
    import errno

    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_link = os.link

    def replacing_link(src: object, dst: object) -> None:
        original_link(src, dst)
        Path(str(dst)).unlink()
        Path(str(dst)).write_bytes(b"competitor-archive")

    def exploding_fsync(path: Path) -> None:
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(os, "link", replacing_link)
    monkeypatch.setattr(launcher, "_fsync_directory", exploding_fsync)

    with pytest.raises(OSError, match="I/O error"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))

    assert archive.exists(), "competitor's file must survive"
    assert archive.read_bytes() == b"competitor-archive", "competitor's content must be intact"


# ---------------------------------------------------------------------------
# PLA-307: fsync durability contract truthfulness
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX errno semantics")
def test_fsync_directory_propagates_real_io_error(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real I/O error (EIO) from os.fsync on a directory must propagate."""
    import errno

    def eio_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "simulated disk failure")

    monkeypatch.setattr(os, "fsync", eio_fsync)

    with pytest.raises(OSError) as exc_info:
        launcher._fsync_directory(tmp_path)

    assert exc_info.value.errno == errno.EIO


@pytest.mark.skipif(os.name != "posix", reason="POSIX errno semantics")
def test_fsync_directory_tolerates_unsupported_operation(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EINVAL from os.fsync (filesystem does not support dir fsync) is tolerated."""
    import errno

    def einval_fsync(fd: int) -> None:
        raise OSError(errno.EINVAL, "operation not supported")

    monkeypatch.setattr(os, "fsync", einval_fsync)

    launcher._fsync_directory(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX errno semantics")
def test_fsync_directory_tolerates_enosys(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ENOSYS (function not implemented) is tolerated."""
    import errno

    def enosys_fsync(fd: int) -> None:
        raise OSError(errno.ENOSYS, "not implemented")

    monkeypatch.setattr(os, "fsync", enosys_fsync)

    launcher._fsync_directory(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX errno semantics")
def test_backup_fails_on_real_fsync_directory_error(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real fsync failure during backup must prevent false success."""
    import errno

    _data_dir, archive = _backup_monkeypatch(launcher, monkeypatch, tmp_path)

    original_fsync = os.fsync

    def selective_eio_fsync(fd: int) -> None:
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            name = ""
        if os.path.isdir(name):
            raise OSError(errno.EIO, "disk failure")
        original_fsync(fd)

    # On macOS /proc/self/fd doesn't exist; use a counter instead.
    call_count = [0]

    def counting_eio_fsync(fd: int) -> None:
        call_count[0] += 1
        if call_count[0] > 1:
            raise OSError(errno.EIO, "disk failure on directory fsync")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_eio_fsync)

    with pytest.raises(OSError, match="disk failure"):
        launcher.backup(launcher.parse_args(["backup", "--archive", str(archive)]))


@pytest.mark.parametrize(
    "output,expected",
    [("Python 3.14.6\n", (3, 14, 6)), ("v22.23.0\n", (22, 23, 0))],
)
def test_executable_version_accepts_python_and_node_prefixes(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: tuple[int, int, int],
) -> None:
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    assert launcher.executable_version("runtime") == expected


def test_atomic_json_write_replaces_complete_document(launcher: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "runtime.json"
    launcher.atomic_write_json(target, {"version": 1, "processes": {"backend": {"pid": 7}}})

    assert json.loads(target.read_text()) == {
        "processes": {"backend": {"pid": 7}},
        "version": 1,
    }
    assert list(target.parent.glob("*.tmp-*")) == []


def test_process_record_rejects_reused_pid(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = owned_record()
    monkeypatch.setattr(launcher, "process_start_token", lambda _pid: "proc:different-birth")
    monkeypatch.setattr(launcher, "process_group", lambda _pid: 417)

    assert launcher.record_matches_process(record) is False


def test_stop_never_signals_a_stale_or_reused_pid(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(launcher, "process_start_token", lambda _pid: "proc:reused")
    monkeypatch.setattr(launcher, "process_group", lambda _pid: 417)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher.os, "killpg", lambda pgid, sig: signaled.append((pgid, sig)))

    assert launcher.stop_owned_component("backend", owned_record(), 8000) is True
    assert signaled == []


def test_stop_signals_verified_owned_process_group(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = iter(["proc:12345", "proc:12345", None])
    signaled: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(launcher, "process_start_token", lambda _pid: next(tokens, None))
    monkeypatch.setattr(launcher, "process_group", lambda _pid: 417)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher.os, "killpg", lambda pgid, sig: signaled.append((pgid, sig)))

    assert launcher.stop_owned_component("backend", owned_record(), 8000) is True
    assert signaled == [(417, signal.SIGTERM)]


def test_component_state_distinguishes_healthy_unowned_server(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: True)

    description, healthy = launcher.component_state(
        "backend", None, 8000, "http://127.0.0.1:8000/api/settings"
    )

    assert "healthy server" in description
    assert "not launcher-owned" in description
    assert healthy is False


def test_component_state_distinguishes_stale_record_and_port_conflict(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)
    monkeypatch.setattr(launcher, "listener_description", lambda _port: "pid 999 other-app")

    description, healthy = launcher.component_state(
        "frontend", owned_record(), 3000, "http://127.0.0.1:3000"
    )

    assert "stale ownership record" in description
    assert "unowned port conflict" in description
    assert "other-app" in description
    assert healthy is False


def test_start_refuses_unowned_listener_instead_of_adopting_it(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda *_args: ("backend: healthy but unowned", False),
    )

    with pytest.raises(launcher.LauncherError, match="never kill or adopt"):
        launcher.ensure_port_available("backend", None, 8000, "http://127.0.0.1:8000/api/settings")


def test_recovers_healthy_backend_from_this_checkout(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = [
        str(launcher.ROOT / ".venv/bin/python"),
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    monkeypatch.setattr(launcher, "listener_pids", lambda _port: (901,))
    monkeypatch.setattr(launcher, "process_group", lambda _pid: 901)
    monkeypatch.setattr(launcher, "process_cwd", lambda _pid: launcher.ROOT)
    monkeypatch.setattr(launcher, "process_command", lambda _pid: command)
    monkeypatch.setattr(launcher, "process_start_token", lambda _pid: "proc:901")
    monkeypatch.setattr(launcher, "url_ready", lambda _url: True)

    record = launcher.recover_checkout_component("backend", 8000)

    assert record is not None
    assert record["pid"] == 901
    assert record["pgid"] == 901
    assert record["recovered"] is True


def test_recovers_frontend_through_its_checkout_owned_process_group(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = [
        "/opt/homebrew/bin/pnpm",
        "--dir",
        str(launcher.FRONTEND),
        "start",
        "--hostname",
        "127.0.0.1",
        "--port",
        "3000",
    ]
    monkeypatch.setattr(launcher, "listener_pids", lambda _port: (914,))
    monkeypatch.setattr(launcher, "process_group", lambda pid: 900 if pid in {900, 914} else None)
    monkeypatch.setattr(launcher, "process_cwd", lambda pid: launcher.ROOT if pid == 900 else None)
    monkeypatch.setattr(launcher, "process_command", lambda pid: command if pid == 900 else [])
    monkeypatch.setattr(launcher, "process_start_token", lambda _pid: "proc:900")
    monkeypatch.setattr(launcher, "url_ready", lambda _url: True)

    record = launcher.recover_checkout_component("frontend", 3000)

    assert record is not None
    assert record["pid"] == 900
    assert record["pgid"] == 900


def test_never_recovers_listener_with_a_foreign_command(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "listener_pids", lambda _port: (901,))
    monkeypatch.setattr(launcher, "process_group", lambda _pid: 901)
    monkeypatch.setattr(launcher, "process_cwd", lambda _pid: launcher.ROOT)
    monkeypatch.setattr(
        launcher,
        "process_command",
        lambda _pid: ["python", "-m", "http.server", "8000"],
    )
    monkeypatch.setattr(launcher, "url_ready", lambda _url: True)

    assert launcher.recover_checkout_component("backend", 8000) is None


def test_reconcile_stops_verified_unhealthy_checkout_process_before_restart(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {},
        "bundled_services": [],
    }
    recovered = owned_record()
    stopped: list[tuple[str, dict[str, object], int]] = []
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(
        launcher,
        "recover_checkout_component",
        lambda _name, _port: recovered,
    )
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)
    monkeypatch.setattr(
        launcher,
        "stop_owned_component",
        lambda name, record, port: stopped.append((name, record, port)) or True,
    )
    monkeypatch.setattr(launcher, "atomic_write_json", lambda *_args: None)

    result = launcher.reconcile_component_record(
        "backend", None, launcher.BACKEND_PORT, launcher.BACKEND_URL, runtime
    )

    assert result is None
    assert stopped == [("backend", recovered, launcher.BACKEND_PORT)]
    assert "backend" not in runtime["processes"]


def test_bundled_service_invocation_uses_narrow_subprocess_contract(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = tmp_path / "infra" / "search.py"
    helper.parent.mkdir()
    helper.touch()
    calls: list[tuple[list[str], Path, bool]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> SimpleNamespace:
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    service = launcher.BundledService("search", helper)

    assert launcher.invoke_bundled_service(service, "doctor", required=True) == 0
    assert calls == [([launcher.sys.executable, str(helper), "doctor"], launcher.ROOT, False)]


def test_bundled_service_start_rolls_back_in_reverse_order(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    services = (
        launcher.BundledService("search", tmp_path / "search.py"),
        launcher.BundledService("inference", tmp_path / "inference.py"),
    )
    events: list[tuple[str, str]] = []

    def invoke(service: SimpleNamespace, command: str, *, required: bool, wait: bool = True) -> int:
        del required, wait
        name = service.name
        events.append((name, command))
        return 1 if (name, command) == ("inference", "start") else 0

    monkeypatch.setattr(launcher, "invoke_bundled_service", invoke)

    with pytest.raises(launcher.LauncherError, match="inference"):
        launcher.start_bundled_services(services)

    assert events == [
        ("search", "start"),
        ("inference", "start"),
        ("inference", "stop"),
        ("search", "stop"),
    ]


def test_existing_bundle_is_not_rolled_back_when_idempotent_start_fails(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = launcher.BundledService("search", tmp_path / "search.py")
    events: list[str] = []

    def invoke(
        _service: SimpleNamespace, command: str, *, required: bool, wait: bool = True
    ) -> int:
        del required, wait
        events.append(command)
        return 1

    monkeypatch.setattr(launcher, "invoke_bundled_service", invoke)

    with pytest.raises(launcher.LauncherError, match="search"):
        launcher.start_bundled_services((service,), preserve_on_failure={"search"})

    assert events == ["start"]


def test_bundled_service_shutdown_continues_after_one_helper_fails(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    services = (
        launcher.BundledService("search", tmp_path / "search.py"),
        launcher.BundledService("inference", tmp_path / "inference.py"),
    )
    events: list[str] = []

    def invoke(service: SimpleNamespace, command: str, *, required: bool, wait: bool = True) -> int:
        del command, required, wait
        events.append(service.name)
        if service.name == "inference":
            raise launcher.LauncherError("inference helper crashed")
        return 0

    monkeypatch.setattr(launcher, "invoke_bundled_service", invoke)

    assert launcher.stop_bundled_services(services) is False
    assert events == ["inference", "search"]


def test_supervisor_stops_remaining_app_and_bundles_when_one_core_process_exits(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = owned_record()
    frontend = {**owned_record(), "pid": 418, "pgid": 418}
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": backend, "frontend": frontend},
        "bundled_services": ["search"],
    }
    stopped_components: list[tuple[str, int]] = []
    stopped_bundles: list[str] = []

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        launcher,
        "record_matches_process",
        lambda record: record is backend,
    )
    monkeypatch.setattr(
        launcher,
        "stop_owned_component",
        lambda name, _record, port: stopped_components.append((name, port)) or True,
    )
    monkeypatch.setattr(
        launcher,
        "stop_configured_bundled_services",
        lambda names: stopped_bundles.extend(names) or True,
    )

    assert launcher.supervise() == 0
    assert stopped_components == [("backend", launcher.BACKEND_PORT)]
    assert stopped_bundles == ["search"]


def test_failed_bundle_shutdown_stays_registered_for_retry(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {},
        "bundled_services": ["search"],
    }
    writes: list[dict[str, object]] = []

    monkeypatch.setattr(
        launcher,
        "atomic_write_json",
        lambda _path, value: writes.append(dict(value)),
    )
    monkeypatch.setattr(launcher, "stop_lyra", lambda _runtime: True)
    monkeypatch.setattr(
        launcher,
        "stop_configured_bundled_services",
        lambda _names: False,
    )

    assert launcher.stop_supervised_stack(runtime) is False
    assert runtime["desired_state"] == "stopped"
    assert runtime["bundled_services"] == ["search"]
    assert writes[-1]["bundled_services"] == ["search"]


def test_failed_idempotent_rerun_does_not_stop_healthy_existing_stack(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": ["search"],
    }
    stopped: list[bool] = []

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "ensure_port_available", lambda *_args: True)
    monkeypatch.setattr(launcher, "load_json", lambda *_args: {})
    monkeypatch.setattr(
        launcher,
        "ensure_python_environment",
        lambda _metadata: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        launcher,
        "stop_supervised_stack",
        lambda _runtime: stopped.append(True) or True,
    )

    with pytest.raises(KeyboardInterrupt):
        launcher.start(launcher.parse_args(["--no-browser"]))

    assert stopped == []


def test_start_does_not_attempt_bundled_service_recovery_when_none_are_registered(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": [],
    }
    attempted: list[bool] = []

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(launcher, "ensure_port_available", lambda *_args: True)
    monkeypatch.setattr(launcher, "load_json", lambda *_args: {})
    monkeypatch.setattr(launcher, "ensure_python_environment", lambda _metadata: Path("python"))
    monkeypatch.setattr(launcher, "ensure_frontend_environment", lambda _metadata: "pnpm")
    monkeypatch.setattr(launcher, "atomic_write_json", lambda *_args: None)
    monkeypatch.setattr(
        launcher,
        "start_bundled_services",
        lambda *_args, **_kwargs: attempted.append(True),
    )

    assert launcher.start(launcher.parse_args(["--no-browser"])) == 0

    assert attempted == []
    assert runtime["bundled_services"] == []


def test_stop_respects_empty_bundle_registry_from_degraded_launch(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": [],
    }
    configured: list[list[str]] = []

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        launcher,
        "stop_supervised_stack",
        lambda state: configured.append(list(state["bundled_services"])) or True,
    )

    assert launcher.stop(launcher.parse_args(["stop"])) == 0
    assert configured == [[]]


def test_status_reports_running_core_stack_without_bundled_services(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": [],
    }

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (f"{name}: running, launcher-owned, healthy", True),
    )
    assert launcher.status(launcher.parse_args(["status"])) == 0


def test_status_ignores_legacy_bundled_service_metadata(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {
            "backend": owned_record(),
            "frontend": owned_record(),
            "supervisor": owned_record(),
        },
        "bundled_services": ["search"],
    }
    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (f"{name}: running, launcher-owned, healthy", True),
    )
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda *_args, **_kwargs: pytest.fail("retired bundled services must not be probed"),
    )

    assert launcher.status(launcher.parse_args(["status"])) == 0
    output = capsys.readouterr().out
    assert "unknown bundled service 'search' in runtime state; ignoring it" in output


def test_doctor_reports_running_core_stack_without_bundled_services(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": [],
    }
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()

    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (f"{name}: running, launcher-owned, healthy", True),
    )
    assert launcher.doctor(launcher.parse_args(["doctor"])) == 0


def test_doctor_fails_when_a_core_port_is_owned_by_another_process(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()

    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "load_runtime", lambda: launcher.empty_runtime())
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (
            (f"{name}: unowned port conflict" if name == "backend" else f"{name}: stopped"),
            False,
        ),
    )

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 1


def test_doctor_ignores_legacy_unavailable_bundled_service_metadata(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {
            "backend": owned_record(),
            "frontend": owned_record(),
            "supervisor": owned_record(),
        },
        "bundled_services": ["search"],
    }
    helper = tmp_path / "search.py"
    helper.touch()
    service = launcher.BundledService("search", helper)
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()

    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (f"{name}: running, launcher-owned, healthy", True),
    )
    monkeypatch.setattr(
        launcher,
        "bundled_services",
        lambda: (service,),
    )
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda _service, command, **_kwargs: 1 if command == "doctor" else 0,
    )

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 0
    output = capsys.readouterr().out
    assert "search" not in output


def test_doctor_does_not_probe_legacy_available_bundled_service_metadata(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {
            "backend": owned_record(),
            "frontend": owned_record(),
            "supervisor": owned_record(),
        },
        "bundled_services": ["search"],
    }
    helper = tmp_path / "search.py"
    helper.touch()
    service = launcher.BundledService("search", helper)
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()

    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        launcher,
        "component_state",
        lambda name, *_args: (f"{name}: running, launcher-owned, healthy", True),
    )
    monkeypatch.setattr(
        launcher,
        "bundled_services",
        lambda: (service,),
    )
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda *_args, **_kwargs: pytest.fail("retired bundled services must not be probed"),
    )

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 0
    output = capsys.readouterr().out
    assert "search" not in output.lower()


def test_main_reports_interrupt_without_stopping_detached_app(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    def interrupted(_args: object) -> int:
        events.append("start")
        raise KeyboardInterrupt

    class NoopLock:
        def __enter__(self) -> None:
            events.append("lock")

        def __exit__(self, *_args: object) -> None:
            events.append("unlock")

    monkeypatch.setattr(launcher, "start", interrupted)
    monkeypatch.setattr(launcher, "LauncherLock", NoopLock)
    monkeypatch.setattr(launcher, "RUNTIME_DIR", tmp_path)

    assert launcher.main(["--no-browser"]) == 130
    assert events == ["lock", "start", "unlock"]


def test_process_start_token_reads_linux_birth_tick(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fields after the command begin at process field 3. Index 19 is Linux field 22,
    # the kernel start-time tick used to distinguish PID reuse.
    fields = ["S", *[str(index) for index in range(4, 22)], "987654", "tail"]
    fake_stat = f"123 (command with spaces) {' '.join(fields)}"

    def fake_read_text(path: Path, *_args: object, **_kwargs: object) -> str:
        assert str(path) == "/proc/123/stat"
        return fake_stat

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert launcher.process_start_token(123) == "proc:987654"


def test_missing_bundled_helper_is_actionable_when_required(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = launcher.BundledService("search", tmp_path / "missing.py")

    with pytest.raises(launcher.LauncherError, match="missing"):
        launcher.invoke_bundled_service(service, "start", required=True)
    assert launcher.invoke_bundled_service(service, "status", required=False) == 1


def test_subprocess_timeout_is_not_mistaken_for_a_valid_dependency_check(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    python.touch()

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired([str(python)], 30)

    monkeypatch.setattr(launcher.subprocess, "run", timeout)

    assert launcher.backend_imports_work(python) is False


def test_diagnostics_is_a_registered_command(launcher: ModuleType) -> None:
    assert launcher.parse_args(["diagnostics"]).command == "diagnostics"


def test_diagnostics_writes_the_backend_bundle_when_the_endpoint_is_up(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "diagnostics.json"
    monkeypatch.setattr(launcher, "DIAGNOSTICS_FILE", destination)
    bundle = {"bundle_version": 1, "schema": {"version": 31, "latest": 31, "current": True}}
    monkeypatch.setattr(launcher, "_fetch_diagnostics_endpoint", lambda: json.dumps(bundle))
    monkeypatch.setattr(
        launcher,
        "_build_diagnostics_offline",
        lambda: pytest.fail("the offline builder must not run when the endpoint answers"),
    )

    assert launcher.diagnostics_command(launcher.parse_args(["diagnostics"])) == 0
    assert json.loads(destination.read_text()) == bundle


def test_diagnostics_falls_back_to_the_offline_builder_when_the_endpoint_is_down(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "diagnostics.json"
    monkeypatch.setattr(launcher, "DIAGNOSTICS_FILE", destination)
    offline = {"bundle_version": 1, "schema": {"version": 31, "latest": 31, "current": True}}
    monkeypatch.setattr(launcher, "_fetch_diagnostics_endpoint", lambda: None)
    monkeypatch.setattr(launcher, "_build_diagnostics_offline", lambda: offline)

    assert launcher.diagnostics_command(launcher.parse_args(["diagnostics"])) == 0
    assert json.loads(destination.read_text()) == offline


def test_diagnostics_writes_a_launcher_only_note_when_nothing_is_reachable(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "diagnostics.json"
    monkeypatch.setattr(launcher, "DIAGNOSTICS_FILE", destination)
    monkeypatch.setattr(launcher, "_fetch_diagnostics_endpoint", lambda: None)
    monkeypatch.setattr(launcher, "_build_diagnostics_offline", lambda: None)

    assert launcher.diagnostics_command(launcher.parse_args(["diagnostics"])) == 0
    written = json.loads(destination.read_text())
    assert written["backend_reachable"] is False
    assert "Start Lyra" in written["note"]


# ---------------------------------------------------------------------------
# PLA-146: runtime state-version skew recovery.
#
# The launcher persists its own ownership state (`.lyra/runtime.json`, keyed by
# STATE_VERSION) separately from SQLite schema migrations. STATE_VERSION has only ever
# been 1, so "older" states are represented here as structurally minimal version-1
# documents (missing the optional fields the current launcher writes), and a
# "newer" state as a version this checkout does not support. These tests prove that no
# stale, missing, malformed, truncated, or future runtime state can strand an owned
# service, signal a process the launcher cannot prove it owns, kill a foreign process
# after PID reuse, or delete user data - and that when automatic recovery is unsafe the
# launcher fails with specific remediation instead of guessing.
# ---------------------------------------------------------------------------


def write_runtime_text(launcher: ModuleType, text: str) -> Path:
    """Write raw bytes to the isolated runtime-state file, creating `.lyra/` as needed."""

    path = launcher.RUNTIME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_runtime(launcher: ModuleType, state: object) -> Path:
    return write_runtime_text(launcher, json.dumps(state))


def legacy_v1_minimal() -> dict[str, object]:
    """A version-1 state from before the launcher wrote the optional lifecycle fields."""

    return {"version": 1, "processes": {}}


def legacy_v1_with_process(record: dict[str, object]) -> dict[str, object]:
    """A version-1 state that predates `mode`, `desired_state`, and `bundled_services`."""

    return {"version": 1, "processes": {"backend": record}}


@pytest.fixture
def spawn_child() -> object:
    """Spawn real, session-leading child processes and guarantee their cleanup.

    Real subprocesses let the ownership checks run against genuine OS process identities
    (birth token and process group), not mocks, which is where PID-reuse safety actually
    has to hold.
    """

    children: list[subprocess.Popen[bytes]] = []

    def _spawn() -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        children.append(proc)
        return proc

    yield _spawn

    for proc in children:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def genuine_record(launcher: ModuleType, proc: subprocess.Popen[bytes]) -> dict[str, object]:
    """Build an ownership record from a live process's real birth identity."""

    token = launcher.process_start_token(proc.pid)
    assert token is not None, "the platform must expose a process birth token for ownership"
    return {
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "start_token": token,
        "command": [sys.executable, "-c", "sleep"],
    }


def a_distinct_birth_token(live_token: str) -> str:
    """Derive a birth token guaranteed to differ from a live process's real one.

    ``record_matches_process`` compares the recorded ``start_token`` against the token the
    OS currently reports for the PID, so any value that cannot equal the live token models
    PID reuse deterministically. We shift the trailing birth counter (Linux clock ticks or
    macOS microseconds) away from the live value rather than harvesting a second process's
    real token: two quick spawns can legitimately share a ``/proc`` start-time tick, which is
    exactly what made the old dead-then-respawn model flaky on the Linux CI runner.
    """

    prefix, separator, counter = live_token.rpartition(":")
    assert separator and counter.isdigit(), f"unexpected birth-token format: {live_token!r}"
    value = int(counter)
    shifted = value - 1000 if value >= 1000 else value + 1000
    token = f"{prefix}:{shifted}"
    assert token != live_token
    return token


def forbid_signals(launcher: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record any raw signal the launcher sends so a test can assert it sent none."""

    signaled: list[object] = []
    monkeypatch.setattr(launcher.os, "kill", lambda *args: signaled.append(args))
    if hasattr(launcher.os, "killpg"):
        monkeypatch.setattr(launcher.os, "killpg", lambda *args: signaled.append(args))
    return signaled


# --- load_runtime classification -------------------------------------------------------


def test_load_runtime_missing_state_is_a_safe_empty_stopped_state(launcher: ModuleType) -> None:
    assert not launcher.RUNTIME_FILE.exists()

    state = launcher.load_runtime()

    assert state == launcher.empty_runtime()
    assert state["version"] == launcher.STATE_VERSION
    assert state["processes"] == {}
    assert state["desired_state"] == "stopped"


def test_load_runtime_normalizes_an_old_minimal_supported_state(launcher: ModuleType) -> None:
    write_runtime(launcher, legacy_v1_minimal())

    state = launcher.load_runtime()

    # Absent optional fields are filled with safe defaults instead of rejected.
    assert state["mode"] is None
    assert state["desired_state"] == "stopped"
    assert state["bundled_services"] == []
    assert state["processes"] == {}


def test_load_runtime_preserves_records_from_a_pre_defaults_state(launcher: ModuleType) -> None:
    write_runtime(launcher, legacy_v1_with_process(owned_record()))

    state = launcher.load_runtime()

    assert state["processes"]["backend"] == owned_record()
    assert state["desired_state"] == "stopped"
    assert state["bundled_services"] == []


def test_load_runtime_rejects_empty_file_with_actionable_message(launcher: ModuleType) -> None:
    write_runtime_text(launcher, "")

    with pytest.raises(launcher.RuntimeStateError) as excinfo:
        launcher.load_runtime()

    message = str(excinfo.value)
    assert "not valid JSON" in message or "empty" in message
    assert "status" in message
    assert "No process is signaled" in message


def test_load_runtime_rejects_truncated_json(launcher: ModuleType) -> None:
    write_runtime_text(launcher, '{"version": 1, "processes": {"backend": {"pid": 4')

    with pytest.raises(launcher.RuntimeStateError, match="not valid JSON"):
        launcher.load_runtime()


def test_load_runtime_rejects_a_non_object_document(launcher: ModuleType) -> None:
    write_runtime_text(launcher, "[1, 2, 3]")

    with pytest.raises(launcher.RuntimeStateError, match="must contain a JSON object"):
        launcher.load_runtime()


def test_load_runtime_rejects_newer_state_with_downgrade_guidance(launcher: ModuleType) -> None:
    write_runtime(launcher, {"version": launcher.STATE_VERSION + 1, "processes": {}})

    with pytest.raises(launcher.RuntimeStateError) as excinfo:
        launcher.load_runtime()

    message = str(excinfo.value)
    assert "newer Lyra" in message
    assert "Do not downgrade" in message
    assert "No process is signaled" in message


def test_load_runtime_rejects_unrecognized_older_version(launcher: ModuleType) -> None:
    write_runtime(launcher, {"version": 0, "processes": {}})

    with pytest.raises(launcher.RuntimeStateError, match="unrecognized state version"):
        launcher.load_runtime()


def test_load_runtime_does_not_accept_boolean_true_as_version_one(launcher: ModuleType) -> None:
    # bool is an int subclass and True == 1; a corrupted flag must not pass as version 1.
    write_runtime(launcher, {"version": True, "processes": {}})

    with pytest.raises(launcher.RuntimeStateError, match="unrecognized state version"):
        launcher.load_runtime()


def test_load_runtime_rejects_a_non_object_processes_field(launcher: ModuleType) -> None:
    write_runtime(launcher, {"version": 1, "processes": ["backend"]})

    with pytest.raises(launcher.RuntimeStateError, match="processes"):
        launcher.load_runtime()


def test_load_runtime_rejects_an_invalid_bundled_services_list(launcher: ModuleType) -> None:
    write_runtime(launcher, {"version": 1, "processes": {}, "bundled_services": [1, 2]})

    with pytest.raises(launcher.RuntimeStateError, match="bundled_services"):
        launcher.load_runtime()


def test_load_runtime_accepts_but_never_trusts_a_malformed_record(launcher: ModuleType) -> None:
    # A structurally incomplete record (no birth token) and a non-object record both load,
    # but neither can ever prove ownership, so neither can trigger a signal.
    write_runtime(
        launcher,
        {
            "version": 1,
            "processes": {"backend": {"pid": 1234}, "frontend": "not-a-record"},
        },
    )

    state = launcher.load_runtime()

    assert launcher.record_matches_process(state["processes"]["backend"]) is False


def test_load_runtime_is_idempotent_on_a_bad_state(launcher: ModuleType) -> None:
    write_runtime(launcher, {"version": 999, "processes": {"backend": owned_record()}})

    for _ in range(3):
        with pytest.raises(launcher.RuntimeStateError, match="newer Lyra"):
            launcher.load_runtime()
    # The refusal never rewrites or deletes the file it could not trust.
    assert json.loads(launcher.RUNTIME_FILE.read_text())["version"] == 999


# --- command semantics across bad state (run/status/doctor/stop) -----------------------


def test_stop_refuses_newer_state_without_signaling(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_runtime(launcher, {"version": 999, "processes": {"backend": owned_record()}})
    signaled = forbid_signals(launcher, monkeypatch)

    with pytest.raises(launcher.RuntimeStateError, match="newer Lyra"):
        launcher.stop(launcher.parse_args(["stop"]))

    assert signaled == []


def test_stop_refuses_corrupt_state_without_signaling(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_runtime_text(launcher, '{"version": 1, "processes": {"backend": ')
    signaled = forbid_signals(launcher, monkeypatch)

    with pytest.raises(launcher.RuntimeStateError, match="not valid JSON"):
        launcher.stop(launcher.parse_args(["stop"]))

    assert signaled == []


def test_stop_with_missing_state_is_a_safe_noop(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled = forbid_signals(launcher, monkeypatch)
    monkeypatch.setattr(launcher, "stop_configured_bundled_services", lambda _names: True)

    assert launcher.stop(launcher.parse_args(["stop"])) == 0
    assert signaled == []


def test_main_stop_on_corrupt_state_returns_one_with_remediation(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_runtime_text(launcher, "not json at all")
    signaled = forbid_signals(launcher, monkeypatch)

    assert launcher.main(["stop"]) == 1

    output = capsys.readouterr().out
    assert "status" in output
    assert "move" in output.lower()
    assert signaled == []


def test_main_start_on_newer_state_refuses_before_spawning_or_signaling(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_runtime(launcher, {"version": launcher.STATE_VERSION + 5, "processes": {}})
    signaled = forbid_signals(launcher, monkeypatch)
    monkeypatch.setattr(
        launcher,
        "spawn_component",
        lambda *_args, **_kwargs: pytest.fail("start must not spawn on an untrusted state"),
    )

    assert launcher.main(["--no-browser"]) == 1

    output = capsys.readouterr().out
    assert "newer Lyra" in output
    assert signaled == []


def test_status_on_corrupt_state_reports_ports_without_signaling(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_runtime_text(launcher, "{ truncated")
    signaled = forbid_signals(launcher, monkeypatch)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)

    assert launcher.status(launcher.parse_args(["status"])) == 1

    output = capsys.readouterr().out
    assert "not valid JSON" in output
    assert "backend: stopped" in output
    assert "frontend: stopped" in output
    assert signaled == []


def test_status_on_newer_state_reports_a_healthy_unowned_listener_without_adopting(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_runtime(launcher, {"version": 4096, "processes": {"backend": owned_record()}})
    signaled = forbid_signals(launcher, monkeypatch)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: True)
    monkeypatch.setattr(launcher, "listener_description", lambda _port: "pid 4242 other")

    assert launcher.status(launcher.parse_args(["status"])) == 1

    output = capsys.readouterr().out
    assert "newer Lyra" in output
    assert "not launcher-owned" in output
    assert signaled == []


def test_doctor_on_corrupt_state_still_checks_the_host_and_reports_remediation(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_runtime_text(launcher, "}{ not json")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)
    signaled = forbid_signals(launcher, monkeypatch)

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 1

    output = capsys.readouterr().out
    # Host prerequisites are still reported before the runtime-state failure.
    assert ".venv exists and backend imports pass" in output
    assert "not valid JSON" in output
    assert "backend: stopped" in output
    assert signaled == []


def test_doctor_on_bad_state_is_idempotent(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_runtime(launcher, {"version": 0, "processes": {}})
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    monkeypatch.setattr(launcher, "FRONTEND", tmp_path)
    monkeypatch.setattr(launcher, "backend_imports_work", lambda _python: True)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda executable: executable if executable in {"pnpm", "node"} else None,
    )
    monkeypatch.setattr(launcher, "executable_version", lambda _path: (22, 23, 0))
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)

    first = launcher.doctor(launcher.parse_args(["doctor"]))
    second = launcher.doctor(launcher.parse_args(["doctor"]))

    assert first == second == 1
    assert json.loads(launcher.RUNTIME_FILE.read_text())["version"] == 0


# --- adversarial process ownership with real subprocesses ------------------------------


def test_old_state_with_running_owned_service_stops_cleanly(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, spawn_child: object
) -> None:
    proc = spawn_child()
    record = genuine_record(launcher, proc)
    write_runtime(launcher, legacy_v1_with_process(record))

    state = launcher.load_runtime()
    assert state["desired_state"] == "stopped"  # normalized from the old shape
    assert launcher.record_matches_process(record) is True

    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    # Reap concurrently so the signaled child does not linger as a zombie whose birth token
    # would still read back and look "alive" to the ownership check. In production the child
    # is reparented to launchd/init, which reaps it; the thread stands in for that here.
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()

    assert launcher.stop_owned_component("backend", record, 8000) is True

    reaper.join(timeout=5)
    assert proc.poll() is not None  # the genuinely-owned process was really stopped


def test_old_state_with_dead_service_discards_record_without_signaling(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, spawn_child: object
) -> None:
    proc = spawn_child()
    record = genuine_record(launcher, proc)
    proc.kill()
    proc.wait(timeout=5)
    signaled = forbid_signals(launcher, monkeypatch)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)

    assert launcher.record_matches_process(record) is False
    assert launcher.stop_owned_component("backend", record, 8000) is True
    assert signaled == []


def test_reused_pid_never_signals_a_live_foreign_process(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, spawn_child: object
) -> None:
    # Model PID reuse without any timing luck: a genuinely live, foreign process occupies the
    # recorded PID, but the ownership record carries a birth token deterministically shifted
    # off that process's real token. To record_matches_process this is exactly PID reuse - the
    # PID resolves to a process whose birth identity is not the one we recorded. No signal
    # primitive is mocked, so the production refuse-path has to protect the foreign process on
    # its own.
    foreign = spawn_child()
    live_token = launcher.process_start_token(foreign.pid)
    assert live_token is not None, "the platform must expose a process birth token for ownership"
    stale_token = a_distinct_birth_token(live_token)
    record = {
        "pid": foreign.pid,
        "pgid": os.getpgid(foreign.pid),
        "start_token": stale_token,
        "command": [sys.executable, "-c", "sleep"],
    }

    # Primary, timing-free correctness check: the PID and process group still resolve to the
    # live foreign process, yet ownership is refused on the birth-token mismatch alone.
    assert launcher.process_start_token(foreign.pid) == live_token
    assert launcher.record_matches_process(record) is False

    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    # Real, unmocked os.kill/os.killpg: a stale record must be discarded, never signaled.
    assert launcher.stop_owned_component("backend", record, 8000) is True

    # Positive proof the foreign process outlived the recovery attempt. The bounded wait
    # confirms the assertions above rather than establishing them: a regression that signaled
    # the group would terminate this sleeper inside the window instead of timing out.
    with pytest.raises(subprocess.TimeoutExpired):
        foreign.wait(timeout=0.5)
    os.kill(foreign.pid, 0)  # raises ProcessLookupError if it were gone


def test_reused_pid_state_reports_stale_record_and_keeps_the_conflict(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, spawn_child: object
) -> None:
    impostor = spawn_child()
    record = {
        "pid": impostor.pid,
        "pgid": os.getpgid(impostor.pid),
        "start_token": "proc:this-token-belongs-to-a-dead-process",
        "command": [sys.executable, "-c", "sleep"],
    }
    write_runtime(launcher, legacy_v1_with_process(record))
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)
    monkeypatch.setattr(launcher, "listener_description", lambda _port: "pid 5 unrelated")

    # component_state has no signaling path at all; the impostor survives being described.
    description, healthy = launcher.component_state(
        "backend",
        launcher.load_runtime()["processes"]["backend"],
        8000,
        launcher.BACKEND_URL,
    )

    assert healthy is False
    assert "stale ownership record" in description
    assert impostor.poll() is None


def test_stale_record_over_unowned_listener_is_reported_not_killed(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale record plus a live listener the launcher cannot prove it owns: stop must warn
    # and leave the listener alone rather than signal an unverified PID.
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: True)
    monkeypatch.setattr(launcher, "listener_description", lambda _port: "pid 77 other-app")
    signaled = forbid_signals(launcher, monkeypatch)

    assert launcher.stop_owned_component("backend", owned_record(), 8000) is False
    assert signaled == []


def test_bad_state_recovery_never_deletes_user_data(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "lyra.db"
    db_path.write_bytes(b"sqlite placeholder")
    sentinel = data_dir / "uploads" / "keep.pdf"
    sentinel.write_bytes(b"user file")
    monkeypatch.setenv("LYRA_DATA_DIR", str(data_dir))
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher, "url_ready", lambda _url: False)
    monkeypatch.setattr(launcher, "stop_configured_bundled_services", lambda _names: True)

    # Missing state -> safe stop; corrupt state -> status refuses; newer state -> doctor refuses.
    assert launcher.stop(launcher.parse_args(["stop"])) == 0
    write_runtime_text(launcher, "corrupt {")
    assert launcher.status(launcher.parse_args(["status"])) == 1
    write_runtime(launcher, {"version": 9001, "processes": {}})
    with pytest.raises(launcher.RuntimeStateError):
        launcher.stop(launcher.parse_args(["stop"]))

    assert data_dir.is_dir()
    assert db_path.is_file()
    assert sentinel.read_bytes() == b"user file"


def test_recovery_then_clean_relaunch_after_stale_state(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, spawn_child: object
) -> None:
    # A stale record from a dead process is discarded on stop; a subsequent load then starts
    # from a clean, empty, stopped state with no manual cleanup required.
    proc = spawn_child()
    record = genuine_record(launcher, proc)
    proc.kill()
    proc.wait(timeout=5)
    write_runtime(launcher, legacy_v1_with_process(record))
    monkeypatch.setattr(launcher, "port_is_open", lambda _port: False)
    monkeypatch.setattr(launcher, "stop_configured_bundled_services", lambda _names: True)

    assert launcher.stop(launcher.parse_args(["stop"])) == 0

    relaunch_state = launcher.load_runtime()
    assert "backend" not in relaunch_state["processes"]
    assert relaunch_state["desired_state"] == "stopped"
    assert relaunch_state["version"] == launcher.STATE_VERSION
