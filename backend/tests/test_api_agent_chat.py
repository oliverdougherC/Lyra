"""Class-agent turns use one explicit profile and persist durable activity references."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat
from backend.core import app_settings, sessions
from backend.core.app_settings import TutorAccess, TutorConfig
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
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            config=TutorConfig("http://127.0.0.1:8080/v1", None, "m", 256),
            document_block=None,
            remote_ack=False,
        ),
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


# --- The document-context consent gate ----------------------------------------------
#
# An agent turn's first round carries the conversation history and the student's message;
# later rounds carry workspace file contents, fetched-page evidence, and command-planning
# context as tool results re-enter the model context. All of that is private material, so
# the route is bound by the same locality/acknowledgement rule as tutor chat: it may reach
# a non-loopback endpoint only after the student has acknowledged it in Settings.

# A documentation-range IP (RFC 5737): non-loopback, and numeric so `is_local_endpoint`
# classifies it without a DNS lookup.
REMOTE_ENDPOINT = "http://203.0.113.10:8081/v1"

PROFILES = ("research", "code", "command")


def _use_remote_endpoint(db: sqlite3.Connection, *, acknowledged: bool) -> None:
    """Point the tutor at a non-loopback endpoint, with or without the acknowledgement."""
    db.execute(
        "update settings set endpoint_url = ?, remote_ack = ?",
        (REMOTE_ENDPOINT, int(acknowledged)),
    )
    db.commit()


def _spy_loop(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """A `run_tool_loop` stand-in that records every call, to prove one never happened."""
    calls: list[tuple[object, ...]] = []

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        calls.append(args)
        return tools.ToolLoopResult(content="Recorded answer.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    return calls


def _spy_registry(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record registry builds. A refused turn must not even assemble its tools."""
    built: list[object] = []
    original = routes_agent_chat.agent_tools.build_agent_registry

    def capture(*args: object, **kwargs: object):  # noqa: ANN002, ANN003, ANN202
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", capture)
    return built


@pytest.mark.parametrize("profile", PROFILES)
def test_an_unacknowledged_remote_endpoint_refuses_and_sends_nothing(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    """Each profile's private surface differs (history, workspace files, command context),
    and none of it may leave for an unacknowledged remote endpoint."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    _seed_turn(db, session_id, 0)
    _use_remote_endpoint(db, acknowledged=False)
    calls = _spy_loop(monkeypatch)
    built = _spy_registry(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A question that must stay on this machine", "profile": profile},
    )

    assert response.status_code == 400
    # The wording points at Settings, like the rest of the privacy language.
    assert "Settings" in str(response.json()["detail"])
    # Zero upstream requests: neither the tool loop nor any tool ran...
    assert calls == []
    # ...the tool registry was never even assembled...
    assert built == []
    # ...and nothing was persisted merely because the gate refused: no orphaned user turn,
    # and the session title was not claimed by the refused question.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]
    assert sessions.get_session(db, session_id)["title"] != (
        "A question that must stay on this machine"
    )


def test_a_refused_turn_persists_nothing_in_an_empty_session(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    _use_remote_endpoint(db, acknowledged=False)
    calls = _spy_loop(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "First question", "profile": "code"},
    )

    assert response.status_code == 400
    assert calls == []
    assert sessions.list_messages(db, session_id) == []
    assert sessions.get_session(db, session_id)["title"] is None


def test_a_loopback_endpoint_answers_without_acknowledgement(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixture endpoint is 127.0.0.1 with remote_ack unset. Loopback is local, so it must
    # never need the remote acknowledgement.
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = _spy_loop(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Stay local", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0][0] == "http://127.0.0.1:8080/v1"


def test_an_acknowledged_remote_endpoint_still_answers(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = int(sessions.create_session(db, class_id)["id"])
    _use_remote_endpoint(db, acknowledged=True)
    calls = _spy_loop(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Remote is allowed now", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0][0] == REMOTE_ENDPOINT
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]


def _settings_row(endpoint_url: str, *, remote_ack: int) -> dict[str, object]:
    """The columns `resolve_tutor_access` reads, shaped like the sqlite row it expects."""
    return {
        "endpoint_url": endpoint_url,
        "model": "test-model",
        "context_window": 8192,
        "remote_ack": remote_ack,
        "extraction_enabled": 1,
    }


def _hand_out(monkeypatch: pytest.MonkeyPatch, *rows: dict[str, object]) -> None:
    """Hook `get_settings_row` to return each of `rows` in turn, then the real row.

    This is the interleaving a two-read code path could hit: the first read sees one endpoint
    state, a later read sees another. A single-read snapshot consumes exactly one.
    """
    real = app_settings.get_settings_row
    handed = iter(rows)
    monkeypatch.setattr(
        app_settings, "get_settings_row", lambda conn: next(handed, None) or real(conn)
    )


def test_a_settings_flip_between_reads_cannot_send_to_the_captured_endpoint(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote captured, then settings go local before a second read would happen.

    The turn resolves the remote endpoint and its consent from one snapshot and refuses. A
    two-read path would pair the remote config with the later local read's permission and
    send the turn upstream.
    """
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = _spy_loop(monkeypatch)
    _hand_out(
        monkeypatch,
        _settings_row(REMOTE_ENDPOINT, remote_ack=0),
        _settings_row("http://127.0.0.1:9/v1", remote_ack=1),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Race the settings write", "profile": "code"},
    )

    assert response.status_code == 400
    assert calls == []
    assert sessions.list_messages(db, session_id) == []


def test_a_local_turn_is_not_redirected_by_a_later_flip_to_remote(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror: local captured, then settings go remote-unacknowledged. The turn is
    answered on the local endpoint from the authorizing snapshot, never the remote one a
    second read would produce."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = _spy_loop(monkeypatch)
    _hand_out(
        monkeypatch,
        _settings_row("http://127.0.0.1:9/v1", remote_ack=0),
        _settings_row(REMOTE_ENDPOINT, remote_ack=0),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Stay where you were authorized", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert [call[0] for call in calls] == ["http://127.0.0.1:9/v1"]
