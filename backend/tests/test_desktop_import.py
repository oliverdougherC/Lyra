"""Safe packaged desktop import publish, rollback, and recovery."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend import desktop_entry
from backend import desktop_import as desktop_import_module
from backend.api import routes_desktop_import
from backend.config import settings
from backend.core.errors import LyraError
from backend.storage import database as database_module
from backend.storage.database import connect, migrate


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "packaged_mode", True)
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()

    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        del request
        body: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            body.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=body)

    app.include_router(routes_desktop_import.router)
    with TestClient(app) as test_client:
        yield test_client


def _seed_source_checkout(root: Path, *, maximum_migration: int | None = None) -> Path:
    checkout = root / "checkout"
    source = checkout / "data"
    (source / "uploads" / "1").mkdir(parents=True)
    (source / "text").mkdir()
    (source / "models").mkdir()
    (source / "pages").mkdir()
    (source / "uploads" / "1" / "1-lecture.pdf").write_bytes(b"%PDF-1.7 source")
    (source / "text" / "1.txt").write_text("notes", encoding="utf-8")
    (source / "models" / "manifest.txt").write_text("runtime", encoding="utf-8")
    (source / "pages" / "render.png").write_bytes(b"preview")
    (source / ".api_key").write_text("secret", encoding="utf-8")

    conn = connect(source / "lyra.db")
    try:
        if maximum_migration is None:
            migrate(conn)
        else:
            for migration_path in sorted(database_module.MIGRATIONS_DIR.glob("*.sql")):
                version = int(migration_path.name.split("_", 1)[0])
                if version > maximum_migration:
                    break
                database_module._apply_migration(conn, version, migration_path)
        class_id = int(
            conn.execute(
                "insert into classes (name, code) values ('Physics', 'PHYS 101')"
            ).lastrowid
            or 0
        )
        conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, ?, ?, ?, ?, 'ready')",
            (class_id, "lecture.pdf", "data/uploads/1/1-lecture.pdf", "application/pdf", 15),
        )
        conn.commit()
    finally:
        conn.close()
    return checkout


def _wait_for_status(expected: str, timeout_seconds: float = 5.0):
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = desktop_import_module.desktop_import_manager.status()
        if latest.status == expected:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for status {expected!r}; saw {latest!r}")


def _register_selection(token: str, checkout: Path) -> str:
    directory = settings.data_dir.parent / ".desktop-import-selections"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{token}.json"
    path.write_text(
        json.dumps({"path": str(checkout), "label": "Imported checkout"}) + "\n",
        encoding="utf-8",
    )
    return token


def _publish_cli(monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, object]]:
    stream = StringIO()
    monkeypatch.setattr(desktop_entry.sys, "argv", ["desktop-entry", "--publish-desktop-import"])
    code = desktop_entry.main(stream=stream)
    payload = json.loads(stream.getvalue().strip())
    return code, payload


def _disable_lifespan_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import main as main_module

    monkeypatch.setattr(main_module, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.solver, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.study, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.drafting, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.embedding_server, "stop_for_app_quit", lambda: None)
    monkeypatch.setattr(main_module.ocr_server, "stop_for_app_quit", lambda: None)
    monkeypatch.setattr(main_module.rerank_server, "stop_for_app_quit", lambda: None)


def test_preview_accepts_a_checkout_root(client: TestClient, tmp_path: Path) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)

    response = client.post("/api/desktop-import/preview", json={"selection_token": token})

    assert response.status_code == 200
    body = response.json()
    assert body["source_name"] == "Imported checkout"
    assert body["source_kind"] == "checkout_root"
    assert body["class_count"] == 1
    assert body["document_count"] == 1
    assert "uploads/1/1-lecture.pdf" in body["sample_entries"]
    assert body["schema_version"] > 0
    assert body["database_identity"].startswith("lyra-db:")
    assert body["conflicts"] == []
    assert body["warnings"][0].startswith("Preview stays read-only")
    assert body["old_runtime_active"] is None
    assert body["source_lock"] == "read_only"
    assert body["asset_summary"]["selected_models"] == 1
    assert body["asset_summary"]["selected_caches"] == 1


def test_preview_does_not_use_writable_sqlite_connect(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("readonly-source", checkout)
    source_db = checkout / "data" / "lyra.db"
    original_connect = desktop_import_module.connect

    def fail_source_connect(db_path=None):
        if db_path == source_db:
            raise AssertionError("preview should stay read-only")
        return original_connect(db_path)

    monkeypatch.setattr(desktop_import_module, "connect", fail_source_connect)

    response = client.post("/api/desktop-import/preview", json={"selection_token": token})

    assert response.status_code == 200


def test_preview_does_not_mutate_a_read_only_source(client: TestClient, tmp_path: Path) -> None:
    checkout = _seed_source_checkout(tmp_path)
    source = checkout / "data"
    source_db = source / "lyra.db"
    family = [source_db.with_name(source_db.name + suffix) for suffix in ("", "-wal", "-shm")]
    token = _register_selection("filesystem-readonly-source", checkout)

    for path in family:
        if path.exists():
            path.chmod(0o444)
    source.chmod(0o555)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_mode & 0o777)
        for path in family
        if path.exists()
    }
    try:
        response = client.post("/api/desktop-import/preview", json={"selection_token": token})
        started = client.post(
            "/api/desktop-import/start",
            json={"selection_token": token, "operation_id": "read-only-stage"},
        )
        _wait_for_status("staged")
        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_mode & 0o777)
            for path in family
            if path.exists()
        }
        assert response.status_code == 200
        assert started.status_code == 200
        assert after == before
        assert source.stat().st_mode & 0o777 == 0o555
    finally:
        source.chmod(0o700)
        for path in family:
            if path.exists():
                path.chmod(0o600)


def test_read_only_snapshot_fails_if_an_existing_writer_changes_the_wal(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    source = checkout / "data"
    source_db = source / "lyra.db"
    writer = sqlite3.connect(str(source_db), isolation_level=None, check_same_thread=False)
    writer.execute("pragma journal_mode = wal")
    family = [source_db.with_name(source_db.name + suffix) for suffix in ("", "-wal", "-shm")]
    token = _register_selection("readonly-live-writer", checkout)
    original_copy = desktop_import_module.copy_regular_file
    wrote = threading.Event()

    def write_during_copy(source_path: Path, destination: Path) -> None:
        if source_path.name == "1-lecture.pdf" and not wrote.is_set():
            writer.execute("insert into classes (name, code) values ('Late', 'LATE 1')")
            writer.commit()
            wrote.set()
        original_copy(source_path, destination)

    monkeypatch.setattr(desktop_import_module, "copy_regular_file", write_during_copy)
    for path in family:
        if path.exists():
            path.chmod(0o444)
    source.chmod(0o555)
    try:
        started = client.post(
            "/api/desktop-import/start",
            json={"selection_token": token, "operation_id": "readonly-live-writer-pass"},
        )
        failed = _wait_for_status("failed")
        assert started.status_code == 200
        assert wrote.is_set()
        assert "read-only Lyra database changed" in str(failed.message)
    finally:
        source.chmod(0o700)
        for path in family:
            if path.exists():
                path.chmod(0o600)
        writer.close()


def test_pre_032_data_is_staged_and_migrated_safely(client: TestClient, tmp_path: Path) -> None:
    checkout = _seed_source_checkout(tmp_path, maximum_migration=31)
    token = _register_selection("legacy-source", checkout)

    preview = client.post("/api/desktop-import/preview", json={"selection_token": token})
    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "legacy-stage"},
    )

    assert preview.status_code == 200
    assert preview.json()["schema_version"] == 31
    assert started.status_code == 200
    _wait_for_status("staged")
    with sqlite3.connect(str(desktop_import_module._stage_db_path())) as staged:
        assert staged.execute("pragma user_version").fetchone()[0] == (
            desktop_import_module._latest_migration_version()
        )
        assert staged.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'storage_intents'"
        ).fetchone() == (1,)


def test_import_stages_without_publishing_and_preserves_the_source(
    client: TestClient, tmp_path: Path
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "pla-330"},
    )
    assert started.status_code == 200

    staged = _wait_for_status("staged")
    assert staged.phase == "awaiting_publish"
    assert staged.requires_restart is True
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    stage_file = (
        settings.data_dir.parent
        / ".desktop-import-stage"
        / "data"
        / "uploads"
        / "1"
        / "1-lecture.pdf"
    )
    assert stage_file.read_bytes() == b"%PDF-1.7 source"
    assert (
        checkout / "data" / "uploads" / "1" / "1-lecture.pdf"
    ).read_bytes() == b"%PDF-1.7 source"

    conn = connect(desktop_import_module._stage_db_path())
    try:
        stored_path = conn.execute("select stored_path from documents where id = 1").fetchone()[0]
    finally:
        conn.close()
    assert stored_path == str(settings.uploads_dir / "1" / "1-lecture.pdf")


def test_snapshot_includes_a_transaction_committed_to_the_live_wal(
    client: TestClient, tmp_path: Path
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    source_db = checkout / "data" / "lyra.db"
    writer = sqlite3.connect(str(source_db), isolation_level=None)
    try:
        writer.execute("pragma journal_mode = wal")
        writer.execute("insert into classes (name, code) values ('Signals', 'ECE 203')")
        writer.commit()
        token = _register_selection("wal-source", checkout)

        response = client.post(
            "/api/desktop-import/start",
            json={"selection_token": token, "operation_id": "wal-pass"},
        )
        assert response.status_code == 200
        _wait_for_status("staged")
    finally:
        writer.close()

    staged = sqlite3.connect(str(desktop_import_module._stage_db_path()))
    try:
        assert staged.execute("select count(*) from classes").fetchone()[0] == 2
    finally:
        staged.close()


def test_first_pass_rejects_source_content_that_changes_during_copy(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("drifting-source", checkout)
    original = desktop_import_module.copy_regular_file

    def drift_then_copy(source: Path, destination: Path) -> None:
        if source.name == "1-lecture.pdf":
            source.write_bytes(b"Z" * len(b"%PDF-1.7 source"))
        original(source, destination)

    monkeypatch.setattr(desktop_import_module, "copy_regular_file", drift_then_copy)

    response = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "drift-pass"},
    )
    assert response.status_code == 200

    failed = _wait_for_status("failed")
    assert "did not match the snapshot" in str(failed.message)


def test_cancel_then_resume_recopies_same_size_stage_corruption(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)
    started_copy = threading.Event()
    release_copy = threading.Event()
    original = desktop_import_module.copy_regular_file

    def slow_copy(source: Path, destination: Path) -> None:
        if source.name == "1-lecture.pdf" and not started_copy.is_set():
            started_copy.set()
            release_copy.wait(timeout=2)
        original(source, destination)

    monkeypatch.setattr(desktop_import_module, "copy_regular_file", slow_copy)

    response = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "first-pass"},
    )
    assert response.status_code == 200
    assert started_copy.wait(timeout=2)

    cancelled = client.post("/api/desktop-import/cancel")
    assert cancelled.status_code == 200
    release_copy.set()

    paused = _wait_for_status("cancelled")
    assert paused.can_resume is True
    stage_file = (
        settings.data_dir.parent
        / ".desktop-import-stage"
        / "data"
        / "uploads"
        / "1"
        / "1-lecture.pdf"
    )
    stage_file.write_bytes(b"Z" * len(b"%PDF-1.7 source"))

    resumed = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "second-pass"},
    )
    assert resumed.status_code == 200

    staged = _wait_for_status("staged")
    assert staged.phase == "awaiting_publish"
    assert stage_file.read_bytes() == b"%PDF-1.7 source"


def test_source_drift_requires_discard_then_allows_fresh_same_folder_import(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)
    started_copy = threading.Event()
    release_copy = threading.Event()
    original = desktop_import_module.copy_regular_file

    def slow_copy(source: Path, destination: Path) -> None:
        if source.name == "1-lecture.pdf" and not started_copy.is_set():
            started_copy.set()
            release_copy.wait(timeout=2)
        original(source, destination)

    monkeypatch.setattr(desktop_import_module, "copy_regular_file", slow_copy)

    response = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "first-pass"},
    )
    assert response.status_code == 200
    assert started_copy.wait(timeout=2)
    client.post("/api/desktop-import/cancel")
    release_copy.set()
    _wait_for_status("cancelled")

    source_pdf = checkout / "data" / "uploads" / "1" / "1-lecture.pdf"
    source_pdf.write_bytes(b"Y" * len(b"%PDF-1.7 source"))

    resumed = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "second-pass"},
    )
    assert resumed.status_code == 200

    failed = _wait_for_status("failed")
    assert "Discard the staged attempt" in str(failed.message)

    discarded = client.post("/api/desktop-import/reset")
    restarted = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "third-pass"},
    )

    assert discarded.status_code == 200
    assert "discarded" in discarded.json()["message"]
    assert restarted.status_code == 200
    staged = _wait_for_status("staged")
    assert staged.phase == "awaiting_publish"
    stage_file = (
        settings.data_dir.parent
        / ".desktop-import-stage"
        / "data"
        / "uploads"
        / "1"
        / "1-lecture.pdf"
    )
    assert stage_file.read_bytes() == b"Y" * len(b"%PDF-1.7 source")


def test_resume_headroom_counts_only_missing_stage_bytes(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("headroom-source", checkout)
    started_copy = threading.Event()
    release_copy = threading.Event()
    original = desktop_import_module.copy_regular_file

    def slow_copy(source: Path, destination: Path) -> None:
        original(source, destination)
        if source.name == "1-lecture.pdf" and not started_copy.is_set():
            started_copy.set()
            release_copy.wait(timeout=2)

    monkeypatch.setattr(desktop_import_module, "copy_regular_file", slow_copy)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "headroom-pass-1"},
    )
    assert started.status_code == 200
    assert started_copy.wait(timeout=2)
    cancelled = client.post("/api/desktop-import/cancel")
    assert cancelled.status_code == 200
    release_copy.set()
    _wait_for_status("cancelled")

    snapshot = desktop_import_module._snapshot_entries(checkout / "data")
    total_bytes = desktop_import_module._manifest_total_bytes(
        snapshot,
        checkout / "data" / "lyra.db",
    )
    plan = desktop_import_module._import_space_plan(
        total_bytes,
        source_data_dir=checkout / "data",
        snapshot=snapshot,
    )
    real_usage = shutil.disk_usage(settings.data_dir.parent)
    fake_usage = type(real_usage)(
        real_usage.total,
        max(0, real_usage.total - (plan.required_free_bytes + 1)),
        plan.required_free_bytes + 1,
    )
    monkeypatch.setattr(desktop_import_module.shutil, "disk_usage", lambda _path: fake_usage)

    resumed = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "headroom-pass-2"},
    )

    assert resumed.status_code == 200
    _wait_for_status("staged")


def test_large_partial_resume_fits_constrained_free_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    total_bytes = 5 * 1024**3
    already_staged = total_bytes - 256 * 1024**2
    source_data_dir = tmp_path / "large-source"
    source_data_dir.mkdir()
    live_model = settings.models_dir / "large-model.gguf"
    with live_model.open("wb") as model_file:
        model_file.truncate(already_staged)
    staged_model = desktop_import_module._stage_data_path() / "models" / live_model.name
    staged_model.parent.mkdir(parents=True)
    with staged_model.open("wb") as model_file:
        model_file.truncate(already_staged)
    desktop_import_module._write_stage_manifest(
        {
            "version": desktop_import_module.MANIFEST_VERSION,
            "source_data_dir": str(source_data_dir),
            "entries": [],
            "staged_database": None,
        }
    )
    plan = desktop_import_module._import_space_plan(
        total_bytes,
        source_data_dir=source_data_dir,
        snapshot=[],
    )
    usage = shutil.disk_usage(settings.data_dir.parent)
    constrained = type(usage)(
        usage.total,
        max(0, usage.total - plan.required_free_bytes),
        plan.required_free_bytes,
    )
    monkeypatch.setattr(desktop_import_module.shutil, "disk_usage", lambda _path: constrained)

    desktop_import_module._assert_headroom(
        total_bytes,
        source_data_dir=source_data_dir,
        snapshot=[],
    )

    assert plan.already_staged_bytes == already_staged
    assert plan.missing_stage_bytes == 256 * 1024**2
    assert plan.required_free_bytes < total_bytes


def test_publish_cli_promotes_staged_import_and_preserves_scaffold(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)
    (settings.models_dir / "stale-model.bin").write_text("drop-me", encoding="utf-8")
    (settings.data_dir / ".api_key").write_text("stale-secret", encoding="utf-8")

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "publish-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    # The install stays usable after staging: every profile change made now
    # must survive publication, even though the frozen stage predates it.
    (settings.models_dir / "late-model.bin").write_text("kept", encoding="utf-8")
    (settings.models_dir / "stale-model.bin").unlink()
    (settings.data_dir / ".api_key").write_text("current-secret", encoding="utf-8")
    (settings.data_dir / ".exa_api_key").write_text("current-exa-secret", encoding="utf-8")
    conn = connect()
    try:
        conn.execute("update settings set allow_web_research = 1 where id = 1")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(checkout)

    code, payload = _publish_cli(monkeypatch)

    assert code == 0
    assert payload == {
        "message": "Desktop import published.",
        "phase": "completed",
        "status": "ok",
    }
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"
    assert (settings.text_dir / "1.txt").read_text(encoding="utf-8") == "notes"
    assert (settings.models_dir / "late-model.bin").read_text(encoding="utf-8") == "kept"
    assert (settings.models_dir / "stale-model.bin").exists() is False
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    assert (settings.data_dir / ".exa_api_key").read_text(encoding="utf-8") == "current-exa-secret"
    conn = connect()
    try:
        allow_web_research = conn.execute(
            "select allow_web_research from settings where id = 1"
        ).fetchone()[0]
        class_names = [
            row[0] for row in conn.execute("select name from classes order by id").fetchall()
        ]
    finally:
        conn.close()
    assert allow_web_research == 1
    assert class_names == ["Physics"]
    assert desktop_import_module._publish_recovery_record_path().exists() is False
    assert desktop_import_module._publish_recovery_root_path().exists() is False
    assert desktop_import_module._stage_root_path().exists() is False
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "completed"
    assert status.requires_restart is False


def test_publish_cli_uses_verified_stage_after_source_removal(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("removed-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "source-removed-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")
    shutil.rmtree(checkout)

    code, payload = _publish_cli(monkeypatch)

    assert code == 0
    assert payload["status"] == "ok"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"


def test_publish_rollback_restores_live_scaffold_and_keeps_stage(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)
    (settings.models_dir / "scaffold.txt").write_text("keep-me", encoding="utf-8")

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "rollback-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    monkeypatch.setattr(
        desktop_import_module,
        "_verify_live_import",
        lambda: (_ for _ in ()).throw(RuntimeError("publish broke")),
    )

    code, payload = _publish_cli(monkeypatch)

    assert code == 1
    assert payload["status"] == "error"
    assert payload["message"] == "publish broke"
    assert (settings.models_dir / "scaffold.txt").read_text(encoding="utf-8") == "keep-me"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    assert desktop_import_module._stage_ready_for_publish() is True
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "staged"
    assert status.phase == "awaiting_publish"


def test_publish_rollback_after_late_profile_refresh_keeps_live_profile(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("late-rollback-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "late-rollback-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    (settings.models_dir / "late-model.bin").write_text("kept", encoding="utf-8")
    (settings.data_dir / ".api_key").write_text("current-secret", encoding="utf-8")
    monkeypatch.setattr(
        desktop_import_module,
        "_verify_live_import",
        lambda: (_ for _ in ()).throw(RuntimeError("publish broke")),
    )

    code, payload = _publish_cli(monkeypatch)

    assert code == 1
    assert payload["status"] == "error"
    assert payload["message"] == "publish broke"
    assert (settings.models_dir / "late-model.bin").read_text(encoding="utf-8") == "kept"
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    assert desktop_import_module._stage_ready_for_publish() is True
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "staged"
    assert status.phase == "awaiting_publish"


def test_publish_fails_closed_when_profile_refresh_breaks(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("refresh-failure-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "refresh-failure-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    (settings.models_dir / "late-model.bin").write_text("kept", encoding="utf-8")
    (settings.data_dir / ".api_key").write_text("current-secret", encoding="utf-8")
    monkeypatch.setattr(
        desktop_import_module,
        "_merge_destination_profile",
        lambda _stage_db: (_ for _ in ()).throw(RuntimeError("refresh broke")),
    )

    code, payload = _publish_cli(monkeypatch)

    assert code == 1
    assert payload["status"] == "error"
    assert payload["message"] == "refresh broke"
    assert (settings.models_dir / "late-model.bin").read_text(encoding="utf-8") == "kept"
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    assert settings.db_path.exists()
    assert desktop_import_module._publish_recovery_record_path().exists() is False
    assert desktop_import_module._publish_recovery_root_path().exists() is False
    assert desktop_import_module._stage_root_path().exists()
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "failed"


def test_publish_fails_closed_when_post_refresh_verification_fails(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("reverify-failure-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "reverify-failure-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    (settings.data_dir / ".api_key").write_text("current-secret", encoding="utf-8")
    original_verify = desktop_import_module._verify_staged_import
    calls = {"count": 0}

    def fail_reverification(stage_data: Path, stage_db: Path) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise RuntimeError("reverify broke")
        original_verify(stage_data, stage_db)

    monkeypatch.setattr(desktop_import_module, "_verify_staged_import", fail_reverification)

    code, payload = _publish_cli(monkeypatch)

    assert code == 1
    assert payload["status"] == "error"
    assert payload["message"] == "reverify broke"
    assert calls["count"] == 2
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    assert settings.db_path.exists()
    assert desktop_import_module._publish_recovery_record_path().exists() is False
    assert desktop_import_module._publish_recovery_root_path().exists() is False
    assert desktop_import_module._stage_root_path().exists()


def test_publish_refuses_when_current_install_gained_user_data(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("late-user-data-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "late-user-data-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    conn = connect()
    try:
        conn.execute("insert into classes (name, code) values ('Late Class', 'LATE 1')")
        conn.commit()
    finally:
        conn.close()
    (settings.uploads_dir / "late-upload.pdf").write_bytes(b"%PDF-1.7 late")

    code, payload = _publish_cli(monkeypatch)

    assert code == 1
    assert payload["status"] == "error"
    assert "already contains Lyra data" in str(payload["message"])
    conn = connect()
    try:
        class_names = [
            row[0] for row in conn.execute("select name from classes order by id").fetchall()
        ]
    finally:
        conn.close()
    assert class_names == ["Late Class"]
    assert (settings.uploads_dir / "late-upload.pdf").read_bytes() == b"%PDF-1.7 late"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() is False
    assert desktop_import_module._stage_root_path().exists()
    assert desktop_import_module._publish_recovery_record_path().exists() is False


def test_publish_recovery_completes_with_refreshed_profile_after_interruption(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("refreshed-recovery-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "refreshed-recovery-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    (settings.models_dir / "late-model.bin").write_text("kept", encoding="utf-8")
    (settings.data_dir / ".api_key").write_text("current-secret", encoding="utf-8")
    conn = connect()
    try:
        conn.execute("update settings set allow_web_research = 1 where id = 1")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(checkout)

    # Simulate the shell dying between the profile refresh and the swap: the
    # stage is refreshed, recovery is initialized, and live data is backed up.
    manifest = desktop_import_module._read_stage_manifest()
    assert manifest is not None
    desktop_import_module._refresh_current_profile_into_stage(manifest)
    desktop_import_module._initialize_publish_recovery()
    settings.data_dir.replace(desktop_import_module._publish_backup_data_path())
    desktop_import_module._patch_publish_recovery_record(phase="live_data_backed_up")

    code, payload = _publish_cli(monkeypatch)

    assert code == 0
    assert payload["status"] == "ok"
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"
    assert (settings.models_dir / "late-model.bin").read_text(encoding="utf-8") == "kept"
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    conn = connect()
    try:
        allow_web_research = conn.execute(
            "select allow_web_research from settings where id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert allow_web_research == 1
    assert desktop_import_module._publish_recovery_record_path().exists() is False
    assert desktop_import_module._publish_recovery_root_path().exists() is False
    assert desktop_import_module._stage_root_path().exists() is False

    repeat_code, repeat_payload = _publish_cli(monkeypatch)

    assert repeat_code == 0
    assert repeat_payload["status"] == "ok"
    assert repeat_payload["message"] == "Desktop import was already published."


def test_startup_recovery_finishes_an_interrupted_external_db_publish(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)
    external_db = tmp_path / "external-db" / "lyra.db"
    monkeypatch.setattr(settings, "db_path", external_db)
    settings.ensure_directories()
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "recovery-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    desktop_import_module._initialize_publish_recovery()
    settings.data_dir.replace(desktop_import_module._publish_backup_data_path())
    desktop_import_module._move_db_family(
        settings.db_path, desktop_import_module._publish_backup_db_path()
    )
    desktop_import_module._patch_publish_recovery_record(phase="live_db_backed_up")

    _disable_lifespan_workers(monkeypatch)
    from backend import main as main_module

    async def run_lifespan() -> None:
        async with main_module.lifespan(None):  # type: ignore[arg-type]
            pass

    asyncio.run(run_lifespan())

    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"
    assert settings.db_path.exists()
    assert desktop_import_module._publish_recovery_record_path().exists() is False
    assert desktop_import_module._publish_recovery_root_path().exists() is False
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "completed"


def test_non_pristine_destination_reports_conflicts(client: TestClient, db) -> None:
    db.execute("insert into classes (name, code) values ('Existing', 'EXIST 1')")
    db.commit()

    response = client.get("/api/desktop-import/status")

    assert response.status_code == 200
    body = response.json()
    assert body["destination_ready"] is False
    assert body["conflicts"]


def test_reset_discards_stage_idempotently(client: TestClient, tmp_path: Path) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("reset-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "reset-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")

    first = client.post("/api/desktop-import/reset")
    second = client.post("/api/desktop-import/reset")

    assert first.status_code == 200
    assert first.json()["status"] == "idle"
    assert "discarded" in first.json()["message"]
    assert second.status_code == 200
    assert second.json()["status"] == "idle"
    assert "No staged import was waiting" in second.json()["message"]
    assert desktop_import_module._stage_root_path().exists() is False
    assert (checkout / "data" / "uploads" / "1" / "1-lecture.pdf").read_bytes() == (
        b"%PDF-1.7 source"
    )


def test_reset_preserves_in_progress_publication_recovery(
    client: TestClient, tmp_path: Path
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("recovery-reset-source", checkout)
    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "recovery-reset-pass"},
    )
    assert started.status_code == 200
    _wait_for_status("staged")
    desktop_import_module._initialize_publish_recovery()

    response = client.post("/api/desktop-import/reset")

    assert response.status_code == 409
    assert desktop_import_module._stage_root_path().exists()
    assert desktop_import_module._publish_recovery_record_path().exists()


def _configure_import_tutor(
    monkeypatch,
    *,
    fallback=False,
    endpoint="https://current.example/v1",
    value="synthetic-current-key",
):
    import keyring.errors

    from backend.storage import private, secrets

    # This helper installs a new backend: do not inherit reachability cached by a
    # different test/backend. Missing storage is distinct from a locked Keychain.
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    if fallback:

        def unavailable(*_args, **_kwargs):
            raise keyring.errors.NoKeyringError("synthetic missing keychain backend")

        monkeypatch.setattr(secrets, "_keyring_call", unavailable)
    private.write_private_text(
        settings.data_dir / ".tutor_credential_generation", "current-generation"
    )
    identity = secrets.stage_tutor_credential(endpoint, value)
    conn = connect()
    try:
        conn.execute(
            "update settings set endpoint_url=?, model='synthetic', tutor_credential_id=?, "
            "remote_ack=1 where id=1",
            (endpoint, identity),
        )
        conn.commit()
    finally:
        conn.close()
    return identity


def _assert_import_tutor_works(identity, endpoint="https://current.example/v1"):
    import httpx

    from backend.core.app_settings import resolve_tutor_config
    from backend.llm import client as llm_client

    conn = connect()
    try:
        config = resolve_tutor_config(conn)
        assert config.credential_id == identity
        assert config.endpoint_url == endpoint
    finally:
        conn.close()

    def provider(request):
        assert request.headers["Authorization"] == "Bearer synthetic-current-key"
        assert str(request.url) == endpoint + "/models"
        return httpx.Response(200, json={"data": [{"id": "synthetic"}]})

    assert asyncio.run(
        llm_client.list_models(
            config.endpoint_url, config.api_key, transport=httpx.MockTransport(provider)
        )
    ) == ["synthetic"]


@pytest.mark.parametrize("fallback", [False, True], ids=["keychain", "file"])
@pytest.mark.parametrize("maximum_migration", [18, None], ids=["older-schema", "current-schema"])
def test_import_preserves_active_credential_snapshot_and_space_accounting(
    client,
    tmp_path,
    monkeypatch,
    isolated_keychain,
    fallback,
    maximum_migration,
):
    from backend.storage import private

    checkout = _seed_source_checkout(tmp_path, maximum_migration=maximum_migration)
    isolated_keychain[("unrelated-service", "unrelated-account")] = "synthetic-unrelated-key"
    identity = _configure_import_tutor(monkeypatch, fallback=fallback)
    private.write_private_text(settings.data_dir / ".api_key.authority", "deleted")
    private.write_private_text(settings.data_dir / ".exa_api_key.authority", "deleted")
    expected = [
        Path("credentials") / f"{identity}.json",
        Path(".tutor_credential_generation"),
        Path(".api_key.authority"),
        Path(".exa_api_key.authority"),
    ]
    credential_bytes = sum((settings.data_dir / path).stat().st_size for path in expected)
    assert desktop_import_module._profile_preserve_bytes() >= credential_bytes
    token = _register_selection("credential-source", checkout)
    assert (
        client.post(
            "/api/desktop-import/start",
            json={
                "selection_token": token,
                "operation_id": "credential-stage",
            },
        ).status_code
        == 200
    )
    _wait_for_status("staged")
    for path in expected:
        copied = desktop_import_module._stage_data_path() / path
        assert copied.read_bytes() == (settings.data_dir / path).read_bytes()
        assert copied.stat().st_mode & 0o777 == 0o600
    assert desktop_import_module._staged_profile_bytes() >= credential_bytes
    code, _ = _publish_cli(monkeypatch)
    assert code == 0
    _assert_import_tutor_works(identity)
    assert (
        isolated_keychain[("unrelated-service", "unrelated-account")] == "synthetic-unrelated-key"
    )
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"


@pytest.mark.parametrize("forget_after_staging", [False, True])
def test_import_preserves_latest_tutor_and_exa_forget(
    client,
    tmp_path,
    monkeypatch,
    isolated_keychain,
    forget_after_staging,
):
    from backend.storage import private, secrets

    checkout = _seed_source_checkout(tmp_path)
    # Reproduce the positive probe cache left by another credential test.
    monkeypatch.setattr(secrets, "_keyring_ok", True)
    identity = _configure_import_tutor(monkeypatch, fallback=True)
    private.write_private_text(settings.data_dir / ".exa_api_key", "synthetic-old-exa")
    private.write_private_text(settings.data_dir / ".exa_api_key.authority", "file")

    def forget():
        secrets.forget_tutor_credentials()
        secrets.delete_exa_api_key()

    if not forget_after_staging:
        forget()
    token = _register_selection("forget-source", checkout)
    assert (
        client.post(
            "/api/desktop-import/start",
            json={
                "selection_token": token,
                "operation_id": "forget-stage",
            },
        ).status_code
        == 200
    )
    _wait_for_status("staged")
    if forget_after_staging:
        forget()
    generation = (settings.data_dir / ".tutor_credential_generation").read_bytes()
    assert _publish_cli(monkeypatch)[0] == 0
    assert (settings.data_dir / ".tutor_credential_generation").read_bytes() == generation
    assert secrets.get_tutor_credential(identity, "https://current.example/v1") is None
    assert secrets.get_exa_api_key() is None
    assert (settings.data_dir / ".api_key.authority").read_text() == "deleted"
    assert (settings.data_dir / ".exa_api_key.authority").read_text() == "deleted"


@pytest.mark.parametrize("publication", ["ordinary", "recovery", "rollback"])
def test_late_credential_change_survives_import_publication_recovery_and_rollback(
    client,
    tmp_path,
    monkeypatch,
    isolated_keychain,
    publication,
):
    checkout = _seed_source_checkout(tmp_path)
    _configure_import_tutor(
        monkeypatch,
        fallback=True,
        endpoint="https://earlier.example/v1",
        value="synthetic-earlier-key",
    )
    token = _register_selection("late-credential-source", checkout)
    assert (
        client.post(
            "/api/desktop-import/start",
            json={
                "selection_token": token,
                "operation_id": "late-credential-stage",
            },
        ).status_code
        == 200
    )
    _wait_for_status("staged")
    identity = _configure_import_tutor(monkeypatch, fallback=True)
    if publication == "recovery":
        manifest = desktop_import_module._read_stage_manifest()
        desktop_import_module._refresh_current_profile_into_stage(manifest)
        desktop_import_module._initialize_publish_recovery()
        settings.data_dir.replace(desktop_import_module._publish_backup_data_path())
        desktop_import_module._patch_publish_recovery_record(phase="live_data_backed_up")
    elif publication == "rollback":
        monkeypatch.setattr(
            desktop_import_module,
            "_verify_live_import",
            lambda: (_ for _ in ()).throw(RuntimeError("injected publish failure")),
        )
    code, _ = _publish_cli(monkeypatch)
    assert code == (1 if publication == "rollback" else 0)
    _assert_import_tutor_works(identity)
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").exists() == (publication != "rollback")
    if publication == "rollback":
        assert desktop_import_module._stage_ready_for_publish()


def test_import_refuses_symlinked_live_credential_before_publication(
    client,
    tmp_path,
    monkeypatch,
):
    checkout = _seed_source_checkout(tmp_path)
    identity = _configure_import_tutor(monkeypatch, fallback=True)
    token = _register_selection("symlink-credential-source", checkout)
    assert (
        client.post(
            "/api/desktop-import/start",
            json={
                "selection_token": token,
                "operation_id": "symlink-credential-stage",
            },
        ).status_code
        == 200
    )
    _wait_for_status("staged")
    outside = tmp_path / "unapproved-record.json"
    outside.write_text("synthetic outside sentinel")
    record = settings.data_dir / "credentials" / f"{identity}.json"
    record.unlink()
    record.symlink_to(outside)
    assert _publish_cli(monkeypatch)[0] == 1
    assert outside.read_text() == "synthetic outside sentinel"
    assert not (settings.uploads_dir / "1" / "1-lecture.pdf").exists()


def test_stage_preserves_profile_files_without_local_model_directory(tmp_path, monkeypatch):
    """Packaged model overrides leave no models folder to create the stage implicitly."""
    data = tmp_path / "destination" / "profile"
    data.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    marker = data / ".permissions-hardened"
    marker.write_text("synthetic-private-profile", encoding="utf-8")
    desktop_import_module._copy_profile_into_stage()
    staged = desktop_import_module._stage_data_path() / marker.name
    assert staged.read_text(encoding="utf-8") == marker.read_text(encoding="utf-8")
    assert staged.parent.stat().st_mode & 0o777 == 0o700
