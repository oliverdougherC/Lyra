"""Class-agent turns use one explicit profile and persist durable activity references."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat
from backend.core import sessions
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.llm import tools
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db.execute(
        "update settings set endpoint_url = 'http://127.0.0.1:8080/v1', tools_supported = 1 "
        "where id = 1"
    )
    db.commit()

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        assert isinstance(registry, dict)
        assert "cas_evaluate" in registry
        return tools.ToolLoopResult(content="Profile-bounded answer.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_agent_chat.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _stub_loop(
    monkeypatch: pytest.MonkeyPatch,
    result: tools.ToolLoopResult,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        captured["messages"] = args[3]
        captured["registry"] = kwargs["registry"]
        assert isinstance(kwargs["registry"], dict)
        assert "cas_evaluate" in kwargs["registry"]
        return result

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    return captured


def _seed_turn(db: sqlite3.Connection, session_id: int, index: int) -> None:
    filler = " ".join([f"history{index}"] * 40)
    sessions.add_message(db, session_id, "user", f"Question {index}: {filler}")
    sessions.add_message(db, session_id, "assistant", f"Answer {index}: {filler}")


def test_agent_turn_persists_in_the_existing_class_conversation(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain the repository", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Profile-bounded answer."
    messages = sessions.list_messages(db, session_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Profile-bounded answer."


def test_agent_turn_refuses_a_session_from_another_class(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    other = int(db.execute("insert into classes (name) values ('Other')").lastrowid or 0)
    db.commit()
    session_id = int(sessions.create_session(db, other)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Search", "profile": "research"},
    )
    assert response.status_code == 404


def test_agent_turn_uses_budgeted_history_and_aligns_private_context(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    for index in range(6):
        _seed_turn(db, session_id, index)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Trimmed answer."))
    seen: dict[str, object] = {}
    original = routes_agent_chat.agent_tools.build_agent_registry

    def capture_registry(*args: object, **kwargs: object):  # noqa: ANN002, ANN003
        seen["private_context"] = kwargs["private_context"]
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", capture_registry)
    monkeypatch.setattr(
        routes_agent_chat,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:8080/v1", None, "m", 256),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Newest question", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    prompt = captured["messages"]
    history = prompt[1:-1]
    assert prompt[-1] == {"role": "user", "content": "Newest question"}
    assert 0 < len(history) < 12
    rendered = [str(message["content"]) for message in history]
    assert all("Question 0:" not in content and "Answer 0:" not in content for content in rendered)
    assert any("Question 5:" in content for content in rendered)
    assert any("Answer 5:" in content for content in rendered)
    assert seen["private_context"] == tuple(rendered) + ("Newest question",)


def test_an_incomplete_agent_turn_returns_a_retry_contract_without_storing_a_reply(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    _stub_loop(
        monkeypatch,
        tools.ToolLoopResult(
            content="",
            stopped=tools.DEPTH,
            detail="Checking stopped after 24 rounds of tool calls.",
        ),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Keep checking", "profile": "command"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Checking stopped after 24 rounds of tool calls.",
        "retryable": False,
        "stopped": "depth",
        "activity": [],
        "source_ids": [],
        "workspace_change_ids": [],
        "command_request_ids": [],
        "profile_fact_ids": [],
    }
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


@pytest.mark.parametrize(
    ("stopped", "detail", "status_code"),
    [
        (tools.TIMEOUT, "Checking took too long and was stopped.", 504),
        (tools.UPSTREAM_FAILED, "The tutor endpoint could not be reached.", 502),
    ],
)
def test_timeout_and_upstream_failures_are_retryable_and_leave_no_assistant_message(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    stopped: str,
    detail: str,
    status_code: int,
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    _stub_loop(
        monkeypatch,
        tools.ToolLoopResult(content="", stopped=stopped, detail=detail),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Retry me", "profile": "code"},
    )

    assert response.status_code == status_code
    assert response.json()["detail"] == detail
    assert response.json()["retryable"] is True
    assert response.json()["stopped"] == stopped
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]
