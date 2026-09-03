"""Class-agent turns use one explicit profile and persist durable activity references."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat
from backend.api.routes_agent_chat import AgentTurnCost
from backend.core import (
    agent_attempts,
    agent_store,
    agent_tools,
    app_settings,
    sessions,
    web_research,
)
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError
from backend.core.query_guard import PrivateContextLedger
from backend.core.writer_budgets import WriterCapabilities
from backend.llm import prompts as llm_prompts
from backend.llm import tools
from backend.rag.retrieve import RetrievalResult, RetrievedChunk
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def empty_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns without a selected document now retrieve class-wide, like the tutor.
    Tests that do not exercise retrieval get the same empty result the tutor tests
    stub with: the hosted CI has no embedding model to embed against."""
    nothing = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id, query, budget_tokens, document_id=None: nothing,
    )


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
        # Mirrors backend.main: structured `extra` fields ride the body beside `detail`.
        content: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

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
        captured["context_budget"] = kwargs.get("context_budget")
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
    # A 3072-token window is roomy enough to host the tool definitions and the newest few
    # exchanges, but not the whole conversation: older turns are trimmed, and the private
    # context the web-query guard sees is exactly the history that survives into the prompt.
    # The window has to leave room for the estimator safety margin the preflight now charges
    # on top of the exact wire-shape accounting; a 2048 window would refuse this whole turn
    # once the margin, the message framing, and the ~1,055-token tool schema are all charged.
    # (A 256-token window, the smallest Settings allows, cannot host the tool schemas at all
    # and is refused up front - see test_a_512_token_window_cannot_host_the_tool_definitions.)
    session_id = int(sessions.create_session(db, class_id)["id"])
    for index in range(6):
        _seed_turn(db, session_id, index)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Trimmed answer."))
    seen: list[object] = []
    original = routes_agent_chat.agent_tools.build_agent_registry

    def capture_registry(*args: object, **kwargs: object):  # noqa: ANN002, ANN003
        seen.append(kwargs["private_context"])
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", capture_registry)
    monkeypatch.setattr(
        routes_agent_chat,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            config=TutorConfig("http://127.0.0.1:8080/v1", None, "m", 3072),
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
    # The registry that runs the turn is built from the private context that survived the
    # trim - the last build wins, and it is the one aligned with the assembled prompt. The
    # read-only preflight probe (built first, with an empty private context) is what makes
    # the tool schemas measurable before history is trimmed. The ledger the execution
    # registry receives snapshots to exactly the prompt's private material: the history
    # that reached the prompt, plus the question.
    assert seen[0] == ()
    last_context = seen[-1]
    assert isinstance(last_context, PrivateContextLedger)
    assert last_context.snapshot() == tuple(rendered) + ("Newest question",)


def test_a_scoped_source_grounds_the_agent_turn(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source scoped in the composer grounds the agent turn as fixed system material."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Grounded answer."))

    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=7,
        content="Convolution combines two signals.",
        token_count=6,
        page_number=1,
        section_title="Convolution",
        section_path="ch2/convolution",
        section_number="2.1",
        problem_number=None,
        part_index=None,
        filename="signals.pdf",
        similarity=0.9,
        score=0.9,
    )
    retrieved: list[int] = []

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        retrieved.append(document_id)
        return RetrievalResult(chunks=[chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution", "document_id": 7},
    )

    assert response.status_code == 200, response.text
    assert retrieved == [7]
    system_prompt = str(captured["messages"][0]["content"])
    assert "Convolution combines two signals." in system_prompt
    assert "signals.pdf" in system_prompt


def test_an_unscoped_agent_turn_retrieves_class_wide_like_the_tutor(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn without a selected document retrieves class-wide, exactly like the tutor.

    An absent `document_id` is the composer's "all material" scope: the whole class's
    indexed material is in play, and the best-matching chunks ground the turn as fixed
    system material. Retrieval is not a scoped-only feature that stays switched off.
    """
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Grounded answer."))

    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=7,
        content="Convolution combines two signals.",
        token_count=6,
        page_number=1,
        section_title="Convolution",
        section_path="ch2/convolution",
        section_number="2.1",
        problem_number=None,
        part_index=None,
        filename="signals.pdf",
        similarity=0.9,
        score=0.9,
    )
    seen_documents: list[int | None] = []

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        seen_documents.append(document_id)
        return RetrievalResult(chunks=[chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution"},  # no document_id: "all material"
    )

    assert response.status_code == 200, response.text
    # Class-wide, not skipped: the retrieval ran with no document filter.
    assert seen_documents == [None]
    system_prompt = str(captured["messages"][0]["content"])
    assert "Convolution combines two signals." in system_prompt
    assert "signals.pdf" in system_prompt


def test_an_empty_retrieval_is_a_valid_agent_turn(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class-wide search that matches nothing is a valid turn, not an error.

    The turn proceeds on history, the prompt contract, and tools: no source section is
    rendered, and the answer still commits as an ordinary class-conversation reply.
    """
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="From history and tools."))
    seen_documents: list[int | None] = []

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        seen_documents.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution"},
    )

    assert response.status_code == 200, response.text
    assert seen_documents == [None]
    # No source section rendered for a matchless search; the answer still lands.
    assert "signals.pdf" not in str(captured["messages"][0]["content"])
    messages = sessions.list_messages(db, session_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]


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

PROFILES = ("research", "code", "command", "agent")


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


# --- Charging the whole agent turn against the context window ------------------------
#
# PLA-290. The current message is appended to every round and never trimmed, and it does
# not stand alone: the generation reserve, the system prompt, the tool definitions sent on
# every round, and the newest history `trim_history` always keeps are non-negotiable too.
# A turn is accepted only when those fit the configured window; anything past that would
# reach the endpoint only by overrunning the window, so it is refused before the message is
# persisted, the class is touched, the tool registry is executed, or any request is made.
# The tool schemas alone cost roughly a thousand tokens, so a window small enough to matter
# cannot host an agent turn at all - which is exactly why the fit check has to account for
# them.

# The side-effect ledgers an agent turn can write to. A refused turn must leave every one
# of them exactly as it found it.
_EFFECT_TABLES = (
    "tool_audit_events",
    "writer_sources",
    "profile_facts",
    "workspace_changes",
    "command_requests",
)


def _effect_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(db.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
        for table in _EFFECT_TABLES
    }


def _set_window(db: sqlite3.Connection, tokens: int) -> None:
    db.execute("update settings set context_window = ?", (tokens,))
    db.commit()


def test_the_current_message_is_charged_against_the_budget() -> None:
    # The unit invariant PLA-290 turns on: an empty-history turn that fits with a trivial
    # message is refused once the real message is charged. If the current message were left
    # out of the budget - the pre-fix bug - `charged.fits` would still be true.
    empty = AgentTurnCost(
        context_window=8192,
        generation=2048,
        system_tokens=100,
        tool_tokens=1055,
        question_tokens=0,
        earlier=(),
    )
    assert empty.fits
    charged = replace(empty, question_tokens=5000)
    assert not charged.fits
    # The refusal is the message's doing: nothing else changed.
    assert charged.reserved == empty.reserved + 5000


def test_the_generation_reserve_is_charged_against_the_budget() -> None:
    # A turn that overruns the window only because the reply reserve is held back. Dropping
    # the reserve - another way to make an oversized turn look admissible - is what would
    # wrongly admit it, so this fails if the reserve is ever removed from the sum.
    with_reserve = AgentTurnCost(
        context_window=8192,
        generation=2048,
        system_tokens=100,
        tool_tokens=1055,
        question_tokens=5000,
        earlier=(),
    )
    assert not with_reserve.fits
    assert replace(with_reserve, generation=0).fits


def test_tool_definitions_are_charged_as_fixed_prompt_space() -> None:
    # The agent injects no retrieval block; its mandatory non-trimmable context is the tool
    # schema, sent on every round. A leaner schema fits where a heavier one cannot, so an
    # oversized tool/workspace surface is charged and can refuse a turn on its own.
    lean = AgentTurnCost(
        context_window=4096,
        generation=1024,
        system_tokens=100,
        tool_tokens=200,
        question_tokens=2000,
        earlier=(),
    )
    assert lean.fits
    assert not replace(lean, tool_tokens=2000).fits


def test_the_newest_history_is_charged_even_when_older_history_is_free_to_trim() -> None:
    # The two newest messages are as non-negotiable as the current message: their cost is
    # charged up front, so a turn cannot be admitted on the assumption that all history is
    # trimmable when the mandatory pair alone will not fit.
    from backend.llm.turn_budget import HistoryMessage

    history = tuple(
        HistoryMessage(role="user" if index % 2 == 0 else "assistant", content="h" * 8000)
        for index in range(6)
    )
    cost = AgentTurnCost(
        context_window=4096,
        generation=1024,
        system_tokens=100,
        tool_tokens=200,
        question_tokens=100,
        earlier=history,
    )
    # Two 2000-token messages are kept whatever the budget, and that pair alone overruns the
    # window once the reserve, system, and tools are set aside.
    assert cost.mandatory_history_tokens == 4000
    assert not cost.fits


def test_the_assembler_and_the_fit_gate_agree_on_the_canonical_accounting() -> None:
    # The trim loop decides what to keep with `conversation_tokens` on the candidate whole
    # request - the same array serialization the fit gate then charges - so a trimmed turn it
    # produces always passes the gate. A per-message sum would drop the array's own framing
    # (the `[` `]` and the commas), keep one message too many, and be refused a line later.
    from backend.llm.turn_budget import HistoryMessage

    history = tuple(
        HistoryMessage(
            role="user" if index % 2 == 0 else "assistant", content=f"turn {index} " * 30
        )
        for index in range(20)
    )
    tool_tokens = 200
    ceiling = 900  # tight enough that older turns must be trimmed
    messages, kept = routes_agent_chat._assemble_within_ceiling(
        "You are an agent.",
        history,
        "The newest question.",
        message_ceiling=ceiling - tool_tokens,
    )

    # Trimming happened but the mandatory recent pair and the newest question survived.
    assert 0 < len(kept) < len(history)
    assert messages[-1] == {"role": "user", "content": "The newest question."}
    # The assembled request, measured the way the gate measures it, fits - so the gate does
    # not raise on a turn the assembler produced.
    assert tools.conversation_tokens(messages) + tool_tokens <= ceiling
    routes_agent_chat._require_request_fits(messages, tool_tokens, ceiling)  # must not raise


def test_a_512_token_window_answers_tool_less_instead_of_refusing(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The smallest window Settings permits. The tool schemas alone outweigh it, so the tool
    # surface cannot fit - but the tool-less surface (no schemas charged) can, and the
    # student's ordinary question is not refused over the cost of optional capability
    # (PLA-401 final pass): it is answered as a plain completion, the full tutor contract,
    # with the capability note that the agent work is unavailable. The loop never runs and
    # no tool schemas are sent; the reply persists like any other.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 512)
    calls = _spy_loop(monkeypatch)
    sent: dict[str, object] = {}

    async def fake_complete(*args: object, **kwargs: object) -> str:
        sent["messages"] = args[3]
        return "Answered without tools."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Anything at all", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Answered without tools."
    # No tool round trip happened: the loop never ran and no schema rode the request.
    assert calls == []
    system = str(sent["messages"][0]["content"])
    assert "cas_evaluate" not in system
    # The legacy profile's own note says its capability is unavailable, plainly.
    assert "Workspace reading is currently disabled or unavailable" in system
    # A normal turn: the question and the answer persist, and the title is claimed.
    roles = [m["role"] for m in sessions.list_messages(db, session_id)]
    assert roles == ["user", "assistant"]
    assert sessions.get_session(db, session_id)["title"] is not None


def test_a_window_that_cannot_host_even_the_toolless_turn_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same smallest window, with a question that even the tool-less surface cannot
    # host: the turn is refused locally, with a bounded student-facing message, before any
    # request, loop run, or persistence. This is the only remaining "too large" refusal.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 512)
    calls = _spy_loop(monkeypatch)
    complete_calls: list[int] = []

    async def refusing_complete(*args: object, **kwargs: object) -> str:
        complete_calls.append(1)
        return "unreachable"

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", refusing_complete)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "word " * 2000, "profile": "code"},
    )

    assert response.status_code == 400
    detail = str(response.json()["detail"])
    assert "context window" in detail
    # Bounded and privacy-safe: no endpoint, path, key, or transcript in the refusal.
    assert "127.0.0.1" not in detail and "http" not in detail
    assert calls == []
    assert complete_calls == []
    assert sessions.list_messages(db, session_id) == []
    assert sessions.get_session(db, session_id)["title"] is None


def test_an_impossible_initial_turn_leaves_the_database_untouched(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression the ticket turns on: an impossible turn is refused before the session
    # title, the user message, class recency, tool audit, sources, profile facts, workspace
    # changes, or command requests are touched, and before the tool loop or any tool runs.
    # If persistence were moved ahead of the preflight, the message and title assertions
    # below would fail.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _seed_turn(db, session_id, 0)  # a prior exchange that must survive untouched
    before_messages = sessions.list_messages(db, session_id)
    before_counts = _effect_counts(db)
    last_active = db.execute(
        "select last_active_at from classes where id = ?", (class_id,)
    ).fetchone()[0]
    _set_window(db, 512)

    calls = _spy_loop(monkeypatch)
    touched: list[int] = []
    monkeypatch.setattr(routes_agent_chat, "touch_class", lambda conn, cid: touched.append(cid))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "An impossible, oversized turn", "profile": "research"},
    )

    assert response.status_code == 400
    # No model request and no tool ran.
    assert calls == []
    # Class recency was never bumped, and nothing new landed in any ledger.
    assert touched == []
    assert (
        db.execute("select last_active_at from classes where id = ?", (class_id,)).fetchone()[0]
        == last_active
    )
    assert _effect_counts(db) == before_counts
    assert all(count == 0 for count in before_counts.values())
    # The conversation is exactly as it was: no orphaned user turn, no claimed title.
    assert sessions.list_messages(db, session_id) == before_messages
    assert sessions.get_session(db, session_id)["title"] is None


def test_a_20000_character_prompt_falls_back_tool_less_at_the_default_window(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `AgentChatRequest.content` permits 20,000 characters - about 5,000 tokens. Beside the
    # generation reserve and the tool schema, that does not fit the default 8,192 window, so
    # the tool surface cannot be sent - but the question itself (without the schemas) does
    # fit, and the turn is not refused over the cost of optional capability (PLA-401 final
    # pass): it is answered tool-less, the full tutor contract with no tool round trip. A
    # 24,576 window has room for the whole agent surface, so the same message is answered
    # with tools there. Worded (not one unbroken run), because the question now also rides
    # the retrieval query and an un-splittable run is an embedding failure, not a
    # turn-shape problem.
    prompt = "word " * 4_000
    fixed_without_question = (
        2048 + estimate_tokens(routes_agent_chat._SYSTEM_PROMPTS["code"]) + 1055
    )
    assert fixed_without_question < 8192  # only the charged question + schemas overfill

    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    toolless_calls = _spy_loop(monkeypatch)
    sent: dict[str, object] = {}

    async def fake_complete(*args: object, **kwargs: object) -> str:
        sent["messages"] = args[3]
        return "Long but answerable, without tools."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)
    first = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": prompt, "profile": "code"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["content"] == "Long but answerable, without tools."
    # The tool surface was refused by the budget, not the endpoint: the loop never ran and
    # the plain completion carried the turn, with the whole message in the prompt - never
    # silently truncated.
    assert toolless_calls == []
    assert sent["messages"][-1] == {"role": "user", "content": prompt.strip()}
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]

    _set_window(db, 24_576)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Long but answerable."))
    ok = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": prompt, "profile": "code"},
    )
    assert ok.status_code == 200, ok.text
    # The whole message survived into the tool-surface prompt too - it is never silently
    # truncated (the request's documented strip is the only change, as in the tutor route).
    assert captured["messages"][-1] == {"role": "user", "content": prompt.strip()}


def test_oversized_tool_overhead_falls_back_to_the_toolless_surface(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent's mandatory non-trimmable context is its tool schema. When that overhead is
    # large enough to fill the window on its own, the turn is not refused - it is planned
    # tool-less (no schemas charged) and answered as a plain completion, the loop never
    # running. The refusal remains only for a window that even the tool-less surface cannot
    # host (test_a_window_that_cannot_host_even_the_toolless_turn_is_refused).
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    monkeypatch.setattr(routes_agent_chat, "schema_tokens", lambda schemas: 10_000)
    calls = _spy_loop(monkeypatch)
    sent: dict[str, object] = {}

    async def fake_complete(*args: object, **kwargs: object) -> str:
        sent["messages"] = args[3]
        return "Answered without tools."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A short question", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Answered without tools."
    # The tool surface was refused by the budget, not the endpoint: no loop, no schemas sent.
    assert calls == []
    system = str(sent["messages"][0]["content"])
    assert "Workspace reading is currently disabled or unavailable" in system
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]


def test_long_history_trims_older_turns_but_keeps_the_mandatory_recent_pair(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long conversation under a window that cannot hold all of it: older optional turns are
    # dropped oldest-first, the newest exchange is always kept, and the assembled prompt plus
    # the tool overhead and the generation reserve fit the window.
    session_id = int(sessions.create_session(db, class_id)["id"])
    for index in range(12):
        _seed_turn(db, session_id, index)
    _set_window(db, 2048)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Answer."))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Latest", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    messages = captured["messages"]
    history = messages[1:-1]
    rendered = [str(message["content"]) for message in history]
    # The newest exchange survives; the oldest is trimmed away.
    assert any("Question 11:" in content for content in rendered)
    assert any("Answer 11:" in content for content in rendered)
    assert all("Question 0:" not in content for content in rendered)
    assert 0 < len(history) < 24
    # The assembled first request, measured the way the budget estimates it, fits the window
    # beside the tool overhead and the generation reserve.
    budget = captured["context_budget"]
    prompt_tokens = sum(estimate_tokens(str(message["content"])) for message in messages)
    assert prompt_tokens + budget.tool_tokens + budget.generation_reserve <= 2048


def test_a_normal_agent_turn_still_succeeds_and_passes_the_loop_a_context_budget(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The happy path stays intact, and the tool loop is handed the same window and reserve
    # the preflight proved the first request against, so later rounds are guarded too.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Ordinary answer."))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain part (b)", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Ordinary answer."
    budget = captured["context_budget"]
    assert isinstance(budget, tools.ContextBudget)
    assert budget.context_window == 8192
    assert budget.generation_reserve == 2048
    assert budget.tool_tokens > 0
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == [
        "user",
        "assistant",
    ]


def test_a_context_overflow_settles_as_a_bounded_failure_without_an_assistant_reply(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A later round whose tool transcript outgrew the window comes back from the loop as
    # CONTEXT_OVERFLOW. The route settles it as an honest, bounded, non-retryable failure:
    # the user turn stays, no assistant reply is invented, and the detail leaks nothing.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    _stub_loop(
        monkeypatch,
        tools.ToolLoopResult(
            content="",
            stopped=tools.CONTEXT_OVERFLOW,
            detail=tools._OVERFLOW_DETAIL,
        ),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Keep using tools", "profile": "research"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["stopped"] == "context_overflow"
    assert body["retryable"] is False
    assert "http" not in body["detail"] and "127.0.0.1" not in body["detail"]
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


def test_an_output_limit_settles_as_a_bounded_non_retryable_failure_without_a_reply(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint cut a guarded round off at the reserved output ceiling, so the loop
    # returns OUTPUT_LIMIT. The route settles it exactly as CONTEXT_OVERFLOW: a bounded,
    # non-retryable 503 that keeps the user turn, invents no assistant reply, and leaks
    # nothing. A truncated fragment is never stored as a successful answer.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    _stub_loop(
        monkeypatch,
        tools.ToolLoopResult(
            content="",
            stopped=tools.OUTPUT_LIMIT,
            detail=tools._OUTPUT_LIMIT_DETAIL,
        ),
    )

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Answer at length", "profile": "research"},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["stopped"] == "output_limit"
    assert body["retryable"] is False
    assert "http" not in body["detail"] and "127.0.0.1" not in body["detail"]
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


# --- Freezing the executable registry to the budgeted one (PLA-290 blocker 2) --------
#
# Tool exposure follows mutable capability state (web-research and workspace grants). The
# preflight budgets one registry and the loop must run that same registry, or the schema
# it charged, the availability wording it wrote, and the schema it sends can drift apart
# when a grant flips mid-turn. The schema-gating state is now read once into a frozen
# snapshot and the executable registry is built from it before any mutation; dispatch-time
# reauthorization still reads the live grant, so a grant revoked after planning fails
# closed and a grant enabled after planning simply waits for the next turn.


def _enable_web_research(db: sqlite3.Connection) -> None:
    db.execute("update settings set allow_web_research = 1 where id = 1")
    db.commit()


def _caps(*, web: bool) -> WriterCapabilities:
    return WriterCapabilities(
        allow_web_research=web,
        parallel_requests=False,
        parallel_concurrency=1,
        source_content_enabled=False,
    )


def _fake_workspace(root: object, *, read: bool, change: bool = False, commands: bool = False):
    """A workspace row shaped like the sqlite row the store returns, over a real directory."""
    import os

    details = os.lstat(str(root))
    return {
        "root_path": str(root),
        "root_device": details.st_dev,
        "root_inode": details.st_ino,
        "read_enabled": read,
        "change_proposals_enabled": change,
        "commands_enabled": commands,
    }


def test_the_tool_schemas_sent_are_exactly_the_schemas_budgeted(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry the loop is handed is charged, to the token, by the same preflight that
    # decided the turn fits: `schema_tokens` of the schemas actually sent equals the
    # `tool_tokens` the loop was budgeted, and the availability copy matches that registry.
    _enable_web_research(db)
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Find public sources", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    registry = captured["registry"]
    budget = captured["context_budget"]
    assert "search_web" in registry
    assert tools.schema_tokens(tools.tool_schemas(registry)) == budget.tool_tokens
    # Availability copy agrees with the frozen registry: the tool is present, so nothing is
    # said about it being disabled.
    assert "disabled" not in str(captured["messages"][0]["content"])


def test_availability_copy_matches_a_disabled_frozen_registry(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Web research off (the default): the schema is absent, the budget charges only what is
    # sent, and the system prompt says the capability is disabled - all from one snapshot.
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Find public sources", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    registry = captured["registry"]
    budget = captured["context_budget"]
    assert "search_web" not in registry
    assert tools.schema_tokens(tools.tool_schemas(registry)) == budget.tool_tokens
    assert "disabled" in str(captured["messages"][0]["content"])


def test_web_enabled_at_planning_then_disabled_before_execution_fails_closed(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Grant on when the turn is planned, revoked before the tool runs. The schema was frozen
    # on, so the tool is offered and budgeted; dispatch-time reauthorization reads the live
    # (now revoked) grant and refuses, so the revocation still fails closed.
    holder = {"web": True}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools,
        "get_writer_capabilities",
        lambda conn, cid: _caps(web=holder["web"]),
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        captured["registry"] = registry
        holder["web"] = False  # revoked after planning, before the tool is dispatched
        captured["dispatch"] = registry["search_web"].handler(query="a neutral public query")
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Find public sources", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    assert "search_web" in captured["registry"]  # frozen on: offered and budgeted
    dispatch = captured["dispatch"]
    assert dispatch.ok is False  # dispatch-time reauthorization fails closed
    assert "disabled" in str(dispatch.error)


def test_web_disabled_at_planning_then_enabled_waits_for_the_next_turn(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Grant off when planned, enabled before the loop runs. The schema selection was frozen
    # off, so the tool is not offered this turn - a newly enabled grant waits, rather than
    # letting the loop run a tool the preflight never charged for.
    holder = {"web": False}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools,
        "get_writer_capabilities",
        lambda conn, cid: _caps(web=holder["web"]),
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        captured["registry"] = kwargs["registry"]
        captured["messages"] = args[3]
        holder["web"] = True  # enabled after planning
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Find public sources", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    assert "search_web" not in captured["registry"]
    assert "disabled" in str(captured["messages"][0]["content"])


def test_workspace_grant_added_after_planning_waits_for_the_next_turn(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # No workspace read grant when planned; added before the loop runs. The code profile's
    # workspace schemas were frozen out, so they are not offered this turn.
    holder: dict[str, object] = {"ws": _fake_workspace(tmp_path, read=False)}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: holder["ws"],
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        captured["registry"] = kwargs["registry"]
        holder["ws"] = _fake_workspace(tmp_path, read=True)  # granted after planning
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Read the repository", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert "read_workspace_file" not in captured["registry"]


def test_workspace_grant_removed_after_planning_fails_closed(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # Read grant present when planned, revoked before the tool runs. The schema was frozen
    # on and budgeted; dispatch-time reauthorization reads the live (now revoked) grant and
    # refuses, so the revocation fails closed.
    holder: dict[str, object] = {"ws": _fake_workspace(tmp_path, read=True)}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: holder["ws"],
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        captured["registry"] = registry
        holder["ws"] = _fake_workspace(tmp_path, read=False)  # revoked after planning
        captured["dispatch"] = registry["read_workspace_file"].handler(relative_path="notes.txt")
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Read the repository", "profile": "code"},
    )

    assert response.status_code == 200, response.text
    assert "read_workspace_file" in captured["registry"]  # frozen on: offered and budgeted
    dispatch = captured["dispatch"]
    assert dispatch.ok is False  # dispatch-time reauthorization fails closed
    assert "disabled" in str(dispatch.error)


def test_a_failure_building_the_runtime_registry_persists_no_user_turn(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The executable registry is constructed inside the read-only preflight, before the title,
    # the user message, class recency, or any ledger is touched. A failure to build it
    # therefore cannot leave an orphaned user turn behind.
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = _spy_loop(monkeypatch)
    original = routes_agent_chat.agent_tools.build_agent_registry

    def maybe_raise(*args: object, **kwargs: object):  # noqa: ANN002, ANN003, ANN202
        # The probe build carries an empty private context; the executable build carries the
        # student's own words. Fail only the executable build.
        if kwargs.get("private_context"):
            raise RuntimeError("registry construction failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", maybe_raise)

    with pytest.raises(RuntimeError):
        client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "Explain the repository", "profile": "code"},
        )

    assert calls == []
    assert sessions.list_messages(db, session_id) == []
    assert sessions.get_session(db, session_id)["title"] is None


# Contextual agent profile (PLA-401): the student never names a profile. The contextual
# turn plans across research, workspace, and command work on its own, and asks for the
# just-in-time access it needs instead of presenting a grant dashboard.


def test_the_contextual_turn_defaults_to_the_agent_profile(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A POST without a profile runs under the contextual agent: the attempt records
    # `agent`, and the loop receives the union registry of the snapshot's admitted
    # families rather than one of the legacy isolated profiles.
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Read the starter project and explain how the pieces fit together"},
    )

    assert response.status_code == 200, response.text
    registry = captured["registry"]
    # No workspace is attached and web research is off: the agent offers the plain
    # reasoning tools and exactly one actionable request - attach a folder.
    assert "request_workspace_access" in registry
    assert "read_workspace_file" not in registry
    assert "search_web" not in registry
    row = db.execute(
        "select profile from agent_turn_attempts where session_id = ? order by id desc limit 1",
        (session_id,),
    ).fetchone()
    assert row is not None and row["profile"] == "agent"


def test_the_agent_registry_unions_the_families_the_snapshot_admits(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # Web on, workspace attached with every grant: the contextual registry is the union of
    # the admitted families, and no access request is offered - everything the snapshot
    # admits is already held, and the student never sees a grant dashboard.
    _enable_web_research(db)
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: _fake_workspace(tmp_path, read=True, change=True, commands=True),
    )
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Run the tests"},
    )

    assert response.status_code == 200, response.text
    registry = captured["registry"]
    for name in (
        "search_web",
        "read_workspace_file",
        "create_workspace_change",
        "create_command_request",
    ):
        assert name in registry
    assert "request_workspace_access" not in registry  # nothing is missing


def test_an_access_request_is_asked_once_per_scope_per_turn(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # Attached workspace with no grants: read is missing, so the request card exists. The
    # model asks once and is told the student decides; a second ask in the same turn is
    # refused as a repeat, so the student sees one card, not a pile.
    holder = {"ws": _fake_workspace(tmp_path, read=False)}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: holder["ws"],
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        captured["registry"] = registry
        first = registry["request_workspace_access"].handler(
            scope="read", reason="read the project files"
        )
        second = registry["request_workspace_access"].handler(
            scope="read", reason="read the project files"
        )
        captured["first"], captured["second"] = first, second
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Read the project"},
    )

    assert response.status_code == 200, response.text
    assert captured["first"].ok is True  # the student is asked once
    assert captured["second"].ok is False  # a repeat in the same turn is refused
    assert "already requested" in str(captured["second"].error)
    # One run-local activity event per scope, so the conversation shows one card.
    events = [e for e in response.json()["activity"] if e["target_kind"] == "capability_request"]
    assert len(events) == 1
    # The task-specific reason survives into the durable audit summary: the card renders
    # it verbatim, so the student sees why this task needs the access, not a status line.
    row = db.execute(
        "select result_summary_json from tool_audit_events where target_kind = ? "
        "order by rowid desc limit 1",
        ("capability_request",),
    ).fetchone()
    assert json.loads(row[0]) == {"scope": "read", "reason": "read the project files"}


def test_a_deferred_scope_is_not_resent_while_the_dismissal_is_active(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # "Not now" has a bounded, server-side lifetime. The model asks once and the student
    # defers; the model's repeat ask in a later turn is reported as deferred (no new
    # request event, no nag card) and told to proceed. Once the dismissal's window lapses,
    # the same scope is askable again.
    holder = {"ws": _fake_workspace(tmp_path, read=False)}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: holder["ws"],
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        captured.setdefault("asks", []).append(
            registry["request_workspace_access"].handler(
                scope="read", reason="read the project files"
            )
        )
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])
    chat_url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"

    first = client.post(chat_url, json={"content": "Read the project"})
    assert first.status_code == 200, first.text
    assert captured["asks"][0].ok is True
    assert captured["asks"][0].value["requested"] is True

    # The student defers the request in the conversation. The endpoint's own contract
    # lives in test_api_agent.py; here the deferral just needs to exist for the tool.
    agent_store.dismiss_workspace_access(db, class_id, session_id, "read")

    second = client.post(chat_url, json={"content": "Please read the project"})
    assert second.status_code == 200, second.text
    deferred = captured["asks"][1]
    assert deferred.ok is True
    assert deferred.value["requested"] is False
    assert deferred.value["deferred"] is True
    # The deferred ask creates no second request card while the deferral is active.
    events = [e for e in second.json()["activity"] if e["target_kind"] == "capability_request"]
    assert events == []
    deferrals = [e for e in second.json()["activity"] if e["target_kind"] == "access_deferral"]
    assert len(deferrals) == 1

    # Once the bounded window lapses, the scope is askable again: a fresh request is
    # recorded and the card may resurface.
    db.execute(
        "update agent_access_dismissals set dismissed_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour') "
        "where class_id = ? and session_id = ?",
        (class_id, session_id),
    )
    db.commit()
    third = client.post(chat_url, json={"content": "Read the project now"})
    assert third.status_code == 200, third.text
    assert captured["asks"][2].value["requested"] is True
    events = [e for e in third.json()["activity"] if e["target_kind"] == "capability_request"]
    assert len(events) == 1


def test_a_scope_granted_mid_turn_is_available_not_requested(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    # The request was frozen into this turn's registry by the snapshot (no read grant). If
    # the student grants read before the model asks, the live re-read reports the scope as
    # already available instead of asking for it twice.
    holder = {"ws": _fake_workspace(tmp_path, read=False)}
    monkeypatch.setattr(
        routes_agent_chat.agent_tools.agent_store,
        "get_workspace_for_class",
        lambda conn, cid: holder["ws"],
    )
    captured: dict[str, object] = {}

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        captured["registry"] = registry
        holder["ws"] = _fake_workspace(tmp_path, read=True)  # granted mid-turn
        captured["dispatch"] = registry["request_workspace_access"].handler(
            scope="read", reason="read the project files"
        )
        return tools.ToolLoopResult(content="done")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)
    session_id = int(sessions.create_session(db, class_id)["id"])

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Read the project"},
    )

    assert response.status_code == 200, response.text
    dispatch = captured["dispatch"]
    assert dispatch.ok is False
    assert "already available" in str(dispatch.error)


def test_legacy_profiles_do_not_gain_the_access_request_tool(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The isolated legacy profiles keep their exact registries: only the contextual agent
    # asks for access just-in-time, so nothing about the old surfaces changes shape.
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Research this", "profile": "research"},
    )

    assert response.status_code == 200, response.text
    assert "request_workspace_access" not in captured["registry"]


def test_the_agent_prompt_keeps_the_conversations_guide_show_contract(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent turn rides the conversation's Guide/Show contract inherited from the
    # shared tutoring prompt (llm_prompts.mode_contract) - not a restatement of it - so
    # the mode toggle keeps its meaning in agent work too and the two surfaces cannot
    # drift: the agent prompt must contain the shared contract for the session's mode.
    session_id = int(sessions.create_session(db, class_id)["id"])  # guide by default
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Help me understand this"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["messages"][0]["content"])
    assert llm_prompts.mode_contract("guide") in prompt

    sessions.set_session_mode(db, session_id, "show")
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Show me the working"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["messages"][0]["content"])
    assert llm_prompts.mode_contract("show") in prompt
    # Only one contract rides the turn: the guide contract does not linger in show work.
    assert llm_prompts.mode_contract("guide") not in prompt


# ---------------------------------------------------------------------------
# Final-parity regressions: full tutor system contract, private-context guard,
# scope persistence through retry/regenerate, and operation IDs.
# ---------------------------------------------------------------------------


def _facts(db: sqlite3.Connection, class_id: int) -> None:
    """One active class fact, one active user fact, and one unconfirmed proposal."""
    db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed) "
        "values (?, 'note', 'style', 'prefers visual proofs', 'high', 1)",
        (class_id,),
    )
    db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed) "
        "values (null, 'note', 'goal', 'audits changes before merging', 'high', 1)"
    )
    db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed) "
        "values (?, 'note', 'rumor', 'unconfirmed scheduling detail', 'low', 0)",
        (class_id,),
    )
    db.commit()


def test_the_agent_turn_builds_on_the_full_tutor_system_prompt(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The contextual agent turn rides the FULL tutor system prompt - base rules, the mode
    # contract, and the active facts - with only the capability layer added on top. Active
    # class and user facts enter; an unconfirmed proposal does not.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _facts(db, class_id)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Help me"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["messages"][0]["content"])
    assert "prefers visual proofs" in prompt
    assert "audits changes before merging" in prompt
    # The unconfirmed, low-confidence proposal is not active material.
    assert "unconfirmed scheduling detail" not in prompt


def test_the_guide_show_contract_appears_exactly_once_in_the_agent_prompt(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guide/Show is owned by the shared tutoring prompt. The agent layer adds capability
    # wording, never a second restatement of the contract: it appears exactly once.
    session_id = int(sessions.create_session(db, class_id)["id"])
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Help me", "mode": "guide"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["messages"][0]["content"])
    assert prompt.count(llm_prompts.mode_contract("guide")) == 1
    assert llm_prompts.mode_contract("show") not in prompt


def test_a_retrieved_chunk_seeds_the_web_query_guard_before_the_network(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A turn that retrieves private material must guard a later search_web against it,
    # refusing before the network is touched. The retrieved chunk enters the run-local
    # ledger at planning; the guard reads the ledger at dispatch.
    session_id = int(sessions.create_session(db, class_id)["id"])
    app_settings.update_settings_row(db, {"allow_web_research": 1})
    chunk_text = (
        "The Meridian ledger holds four thousand one hundred and twelve unposted "
        "entries for the autumn close."
    )
    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=7,
        content=chunk_text,
        token_count=16,
        page_number=1,
        section_title=None,
        section_path=None,
        section_number=None,
        problem_number=None,
        part_index=None,
        filename="notes.md",
        similarity=0.9,
        score=0.9,
    )

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        return RetrievalResult(chunks=[chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    network_calls: list[str] = []

    def recording_search(query: str, **kwargs: object) -> list[dict[str, str]]:
        network_calls.append(query)
        return []

    monkeypatch.setattr(web_research, "search_web", recording_search)

    async def tool_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        # The model asks for exactly the private material it just saw retrieved.
        outcome = registry["search_web"].handler(
            query="the meridian ledger holds unposted entries for the autumn close"
        )
        assert outcome.ok is False
        return tools.ToolLoopResult(content="Search refused by policy.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", tool_loop)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Summarize the ledger status"},
    )

    assert response.status_code == 200, response.text
    # The overlap was caught by the guard, never by the network.
    assert network_calls == []


def test_a_workspace_read_mid_turn_seeds_the_web_query_guard(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Content a workspace read surfaces mid-turn is private from that point on: a search_web
    # later in the same turn that repeats it is refused before the network, even though the
    # material was not present when the turn was planned.
    session_id = int(sessions.create_session(db, class_id)["id"])
    app_settings.update_settings_row(db, {"allow_web_research": 1})
    root = tmp_path / "repo"
    root.mkdir()
    file_text = (
        "Internal note: the Meridian ledger holds four thousand one hundred and twelve "
        "unposted entries for the autumn close."
    )
    (root / "ledger.md").write_text(file_text, encoding="utf-8")
    agent_store.attach_workspace(db, class_id, root_path=str(root))
    agent_store.update_workspace_grants(db, class_id, read_enabled=True)

    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id_, query, budget_tokens, document_id=None: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )

    network_calls: list[str] = []

    def recording_search(query: str, **kwargs: object) -> list[dict[str, str]]:
        network_calls.append(query)
        return []

    monkeypatch.setattr(web_research, "search_web", recording_search)

    async def tool_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        registry = kwargs["registry"]
        read = registry["read_workspace_file"].handler(relative_path="ledger.md")
        assert read.ok is True
        # A later search repeats what the read just surfaced.
        outcome = registry["search_web"].handler(
            query="the meridian ledger holds unposted entries for the autumn close"
        )
        assert outcome.ok is False
        return tools.ToolLoopResult(content="Search refused by policy.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", tool_loop)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Check the ledger and tell me its status"},
    )

    assert response.status_code == 200, response.text
    assert network_calls == []


def test_the_guarded_context_is_never_persisted(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The ledger is run-local: the private material it guards with is never written to the
    # database. A refused search leaves an audit row naming the refusal, but the corpus it
    # compared against - document text, workspace file contents - appears nowhere durable.
    session_id = int(sessions.create_session(db, class_id)["id"])
    app_settings.update_settings_row(db, {"allow_web_research": 1})
    root = tmp_path / "repo"
    root.mkdir()
    file_text = (
        "Internal note: the Meridian ledger holds four thousand one hundred and twelve "
        "unposted entries for the autumn close."
    )
    (root / "ledger.md").write_text(file_text, encoding="utf-8")
    agent_store.attach_workspace(db, class_id, root_path=str(root))
    agent_store.update_workspace_grants(db, class_id, read_enabled=True)

    registry, _ = agent_tools.build_agent_registry(db, class_id, session_id, "agent")
    registry["read_workspace_file"].handler(relative_path="ledger.md")
    result = registry["search_web"].handler(
        query="the meridian ledger holds unposted entries for the autumn close"
    )

    assert result.ok is False
    # Nothing durable carries the private corpus.
    blob = " ".join(
        str(row["arguments_json"])
        for row in db.execute("select arguments_json from tool_audit_events")
    )
    assert "four thousand one hundred and twelve" not in blob
    for table in ("messages", "agent_turn_attempts", "tool_audit_events"):
        rows = db.execute(f"select * from {table}").fetchall()  # noqa: S608 - fixed table names
        for row in rows:
            for key in row.keys():  # noqa: SIM118 - sqlite3.Row keys
                value = row[key]
                if isinstance(value, (str, bytes)):
                    assert "four thousand one hundred and twelve" not in str(value)


def test_a_retry_reuses_the_scope_the_turn_was_asked_under(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retry re-answers the same logical turn: the source scope (document filter) and the
    # Guide/Show mode are persisted on the attempt and win over the (absent) body.
    session_id = int(sessions.create_session(db, class_id)["id"])
    seen_documents: list[int | None] = []

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        seen_documents.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    # A send scoped to a document, in show mode, that fails.
    async def failing_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", failing_loop)
    refused = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain the close", "mode": "show", "document_id": 7},
    )
    assert refused.status_code == 504, refused.text

    # The retry runs the same scope: the document filter and the mode the turn was asked with.
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Answered."))
    retry = client.post(f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry")
    assert retry.status_code == 200, retry.text
    assert seen_documents == [7, 7]
    prompt = str(captured["messages"][0]["content"])
    assert llm_prompts.mode_contract("show") in prompt
    assert llm_prompts.mode_contract("guide") not in prompt


def test_a_regeneration_uses_the_current_selection(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manual regeneration carries the CURRENT Guide/Show selection and source scope (like
    # the tutor's) and uses them; it re-answers and supersedes, leaving one reply.
    session_id = int(sessions.create_session(db, class_id)["id"])
    seen_documents: list[int | None] = []

    def fake_retrieve(  # noqa: ANN001
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        seen_documents.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)

    _stub_loop(monkeypatch, tools.ToolLoopResult(content="First reply."))
    r1 = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain the close", "mode": "guide"},
    )
    assert r1.status_code == 200, r1.text

    # The student now regenerates in show mode, scoped to a document.
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Second reply."))
    r2 = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/regenerate",
        json={"mode": "show", "document_id": 7},
    )
    assert r2.status_code == 200, r2.text
    assert seen_documents == [None, 7]
    prompt = str(captured["messages"][0]["content"])
    assert llm_prompts.mode_contract("show") in prompt
    assert llm_prompts.mode_contract("guide") not in prompt
    # One reply: the superseded one is removed the moment the new one commits.
    messages = sessions.list_messages(db, session_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Second reply."


def test_an_operation_id_replays_the_completed_turn_without_rerunning(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PLA-313: a completed send re-submitted with the same operation_id replays the stored
    # reply - zero model/tool work, exactly one durable user message and one reply.
    session_id = int(sessions.create_session(db, class_id)["id"])
    calls = 0

    async def counting_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        nonlocal calls
        calls += 1
        return tools.ToolLoopResult(content="Once.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", counting_loop)
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id_, query, budget_tokens, document_id=None: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )
    url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"

    r1 = client.post(url, json={"content": "Explain", "operation_id": "op-1"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(url, json={"content": "Explain", "operation_id": "op-1"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["message_id"] == r1.json()["message_id"]
    assert calls == 1, "the replay must not re-run the model"

    user_msgs = db.execute(
        "select id from messages where session_id = ? and role = 'user'", (session_id,)
    ).fetchall()
    asst_msgs = db.execute(
        "select id from messages where session_id = ? and role = 'assistant'", (session_id,)
    ).fetchall()
    assert len(user_msgs) == 1
    assert len(asst_msgs) == 1


def test_an_operation_id_mismatch_is_structured(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reusing an operation_id for a genuinely different request is a client bug, refused with
    # a structured code the client can tell apart from the ordinary conversation-busy 409.
    session_id = int(sessions.create_session(db, class_id)["id"])

    async def counting_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        return tools.ToolLoopResult(content="Answer.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", counting_loop)
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id_, query, budget_tokens, document_id=None: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )
    url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"

    r1 = client.post(url, json={"content": "question A", "operation_id": "op-code"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(url, json={"content": "question B", "operation_id": "op-code"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "operation_id_mismatch"


def test_a_failed_operation_reruns_without_a_duplicate_message(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A send whose only attempt failed, re-submitted with the same operation_id and content,
    # re-runs the turn under a fresh attempt but keeps the single durable user message.
    session_id = int(sessions.create_session(db, class_id)["id"])
    state = {"count": 0}

    async def flaky_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        state["count"] += 1
        if state["count"] == 1:
            return tools.ToolLoopResult(content="", stopped=tools.TIMEOUT, detail="slow")
        return tools.ToolLoopResult(content="Recovered.")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", flaky_loop)
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id_, query, budget_tokens, document_id=None: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )
    url = f"/api/classes/{class_id}/sessions/{session_id}/agent-chat"

    r1 = client.post(url, json={"content": "Explain", "operation_id": "op-fail"})
    assert r1.status_code == 504, r1.text
    r2 = client.post(url, json={"content": "Explain", "operation_id": "op-fail"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["content"] == "Recovered."
    assert state["count"] == 2
    user_msgs = db.execute(
        "select id from messages where session_id = ? and role = 'user'", (session_id,)
    ).fetchall()
    assert len(user_msgs) == 1, "the re-run must not append a duplicate question"


async def test_the_stop_endpoint_cancels_the_inflight_turn(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The non-streaming handler cannot see its own client disconnect, so the UI's Stop is
    # explicit: it cancels the in-flight task, which settles the durable attempt as stopped
    # and releases the per-session claim - no hidden side effect, the turn is retryable. A
    # session with nothing in flight is simply "nothing to stop".
    session_id = int(sessions.create_session(db, class_id)["id"])
    db.execute(
        "update settings set endpoint_url = 'http://127.0.0.1:8080/v1', tools_supported = 1 "
        "where id = 1"
    )
    db.commit()
    started = asyncio.Event()

    async def blocking_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        started.set()
        await asyncio.sleep(30)
        return tools.ToolLoopResult(content="too late")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", blocking_loop)
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda conn, class_id_, query, budget_tokens, document_id=None: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )

    conn_a, conn_b = connect(), connect()
    try:
        turn = asyncio.ensure_future(
            routes_agent_chat.send_agent_chat(
                class_id,
                session_id,
                routes_agent_chat.AgentChatRequest(content="Take a while"),
                conn_a,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5.0)

        # Explicit stop on the in-flight turn.
        stopped = await routes_agent_chat.stop_agent_chat(class_id, session_id, conn_b)
        assert stopped == {"stopped": True}

        # The stopped turn settles: attempt stopped, claim released, no hidden work left.
        # The request itself completes normally with a bounded "stopped" body - it never
        # surfaces as a bare cancellation, because a cancelled request task would leave the
        # HTTP middleware with no response and log a 500.
        turn_result = await asyncio.wait_for(asyncio.shield(turn), timeout=5.0)
    finally:
        conn_a.close()
        conn_b.close()

    assert isinstance(turn_result, JSONResponse)
    assert json.loads(turn_result.body)["stopped"] == "stopped"
    assert sessions.active_turn(session_id) is None
    row = db.execute("select state from agent_turn_attempts order by id desc limit 1").fetchone()
    assert row["state"] == "stopped"

    # Nothing in flight now: a further stop is a no-op, not an error.
    conn_c = connect()
    try:
        again = await routes_agent_chat.stop_agent_chat(class_id, session_id, conn_c)
    finally:
        conn_c.close()
    assert again == {"stopped": False}


# ---------------------------------------------------------------------------
# PLA-401 final pass: the tool-less surface, the attempt lifecycle, and the
# persisted-scope sentinel.
# ---------------------------------------------------------------------------


def _settings_verdict(db: sqlite3.Connection) -> tuple[int | None, str | None]:
    row = db.execute("select tools_supported, tools_message from settings where id = 1").fetchone()
    return row["tools_supported"], row["tools_message"]


def test_a_known_tool_incompatible_endpoint_answers_basic_chat_tool_less(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1, known-false: a settings row measured `tools_supported = 0` takes the
    tool-less path at once - one plain completion carrying the full tutor contract, the
    loop never running, no tool schemas charged or sent, and the measured verdict left
    untouched by the turn."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    db.execute("update settings set tools_supported = 0 where id = 1")
    db.commit()

    loop_calls: list[int] = []

    async def loop_never(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        loop_calls.append(1)
        return tools.ToolLoopResult(content="unreachable")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", loop_never)
    sent: dict[str, object] = {}

    async def fake_complete(*args: object, **kwargs: object) -> str:
        sent["messages"] = args[3]
        return "Convolution combines each input with a kernel."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution", "profile": "agent"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"].startswith("Convolution")
    # The tool-less surface: the loop never ran and no tool schema rode the request.
    assert loop_calls == []
    system = str(sent["messages"][0]["content"])
    # The full tutor contract rides the plain completion: the mode contract is present,
    # and the one sentence that says the agent work is unavailable replaces the agent
    # capability layer (which describes tools this turn never sends).
    assert "Mode: Guide." in system
    assert routes_agent_chat._TOOLLESS_AGENT_NOTE in system
    assert routes_agent_chat._SYSTEM_PROMPTS["agent"] not in system
    # One durable attempt, completed with the reply; no tool activity of any kind.
    rows = db.execute(
        "select state, assistant_message_id, mode from agent_turn_attempts order by id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "completed"
    assert rows[0]["assistant_message_id"] is not None
    assert rows[0]["mode"] == "guide"
    assert response.json()["activity"] == []
    # The verdict is a measured fact: the turn neither clears nor changes it.
    assert _settings_verdict(db) == (0, None)


def test_an_unknown_endpoints_first_tools_refusal_falls_back_and_remembers_the_verdict(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1, unknown: an unproven endpoint refuses the turn's first tools request. The
    same logical turn continues tool-less - no duplicate user message, one reply - the
    abandoned tool pass settles as a stopped attempt beside the completed one, and the
    verdict is remembered on the settings row so the next turn never asks the question
    again."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    db.execute("update settings set tools_supported = NULL, tools_message = NULL where id = 1")
    db.commit()

    completions: list[list[dict[str, object]]] = []

    async def fake_complete(*args: object, **kwargs: object) -> str:
        completions.append(args[3])
        return "The answer, without tools."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)
    loop_calls: list[dict[str, object]] = []

    async def fake_loop(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        loop_calls.append({"messages": args[3], "registry": kwargs["registry"]})
        return tools.ToolLoopResult(
            content="", calls=(), stopped=tools.NO_TOOL_SUPPORT, detail="tools refused"
        )

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", fake_loop)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution", "profile": "agent", "operation_id": "op-1"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "The answer, without tools."
    # One question, one answer: the fallback continued the turn, it did not re-send it.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user", "assistant"]
    # The loop made exactly one tools request (the refused one); the continuation was a
    # plain completion, not a second round.
    assert len(loop_calls) == 1
    assert len(completions) == 1
    # The two attempts are one lineage: the abandoned tool pass stopped as no_tool_support
    # (carrying the operation ID, no reply), the continuation completed with the reply.
    attempts = db.execute(
        "select state, stopped_reason, operation_id, assistant_message_id "
        "from agent_turn_attempts order by id"
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["state"] == "stopped"
    assert attempts[0]["stopped_reason"] == tools.NO_TOOL_SUPPORT
    assert attempts[0]["operation_id"] == "op-1"
    assert attempts[0]["assistant_message_id"] is None
    assert attempts[1]["state"] == "completed"
    assert attempts[1]["operation_id"] is None
    assert attempts[1]["assistant_message_id"] is not None
    # The tool-less continuation carried the full tutor contract, not the agent layer.
    system = str(completions[0][0]["content"])
    assert "Mode: Guide." in system
    assert routes_agent_chat._TOOLLESS_AGENT_NOTE in system
    assert routes_agent_chat._SYSTEM_PROMPTS["agent"] not in system
    # The verdict is remembered for the next turn and the settings screen.
    assert _settings_verdict(db) == (0, routes_agent_chat._NO_TOOL_SUPPORT_VERDICT_MESSAGE)

    # The next turn is known-tool-less: no second tools request is ever made.
    again = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "And what about a conv layer?", "profile": "agent"},
    )
    assert again.status_code == 200, again.text
    assert len(loop_calls) == 1
    assert len(completions) == 2


def test_a_toolless_turn_carries_retrieval_and_facts_like_the_tool_turn(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1: the tool-less answer is the same tutoring surface, not a degraded one -
    retrieved chunks and active facts ride its prompt exactly as they ride a tool turn,
    so the endpoint that cannot run tools still answers grounded on the class material."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    _facts(db, class_id)
    db.execute("update settings set tools_supported = 0 where id = 1")
    db.commit()

    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=7,
        content="Convolution combines two signals.",
        token_count=6,
        page_number=1,
        section_title="Convolution",
        section_path="ch2/convolution",
        section_number="2.1",
        problem_number=None,
        part_index=None,
        filename="signals.pdf",
        similarity=0.9,
        score=0.9,
    )
    retrieved: list[int | None] = []

    def fake_retrieve(
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        retrieved.append(document_id)
        return RetrievalResult(chunks=[chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", fake_retrieve)
    loop_calls: list[int] = []

    async def loop_never(*args: object, **kwargs: object) -> tools.ToolLoopResult:
        loop_calls.append(1)
        return tools.ToolLoopResult(content="unreachable")

    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", loop_never)
    sent: dict[str, object] = {}

    async def fake_complete(*args: object, **kwargs: object) -> str:
        sent["messages"] = args[3]
        return "Grounded answer."

    monkeypatch.setattr(routes_agent_chat.llm_client, "complete", fake_complete)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Explain convolution", "profile": "agent"},
    )

    assert response.status_code == 200, response.text
    assert loop_calls == []
    # The unscoped turn retrieves class-wide, and the retrieval and the facts are in the
    # plain completion's prompt.
    assert retrieved == [None]
    system = str(sent["messages"][0]["content"])
    assert "Convolution combines two signals." in system
    assert "signals.pdf" in system
    assert "prefers visual proofs" in system
    assert "Mode: Guide." in system
    assert routes_agent_chat._TOOLLESS_AGENT_NOTE in system


def _client_without_server_exceptions(db: sqlite3.Connection) -> TestClient:
    """A client that reports server-side exceptions as 500 bodies instead of raising,
    so an injected preflight failure is asserted on its response and its aftermath.
    Configures the endpoint the way the `client` fixture does, so a refused turn is
    refused by the injected seam, not by a missing endpoint."""
    db.execute("update settings set endpoint_url = 'http://127.0.0.1:8080/v1' where id = 1")
    db.commit()
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

    app.include_router(routes_agent_chat.router)
    app.dependency_overrides[get_db] = _request_db
    return TestClient(app, raise_server_exceptions=False)


def test_a_fresh_send_rejected_by_a_failed_preflight_persists_nothing_and_moves_no_mode(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4: a fresh send whose preflight fails (injected retrieval failure) persists
    nothing and changes nothing: no user message, no attempt to orphan as RUNNING, no
    session-mode mutation, no title, no class touch. The plan runs before the first
    durable write, so a refused turn cannot leave the conversation moved."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    monkeypatch.setattr(
        routes_agent_chat,
        "retrieve",
        lambda *args: (_ for _ in ()).throw(RuntimeError("injected retrieval failure")),
    )
    touched: list[int] = []
    monkeypatch.setattr(routes_agent_chat, "touch_class", lambda conn, cid: touched.append(cid))
    client = _client_without_server_exceptions(db)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A question", "profile": "agent", "mode": "show"},
    )

    assert response.status_code == 500
    # Nothing of the turn landed, and the mode toggle the body carried was never written.
    assert sessions.list_messages(db, session_id) == []
    assert db.execute("select count(*) from agent_turn_attempts").fetchone()[0] == 0
    assert sessions.get_session(db, session_id)["mode"] == "guide"
    assert sessions.get_session(db, session_id)["title"] is None
    assert touched == []
    assert sessions.active_turn(session_id) is None


def test_a_fit_refused_fresh_send_moves_no_mode_and_persists_nothing(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4, the fit gate: a window that cannot host even the tool-less surface refuses
    with the bounded message, and - the regression this reorders - the session's mode is
    still the one the turn started with: the mode is written only once the preflight
    succeeded."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    db.execute("update settings set context_window = 512 where id = 1")
    db.commit()
    client = _client_without_server_exceptions(db)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "word " * 2000, "profile": "agent", "mode": "show"},
    )

    assert response.status_code == 400
    assert "context window" in str(response.json()["detail"])
    assert sessions.list_messages(db, session_id) == []
    assert db.execute("select count(*) from agent_turn_attempts").fetchone()[0] == 0
    # The turn was refused, not started: the toggle stays where it was.
    assert sessions.get_session(db, session_id)["mode"] == "guide"
    assert sessions.active_turn(session_id) is None


def test_a_retry_whose_preflight_fails_leaves_no_attempt_and_no_host_effect(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4, retry: a retry plans before it persists anything. An injected registry
    failure on the plan leaves the original failed attempt exactly as it was - no new
    RUNNING attempt beside it, no reply, no mode change, the claim released."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    client = _client_without_server_exceptions(db)
    # One failed attempt to retry.
    stub = tools.ToolLoopResult(
        content="",
        calls=(),
        stopped=tools.UPSTREAM_FAILED,
        detail="The tutor endpoint could not be reached.",
    )
    monkeypatch.setattr(routes_agent_chat, "run_tool_loop", lambda *a, **k: _stub_result(stub))
    first = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A question", "profile": "agent"},
    )
    assert first.status_code == 502
    attempts_before = db.execute("select count(*) from agent_turn_attempts").fetchone()[0]
    mode_before = sessions.get_session(db, session_id)["mode"]

    def broken_registry(*args: object, **kwargs: object):
        raise RuntimeError("injected registry failure")

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", broken_registry)
    retry = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry",
        json={"mode": "show"},
    )

    assert retry.status_code == 500
    # No new attempt was created by the failed retry plan, and none is left reading
    # RUNNING.
    attempts_after = db.execute("select state from agent_turn_attempts order by id").fetchall()
    assert len(attempts_after) == attempts_before
    assert all(row["state"] != "running" for row in attempts_after)
    # The conversation is untouched by the failed retry: no reply, mode where it was.
    assert [m["role"] for m in sessions.list_messages(db, session_id)] == ["user"]
    assert sessions.get_session(db, session_id)["mode"] == mode_before
    assert sessions.active_turn(session_id) is None


async def _stub_result(result: tools.ToolLoopResult) -> tools.ToolLoopResult:
    return result


def test_a_regenerate_whose_preflight_fails_preserves_the_existing_reply(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4, regeneration: a regeneration that fails its preflight leaves the reply it
    was about to replace exactly where it was - the supersede only happens when the new
    reply commits - and does not create an attempt, move the mode, or hold the claim."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    client = _client_without_server_exceptions(db)
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(tools.ToolLoopResult(content="The original answer.")),
    )
    first = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A question", "profile": "agent"},
    )
    assert first.status_code == 200
    assert first.json()["content"] == "The original answer."
    reply_id = first.json()["message_id"]
    attempts_before = db.execute("select count(*) from agent_turn_attempts").fetchone()[0]
    mode_before = sessions.get_session(db, session_id)["mode"]

    def broken_registry(*args: object, **kwargs: object):
        raise RuntimeError("injected registry failure")

    monkeypatch.setattr(routes_agent_chat.agent_tools, "build_agent_registry", broken_registry)
    regen = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/regenerate",
        json={"mode": "show"},
    )

    assert regen.status_code == 500
    # The reply the regeneration was about to supersede is still there, undisturbed.
    assert [m["id"] for m in sessions.list_messages(db, session_id)] == [
        1,
        reply_id,
    ]
    assert db.execute("select count(*) from agent_turn_attempts").fetchone()[0] == (attempts_before)
    assert sessions.get_session(db, session_id)["mode"] == mode_before
    assert sessions.active_turn(session_id) is None


def test_a_retry_of_an_all_material_turn_stays_class_wide_even_when_a_document_is_named(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: a modern attempt persists its scope with the `scope_persisted` flag, and a
    persisted `document_id` of NULL is the real value 'All material' - not an absence. A
    retry of a class-wide turn must retrieve class-wide even when its body names a
    document: the sentinel makes the stored NULL authoritative."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    retrieved: list[int | None] = []

    def spy_retrieve(
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        retrieved.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", spy_retrieve)
    # Turn one: All material (no document_id in the body).
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(tools.ToolLoopResult(content="First answer.")),
    )
    assert (
        client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "First", "profile": "agent"},
        ).status_code
        == 200
    )
    # Turn two: a failed attempt to retry, also All material.
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(
            tools.ToolLoopResult(
                content="",
                calls=(),
                stopped=tools.UPSTREAM_FAILED,
                detail="The tutor endpoint could not be reached.",
            )
        ),
    )
    assert (
        client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "Second", "profile": "agent"},
        ).status_code
        == 502
    )
    last_user_id = int(
        db.execute(
            "select max(id) from messages where session_id = ? and role = 'user'",
            (session_id,),
        ).fetchone()[0]
    )
    attempt = agent_attempts.latest_attempts_by_message(db, session_id)[last_user_id]
    assert attempt["scope_persisted"] == 1
    assert attempt["document_id"] is None

    # The retry names a document in its body - the malicious case. The persisted NULL
    # wins: retrieval runs class-wide.
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(tools.ToolLoopResult(content="Retried answer.")),
    )
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry",
        json={"document_id": 7},
    )
    assert response.status_code == 200, response.text
    assert retrieved[-1] is None


def test_a_legacy_attempt_scope_backstops_from_the_request(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5, legacy rows: an attempt created before the persisted-scope column (flag 0)
    does not own its scope, so a retry backstops from the request-provided scope - the
    pre-sentinel behavior - and a stored NULL there is an absence, not 'All material'."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    # Seed the user message and a legacy failed attempt directly, flag 0.
    user_id = sessions.add_message(db, session_id, "user", "A legacy question")
    db.execute(
        "insert into agent_turn_attempts "
        "(session_id, user_message_id, profile, state, mode, document_id, operation_id, "
        "scope_persisted) values (?, ?, 'agent', 'failed', 'guide', NULL, NULL, 0)",
        (session_id, user_id),
    )
    db.commit()

    retrieved: list[int | None] = []

    def spy_retrieve(
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        retrieved.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", spy_retrieve)
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(tools.ToolLoopResult(content="Legacy retry.")),
    )

    # The request names a document: with no sentinel, that backstop is the scope.
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry",
        json={"document_id": 7},
    )
    assert response.status_code == 200, response.text
    assert retrieved[-1] == 7

    # A second legacy row, this one with a stored document: a body-less retry backstops to
    # the stored value, the pre-sentinel behavior for rows that never carried the flag.
    user_id_2 = sessions.add_message(db, session_id, "user", "Another legacy question")
    db.execute(
        "insert into agent_turn_attempts "
        "(session_id, user_message_id, profile, state, mode, document_id, operation_id, "
        "scope_persisted) values (?, ?, 'agent', 'failed', 'guide', 3, NULL, 0)",
        (session_id, user_id_2),
    )
    db.commit()
    client.post(f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/retry")
    assert retrieved[-1] == 3


def test_a_regenerate_with_an_explicit_null_document_retrieves_class_wide(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2, server side: a regeneration body that carries an explicit `document_id:
    null` (the composer's 'All material') names the scope by property presence - the
    stored scope is ignored and retrieval runs class-wide, even for a turn that was
    asked under a document."""
    session_id = int(sessions.create_session(db, class_id)["id"])
    retrieved: list[int | None] = []

    def spy_retrieve(
        conn: object,
        class_id_: int,
        query: str,
        budget_tokens: int,
        document_id: int | None = None,
    ) -> RetrievalResult:
        retrieved.append(document_id)
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_agent_chat, "retrieve", spy_retrieve)
    monkeypatch.setattr(
        routes_agent_chat,
        "run_tool_loop",
        lambda *a, **k: _stub_result(tools.ToolLoopResult(content="Scoped answer.")),
    )
    # The original turn was asked under document 7.
    assert (
        client.post(
            f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
            json={"content": "Scoped question", "profile": "agent", "document_id": 7},
        ).status_code
        == 200
    )
    assert retrieved == [7]
    (attempt,) = agent_attempts.latest_attempts_by_message(db, session_id).values()
    assert attempt["document_id"] == 7

    # Manual regeneration to All material: explicit null in the body wins over the
    # stored 7.
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat/regenerate",
        json={"document_id": None},
    )
    assert response.status_code == 200, response.text
    assert retrieved[-1] is None
