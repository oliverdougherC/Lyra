"""Contract tests for the draft endpoints.

The drafting worker is never started here: `drafting.enqueue` is stubbed so `/suggest`
stays a pure write. `/write` streams from a stubbed `stream_chat`. This file is the HTTP
surface; the hunk math is test_suggestions.py and the run is test_drafting.py.
"""

import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import artifacts, drafting, suggestions
from backend.core.errors import LyraError
from backend.llm.client import StreamDelta
from backend.storage.database import connect, get_db

BASE = (
    "# Essay\n"
    "\n"
    "The delta function is even.\n"
    "Its sifting property picks out x(0).\n"
    "\n"
    "## Scaling\n"
    "\n"
    "Scaling scales the area.\n"
    "This paragraph says more about that.\n"
    "And one more line for good measure.\n"
)
PROPOSED = BASE.replace(
    "The delta function is even.\n", "The delta function is even, delta(t) = delta(-t).\n"
)


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[drafting._Job]:
    """Record what would have been queued instead of running it."""
    queued: list[drafting._Job] = []
    monkeypatch.setattr(routes_drafts.drafting, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the drafts router."""
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_drafts.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _draft(db: sqlite3.Connection, class_id: int, content: str = BASE) -> tuple[int, int]:
    """A ready draft with a body holding `content`. Returns (artifact_id, part_id)."""
    response_artifact = artifacts.create_artifact(
        db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT
    )
    artifact_id = int(response_artifact["id"])
    part_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id, part_id


def test_creating_a_draft_returns_it_ready_with_an_empty_body(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    response = client.post(f"/api/classes/{class_id}/drafts", json={"title": "Essay"})

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == artifacts.KIND_DRAFT
    assert body["state"] == artifacts.READY
    read = client.get(f"/api/drafts/{body['id']}").json()
    assert read["body"] == ""
    assert read["pending"] is False


def test_draft_routes_are_kind_guarded(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, 'n.pdf', '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    deck = artifacts.create_artifact(
        db,
        class_id,
        "Deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )

    assert client.get(f"/api/drafts/{deck['id']}").status_code == 404
    assert client.patch(f"/api/drafts/{deck['id']}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/drafts/{deck['id']}").status_code == 404
    assert client.get(f"/api/drafts/{deck['id']}/pending").status_code == 404


def test_autosave_writes_no_revision_but_a_snapshot_does(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    client.patch(f"/api/drafts/{artifact_id}/body", json={"content": PROPOSED})
    assert artifacts.list_revisions(db, part_id)[0]["content"] == BASE
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED

    client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED + "\nMore.\n", "snapshot": True},
    )
    revisions = artifacts.list_revisions(db, part_id)
    assert revisions[0]["content"] == PROPOSED + "\nMore.\n"
    assert revisions[0]["note"] == "snapshot"
    assert revisions[0]["origin"] == artifacts.USER_CORRECTED


def test_suggest_queues_a_run(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    artifact_id, _ = _draft(db, class_id)

    response = client.post(
        f"/api/drafts/{artifact_id}/suggest", json={"instruction": "Argue the converse"}
    )

    assert response.status_code == 202
    assert [job.artifact_id for job in no_worker] == [artifact_id]
    assert no_worker[0].instruction == "Argue the converse"


def test_pending_reads_null_then_the_edit(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    assert client.get(f"/api/drafts/{artifact_id}/pending").json() is None

    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    assert edit["note"] == "Tighten"
    assert edit["stale"] is False
    assert len(edit["hunks"]) == 1
    assert "base_content" not in edit


def test_accept_and_reject_over_http(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()

    accepted = client.post(f"/api/pending-edits/{edit['id']}/accept", json={})
    assert accepted.status_code == 200
    assert accepted.json()["remaining"] == 0
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED

    suggestions.propose(db, part_id, PROPOSED + "\nA tail.\n", "Extend")
    again = client.get(f"/api/drafts/{artifact_id}/pending").json()
    rejected = client.post(f"/api/pending-edits/{again['id']}/reject", json={})
    assert rejected.json()["remaining"] == 0
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED
    assert client.get(f"/api/drafts/{artifact_id}/pending").json() is None


def test_a_hunk_accept_over_http(client: TestClient, db: sqlite3.Connection, class_id: int) -> None:
    two_change = PROPOSED.replace(
        "And one more line for good measure.\n", "And a final line to close with.\n"
    )
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, two_change, "Two changes")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    assert len(edit["hunks"]) == 2

    first = edit["hunks"][0]
    result = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"hunk": {"index": first["index"], "hash": first["hash"]}},
    )

    assert result.status_code == 200
    assert result.json()["remaining"] == 1
    content = str(artifacts.get_part(db, part_id)["content"])
    assert "delta(t) = delta(-t)" in content
    assert "And one more line for good measure." in content


def test_a_stale_edit_rejects_plain_accept_with_409(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    artifacts.set_part_content(
        db,
        part_id,
        BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n"),
        origin=artifacts.USER_CORRECTED,
    )

    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    assert edit["stale"] is True
    assert "base_content" in edit

    refused = client.post(f"/api/pending-edits/{edit['id']}/accept", json={})
    assert refused.status_code == 409
    forced = client.post(f"/api/pending-edits/{edit['id']}/accept", json={"force": True})
    assert forced.status_code == 200
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED


def test_pending_edits_outside_drafts_are_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """A pending_edits row must never point at a non-draft part; if one somehow does,
    the accept/reject surface refuses to see it."""
    artifact_id, _ = _draft(db, class_id)
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, 'n.pdf', '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    solution = artifacts.create_artifact(
        db, class_id, "Set", [artifacts.SourceSpec(document_id=document_id)]
    )
    problem_part = artifacts.create_part(
        db, int(solution["id"]), artifacts.PROBLEM, 1, content="Find x."
    )
    db.execute(
        "insert into pending_edits (part_id, base_content, base_hash, proposed_content) "
        "values (?, 'a', 'h', 'b')",
        (problem_part,),
    )
    db.commit()
    edit_id = int(db.execute("select max(id) from pending_edits").fetchone()[0])

    assert client.post(f"/api/pending-edits/{edit_id}/accept", json={}).status_code == 404
    assert client.post(f"/api/pending-edits/{edit_id}/reject", json={}).status_code == 404


def test_write_streams_tokens_then_done(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    monkeypatch.setattr(routes_drafts, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_config",
        lambda conn: routes_drafts.TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )

    async def fake_stream(*args: object, **kwargs: object) -> Iterator[StreamDelta]:
        yield StreamDelta("reasoning", "thinking")
        yield StreamDelta("answer", "The first ")
        yield StreamDelta("answer", "passage.")

    monkeypatch.setattr(routes_drafts.client, "stream_chat", fake_stream)

    response = client.post(
        f"/api/drafts/{artifact_id}/write", json={"instruction": "Open the essay"}
    )

    assert response.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    # Reasoning never reaches the widget: the passage is the answer channel only.
    assert frames == [
        {"type": "token", "text": "The first "},
        {"type": "token", "text": "passage."},
        {"type": "done"},
    ]


def test_write_is_blocked_without_an_endpoint(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)

    response = client.post(
        f"/api/drafts/{artifact_id}/write", json={"instruction": "Open the essay"}
    )

    assert response.status_code == 400
    assert "No tutor endpoint" in response.json()["detail"]
