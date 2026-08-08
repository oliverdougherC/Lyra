"""HTTP contracts for plans, source ledger visibility, and class writer overrides."""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_writer
from backend.core import artifacts, writer_plans
from backend.core.errors import LyraError
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_writer.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _draft(db: sqlite3.Connection, class_id: int) -> int:
    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    artifacts.create_part(
        db,
        int(artifact["id"]),
        artifacts.DRAFT_BODY,
        1,
        content="",
        status=artifacts.PART_COMPLETE,
    )
    return int(artifact["id"])


def test_plan_edits_create_versions_and_adapt_source_fields(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    assert client.get(f"/api/drafts/{draft_id}/plan").json() is None
    payload = {
        "brief_analysis": "Explain, then defend.",
        "thesis": "The mechanism matters.",
        "argument_map": [{"claim": "Mechanism"}],
        "sections": [
            {
                "id": 999,
                "section_ref": "1",
                "ordinal": 0,
                "title": "Mechanism",
                "job": "Establish the mechanism",
                "claim": "It explains the effect",
                "evidence": ["Course experiment"],
                "sources": [],
                "word_budget": 300,
                "research_notes": "",
            }
        ],
    }

    first = client.put(f"/api/drafts/{draft_id}/plan", json=payload)
    assert first.status_code == 200
    assert first.json()["version"] == 1
    assert first.json()["status"] == "active"
    assert first.json()["argument_map"] == [{"claim": "Mechanism"}]
    assert first.json()["sections"][0]["sources"] == []

    payload["thesis"] = "The revised mechanism matters."
    second = client.put(f"/api/drafts/{draft_id}/plan", json=payload)
    assert second.status_code == 200
    assert second.json()["version"] == 2
    assert len(writer_plans.list_plan_versions(db, draft_id)) == 2


def test_plan_api_adapts_a_legacy_argument_object_to_the_public_list(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    legacy = {
        "claims": [
            {"id": "c1", "claim": "The mechanism matters", "supports": []},
            {"id": "c2", "claim": "The evidence identifies it", "supports": ["c1"]},
        ]
    }

    written = client.put(
        f"/api/drafts/{draft_id}/plan",
        json={"thesis": "A thesis", "argument_map": legacy, "sections": []},
    )

    assert written.status_code == 200
    expected = legacy["claims"]
    assert written.json()["argument_map"] == expected
    assert client.get(f"/api/drafts/{draft_id}/plan").json()["argument_map"] == expected


def test_sources_endpoint_makes_ready_course_documents_first_class(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'reader.pdf', 'reader.pdf', 'application/pdf', 12, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()

    response = client.get(f"/api/classes/{class_id}/sources")
    assert response.status_code == 200
    source = response.json()[0]
    assert source["document_id"] == document_id
    assert source["title"] == "reader.pdf"
    assert source["source_type"] == "course"


def test_class_writer_settings_inherit_then_override(client: TestClient, class_id: int) -> None:
    inherited = client.get(f"/api/classes/{class_id}/writer-settings").json()
    assert inherited["effective"] == {
        "allow_web_research": False,
        "parallel_requests": False,
        "parallel_concurrency": 1,
    }

    changed = client.put(
        f"/api/classes/{class_id}/writer-settings",
        json={
            "allow_web_research": True,
            "parallel_requests": True,
            "parallel_concurrency": 3,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["effective"] == {
        "allow_web_research": True,
        "parallel_requests": True,
        "parallel_concurrency": 3,
    }

    partial = client.put(
        f"/api/classes/{class_id}/writer-settings",
        json={"allow_web_research": False},
    )
    assert partial.status_code == 200
    assert partial.json()["overrides"]["parallel_requests"] == 1
    assert partial.json()["overrides"]["parallel_concurrency"] == 3
    assert partial.json()["effective"] == {
        "allow_web_research": False,
        "parallel_requests": True,
        "parallel_concurrency": 3,
    }
