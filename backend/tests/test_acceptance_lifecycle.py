"""Acceptance coverage across the main class workflows with deterministic fakes.

These tests exercise the real HTTP surfaces together rather than one router at a time.
Workers and upstream services are replaced with bounded fakes so the suite stays fast and
repeatable while still covering the user-visible lifecycle edges.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import (
    routes_chat,
    routes_classes,
    routes_documents,
    routes_drafts,
    routes_health,
    routes_solutions,
    routes_study,
)
from backend.config import settings
from backend.core import app_settings, artifacts, drafting, sessions, solver
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.core.firecrawl import FirecrawlError
from backend.core.segmentation import SegmentedProblem
from backend.llm.client import StreamDelta
from backend.llm.tools import COMPLETED, TIMEOUT, UPSTREAM_FAILED, ToolLoopResult
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect, get_db

QUESTION = "Explain the sifting property."
ENDPOINT = "http://127.0.0.1:8081/v1"


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "get_api_key", lambda: None)


@pytest.fixture
def queues(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    seen = {
        "documents": [],
        "solutions": [],
        "solve": [],
        "study": [],
        "draft_pass": [],
        "review": [],
    }
    monkeypatch.setattr(routes_documents, "enqueue", seen["documents"].append)
    monkeypatch.setattr(routes_solutions.solver, "enqueue", seen["solutions"].append)
    monkeypatch.setattr(routes_solutions.solver, "enqueue_solve", seen["solve"].append)
    monkeypatch.setattr(routes_study.study, "enqueue", seen["study"].append)
    monkeypatch.setattr(routes_drafts.writer_pipeline, "enqueue", seen["draft_pass"].append)
    monkeypatch.setattr(routes_drafts.review_pipeline, "enqueue", seen["review"].append)
    return seen


@pytest.fixture
def client(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_classes.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_solutions.router)
    app.include_router(routes_study.router)
    app.include_router(routes_drafts.router)
    app.include_router(routes_health.router)
    app.dependency_overrides[get_db] = _request_db

    monkeypatch.setattr(
        routes_chat, "retrieve", lambda *args, **kwargs: RetrievalResult([], False, 0)
    )

    async def fake_stream_chat(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        assert endpoint_url == ENDPOINT
        yield StreamDelta("answer", "The delta picks out the value at zero.")

    monkeypatch.setattr(routes_chat, "stream_chat", fake_stream_chat)

    with TestClient(app) as test_client:
        yield test_client


def _frames(body: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for block in body.split("\n\n"):
        if not block:
            continue
        parsed.append(json.loads(block.removeprefix("data: ")))
    return parsed


def _set_endpoint(db: sqlite3.Connection) -> None:
    db.execute("update settings set endpoint_url = ?, model = 'local-qwen'", (ENDPOINT,))
    db.commit()


def _mark_document_ready(
    db: sqlite3.Connection, document_id: int, text: str = "delta notes"
) -> None:
    db.execute(
        "update documents set state = 'ready', stage_detail = null, "
        "error_message = null where id = ?",
        (document_id,),
    )
    db.commit()
    text_dir = settings.text_dir
    text_dir.mkdir(parents=True, exist_ok=True)
    (text_dir / f"{document_id}.txt").write_text(text, encoding="utf-8")


def _seed_solution_gate(db: sqlite3.Connection, artifact_id: int, document_id: int) -> None:
    solver.write_problems(
        db,
        artifact_id,
        [
            SegmentedProblem(
                label="Problem 1",
                number="1",
                statement="Find x(0).",
                document_id=document_id,
                page_number=1,
            )
        ],
    )
    artifacts.set_problems_total(db, artifact_id, 1)
    artifacts.set_artifact_state(db, artifact_id, artifacts.FAILED, "The previous solve crashed.")


def _seed_quiz_ready(db: sqlite3.Connection, artifact_id: int) -> None:
    artifacts.create_part(
        db,
        artifact_id,
        artifacts.QUIZ_QUESTION,
        1,
        label="delta",
        content=json.dumps(
            {
                "type": "mcq",
                "question": "Which property evaluates x(0)?",
                "options": ["Sifting", "Shifting", "Scaling", "Convolution"],
                "correct_index": 0,
                "explanation": "The delta distribution sifts out x(0).",
                "topic": "delta",
                "difficulty": "basic",
            }
        ),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)


def _draft(
    db: sqlite3.Connection,
    class_id: int,
    content: str = "# Essay\n\nStudent draft.\n",
) -> int:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    artifact_id = int(created["id"])
    artifacts.create_part(
        db,
        artifact_id,
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id


def test_ready_health_reports_firecrawl_degradation_without_failing_lyra(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableFirecrawl:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:3002"

        def check_readiness(self) -> dict[str, object]:
            raise FirecrawlError("hidden upstream detail")

    monkeypatch.setattr(routes_health, "FirecrawlClient", UnavailableFirecrawl)

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["database"]["status"] == "ready"
    assert body["components"]["firecrawl"] == {
        "status": "temporarily_unavailable",
        "required": False,
        "message": "Firecrawl is temporarily unavailable; web research is disabled.",
    }
    assert body["components"]["web_scrape"] == {
        "status": "not_ready",
        "required": False,
        "message": "Web scraping remains disabled until the redirect-safety gate passes.",
    }


def test_learning_workflow_round_trips_class_document_chat_solution_and_study(
    client: TestClient, db: sqlite3.Connection, queues: dict[str, list[object]]
) -> None:
    created_class = client.post("/api/classes", json={"name": "Signals"}).json()
    class_id = created_class["id"]

    upload = client.post(
        f"/api/classes/{class_id}/documents",
        files={
            "file": (
                "signals.md",
                b"# Delta\n\nThe sifting property returns x(0).\n",
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 202
    document_id = upload.json()["id"]
    assert queues["documents"] == [document_id]

    db.execute(
        "update documents set state = 'failed', "
        "error_message = 'Interrupted by restart.' where id = ?",
        (document_id,),
    )
    db.commit()
    retried = client.post(f"/api/documents/{document_id}/reingest")
    assert retried.status_code == 202
    assert queues["documents"] == [document_id, document_id]

    _mark_document_ready(db, document_id)
    _set_endpoint(db)

    session_id = client.post(f"/api/classes/{class_id}/sessions", json={}).json()["id"]
    answer = client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": QUESTION, "mode": "guide", "document_id": None},
    )
    assert answer.status_code == 200
    frames = _frames(answer.text)
    assert [frame["type"] for frame in frames][-2:] == ["token", "done"]
    assert (
        "".join(str(frame.get("text", "")) for frame in frames)
        == "The delta picks out the value at zero."
    )
    transcript = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["role"] for message in transcript] == ["user", "assistant"]

    created_solution = client.post(
        f"/api/classes/{class_id}/solutions",
        json={"sources": [{"document_id": document_id}]},
    )
    assert created_solution.status_code == 202
    solution_id = created_solution.json()["id"]
    assert queues["solutions"] == [solution_id]

    _seed_solution_gate(db, solution_id, document_id)
    restarted_solution = client.post(f"/api/solutions/{solution_id}/start")
    assert restarted_solution.status_code == 202
    assert restarted_solution.json()["state"] == "pending"
    assert queues["solve"] == [solution_id]

    created_quiz = client.post(
        f"/api/classes/{class_id}/quizzes",
        json={"title": "Week 1 quiz", "count": 3, "difficulty": "basic", "types": ["mcq"]},
    )
    assert created_quiz.status_code == 202
    quiz_id = created_quiz.json()["id"]
    assert len(queues["study"]) == 1

    _seed_quiz_ready(db, quiz_id)
    first_attempt = client.post(f"/api/quizzes/{quiz_id}/attempts")
    assert first_attempt.status_code == 200
    attempt_id = first_attempt.json()["attempt_id"]
    part_id = first_attempt.json()["question_part_ids"][0]
    graded = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": 0},
    )
    assert graded.status_code == 200
    assert graded.json()["correct"] is True
    finished = client.post(f"/api/attempts/{attempt_id}/finish")
    assert finished.status_code == 200
    assert finished.json()["score"] == 1
    assert finished.json()["total"] == 1

    second_attempt = client.post(f"/api/quizzes/{quiz_id}/attempts")
    assert second_attempt.status_code == 200
    assert second_attempt.json()["attempt_id"] != attempt_id


def test_draft_workflow_preserves_restart_and_cancel_controls(
    client: TestClient, db: sqlite3.Connection, queues: dict[str, list[object]]
) -> None:
    class_id = client.post("/api/classes", json={"name": "Writing"}).json()["id"]
    created = client.post(f"/api/classes/{class_id}/drafts", json={"title": "Lab report"})
    assert created.status_code == 201
    draft_id = created.json()["id"]

    started = client.post(
        f"/api/drafts/{draft_id}/pass", json={"instruction": "Tighten the introduction."}
    )
    assert started.status_code == 202
    assert started.json()["state"] == "pending"
    assert len(queues["draft_pass"]) == 1

    requeued, resumed = drafting.reconcile_interrupted(db)
    assert (requeued, resumed) == (1, 0)

    status_after_restart = client.get(f"/api/drafts/{draft_id}/status")
    assert status_after_restart.status_code == 200
    body = status_after_restart.json()
    assert body["state"] == "pending"
    assert body["run_status"] == "queued"
    assert body["warnings"] == [
        {
            "code": "resumed_after_restart",
            "message": "This queued run resumed after a restart.",
        }
    ]

    cancelled = client.post(f"/api/drafts/{draft_id}/cancel")
    assert cancelled.status_code == 200
    cancel_body = cancelled.json()
    assert cancel_body["run_status"] == "cancel_requested"
    assert cancel_body["cancel_requested"] is True
    assert cancel_body["cancel_requested_at"]
    assert cancel_body["stage_detail"] == "Cancelling after the current step"


@pytest.mark.parametrize(
    ("stopped", "detail"),
    [
        (TIMEOUT, "Checking took too long."),
        (UPSTREAM_FAILED, "The tutor endpoint could not be reached."),
    ],
)
def test_writer_chat_faults_leave_only_the_user_turn_for_retry(
    client: TestClient,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    stopped: str,
    detail: str,
) -> None:
    class_id = client.post("/api/classes", json={"name": "Writing"}).json()["id"]
    draft_id = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{draft_id}/sessions").json()["id"]

    monkeypatch.setattr(routes_drafts, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )

    async def failed_loop(*args: object, **kwargs: object) -> ToolLoopResult:
        return ToolLoopResult(content="", stopped=stopped, detail=detail)

    monkeypatch.setattr(routes_drafts, "run_tool_loop", failed_loop)

    response = client.post(
        f"/api/drafts/{draft_id}/chat/{session_id}", json={"content": "Slow question."}
    )

    assert response.status_code == 200
    assert [frame["type"] for frame in _frames(response.text)] == ["start", "status", "error"]
    assert _frames(response.text)[-1]["message"] == detail
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


def test_writer_chat_replaces_malformed_draft_answers_with_an_honest_retry_message(
    client: TestClient,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class_id = client.post("/api/classes", json={"name": "Writing"}).json()["id"]
    draft_id = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{draft_id}/sessions").json()["id"]

    monkeypatch.setattr(routes_drafts, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )

    async def malformed_loop(*args: object, **kwargs: object) -> ToolLoopResult:
        return ToolLoopResult(content="Here is the whole paper.", stopped=COMPLETED)

    monkeypatch.setattr(routes_drafts, "run_tool_loop", malformed_loop)

    response = client.post(
        f"/api/drafts/{draft_id}/chat/{session_id}",
        json={"content": "Write the whole paper."},
    )

    assert response.status_code == 200
    frames = _frames(response.text)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert "did not route that drafting request" in str(token["text"])
    assert "Here is the whole paper." not in response.text
    stored = sessions.list_messages(db, session_id)
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[-1]["content"] == token["text"]
