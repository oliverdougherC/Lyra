"""Regression tests for the dependency-free local application launcher."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

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
        "firecrawl_base_url = ?, allow_web_research = ?, parallel_requests = ?, "
        "parallel_concurrency = ? where id = 1",
        (
            "http://127.0.0.1:8080/v1",
            "qwen-local",
            16384,
            "http://127.0.0.1:3002",
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
            "content, status, origin, verdict) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    return data_dir, db_path


def assert_restored_workspace(data_dir: Path, db_path: Path) -> None:
    assert (data_dir / "uploads" / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 lecture\n"
    assert (data_dir / "text" / "1.txt").read_text(encoding="utf-8") == "Magnetic flux notes"
    assert (data_dir / "pages" / "1" / "1.png").read_bytes() == b"png-page-1"
    assert (data_dir / "models" / "manifest.txt").read_text(
        encoding="utf-8"
    ) == "local model marker"

    conn = sqlite3.connect(db_path)
    try:
        settings = conn.execute(
            "select endpoint_url, model, context_window, firecrawl_base_url, "
            "allow_web_research, parallel_requests, parallel_concurrency from settings where id = 1"
        ).fetchone()
        assert settings == (
            "http://127.0.0.1:8080/v1",
            "qwen-local",
            16384,
            "http://127.0.0.1:3002",
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


def test_firecrawl_uses_narrow_subprocess_contract(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = tmp_path / "infra" / "firecrawl.py"
    helper.parent.mkdir()
    helper.touch()
    calls: list[tuple[list[str], Path, bool]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> SimpleNamespace:
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher, "FIRECRAWL_SCRIPT", helper)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.firecrawl_command("doctor", required=True) == 0
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
        "bundled_services": ["firecrawl"],
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
    assert stopped_bundles == ["firecrawl"]


def test_failed_bundle_shutdown_stays_registered_for_retry(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {},
        "bundled_services": ["firecrawl"],
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
    assert runtime["bundled_services"] == ["firecrawl"]
    assert writes[-1]["bundled_services"] == ["firecrawl"]


def test_failed_idempotent_rerun_does_not_stop_healthy_existing_stack(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": ["firecrawl"],
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


def test_failed_bundle_start_degrades_without_stopping_existing_app(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = {
        "version": launcher.STATE_VERSION,
        "mode": "production",
        "desired_state": "running",
        "processes": {"backend": owned_record(), "frontend": owned_record()},
        "bundled_services": [],
    }
    stopped_stack: list[bool] = []
    stopped_bundles: list[str] = []
    preserved: list[set[str]] = []

    monkeypatch.setattr(launcher, "load_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(launcher, "ensure_port_available", lambda *_args: True)
    monkeypatch.setattr(launcher, "load_json", lambda *_args: {})
    monkeypatch.setattr(launcher, "ensure_python_environment", lambda _metadata: Path("python"))
    monkeypatch.setattr(launcher, "ensure_frontend_environment", lambda _metadata: "pnpm")
    monkeypatch.setattr(launcher, "atomic_write_json", lambda *_args: None)
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda _service, command, **_kwargs: 1 if command == "status" else 0,
    )

    def fail_start(_services: object, *, preserve_on_failure: set[str]) -> None:
        preserved.append(set(preserve_on_failure))
        stopped_bundles.append("firecrawl")
        raise launcher.LauncherError("bundle recovery failed")

    monkeypatch.setattr(launcher, "start_bundled_services", fail_start)
    monkeypatch.setattr(
        launcher,
        "stop_bundled_services",
        lambda services: stopped_bundles.extend(service.name for service in services) or True,
    )
    monkeypatch.setattr(
        launcher,
        "stop_supervised_stack",
        lambda _runtime: stopped_stack.append(True) or True,
    )
    monkeypatch.setattr(
        launcher,
        "ensure_supervisor",
        lambda _runtime: pytest.fail("degraded launches must not start the bundle supervisor"),
    )

    assert launcher.start(launcher.parse_args(["--no-browser"])) == 0

    assert preserved == [set()]
    assert stopped_bundles == ["firecrawl"]
    assert stopped_stack == []
    assert runtime["bundled_services"] == []
    assert "continuing without web research" in capsys.readouterr().out


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


def test_status_treats_running_degraded_stack_as_intentionally_skipped(
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
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda *_args, **_kwargs: pytest.fail("Firecrawl must stay skipped for degraded status"),
    )

    assert launcher.status(launcher.parse_args(["status"])) == 0


def test_status_reports_temporary_firecrawl_unavailability_without_failing_core_app(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        "bundled_services": ["firecrawl"],
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
        lambda _service, command, **_kwargs: 1 if command == "status" else 0,
    )

    assert launcher.status(launcher.parse_args(["status"])) == 0
    output = capsys.readouterr().out
    assert "temporarily unavailable" in output
    assert "core Lyra remains usable without web research" in output


def test_status_reports_available_firecrawl_explicitly(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        "bundled_services": ["firecrawl"],
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
        lambda _service, command, **_kwargs: 0 if command == "status" else 0,
    )

    assert launcher.status(launcher.parse_args(["status"])) == 0
    output = capsys.readouterr().out
    assert "available; web research is enabled for this app session" in output


def test_doctor_treats_running_degraded_stack_as_intentionally_skipped(
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
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda *_args, **_kwargs: pytest.fail("Firecrawl doctor must stay skipped"),
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

    assert launcher.doctor(launcher.parse_args(["doctor", "--skip-firecrawl"])) == 1


def test_doctor_reports_temporary_firecrawl_unavailability_without_failing_core_app(
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
        "bundled_services": ["firecrawl"],
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
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda _service, command, **_kwargs: 1 if command == "doctor" else 0,
    )

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 0
    output = capsys.readouterr().out
    assert "temporarily unavailable" in output
    assert "core Lyra can still run without web research" in output


def test_doctor_reports_available_firecrawl_explicitly(
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
        "bundled_services": ["firecrawl"],
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
    monkeypatch.setattr(
        launcher,
        "invoke_bundled_service",
        lambda _service, command, **_kwargs: 0 if command == "doctor" else 0,
    )

    assert launcher.doctor(launcher.parse_args(["doctor"])) == 0
    output = capsys.readouterr().out
    assert "firecrawl is available" in output.lower()


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

    assert launcher.main(["--skip-firecrawl", "--no-browser"]) == 130
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


def test_firecrawl_missing_is_actionable_unless_explicitly_skipped(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "FIRECRAWL_SCRIPT", tmp_path / "missing.py")

    with pytest.raises(launcher.LauncherError, match="--skip-firecrawl"):
        launcher.firecrawl_command("start", required=True)
    assert launcher.firecrawl_command("status", required=False) == 1


def test_subprocess_timeout_is_not_mistaken_for_a_valid_dependency_check(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    python.touch()

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired([str(python)], 30)

    monkeypatch.setattr(launcher.subprocess, "run", timeout)

    assert launcher.backend_imports_work(python) is False
