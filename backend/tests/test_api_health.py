"""Health endpoints separate core Lyra readiness from optional web research."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_health
from backend.config import settings
from backend.core.origins import LOOPBACK_CLIENT_HEADER
from backend.desktop_bootstrap import SESSION_HEADER
from backend.main import create_app
from backend.storage import secrets
from backend.storage.database import connect, migrate


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes_health.router)
    with TestClient(app) as test_client:
        yield test_client


def _migrated_database(path: Path) -> None:
    conn = connect(path)
    try:
        migrate(conn)
    finally:
        conn.close()


def test_application_registers_the_health_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/health/live" in paths
    assert "/api/health/ready" in paths
    assert "/api/health/diagnostics" in paths


def test_diagnostics_returns_a_redacted_bundle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _migrated_database(settings.db_path)
    monkeypatch.setattr(secrets, "has_api_key", lambda: False)
    monkeypatch.setattr(secrets, "api_key_storage", lambda: "keychain")
    monkeypatch.setattr(secrets, "has_exa_api_key", lambda: False)
    monkeypatch.setattr(secrets, "exa_api_key_storage", lambda: "keychain")

    response = client.get("/api/health/diagnostics")

    assert response.status_code == 200
    body = response.json()
    assert body["schema"]["current"] is True
    assert body["api_key"] == {"present": False, "storage": "keychain"}
    assert body["web_research"]["exa_key_present"] is False
    assert body["web_research"]["exa_key_storage"] == "keychain"


def test_live_does_not_probe_dependencies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_health,
        "_check_database",
        lambda: pytest.fail("liveness must not touch the database"),
    )

    response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_does_not_open_the_keychain_or_probe_exa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))
    monkeypatch.setattr(
        secrets,
        "has_exa_api_key",
        lambda: pytest.fail("readiness must not inspect provider credentials"),
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {
            "database": {
                "status": "ready",
                "required": True,
                "message": "Database is ready.",
            },
            "web_research": {
                "status": "not_ready",
                "required": False,
                "message": "Web research is configured but currently disabled in Settings.",
            },
        },
    }


def test_ready_reports_web_research_disabled_even_when_a_key_exists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["web_research"] == {
        "status": "not_ready",
        "required": False,
        "message": "Web research is configured but currently disabled in Settings.",
    }


def test_packaged_ready_requires_the_session_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))
    monkeypatch.setattr(settings, "packaged_mode", True)
    client = TestClient(create_app(session_secret="a" * 64))

    rejected = client.get("/api/health/ready", headers={"host": "127.0.0.1:8000"})
    accepted = client.get(
        "/api/health/ready",
        headers={"host": "127.0.0.1:8000", SESSION_HEADER: "a" * 64},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ready"


def test_packaged_shutdown_uses_the_registered_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "packaged_mode", True)
    client = TestClient(create_app(session_secret="a" * 64))
    calls: list[str] = []
    client.app.state.request_shutdown = lambda: calls.append("requested")

    response = client.post(
        "/api/health/shutdown",
        headers={
            "host": "127.0.0.1:8000",
            SESSION_HEADER: "a" * 64,
            LOOPBACK_CLIENT_HEADER: "desktop-shell",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"status": "stopping"}
    assert calls == ["requested"]


def test_ready_reports_web_research_ready_without_contacting_exa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    conn = connect(db_path)
    try:
        conn.execute("update settings set allow_web_research = 1 where id = 1")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["web_research"] == {
        "status": "available",
        "required": False,
        "message": (
            "Web research is enabled. Credential presence and connectivity are checked only "
            "when Settings or an explicit Exa action requests them."
        ),
    }


def test_ready_returns_503_when_migrations_are_behind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("pragma user_version = 0")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))
    monkeypatch.setattr(
        routes_health,
        "_web_research_component",
        lambda **_: pytest.fail("web research must be skipped when the database is not ready"),
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {
            "database": {
                "status": "not_ready",
                "required": True,
                "message": "Database migrations are not current.",
            },
            "web_research": {
                "status": "skipped",
                "required": False,
                "message": "Web research was not checked because the database is not ready.",
            },
        },
    }


def test_ready_redacts_database_failure_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_connect() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open /Users/private/secret.db")

    monkeypatch.setattr(routes_health, "connect", fail_connect)

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["database"] == {
        "status": "unavailable",
        "required": True,
        "message": "Database is unavailable.",
    }
    assert "/Users/private/secret.db" not in response.text


def test_update_schema_reads_actual_profile_version(client: TestClient) -> None:
    with connect() as conn:
        conn.execute("pragma user_version = 1")
    response = client.get("/api/health/update-schema")
    assert response.status_code == 200
    assert response.json() == {"version": 1}
