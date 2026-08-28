"""Tutor-chat durable attempt lifecycle, causal retry, and failure recovery (PLA-306).

Every accepted tutor turn has a durable attempt. A failed/stopped attempt shows
a truthful state and offers a causal Retry that reuses the original user message
rather than duplicating it. A completed attempt whose HTTP response was lost is
replayed from the database rather than re-running the model. Concurrent retries
are refused by the same per-session turn claim that serializes ordinary turns.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_chat
from backend.core import app_settings, sessions, tutor_attempts
from backend.core.errors import LyraError, UpstreamError
from backend.llm.client import StreamDelta
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect, get_db

StreamFactory = Callable[..., AsyncIterator[StreamDelta]]

QUESTION = "What is the fundamental theorem of calculus?"
ENDPOINT = "http://127.0.0.1:8081/v1"
CONTEXT_WINDOW = 32_768

NOTHING_RETRIEVED = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _stream_of(*tokens: str) -> StreamFactory:
    async def stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        for token in tokens:
            yield StreamDelta("answer", token)

    return stream


def _stream_then_fail(*tokens: str) -> StreamFactory:
    async def stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        for token in tokens:
            yield StreamDelta("answer", token)
        raise UpstreamError("The tutor endpoint is not reachable.")

    return stream


def _stream_fail_immediately() -> StreamFactory:
    async def stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        raise UpstreamError("Connection refused")
        yield  # noqa: RET503 - make it a generator  # type: ignore[misc]

    return stream


def _stream_unexpected_error() -> StreamFactory:
    async def stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        raise RuntimeError("Unexpected crash in model client")
        yield  # noqa: RET503  # type: ignore[misc]

    return stream


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "get_api_key", lambda: None)


@pytest.fixture(autouse=True)
def configured_endpoint(db: sqlite3.Connection) -> None:
    db.execute(
        "update settings set endpoint_url = ?, model = 'qwen', context_window = ?",
        (ENDPOINT, CONTEXT_WINDOW),
    )
    db.commit()


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_chat, "retrieve", lambda *a, **k: NOTHING_RETRIEVED)


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

    app.include_router(routes_chat.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def _send(client: TestClient, session_id: int, content: str = QUESTION) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": content, "mode": "guide", "document_id": None},
    )


def _retry(client: TestClient, session_id: int) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/retry",
        json={"mode": "guide", "document_id": None},
    )


def _messages(client: TestClient, session_id: int) -> list[dict]:
    return client.get(f"/api/sessions/{session_id}/messages").json()


def _parse_frames(response: httpx.Response) -> list[dict]:
    frames = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[6:]))
    return frames


def _seed_completed_turn(
    client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("First answer."))
    response = _send(client, session_id)
    assert response.status_code == 200
    _ = response.text  # consume stream
    assert sessions.active_turn(session_id) is None


def _seed_failed_turn(client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
    response = _send(client, session_id)
    assert response.status_code == 200
    _ = response.text
    assert sessions.active_turn(session_id) is None


# ---------------------------------------------------------------------------
# 1. Attempt creation: every accepted turn gets a durable attempt
# ---------------------------------------------------------------------------


class TestAttemptCreation:
    def test_a_successful_turn_creates_a_completed_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("The answer."))
        response = _send(client, session_id)
        assert response.status_code == 200
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"] is not None
        assert user_msg["tutor_attempt"]["state"] == "completed"

    def test_attempt_and_user_message_are_atomic(
        self,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
        client: TestClient,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))
        _send(client, session_id)

        conn = connect()
        try:
            user_rows = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchall()
            for row in user_rows:
                att = tutor_attempts.latest_attempt_for_message(conn, int(row["id"]))
                assert att is not None, "Every user message must have an attempt"
        finally:
            conn.close()

    def test_a_failed_turn_creates_a_failed_attempt_with_detail(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"
        assert user_msg["tutor_attempt"]["stopped_reason"] == "upstream_failed"
        assert user_msg["tutor_attempt"]["detail"] is not None

    def test_an_unexpected_error_creates_a_failed_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_unexpected_error())
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"
        assert user_msg["tutor_attempt"]["stopped_reason"] == "unexpected"

    def test_no_user_message_duplication_on_failure(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        _send(client, session_id)

        messages = _messages(client, session_id)
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1


# ---------------------------------------------------------------------------
# 2. Atomic completion: reply and completed state land together
# ---------------------------------------------------------------------------


class TestAtomicCompletion:
    def test_assistant_message_and_completed_state_are_atomic(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))
        _send(client, session_id)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            att = tutor_attempts.latest_attempt_for_message(conn, int(user_msg["id"]))
            assert att is not None
            assert att["state"] == "completed"
            assert att["assistant_message_id"] is not None
            reply = conn.execute(
                "select id from messages where id = ?", (int(att["assistant_message_id"]),)
            ).fetchone()
            assert reply is not None
        finally:
            conn.close()

    def test_completed_attempt_links_to_the_correct_reply(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("specific ", "reply."))
        _send(client, session_id)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            att = tutor_attempts.latest_attempt_for_message(conn, int(user_msg["id"]))
            assert att is not None
            reply = conn.execute(
                "select content from messages where id = ?", (int(att["assistant_message_id"]),)
            ).fetchone()
            assert reply["content"] == "specific reply."
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 3. Causal retry: reuses original user message, no duplication
# ---------------------------------------------------------------------------


class TestCausalRetry:
    def test_retry_after_failure_reuses_original_user_message(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered reply"))
        response = _retry(client, session_id)
        assert response.status_code == 200
        _ = response.text

        messages = _messages(client, session_id)
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == QUESTION
        assert user_msgs[0]["tutor_attempt"]["state"] == "completed"

    def test_retry_creates_a_new_attempt_on_the_same_message(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            first_attempts = conn.execute(
                "select id from tutor_turn_attempts where user_message_id = ?",
                (int(user_msg["id"]),),
            ).fetchall()
            assert len(first_attempts) == 1
        finally:
            conn.close()

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        _retry(client, session_id)

        conn = connect()
        try:
            all_attempts = conn.execute(
                "select id, state from tutor_turn_attempts where user_message_id = ?",
                (int(user_msg["id"]),),
            ).fetchall()
            assert len(all_attempts) == 2
            assert all_attempts[0]["state"] == "failed"
            assert all_attempts[1]["state"] == "completed"
        finally:
            conn.close()

    def test_retry_does_not_insert_duplicate_user_message(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))
        _retry(client, session_id)

        conn = connect()
        try:
            user_rows = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchall()
            assert len(user_rows) == 1
        finally:
            conn.close()

    def test_retry_with_no_tutor_attempt_is_404(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
    ) -> None:
        response = _retry(client, session_id)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 4. Replay: completed but HTTP-lost turn is replayed, not re-run
# ---------------------------------------------------------------------------


class TestReplay:
    def test_retry_on_completed_attempt_replays_stored_reply(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)

        called = {"count": 0}
        original_stream = _stream_of("should not be called")

        async def counting_stream(*args: object, **kwargs: object) -> AsyncIterator[StreamDelta]:
            called["count"] += 1
            async for delta in original_stream(*args, **kwargs):
                yield delta

        monkeypatch.setattr(routes_chat, "stream_chat", counting_stream)
        response = _retry(client, session_id)
        assert response.status_code == 200
        frames = _parse_frames(response)

        token_frames = [f for f in frames if f["type"] == "token"]
        assert len(token_frames) > 0
        replayed = "".join(f["text"] for f in token_frames)
        assert replayed == "First answer."
        assert called["count"] == 0

    def test_replay_does_not_create_a_new_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            before = conn.execute(
                "select count(*) as cnt from tutor_turn_attempts where user_message_id = ?",
                (int(user_msg["id"]),),
            ).fetchone()["cnt"]
        finally:
            conn.close()

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("nope"))
        _retry(client, session_id)

        conn = connect()
        try:
            after = conn.execute(
                "select count(*) as cnt from tutor_turn_attempts where user_message_id = ?",
                (int(user_msg["id"]),),
            ).fetchone()["cnt"]
        finally:
            conn.close()

        assert after == before


# ---------------------------------------------------------------------------
# 5. Concurrency: double-click, two tabs, retry racing a fresh turn
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_retry_overlapping_an_active_turn_is_409(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        token = sessions.begin_turn(session_id)

        response = _retry(client, session_id)
        assert response.status_code == 409

        sessions.end_turn(session_id, token)

    def test_send_overlapping_a_retry_is_409(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        token = sessions.begin_turn(session_id)

        response = _send(client, session_id, "new question")
        assert response.status_code == 409

        sessions.end_turn(session_id, token)

    def test_double_retry_is_serialized(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))

        response1 = _retry(client, session_id)
        assert response1.status_code == 200
        _ = response1.text

        response2 = _retry(client, session_id)
        assert response2.status_code == 200
        frames = _parse_frames(response2)
        token_frames = [f for f in frames if f["type"] == "token"]
        replayed = "".join(f["text"] for f in token_frames)
        assert replayed == "recovered"


# ---------------------------------------------------------------------------
# 6. Mid-stream failure: partial content, disconnect
# ---------------------------------------------------------------------------


class TestMidStreamFailure:
    def test_mid_stream_failure_creates_failed_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial ", "content "))
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"

    def test_mid_stream_failure_does_not_persist_partial_reply(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial"))
        _send(client, session_id)

        messages = _messages(client, session_id)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 0


# ---------------------------------------------------------------------------
# 7. Restart reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_running_attempts_are_settled_as_stopped_on_restart(
        self,
        db: sqlite3.Connection,
        session_id: int,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", "question")
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()

        att = tutor_attempts.latest_attempt_for_message(db, user_msg_id)
        assert att is not None
        assert att["state"] == "running"

        reconciled = tutor_attempts.reconcile_running(db)
        assert reconciled == 1

        att = tutor_attempts.latest_attempt_for_message(db, user_msg_id)
        assert att is not None
        assert att["state"] == "stopped"
        assert att["stopped_reason"] == "abandoned"

    def test_reconciliation_is_idempotent(
        self,
        db: sqlite3.Connection,
        session_id: int,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", "question")
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()

        tutor_attempts.reconcile_running(db)
        second = tutor_attempts.reconcile_running(db)
        assert second == 0

    def test_stopped_attempt_is_retryable_after_reconciliation(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", QUESTION)
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()
        tutor_attempts.reconcile_running(db)

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered after restart"))
        response = _retry(client, session_id)
        assert response.status_code == 200
        _ = response.text

        messages = _messages(client, session_id)
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["tutor_attempt"]["state"] == "completed"


# ---------------------------------------------------------------------------
# 8. Truthful transcript after reload
# ---------------------------------------------------------------------------


class TestTranscriptState:
    def test_failed_turn_shows_truthful_state_after_reload(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)

        messages = _messages(client, session_id)
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["tutor_attempt"]["state"] == "failed"
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 0

    def test_completed_turn_has_no_stopped_reason(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "completed"
        assert user_msg["tutor_attempt"]["stopped_reason"] is None


# ---------------------------------------------------------------------------
# 9. Newer turn safety: retry not offered when conversation moved on
# ---------------------------------------------------------------------------


class TestNewerTurnSafety:
    def test_retry_targets_the_last_user_message_only(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("second answer"))
        _send(client, session_id, "A completely different question")

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("should not see me"))
        response = _retry(client, session_id)
        assert response.status_code == 200
        frames = _parse_frames(response)
        token_frames = [f for f in frames if f["type"] == "token"]
        replayed = "".join(f["text"] for f in token_frames)
        assert replayed == "second answer"


# ---------------------------------------------------------------------------
# 10. Cascade: deleting a session removes attempts
# ---------------------------------------------------------------------------


class TestCascade:
    def test_deleting_a_session_removes_its_attempts(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)

        conn = connect()
        try:
            before = conn.execute(
                "select count(*) as cnt from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()["cnt"]
            assert before > 0
        finally:
            conn.close()

        response = client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 204

        conn = connect()
        try:
            after = conn.execute(
                "select count(*) as cnt from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()["cnt"]
            assert after == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 11. Regeneration still works with attempt tracking
# ---------------------------------------------------------------------------


class TestRegenerationCoexistence:
    def test_regeneration_after_completed_attempt_still_works(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("regenerated."))
        response = client.post(
            f"/api/sessions/{session_id}/regenerate",
            json={"mode": "guide"},
        )
        assert response.status_code == 200
        _ = response.text

        messages = _messages(client, session_id)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "regenerated."


# ---------------------------------------------------------------------------
# 12. Finding 3: interrupted partial output must be STOPPED, never COMPLETED
# ---------------------------------------------------------------------------


def _send_with_mode(
    client: TestClient,
    session_id: int,
    *,
    mode: str = "guide",
    document_id: int | None = None,
    content: str = QUESTION,
) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": content, "mode": mode, "document_id": document_id},
    )


def _retry_with_mode(
    client: TestClient,
    session_id: int,
    *,
    mode: str = "guide",
    document_id: int | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/retry",
        json={"mode": mode, "document_id": document_id},
    )


class TestInterruptedPartialOutput:
    """Finding 3: a cancelled turn with partial tokens must be STOPPED, not COMPLETED."""

    def test_mid_stream_upstream_failure_with_partial_tokens_is_failed(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial ", "content"))
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"
        assert user_msg["tutor_attempt"]["stopped_reason"] == "upstream_failed"

    def test_mid_stream_failure_does_not_persist_partial_reply_as_complete(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial"))
        _send(client, session_id)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            att = tutor_attempts.latest_attempt_for_message(conn, int(user_msg["id"]))
            assert att is not None
            assert att["state"] != "completed", (
                "A turn that failed mid-stream must not be marked completed"
            )
        finally:
            conn.close()

    def test_failed_partial_turn_is_retryable(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial "))
        _send(client, session_id)

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        response = _retry(client, session_id)
        assert response.status_code == 200
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "completed"


# ---------------------------------------------------------------------------
# 13. Finding 4: causal retry preserves original intent (mode/document_id)
# ---------------------------------------------------------------------------


class TestRetryOriginalIntent:
    """Finding 4: retry uses the failed attempt's mode/document_id, not the frontend's current."""

    def test_attempt_stores_mode_and_document_id(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        _send_with_mode(client, session_id, mode="show", document_id=42)

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            att = tutor_attempts.latest_attempt_for_message(conn, int(user_msg["id"]))
            assert att is not None
            assert att["mode"] == "show"
            assert att["document_id"] == 42
        finally:
            conn.close()

    def test_retry_uses_original_mode_not_current_frontend_mode(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        _send_with_mode(client, session_id, mode="show", document_id=7)

        captured: dict = {}

        original_stream = _stream_of("recovered")

        async def capturing_stream(
            endpoint_url: str,
            api_key: str | None,
            model: str | None,
            messages: list[dict[str, str]],
            **kwargs: object,
        ) -> AsyncIterator[StreamDelta]:
            captured["messages"] = messages
            async for delta in original_stream(endpoint_url, api_key, model, messages, **kwargs):
                yield delta

        monkeypatch.setattr(routes_chat, "stream_chat", capturing_stream)
        response = _retry_with_mode(client, session_id, mode="guide", document_id=99)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            attempts = conn.execute(
                "select mode, document_id from tutor_turn_attempts "
                "where user_message_id = ? order by id",
                (int(user_msg["id"]),),
            ).fetchall()
            retry_attempt = attempts[-1]
            assert retry_attempt["mode"] == "show", (
                "Retry must use the original attempt's mode, not the frontend's current mode"
            )
            assert retry_attempt["document_id"] == 7, (
                "Retry must use the original attempt's document_id"
            )
        finally:
            conn.close()

    def test_retry_falls_back_to_request_for_pre_migration_attempts(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", QUESTION)
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()
        tutor_attempts.fail_attempt(db, 1, stopped_reason="upstream_failed", detail="test")

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        response = _retry_with_mode(client, session_id, mode="show", document_id=5)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            attempts = conn.execute(
                "select mode, document_id from tutor_turn_attempts "
                "where user_message_id = ? order by id",
                (user_msg_id,),
            ).fetchall()
            retry_attempt = attempts[-1]
            assert retry_attempt["mode"] == "show"
            assert retry_attempt["document_id"] == 5
        finally:
            conn.close()

    def test_retry_preserves_explicit_document_id_none(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed turn with document_id=None must keep None on retry, not adopt the
        frontend's current selection."""
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        _send_with_mode(client, session_id, mode="show", document_id=None)

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        response = _retry_with_mode(client, session_id, mode="guide", document_id=99)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            user_msg = conn.execute(
                "select id from messages where session_id = ? and role = 'user'",
                (session_id,),
            ).fetchone()
            attempts = conn.execute(
                "select mode, document_id from tutor_turn_attempts "
                "where user_message_id = ? order by id",
                (int(user_msg["id"]),),
            ).fetchall()
            retry_attempt = attempts[-1]
            assert retry_attempt["mode"] == "show", (
                "Retry must use the original mode, not the frontend's current mode"
            )
            assert retry_attempt["document_id"] is None, (
                "Retry must preserve explicit None, not adopt the frontend's document_id"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 14. Response-lifetime settlement: every exit path settles the attempt
# ---------------------------------------------------------------------------


class TestResponseLifetimeSettlement:
    """When a response owning a tutor attempt exits, the attempt must be settled.

    stop_attempt() only updates state='running' rows, so it is safe to call
    unconditionally: completed/failed attempts are not reverted.
    """

    def test_normal_completion_attempt_is_completed(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Hello"))
        response = _send(client, session_id)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            att = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()
            assert att["state"] == "completed"
        finally:
            conn.close()

    def test_upstream_failure_attempt_is_failed(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        response = _send(client, session_id)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            att = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()
            assert att["state"] == "failed"
        finally:
            conn.close()

    def test_completion_then_response_exit_does_not_revert_to_stopped(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Answer"))
        response = _send(client, session_id)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            att = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()
            assert att["state"] == "completed", (
                "Response exit must not revert a completed attempt to stopped"
            )
        finally:
            conn.close()

    def test_failure_then_response_exit_does_not_revert_to_stopped(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        response = _send(client, session_id)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            att = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()
            assert att["state"] == "failed", (
                "Response exit must not revert a failed attempt to stopped"
            )
        finally:
            conn.close()

    def test_unexpected_error_attempt_is_not_left_running(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_unexpected_error())
        response = _send(client, session_id)
        _ = response.text

        conn = connect()
        try:
            att = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchone()
            assert att is not None
            assert att["state"] != "running", (
                "No attempt may be left running after the response exits"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 15. Finding 2: post-commit exceptions settle attempts, not strand them
# ---------------------------------------------------------------------------


class TestPostCommitStranding:
    """Finding 2: if bind_turn or touch_class fails after the attempt is committed,
    the attempt must be settled as stopped rather than left running forever."""

    def test_open_turn_post_commit_failure_settles_attempt(
        self,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))

        def failing_bind(sid: int, token: int, msg_id: int) -> None:
            raise RuntimeError("simulated bind_turn failure")

        monkeypatch.setattr(sessions, "bind_turn", failing_bind)

        app = FastAPI()

        @app.exception_handler(LyraError)
        async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
            return JSONResponse(status_code=exc.status, content={"detail": exc.message})

        app.include_router(routes_chat.router)
        app.dependency_overrides[get_db] = _request_db
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = _send(test_client, session_id)
            assert response.status_code == 500

        conn = connect()
        try:
            rows = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchall()
            for row in rows:
                assert row["state"] != "running", (
                    "A post-commit failure must not leave the attempt in running state"
                )
        finally:
            conn.close()

    def test_open_retry_post_commit_failure_settles_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)

        def failing_touch(conn: sqlite3.Connection, class_id: int) -> None:
            raise RuntimeError("simulated touch_class failure")

        monkeypatch.setattr(routes_chat, "touch_class", failing_touch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))

        app = FastAPI()

        @app.exception_handler(LyraError)
        async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
            return JSONResponse(status_code=exc.status, content={"detail": exc.message})

        app.include_router(routes_chat.router)
        app.dependency_overrides[get_db] = _request_db
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = _retry(test_client, session_id)
            assert response.status_code == 500

        conn = connect()
        try:
            rows = conn.execute(
                "select state from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchall()
            for row in rows:
                assert row["state"] != "running", (
                    "A post-commit failure must not leave the attempt in running state"
                )
        finally:
            conn.close()

    def test_session_claim_is_released_after_post_commit_failure(
        self,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))

        def failing_bind(sid: int, token: int, msg_id: int) -> None:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(sessions, "bind_turn", failing_bind)

        app = FastAPI()

        @app.exception_handler(LyraError)
        async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
            return JSONResponse(status_code=exc.status, content={"detail": exc.message})

        app.include_router(routes_chat.router)
        app.dependency_overrides[get_db] = _request_db
        with TestClient(app, raise_server_exceptions=False) as test_client:
            _send(test_client, session_id)

        assert sessions.active_turn(session_id) is None, (
            "The session claim must be released even when post-commit setup fails"
        )


# ---------------------------------------------------------------------------
# 15. Comprehensive disconnect/failure matrix
# ---------------------------------------------------------------------------


class TestDisconnectMatrix:
    """Every failure timing must settle the attempt and release the claim."""

    def test_failure_before_first_token_creates_failed_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"
        assert sessions.active_turn(session_id) is None

    def test_unexpected_error_releases_claim(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_unexpected_error())
        response = _send(client, session_id)
        _ = response.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "failed"
        assert sessions.active_turn(session_id) is None

    def test_successful_turn_releases_claim(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("reply"))
        response = _send(client, session_id)
        _ = response.text

        assert sessions.active_turn(session_id) is None

    def test_retry_after_failed_releases_claim(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        response = _retry(client, session_id)
        _ = response.text

        assert sessions.active_turn(session_id) is None

    def test_retry_replay_releases_claim(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_completed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("nope"))
        response = _retry(client, session_id)
        _ = response.text

        assert sessions.active_turn(session_id) is None

    def test_mid_stream_failure_releases_claim(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("partial"))
        response = _send(client, session_id)
        _ = response.text

        assert sessions.active_turn(session_id) is None

    def test_retry_of_stopped_attempt_creates_new_attempt(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", QUESTION)
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()
        tutor_attempts.reconcile_running(db)

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("recovered"))
        response = _retry(client, session_id)
        assert response.status_code == 200
        _ = response.text

        conn = connect()
        try:
            attempts = conn.execute(
                "select state from tutor_turn_attempts where user_message_id = ? order by id",
                (user_msg_id,),
            ).fetchall()
            assert len(attempts) == 2
            assert attempts[0]["state"] == "stopped"
            assert attempts[1]["state"] == "completed"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 16. Concurrency: retry while still running is 409
# ---------------------------------------------------------------------------


class TestConcurrencyMatrix:
    def test_send_while_another_turn_active_is_409(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
    ) -> None:
        token = sessions.begin_turn(session_id)
        response = _send(client, session_id)
        assert response.status_code == 409
        sessions.end_turn(session_id, token)

    def test_retry_while_running_attempt_is_refused(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_msg_id = sessions.add_message(db, session_id, "user", QUESTION)
        tutor_attempts.create_attempt(db, session_id=session_id, user_message_id=user_msg_id)
        db.commit()

        response = _retry(client, session_id)
        assert response.status_code == 400

        sessions._active_turns.clear()

    def test_sequential_retries_both_succeed(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        session_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_failed_turn(client, session_id, monkeypatch)
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_fail_immediately())
        response1 = _retry(client, session_id)
        _ = response1.text

        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("finally recovered"))
        response2 = _retry(client, session_id)
        assert response2.status_code == 200
        _ = response2.text

        messages = _messages(client, session_id)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert user_msg["tutor_attempt"]["state"] == "completed"

        conn = connect()
        try:
            attempts = conn.execute(
                "select state from tutor_turn_attempts where session_id = ? order by id",
                (session_id,),
            ).fetchall()
            assert len(attempts) == 3
            assert attempts[0]["state"] == "failed"
            assert attempts[1]["state"] == "failed"
            assert attempts[2]["state"] == "completed"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PLA-313: client-generated operation_id idempotency
# ---------------------------------------------------------------------------


def _send_with_op(
    client: TestClient,
    session_id: int,
    content: str = QUESTION,
    *,
    operation_id: str | None = None,
    mode: str = "guide",
    document_id: int | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/chat",
        json={
            "content": content,
            "mode": mode,
            "document_id": document_id,
            "operation_id": operation_id,
        },
    )


class TestOperationIdReplay:
    """PLA-313 blocker 1: a completed operation_id replays the stored reply with
    zero model calls, zero new messages, and zero new attempts."""

    def test_completed_operation_replays_stored_reply(
        self, client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0
        original = _stream_of("The answer is 42.")

        async def counting_stream(*a: object, **kw: object) -> AsyncIterator[StreamDelta]:
            nonlocal call_count
            call_count += 1
            async for delta in original(*a, **kw):
                yield delta

        monkeypatch.setattr(routes_chat, "stream_chat", counting_stream)
        r1 = _send_with_op(client, session_id, operation_id="op-1")
        assert r1.status_code == 200
        _ = r1.text
        assert call_count == 1

        r2 = _send_with_op(client, session_id, operation_id="op-1")
        assert r2.status_code == 200
        frames = _parse_frames(r2)
        assert call_count == 1, "replay must not call the model"

        token_texts = [f["text"] for f in frames if f["type"] == "token"]
        assert "".join(token_texts) == "The answer is 42."

    def test_replay_creates_no_new_messages_or_attempts(
        self, client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Reply."))
        _send_with_op(client, session_id, operation_id="op-2")

        messages_before = _messages(client, session_id)
        conn = connect()
        try:
            attempts_before = conn.execute(
                "select id from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        _send_with_op(client, session_id, operation_id="op-2")

        messages_after = _messages(client, session_id)
        conn = connect()
        try:
            attempts_after = conn.execute(
                "select id from tutor_turn_attempts where session_id = ?",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        assert len(messages_after) == len(messages_before)
        assert len(attempts_after) == len(attempts_before)


class TestOperationIdLogicalRequestBinding:
    """PLA-313 blocker 2: the operation_id is bound to the normalized content,
    mode, and document scope. A resubmit with different parameters is rejected."""

    def test_different_content_rejected(
        self, client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Answer."))
        r1 = _send_with_op(client, session_id, content="question A", operation_id="op-bind")
        assert r1.status_code == 200
        _ = r1.text

        r2 = _send_with_op(client, session_id, content="question B", operation_id="op-bind")
        assert r2.status_code == 409

    def test_different_mode_rejected(
        self, client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Answer."))
        r1 = _send_with_op(client, session_id, operation_id="op-mode", mode="guide")
        assert r1.status_code == 200
        _ = r1.text

        r2 = _send_with_op(client, session_id, operation_id="op-mode", mode="show")
        assert r2.status_code == 409

    def test_same_logical_request_accepted(
        self, client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Answer."))
        r1 = _send_with_op(client, session_id, operation_id="op-same")
        assert r1.status_code == 200
        _ = r1.text

        r2 = _send_with_op(client, session_id, operation_id="op-same")
        assert r2.status_code == 200
