"""The inline writer path keeps its prompt inside the configured context window (PLA-300).

`_open_write` budgets the `/drafts/{id}/write` prompt against the resolved
`TutorConfig.context_window`: the writing system prompt and the student's instruction are
mandatory, and so is any selected text (it is the operation's subject); optional heading,
nearby, retrieval, brief, and fact context is trimmed in a fixed priority order. An
impossible mandatory prompt is refused locally, before retrieval and before any upstream
call, and the path never touches the draft body.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.api import routes_drafts
from backend.api.routes_drafts import WriteRequest, _open_write
from backend.core import artifacts
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError
from backend.llm.tools import conversation_tokens
from backend.llm.turn_budget import TurnBudget
from backend.rag.retrieve import RetrievalResult

ENDPOINT = "http://127.0.0.1:8080/v1"


@pytest.fixture
def draft_id(db: sqlite3.Connection, class_id: int) -> int:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    artifacts.create_part(
        db, int(created["id"]), artifacts.DRAFT_BODY, 1, content="", status=artifacts.PART_COMPLETE
    )
    return int(created["id"])


def _use_window(monkeypatch: pytest.MonkeyPatch, window: int) -> None:
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            config=TutorConfig(ENDPOINT, None, "m", window),
            document_block=None,
            remote_ack=False,
        ),
    )


def _stub_retrieval(monkeypatch: pytest.MonkeyPatch, *, chars_per_budget: int = 4) -> list[int]:
    """Record the retrieval budget and return a chunk sized to it, as real retrieval does.

    Returns the list of budgets `_open_write` asked retrieval for, so a test can assert the
    fetch was sized to the room left rather than to the fixed cap.
    """
    budgets: list[int] = []

    class _Chunk:
        def __init__(self, content: str) -> None:
            self.content = content
            self.filename = "notes.md"
            self.page_number = None
            self.section_title = None
            self.section_path = None
            self.section_number = None
            self.problem_number = None

    def fake_retrieve(conn: object, class_id: int, query: str, budget: int) -> RetrievalResult:
        budgets.append(budget)
        body = "x" * (budget * chars_per_budget)
        return RetrievalResult(chunks=[_Chunk(body)], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(routes_drafts, "retrieve", fake_retrieve)
    return budgets


def _no_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routes_drafts,
        "retrieve",
        lambda conn, class_id, query, budget: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )


def _user_content(messages: list[dict[str, str]]) -> str:
    return messages[-1]["content"]


# --- The normal path still works ----------------------------------------------------


def test_a_normal_default_window_assembles_the_full_prompt(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_window(monkeypatch, 8192)
    _stub_retrieval(monkeypatch)
    monkeypatch.setattr(
        routes_drafts.briefs, "get_brief", lambda conn, aid: {"summary": "An essay"}
    )
    config, messages = _open_write(
        db,
        draft_id,
        WriteRequest(
            instruction="Write an introduction",
            heading="Introduction",
            selection=None,
            nearby="The paragraph that follows.",
        ),
    )
    assert config.context_window == 8192
    content = _user_content(messages)
    assert "Write an introduction" in content
    assert "Introduction" in content
    assert "The paragraph that follows." in content
    # The whole assembled prompt fits the window minus the reply reserve.
    assert conversation_tokens(messages) <= 8192


# --- Impossible mandatory input fails locally, before any upstream call --------------


def test_an_impossible_instruction_is_refused_before_retrieval(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_window(monkeypatch, 512)
    budgets = _stub_retrieval(monkeypatch)
    with pytest.raises(LyraError) as excinfo:
        _open_write(db, draft_id, WriteRequest(instruction="write this. " * 400))
    # Bounded, privacy-safe, actionable; no endpoint, path, or prompt content.
    assert "context window" in excinfo.value.message
    assert "http" not in excinfo.value.message and "127.0.0.1" not in excinfo.value.message
    # Refused before any retrieval ran.
    assert budgets == []


def test_a_large_selection_is_mandatory_and_fails_locally(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The selection is the subject of a "rewrite this" operation, so it is never silently
    # dropped: when it cannot fit, the turn is refused rather than sent without it.
    _use_window(monkeypatch, 1024)
    budgets = _stub_retrieval(monkeypatch)
    with pytest.raises(LyraError):
        _open_write(
            db,
            draft_id,
            WriteRequest(instruction="Tighten this", selection="word " * 3000),
        )
    assert budgets == []


# --- Optional context is trimmed; mandatory content is preserved ---------------------


def test_a_large_nearby_block_is_dropped_but_the_turn_still_runs(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_window(monkeypatch, 2048)
    _no_retrieval(monkeypatch)
    monkeypatch.setattr(routes_drafts.briefs, "get_brief", lambda conn, aid: None)
    huge_nearby = "surrounding sentence. " * 2000
    _config, messages = _open_write(
        db,
        draft_id,
        WriteRequest(
            instruction="Continue the argument",
            selection="the thesis sentence",
            nearby=huge_nearby,
        ),
    )
    content = _user_content(messages)
    # Mandatory content survives; the oversized optional block is dropped, not the turn.
    assert "Continue the argument" in content
    assert "the thesis sentence" in content
    assert "surrounding sentence." not in content
    assert conversation_tokens(messages) <= 2048


def test_optional_context_is_trimmed_lowest_priority_first(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Brief outranks facts. Sized so the brief fits beside the mandatory floor but the facts
    # block does not: facts is dropped, the higher-priority brief is kept.
    _no_retrieval(monkeypatch)
    monkeypatch.setattr(routes_drafts.briefs, "get_brief", lambda conn, aid: {"summary": "brief"})
    monkeypatch.setattr(routes_drafts, "select_active_facts", lambda conn, cid: [object()])
    monkeypatch.setattr(routes_drafts.prompts, "format_brief_block", lambda brief: "BRIEF " * 20)
    monkeypatch.setattr(routes_drafts.prompts, "format_facts_block", lambda facts: "FACTS " * 400)

    request = WriteRequest(instruction="Add a sentence")
    mandatory = routes_drafts.prompts.build_write_prompt(
        request.instruction, None, None, None, "", "", ""
    )
    brief_only = routes_drafts.prompts.build_write_prompt(
        request.instruction, None, None, None, "", "", "BRIEF " * 20
    )
    # A window (with no reserve, below) that admits the brief but not the much larger facts.
    monkeypatch.setattr(routes_drafts, "plan_budget", lambda window: TurnBudget(0, 0, 0, 0))
    window = conversation_tokens(brief_only) + 5
    assert conversation_tokens(brief_only) <= window  # brief fits
    _use_window(monkeypatch, window)

    _config, messages = _open_write(db, draft_id, request)
    content = _user_content(messages)
    assert "BRIEF" in content  # higher priority kept
    assert "FACTS" not in content  # lower priority dropped
    assert conversation_tokens(messages) <= window
    assert conversation_tokens(mandatory) <= window


def test_retrieval_is_sized_to_the_room_left_by_local_context(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_drafts.briefs, "get_brief", lambda conn, aid: None)

    # Roomy window, no competing local context: retrieval gets the full cap.
    _use_window(monkeypatch, 8192)
    roomy = _stub_retrieval(monkeypatch)
    _open_write(db, draft_id, WriteRequest(instruction="Ground this claim"))
    assert roomy == [routes_drafts.WRITE_RETRIEVAL_BUDGET]

    # A large nearby block eats into the room, so the fetch is sized down below the cap.
    _use_window(monkeypatch, 4096)
    tight = _stub_retrieval(monkeypatch)
    _open_write(
        db,
        draft_id,
        WriteRequest(instruction="Ground this claim", nearby="context. " * 400),
    )
    assert len(tight) == 1
    assert 0 < tight[0] < routes_drafts.WRITE_RETRIEVAL_BUDGET


# --- Exact-fit and one-over boundaries ----------------------------------------------


def test_the_mandatory_floor_at_exact_fit_and_one_over(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_retrieval(monkeypatch)
    monkeypatch.setattr(routes_drafts.briefs, "get_brief", lambda conn, aid: None)
    monkeypatch.setattr(routes_drafts, "select_active_facts", lambda conn, cid: [])
    # No reply reserve, so the ceiling is the window itself and the boundary is exact.
    monkeypatch.setattr(routes_drafts, "plan_budget", lambda window: TurnBudget(0, 0, 0, 0))

    request = WriteRequest(instruction="Say something brief")
    mandatory = routes_drafts.prompts.build_write_prompt(
        request.instruction, None, None, None, "", "", ""
    )
    exact = conversation_tokens(mandatory)

    _use_window(monkeypatch, exact)  # ceiling == cost: fits (<=)
    _config, messages = _open_write(db, draft_id, request)
    assert conversation_tokens(messages) == exact

    _use_window(monkeypatch, exact - 1)  # one token over
    with pytest.raises(LyraError):
        _open_write(db, draft_id, request)


def test_the_write_path_never_touches_the_draft_body(
    db: sqlite3.Connection, draft_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refusal is stateless: the body is untouched, whatever the outcome.
    part = routes_drafts._body_part(db, draft_id)
    before = str(part["content"])
    _use_window(monkeypatch, 512)
    _stub_retrieval(monkeypatch)
    with pytest.raises(LyraError):
        _open_write(db, draft_id, WriteRequest(instruction="huge. " * 500))
    after = str(routes_drafts._body_part(db, draft_id)["content"])
    assert after == before
