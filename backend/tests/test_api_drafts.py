"""Contract tests for the draft endpoints.

The drafting worker is never started here: `writer_pipeline.enqueue` is stubbed so
`/pass` stays a pure write. `/write` streams from a stubbed `stream_chat`. This file is
the HTTP surface; the hunk math is test_suggestions.py and the pass itself is
test_writer_pipeline.py.
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import artifacts, comments, live_drafts, suggestions, writer_pipeline
from backend.core.app_settings import TutorAccess
from backend.core.errors import ConflictError, LyraError
from backend.llm.client import StreamDelta
from backend.rag.retrieve import RetrievalResult
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
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[writer_pipeline.PassJob]:
    """Record what would have been queued instead of running it."""
    queued: list[writer_pipeline.PassJob] = []
    monkeypatch.setattr(routes_drafts.writer_pipeline, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the drafts router."""
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

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
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    saved = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED, "expected_version": version},
    ).json()
    assert saved["version"] == version + 1
    assert artifacts.list_revisions(db, part_id)[0]["content"] == BASE
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED

    snapshot = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={
            "content": PROPOSED + "\nMore.\n",
            "expected_version": saved["version"],
            "snapshot": True,
        },
    ).json()
    assert snapshot["version"] == version + 2
    revisions = artifacts.list_revisions(db, part_id)
    assert revisions[0]["content"] == PROPOSED + "\nMore.\n"
    assert revisions[0]["note"] == "snapshot"
    assert revisions[0]["origin"] == artifacts.USER_CORRECTED


def test_read_exposes_the_body_version_and_writes_advance_it(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)

    first = client.get(f"/api/drafts/{artifact_id}").json()
    assert first["body_version"] == 0

    result = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED, "expected_version": 0},
    ).json()
    assert result == {"part_id": first["part_id"], "saved": True, "version": 1}
    assert client.get(f"/api/drafts/{artifact_id}").json()["body_version"] == 1


def test_a_body_write_requires_an_expected_version(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    # No version to check is precisely the last-writer-wins race this endpoint closes.
    response = client.patch(f"/api/drafts/{artifact_id}/body", json={"content": PROPOSED})
    assert response.status_code == 422


def test_a_stale_body_write_is_refused_and_mutates_nothing(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    # Newer writer B lands first, moving the version from 0 to 1.
    newer = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED, "expected_version": 0},
    ).json()
    assert newer["version"] == 1
    revisions_before = artifacts.list_revisions(db, part_id)

    # Older writer A resolves late, still holding version 0. It must not win.
    stale = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": BASE + "stale tail\n", "expected_version": 0},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "stale_body_version"
    assert body["current_version"] == 1
    assert body["server_body"] == PROPOSED

    # The stored body, the version, and the revision history are exactly B's landing.
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED
    assert artifacts.get_part(db, part_id)["content_version"] == 1
    assert artifacts.list_revisions(db, part_id) == revisions_before


def test_two_concurrent_body_writers_yield_one_winner_and_one_conflict(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    start = threading.Barrier(2)

    def write(tail: str) -> int:
        start.wait()
        return client.patch(
            f"/api/drafts/{artifact_id}/body",
            json={"content": BASE + tail, "expected_version": 0},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(write, ["one\n", "two\n"]))

    # Exactly one write landed; the other got the deterministic conflict.
    assert statuses == [200, 409]
    assert artifacts.get_part(db, part_id)["content_version"] == 1


def test_a_lost_response_retry_is_idempotent_under_the_version(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # The server answered but the client never saw it, so the client retries the same
    # body on the same expected version. The first landed (0 -> 1); the retry now holds a
    # stale version and is refused, which is the honest answer: the body is already there.
    artifact_id, part_id = _draft(db, class_id)

    first = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED, "expected_version": 0},
    )
    assert first.status_code == 200

    retry = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": PROPOSED, "expected_version": 0},
    )
    assert retry.status_code == 409
    # Its server_body is the content the retry meant to write, so the client reconciles to
    # a no-op: nothing was lost, and the body was never written twice.
    assert retry.json()["server_body"] == PROPOSED
    assert artifacts.get_part(db, part_id)["content_version"] == 1


def test_a_server_body_mutation_makes_a_stale_editor_autosave_conflict(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # The editor read the body at version 0. Then an AI pass / accepted suggestion /
    # restore rewrote the body through `set_part_content`, which moves the version. The
    # editor's next autosave still carries version 0, so it must conflict here rather than
    # silently overwriting the AI's result with the pre-pass text.
    artifact_id, part_id = _draft(db, class_id)
    editor_version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    ai_body = BASE + "\nA section the pass drafted.\n"
    artifacts.set_part_content(db, part_id, ai_body, origin=artifacts.GENERATED, note="pass")

    conflict = client.patch(
        f"/api/drafts/{artifact_id}/body",
        json={"content": BASE + "the student's stale edit\n", "expected_version": editor_version},
    )
    assert conflict.status_code == 409
    assert conflict.json()["server_body"] == ai_body
    assert str(artifacts.get_part(db, part_id)["content"]) == ai_body


def test_a_pass_queues_with_its_lens_and_filter(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    artifact_id, _ = _draft(db, class_id)

    response = client.post(
        f"/api/drafts/{artifact_id}/pass",
        json={"instruction": "Argue the converse", "sections": [" 2 ", ""]},
    )

    assert response.status_code == 202
    assert [job.artifact_id for job in no_worker] == [artifact_id]
    assert no_worker[0].instruction == "Argue the converse"
    # Refs arrive stripped, and blank ones do not survive validation.
    assert no_worker[0].section_refs == ("2",)
    assert no_worker[0].depth == "quick"
    assert no_worker[0].pause_at_plan is False


def test_an_empty_body_is_the_full_draft_pass(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    artifact_id, _ = _draft(db, class_id)

    response = client.post(f"/api/drafts/{artifact_id}/pass", json={})

    assert response.status_code == 202
    assert no_worker[0].instruction is None
    assert no_worker[0].section_refs == ()
    live = client.get(f"/api/drafts/{artifact_id}/live-suggestion").json()
    assert live["run_id"] == no_worker[0].run_id
    assert live["stage"] == "gathering"
    assert live["status"] == "pending"


def test_pass_contract_carries_depth_pause_and_the_comment_being_addressed(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Strengthen this section.")

    response = client.post(
        f"/api/drafts/{artifact_id}/pass",
        json={
            "depth": "deep",
            "pause_at_plan": True,
            "address_comment_id": root["id"],
            "sections": ["1"],
        },
    )

    assert response.status_code == 202
    assert no_worker[0].depth == "deep"
    assert no_worker[0].pause_at_plan is True
    assert no_worker[0].address_comment_id == root["id"]
    status = client.get(f"/api/drafts/{artifact_id}/status").json()
    assert (status["job_kind"], status["depth"]) == ("pass", "deep")
    assert status["started_at"]


def test_a_review_queues_on_the_same_worker(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import review_pipeline

    artifact_id, _ = _draft(db, class_id)
    queued: list[review_pipeline.ReviewJob] = []
    monkeypatch.setattr(routes_drafts.review_pipeline, "enqueue", queued.append)

    response = client.post(f"/api/drafts/{artifact_id}/review", json={"depth": "standard"})

    assert response.status_code == 202
    assert len(queued) == 1
    assert queued[0].artifact_id == artifact_id
    assert queued[0].depth == "standard"
    assert queued[0].run_id is not None
    assert client.get(f"/api/drafts/{artifact_id}/status").json()["depth"] == "standard"
    assert client.post("/api/drafts/999999/review").status_code == 404


def test_a_queued_run_is_pending_before_the_worker_touches_it(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug that made the Review button look dead.

    The workspace polls `/status` and gives up the moment the artifact is neither pending
    nor generating. Both queueing endpoints used to leave it `ready` and let the worker
    move it, which the first poll beat every time: the poll stopped, the comments tab
    never refetched, and a review that filed four findings looked like nothing happened.
    """
    from backend.core import review_pipeline

    monkeypatch.setattr(routes_drafts.review_pipeline, "enqueue", lambda job: None)

    review_id, _ = _draft(db, class_id)
    queued = client.post(f"/api/drafts/{review_id}/review").json()
    assert queued["state"] == artifacts.PENDING
    # The prefix is the contract that keeps the editor live under a review, and it has to
    # hold from the first poll - not from whenever the worker gets to the job.
    assert str(queued["stage_detail"]).startswith("Reviewing")
    assert review_pipeline is not None

    pass_id, _ = _draft(db, class_id)
    started = client.post(f"/api/drafts/{pass_id}/pass", json={}).json()
    assert started["state"] == artifacts.PENDING
    # A pass owns the document, so its detail must *not* read as a review.
    assert not str(started["stage_detail"]).startswith("Reviewing")


def test_a_second_run_on_a_busy_draft_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One worker, one draft, one run: a double-click must not stack two jobs."""
    monkeypatch.setattr(routes_drafts.review_pipeline, "enqueue", lambda job: None)
    artifact_id, _ = _draft(db, class_id)

    assert client.post(f"/api/drafts/{artifact_id}/review").status_code == 202
    assert client.post(f"/api/drafts/{artifact_id}/review").status_code == 409
    assert client.post(f"/api/drafts/{artifact_id}/pass", json={}).status_code == 409


def test_concurrent_writer_starts_claim_the_draft_once(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two request connections cannot both pass the ready-to-pending transition."""
    artifact_id, _ = _draft(db, class_id)
    barrier = threading.Barrier(2)
    original_get = routes_drafts.artifacts.get_artifact

    def synchronized_get(conn: sqlite3.Connection, requested_id: int) -> dict[str, object]:
        artifact = original_get(conn, requested_id)
        if requested_id == artifact_id and artifact["state"] == artifacts.READY:
            barrier.wait(timeout=2)
        return artifact

    monkeypatch.setattr(routes_drafts.artifacts, "get_artifact", synchronized_get)

    def attempt() -> str:
        conn = connect()
        try:
            routes_drafts.begin_writer_run(conn, artifact_id, routes_drafts.PASS_JOB_KIND, "quick")
            return "queued"
        except ConflictError:
            return "busy"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))

    assert sorted(outcomes) == ["busy", "queued"]


def test_the_status_endpoint_carries_the_progress_counters(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The polled contract, whole: a stage name with no count reads as a hang."""
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Reviewing prose")
    artifacts.set_problems_total(db, artifact_id, 4)
    artifacts.set_problems_done(db, artifact_id, 2)

    body = client.get(f"/api/drafts/{artifact_id}/status").json()

    assert set(body) == {
        "state",
        "stage_detail",
        "error_message",
        "problems_total",
        "problems_done",
        "run_id",
        "job_kind",
        "depth",
        "started_at",
        "run_status",
        "cancel_requested",
        "cancel_requested_at",
        "finished_at",
        "warnings",
    }
    assert body["problems_total"] == 4
    assert body["problems_done"] == 2
    assert body["job_kind"] == "review"


def test_cancelling_a_draft_run_marks_cancel_requested_in_status(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import review_pipeline

    monkeypatch.setattr(routes_drafts.review_pipeline, "enqueue", lambda job: None)
    artifact_id, _ = _draft(db, class_id)

    started = client.post(f"/api/drafts/{artifact_id}/review", json={"depth": "deep"})
    assert started.status_code == 202

    cancelled = client.post(f"/api/drafts/{artifact_id}/cancel")

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["job_kind"] == "review"
    assert body["run_status"] == "cancel_requested"
    assert body["cancel_requested"] is True
    assert body["cancel_requested_at"]
    assert body["stage_detail"] == "Cancelling after the current step"
    assert review_pipeline is not None


def test_comments_list_anchored_threads_against_the_current_body(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Say which area.",
        severity="major",
        quote="Scaling scales the area.",
        hint=BASE.index("Scaling scales"),
    )
    comments.add_reply(db, int(root["id"]), comments.STUDENT, "The unit area?")
    comments.add_comment(
        db, part_id, comments.REVIEWER, "A finding whose passage is gone.", quote="vanished text"
    )

    response = client.get(f"/api/drafts/{artifact_id}/comments")

    assert response.status_code == 200
    anchored, orphaned = response.json()
    assert anchored["severity"] == "major"
    assert anchored["anchor"]["start"] == BASE.index("Scaling scales")
    assert anchored["orphaned"] == 0
    assert [reply["body"] for reply in anchored["replies"]] == ["The unit area?"]
    assert orphaned["anchor"] is None
    assert orphaned["orphaned"] == 1
    assert client.get("/api/drafts/999999/comments").status_code == 404


def test_export_returns_a_pdf_attachment(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import exporting

    artifact_id, _ = _draft(db, class_id)
    rendered: list[tuple[str, str, str]] = []
    real_render = exporting.render_pdf

    def fake_render(body: str, title: str, class_name: str, sources=None) -> bytes:
        rendered.append((body, title, class_name))
        return b"%PDF-fake"

    monkeypatch.setattr(routes_drafts.exporting, "render_pdf", fake_render)

    response = client.post(f"/api/drafts/{artifact_id}/export")

    assert response.status_code == 200
    assert response.content == b"%PDF-fake"
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="Essay.pdf"' in response.headers["content-disposition"]
    assert rendered[0][0] == BASE
    assert client.post("/api/drafts/999999/export").status_code == 404

    monkeypatch.setattr(routes_drafts.exporting, "render_pdf", real_render)
    monkeypatch.setattr(routes_drafts.exporting.shutil, "which", lambda name: None)
    blocked = client.post(f"/api/drafts/{artifact_id}/export")
    assert blocked.status_code == 400
    assert "pandoc" in blocked.json()["detail"]


def test_export_availability_reports_the_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_drafts.exporting, "export_available", lambda: None)
    assert client.get("/api/export/availability").json() == {"available": True, "message": None}

    monkeypatch.setattr(
        routes_drafts.exporting, "export_available", lambda: "PDF export needs typst."
    )
    answer = client.get("/api/export/availability").json()
    assert answer["available"] is False and "typst" in answer["message"]


def test_replying_and_resolving_over_http(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Say which area.", severity="major")
    root_id = int(root["id"])

    reply = client.post(f"/api/comments/{root_id}/replies", json={"body": "  The unit area.  "})
    assert reply.status_code == 201
    assert reply.json()["author"] == "student"
    assert reply.json()["body"] == "The unit area."

    resolved = client.post(f"/api/comments/{root_id}/resolve", json={"resolved": True})
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] == 1
    reopened = client.post(f"/api/comments/{root_id}/resolve", json={"resolved": False})
    assert reopened.json()["resolved"] == 0

    # A reply is not a thread: resolving one is refused, as is replying under it.
    reply_id = int(reply.json()["id"])
    assert client.post(f"/api/comments/{reply_id}/resolve", json={}).status_code == 404
    assert client.post(f"/api/comments/{reply_id}/replies", json={"body": "x"}).status_code == 404
    assert client.post("/api/comments/999999/replies", json={"body": "x"}).status_code == 404
    assert client.post(f"/api/comments/{root_id}/replies", json={"body": "  "}).status_code == 422


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


def test_live_suggestion_reads_null_then_the_latest_suggestion(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)

    assert client.get(f"/api/drafts/{artifact_id}/live-suggestion").json() is None

    suggestion = live_drafts.create_live_suggestion(
        db,
        artifact_id,
        run_id=41,
        stage="drafting",
        status="running",
        detail="Drafting the structure",
        version=2,
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        section_ref="1.1",
        paragraph_ordinal=1,
        heading="Introduction",
        content="A tighter opening.",
    )

    response = client.get(f"/api/drafts/{artifact_id}/live-suggestion")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == 41
    assert body["stage"] == "drafting"
    assert body["status"] == "running"
    assert body["detail"] == "Drafting the structure"
    assert body["version"] == 2
    assert [block["stable_key"] for block in body["blocks"]] == ["intro-1"]


def test_live_suggestion_block_patch_is_cas_guarded(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=43)
    block = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="A first draft.",
    )

    accepted = client.patch(
        f"/api/drafts/{artifact_id}/live-suggestion/blocks/{block['id']}",
        json={
            "expected_revision": block["revision"],
            "content": "My own first draft.",
            "status": "editing",
        },
    )

    assert accepted.status_code == 200
    body = accepted.json()
    assert body["content"] == "My own first draft."
    assert body["status"] == "editing"
    assert body["user_revision"] == body["revision"]

    streamed = live_drafts.append_block_text(
        db,
        int(suggestion["id"]),
        "intro-1",
        " Streamed suffix.",
    )
    merged = client.patch(
        f"/api/drafts/{artifact_id}/live-suggestion/blocks/{block['id']}",
        json={
            "expected_revision": body["revision"],
            "base_content": body["content"],
            "content": "My edited opening.",
        },
    )

    assert streamed["content"] == "My own first draft. Streamed suffix."
    assert merged.status_code == 200
    assert merged.json()["content"] == "My edited opening. Streamed suffix."

    stale = client.patch(
        f"/api/drafts/{artifact_id}/live-suggestion/blocks/{block['id']}",
        json={"expected_revision": block["revision"], "content": "A stale overwrite."},
    )

    assert stale.status_code == 409
    assert "changed since it was fetched" in stale.json()["detail"]


def test_live_suggestion_finalize_enters_pending_review_without_writing_the_body(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, content="Student draft.\n")
    suggestion = live_drafts.create_live_suggestion(
        db,
        artifact_id,
        run_id=47,
        stage="completed",
        status="ready",
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "1.1:p1",
        section_ref="1.1",
        paragraph_ordinal=1,
        heading="Introduction",
        content="Suggested introduction.",
        status="complete",
    )

    response = client.post(f"/api/drafts/{artifact_id}/live-suggestion/finalize")

    assert response.status_code == 200
    assert response.json()["proposed_content"].startswith("## Introduction")
    assert artifacts.get_part(db, part_id)["content"] == "Student draft.\n"


def test_accept_and_reject_over_http(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    accepted = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"expected_body_version": version},
    )
    assert accepted.status_code == 200
    assert accepted.json()["remaining"] == 0
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED

    suggestions.propose(db, part_id, PROPOSED + "\nA tail.\n", "Extend")
    again = client.get(f"/api/drafts/{artifact_id}/pending").json()
    rejected = client.post(f"/api/pending-edits/{again['id']}/reject", json={})
    assert rejected.json()["remaining"] == 0
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED
    assert client.get(f"/api/drafts/{artifact_id}/pending").json() is None


def test_address_comment_resolves_only_after_its_linked_proposal_lands(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "State the symmetry explicitly.",
        severity="major",
        quote="The delta function is even.",
        section_ref="Essay",
    )
    edit = suggestions.propose(db, part_id, PROPOSED, "Address comment")
    assert edit is not None
    db.execute(
        "insert into pending_edit_comment_links (edit_id, comment_id) values (?, ?)",
        (edit.id, root["id"]),
    )
    db.commit()

    assert comments._get(db, int(root["id"]))["resolved"] == 0
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]
    accepted = client.post(
        f"/api/pending-edits/{edit.id}/accept",
        json={"expected_body_version": version},
    )

    assert accepted.status_code == 200
    assert comments._get(db, int(root["id"]))["resolved"] == 1


def test_rejecting_a_linked_proposal_keeps_the_comment_open(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    _, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "State the symmetry explicitly.",
        section_ref="Essay",
    )
    edit = suggestions.propose(db, part_id, PROPOSED, "Address comment")
    assert edit is not None
    db.execute(
        "insert into pending_edit_comment_links (edit_id, comment_id) values (?, ?)",
        (edit.id, root["id"]),
    )
    db.commit()

    rejected = client.post(f"/api/pending-edits/{edit.id}/reject", json={})

    assert rejected.status_code == 200
    assert comments._get(db, int(root["id"]))["resolved"] == 0


def test_accepting_an_unrelated_hunk_does_not_resolve_the_addressed_section(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    two_change = PROPOSED.replace(
        "And one more line for good measure.\n", "And a final line to close with.\n"
    )
    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Strengthen the close.",
        section_ref="Scaling",
    )
    edit = suggestions.propose(db, part_id, two_change, "Address scaling")
    assert edit is not None
    db.execute(
        "insert into pending_edit_comment_links (edit_id, comment_id) values (?, ?)",
        (edit.id, root["id"]),
    )
    db.commit()
    pending = client.get(f"/api/drafts/{artifact_id}/pending").json()

    first = pending["hunks"][0]
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]
    accepted = client.post(
        f"/api/pending-edits/{edit.id}/accept",
        json={
            "hunk": {"index": first["index"], "hash": first["hash"]},
            "expected_body_version": version,
        },
    )

    assert accepted.json()["remaining"] == 1
    assert comments._get(db, int(root["id"]))["resolved"] == 0
    # The hunk accept moved the body version, so the finishing accept carries the new one.
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]
    finished = client.post(
        f"/api/pending-edits/{edit.id}/accept",
        json={"expected_body_version": version},
    )
    assert finished.json()["remaining"] == 0
    assert comments._get(db, int(root["id"]))["resolved"] == 1


def test_accept_with_a_matching_expected_body_version_lands(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    accepted = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"expected_body_version": version},
    )

    assert accepted.status_code == 200
    assert accepted.json()["remaining"] == 0
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED
    # The accept moved the body version, so a later stale autosave conflicts against it.
    assert int(artifacts.get_part(db, part_id)["content_version"]) == version + 1


def test_a_draft_accept_without_a_version_is_refused_and_mutates_nothing(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # This endpoint is draft-only, so the version token is required: a request without it is
    # rejected before the handler runs, and no direct/stale client can force-replace a draft
    # body versionlessly. Nothing is written - not the body, the edit, or the history.
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    before_content = str(artifacts.get_part(db, part_id)["content"])
    before_version = int(artifacts.get_part(db, part_id)["content_version"])
    before_revisions = artifacts.list_revisions(db, part_id)

    for body in ({}, {"force": True}):
        refused = client.post(f"/api/pending-edits/{edit['id']}/accept", json=body)
        assert refused.status_code == 422

    # The suggestion still pends and nothing about the body or its history moved.
    assert client.get(f"/api/drafts/{artifact_id}/pending").json() is not None
    assert str(artifacts.get_part(db, part_id)["content"]) == before_content
    assert int(artifacts.get_part(db, part_id)["content_version"]) == before_version
    assert artifacts.list_revisions(db, part_id) == before_revisions


def test_accept_racing_a_body_change_conflicts_without_overwriting(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # The student reviewed the suggestion at version 0, but a second tab (or an autosave)
    # moved the body before the accept landed. The accept must conflict, not clobber.
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    reviewed_version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    elsewhere = BASE + "\nA line from another tab.\n"
    artifacts.set_part_content(db, part_id, elsewhere, origin=artifacts.USER_CORRECTED)
    moved_version = int(artifacts.get_part(db, part_id)["content_version"])

    refused = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"expected_body_version": reviewed_version},
    )

    assert refused.status_code == 409
    assert refused.json()["code"] == "stale_body_version"
    assert refused.json()["current_version"] == moved_version
    assert refused.json()["server_body"] == elsewhere
    # Nothing was overwritten: the other tab's body stands, and the edit still pends.
    assert str(artifacts.get_part(db, part_id)["content"]) == elsewhere
    assert int(artifacts.get_part(db, part_id)["content_version"]) == moved_version
    assert client.get(f"/api/drafts/{artifact_id}/pending").json() is not None


def test_force_replace_racing_a_body_change_conflicts(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # A force-replace is still relative to the version the student reviewed: a change that
    # landed after they looked must conflict rather than be silently overwritten.
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    reviewed_version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]

    edited = BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)
    moved_version = int(artifacts.get_part(db, part_id)["content_version"])

    refused = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"force": True, "expected_body_version": reviewed_version},
    )

    assert refused.status_code == 409
    assert refused.json()["code"] == "stale_body_version"
    # The force did not overwrite the version the student never saw.
    assert str(artifacts.get_part(db, part_id)["content"]) == edited
    assert int(artifacts.get_part(db, part_id)["content_version"]) == moved_version


def test_force_replace_with_the_reviewed_version_lands(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # The suggestion went stale under the student's own edit; forcing against the version
    # they are looking at (the side-by-side "current") replaces the document as intended.
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    edited = BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)
    reviewed_version = int(artifacts.get_part(db, part_id)["content_version"])

    forced = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"force": True, "expected_body_version": reviewed_version},
    )

    assert forced.status_code == 200
    assert str(artifacts.get_part(db, part_id)["content"]) == PROPOSED


def test_a_hunk_accept_over_http(client: TestClient, db: sqlite3.Connection, class_id: int) -> None:
    two_change = PROPOSED.replace(
        "And one more line for good measure.\n", "And a final line to close with.\n"
    )
    artifact_id, part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, two_change, "Two changes")
    edit = client.get(f"/api/drafts/{artifact_id}/pending").json()
    assert len(edit["hunks"]) == 2

    first = edit["hunks"][0]
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]
    result = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={
            "hunk": {"index": first["index"], "hash": first["hash"]},
            "expected_body_version": version,
        },
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

    # The edit is stale against its base, but the body version is current: the accept still
    # carries the token (now required), and the stale-base 409 is what refuses it.
    version = client.get(f"/api/drafts/{artifact_id}").json()["body_version"]
    refused = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"expected_body_version": version},
    )
    assert refused.status_code == 409
    forced = client.post(
        f"/api/pending-edits/{edit['id']}/accept",
        json={"force": True, "expected_body_version": version},
    )
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

    # A valid body gets past request validation, so the 404 proves the kind guard itself
    # refuses a non-draft edit (not merely the now-required version token).
    assert (
        client.post(
            f"/api/pending-edits/{edit_id}/accept",
            json={"expected_body_version": 0},
        ).status_code
        == 404
    )
    assert client.post(f"/api/pending-edits/{edit_id}/reject", json={}).status_code == 404


def test_write_streams_tokens_then_done(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=routes_drafts.TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )
    # Patched where the route looks it up, as test_api_chat does. Unstubbed, retrieval
    # embeds the query, and in a test data dir that means "weights not downloaded" - it
    # only ever passed by adopting a llama-server some earlier run had leaked.
    monkeypatch.setattr(
        routes_drafts,
        "retrieve",
        lambda *args, **kwargs: RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0),
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


# --- Draft revision/restore endpoints (PLA-311: kind boundary) ---


def test_draft_revisions_endpoint(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "first draft")
    artifacts.set_part_content(db, part_id, "second draft", origin=artifacts.USER_CORRECTED)

    response = client.get(f"/api/drafts/{artifact_id}/parts/{part_id}/revisions")

    assert response.status_code == 200
    contents = [r["content"] for r in response.json()]
    assert contents == ["second draft", "first draft"]


def test_draft_restore_with_the_current_version_succeeds(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "first draft")
    artifacts.set_part_content(db, part_id, "second draft", origin=artifacts.USER_CORRECTED)
    version = int(artifacts.get_part(db, part_id)["content_version"])

    restored = client.post(
        f"/api/drafts/{artifact_id}/parts/{part_id}/restore",
        json={"revision": 1, "expected_version": version},
    )

    assert restored.status_code == 200
    assert str(artifacts.get_part(db, part_id)["content"]) == "first draft"
    assert int(artifacts.get_part(db, part_id)["content_version"]) == version + 1


def test_stale_draft_restore_is_refused_without_mutation(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "first draft")
    artifacts.set_part_content(db, part_id, "second draft", origin=artifacts.USER_CORRECTED)
    stale_version = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.set_part_content(db, part_id, "third from elsewhere", origin=artifacts.USER_CORRECTED)
    moved_version = int(artifacts.get_part(db, part_id)["content_version"])

    refused = client.post(
        f"/api/drafts/{artifact_id}/parts/{part_id}/restore",
        json={"revision": 1, "expected_version": stale_version},
    )

    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "stale_body_version"
    assert body["current_version"] == moved_version
    assert body["server_body"] == "third from elsewhere"
    assert str(artifacts.get_part(db, part_id)["content"]) == "third from elsewhere"
    assert int(artifacts.get_part(db, part_id)["content_version"]) == moved_version


def test_draft_restore_without_a_version_is_rejected(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "first draft")
    artifacts.set_part_content(db, part_id, "second draft", origin=artifacts.USER_CORRECTED)

    refused = client.post(
        f"/api/drafts/{artifact_id}/parts/{part_id}/restore",
        json={"revision": 1},
    )

    assert refused.status_code == 422
    assert str(artifacts.get_part(db, part_id)["content"]) == "second draft"


def test_draft_restore_preserves_the_pre_restore_autosaved_body_as_history(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "revision A body")
    assert [r["content"] for r in artifacts.list_revisions(db, part_id)] == ["revision A body"]
    version = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.compare_and_set_part_content(
        db,
        part_id,
        "manually autosaved body B",
        artifacts.USER_CORRECTED,
        expected_version=version,
        record_revision=False,
    )
    restore_version = int(artifacts.get_part(db, part_id)["content_version"])

    restored = client.post(
        f"/api/drafts/{artifact_id}/parts/{part_id}/restore",
        json={"revision": 1, "expected_version": restore_version},
    )

    assert restored.status_code == 200
    assert str(artifacts.get_part(db, part_id)["content"]) == "revision A body"
    contents = [r["content"] for r in artifacts.list_revisions(db, part_id)]
    assert contents == ["revision A body", "manually autosaved body B", "revision A body"]


def test_stale_draft_restore_creates_no_phantom_revision(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id, "first draft")
    artifacts.set_part_content(db, part_id, "second draft", origin=artifacts.USER_CORRECTED)
    stale_version = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.set_part_content(db, part_id, "third from elsewhere", origin=artifacts.USER_CORRECTED)
    before_revisions = artifacts.list_revisions(db, part_id)

    refused = client.post(
        f"/api/drafts/{artifact_id}/parts/{part_id}/restore",
        json={"revision": 1, "expected_version": stale_version},
    )

    assert refused.status_code == 409
    assert artifacts.list_revisions(db, part_id) == before_revisions
    assert str(artifacts.get_part(db, part_id)["content"]) == "third from elsewhere"


def test_concurrent_autosave_between_read_and_restore_cannot_be_overwritten(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Adversarial: a concurrent autosave lands between the caller's read and its restore.

    The restore must conflict: the autosave moved the version, so the caller's
    expected_version is stale. The autosaved body must not be silently clobbered.
    """
    artifact_id, part_id = _draft(db, class_id, "original")
    artifacts.set_part_content(db, part_id, "edited", origin=artifacts.USER_CORRECTED)
    caller_version = int(artifacts.get_part(db, part_id)["content_version"])
    revision = artifacts.get_revision(db, part_id, 1)

    conn2 = connect()
    try:
        artifacts.compare_and_set_part_content(
            conn2,
            part_id,
            "autosaved body",
            artifacts.USER_CORRECTED,
            expected_version=caller_version,
            record_revision=False,
        )
    finally:
        conn2.close()

    from backend.core.errors import StaleContentError

    with pytest.raises(StaleContentError) as exc_info:
        artifacts.compare_and_restore_part_content(
            db,
            part_id,
            str(revision["content"]),
            artifacts.USER_CORRECTED,
            expected_version=caller_version,
            restored_note="Restored version 1.",
            preserved_origin=artifacts.USER_CORRECTED,
            preserved_note="Pre-restore body.",
        )
    assert exc_info.value.current_content == "autosaved body"
    assert str(artifacts.get_part(db, part_id)["content"]) == "autosaved body"


def test_one_concurrent_writer_wins_and_the_stale_restore_conflicts(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Adversarial: two threads race to restore vs autosave; exactly one wins."""
    artifact_id, part_id = _draft(db, class_id, "body A")
    artifacts.set_part_content(db, part_id, "body B", origin=artifacts.USER_CORRECTED)
    shared_version = int(artifacts.get_part(db, part_id)["content_version"])

    barrier = threading.Barrier(2)
    results: list[str] = []

    def do_restore() -> None:
        conn = connect()
        try:
            barrier.wait(timeout=2)
            artifacts.compare_and_restore_part_content(
                conn,
                part_id,
                "body A",
                artifacts.USER_CORRECTED,
                expected_version=shared_version,
                restored_note="Restored.",
                preserved_origin=artifacts.USER_CORRECTED,
                preserved_note="Before restore.",
            )
            results.append("restored")
        except Exception:
            results.append("conflict")
        finally:
            conn.close()

    def do_autosave() -> None:
        conn = connect()
        try:
            barrier.wait(timeout=2)
            artifacts.compare_and_set_part_content(
                conn,
                part_id,
                "body C autosave",
                artifacts.USER_CORRECTED,
                expected_version=shared_version,
                record_revision=False,
            )
            results.append("saved")
        except Exception:
            results.append("conflict")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(do_restore)
        pool.submit(do_autosave)
        pool.shutdown(wait=True)

    assert sorted(results) == ["conflict", sorted(["restored", "saved"])[1]] or sorted(results) in [
        ["conflict", "restored"],
        ["conflict", "saved"],
    ]
    assert int(artifacts.get_part(db, part_id)["content_version"]) == shared_version + 1


def test_stale_restore_creates_no_phantom_revisions_under_concurrent_pressure(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Adversarial: a stale restore must leave zero new revisions behind."""
    artifact_id, part_id = _draft(db, class_id, "alpha")
    artifacts.set_part_content(db, part_id, "beta", origin=artifacts.USER_CORRECTED)
    stale_version = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.set_part_content(db, part_id, "gamma", origin=artifacts.USER_CORRECTED)
    before_revisions = [dict(r) for r in artifacts.list_revisions(db, part_id)]
    before_count = len(before_revisions)

    from backend.core.errors import StaleContentError

    with pytest.raises(StaleContentError):
        artifacts.compare_and_restore_part_content(
            db,
            part_id,
            "alpha",
            artifacts.USER_CORRECTED,
            expected_version=stale_version,
            restored_note="Restored.",
            preserved_origin=artifacts.USER_CORRECTED,
            preserved_note="Pre-restore.",
        )

    after_revisions = artifacts.list_revisions(db, part_id)
    assert len(after_revisions) == before_count
    assert str(artifacts.get_part(db, part_id)["content"]) == "gamma"


def test_outgoing_autosaved_body_remains_recoverable_after_restore(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The autosaved-only body (no recorded revision) must appear in history after restore."""
    artifact_id, part_id = _draft(db, class_id, "recorded body")
    v0 = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.compare_and_set_part_content(
        db,
        part_id,
        "autosaved-only body",
        artifacts.USER_CORRECTED,
        expected_version=v0,
        record_revision=False,
    )
    v1 = int(artifacts.get_part(db, part_id)["content_version"])

    artifacts.compare_and_restore_part_content(
        db,
        part_id,
        "recorded body",
        artifacts.USER_CORRECTED,
        expected_version=v1,
        restored_note="Restored version 1.",
        preserved_origin=artifacts.USER_CORRECTED,
        preserved_note="Before restore.",
    )

    contents = [r["content"] for r in artifacts.list_revisions(db, part_id)]
    assert "autosaved-only body" in contents
    assert str(artifacts.get_part(db, part_id)["content"]) == "recorded body"


def test_restore_and_history_publication_are_atomic(db: sqlite3.Connection, class_id: int) -> None:
    """The restored body and its history entries either both land or neither does."""
    artifact_id, part_id = _draft(db, class_id, "body 1")
    artifacts.set_part_content(db, part_id, "body 2", origin=artifacts.USER_CORRECTED)
    v = int(artifacts.get_part(db, part_id)["content_version"])
    revisions_before = len(artifacts.list_revisions(db, part_id))

    result = artifacts.compare_and_restore_part_content(
        db,
        part_id,
        "body 1",
        artifacts.USER_CORRECTED,
        expected_version=v,
        restored_note="Restored version 1.",
        preserved_origin=artifacts.USER_CORRECTED,
        preserved_note="Before restore.",
    )

    assert result["version"] == v + 1
    assert result["content"] == "body 1"
    revisions_after = artifacts.list_revisions(db, part_id)
    assert len(revisions_after) > revisions_before
    assert revisions_after[0]["content"] == "body 1"
    assert revisions_after[0]["note"] == "Restored version 1."


def test_injected_failure_cannot_leave_only_preserved_body_committed(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the restore write fails after the outgoing body is preserved, both are rolled back."""
    artifact_id, part_id = _draft(db, class_id, "safe body")
    artifacts.set_part_content(db, part_id, "recorded body", origin=artifacts.USER_CORRECTED)
    v0 = int(artifacts.get_part(db, part_id)["content_version"])
    artifacts.compare_and_set_part_content(
        db,
        part_id,
        "autosaved-only body",
        artifacts.USER_CORRECTED,
        expected_version=v0,
        record_revision=False,
    )
    v = int(artifacts.get_part(db, part_id)["content_version"])
    revisions_before = artifacts.list_revisions(db, part_id)

    call_count = 0
    original_insert = artifacts._insert_revision

    def failing_insert(conn, pid, content, origin, note=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("injected failure on second revision insert")
        return original_insert(conn, pid, content, origin, note)

    monkeypatch.setattr(artifacts, "_insert_revision", failing_insert)

    with pytest.raises(RuntimeError, match="injected failure"):
        artifacts.compare_and_restore_part_content(
            db,
            part_id,
            "safe body",
            artifacts.USER_CORRECTED,
            expected_version=v,
            restored_note="Restored version 1.",
            preserved_origin=artifacts.USER_CORRECTED,
            preserved_note="Before restore.",
        )

    assert str(artifacts.get_part(db, part_id)["content"]) == "autosaved-only body"
    assert int(artifacts.get_part(db, part_id)["content_version"]) == v
    assert artifacts.list_revisions(db, part_id) == revisions_before


def _solution_set(db: sqlite3.Connection, class_id: int) -> dict[str, object]:
    """A solution-set artifact in the same class, for cross-kind refusal tests."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'hw.pdf', 'irrelevant', 'application/pdf', 100, 'ready')",
        (class_id,),
    )
    doc_id = int(cursor.lastrowid or 0)
    return artifacts.create_artifact(
        db, class_id, "Solutions", [artifacts.SourceSpec(document_id=doc_id)]
    )


def test_draft_revisions_refuses_a_solution_set(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    created = _solution_set(db, class_id)
    response = client.get(f"/api/drafts/{created['id']}/parts/1/revisions")
    assert response.status_code == 404


def test_draft_restore_refuses_a_solution_set(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    created = _solution_set(db, class_id)
    response = client.post(
        f"/api/drafts/{created['id']}/parts/1/restore",
        json={"revision": 1, "expected_version": 0},
    )
    assert response.status_code == 404


def test_http_durable_draft_rejects_oversized_existing_body_before_inference(
    client,
    db,
    class_id,
    no_worker,
    monkeypatch,
):
    from backend.core import writer_runs
    from backend.core.app_settings import TutorConfig

    body = "Student writing that must survive. " * 4000
    artifact = artifacts.create_artifact(db, class_id, "Long essay", [], kind=artifacts.KIND_DRAFT)
    part = artifacts.create_part(db, artifact["id"], artifacts.DRAFT_BODY, 1, content=body)
    artifacts.set_artifact_state(db, artifact["id"], artifacts.READY)
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda *args: TutorAccess(
            config=TutorConfig("http://127.0.0.1:9/v1", None, "m", 2048),
            document_block=None,
            remote_ack=True,
        ),
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Over-budget request reached provider")

    monkeypatch.setattr(writer_pipeline.client, "complete", forbidden)
    response = client.post(f"/api/drafts/{artifact['id']}/pass", json={"depth": "quick"})
    assert response.status_code == 202
    assert len(no_worker) == 1 and no_worker[0].run_id is not None
    writer_pipeline.run_pass(no_worker[0])
    run = writer_runs.get_run(db, no_worker[0].run_id)
    assert run["status"] == "failed"
    assert "context window" in run["error_message"]
    assert artifacts.get_part(db, part)["content"] == body
    live = live_drafts.get_live_suggestion_for_run(db, run["id"])
    assert live["status"] == "failed"
    # Restart/retry creates a new intent and keeps the same refusal and student body.
    assert (
        client.post(f"/api/drafts/{artifact['id']}/pass", json={"depth": "quick"}).status_code
        == 202
    )
    writer_pipeline.run_pass(no_worker[-1])
    assert artifacts.get_part(db, part)["content"] == body
