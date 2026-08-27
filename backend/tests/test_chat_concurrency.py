"""Per-session turn serialization and concurrency-safe regeneration.

Tutor chat correctness must not depend on the frontend never overlapping requests. These
tests pin the server-side protocol: at most one mutating/generating turn per session,
enforced by a claim taken before anything is persisted; an overlapping send or
regeneration receives a deterministic 409 with nothing stored and nothing on the wire;
the claim is released however a turn ends; and regeneration deletes only the message ids
its plan named when it opened, so a newer independent turn can never be collateral
damage.

Concurrency is made deterministic rather than raced: an in-flight turn is represented by
holding its claim (exactly what a streaming turn holds), and the collateral-damage test
drives the commit step directly against a hand-built interleaving that the claim would
normally forbid - proving the destructive step is safe by construction, not only by
serialization.
"""

import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_chat
from backend.core import app_settings, sessions
from backend.core.errors import ConflictError, LyraError, UpstreamError
from backend.llm.client import StreamDelta
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect, get_db

StreamFactory = Callable[..., AsyncIterator[StreamDelta]]

QUESTION = "Explain the chain rule"
ENDPOINT = "http://127.0.0.1:8081/v1"

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


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "get_api_key", lambda: None)


@pytest.fixture(autouse=True)
def configured_endpoint(db: sqlite3.Connection) -> None:
    db.execute("update settings set endpoint_url = ?, model = 'qwen'", (ENDPOINT,))
    db.commit()


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_chat, "retrieve", lambda *a, **k: NOTHING_RETRIEVED)


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

    app.include_router(routes_chat.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def _send(client: TestClient, session_id: int) -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": QUESTION, "mode": "guide", "document_id": None},
    )


def _regenerate(client: TestClient, session_id: int) -> httpx.Response:
    return client.post(f"/api/sessions/{session_id}/regenerate", json={"mode": "guide"})


def _seed_turn(client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """One completed question/answer exchange, so regeneration has something to retry."""
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("First answer."))
    response = _send(client, session_id)
    assert response.status_code == 200
    # Consuming the body ran the stream to completion, so the claim is released.
    assert sessions.active_turn(session_id) is None


# --- Overlap: a second mutating turn is a deterministic conflict ---------------------
#
# An in-flight turn is represented by holding its claim, which is precisely the state a
# streaming turn is in between opening and its final frame. No sleeps, no thread races:
# the overlap is a fact, not a probability.


def test_a_send_overlapping_an_active_turn_is_refused_and_persists_nothing(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Never sent."))
    token = sessions.begin_turn(session_id)

    response = _send(client, session_id)

    assert response.status_code == 409
    assert "answering" in response.json()["detail"]
    # Refused before persistence: no orphaned question, no claimed title.
    assert sessions.list_messages(db, session_id) == []
    assert sessions.get_session(db, session_id)["title"] is None
    # The refusal did not disturb the turn that holds the session.
    active = sessions.active_turn(session_id)
    assert active is not None and active.token == token


def test_a_regeneration_overlapping_an_active_turn_is_refused(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turn(client, session_id, monkeypatch)
    before = sessions.list_messages(db, session_id)
    sessions.begin_turn(session_id)

    response = _regenerate(client, session_id)

    assert response.status_code == 409
    # The conversation is exactly as it was: the existing answer is not superseded.
    assert sessions.list_messages(db, session_id) == before


def test_a_send_overlapping_a_regeneration_is_refused(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim is one slot, not one per kind: a regeneration in flight refuses a send
    exactly as a send refuses a send."""
    _seed_turn(client, session_id, monkeypatch)
    before = sessions.list_messages(db, session_id)
    sessions.begin_turn(session_id)

    response = _send(client, session_id)

    assert response.status_code == 409
    assert sessions.list_messages(db, session_id) == before


def test_two_regenerations_cannot_overlap(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turn(client, session_id, monkeypatch)
    sessions.begin_turn(session_id)

    assert _regenerate(client, session_id).status_code == 409


def test_claims_are_per_session_not_global(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serializing one conversation must not stall the others."""
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Parallel answer."))
    busy = int(sessions.create_session(db, class_id)["id"])
    free = int(sessions.create_session(db, class_id)["id"])
    sessions.begin_turn(busy)

    response = _send(client, free)

    assert response.status_code == 200
    assert [m["role"] for m in sessions.list_messages(db, free)] == ["user", "assistant"]


# --- Release: every ending frees the session -----------------------------------------


def test_a_completed_turn_releases_the_session_for_the_next_one(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turn(client, session_id, monkeypatch)

    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Second answer."))
    assert _send(client, session_id).status_code == 200
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_an_upstream_failure_releases_the_session(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail())
    first = _send(client, session_id)
    assert any(frame.startswith("data: ") for frame in first.text.split("\n\n") if frame)
    assert sessions.active_turn(session_id) is None

    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Recovered."))
    assert _send(client, session_id).status_code == 200


def test_a_refused_open_releases_the_session(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that never starts streaming - here, refused by the consent gate - must not
    leave the session claimed."""
    db.execute("update settings set endpoint_url = 'http://203.0.113.9:1/v1', remote_ack = 0")
    db.commit()
    assert _send(client, session_id).status_code == 400
    assert sessions.active_turn(session_id) is None

    db.execute("update settings set endpoint_url = ?, remote_ack = 0", (ENDPOINT,))
    db.commit()
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Back to local."))
    assert _send(client, session_id).status_code == 200


async def test_a_disconnect_releases_the_session_while_keeping_the_partial_reply(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the generator is what Starlette does when the reader goes away. The
    partial reply stays (existing behavior) and the claim goes with the turn."""
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Half ", "an ", "answer."))
    request = routes_chat.TurnInput(content=QUESTION, mode="guide")
    config = app_settings.TutorConfig(
        endpoint_url=ENDPOINT, api_key=None, model=None, context_window=8192
    )
    user_message_id = sessions.add_message(db, session_id, "user", QUESTION)
    plan = routes_chat.TurnPlan(user_message_id=user_message_id)
    cost = routes_chat._plan_turn_cost(db, session_id, "guide", QUESTION, plan.excluded, config)
    turn_token = sessions.begin_turn(session_id)
    stream = routes_chat._stream_turn(session_id, request, config, plan, cost, turn_token)
    for _ in range(5):  # start, two statuses, composing, first token
        await anext(stream)

    await stream.aclose()

    stored = sessions.list_messages(db, session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[1]["content"] == "Half "
    # The disconnect ended the turn, so the session is free for the next one.
    assert sessions.active_turn(session_id) is None


def test_claims_of_a_dead_process_do_not_survive_into_the_next_start(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim registry lives in process memory, so a crash mid-turn cannot leave a
    stale marker for a later start to trip over: the next process begins with no claims,
    by construction rather than by reconciliation. Clearing the registry is exactly what
    process death does to it."""
    sessions.begin_turn(session_id)
    sessions._active_turns.clear()

    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Fresh start."))
    assert _send(client, session_id).status_code == 200


# --- Regeneration owns only what it observed -----------------------------------------


def test_regeneration_still_replaces_the_reply_it_retried(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_turn(client, session_id, monkeypatch)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Second attempt."))

    response = _regenerate(client, session_id)

    assert response.status_code == 200
    assert [(m["role"], m["content"]) for m in sessions.list_messages(db, session_id)] == [
        ("user", QUESTION),
        ("assistant", "Second attempt."),
    ]


def test_regeneration_can_never_delete_a_turn_it_did_not_observe(
    db: sqlite3.Connection, session_id: int
) -> None:
    """The collateral-damage regression, driven at the commit step itself.

    The plan is built from what the regeneration observed when it opened; a newer
    question and reply then land as a racing turn would have left them. On the pre-fix
    implementation the replacement deleted "everything after the question" and took the
    newer turn with it; deleting by the observed ids leaves it untouched even in this
    hand-built interleaving the claim would normally forbid.
    """
    question_id = sessions.add_message(db, session_id, "user", "First question")
    stale_reply_id = sessions.add_message(db, session_id, "assistant", "Stale reply")
    plan = routes_chat.TurnPlan(user_message_id=question_id, superseded=(stale_reply_id,))
    newer_question_id = sessions.add_message(db, session_id, "user", "Newer question")
    newer_reply_id = sessions.add_message(db, session_id, "assistant", "Newer reply")

    routes_chat._commit_reply_atomic(
        db, session_id, plan, ["Replacement reply"], [], NOTHING_RETRIEVED
    )

    remaining = {
        int(m["id"]): (m["role"], m["content"]) for m in sessions.list_messages(db, session_id)
    }
    # The stale reply is gone; the newer independent turn survives intact.
    assert stale_reply_id not in remaining
    assert remaining[newer_question_id] == ("user", "Newer question")
    assert remaining[newer_reply_id] == ("assistant", "Newer reply")
    assert ("assistant", "Replacement reply") in remaining.values()


def test_delete_messages_only_touches_the_named_conversation(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Belt and braces on the destructive primitive: ids are scoped to the session."""
    ours = int(sessions.create_session(db, class_id)["id"])
    theirs = int(sessions.create_session(db, class_id)["id"])
    their_message = sessions.add_message(db, theirs, "user", "Someone else's question")

    sessions.delete_messages(db, ours, (their_message,))

    assert [m["id"] for m in sessions.list_messages(db, theirs)] == [their_message]


def test_stale_release_cannot_free_a_claim_it_does_not_own(session_id: int) -> None:
    """A late `end_turn` from a finished or refused turn must not unlock the session out
    from under the turn that now holds it."""
    stale = sessions.begin_turn(session_id)
    sessions.end_turn(session_id, stale)
    current = sessions.begin_turn(session_id)

    sessions.end_turn(session_id, stale)  # a duplicate release from the earlier turn

    active = sessions.active_turn(session_id)
    assert active is not None and active.token == current
    sessions.end_turn(session_id, current)
    assert sessions.active_turn(session_id) is None


def test_the_claim_carries_the_question_it_answers(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-flight turn's identity is bound to the persisted question: observable while
    the stream is open, which is when another actor would ask who holds the session."""
    observed: list[sessions.TurnClaim | None] = []

    async def observing_stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        observed.append(sessions.active_turn(session_id))
        yield StreamDelta("answer", "Bound.")

    monkeypatch.setattr(routes_chat, "stream_chat", observing_stream)

    assert _send(client, session_id).status_code == 200

    stored = sessions.list_messages(db, session_id)
    assert observed and observed[0] is not None
    assert observed[0].user_message_id == int(stored[0]["id"])


# --- Release must structurally dominate every post-claim failure ---------------------


def test_a_connection_failure_after_the_claim_releases_the_session(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`connect()` is the first statement of the stream generator and used to run before
    the releasing `try`: a failure there claimed the session forever. It must now surface
    as an error frame and free the session for the next request."""
    real_connect = routes_chat.connect

    def failing_connect() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(routes_chat, "connect", failing_connect)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Never streamed."))

    response = _send(client, session_id)

    assert response.status_code == 200
    assert '"type":"error"' in response.text
    assert sessions.active_turn(session_id) is None

    # The session is genuinely usable, not just unmarked.
    monkeypatch.setattr(routes_chat, "connect", real_connect)
    assert _send(client, session_id).status_code == 200


async def test_a_body_that_never_starts_still_releases_the_session(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim crosses the StreamingResponse boundary: taken before the route returns,
    normally released inside the generator. But Python closes a never-started generator
    without running any of it, so a transport that dies before the first frame must be
    caught by the response's own finalizer, not by hoping the generator ran.
    """
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Unreached."))
    payload = routes_chat.ChatRequest(content=QUESTION, mode="guide", document_id=None)
    response = await routes_chat.send_chat(session_id, payload, db)
    # The route has returned: the claim is held and only the response can end the turn.
    active = sessions.active_turn(session_id)
    assert active is not None

    async def dead_send(message: dict[str, object]) -> None:
        raise RuntimeError("client vanished before the first frame")

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    with pytest.raises(BaseException, match="client vanished"):
        await response({"type": "http", "method": "POST", "headers": []}, receive, dead_send)

    assert sessions.active_turn(session_id) is None


async def test_a_failing_connection_close_cannot_suppress_the_release(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must be exception-safe itself: a `conn.close()` that raises after the last
    frame is bookkeeping noise, not a reason to leave the session claimed."""

    class CloseFails:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

        def close(self) -> None:
            self._real.close()
            raise sqlite3.OperationalError("close failed")

    monkeypatch.setattr(routes_chat, "connect", lambda: CloseFails(connect()))
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Full answer."))
    request = routes_chat.TurnInput(content=QUESTION, mode="guide")
    config = app_settings.TutorConfig(
        endpoint_url=ENDPOINT, api_key=None, model=None, context_window=8192
    )
    user_message_id = sessions.add_message(db, session_id, "user", QUESTION)
    plan = routes_chat.TurnPlan(user_message_id=user_message_id)
    cost = routes_chat._plan_turn_cost(db, session_id, "guide", QUESTION, plan.excluded, config)
    turn_token = sessions.begin_turn(session_id)
    stream = routes_chat._stream_turn(session_id, request, config, plan, cost, turn_token)

    with pytest.raises(sqlite3.OperationalError, match="close failed"):
        async for _ in stream:
            pass

    # The reply committed before the close, and the failing close did not hold the claim.
    stored = sessions.list_messages(db, session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[1]["content"] == "Full answer."
    assert sessions.active_turn(session_id) is None


async def _eventually(predicate: Callable[[], bool], deadline_s: float = 5.0) -> None:
    """Wait for a condition owned by a still-running worker thread to become true."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


class _Barrier:
    """Pause a worker thread at one chosen statement, visibly and releasably.

    `entered` is set the moment the worker reaches the instrumented call, so the test
    can cancel the route while the worker is *provably* alive inside `_open_turn` /
    `_open_regeneration` - not before it started, not after it finished. The worker then
    blocks until the test calls `release()`, making the interleaving a fact rather than
    a race.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self._release = threading.Event()

    def pause(self) -> None:
        self.entered.set()
        assert self._release.wait(timeout=5.0), "test never released the paused worker"

    def release(self) -> None:
        self._release.set()

    async def entered_wait(self) -> None:
        import asyncio

        assert await asyncio.to_thread(self.entered.wait, 5.0), "worker never reached the barrier"


async def test_cancellation_while_the_opener_is_still_validating_neither_leaks_nor_releases_early(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation delivered while the worker is alive but has not yet persisted
    anything. The old shared-list handoff could observe an empty list here and walk
    away, leaving whatever the worker went on to claim leaked forever. Now the claim is
    taken by the coroutine before the worker starts, so cancellation must (a) keep the
    session claimed while the worker runs - a second request may not overlap it - and
    (b) release it once the worker has definitely stopped."""
    import asyncio

    barrier = _Barrier()
    real_access = routes_chat.resolve_tutor_access

    def paused_access(conn: sqlite3.Connection) -> object:
        barrier.pause()
        return real_access(conn)

    monkeypatch.setattr(routes_chat, "resolve_tutor_access", paused_access)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Unreached."))
    payload = routes_chat.ChatRequest(content=QUESTION, mode="guide", document_id=None)
    task = asyncio.get_running_loop().create_task(routes_chat.send_chat(session_id, payload, db))
    await barrier.entered_wait()  # the worker is alive inside _open_turn

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker is still paused: the claim must still be held, and no second turn may
    # acquire the session and overlap the abandoned worker's reads and writes.
    assert sessions.active_turn(session_id) is not None
    with pytest.raises(ConflictError):
        sessions.begin_turn(session_id)
    with pytest.raises(ConflictError):
        await routes_chat.send_chat(session_id, payload, db)

    barrier.release()
    await _eventually(lambda: sessions.active_turn(session_id) is None)
    # The session is genuinely usable again, not just unmarked.
    token = sessions.begin_turn(session_id)
    sessions.end_turn(session_id, token)


async def test_cancellation_after_the_worker_persisted_waits_for_it_before_releasing(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation delivered after the worker persisted the question but before
    `_open_turn` returned. Releasing at the moment of cancellation would let a second
    request claim the session while this worker is still writing to it; the release must
    instead wait for the worker to stop. The persisted question staying behind matches
    what a disconnect during the stream already does: the turn's question outlives the
    reader that asked it."""
    import asyncio

    barrier = _Barrier()
    real_touch = routes_chat.touch_class

    def paused_touch(conn: sqlite3.Connection, class_id: int) -> None:
        barrier.pause()
        real_touch(conn, class_id)

    monkeypatch.setattr(routes_chat, "touch_class", paused_touch)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Unreached."))
    payload = routes_chat.ChatRequest(content=QUESTION, mode="guide", document_id=None)
    task = asyncio.get_running_loop().create_task(routes_chat.send_chat(session_id, payload, db))
    await barrier.entered_wait()  # the question is persisted; the worker has not returned

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # While the worker still runs, the session must stay claimed against overlap.
    active = sessions.active_turn(session_id)
    assert active is not None
    with pytest.raises(ConflictError):
        sessions.begin_turn(session_id)

    barrier.release()
    await _eventually(lambda: sessions.active_turn(session_id) is None)
    # The abandoned open left exactly the persisted question - nothing streamed, no
    # reply - and the session is free for the next turn.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    monkeypatch.setattr(routes_chat, "touch_class", real_touch)
    token = sessions.begin_turn(session_id)
    sessions.end_turn(session_id, token)


async def test_regeneration_cancellation_mid_validation_neither_leaks_nor_releases_early(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    sessions.add_message(db, session_id, "user", QUESTION)
    sessions.add_message(db, session_id, "assistant", "First answer.")
    barrier = _Barrier()
    real_access = routes_chat.resolve_tutor_access

    def paused_access(conn: sqlite3.Connection) -> object:
        barrier.pause()
        return real_access(conn)

    monkeypatch.setattr(routes_chat, "resolve_tutor_access", paused_access)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Unreached."))
    payload = routes_chat.RegenerateRequest(mode="guide")
    task = asyncio.get_running_loop().create_task(
        routes_chat.regenerate_chat(session_id, payload, db)
    )
    await barrier.entered_wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sessions.active_turn(session_id) is not None
    with pytest.raises(ConflictError):
        sessions.begin_turn(session_id)

    barrier.release()
    await _eventually(lambda: sessions.active_turn(session_id) is None)
    # A regeneration abandoned at open mutated nothing: the conversation is intact.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]
    token = sessions.begin_turn(session_id)
    sessions.end_turn(session_id, token)


async def test_regeneration_cancellation_late_in_the_opener_waits_for_the_worker(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    sessions.add_message(db, session_id, "user", QUESTION)
    sessions.add_message(db, session_id, "assistant", "First answer.")
    barrier = _Barrier()
    real_touch = routes_chat.touch_class

    def paused_touch(conn: sqlite3.Connection, class_id: int) -> None:
        barrier.pause()
        real_touch(conn, class_id)

    monkeypatch.setattr(routes_chat, "touch_class", paused_touch)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Unreached."))
    payload = routes_chat.RegenerateRequest(mode="guide")
    task = asyncio.get_running_loop().create_task(
        routes_chat.regenerate_chat(session_id, payload, db)
    )
    await barrier.entered_wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sessions.active_turn(session_id) is not None
    with pytest.raises(ConflictError):
        sessions.begin_turn(session_id)

    barrier.release()
    await _eventually(lambda: sessions.active_turn(session_id) is None)
    # The reply being retried was never deleted: replacement happens at commit, which
    # this turn never reached.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]
    token = sessions.begin_turn(session_id)
    sessions.end_turn(session_id, token)


async def test_a_cancellation_at_the_open_await_releases_the_claim(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowest window: `_open_turn` completes in its worker thread, and the
    cancellation lands exactly on the `await` carrying its result back. The tuple with
    the token is discarded, so the route must be able to release a claim it never
    received."""
    import asyncio

    real_to_thread = asyncio.to_thread

    async def cancelled_at_the_boundary(fn: object, *args: object, **kwargs: object) -> object:
        await real_to_thread(fn, *args, **kwargs)  # the claim is now held
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "to_thread", cancelled_at_the_boundary)
    payload = routes_chat.ChatRequest(content=QUESTION, mode="guide", document_id=None)

    with pytest.raises(asyncio.CancelledError):
        await routes_chat.send_chat(session_id, payload, db)

    assert sessions.active_turn(session_id) is None


async def test_a_cancellation_at_the_regeneration_open_await_releases_the_claim(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    sessions.add_message(db, session_id, "user", QUESTION)
    sessions.add_message(db, session_id, "assistant", "First answer.")
    real_to_thread = asyncio.to_thread

    async def cancelled_at_the_boundary(fn: object, *args: object, **kwargs: object) -> object:
        await real_to_thread(fn, *args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "to_thread", cancelled_at_the_boundary)
    payload = routes_chat.RegenerateRequest(mode="guide")

    with pytest.raises(asyncio.CancelledError):
        await routes_chat.regenerate_chat(session_id, payload, db)

    assert sessions.active_turn(session_id) is None
