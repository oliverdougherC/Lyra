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
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat, routes_chat
from backend.core import agent_attempts, sessions
from backend.core.errors import ConflictError, LyraError
from backend.llm import tools
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


def _send(client: TestClient, class_id: int, session_id: int, content: str = "A question"):
    return client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": content, "profile": "code"},
    )


def _retry(client: TestClient, class_id: int, session_id: int):
    return client.post(f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry")


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
    db.execute("update settings set context_window = 512")
    db.commit()
    assert _send(client, class_id, session_id).status_code == 400
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
    """A client disconnect cancels the handler coroutine at the loop await; the finally must
    release the claim so the session is usable again."""
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
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        conn.close()
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
    # turn, and the attempt did not settle as completed.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["state"] != agent_attempts.COMPLETED
    assert attempt["assistant_message_id"] is None
    assert sessions.active_turn(session_id) is None


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
        return routes_agent_chat.retry_agent_chat(class_id, session_id, conn)

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
        return routes_agent_chat.retry_agent_chat(class_id, session_id, conn)

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
