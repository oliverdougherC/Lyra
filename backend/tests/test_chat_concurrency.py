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
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_chat
from backend.core import app_settings, sessions
from backend.core.errors import LyraError, UpstreamError
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

    routes_chat._commit_reply(db, session_id, plan, ["Replacement reply"], [], NOTHING_RETRIEVED)

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
