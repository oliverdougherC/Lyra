"""Health endpoints separate core Lyra readiness from optional web research."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_health
from backend.main import create_app
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


def test_application_registers_both_health_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/health/live" in paths
    assert "/api/health/ready" in paths


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


def test_ready_reports_optional_firecrawl_outage_without_failing_lyra(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))

    class UnavailableFirecrawl:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3002"

        def check_readiness(self) -> dict[str, object]:
            raise routes_health.FirecrawlError("contains upstream details")

    monkeypatch.setattr(routes_health, "FirecrawlClient", UnavailableFirecrawl)

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
            "firecrawl": {
                "status": "temporarily_unavailable",
                "required": False,
                "message": "Firecrawl is temporarily unavailable; web research is disabled.",
            },
            "web_scrape": {
                "status": "not_ready",
                "required": False,
                "message": "Web scraping remains disabled until the redirect-safety gate passes.",
            },
        },
    }
    assert "upstream details" not in response.text


def test_ready_reports_misconfigured_firecrawl_without_failing_lyra(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))

    class MisconfiguredFirecrawl:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3002"

        def check_readiness(self) -> dict[str, object]:
            raise routes_health.FirecrawlMisconfiguredError("wrong endpoint")

    monkeypatch.setattr(routes_health, "FirecrawlClient", MisconfiguredFirecrawl)

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["firecrawl"] == {
        "status": "misconfigured",
        "required": False,
        "message": "Firecrawl is misconfigured; web research is disabled.",
    }
    assert "wrong endpoint" not in response.text


def test_ready_reports_available_firecrawl_when_the_probe_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "lyra.db"
    _migrated_database(db_path)
    monkeypatch.setattr(routes_health, "connect", lambda: connect(db_path))

    class ReadyFirecrawl:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3002"

        def check_readiness(self) -> dict[str, object]:
            return {"status": "ok"}

    monkeypatch.setattr(routes_health, "FirecrawlClient", ReadyFirecrawl)

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["components"]["firecrawl"] == {
        "status": "available",
        "required": False,
        "message": "Firecrawl is available.",
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
        "_check_firecrawl",
        lambda _: pytest.fail("Firecrawl must be skipped when the database is not ready"),
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
            "firecrawl": {
                "status": "skipped",
                "required": False,
                "message": "Firecrawl was not checked because the database is not ready.",
            },
            "web_scrape": {
                "status": "skipped",
                "required": False,
                "message": (
                    "The web scrape policy was not checked because the database is not ready."
                ),
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
