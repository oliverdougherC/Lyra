"""Contract tests for the writer's conversation surface: brief, sessions, and the turn.

The tool loop is stubbed at the route's seam (`routes_drafts.run_tool_loop`), because
the loop's own behavior is test_tool_loop.py. What is under test here is the frame
protocol - activity narrated in order, effects reported as their own frames, the answer
persisted with its trail - and the guards around whose conversation a turn may enter.
"""

import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import artifacts, briefs, sessions
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError
from backend.llm.tools import COMPLETED, TIMEOUT, UPSTREAM_FAILED, RecordedCall, ToolLoopResult
from backend.storage.database import connect, get_db

BODY = "# Essay\n\nProse the student wrote.\n\n## Later\n\nMore prose.\n"


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_drafts.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured, local, acknowledged endpoint, so turns open."""
    monkeypatch.setattr(
        routes_drafts,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )


def _draft(db: sqlite3.Connection, class_id: int, content: str = BODY) -> tuple[int, int]:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    artifact_id = int(created["id"])
    part_id = artifacts.create_part(
        db, artifact_id, artifacts.DRAFT_BODY, 1, content=content, status=artifacts.PART_COMPLETE
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id, part_id


def _frames(text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


# ---------------------------------------------------------------------------------
# The brief endpoints.
# ---------------------------------------------------------------------------------


def test_brief_reads_null_then_roundtrips_the_students_edit(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)

    assert client.get(f"/api/drafts/{artifact_id}/brief").json() is None

    written = client.put(
        f"/api/drafts/{artifact_id}/brief",
        json={"assignment_type": "essay", "summary": "On entropy.", "length_target": "5 pages"},
    )

    assert written.status_code == 200
    body = written.json()
    # The student's own edit lands confirmed: saving your own words is agreeing with them.
    assert body["status"] == briefs.CONFIRMED
    assert client.get(f"/api/drafts/{artifact_id}/brief").json()["summary"] == "On entropy."


def test_confirm_flips_a_proposed_brief(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    briefs.save_brief(db, artifact_id, summary="Lyra's guess.")

    confirmed = client.post(f"/api/drafts/{artifact_id}/brief/confirm")

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == briefs.CONFIRMED


def test_confirming_a_missing_brief_is_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)

    assert client.post(f"/api/drafts/{artifact_id}/brief/confirm").status_code == 404


# ---------------------------------------------------------------------------------
# Writer sessions.
# ---------------------------------------------------------------------------------


def test_a_writer_session_is_created_anchored_and_listed_for_its_draft(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    created = client.post(f"/api/drafts/{artifact_id}/sessions")

    assert created.status_code == 201
    session = created.json()
    assert session["mode"] == sessions.WRITER
    assert session["artifact_part_id"] == part_id
    listed = client.get(f"/api/drafts/{artifact_id}/sessions").json()
    assert [row["id"] for row in listed] == [session["id"]]


def test_writer_sessions_stay_out_of_the_class_sidebar(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    client.post(f"/api/drafts/{artifact_id}/sessions")

    assert sessions.list_sessions(db, class_id) == []


# ---------------------------------------------------------------------------------
# The turn.
# ---------------------------------------------------------------------------------


def _scripted_loop(
    monkeypatch: pytest.MonkeyPatch,
    result: ToolLoopResult,
    narrate: list[RecordedCall] | None = None,
    act: str | None = None,
):
    """Stub the loop at the route's seam, optionally narrating calls and acting on tools.

    `act` names one tool from the registry the stub invokes with scripted arguments, so
    an effect (a proposal, a saved brief) really lands through the same handlers the
    model would use.
    """
    captured: dict[str, object] = {}

    async def fake_loop(endpoint, api_key, model, messages, **kwargs):  # noqa: ANN001, ANN003
        captured["messages"] = messages
        captured["registry"] = kwargs.get("registry")
        on_call = kwargs.get("on_call")
        registry = kwargs.get("registry") or {}
        if act == "propose":
            registry["propose_revision"].handler(
                section="Later", replacement="## Later\n\nRevised prose.\n"
            )
        if act == "brief":
            registry["save_brief"].handler(summary="A guessed brief.")
        if act == "review":
            registry["start_review"].handler(depth="quick")
        if act == "pass":
            registry["start_draft_pass"].handler(instruction="Finish the draft.")
        for call in narrate or []:
            if on_call is not None:
                on_call(call)
        return result

    monkeypatch.setattr(routes_drafts, "run_tool_loop", fake_loop)
    return captured


def _call(name: str, **arguments: object) -> RecordedCall:
    return RecordedCall(
        name=name, arguments=arguments, raw_arguments=json.dumps(arguments), ok=True, result={}
    )


def _failed_call(name: str, **arguments: object) -> RecordedCall:
    return RecordedCall(
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
        ok=False,
        result={"error": "tool failed"},
    )


def test_a_turn_streams_activity_then_answer_and_persists_the_trail(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="Your intro carries the argument.", stopped=COMPLETED),
        narrate=[_call("read_section", ref="Essay"), _call("search_course_material", query="q")],
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "Read my intro?"}
    )

    assert response.status_code == 200
    frames = _frames(response.text)
    kinds = [frame["type"] for frame in frames]
    assert kinds == ["start", "status", "activity", "activity", "token", "done"]
    assert frames[2]["label"] == 'Reading section "Essay"'
    assert frames[4]["text"] == "Your intro carries the argument."

    stored = sessions.list_messages(db, session_id)
    assert [message["role"] for message in stored] == ["user", "assistant"]
    assert stored[1]["content"] == "Your intro carries the argument."
    assert [entry["tool"] for entry in stored[1]["tool_activity"]] == [
        "read_section",
        "search_course_material",
    ]
    # The conversation was named from the first message.
    assert sessions.get_session(db, session_id)["title"] == "Read my intro?"


def test_writer_turn_passes_trimmed_history_and_current_message_as_private_context(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    sessions.add_message(db, session_id, "user", "Earlier question")
    sessions.add_message(db, session_id, "assistant", "Answer.")
    seen: dict[str, object] = {}
    original_build = routes_drafts.writer_tools.build_registry

    def capture_registry(conn, artifact_id, profile, **kwargs):  # noqa: ANN001, ANN003
        seen["private_context"] = kwargs.get("private_context")
        return original_build(conn, artifact_id, profile, **kwargs)

    monkeypatch.setattr(routes_drafts.writer_tools, "build_registry", capture_registry)
    _scripted_loop(monkeypatch, ToolLoopResult(content="Answer.", stopped=COMPLETED))

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Newest question"},
    )

    assert response.status_code == 200
    assert seen["private_context"] == ("Earlier question", "Answer.", "Newest question")


def test_a_draft_request_without_a_pass_is_replaced_with_a_routing_failure(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(monkeypatch, ToolLoopResult(content="Here is a quick draft.", stopped=COMPLETED))

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Write the whole paper."},
    )

    token = next(frame for frame in _frames(response.text) if frame["type"] == "token")
    assert "did not route" in str(token["text"])
    assert "quick draft" not in response.text
    assert "violated draft contract" in caplog.text
    stored = sessions.list_messages(db, session_id)
    assert stored[-1]["content"] == token["text"]


def test_a_long_multiparagraph_question_answer_stays_inline_when_the_turn_is_question_only(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    answer = "\n\n".join(
        [
            "The transition works because the introduction ends on the research question.",
            "The methods section then answers the exact 'how' that question raises, so "
            "the handoff is clear.",
            "I would only add one sentence naming the experiment before the subsection break.",
        ]
    )
    _scripted_loop(monkeypatch, ToolLoopResult(content=answer, stopped=COMPLETED))

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Does the transition between my intro and methods make sense?"},
    )

    token = next(frame for frame in _frames(response.text) if frame["type"] == "token")
    assert token["text"] == answer
    assert "did not route" not in str(token["text"])


def test_a_proposal_made_mid_loop_is_reported_as_its_own_frame(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I proposed a tightening of Later.", stopped=COMPLETED),
        act="propose",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "Tighten Later."}
    )

    frames = _frames(response.text)
    proposed = next(frame for frame in frames if frame["type"] == "proposed")
    from backend.core import suggestions

    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None and proposed["edit_id"] == pending["id"]
    # The body itself did not move.
    assert str(artifacts.get_part(db, part_id)["content"]) == BODY


def test_a_saved_brief_is_reported_as_its_own_frame(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I read the handout and drafted a brief.", stopped=COMPLETED),
        act="brief",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "What is this?"}
    )

    assert any(frame["type"] == "brief" for frame in _frames(response.text))
    stored = briefs.get_brief(db, artifact_id)
    assert stored is not None and stored["status"] == briefs.PROPOSED


def test_a_review_request_without_starting_review_is_replaced_with_a_review_failure(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(monkeypatch, ToolLoopResult(content="Here is my feedback.", stopped=COMPLETED))

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Review the whole draft and leave feedback."},
    )

    token = next(frame for frame in _frames(response.text) if frame["type"] == "token")
    assert "did not start the review" in str(token["text"])


def test_a_review_request_that_starts_review_keeps_the_answer_and_reports_the_frame(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(
            content="I started a review and the comments tab will fill in.",
            stopped=COMPLETED,
        ),
        act="review",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Review the whole draft and leave feedback."},
    )

    frames = _frames(response.text)
    assert any(frame["type"] == "review" for frame in frames)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert token["text"] == "I started a review and the comments tab will fill in."


def test_a_research_request_must_use_research_tools_but_may_answer_inline_afterward(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(
            content="The handout and the article disagree on the sample size.",
            stopped=COMPLETED,
        ),
        narrate=[
            _call("search_course_material", query="sample size"),
            _call("fetch_source", url="https://example.com"),
        ],
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Compare contradictory sources on this claim before I cite it."},
    )

    token = next(frame for frame in _frames(response.text) if frame["type"] == "token")
    assert token["text"] == "The handout and the article disagree on the sample size."


def test_a_failed_research_tool_attempt_does_not_satisfy_the_research_contract(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I think this source says the claims align.", stopped=COMPLETED),
        narrate=[_failed_call("search_course_material", query="claims align")],
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Research this claim before I cite it."},
    )

    token = next(frame for frame in _frames(response.text) if frame["type"] == "token")
    assert "without gathering sources first" in str(token["text"])


def test_section_drafting_requests_route_into_the_pass_contract(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I started a pass for the conclusion.", stopped=COMPLETED),
        act="pass",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Write a conclusion for this essay."},
    )

    frames = _frames(response.text)
    assert any(frame["type"] == "pass" for frame in frames)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert token["text"] == "I started a pass for the conclusion."


@pytest.mark.parametrize(
    "prompt",
    [
        "Can you write the conclusion?",
        "Can you draft the discussion section?",
        "Could you write the abstract?",
    ],
)
def test_polite_interrogative_draft_requests_route_into_the_pass_contract(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I started a pass for that section.", stopped=COMPLETED),
        act="pass",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": prompt},
    )

    frames = _frames(response.text)
    assert any(frame["type"] == "pass" for frame in frames)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert token["text"] == "I started a pass for that section."


def test_advisory_and_definition_questions_stay_inline_instead_of_triggering_tool_contracts(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    how_session = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(
            content="Start by making the conclusion name the claim again.",
            stopped=COMPLETED,
        ),
    )

    how_response = client.post(
        f"/api/drafts/{artifact_id}/chat/{how_session}",
        json={"content": "How can I improve the conclusion?"},
    )

    how_token = next(frame for frame in _frames(how_response.text) if frame["type"] == "token")
    assert how_token["text"] == "Start by making the conclusion name the claim again."

    what_session = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(
            content="Peer review means another reader evaluates the draft before submission.",
            stopped=COMPLETED,
        ),
    )

    what_response = client.post(
        f"/api/drafts/{artifact_id}/chat/{what_session}",
        json={"content": "What does peer review mean in this class?"},
    )

    what_token = next(frame for frame in _frames(what_response.text) if frame["type"] == "token")
    assert "did not start the review" not in str(what_token["text"])
    assert "evaluates the draft" in str(what_token["text"])


def test_polite_interrogative_revision_requests_route_into_the_revision_contract(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I proposed a revision to the conclusion.", stopped=COMPLETED),
        act="propose",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Can you revise the conclusion?"},
    )

    frames = _frames(response.text)
    assert any(frame["type"] == "proposed" for frame in frames)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert token["text"] == "I proposed a revision to the conclusion."


def test_research_and_draft_the_literature_review_uses_the_draft_contract_not_review(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="I started a pass for the literature review.", stopped=COMPLETED),
        act="pass",
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}",
        json={"content": "Research and draft the literature review."},
    )

    frames = _frames(response.text)
    assert any(frame["type"] == "pass" for frame in frames)
    assert not any(frame["type"] == "review" for frame in frames)
    token = next(frame for frame in frames if frame["type"] == "token")
    assert token["text"] == "I started a pass for the literature review."


@pytest.mark.parametrize(
    ("stopped", "detail"),
    [
        (TIMEOUT, "Checking took too long."),
        (UPSTREAM_FAILED, "The tutor endpoint could not be reached."),
    ],
)
def test_an_incomplete_loop_is_an_error_frame_and_no_stored_reply(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
    stopped: str,
    detail: str,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    _scripted_loop(
        monkeypatch,
        ToolLoopResult(content="", stopped=stopped, detail=detail),
        narrate=[_call("read_outline")],
    )

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "Slow question."}
    )

    frames = _frames(response.text)
    assert [frame["type"] for frame in frames] == ["start", "status", "activity", "error"]
    assert frames[-1]["message"] == detail
    # The question is kept; the failed answer is not. A reload shows a turn to retry.
    assert [message["role"] for message in sessions.list_messages(db, session_id)] == ["user"]


def test_the_turn_prompt_carries_brief_outline_and_the_question_last(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    briefs.save_brief(db, artifact_id, summary="An essay on entropy.")
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]
    captured = _scripted_loop(monkeypatch, ToolLoopResult(content="ok", stopped=COMPLETED))

    client.post(f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "Hello"})

    messages = captured["messages"]
    system = str(messages[0]["content"])
    assert "An essay on entropy." in system
    assert "1 Essay" in system  # the outline
    assert "not confirmed" in system  # proposed brief carries its caveat
    assert messages[-1] == {"role": "user", "content": "Hello"}
    registry = captured["registry"]
    assert "propose_revision" in registry and "write_section" not in registry


def test_a_turn_refuses_someone_elses_session(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    allowed: None,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    other_id, _ = _draft(db, class_id)
    other_session = client.post(f"/api/drafts/{other_id}/sessions").json()["id"]
    tutor_session = int(sessions.create_session(db, class_id)["id"])

    wrong_draft = client.post(
        f"/api/drafts/{artifact_id}/chat/{other_session}", json={"content": "x"}
    )
    wrong_kind = client.post(
        f"/api/drafts/{artifact_id}/chat/{tutor_session}", json={"content": "x"}
    )

    assert wrong_draft.status_code == 404
    assert wrong_kind.status_code == 404


def test_a_turn_is_blocked_without_an_endpoint(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    session_id = client.post(f"/api/drafts/{artifact_id}/sessions").json()["id"]

    response = client.post(
        f"/api/drafts/{artifact_id}/chat/{session_id}", json={"content": "Hello"}
    )

    assert response.status_code == 400
    assert "endpoint" in response.json()["detail"]


def test_tutor_routes_refuse_writer_sessions(db: sqlite3.Connection, class_id: int) -> None:
    from backend.api import routes_chat

    part = _draft(db, class_id)
    session = sessions.create_session(db, class_id, artifact_part_id=part[1], mode=sessions.WRITER)

    with pytest.raises(LyraError, match="belongs to a draft"):
        routes_chat._open_turn(
            db,
            int(session["id"]),
            routes_chat.TurnInput(content="hi", mode="guide"),
        )
