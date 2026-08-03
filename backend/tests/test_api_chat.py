"""Contract tests for the chat endpoints and the SSE frame protocol.

Nothing here reaches a model. `stream_chat` is replaced with an async generator of fixed
tokens and `retrieve` with a fixed result, both patched where `routes_chat` looks them up,
so what is under test is the turn: what is persisted, in what order, and what goes on the
wire.
"""

import json
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
from backend.rag.retrieve import RetrievalResult, RetrievedChunk
from backend.storage.database import connect, get_db

StreamFactory = Callable[..., AsyncIterator[StreamDelta]]

QUESTION = "Explain the chain rule"
ENDPOINT = "http://127.0.0.1:8081/v1"

NOTHING_RETRIEVED = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

_CHUNK = RetrievedChunk(
    chunk_id=1,
    document_id=1,
    content="The chain rule differentiates a composition of functions.",
    token_count=14,
    page_number=3,
    section_title="Derivatives",
    problem_number=None,
    part_index=None,
    filename="lecture-2.pdf",
    similarity=0.82,
    score=0.86,
)
TRIMMED_RETRIEVAL = RetrievalResult(chunks=[_CHUNK], trimmed=True, omitted_document_count=2)


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _stored_messages() -> list[tuple[str, str]]:
    """Every persisted message, read on a fresh connection from wherever we are."""
    conn = connect()
    try:
        rows = conn.execute("select role, content from messages order by id")
        return [(str(row["role"]), str(row["content"])) for row in rows]
    finally:
        conn.close()


def _returns(result: RetrievalResult) -> Callable[..., RetrievalResult]:
    """A stand-in for `retrieve` that ignores its arguments."""

    def fake(*args: object, **kwargs: object) -> RetrievalResult:
        return result

    return fake


def _stream_of(*tokens: str, recorder: list[list[tuple[str, str]]] | None = None) -> StreamFactory:
    """A stand-in for `stream_chat` yielding fixed answer deltas.

    When a recorder is passed, the conversation as stored at the moment streaming starts
    is captured into it, which is how the ordering of the two writes is pinned down.
    """
    return _stream_deltas(*(StreamDelta("answer", token) for token in tokens), recorder=recorder)


def _stream_deltas(
    *deltas: StreamDelta, recorder: list[list[tuple[str, str]]] | None = None
) -> StreamFactory:
    """A stand-in for `stream_chat` yielding deltas across both channels."""

    async def stream(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        if recorder is not None:
            recorder.append(_stored_messages())
        for delta in deltas:
            yield delta

    return stream


def _stream_then_fail(*tokens: str) -> StreamFactory:
    """An upstream that dies partway, the way a local server does when it is killed."""

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


def _frames(body: str) -> list[dict[str, object]]:
    """Parse an SSE body, asserting the wire shape of every frame as it goes."""
    parsed: list[dict[str, object]] = []
    for block in body.split("\n\n"):
        if not block:
            continue
        assert block.startswith("data: ")
        assert "\n" not in block
        parsed.append(json.loads(block.removeprefix("data: ")))
    return parsed


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving the tutor config reads the key, never from the developer's keychain."""
    monkeypatch.setattr(app_settings, "get_api_key", lambda: None)


@pytest.fixture(autouse=True)
def configured_endpoint(db: sqlite3.Connection) -> None:
    """A tutor endpoint, without which a turn refuses to start."""
    db.execute("update settings set endpoint_url = ?, model = 'qwen'", (ENDPOINT,))
    db.commit()


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval has its own tests. Here it is a fixed, empty result unless overridden."""
    monkeypatch.setattr(routes_chat, "retrieve", _returns(NOTHING_RETRIEVED))


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the chat router.

    The override pins the app to the `db` fixture's database rather than to its
    connection object: sync handlers run in a threadpool, and a `sqlite3` connection may
    only be used from the thread that opened it.
    """
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


def _send(client: TestClient, session_id: int, mode: str = "guide") -> httpx.Response:
    return client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": QUESTION, "mode": mode, "document_id": None},
    )


def test_sessions_round_trip_through_their_class(client: TestClient, class_id: int) -> None:
    created = client.post(f"/api/classes/{class_id}/sessions", json={"title": "Week 9"})

    assert created.status_code == 201
    assert created.json()["mode"] == "guide"

    session_id = created.json()["id"]
    assert client.get(f"/api/classes/{class_id}/sessions").json() == [created.json()]
    assert client.get(f"/api/sessions/{session_id}/messages").json() == []
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}/messages").status_code == 404
    assert client.get("/api/classes/404/sessions").status_code == 404


def test_a_normal_turn_streams_start_then_tokens_then_done(
    client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The middle token carries a blank line, which is the SSE record separator. Lyra
    # streams markdown and LaTeX, so this is the common case, not an edge case.
    monkeypatch.setattr(
        routes_chat, "stream_chat", _stream_of("The chain rule:\n\n", "$f'(g(x))", "g'(x)$")
    )

    response = _send(client, session_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"

    frames = _frames(response.text)
    assert [frame["type"] for frame in frames] == [
        "start",
        "status",
        "status",
        "status",
        "token",
        "token",
        "token",
        "done",
    ]
    assert [frames[index]["stage"] for index in (1, 2, 3)] == [
        "prompt_processing",
        "reviewing_documents",
        "composing_answer",
    ]
    answer = "".join(str(f["text"]) for f in frames if f["type"] == "token")
    assert answer == "The chain rule:\n\n$f'(g(x))g'(x)$"
    # The frame types are the contract; there is no sentinel to look for.
    assert "[DONE]" not in response.text


def test_a_trimmed_retrieval_adds_a_notice_immediately_after_start(
    client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "retrieve", _returns(TRIMMED_RETRIEVAL))
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Sure."))

    frames = _frames(_send(client, session_id).text)
    assert [frame["type"] for frame in frames] == [
        "start",
        "status",
        "status",
        "notice",
        "status",
        "token",
        "done",
    ]
    assert frames[1] == {"type": "status", "stage": "prompt_processing"}
    assert frames[2] == {"type": "status", "stage": "reviewing_documents"}
    assert frames[3] == {
        "type": "notice",
        "retrieval_trimmed": True,
        "omitted_document_count": 2,
    }
    assert frames[4] == {"type": "status", "stage": "composing_answer"}


def test_the_question_is_stored_before_the_reply_and_the_reply_after(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_stream_start: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(routes_chat, "retrieve", _returns(TRIMMED_RETRIEVAL))
    monkeypatch.setattr(
        routes_chat, "stream_chat", _stream_of("Sure. ", "Start here.", recorder=at_stream_start)
    )

    frames = _frames(_send(client, session_id).text)
    stored = sessions.list_messages(db, session_id)

    # By the time the upstream produced its first token the question was already saved
    # and the answer was not.
    assert at_stream_start[0] == [("user", QUESTION)]
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[0]["id"] == frames[0]["message_id"]
    assert stored[1]["id"] == frames[-1]["message_id"]
    assert stored[1]["content"] == "Sure. Start here."
    assert stored[1]["retrieval_trimmed"] == 1
    assert stored[1]["omitted_document_count"] == 2

    listed = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["role"] for message in listed] == ["user", "assistant"]
    assert listed[1]["retrieval_trimmed"] is True
    assert listed[1]["omitted_document_count"] == 2


def test_no_configured_endpoint_is_a_plain_400_rather_than_a_stream(
    client: TestClient, db: sqlite3.Connection, session_id: int
) -> None:
    db.execute("update settings set endpoint_url = null")
    db.commit()

    response = _send(client, session_id)

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]
    # The turn never opened, so it left nothing behind to answer later.
    assert sessions.list_messages(db, session_id) == []


def test_an_upstream_failure_mid_stream_arrives_as_an_error_frame(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_then_fail("Let us "))

    response = _send(client, session_id)
    frames = _frames(response.text)

    assert response.status_code == 200
    assert [frame["type"] for frame in frames] == [
        "start",
        "status",
        "status",
        "status",
        "token",
        "error",
    ]
    assert [frames[index]["stage"] for index in (1, 2, 3)] == [
        "prompt_processing",
        "reviewing_documents",
        "composing_answer",
    ]
    assert frames[-1]["message"]
    # A failed turn is one to retry, so no half-answer is stored to look complete later.
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


def test_the_requested_mode_is_persisted_on_the_session(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Here it is."))
    assert sessions.get_session(db, session_id)["mode"] == "guide"

    _send(client, session_id, mode="show")

    assert sessions.get_session(db, session_id)["mode"] == "show"


def test_an_untitled_session_is_named_after_its_first_message(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Here it is."))
    assert sessions.get_session(db, session_id)["title"] is None

    _send(client, session_id)

    assert sessions.get_session(db, session_id)["title"] == QUESTION


def test_a_later_message_does_not_rename_the_session(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Here it is."))
    _send(client, session_id)
    first_title = sessions.get_session(db, session_id)["title"]

    client.post(
        f"/api/sessions/{session_id}/chat",
        json={"content": "A completely different question", "mode": "guide"},
    )

    assert sessions.get_session(db, session_id)["title"] == first_title


def test_a_session_created_with_a_title_keeps_it(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Here it is."))
    created = client.post(f"/api/classes/{class_id}/sessions", json={"title": "Week 9"})

    _send(client, created.json()["id"])

    assert sessions.get_session(db, created.json()["id"])["title"] == "Week 9"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Short one", "Short one"),
        ("  collapses\n  whitespace  ", "collapses whitespace"),
        # Cut at the last word boundary inside the limit, never mid-word.
        (
            "Explain how the region of convergence constrains the inverse transform",
            "Explain how the region of convergence...",
        ),
        # No boundary to cut at, so the hard slice stands rather than returning nothing.
        ("x" * 80, f"{'x' * 48}..."),
    ],
)
def test_a_title_is_condensed_at_a_word_boundary(content: str, expected: str) -> None:
    assert sessions.title_from_message(content) == expected


def test_a_blank_message_is_rejected_before_anything_is_stored(
    client: TestClient, db: sqlite3.Connection, session_id: int
) -> None:
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"content": "   ", "mode": "guide"}
    )

    assert response.status_code == 422
    assert sessions.list_messages(db, session_id) == []


async def test_a_disconnect_keeps_the_part_of_the_answer_that_arrived(
    db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the generator is what Starlette does when the reader goes away."""
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Half ", "an ", "answer."))
    request = routes_chat.ChatRequest(content=QUESTION, mode="guide")
    config = app_settings.TutorConfig(
        endpoint_url=ENDPOINT, api_key=None, model=None, context_window=8192
    )
    user_message_id = sessions.add_message(db, session_id, "user", QUESTION)

    plan = routes_chat.TurnPlan(user_message_id=user_message_id)
    stream = routes_chat._stream_turn(session_id, request, config, plan)
    started = _frames(await anext(stream))
    prompt_status = _frames(await anext(stream))
    retrieval_status = _frames(await anext(stream))
    composing_status = _frames(await anext(stream))
    first_token = _frames(await anext(stream))
    await stream.aclose()

    assert started[0] == {"type": "start", "message_id": user_message_id}
    assert prompt_status[0] == {"type": "status", "stage": "prompt_processing"}
    assert retrieval_status[0] == {"type": "status", "stage": "reviewing_documents"}
    assert composing_status[0] == {"type": "status", "stage": "composing_answer"}
    assert first_token[0] == {"type": "token", "text": "Half "}
    stored = sessions.list_messages(db, session_id)
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[1]["content"] == "Half "


def test_reasoning_streams_on_its_own_frames_and_is_stored_beside_the_answer(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_chat,
        "stream_chat",
        _stream_deltas(
            StreamDelta("reasoning", "Chain rule "),
            StreamDelta("reasoning", "applies here."),
            StreamDelta("answer", "Differentiate the outside first."),
        ),
    )

    frames = _frames(_send(client, session_id).text)

    assert [frame["type"] for frame in frames] == [
        "start",
        "status",
        "status",
        "status",
        "reasoning",
        "reasoning",
        "token",
        "done",
    ]
    stored = sessions.list_messages(db, session_id)
    assert stored[1]["content"] == "Differentiate the outside first."
    assert stored[1]["thinking"] == "Chain rule applies here."
    # The thought is never mixed into the reply the student reads.
    assert "Chain rule" not in str(stored[1]["content"])


def test_a_model_that_does_not_think_stores_an_empty_thought(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Straight to it."))

    _send(client, session_id)

    assert sessions.list_messages(db, session_id)[1]["thinking"] == ""


def test_regenerate_replaces_the_previous_answer_rather_than_appending_a_turn(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("First attempt."))
    _send(client, session_id)
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("Second attempt."))

    response = client.post(
        f"/api/sessions/{session_id}/regenerate", json={"mode": "show", "document_id": None}
    )

    assert response.status_code == 200
    stored = sessions.list_messages(db, session_id)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", QUESTION),
        ("assistant", "Second attempt."),
    ]
    # The question is answered again, not asked again.
    assert sessions.get_session(db, session_id)["mode"] == "show"


def test_the_regenerated_turn_does_not_show_the_model_its_discarded_attempt(
    client: TestClient, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[dict[str, str]]] = []
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("First attempt."))
    _send(client, session_id)

    async def capture(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        seen.append(messages)
        yield StreamDelta("answer", "Second attempt.")

    monkeypatch.setattr(routes_chat, "stream_chat", capture)
    client.post(f"/api/sessions/{session_id}/regenerate", json={"mode": "guide"})

    contents = [message["content"] for message in seen[0]]
    assert "First attempt." not in contents
    assert contents[-1] == QUESTION


def test_regenerating_an_empty_conversation_is_a_404_with_a_reason(
    client: TestClient, session_id: int
) -> None:
    response = client.post(f"/api/sessions/{session_id}/regenerate", json={"mode": "guide"})

    assert response.status_code == 404
    assert "try again" in str(response.json()["detail"])


def test_a_retry_that_fails_upstream_leaves_the_previous_answer_in_place(
    client: TestClient, db: sqlite3.Connection, session_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing a good answer to a failed retry would make the button unsafe to press."""
    monkeypatch.setattr(routes_chat, "stream_chat", _stream_of("The answer they already have."))
    _send(client, session_id)

    async def dies(
        endpoint_url: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> AsyncIterator[StreamDelta]:
        raise UpstreamError("The tutor endpoint is not reachable.")
        yield  # pragma: no cover - never reached, but this must stay a generator.

    monkeypatch.setattr(routes_chat, "stream_chat", dies)
    frames = _frames(
        client.post(f"/api/sessions/{session_id}/regenerate", json={"mode": "guide"}).text
    )

    assert frames[-1]["type"] == "error"
    stored = sessions.list_messages(db, session_id)
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", QUESTION),
        ("assistant", "The answer they already have."),
    ]
