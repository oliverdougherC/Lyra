"""Class-agent turns use one explicit profile and persist durable activity references."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat
from backend.api.routes_agent_chat import AgentTurnCost
from backend.core import app_settings, sessions
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError
from backend.core.writer_budgets import WriterCapabilities
from backend.llm import tools
from backend.rag.tokens import estimate_tokens
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
    # the tool schemas measurable before history is trimmed.
    assert seen[0] == ()
    assert seen[-1] == tuple(rendered) + ("Newest question",)


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


def test_a_512_token_window_cannot_host_the_tool_definitions(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The smallest window Settings permits. The tool schema alone outweighs it, so no agent
    # turn can fit: it is refused locally, with a bounded student-facing message, before any
    # request and before anything is stored.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 512)
    calls = _spy_loop(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Anything at all", "profile": "code"},
    )

    assert response.status_code == 400
    detail = str(response.json()["detail"])
    assert "context window" in detail
    # Bounded and privacy-safe: no endpoint, path, key, or transcript in the refusal.
    assert "127.0.0.1" not in detail and "http" not in detail
    assert calls == []
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


def test_a_20000_character_prompt_is_refused_at_the_default_window_but_fits_a_larger_one(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `AgentChatRequest.content` permits 20,000 characters - about 5,000 tokens. Beside the
    # generation reserve and the tool schema, that does not fit the default 8,192 window, so
    # it is refused; a 16,384 window has room, so the same message is answered. The refusal
    # is the current message being charged: without it, the fixed material fits the small
    # window with thousands of tokens to spare.
    prompt = "q" * 20_000
    fixed_without_question = (
        2048 + estimate_tokens(routes_agent_chat._SYSTEM_PROMPTS["code"]) + 1055
    )
    assert fixed_without_question < 8192  # the turn is refused only because the message is charged

    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    refused_calls = _spy_loop(monkeypatch)
    refused = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": prompt, "profile": "code"},
    )
    assert refused.status_code == 400
    assert refused_calls == []
    assert sessions.list_messages(db, session_id) == []

    _set_window(db, 16_384)
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="Long but answerable."))
    ok = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": prompt, "profile": "code"},
    )
    assert ok.status_code == 200, ok.text
    # The whole message survived into the prompt - it is never silently truncated.
    assert captured["messages"][-1] == {"role": "user", "content": prompt}


def test_oversized_tool_overhead_refuses_the_turn_before_any_effect(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent's mandatory non-trimmable context is its tool schema. When that overhead is
    # large enough to fill the window on its own, the turn is refused before persistence or
    # any request - the same fit check, driven by tool/workspace context rather than history.
    session_id = int(sessions.create_session(db, class_id)["id"])
    _set_window(db, 8192)
    monkeypatch.setattr(routes_agent_chat, "schema_tokens", lambda schemas: 10_000)
    calls = _spy_loop(monkeypatch)

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "A short question", "profile": "code"},
    )

    assert response.status_code == 400
    assert calls == []
    assert sessions.list_messages(db, session_id) == []


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
    # The agent turn rides the conversation's Guide/Show mode (Workstream A), so the mode
    # toggle keeps its meaning in agent work too.
    session_id = int(sessions.create_session(db, class_id)["id"])  # guide by default
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))

    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Help me understand this"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["messages"][0]["content"])
    assert "guide mode" in prompt

    sessions.set_session_mode(db, session_id, "show")
    captured = _stub_loop(monkeypatch, tools.ToolLoopResult(content="ok"))
    response = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/agent-chat",
        json={"content": "Show me the working"},
    )

    assert response.status_code == 200, response.text
    assert "show mode" in str(captured["messages"][0]["content"])
