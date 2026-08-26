"""Writer-chat turn serialization, context budget, and durable attempt lifecycle.

PLA-308: per-session turn claim prevents concurrent writer turns.
PLA-309: context budget bounds each round of the writer tool loop.
PLA-310: durable attempt lifecycle with causal retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_chat, routes_drafts
from backend.core import artifacts, sessions, writer_attempts
from backend.core.errors import LyraError
from backend.llm import tools
from backend.storage.database import connect, get_db

ENDPOINT = "http://127.0.0.1:8080/v1"
CONTEXT_WINDOW = 32_768


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def configured_endpoint(db: sqlite3.Connection) -> None:
    db.execute(
        "update settings set endpoint_url = ?, model = 'qwen', "
        "context_window = ?, tools_supported = 1 where id = 1",
        (ENDPOINT, CONTEXT_WINDOW),
    )
    db.commit()


@pytest.fixture(autouse=True)
def released_claims() -> Iterator[None]:
    yield
    sessions._active_turns.clear()


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

    app.include_router(routes_drafts.router)
    app.include_router(routes_chat.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _draft(db: sqlite3.Connection, class_id: int) -> int:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    artifact_id = int(created["id"])
    artifacts.create_part(
        db,
        artifact_id,
        artifacts.DRAFT_BODY,
        1,
        content="Introduction.\n\nBody.\n\nConclusion.",
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id


@pytest.fixture
def artifact_id(db: sqlite3.Connection, class_id: int) -> int:
    return _draft(db, class_id)


@pytest.fixture
def writer_session(db: sqlite3.Connection, class_id: int, artifact_id: int) -> int:
    part = db.execute(
        "select id from artifact_parts where artifact_id = ? and kind = ?",
        (artifact_id, artifacts.DRAFT_BODY),
    ).fetchone()
    return int(
        sessions.create_session(
            db, class_id, artifact_part_id=int(part["id"]), mode=sessions.WRITER
        )["id"]
    )


def _stub_loop(monkeypatch: pytest.MonkeyPatch, result: tools.ToolLoopResult) -> None:
    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return result

    monkeypatch.setattr(routes_drafts, "run_tool_loop", fake_loop)


def _stub_blocking_loop(
    monkeypatch: pytest.MonkeyPatch, result: tools.ToolLoopResult
) -> asyncio.Event:
    gate = asyncio.Event()

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        await gate.wait()
        return result

    monkeypatch.setattr(routes_drafts, "run_tool_loop", fake_loop)
    return gate


def _send_chat(client: TestClient, artifact_id: int, session_id: int, content: str = "Help"):
    return client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": content},
    )


def _retry_chat(client: TestClient, artifact_id: int, session_id: int):
    return client.post(f"/api/drafts/{artifact_id}/chat/{session_id}/retry")


def _parse_frames(response) -> list[dict]:
    frames = []
    for line in response.text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            with contextlib.suppress(json.JSONDecodeError):
                frames.append(json.loads(line[5:].strip()))
    return frames


# ---------------------------------------------------------------------------
# PLA-308: turn serialization
# ---------------------------------------------------------------------------


class TestTurnSerialization:
    def test_concurrent_turn_returns_409(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch
    ):
        """Two writer turns on the same session: the second is refused with 409."""
        _stub_blocking_loop(monkeypatch, tools.ToolLoopResult(content="Answer", calls=()))
        # Hold the claim manually to simulate an in-flight turn
        token = sessions.begin_turn(writer_session)
        resp = _send_chat(client, artifact_id, writer_session)
        assert resp.status_code == 409
        sessions.end_turn(writer_session, token)

    def test_claim_released_after_success(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch
    ):
        """After a successful turn, the session is available for the next one."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="First", calls=()))
        resp1 = _send_chat(client, artifact_id, writer_session, "Q1")
        assert resp1.status_code == 200
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Second", calls=()))
        resp2 = _send_chat(client, artifact_id, writer_session, "Q2")
        assert resp2.status_code == 200

    def test_claim_released_after_failure(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch
    ):
        """After a failed turn, the session is available for retry."""
        _stub_loop(
            monkeypatch,
            tools.ToolLoopResult(
                content="", calls=(), stopped=tools.UPSTREAM_FAILED, detail="Upstream error"
            ),
        )
        resp1 = _send_chat(client, artifact_id, writer_session)
        assert resp1.status_code == 200  # SSE stream opened
        frames = _parse_frames(resp1)
        assert any(f["type"] == "error" for f in frames)
        # Claim should be released - next turn should work
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="OK", calls=()))
        resp2 = _send_chat(client, artifact_id, writer_session, "Try again")
        assert resp2.status_code == 200

    def test_wrong_session_returns_404(
        self,
        client: TestClient,
        artifact_id: int,
        class_id: int,
        db: sqlite3.Connection,
        monkeypatch,
    ):
        """A session that doesn't belong to this draft is refused."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="X", calls=()))
        tutor_session = int(sessions.create_session(db, class_id)["id"])
        resp = _send_chat(client, artifact_id, tutor_session)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PLA-309: context budget
# ---------------------------------------------------------------------------


class TestContextBudget:
    def test_budget_passed_to_tool_loop(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch
    ):
        """The tool loop receives a ContextBudget."""
        received_budget = []

        async def capturing_loop(*args, **kwargs):
            received_budget.append(kwargs.get("context_budget"))
            return tools.ToolLoopResult(content="Answer", calls=())

        monkeypatch.setattr(routes_drafts, "run_tool_loop", capturing_loop)
        resp = _send_chat(client, artifact_id, writer_session)
        assert resp.status_code == 200
        assert len(received_budget) == 1
        budget = received_budget[0]
        assert isinstance(budget, tools.ContextBudget)
        assert budget.context_window == CONTEXT_WINDOW
        assert budget.generation_reserve > 0
        assert budget.tool_tokens > 0

    def test_oversized_question_refused(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A question that would overflow the context window is refused before persistence."""
        # Set a tiny context window
        db.execute("update settings set context_window = 100 where id = 1")
        db.commit()
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="X", calls=()))
        resp = _send_chat(client, artifact_id, writer_session, "x" * 500)
        assert resp.status_code == 400
        # The question should not have been persisted
        messages = sessions.list_messages(db, writer_session)
        assert len(messages) == 0


# ---------------------------------------------------------------------------
# PLA-310: durable attempt lifecycle
# ---------------------------------------------------------------------------


class TestAttemptLifecycle:
    def test_successful_turn_creates_completed_attempt(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A successful turn creates a completed attempt linked to the assistant message."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Great essay!", calls=()))
        resp = _send_chat(client, artifact_id, writer_session)
        assert resp.status_code == 200
        frames = _parse_frames(resp)
        done_frame = next((f for f in frames if f["type"] == "done"), None)
        assert done_frame is not None
        # Check the attempt
        messages = sessions.list_messages(db, writer_session)
        user_msg = next(m for m in messages if m["role"] == "user")
        attempt = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt is not None
        assert attempt["state"] == "completed"
        assert attempt["assistant_message_id"] == done_frame["message_id"]

    def test_failed_turn_creates_failed_attempt(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A failed turn creates a failed attempt with the stop reason."""
        _stub_loop(
            monkeypatch,
            tools.ToolLoopResult(
                content="", calls=(), stopped=tools.TIMEOUT, detail="Took too long"
            ),
        )
        resp = _send_chat(client, artifact_id, writer_session)
        frames = _parse_frames(resp)
        assert any(f["type"] == "error" for f in frames)
        messages = sessions.list_messages(db, writer_session)
        user_msg = next(m for m in messages if m["role"] == "user")
        attempt = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt is not None
        assert attempt["state"] == "failed"
        assert attempt["stopped_reason"] == tools.TIMEOUT

    def test_empty_answer_creates_failed_attempt(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A completed-but-empty turn is treated as failed."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="   ", calls=()))
        resp = _send_chat(client, artifact_id, writer_session)
        frames = _parse_frames(resp)
        assert any(f["type"] == "error" for f in frames)
        messages = sessions.list_messages(db, writer_session)
        user_msg = next(m for m in messages if m["role"] == "user")
        attempt = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt is not None
        assert attempt["state"] == "failed"
        assert attempt["stopped_reason"] == "empty"

    def test_atomic_completion(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """The assistant message and completed attempt are committed together."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Done", calls=()))
        _send_chat(client, artifact_id, writer_session)
        messages = sessions.list_messages(db, writer_session)
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        user_msg = next(m for m in messages if m["role"] == "user")
        attempt = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt["state"] == "completed"
        assert attempt["assistant_message_id"] == int(assistant_msg["id"])


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retry_failed_turn(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """Retry of a failed turn re-runs the model and produces a new attempt."""
        _stub_loop(
            monkeypatch,
            tools.ToolLoopResult(
                content="", calls=(), stopped=tools.UPSTREAM_FAILED, detail="Oops"
            ),
        )
        _send_chat(client, artifact_id, writer_session, "Explain this")
        # First attempt: failed
        messages = sessions.list_messages(db, writer_session)
        user_msg = next(m for m in messages if m["role"] == "user")
        attempt1 = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt1["state"] == "failed"
        # Retry
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Here you go", calls=()))
        resp = _retry_chat(client, artifact_id, writer_session)
        assert resp.status_code == 200
        frames = _parse_frames(resp)
        assert any(f["type"] == "done" for f in frames)
        # Second attempt: completed
        attempt2 = writer_attempts.latest_attempt_for_message(db, int(user_msg["id"]))
        assert attempt2["state"] == "completed"
        assert attempt2["id"] != attempt1["id"]
        # No duplicate user message
        user_messages = [
            m for m in sessions.list_messages(db, writer_session) if m["role"] == "user"
        ]
        assert len(user_messages) == 1

    def test_retry_completed_replays(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """Retry of a completed turn replays the stored reply (lost-response case)."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Original", calls=()))
        _send_chat(client, artifact_id, writer_session, "Help me")
        # Retry should replay, not re-run
        loop_called = []

        async def trap_loop(*args, **kwargs):
            loop_called.append(True)
            return tools.ToolLoopResult(content="Should not appear", calls=())

        monkeypatch.setattr(routes_drafts, "run_tool_loop", trap_loop)
        resp = _retry_chat(client, artifact_id, writer_session)
        assert resp.status_code == 200
        frames = _parse_frames(resp)
        token_frames = [f for f in frames if f["type"] == "token"]
        assert len(token_frames) == 1
        assert token_frames[0]["text"] == "Original"
        assert len(loop_called) == 0  # model was NOT re-run

    def test_retry_without_turn_returns_404(
        self, client: TestClient, artifact_id: int, writer_session: int
    ):
        """Retry on a session with no writer turns returns 404."""
        resp = _retry_chat(client, artifact_id, writer_session)
        assert resp.status_code == 404

    def test_retry_serialized(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A retry is serialized by the same per-session claim."""
        _stub_loop(
            monkeypatch,
            tools.ToolLoopResult(
                content="", calls=(), stopped=tools.UPSTREAM_FAILED, detail="Error"
            ),
        )
        _send_chat(client, artifact_id, writer_session)
        # Hold the claim
        token = sessions.begin_turn(writer_session)
        resp = _retry_chat(client, artifact_id, writer_session)
        assert resp.status_code == 409
        sessions.end_turn(writer_session, token)


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_reconcile_running_attempts(self, db: sqlite3.Connection, writer_session: int):
        """Startup reconciliation settles running writer attempts as stopped."""
        msg_id = sessions.add_message(db, writer_session, "user", "Q")
        writer_attempts.create_attempt(
            db, session_id=writer_session, user_message_id=msg_id, intent="general"
        )
        count = writer_attempts.reconcile_running(db)
        assert count == 1
        attempt = writer_attempts.latest_attempt_for_message(db, msg_id)
        assert attempt is not None
        assert attempt["state"] == "stopped"
        assert attempt["stopped_reason"] == "abandoned"

    def test_reconcile_skips_completed(self, db: sqlite3.Connection, writer_session: int):
        """Reconciliation does not touch completed attempts."""
        msg_id = sessions.add_message(db, writer_session, "user", "Q")
        attempt_id = writer_attempts.create_attempt(
            db, session_id=writer_session, user_message_id=msg_id, intent="general"
        )
        reply_id = sessions.add_message(db, writer_session, "assistant", "A")
        writer_attempts.mark_completed(db, attempt_id, reply_id)
        db.commit()
        count = writer_attempts.reconcile_running(db)
        assert count == 0


# ---------------------------------------------------------------------------
# Message annotation
# ---------------------------------------------------------------------------


class TestMessageAnnotation:
    def test_writer_attempt_in_message_list(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """The message list annotates writer turns with their attempt state."""
        _stub_loop(monkeypatch, tools.ToolLoopResult(content="Reply", calls=()))
        _send_chat(client, artifact_id, writer_session)
        resp = client.get(f"/api/sessions/{writer_session}/messages")
        assert resp.status_code == 200
        messages = resp.json()
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "writer_attempt" in user_msg
        assert user_msg["writer_attempt"]["state"] == "completed"

    def test_failed_attempt_annotated(
        self, client: TestClient, artifact_id: int, writer_session: int, monkeypatch, db
    ):
        """A failed writer attempt is visible in the message list."""
        _stub_loop(
            monkeypatch,
            tools.ToolLoopResult(content="", calls=(), stopped=tools.TIMEOUT, detail="Too slow"),
        )
        _send_chat(client, artifact_id, writer_session)
        resp = client.get(f"/api/sessions/{writer_session}/messages")
        messages = resp.json()
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["writer_attempt"]["state"] == "failed"
        assert user_msg["writer_attempt"]["detail"] == "Too slow"
