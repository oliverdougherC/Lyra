"""Safe packaged desktop import publish, rollback, and recovery."""

from __future__ import annotations

import asyncio
import json
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


def _seed_source_checkout(root: Path) -> Path:
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
        migrate(conn)
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
    assert body["old_runtime_active"] is False
    assert body["source_lock"] == "available"
    assert body["asset_summary"]["selected_models"] == 1
    assert body["asset_summary"]["selected_caches"] == 1


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


def test_resume_refuses_same_size_source_drift(
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
    assert "changed while it was being staged" in str(failed.message)


def test_publish_cli_promotes_staged_import_and_preserves_scaffold(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _seed_source_checkout(tmp_path)
    token = _register_selection("selected-source", checkout)

    started = client.post(
        "/api/desktop-import/start",
        json={"selection_token": token, "operation_id": "publish-pass"},
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

    code, payload = _publish_cli(monkeypatch)

    assert code == 0
    assert payload == {
        "message": "Desktop import published.",
        "phase": "completed",
        "status": "ok",
    }
    assert (settings.uploads_dir / "1" / "1-lecture.pdf").read_bytes() == b"%PDF-1.7 source"
    assert (settings.models_dir / "late-model.bin").read_text(encoding="utf-8") == "kept"
    assert (settings.data_dir / ".api_key").read_text(encoding="utf-8") == "current-secret"
    assert (
        checkout / "data" / "uploads" / "1" / "1-lecture.pdf"
    ).read_bytes() == b"%PDF-1.7 source"
    conn = connect()
    try:
        allow_web_research = conn.execute(
            "select allow_web_research from settings where id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert allow_web_research == 1
    status = desktop_import_module.desktop_import_manager.status()
    assert status.status == "completed"
    assert status.requires_restart is False


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
