"""Agent SSE sends live model fragments and preserves the durable turn contract."""

import asyncio
import json

import pytest
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from backend.api import routes_agent_chat as routes
from backend.core import sessions
from backend.core.errors import ConflictError
from backend.llm import tools
from backend.llm.client import StreamDelta
from backend.tests.test_api_agent_chat import client, empty_retrieval  # noqa: F401


def frames(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_stream_persists_reasoning_and_replays_without_synthetic_tokens(
    client,  # noqa: F811
    db,
    class_id,
    monkeypatch,  # noqa: F811
):
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = 0

    async def model(*args, on_delta, **kwargs):
        nonlocal calls
        calls += 1
        on_delta(StreamDelta("reasoning", "Check the premise."))
        on_delta(StreamDelta("answer", "Intermediate"))
        on_delta(StreamDelta("reset", ""))
        on_delta(StreamDelta("answer", "Final answer."))
        return tools.ToolLoopResult(content="Final answer.")

    monkeypatch.setattr(routes, "run_tool_loop", model)
    url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"
    body = {"content": "Explain it", "operation_id": "stream-operation"}
    response = client.post(url, json=body, headers={"Accept": "text/event-stream"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = frames(response)
    assert [event["type"] for event in events] == [
        "status",
        "reasoning",
        "token",
        "reset",
        "token",
        "result",
    ]
    message_id = events[-1]["result"]["message_id"]
    stored = db.execute(
        "select content, thinking from messages where id = ?", (message_id,)
    ).fetchone()
    assert tuple(stored) == ("Final answer.", "Check the premise.")
    replay = frames(client.post(url, json=body, headers={"Accept": "text/event-stream"}))
    assert len(replay) == 1
    assert replay[0]["result"]["message_id"] == message_id
    assert calls == 1
    assert sessions.active_turn(session_id) is None


@pytest.mark.parametrize("action", ["retry", "regenerate"])
def test_stream_retry_and_regenerate(client, db, class_id, monkeypatch, action):  # noqa: F811
    session_id = int(sessions.create_session(db, class_id)["id"])
    url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"
    original = client.post(url, json={"content": "Explain it"}).json()

    async def model(*args, on_delta, **kwargs):
        on_delta(StreamDelta("answer", "New reply."))
        return tools.ToolLoopResult(content="New reply.")

    monkeypatch.setattr(routes, "run_tool_loop", model)
    events = frames(client.post(f"{url}/{action}", headers={"Accept": "text/event-stream"}))
    assert events[-1]["type"] == "result"
    if action == "retry":
        assert len(events) == 1
        assert events[0]["result"]["message_id"] == original["message_id"]
    else:
        assert events[0] == {"type": "status", "stage": "composing_answer"}
        assert events[1] == {"type": "token", "text": "New reply."}
        assert events[-1]["result"]["message_id"] != original["message_id"]
    assert sessions.active_turn(session_id) is None


def test_toolless_stream_persists_thinking(client, db, class_id, monkeypatch):  # noqa: F811
    db.execute("update settings set tools_supported = 0 where id = 1")
    db.commit()
    session_id = int(sessions.create_session(db, class_id)["id"])

    async def model(*args, require_complete):
        assert require_complete
        yield StreamDelta("reasoning", "Considering the example.")
        yield StreamDelta("answer", "One ")
        yield StreamDelta("answer", "example.")

    monkeypatch.setattr(routes.llm_client, "stream_chat", model)
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain it"},
        headers={"Accept": "text/event-stream"},
    )
    events = frames(response)
    assert [event["type"] for event in events] == [
        "status",
        "reasoning",
        "token",
        "token",
        "result",
    ]
    row = db.execute(
        "select thinking from messages where id = ?", (events[-1]["result"]["message_id"],)
    ).fetchone()
    assert row["thinking"] == "Considering the example."


@pytest.mark.asyncio
async def test_incremental_delivery_and_disconnect_hold_claim_until_worker_quiesces(
    db, class_id, monkeypatch
):
    session_id = int(sessions.create_session(db, class_id)["id"])
    stopped = asyncio.Event()

    async def model(*args, gate, on_delta, **kwargs):
        on_delta(StreamDelta("reasoning", "Live thought"))
        try:
            await asyncio.Event().wait()
        finally:
            assert gate.stopped
            assert sessions.active_turn(session_id) is not None
            db.execute("select 1")
            stopped.set()

    monkeypatch.setattr(routes, "_run_agent_turn", model)
    response = await routes.send_agent_chat(
        class_id,
        session_id,
        routes.AgentChatRequest(content="Explain it"),
        db,
        accept="text/event-stream",
    )
    assert isinstance(response, StreamingResponse)
    iterator = response.body_iterator
    first = json.loads((await asyncio.wait_for(anext(iterator), 1))[6:])
    assert first == {"type": "reasoning", "text": "Live thought"}
    assert not stopped.is_set()
    assert sessions.active_turn(session_id) is not None
    await iterator.aclose()
    assert stopped.is_set()
    assert sessions.active_turn(session_id) is None


@pytest.mark.asyncio
async def test_explicit_stop_produces_terminal_result(db, class_id, monkeypatch):
    session_id = int(sessions.create_session(db, class_id)["id"])

    async def model(*args, on_delta, **kwargs):
        on_delta(StreamDelta("answer", "Partial"))
        await asyncio.Event().wait()

    monkeypatch.setattr(routes, "_run_agent_turn", model)
    response = await routes.send_agent_chat(
        class_id,
        session_id,
        routes.AgentChatRequest(content="Explain it"),
        db,
        accept="text/event-stream",
    )
    iterator = response.body_iterator
    await anext(iterator)
    stop = await routes.stop_agent_chat(class_id, session_id, db)
    assert stop["stopped"]
    terminal = json.loads((await asyncio.wait_for(anext(iterator), 1))[6:])
    assert terminal["type"] == "result"
    assert terminal["result"]["stopped"] == "stopped"
    await iterator.aclose()
    assert sessions.active_turn(session_id) is None


def test_stream_errors_keep_status_and_structured_code(client, db, class_id, monkeypatch):  # noqa: F811
    session_id = int(sessions.create_session(db, class_id)["id"])

    async def reject(*args, **kwargs):
        raise ConflictError("Different request", extra={"code": "operation_id_mismatch"})

    monkeypatch.setattr(routes, "_run_agent_turn", reject)
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain it"},
        headers={"Accept": "text/event-stream"},
    )
    assert frames(response) == [
        {
            "type": "error",
            "status": 409,
            "detail": "Different request",
            "code": "operation_id_mismatch",
        }
    ]
    assert sessions.active_turn(session_id) is None


@pytest.mark.asyncio
async def test_claim_released_when_headers_fail_before_iterator_starts(db, class_id, monkeypatch):
    session_id = int(sessions.create_session(db, class_id)["id"])
    started = False

    async def model(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("No model should start")

    monkeypatch.setattr(routes, "_run_agent_turn", model)
    response = await routes.send_agent_chat(
        class_id,
        session_id,
        routes.AgentChatRequest(content="Explain it"),
        db,
        accept="text/event-stream",
    )

    async def send(message):
        raise OSError("Transport gone")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not started
    assert sessions.active_turn(session_id) is None


def test_failed_stream_never_commits_partial_reply(client, db, class_id, monkeypatch):  # noqa: F811
    session_id = int(sessions.create_session(db, class_id)["id"])

    async def model(*args, on_delta, **kwargs):
        on_delta(StreamDelta("answer", "Incomplete"))
        return tools.ToolLoopResult(
            content="Incomplete", stopped=tools.UPSTREAM_FAILED, detail="Connection lost"
        )

    monkeypatch.setattr(routes, "run_tool_loop", model)
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain it"},
        headers={"Accept": "text/event-stream"},
    )
    events = frames(response)
    assert events[1] == {"type": "token", "text": "Incomplete"}
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 502
    assert events[-1]["retryable"] is True
    assert (
        db.execute(
            "select count(*) from messages where session_id = ? and role = 'assistant'",
            (session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "select state from agent_turn_attempts where session_id = ?", (session_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert sessions.active_turn(session_id) is None


@pytest.mark.parametrize(
    "ending", ["", 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n']
)
def test_toolless_incomplete_stream_does_not_persist_answer(
    client,  # noqa: F811
    db,
    class_id,
    monkeypatch,
    ending,  # noqa: F811
):
    import httpx

    db.execute("update settings set tools_supported = 0 where id = 1")
    db.commit()
    session_id = int(sessions.create_session(db, class_id)["id"])
    original = routes.llm_client.stream_chat
    wire = 'data: {"choices":[{"delta":{"content":"Partial"}}]}\n\n' + ending
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=wire))

    async def model(*args, **kwargs):
        async for delta in original(*args, transport=transport, **kwargs):
            yield delta

    monkeypatch.setattr(routes.llm_client, "stream_chat", model)
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain it"},
        headers={"Accept": "text/event-stream"},
    )
    events = frames(response)
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 502
    assert (
        db.execute(
            "select count(*) from messages where session_id = ? and role = 'assistant'",
            (session_id,),
        ).fetchone()[0]
        == 0
    )
    assert sessions.active_turn(session_id) is None
