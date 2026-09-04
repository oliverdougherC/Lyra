"""Per-session serialization and the durable attempt lifecycle for agent chat.

PLA-279 extends the tutor's per-session turn claim to the agent-chat route. The claim now
wraps `_plan_agent_turn` too, not only persistence: that preflight is read-only but
history-dependent (it trims history, freezes the capability snapshot, and builds the
budget and registry from it), so an overlapping turn must be refused before it can even
plan from a stale snapshot.

PLA-295 adds a durable attempt lifecycle so a retry is causal: one user message is one
turn, a retry reuses that message instead of appending a duplicate, tool audit rows carry
the attempt that produced them, and a lost successful response is replayed rather than
re-run.

Concurrency is made a fact rather than a probability: an in-flight turn is represented by
holding its claim (exactly what a running turn holds), and the races that matter - two
retries, a retry against a new turn - are driven with real overlapping coroutines blocked
inside the tool loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat, routes_chat
from backend.core import (
    agent_attempts,
    agent_store,
    app_settings,
    profiles,
    sessions,
    source_ledger,
    tool_audit,
    web_research,
)
from backend.core.errors import ConflictError, LyraError
from backend.llm import client as llm_client
from backend.llm import tools
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect, get_db

ENDPOINT = "http://127.0.0.1:8080/v1"
REMOTE_ENDPOINT = "http://203.0.113.10:8081/v1"


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def configured_endpoint(db: sqlite3.Connection) -> None:
    db.execute(
        "update settings set endpoint_url = ?, model = 'qwen', tools_supported = 1 where id = 1",
        (ENDPOINT,),
    )
    db.commit()


@pytest.fixture(autouse=True)
def released_claims() -> Iterator[None]:
    """No test may leak a turn claim into the next: the registry is process-global."""
    yield
    sessions._active_turns.clear()


@pytest.fixture(autouse=True)
def empty_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise claims and attempt lifecycle, not retrieval. The hosted CI
    has no embedding model downloaded, so serve the same empty retrieval the tutor tests
    stub (`routes_chat.retrieve`), for both routes these turns can take."""

    def nothing_retrieved(
        conn: object,
        class_id: object,
        query: object,
        budget_tokens: object,
        document_id: object | None = None,
    ) -> RetrievalResult:
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", nothing_retrieved)
    monkeypatch.setattr(routes_chat, "retrieve", nothing_retrieved)


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_agent_chat.router)
    app.include_router(routes_chat.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def _stub_loop(monkeypatch: pytest.MonkeyPatch, result: tools.ToolLoopResult) -> None:
    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return result

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)


def _spy_registry(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record registry builds, to prove a refused overlap never plans."""
    built: list[object] = []
    original = routes_agent_chat.agent_tools.build_agent_registry

    def capture(*args: object, **kwargs: object):  # noqa: ANN002, ANN003, ANN202
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", capture)
    return built


def _send(
    client: TestClient,
    class_id: int,
    session_id: int,
    content: str = "A question",
    *,
    profile: str = "code",
):
    return client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": content, "profile": profile},
    )


def _retry(client: TestClient, class_id: int, session_id: int):
    return client.post(f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry")


def _enable_workspace(
    db: sqlite3.Connection,
    class_id: int,
    root: Path,
    *,
    changes: bool = False,
    commands: bool = False,
) -> None:
    root.mkdir()
    agent_store.attach_workspace(db, class_id, root_path=str(root))
    agent_store.update_workspace_grants(
        db,
        class_id,
        read_enabled=changes,
        change_proposals_enabled=changes,
        commands_enabled=commands,
    )


def _target_attempt(
    conn: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: int,
) -> int:
    rows = conn.execute(
        "select attempt_id from agent_attempt_targets "
        "where target_kind = ? and target_id = ? order by rowid",
        (target_kind, str(target_id)),
    ).fetchall()
    assert len(rows) == 1
    return int(rows[0]["attempt_id"])


def _create_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    content: str,
    profile: str = "code",
) -> int:
    user_message_id = sessions.add_message(conn, session_id, "user", content)
    return agent_attempts.create_attempt(
        conn,
        session_id=session_id,
        user_message_id=user_message_id,
        profile=profile,
    )


class _FailFinalCommit:
    """Connection proxy that fails the commit containing reply + completion exactly once."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.failed = False

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._conn, name)

    def commit(self) -> None:
        if not self.failed and self._conn.in_transaction:
            reply = self._conn.execute(
                "select count(*) from messages where role = 'assistant'"
            ).fetchone()[0]
            attempt = self._conn.execute(
                "select state from agent_turn_attempts order by id desc limit 1"
            ).fetchone()
            if reply and attempt is not None and attempt["state"] == agent_attempts.COMPLETED:
                self.failed = True
                raise sqlite3.OperationalError("injected final commit failure")
        self._conn.commit()


# --- Overlap is a deterministic conflict, for every pairing --------------------------


def test_an_agent_send_overlapping_an_active_turn_is_refused_and_persists_nothing(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="Never sent."))
    built = _spy_registry(monkeypatch)
    token = sessions.begin_turn(session_id)

    response = _send(client, class_id, session_id)

    assert response.status_code == 409
    # Refused before persistence: no orphaned question, no claimed title, no attempt.
    assert sessions.list_messages(db, session_id) == []
    assert sessions.get_session(db, session_id)["title"] is None
    # Stale-plan regression: the refused turn never even read history to plan (PLA-279).
    assert built == []
    active = sessions.active_turn(session_id)
    assert active is not None and active.token == token


def test_an_agent_send_overlapping_a_tutor_turn_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int, session_id: int
) -> None:
    """A tutor turn holding the session refuses an agent turn: one slot, not one per kind."""
    sessions.begin_turn(session_id)  # a tutor turn's claim
    assert _send(client, class_id, session_id).status_code == 409
    assert sessions.list_messages(db, session_id) == []


def test_a_tutor_send_overlapping_an_agent_turn_is_refused(
    client: TestClient, db: sqlite3.Connection, session_id: int
) -> None:
    """The mirror: an agent turn's claim refuses a tutor send with the same 409."""
    sessions.begin_turn(session_id)  # an agent turn's claim
    response = client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": "Tutor question", "mode": "guide", "document_id": None},
    )
    assert response.status_code == 409
    assert sessions.list_messages(db, session_id) == []


async def test_a_real_agent_turn_in_flight_refuses_both_an_agent_and_a_tutor_turn(
    db: sqlite3.Connection, class_id: int, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """True concurrency: an agent turn blocked inside the tool loop holds the one slot, and
    both a second agent turn and a tutor turn racing it are refused with a 409."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        entered.set()
        await release.wait()
        return tools.ToolLoopResult(content="Done at last.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", gated_loop)
    conn_a, conn_b = connect(), connect()
    try:
        payload = routes_agent_chat.AgentChatRequest(content="Hold the slot", profile="code")
        task = asyncio.create_task(
            routes_agent_chat.send_agent_chat(class_id, session_id, payload, conn_a)
        )
        await asyncio.wait_for(entered.wait(), timeout=5.0)  # A is inside the loop, holding

        with pytest.raises(ConflictError):
            await routes_agent_chat.send_agent_chat(
                class_id,
                session_id,
                routes_agent_chat.AgentChatRequest(content="Overlap", profile="research"),
                conn_b,
            )
        with pytest.raises(ConflictError):
            await routes_chat.send_chat(
                session_id,
                routes_chat.ChatRequest(content="Tutor overlap", mode="guide", document_id=None),
                conn_b,
            )

        release.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result.content == "Done at last."
    finally:
        release.set()
        conn_a.close()
        conn_b.close()
    assert sessions.active_turn(session_id) is None
    # Only the one accepted turn persisted anything.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]


# --- Release must dominate every failure category -----------------------------------


def test_a_consent_refusal_releases_the_session_and_persists_nothing(
    client: TestClient, db: sqlite3.Connection, class_id: int, session_id: int
) -> None:
    db.execute("update settings set endpoint_url = ?, remote_ack = 0", (REMOTE_ENDPOINT,))
    db.commit()
    assert _send(client, class_id, session_id).status_code == 400
    assert sessions.active_turn(session_id) is None
    assert sessions.list_messages(db, session_id) == []


def test_an_impossible_context_refusal_releases_the_session(
    client: TestClient, db: sqlite3.Connection, class_id: int, session_id: int
) -> None:
    # A turn the window cannot host on EITHER surface: the smallest window plus a question
    # too large even for the tool-less fallback. The refusal is local and pre-flight, so it
    # releases the session claim and persists nothing. (A window too small for the TOOL
    # surface alone no longer refuses - the turn falls back to the tool-less answer.)
    db.execute("update settings set context_window = 512")
    db.commit()
    assert _send(client, class_id, session_id, content="word " * 2000).status_code == 400
    assert sessions.active_turn(session_id) is None
    assert sessions.list_messages(db, session_id) == []


@pytest.mark.parametrize(
    ("stopped", "detail"),
    [
        (tools.TIMEOUT, "Checking took too long and was stopped."),
        (tools.UPSTREAM_FAILED, "The tutor endpoint could not be reached."),
        (tools.DEPTH, "Checking stopped after 24 rounds of tool calls."),
        (tools.CONTEXT_OVERFLOW, tools._OVERFLOW_DETAIL),
        (tools.OUTPUT_LIMIT, tools._OUTPUT_LIMIT_DETAIL),
    ],
)
def test_every_tool_loop_failure_category_releases_the_session(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
    stopped: str,
    detail: str,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="", stopped=stopped, detail=detail))
    response = _send(client, class_id, session_id)
    assert response.status_code in {502, 503, 504}
    assert sessions.active_turn(session_id) is None
    # The turn is a failed attempt, not a silent one: the user message stays, no reply.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    latest = agent_attempts.latest_attempts_by_message(db, session_id)
    (attempt,) = latest.values()
    assert attempt["state"] == agent_attempts.FAILED
    assert attempt["stopped_reason"] == stopped


def test_an_empty_completed_answer_releases_the_session_and_fails_the_attempt(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="   "))
    assert _send(client, class_id, session_id).status_code == 400
    assert sessions.active_turn(session_id) is None
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.FAILED


def test_an_unexpected_loop_exception_releases_the_session(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        raise RuntimeError("loop blew up")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", boom)
    with pytest.raises(RuntimeError):
        _send(client, class_id, session_id)
    assert sessions.active_turn(session_id) is None


def test_a_planning_failure_releases_the_session_and_persists_no_user_turn(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = routes_agent_chat.agent_tools.build_agent_registry

    def maybe_raise(*args: object, **kwargs: object):  # noqa: ANN002, ANN003, ANN202
        if kwargs.get("private_context"):
            raise RuntimeError("registry construction failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", maybe_raise)
    with pytest.raises(RuntimeError):
        _send(client, class_id, session_id)
    assert sessions.active_turn(session_id) is None
    assert sessions.list_messages(db, session_id) == []


async def test_cancellation_mid_loop_releases_the_session(
    db: sqlite3.Connection, class_id: int, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop cancels the in-flight turn mid tool-loop. The route must settle the attempt
    as stopped, release the session claim, and still complete the request with a bounded
    response - a request task that dies without one makes the HTTP middleware log a 500."""
    entered = asyncio.Event()

    async def hangs(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        entered.set()
        await asyncio.Event().wait()  # never returns
        raise AssertionError("unreachable")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", hangs)
    conn = connect()
    try:
        payload = routes_agent_chat.AgentChatRequest(content="Cancel me", profile="code")
        task = asyncio.create_task(
            routes_agent_chat.send_agent_chat(class_id, session_id, payload, conn)
        )
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        # Stop the in-flight turn through the real endpoint (it cancels the turn task, not
        # the request task, so the request itself can still settle with a response).
        stopped = await routes_agent_chat.stop_agent_chat(class_id, session_id, conn)
        assert stopped == {"stopped": True, "settling": False}
        # The request completes with a bounded "stopped" body, not a bare cancellation.
        result = await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    finally:
        conn.close()
    assert isinstance(result, JSONResponse)
    assert json.loads(result.body)["stopped"] == "stopped"
    assert sessions.active_turn(session_id) is None
    # The cancelled turn is settled truthfully, not left reading as forever in flight, so
    # the transcript can offer Retry instead of a perpetual spinner.
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.STOPPED


# --- The attempt lifecycle and retry (PLA-295) --------------------------------------


def test_a_failed_turn_shows_a_truthful_attempt_state_in_the_transcript(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow"))
    assert _send(client, class_id, session_id, "Explain part b").status_code == 504

    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["agent_attempt"]["state"] == "failed"
    assert messages[0]["agent_attempt"]["stopped_reason"] == "timeout"


def test_a_retry_reuses_the_user_message_and_passes_the_prompt_once(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow"))
    assert _send(client, class_id, session_id, "The one question").status_code == 504
    first = sessions.list_messages(db, session_id)
    assert [m["content"] for m in first] == ["The one question"]

    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        captured["messages"] = args[3]
        return tools.ToolLoopResult(content="Second time's the charm.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    response = _retry(client, class_id, session_id)

    assert response.status_code == 200, response.text
    # The original user message was reused: still exactly one copy, plus the new reply.
    stored = sessions.list_messages(db, session_id)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", "The one question"),
        ("assistant", "Second time's the charm."),
    ]
    # The prompt carried the question exactly once - as the current message, not also as
    # history.
    prompt = captured["messages"]
    assert prompt[-1] == {"role": "user", "content": "The one question"}
    assert sum(1 for m in prompt if m.get("content") == "The one question") == 1
    # Two attempts on the one turn: the failed one survives beside the completed retry.
    attempts = db.execute(
        "select state from agent_turn_attempts where user_message_id = ? order by id",
        (int(stored[0]["id"]),),
    ).fetchall()
    assert [row["state"] for row in attempts] == ["failed", "completed"]


def test_turn_completion_is_atomic_so_a_crash_cannot_orphan_a_reply(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assistant reply and the attempt's completion commit together. If the completion
    write fails, the reply is rolled back too - so a crash in that window can never leave a
    stored reply beside a still-running attempt, which a later retry would re-run into a
    second answer."""
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="An answer that must not orphan."))

    def boom(conn: sqlite3.Connection, attempt_id: int, message_id: int) -> None:
        raise RuntimeError("crash between the two writes")

    monkeypatch.setattr(routes_agent_chat.agent_attempts, "mark_completed", boom)
    with pytest.raises(RuntimeError):
        _send(client, class_id, session_id, "Answer atomically")

    # No orphaned assistant reply: the whole completion rolled back, leaving only the user
    # turn, and the fresh settlement records the persistence failure truthfully.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.FAILED
    assert attempt["stopped_reason"] == "persistence_failed"
    assert attempt["assistant_message_id"] is None
    assert sessions.active_turn(session_id) is None


def test_assistant_insert_failure_rolls_back_and_settles_in_a_fresh_transaction(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="Generated but not stored."))
    original_fail = agent_attempts.fail_attempt
    settled_after_rollback: list[bool] = []

    original_insert = sessions.insert_message

    def fail_insert(*args: object, **kwargs: object) -> int:
        if len(args) > 2 and args[2] == "assistant":
            raise sqlite3.OperationalError("injected assistant insert failure")
        return original_insert(*args, **kwargs)  # type: ignore[arg-type]

    def capture_settlement(
        conn: sqlite3.Connection,
        attempt_id: int,
        *,
        stopped_reason: str,
        detail: str,
    ) -> None:
        settled_after_rollback.append(not conn.in_transaction)
        original_fail(
            conn,
            attempt_id,
            stopped_reason=stopped_reason,
            detail=detail,
        )

    monkeypatch.setattr(routes_agent_chat.sessions, "insert_message", fail_insert)
    monkeypatch.setattr(routes_agent_chat.agent_attempts, "fail_attempt", capture_settlement)
    with pytest.raises(sqlite3.OperationalError, match="assistant insert"):
        _send(client, class_id, session_id, "Persist this")

    assert settled_after_rollback == [True]
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.FAILED
    assert attempt["stopped_reason"] == "persistence_failed"


async def test_final_commit_failure_leaves_no_reply_or_running_attempt(
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="Generated before commit failed."))
    raw = connect()
    conn = _FailFinalCommit(raw)
    try:
        payload = routes_agent_chat.AgentChatRequest(content="Commit atomically", profile="code")
        with pytest.raises(sqlite3.OperationalError, match="final commit"):
            await routes_agent_chat.send_agent_chat(class_id, session_id, payload, conn)  # type: ignore[arg-type]
    finally:
        raw.close()

    assert conn.failed is True
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.FAILED
    assert attempt["stopped_reason"] == "persistence_failed"
    assert sessions.active_turn(session_id) is None


def test_retry_after_final_persistence_failure_is_causal_and_stores_one_reply(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="The regenerated answer."))
    original_insert = sessions.insert_message
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> int:
        nonlocal calls
        if len(args) > 2 and args[2] == "assistant":
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("injected one-shot insert failure")
        return original_insert(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(routes_agent_chat.sessions, "insert_message", fail_once)
    with pytest.raises(sqlite3.OperationalError, match="one-shot"):
        _send(client, class_id, session_id, "Answer once")

    response = _retry(client, class_id, session_id)
    assert response.status_code == 200, response.text
    stored = sessions.list_messages(db, session_id)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", "Answer once"),
        ("assistant", "The regenerated answer."),
    ]
    attempts = db.execute(
        "select state, assistant_message_id from agent_turn_attempts order by id"
    ).fetchall()
    assert [row["state"] for row in attempts] == ["failed", "completed"]
    assert attempts[0]["assistant_message_id"] is None
    assert attempts[1]["assistant_message_id"] == stored[-1]["id"]


def test_failed_fallback_settlement_logs_bounded_context_and_leaves_reconciliation_fallback(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="Generated."))

    original_insert = sessions.insert_message

    def fail_insert(*args: object, **kwargs: object) -> int:
        if len(args) > 2 and args[2] == "assistant":
            raise sqlite3.OperationalError("secret database path /private/example")
        return original_insert(*args, **kwargs)  # type: ignore[arg-type]

    def fail_settlement(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("second secret database path /private/other")

    monkeypatch.setattr(routes_agent_chat.sessions, "insert_message", fail_insert)
    monkeypatch.setattr(routes_agent_chat.agent_attempts, "fail_attempt", fail_settlement)
    with pytest.raises(sqlite3.OperationalError, match="secret database path"):
        _send(client, class_id, session_id, "Fallback")

    assert "startup reconciliation remains the fallback" in caplog.text
    assert "/private/example" not in caplog.text
    assert "/private/other" not in caplog.text
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] == agent_attempts.RUNNING


def test_a_retry_with_no_agent_turn_to_retry_is_a_404(
    client: TestClient, class_id: int, session_id: int
) -> None:
    assert _retry(client, class_id, session_id).status_code == 404


def test_a_retry_of_a_lost_successful_response_replays_without_running_the_model(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn already completed and stored its reply, but the HTTP response was lost. The
    retry must replay that reply, never run the model again."""
    _stub_loop(monkeypatch, tools.ToolLoopResult(content="The committed answer."))
    assert _send(client, class_id, session_id, "Answer me").status_code == 200
    committed = sessions.list_messages(db, session_id)
    assert [m["role"] for m in committed] == ["user", "assistant"]

    ran = {"count": 0}

    async def must_not_run(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        ran["count"] += 1
        return tools.ToolLoopResult(content="A second, wrong answer.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", must_not_run)
    response = _retry(client, class_id, session_id)

    assert response.status_code == 200, response.text
    assert ran["count"] == 0  # no new upstream run
    assert response.json()["content"] == "The committed answer."
    # No duplicate assistant message: the conversation is unchanged.
    assert [(m["role"], m["content"]) for m in sessions.list_messages(db, session_id)] == [
        (m["role"], m["content"]) for m in committed
    ]


def test_tool_records_are_tied_to_the_attempt_that_produced_them_across_a_retry(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after a successful tool call, then a retry that runs its own tool. Each tool
    audit row carries the attempt that wrote it; the retry never re-labels prior evidence."""

    def _loop_that_runs_a_tool(result: tools.ToolLoopResult):
        async def loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
            registry = kwargs["registry"]
            assert isinstance(registry, dict)
            registry["cas_evaluate"].handler(expression="1+1")  # one succeeded tool row
            return result

        return loop

    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        _loop_that_runs_a_tool(
            tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")
        ),
    )
    assert _send(client, class_id, session_id, "Use a tool then fail").status_code == 504

    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        _loop_that_runs_a_tool(tools.ToolLoopResult(content="Recovered.")),
    )
    assert _retry(client, class_id, session_id).status_code == 200

    attempts = db.execute("select id, state from agent_turn_attempts order by id").fetchall()
    assert [row["state"] for row in attempts] == ["failed", "completed"]
    first_attempt, second_attempt = int(attempts[0]["id"]), int(attempts[1]["id"])
    rows = db.execute(
        "select attempt_id from tool_audit_events order by started_at, rowid"
    ).fetchall()
    attempt_ids = [row["attempt_id"] for row in rows]
    # Two tool rows, one per attempt, each tagged with its own attempt - the retry's tool
    # activity is never attributed to the failed attempt or vice versa.
    assert first_attempt in attempt_ids
    assert second_attempt in attempt_ids
    assert attempt_ids.count(first_attempt) == 1
    assert attempt_ids.count(second_attempt) == 1


def test_workspace_proposals_keep_attempt_ownership_across_retry_and_restart(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _enable_workspace(db, class_id, root, changes=True)
    source = root / "answer.txt"
    source.write_text("original\n")
    base_hash = hashlib.sha256(b"original\n").hexdigest()
    run = 0
    original_finish = tool_audit.finish_event

    def fail_first_audit_finish(
        conn: sqlite3.Connection, event_id: str, **kwargs: object
    ) -> object:
        event = conn.execute(
            "select tool from tool_audit_events where id = ?", (event_id,)
        ).fetchone()
        if run == 1 and event["tool"] == "create_workspace_change":
            raise sqlite3.OperationalError("injected audit settlement failure")
        return original_finish(conn, event_id, **kwargs)  # type: ignore[arg-type]

    async def proposal_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        nonlocal run
        run += 1
        registry = kwargs["registry"]
        proposal = registry["create_workspace_change"].handler(  # type: ignore[index]
            relative_path="answer.txt",
            observed_base_hash=base_hash,
            proposed_content=f"proposal {run}\n",
            rationale=f"attempt {run}",
        )
        assert proposal.ok is True
        if run == 1:
            return tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")
        return tools.ToolLoopResult(content="Recovered with a separate proposal.")

    monkeypatch.setattr(tool_audit, "finish_event", fail_first_audit_finish)
    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", proposal_loop)
    failed = _send(client, class_id, session_id, "Propose a change")
    assert failed.status_code == 504
    old_id = int(failed.json()["workspace_change_ids"][0])

    retried = _retry(client, class_id, session_id)
    assert retried.status_code == 200, retried.text
    new_id = int(retried.json()["workspace_change_ids"][0])
    assert new_id != old_id

    attempts = db.execute("select id, state from agent_turn_attempts order by id").fetchall()
    assert [row["state"] for row in attempts] == ["failed", "completed"]
    first_attempt, second_attempt = (int(row["id"]) for row in attempts)

    # A fresh connection plus the startup reconcilers is the durable reload/restart proof.
    reloaded = connect()
    try:
        assert agent_attempts.reconcile_running(reloaded) == 0
        assert tool_audit.reconcile_inflight(reloaded) == 1
        assert (
            _target_attempt(reloaded, target_kind="workspace_change", target_id=old_id)
            == first_attempt
        )
        assert (
            _target_attempt(reloaded, target_kind="workspace_change", target_id=new_id)
            == second_attempt
        )
        states = reloaded.execute(
            "select id, state from workspace_changes where id in (?, ?) order by id",
            (old_id, new_id),
        ).fetchall()
        assert [(row["id"], row["state"]) for row in states] == [
            (old_id, "pending"),
            (new_id, "pending"),
        ]
    finally:
        reloaded.close()
    assert source.read_text() == "original\n"  # Retry never applies either proposal.


def test_command_proposals_keep_attempt_ownership_across_retry_and_restart(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _enable_workspace(db, class_id, root, commands=True)
    run = 0
    original_finish = tool_audit.finish_event

    def fail_first_audit_finish(
        conn: sqlite3.Connection, event_id: str, **kwargs: object
    ) -> object:
        event = conn.execute(
            "select tool from tool_audit_events where id = ?", (event_id,)
        ).fetchone()
        if run == 1 and event["tool"] == "create_command_request":
            raise sqlite3.OperationalError("injected audit settlement failure")
        return original_finish(conn, event_id, **kwargs)  # type: ignore[arg-type]

    async def proposal_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        nonlocal run
        run += 1
        registry = kwargs["registry"]
        proposal = registry["create_command_request"].handler(  # type: ignore[index]
            argv=["python", "-V"],
            relative_cwd=".",
            reason=f"verify attempt {run}",
            expected_signal="version",
            timeout_seconds=30,
        )
        assert proposal.ok is True
        if run == 1:
            return tools.ToolLoopResult(content="", stopped=tools.UPSTREAM_FAILED, detail="down")
        return tools.ToolLoopResult(content="Recovered with a separate command request.")

    monkeypatch.setattr(tool_audit, "finish_event", fail_first_audit_finish)
    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", proposal_loop)
    failed = _send(client, class_id, session_id, "Propose a command", profile="command")
    assert failed.status_code == 502
    old_id = int(failed.json()["command_request_ids"][0])

    retried = _retry(client, class_id, session_id)
    assert retried.status_code == 200, retried.text
    new_id = int(retried.json()["command_request_ids"][0])
    assert new_id != old_id

    attempts = db.execute("select id, state from agent_turn_attempts order by id").fetchall()
    first_attempt, second_attempt = (int(row["id"]) for row in attempts)
    reloaded = connect()
    try:
        assert agent_attempts.reconcile_running(reloaded) == 0
        assert tool_audit.reconcile_inflight(reloaded) == 1
        assert (
            _target_attempt(reloaded, target_kind="command_request", target_id=old_id)
            == first_attempt
        )
        assert (
            _target_attempt(reloaded, target_kind="command_request", target_id=new_id)
            == second_attempt
        )
        states = reloaded.execute(
            "select id, state, started_at from command_requests where id in (?, ?) order by id",
            (old_id, new_id),
        ).fetchall()
        assert [(row["id"], row["state"], row["started_at"]) for row in states] == [
            (old_id, "pending", None),
            (new_id, "pending", None),
        ]
    finally:
        reloaded.close()


def test_source_and_profile_proposals_keep_attempt_ownership_across_retry_and_restart(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_settings.update_settings_row(
        db,
        {"allow_web_research": 1, "source_content_enabled": 1},
    )
    run = 0
    revision_ids: list[int] = []
    original_finish = tool_audit.finish_event

    def fail_first_proposal_audit_finishes(
        conn: sqlite3.Connection, event_id: str, **kwargs: object
    ) -> object:
        event = conn.execute(
            "select tool from tool_audit_events where id = ?", (event_id,)
        ).fetchone()
        if run == 1 and str(event["tool"]).startswith("propose_"):
            raise sqlite3.OperationalError("injected audit settlement failure")
        return original_finish(conn, event_id, **kwargs)  # type: ignore[arg-type]

    def fake_fetch(url: str, **kwargs: object) -> dict[str, object]:
        return {
            "url": url,
            "final_url": url,
            "title": "Stable source identity",
            "accessed_at": "2026-08-22T12:00:00+00:00",
            "content_type": "text/plain",
            "snapshot": f"Evidence for attempt {run}.",
            "truncated": False,
            "warning": None,
        }

    monkeypatch.setattr(web_research, "fetch_source", fake_fetch)

    async def proposal_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        nonlocal run
        run += 1
        registry = kwargs["registry"]
        fetched = registry["fetch_source"].handler(  # type: ignore[index]
            url="https://example.com/stable-source"
        )
        assert fetched.ok is True
        source = registry["propose_source_snapshot"].handler(  # type: ignore[index]
            fetch_id=str(fetched.value["fetch_id"])
        )
        assert source.ok is True
        source_id = int(source.value["source"]["id"])  # type: ignore[index]
        revision_ids.append(int(source.value["source_revision_id"]))
        excerpt = registry["propose_source_excerpt"].handler(  # type: ignore[index]
            source_id=source_id,
            excerpt=f"Evidence for attempt {run}.",
        )
        assert excerpt.ok is True
        excerpt_id = int(excerpt.value["excerpt"]["id"])  # type: ignore[index]
        fact = registry["propose_profile_fact"].handler(  # type: ignore[index]
            kind="note",
            label=f"Method {run}",
            value=f"Use evidence {run}",
            source_id=source_id,
            excerpt_id=excerpt_id,
        )
        assert fact.ok is True
        if run == 1:
            return tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")
        return tools.ToolLoopResult(content="Recovered with separate research proposals.")

    monkeypatch.setattr(tool_audit, "finish_event", fail_first_proposal_audit_finishes)
    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", proposal_loop)
    failed = _send(client, class_id, session_id, "Research and propose", profile="research")
    assert failed.status_code == 504
    old_source = int(failed.json()["source_ids"][0])
    old_fact = int(failed.json()["profile_fact_ids"][0])

    retried = _retry(client, class_id, session_id)
    assert retried.status_code == 200, retried.text
    # The retry updated the same stable source row, so it does not report that older id as
    # newly produced. Its new causal artifact is the source revision in tool activity.
    assert retried.json()["source_ids"] == []
    new_fact = int(retried.json()["profile_fact_ids"][0])
    assert new_fact != old_fact
    assert any(
        item["target_kind"] == "source_revision" and int(item["target_id"]) == revision_ids[1]
        for item in retried.json()["activity"]
    )

    attempts = db.execute("select id, state from agent_turn_attempts order by id").fetchall()
    first_attempt, second_attempt = (int(row["id"]) for row in attempts)
    reloaded = connect()
    try:
        assert agent_attempts.reconcile_running(reloaded) == 0
        assert tool_audit.reconcile_inflight(reloaded) == 3
        assert (
            _target_attempt(reloaded, target_kind="source", target_id=old_source) == first_attempt
        )
        assert (
            _target_attempt(reloaded, target_kind="profile_fact", target_id=old_fact)
            == first_attempt
        )
        assert (
            _target_attempt(reloaded, target_kind="profile_fact", target_id=new_fact)
            == second_attempt
        )

        revisions = reloaded.execute(
            "select attempt_id from agent_attempt_targets "
            "where target_kind = 'source_revision' order by target_id"
        ).fetchall()
        assert [int(row["attempt_id"]) for row in revisions] == [
            first_attempt,
            second_attempt,
        ]

        excerpts = reloaded.execute(
            "select target_id, attempt_id from agent_attempt_targets "
            "where target_kind = 'source_excerpt' order by rowid"
        ).fetchall()
        assert [int(row["attempt_id"]) for row in excerpts] == [
            first_attempt,
            second_attempt,
        ]
        facts = reloaded.execute(
            "select id, confirmed, rejected, confidence from profile_facts "
            "where id in (?, ?) order by id",
            (old_fact, new_fact),
        ).fetchall()
        assert [(row["confirmed"], row["rejected"], row["confidence"]) for row in facts] == [
            (0, 0, "low"),
            (0, 0, "low"),
        ]
        assert not {old_fact, new_fact} & {
            int(row["id"]) for row in profiles.select_active_facts(reloaded, class_id)
        }
    finally:
        reloaded.close()


def test_retrying_identical_research_proposals_does_not_reown_deduplicated_records(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_settings.update_settings_row(
        db,
        {"allow_web_research": 1, "source_content_enabled": 1},
    )
    snapshot = "The identical evidence sentence."

    def fake_fetch(url: str, **kwargs: object) -> dict[str, object]:
        return {
            "url": url,
            "final_url": url,
            "title": "Stable source",
            "accessed_at": "2026-08-22T12:00:00+00:00",
            "content_type": "text/plain",
            "snapshot": snapshot,
            "truncated": False,
            "warning": None,
        }

    monkeypatch.setattr(web_research, "fetch_source", fake_fetch)
    run = 0
    produced: list[tuple[int, int, int]] = []

    async def identical_proposal_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        nonlocal run
        run += 1
        registry = kwargs["registry"]
        fetched = registry["fetch_source"].handler(  # type: ignore[index]
            url="https://example.com/stable"
        )
        source = registry["propose_source_snapshot"].handler(  # type: ignore[index]
            fetch_id=str(fetched.value["fetch_id"])
        )
        source_id = int(source.value["source"]["id"])  # type: ignore[index]
        excerpt = registry["propose_source_excerpt"].handler(  # type: ignore[index]
            source_id=source_id,
            excerpt=snapshot,
        )
        excerpt_id = int(excerpt.value["excerpt"]["id"])  # type: ignore[index]
        fact = registry["propose_profile_fact"].handler(  # type: ignore[index]
            kind="note",
            label="Stable method",
            value="Use the stable method",
            source_id=source_id,
            excerpt_id=excerpt_id,
        )
        fact_id = int(fact.value["fact_id"])
        produced.append((source_id, excerpt_id, fact_id))
        if run == 1:
            return tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")
        return tools.ToolLoopResult(content="Recovered without adopting old records.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", identical_proposal_loop)
    assert (
        _send(client, class_id, session_id, "Repeat research", profile="research").status_code
        == 504
    )
    retried = _retry(client, class_id, session_id)
    assert retried.status_code == 200
    assert produced[0] == produced[1]  # all three storage helpers deduplicated the retry
    assert retried.json()["source_ids"] == []
    assert retried.json()["profile_fact_ids"] == []
    assert {
        item["target_kind"]
        for item in retried.json()["activity"]
        if item["tool"].startswith("propose_")
    } == {
        "source_revision_reference",
        "source_excerpt_reference",
        "profile_fact_reference",
    }

    attempts = db.execute("select id, state from agent_turn_attempts order by id").fetchall()
    first_attempt, second_attempt = (int(row["id"]) for row in attempts)
    source_id, excerpt_id, fact_id = produced[0]
    assert _target_attempt(db, target_kind="source", target_id=source_id) == first_attempt
    assert _target_attempt(db, target_kind="source_excerpt", target_id=excerpt_id) == first_attempt
    assert _target_attempt(db, target_kind="profile_fact", target_id=fact_id) == first_attempt
    # The identical source revision is likewise still owned by the first attempt, and the
    # retry has no ownership rows to make the old records look newly produced.
    revision_id = int(
        db.execute(
            "select current_revision_id from writer_sources where id = ?", (source_id,)
        ).fetchone()["current_revision_id"]
    )
    assert (
        _target_attempt(db, target_kind="source_revision", target_id=revision_id) == first_attempt
    )
    assert (
        db.execute(
            "select count(*) from agent_attempt_targets where attempt_id = ?",
            (second_attempt,),
        ).fetchone()[0]
        == 0
    )
    fact = db.execute(
        "select confirmed, rejected, confidence from profile_facts where id = ?", (fact_id,)
    ).fetchone()
    assert (fact["confirmed"], fact["rejected"], fact["confidence"]) == (0, 0, "low")


def test_deleted_targets_get_new_autoincrement_ids_so_attempt_ownership_cannot_shift(
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
) -> None:
    attempt_id = _create_attempt(db, session_id=session_id, content="Track source ownership")
    source = source_ledger.upsert_source(
        db,
        class_id=class_id,
        source_type=source_ledger.WEB,
        url="https://example.com/original",
        title="Original source",
        snapshot="Original evidence",
        final_url="https://example.com/original",
        content_type="text/plain",
        attempt_id=attempt_id,
    )
    deleted_source_id = int(source["id"])
    db.execute("delete from writer_sources where id = ?", (deleted_source_id,))
    db.commit()

    replacement_source = source_ledger.upsert_source(
        db,
        class_id=class_id,
        source_type=source_ledger.WEB,
        url="https://example.com/replacement",
        title="Replacement source",
        snapshot="Replacement evidence",
        final_url="https://example.com/replacement",
        content_type="text/plain",
    )
    assert int(replacement_source["id"]) > deleted_source_id
    assert (
        agent_attempts.target_owner(
            db,
            target_kind="source",
            target_id=int(replacement_source["id"]),
        )
        is None
    )

    fact_attempt_id = _create_attempt(db, session_id=session_id, content="Track fact ownership")
    fact_source = source_ledger.upsert_source(
        db,
        class_id=class_id,
        source_type=source_ledger.WEB,
        url="https://example.com/fact-source",
        title="Fact source",
        snapshot="Fact evidence",
        final_url="https://example.com/fact-source",
        content_type="text/plain",
    )
    excerpt = source_ledger.add_excerpt(db, int(fact_source["id"]), "Fact evidence")
    fact = profiles.propose_ledger_fact(
        db,
        class_id=class_id,
        kind="note",
        label="Method",
        value="Use the durable path",
        source_id=int(fact_source["id"]),
        excerpt_id=int(excerpt["id"]),
        attempt_id=fact_attempt_id,
    )
    deleted_fact_id = int(fact["id"])
    db.execute("delete from profile_facts where id = ?", (deleted_fact_id,))
    db.commit()

    replacement_fact_id = int(
        db.execute(
            "insert into profile_facts (class_id, kind, label, value, confidence) "
            "values (?, 'note', 'Replacement', 'Fresh fact', 'low')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()
    assert replacement_fact_id > deleted_fact_id
    assert (
        agent_attempts.target_owner(
            db,
            target_kind="profile_fact",
            target_id=replacement_fact_id,
        )
        is None
    )


def test_deleting_a_session_cascades_attempts_and_ownership_rows_without_touching_targets(
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
) -> None:
    attempt_id = _create_attempt(db, session_id=session_id, content="Delete the session")
    source = source_ledger.upsert_source(
        db,
        class_id=class_id,
        source_type=source_ledger.WEB,
        url="https://example.com/session-target",
        title="Session target",
        snapshot="Session evidence",
        final_url="https://example.com/session-target",
        content_type="text/plain",
        attempt_id=attempt_id,
    )
    excerpt = source_ledger.add_excerpt(
        db,
        int(source["id"]),
        "Session evidence",
        attempt_id=attempt_id,
    )
    fact = profiles.propose_ledger_fact(
        db,
        class_id=class_id,
        kind="note",
        label="Session fact",
        value="Will outlive the session",
        source_id=int(source["id"]),
        excerpt_id=int(excerpt["id"]),
        attempt_id=attempt_id,
    )

    db.execute("delete from chat_sessions where id = ?", (session_id,))
    db.commit()

    assert db.execute("select count(*) from agent_turn_attempts").fetchone()[0] == 0
    assert db.execute("select count(*) from agent_attempt_targets").fetchone()[0] == 0
    assert db.execute("select count(*) from messages").fetchone()[0] == 0
    assert (
        db.execute(
            "select count(*) from writer_sources where id = ?",
            (int(source["id"]),),
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "select count(*) from writer_source_excerpts where id = ?",
            (int(excerpt["id"]),),
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "select count(*) from profile_facts where id = ?",
            (int(fact["id"]),),
        ).fetchone()[0]
        == 1
    )


def test_deleting_a_class_cascades_attempts_targets_and_agent_owned_rows_without_garbage(
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    _enable_workspace(db, class_id, workspace_root, changes=True, commands=True)
    workspace_id = int(agent_store.get_workspace_for_class(db, class_id)["id"])
    attempt_id = _create_attempt(db, session_id=session_id, content="Delete the class")

    change = agent_store.create_workspace_change(
        db,
        class_id,
        workspace_id=workspace_id,
        session_id=session_id,
        relative_path="notes.txt",
        base_hash="base",
        base_content="before",
        proposed_content="after",
        file_device=1,
        file_inode=1,
        file_mode=0o100644,
        newline="\n",
        rationale="track change",
        attempt_id=attempt_id,
    )
    command = agent_store.create_command_request(
        db,
        class_id,
        workspace_id=workspace_id,
        session_id=session_id,
        argv=["python3", "--version"],
        relative_cwd=".",
        reason="track command",
        expected_signal="prints version",
        attempt_id=attempt_id,
    )

    db.execute("delete from classes where id = ?", (class_id,))
    db.commit()

    assert db.execute("select count(*) from agent_turn_attempts").fetchone()[0] == 0
    assert db.execute("select count(*) from agent_attempt_targets").fetchone()[0] == 0
    assert db.execute("select count(*) from chat_sessions").fetchone()[0] == 0
    assert db.execute("select count(*) from class_workspaces").fetchone()[0] == 0
    assert db.execute("select count(*) from workspace_changes").fetchone()[0] == 0
    assert db.execute("select count(*) from command_requests").fetchone()[0] == 0
    assert (
        db.execute(
            "select count(*) from workspace_changes where id = ?",
            (int(change["id"]),),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "select count(*) from command_requests where id = ?",
            (int(command["id"]),),
        ).fetchone()[0]
        == 0
    )


def test_a_retry_after_reload_still_works_from_the_durable_attempt(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload only re-reads state; the failed attempt is durable, so Retry still runs."""
    _stub_loop(
        monkeypatch, tools.ToolLoopResult(content="", stopped=tools.UPSTREAM_FAILED, detail="x")
    )
    assert _send(client, class_id, session_id, "Durable question").status_code == 502

    # Simulate a reload: a fresh read of the transcript still shows the failed state.
    reloaded = client.get(f"/api/sessions/{session_id}/messages").json()
    assert reloaded[0]["agent_attempt"]["state"] == "failed"

    _stub_loop(monkeypatch, tools.ToolLoopResult(content="Answered after reload."))
    assert _retry(client, class_id, session_id).status_code == 200
    assert sessions.list_messages(db, session_id)[-1]["content"] == "Answered after reload."


def test_reconcile_running_settles_an_interrupted_attempt_as_stopped(
    db: sqlite3.Connection, session_id: int
) -> None:
    """A crash mid-turn leaves a running attempt; startup reconciliation settles it as a
    truthful, retryable stopped state rather than leaving it in flight forever."""
    user_message_id = sessions.add_message(db, session_id, "user", "Interrupted")
    attempt_id = agent_attempts.create_attempt(
        db, session_id=session_id, user_message_id=user_message_id, profile="code"
    )
    assert agent_attempts.reconcile_running(db) == 1
    latest = agent_attempts.latest_attempt_for_message(db, user_message_id)
    assert latest is not None and latest["state"] == agent_attempts.STOPPED
    assert latest["id"] == attempt_id


# --- The retry serializes against a second retry and a new turn (true concurrency) --


async def _run_two_overlapping(
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
    first,
    second,
):
    """Start `first` (blocked in the loop, holding the claim), then run `second` against it.

    Returns (first_result_or_exc, second_result_or_exc).
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        entered.set()
        await release.wait()
        return tools.ToolLoopResult(content="First, at last.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", gated_loop)
    conn_a, conn_b = connect(), connect()
    try:
        task = asyncio.create_task(first(conn_a))
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        try:
            second_result: object = await second(conn_b)
        except BaseException as exc:  # noqa: BLE001 - the refusal is the result under test
            second_result = exc
        release.set()
        first_result = await asyncio.wait_for(task, timeout=5.0)
        return first_result, second_result
    finally:
        release.set()
        conn_a.close()
        conn_b.close()


async def test_two_retries_cannot_overlap(
    db: sqlite3.Connection, class_id: int, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a failed turn to retry.
    user_message_id = sessions.add_message(db, session_id, "user", "Retry target")
    agent_attempts.fail_attempt(
        db,
        agent_attempts.create_attempt(
            db, session_id=session_id, user_message_id=user_message_id, profile="code"
        ),
        stopped_reason=tools.TIMEOUT,
        detail="slow",
    )

    def retry(conn: sqlite3.Connection):
        return routes_agent_chat.retry_agent_chat(conn, class_id, session_id)

    first, second = await _run_two_overlapping(class_id, session_id, monkeypatch, retry, retry)
    # One retry ran; the other hit the shared claim and was refused deterministically.
    assert isinstance(second, ConflictError)
    assert not isinstance(first, BaseException)
    assert sessions.active_turn(session_id) is None


async def test_a_retry_racing_a_new_turn_obeys_the_shared_claim(
    db: sqlite3.Connection, class_id: int, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_message_id = sessions.add_message(db, session_id, "user", "Retry target")
    agent_attempts.fail_attempt(
        db,
        agent_attempts.create_attempt(
            db, session_id=session_id, user_message_id=user_message_id, profile="code"
        ),
        stopped_reason=tools.TIMEOUT,
        detail="slow",
    )

    def retry(conn: sqlite3.Connection):
        return routes_agent_chat.retry_agent_chat(conn, class_id, session_id)

    def new_turn(conn: sqlite3.Connection):
        return routes_agent_chat.send_agent_chat(
            class_id,
            session_id,
            routes_agent_chat.AgentChatRequest(content="A brand new turn", profile="code"),
            conn,
        )

    _first, second = await _run_two_overlapping(class_id, session_id, monkeypatch, retry, new_turn)
    assert isinstance(second, ConflictError)
    assert sessions.active_turn(session_id) is None


# ---------------------------------------------------------------------------
# PLA-401 final pass: planning off the event loop, and Stop during real dispatch.
# ---------------------------------------------------------------------------


def test_planning_off_the_event_loop_does_not_freeze_the_app(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: a turn's blocking planning (embedding, retrieval, rerank) runs in a worker
    thread, not on the FastAPI event loop. While one turn sits inside its planning
    barrier, unrelated API work on the same app still completes, a second turn on the
    same session is still refused with the ordinary 409 (serialization by the claim is
    unchanged), and the held turn completes normally once the barrier opens."""
    entered = threading.Event()
    release = threading.Event()

    def barrier_retrieve(
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        entered.set()
        release.wait(timeout=10.0)  # bounded backstop: the suite can never hang forever
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", barrier_retrieve)

    async def fast_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return tools.ToolLoopResult(content="Unblocked.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fast_loop)

    results: dict[str, object] = {}

    def run_turn() -> None:
        results["turn"] = client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "A question that plans", "profile": "agent"},
        )

    turn_thread = threading.Thread(target=run_turn)
    turn_thread.start()
    assert entered.wait(timeout=5.0), "the planning worker never reached the barrier"

    # The planner is blocked off-loop right now. Unrelated work on the same app still
    # completes: this Stop takes no claim and touches no turn of ours.
    other_session = int(sessions.create_session(db, class_id)["id"])
    unrelated = client.post(f"/api/classes/{class_id}/sessions/{other_session}/agent-chat/stop")
    assert unrelated.status_code == 200
    assert unrelated.json() == {"stopped": False, "settling": False}

    # And serialization is preserved: a second turn on the held session is refused with
    # the ordinary conversation-busy 409, without waiting on the planner.
    busy = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A second question", "profile": "agent"},
    )
    assert busy.status_code == 409

    # Open the barrier: the turn's model work runs and it settles like any healthy turn.
    release.set()
    turn_thread.join(timeout=10.0)
    assert not turn_thread.is_alive()
    assert results["turn"].status_code == 200
    assert results["turn"].json()["content"] == "Unblocked."
    assert sessions.active_turn(session_id) is None


def test_stop_during_real_tool_dispatch_leaves_no_later_effect(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8: a turn stopped while a tool is in flight in its worker thread settles only
    once the worker has left (the Stop waits for quiescence), and from the moment the Stop
    latched, the in-flight tool could create no new durable consequence: every durable
    tool re-checks the turn's stop flag before its write, the cancelled loop makes no
    further model call, the attempt settles as stopped, and the session claim frees."""
    app_settings.update_settings_row(db, {"allow_web_research": 1})

    # Script the model: ask for one search, then answer. The second reply never happens -
    # the turn is stopped in the first dispatch - so the iterator going dry would be a
    # loud sign of exactly the bug this test forbids (a model call after the stop).
    scripted = iter(
        [
            llm_client.AssistantMessage(
                content="",
                tool_calls=(
                    llm_client.ToolCall(
                        id="call-1",
                        name="search_web",
                        arguments='{"query": "definition of convolution"}',
                    ),
                ),
                truncated=False,
            ),
            llm_client.AssistantMessage(content="The answer.", tool_calls=(), truncated=False),
        ]
    )

    async def scripted_with_tools(
        endpoint: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        **kwargs: object,
    ) -> llm_client.AssistantMessage:
        return next(scripted)

    monkeypatch.setattr(tools, "complete_with_tools", scripted_with_tools)

    dispatched = threading.Event()
    release = threading.Event()
    network_calls: list[str] = []

    def blocking_search(query: str, **kwargs: object) -> list[dict[str, str]]:
        # The real dispatch seam, blocked mid-call: this worker cannot be cancelled, only
        # watched. The stop flag it meets on the way out is what makes the guarantee hold.
        network_calls.append(query)
        dispatched.set()
        release.wait(timeout=10.0)
        return [{"title": "A result", "url": "https://example.com", "content": "Body text."}]

    monkeypatch.setattr(web_research, "search_web", blocking_search)

    results: dict[str, object] = {}

    def run_turn() -> None:
        results["turn"] = client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "Research the definition of convolution", "profile": "agent"},
        )

    turn_thread = threading.Thread(target=run_turn)
    turn_thread.start()
    assert dispatched.wait(timeout=5.0), "the real dispatch never started"

    # A real Stop on the running turn. It completes only once the in-flight worker has
    # left, so it runs in its own thread; the release below follows the latched gate, so
    # the stop flag is set before the in-flight read can finish and do anything.
    stop_results: dict[str, object] = {}

    def run_stop() -> None:
        stop_results["stop"] = client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/stop"
        )

    stop_thread = threading.Thread(target=run_stop)
    stop_thread.start()
    deadline = time.monotonic() + 5.0
    entry = None
    while time.monotonic() < deadline:
        entry = routes_agent_chat._inflight.get(session_id)
        if entry is not None and entry[2].stopped:
            break
        time.sleep(0.01)
    assert entry is not None and entry[2].stopped, "the Stop never latched the turn's gate"
    release.set()

    stop_thread.join(timeout=10.0)
    turn_thread.join(timeout=10.0)
    assert not stop_thread.is_alive() and not turn_thread.is_alive()

    # The Stop reported a real stop, and the turn settled with the same bounded body.
    assert stop_results["stop"].status_code == 200
    assert stop_results["stop"].json() == {"stopped": True, "settling": False}
    turn = results["turn"]
    assert turn.status_code == 200
    assert json.loads(turn.content)["stopped"] == "stopped"

    # Exactly one network search - the one that was in flight - and nothing after the
    # Stop completed: the cancelled loop made no further model call.
    assert len(network_calls) == 1
    # No durable consequence from the turn: the search result was dropped at the stop
    # boundary, so nothing was proposed, persisted, or requested.
    for table in ("writer_sources", "workspace_changes", "command_requests"):
        assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0  # noqa: S608
    # The attempt settled as stopped - not failed, not forever running.
    row = db.execute("select state from agent_turn_attempts order by id desc limit 1").fetchone()
    assert row["state"] == "stopped"
    # And the session is free: the claim released only after the worker left.
    assert sessions.active_turn(session_id) is None


def test_a_non_quiesced_ending_holds_the_claim_until_the_worker_leaves(
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn's ending where `wait_quiesced` reports false does not claim the turn is
    settled: the session stays held (nothing may start under an in-flight worker, and the
    connection it reads through must not close out from under it) until the worker has
    actually left - while a quiesced ending releases in place. The bounded watcher's
    last-resort release is the only other exit, and it is a loud one."""
    monkeypatch.setattr(routes_agent_chat, "QUIESCENCE_SECONDS", 0.1)
    token = sessions.begin_turn(session_id)
    gate = tools.ToolStopGate()
    worker_left = gate.begin_work()

    def wait_for_release() -> None:
        deadline = time.monotonic() + 5.0
        while sessions.active_turn(session_id) is not None and time.monotonic() < deadline:
            time.sleep(0.01)

    async def ending() -> None:
        await routes_agent_chat._release_turn(session_id, token, gate)
        # The non-quiesced ending kept the claim held.
        assert sessions.active_turn(session_id) is not None
        # The worker leaves: the bounded watcher releases the moment it sees quiescence.
        gate.finish_work(worker_left)
        await asyncio.wait_for(asyncio.to_thread(wait_for_release), timeout=6.0)

    asyncio.run(ending())
    # ...released, and only once the worker left.
    assert sessions.active_turn(session_id) is None

    # The quiesced ending (no in-flight worker) releases in place.
    token2 = sessions.begin_turn(session_id)
    quiet_gate = tools.ToolStopGate()

    async def quiet_ending() -> None:
        await routes_agent_chat._release_turn(session_id, token2, quiet_gate)
        assert sessions.active_turn(session_id) is None

    asyncio.run(quiet_ending())
    assert sessions.active_turn(session_id) is None


def test_stop_reports_settling_when_a_worker_outlives_the_quiescence_bound(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quiescence-timeout branch, end to end: a worker still inside its dispatch when
    the Stop's bounded wait expires makes /stop report the truth - the stop was latched
    and the cancellation delivered, but quiescence is NOT claimed. The attempt still
    settles as stopped, the session frees as soon as the worker actually leaves, and the
    conversation is not wedged afterwards.

    Both quiescence bounds shrink to keep the branch inside the test's clock: the route's
    own waits AND the loop's shielded wait read their module's constant at call time, so
    the turn's bounded settlement stays bounded while the barrier holds the worker."""
    monkeypatch.setattr(routes_agent_chat, "QUIESCENCE_SECONDS", 0.1)
    monkeypatch.setattr(tools, "QUIESCENCE_SECONDS", 0.1)
    app_settings.update_settings_row(db, {"allow_web_research": 1})

    scripted = iter(
        [
            llm_client.AssistantMessage(
                content="",
                tool_calls=(
                    llm_client.ToolCall(
                        id="call-1",
                        name="search_web",
                        arguments='{"query": "definition of convolution"}',
                    ),
                ),
                truncated=False,
            ),
            llm_client.AssistantMessage(content="The answer.", tool_calls=(), truncated=False),
        ]
    )

    async def scripted_with_tools(
        endpoint: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        **kwargs: object,
    ) -> llm_client.AssistantMessage:
        return next(scripted)

    monkeypatch.setattr(tools, "complete_with_tools", scripted_with_tools)

    dispatched = threading.Event()
    release = threading.Event()
    network_calls: list[str] = []

    def blocking_search(query: str, **kwargs: object) -> list[dict[str, str]]:
        network_calls.append(query)
        dispatched.set()
        release.wait(timeout=10.0)
        return [{"title": "A result", "url": "https://example.com", "content": "Body text."}]

    monkeypatch.setattr(web_research, "search_web", blocking_search)

    results: dict[str, object] = {}

    def run_turn() -> None:
        results["turn"] = client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "Research the definition of convolution", "profile": "agent"},
        )

    turn_thread = threading.Thread(target=run_turn)
    turn_thread.start()
    assert dispatched.wait(timeout=5.0), "the real dispatch never started"

    stop_results: dict[str, object] = {}

    def run_stop() -> None:
        stop_results["stop"] = client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/stop"
        )

    stop_thread = threading.Thread(target=run_stop)
    stop_thread.start()
    deadline = time.monotonic() + 5.0
    entry = None
    while time.monotonic() < deadline:
        entry = routes_agent_chat._inflight.get(session_id)
        if entry is not None and entry[2].stopped:
            break
        time.sleep(0.01)
    assert entry is not None and entry[2].stopped, "the Stop never latched the turn's gate"

    # The worker is STILL inside its dispatch when the Stop's bounded wait expires:
    # the response must not claim a stop that has not provably happened.
    stop_thread.join(timeout=10.0)
    assert not stop_thread.is_alive()
    assert stop_results["stop"].status_code == 200
    assert stop_results["stop"].json() == {"stopped": False, "settling": True}

    # The attempt settled as stopped either way - the turn is over for the student.
    row = db.execute("select state from agent_turn_attempts order by id desc limit 1").fetchone()
    assert row["state"] == "stopped"

    # The worker leaves: the session frees as soon as the worker actually left.
    release.set()
    turn_thread.join(timeout=10.0)
    assert not turn_thread.is_alive()
    assert json.loads(results["turn"].content)["stopped"] == "stopped"
    deadline = time.monotonic() + 5.0
    while sessions.active_turn(session_id) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sessions.active_turn(session_id) is None
    # Exactly one network search - the one that was in flight - and nothing after.
    assert len(network_calls) == 1

    # And the session is not wedged: the next turn runs and settles.
    async def fast_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return tools.ToolLoopResult(content="The next answer.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fast_loop)
    next_turn = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A question after the stop", "profile": "agent"},
    )
    assert next_turn.status_code == 200
    assert next_turn.json()["content"] == "The next answer."
