"""The tutor semantic eval harness, tested without a model.

An evaluation harness that grades wrongly is worse than none: it reports a number nobody
can check, and the prompt work gets tuned against it. So the parts that do not need a
model are tested the same way the product is - the corpus contract, the turn assembly,
and the verdict arithmetic - with the model calls left to `scripts/eval_tutor.py` at run
time.

Nothing here reaches an endpoint or the student's own database.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import routes_chat  # noqa: E402
from backend.core.app_settings import TutorConfig  # noqa: E402
from backend.llm.prompts import TUTOR_PROMPT_CONTRACT_VERSION, build_system_prompt  # noqa: E402
from backend.rag.retrieve import RetrievalResult, RetrievedChunk  # noqa: E402
from scripts import eval_tutor  # noqa: E402
from scripts.eval_tutor import (  # noqa: E402
    DIMENSIONS,
    GRADING_SCHEMA,
    Case,
    _parse_grading,
    case_messages,
    grading_messages,
    load_corpus,
    verdict_for,
)

CORPUS = ROOT / "scripts" / "eval_corpora" / "tutor_semantic.json"

# The request shapes PLA-401's spec requires the corpus to cover: id -> (mode, user).
REQUIRED_CASES: dict[str, tuple[str, str]] = {
    "explain-convolution": ("guide", "Explain convolution"),
    "what-is-a-derivative": ("guide", "What is a derivative?"),
    "why-can-they-cancel-these-terms": ("guide", "Why can they cancel these terms?"),
    "no-idea-how-to-start": ("guide", "I have no idea how to start."),
    "attempt-where-did-i-go-wrong": ("guide", "where did I go wrong?"),
    "is-my-answer-correct": ("guide", "Is my answer correct?"),
    "just-tell-me-the-answer": ("guide", "Just tell me the answer."),
    "explain-that-more-simply": ("guide", "Explain that more simply."),
    "dont-ask-me-questions-teach-it": ("guide", "Don't ask me questions; teach it."),
    "intuition-not-the-derivation": ("guide", "Give me the intuition, not the derivation."),
    "five-minutes-before-exam": ("guide", "I'm studying five minutes before the exam"),
}


def _header_cases() -> tuple[dict[str, object], list[Case]]:
    return load_corpus(CORPUS)


def test_the_shipped_corpus_loads_versioned_against_the_current_contract() -> None:
    """A case graded against the wrong contract version measures something else."""
    header, cases = _header_cases()

    assert header["corpus_version"]
    assert header["prompt_contract_version"] == TUTOR_PROMPT_CONTRACT_VERSION
    assert header["dimensions"] == DIMENSIONS
    assert cases


def test_the_corpus_covers_every_required_request_shape() -> None:
    """The spec's minimum: every request shape a student actually makes is in the set."""
    header, cases = _header_cases()
    by_id = {case.id: case for case in cases}

    for case_id, (mode, needle) in REQUIRED_CASES.items():
        assert case_id in by_id, f"missing required case {case_id}"
        case = by_id[case_id]
        assert case.mode == mode
        assert needle.lower() in case.user.lower()
        # A case with nothing to grade is not a case.
        assert case.must, f"{case_id} needs at least one must item"
        assert case.must_not, f"{case_id} needs at least one must_not item"
        assert case.notes, f"{case_id} needs ground truth for the grader"


def test_explain_convolution_keeps_its_regression_contract() -> None:
    """The PLA-401 motivating failure: the case that must stay pointed at the old bug.

    "Explain convolution" used to get an indirect Socratic setup - which values of a
    variable make two functions nonzero - instead of the explanation. The corpus case is
    the durable guard: it is Guide mode, it asks for the explanation directly, and its
    must_not items name the old behavior.
    """
    _, cases = _header_cases()
    case = next(entry for entry in cases if entry.id == "explain-convolution")

    assert case.mode == "guide"
    assert case.user == "Explain convolution"
    joined_must = " ".join(case.must).lower()
    assert "mental" in joined_must
    joined_must_not = " ".join(case.must_not).lower()
    # The two failure shapes the old prompt produced, written as behaviors.
    assert "question" in joined_must_not
    assert "withhold" in joined_must_not


def test_the_convolution_cases_encode_the_correct_integral() -> None:
    """The ground truth the grader judges correctness against must itself be correct.

    x(t) = u(t) - u(t-1) convolved with h(t) = e^{-t} u(t) gives, on 0 <= t <= 1,
    the integral from 0 to t of e^{-(t-tau)} d(tau) = 1 - e^{-t}. An early draft of
    the corpus encoded e^{-t}(1 - e^{-t}) there, so a tutor reply that did the math
    right would have failed the correctness dimension; this pins the value in both
    convolution cases.
    """
    _, cases = _header_cases()
    by_id = {case.id: case for case in cases}
    for case_id in ("attempt-where-did-i-go-wrong", "show-convolution-worked"):
        case = by_id[case_id]
        ground_truth = " ".join((*case.must, *case.may, case.notes))
        assert "1 - e^{-t}" in ground_truth, case_id
        assert "e^{-t}(1 - e^{-t})" not in ground_truth, case_id
    # The t >= 1 branch was correct all along and the fix must not touch it.
    case = by_id["show-convolution-worked"]
    assert "e^{-t}(e - 1)" in " ".join((*case.must, case.notes))


def test_case_messages_match_the_chat_routes_assembly(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The harness must send the product what the product sends.

    The route's `_build_turn` is the reference assembly: system prompt, anchored step
    (none here), retrieved context, history, then the question. The harness assembles
    the same case independently; this asserts the two agree message for message, so a
    change on one side that drifts the other fails here.
    """
    _, cases = _header_cases()
    for case in cases:
        harness_messages = case_messages(case)

        chunks = [
            RetrievedChunk(
                chunk_id=1,
                document_id=1,
                content=str(chunk.get("content") or ""),
                token_count=10,
                page_number=chunk.get("page_number"),
                section_title=chunk.get("section_title"),
                section_path=chunk.get("section_path"),
                section_number=chunk.get("section_number"),
                problem_number=chunk.get("problem_number"),
                part_index=None,
                filename=str(chunk.get("filename") or ""),
                similarity=1.0,
                score=1.0,
            )
            for chunk in case.context
        ]
        preparation = routes_chat.TurnPreparation(
            class_id=class_id,
            system_prompt=build_system_prompt(case.mode, [], []),
            history=[dict(turn) for turn in case.history],
            retrieval_budget=10_000,
            anchor=None,
        )
        route_turn = routes_chat._build_turn(
            preparation,
            routes_chat.TurnInput(content=case.user, mode=case.mode),
            RetrievalResult(chunks=chunks, trimmed=False, omitted_document_count=0),
        )

        assert harness_messages == route_turn.messages, case.id


def test_case_messages_render_a_context_block_when_the_case_carries_one() -> None:
    """A case with retrieved material gets it rendered the way the route renders it."""
    case = Case(
        id="ctx",
        mode="guide",
        user="What did the notes say about convolution?",
        history=(),
        context=(
            {
                "content": "The convolution integral sums the overlap of f and a flipped g.",
                "filename": "Lecture 4.pdf",
                "page_number": 12,
                "section_title": "Convolution",
            },
        ),
        must=("uses the course material",),
        must_not=("ignores the retrieved material",),
        may=(),
        notes="The course defines convolution via the overlap integral.",
    )

    messages = case_messages(case)

    system = messages[0]["content"]
    assert "Retrieved context from the student's uploaded material:" in system
    assert "Lecture 4.pdf" in system
    assert "page 12" in system
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": case.user}


def test_history_order_and_roles_survive_into_the_messages() -> None:
    """A multi-turn case keeps its turns in order, with the request last."""
    _, cases = _header_cases()
    case = next(entry for entry in cases if entry.history)

    messages = case_messages(case)

    roles = [message["role"] for message in messages]
    assert roles[0] == "system"
    assert roles[1:] == [turn["role"] for turn in case.history] + ["user"]
    assert messages[-1]["content"] == case.user


def _grading(
    case: Case, *, must_met: bool = True, violated: bool = False, correctness: int = 2
) -> dict[str, object]:
    """A grader reply built to the contract, with the levers the verdict reads."""
    dimensions = {
        dimension: {"score": correctness if dimension == "correctness" else 2, "note": "ok"}
        for dimension in DIMENSIONS
    }
    return {
        "dimensions": dimensions,
        "must": [
            {"index": index, "met": must_met, "why": "evidence"}
            for index in range(1, len(case.must) + 1)
        ],
        "must_not": [
            {"index": index, "violated": violated, "why": "evidence"}
            for index in range(1, len(case.must_not) + 1)
        ],
    }


def test_the_verdict_is_computed_not_taken_from_the_model() -> None:
    """The pass/fail is arithmetic over the grader's per-item judgments."""
    _, cases = _header_cases()
    case = next(entry for entry in cases if entry.id == "is-my-answer-correct")

    assert verdict_for(case, _grading(case)) == "pass"
    # A missed must item is a fail, whatever the dimensions say.
    assert verdict_for(case, _grading(case, must_met=False)) == "fail"
    # A violated must_not is a fail.
    assert verdict_for(case, _grading(case, violated=True)) == "fail"
    # A substantive correctness error is a fail; a minor one is not.
    assert verdict_for(case, _grading(case, correctness=0)) == "fail"
    assert verdict_for(case, _grading(case, correctness=1)) == "pass"


def test_an_unreadable_or_incomplete_grade_is_a_fail() -> None:
    """A grade missing an index, a dimension, or a shape is not a pass."""
    _, cases = _header_cases()
    case = next(entry for entry in cases if entry.id == "explain-convolution")

    missing_must_index = _grading(case)
    missing_must_index["must"] = missing_must_index["must"][:-1]  # type: ignore[index]
    assert verdict_for(case, missing_must_index) == "fail"

    missing_dimension = _grading(case)
    del missing_dimension["dimensions"]["correctness"]  # type: ignore[index]
    assert verdict_for(case, missing_dimension) == "fail"

    assert verdict_for(case, {"dimensions": "not a dict"}) == "fail"
    assert verdict_for(case, {}) == "fail"


def test_parse_grading_tolerates_a_code_fence() -> None:
    """A server that drops constrained decoding still adds the fence the product strips."""
    payload = {"dimensions": {}, "must": [], "must_not": []}
    text = json.dumps(payload)

    assert _parse_grading(text) == payload
    assert _parse_grading("```json\n" + text + "\n```") == payload
    assert _parse_grading("not json at all") is None
    assert _parse_grading('["a", "list"]') is None


def test_grading_messages_number_the_items_and_carry_the_ground_truth() -> None:
    """The grader gets the transcript, the reply, the notes, and numbered items."""
    _, cases = _header_cases()
    case = next(entry for entry in cases if entry.id == "attempt-where-did-i-go-wrong")
    reply = "The error is treating e^{-t} as a constant."

    messages = grading_messages(case, reply)

    user_content = messages[1]["content"]
    assert "The mode: guide" in user_content
    assert "The tutor's reply:\n" + reply in user_content
    assert case.notes in user_content
    # Every item appears, numbered from one.
    assert f"1. {case.must[0]}" in user_content
    assert f"{len(case.must)}. {case.must[-1]}" in user_content
    assert f"{len(case.must_not)}. {case.must_not[-1]}" in user_content


def test_the_grader_schema_pins_all_seven_dimensions() -> None:
    """A schema missing a dimension would let the harness average a hole."""
    properties = GRADING_SCHEMA.schema["properties"]

    assert set(GRADING_SCHEMA.schema["required"]) == {"dimensions", "must", "must_not"}
    dimension_props = properties["dimensions"]["properties"]  # type: ignore[index]
    assert set(dimension_props) == set(DIMENSIONS)
    for entry in dimension_props.values():
        assert entry["required"] == ["score", "note"]
        assert entry["properties"]["score"] == {  # type: ignore[index]
            "type": "integer",
            "minimum": 0,
            "maximum": 2,
        }


def test_a_run_of_only_failed_cases_still_leaves_a_gradeable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed case is a terminal result, not a missing one.

    When every selected case fails, `run` still writes the runs.json that `grade`
    consumes, and the run's meta carries a locality class, not the endpoint URL.
    """

    async def _down(endpoint, api_key, model, case, messages):
        raise ConnectionError("endpoint down")

    config = TutorConfig("http://127.0.0.1:1234/v1", None, "tutor-model", 16384)
    monkeypatch.setattr(eval_tutor, "_run_one", _down)
    monkeypatch.setattr(eval_tutor, "_endpoint_config", lambda path: config)

    args = argparse.Namespace(
        corpus=str(CORPUS),
        workspace=str(tmp_path),
        source_db="/ignored",
        case=["what-is-a-derivative"],
        surface="tutor",
        class_id=None,
        session_id=None,
    )
    assert eval_tutor.cmd_run(args) == 0

    runs = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    record = runs["cases"]["what-is-a-derivative"]
    assert record["status"] == "error"
    assert record["error"] == "ConnectionError"
    assert record["stopped"] == "exception"
    assert record["provenance"]["hashes"]["corpus"] == eval_tutor._sha256(CORPUS.read_bytes())

    meta_text = (tmp_path / "meta.json").read_text(encoding="utf-8")
    meta = json.loads(meta_text)
    assert "http://127.0.0.1:1234/v1" not in meta_text
    assert "endpoint" not in meta
    assert meta["endpoint_locality"] == "local"
    assert meta["model"] == "tutor-model"
    assert meta["context_window"] == 16384

    grade_args = argparse.Namespace(
        corpus=str(CORPUS),
        workspace=str(tmp_path),
        source_db="/ignored",
        case=None,
        judge_source_db=None,
        judge_model=None,
    )
    assert eval_tutor.cmd_grade(grade_args) == 0
    grades = json.loads((tmp_path / "grades.json").read_text(encoding="utf-8"))
    assert grades["what-is-a-derivative"]["verdict"] == "not_run"


def test_grade_records_the_judge_it_uses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge is provenance, not a guess: grade_meta says which model graded, and
    whether it was the tutor's own.
    """
    config = TutorConfig("http://127.0.0.1:1234/v1", None, "tutor-model", 16384)
    monkeypatch.setattr(eval_tutor, "_endpoint_config", lambda path: config)
    seen_models: list[str | None] = []

    async def _grade(endpoint, api_key, model, case, reply):
        seen_models.append(model)
        return _grading(case)

    monkeypatch.setattr(eval_tutor, "_grade_one", _grade)
    (tmp_path / "runs.json").write_text(
        json.dumps(
            {
                "cases": {
                    "what-is-a-derivative": {
                        "status": "ok",
                        "generated_at": "2026-09-02T00:00:00",
                        "response": "A derivative is the instantaneous rate of change.",
                        "system_prompt_tokens": 100,
                        "user": "What is a derivative?",
                        "mode": "guide",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def grade_args(judge_model: str | None) -> argparse.Namespace:
        return argparse.Namespace(
            corpus=str(CORPUS),
            workspace=str(tmp_path),
            source_db="/ignored",
            case=None,
            judge_source_db=None,
            judge_model=judge_model,
        )

    # The default judge is the tutor's own configuration, and that is recorded.
    assert eval_tutor.cmd_grade(grade_args(None)) == 0
    meta = json.loads((tmp_path / "grade_meta.json").read_text(encoding="utf-8"))
    assert meta["tutor"] == {"model": "tutor-model", "locality": "local"}
    assert meta["judge"]["model"] == "tutor-model"
    assert meta["judge"]["same_as_tutor"] is True
    assert seen_models == ["tutor-model"]

    # A re-grade with an explicit judge updates the entry and records the switch.
    assert eval_tutor.cmd_grade(grade_args("judge-model")) == 0
    meta = json.loads((tmp_path / "grade_meta.json").read_text(encoding="utf-8"))
    assert meta["judge"]["model"] == "judge-model"
    assert meta["judge"]["same_as_tutor"] is False
    assert seen_models[-1] == "judge-model"
    grades = json.loads((tmp_path / "grades.json").read_text(encoding="utf-8"))
    assert grades["what-is-a-derivative"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# PLA-401 final pass, item 7: the class_chat surface assembles what the class
# chat ACTUALLY sends - through the production planner - and the tool-less
# fallback is part of the surface, not a divergence from it.
# ---------------------------------------------------------------------------


def _class_chat_case() -> Case:
    """One corpus-shaped case: history, retrieved context, and the question."""
    return Case(
        id="explain-convolution",
        mode="guide",
        user="Explain convolution",
        history=(
            {"role": "user", "content": "What is a transfer function?"},
            {"role": "assistant", "content": "It maps an input to an output."},
        ),
        context=(
            {
                "content": "The convolution integral sums the overlap of f and a flipped g.",
                "filename": "Lecture 4.pdf",
                "page_number": 12,
                "section_title": "Convolution",
            },
        ),
        must=("gives the overlap integral",),
        must_not=("withholds the requested explanation",),
        may=(),
        notes="The course defines convolution via the overlap integral.",
    )


def test_the_class_chat_surface_matches_the_production_assembly(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The harness must send the product what the product sends.

    `class_chat_assembly` is the harness's surface; `routes_agent_chat._plan_agent_turn`
    is the route's planner. This assembles the same case through both and asserts they
    agree message for message and tool for tool, so a change to the production assembly
    (the prompt, the retrieval block, the registry) is visible to the eval here.
    """
    from backend.api import routes_agent_chat
    from backend.core import sessions as sessions_core
    from backend.core.app_settings import TutorConfig
    from backend.llm import tools as llm_tools
    from backend.llm.turn_budget import HistoryMessage
    from scripts import eval_tutor

    session_id = int(sessions_core.create_session(db, class_id)["id"])
    config = TutorConfig(
        endpoint_url="http://127.0.0.1:8000/v1",
        api_key=None,
        model="local-model",
        context_window=8192,
    )
    case = _class_chat_case()

    # The harness surface.
    harness = eval_tutor.class_chat_assembly(db, class_id, session_id, config, case)

    # The route's planner, with the case's history and context handed to it the same way.
    history = tuple(
        HistoryMessage(role=turn["role"], content=turn["content"]) for turn in case.history
    )
    retrieval = RetrievalResult(
        chunks=tuple(
            RetrievedChunk(
                chunk_id=index + 1,
                document_id=0,
                content=str(chunk.get("content") or ""),
                token_count=0,
                page_number=chunk.get("page_number"),
                section_title=chunk.get("section_title"),
                section_path=chunk.get("section_path"),
                section_number=chunk.get("section_number"),
                problem_number=chunk.get("problem_number"),
                part_index=None,
                filename=str(chunk.get("filename") or ""),
                similarity=1.0,
                score=1.0,
            )
            for index, chunk in enumerate(case.context)
        ),
        trimmed=False,
        omitted_document_count=0,
    )
    plan = routes_agent_chat._plan_agent_turn(
        db,
        class_id,
        session_id,
        config,
        profile="agent",
        content=case.user,
        mode=case.mode,
        document_id=None,
        cached_retrieval=retrieval,
        history=history,
    )

    # The two agree, message for message and tool for tool.
    assert [dict(message) for message in harness.messages] == [
        dict(message) for message in plan.messages
    ]
    assert harness.tools == tuple(llm_tools.tool_schemas(plan.registry))
    assert harness.toolless is plan.toolless


def test_the_class_chat_surface_carries_the_contract_the_class_chat_sends(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The surface is not just a mirror: it carries the pieces that make it the class
    chat - the mode contract, the agent capability layer, the retrieved context block,
    the history, the question, and the class's real tool schemas."""
    from backend.core import sessions as sessions_core
    from backend.core.app_settings import TutorConfig
    from scripts import eval_tutor

    session_id = int(sessions_core.create_session(db, class_id)["id"])
    config = TutorConfig(
        endpoint_url="http://127.0.0.1:8000/v1",
        api_key=None,
        model="local-model",
        context_window=8192,
    )
    case = _class_chat_case()
    assembly = eval_tutor.class_chat_assembly(db, class_id, session_id, config, case)

    system = str(assembly.messages[0]["content"])
    # The mode contract the turn runs under (the full tutor prompt, not a stub).
    assert "Mode: Guide." in system
    assert "omit closing questions and follow-up offers" in system
    assert "one concrete first move" in system
    assert "less abstraction, not a second full lecture" in system
    # The agent capability layer, appended on top of it.
    assert "You are Lyra's class agent" in system
    assert "The latest user request sets the answer's scope" in system
    assert "Use verified results only for the claims they actually check" in system
    # The case's retrieved context, rendered as the route renders it.
    assert "Retrieved context from the student's uploaded material:" in system
    assert "Lecture 4.pdf" in system
    assert "page 12" in system
    # The conversation: history in order, then the question.
    contents = [str(message["content"]) for message in assembly.messages]
    assert "What is a transfer function?" in contents
    assert contents.index("It maps an input to an output.") < contents.index("Explain convolution")
    assert assembly.messages[-1] == {"role": "user", "content": case.user}
    # The class's real tool surface: a fresh class offers the case-evaluation tool...
    tool_names = {str(schema.get("function", {}).get("name")) for schema in assembly.tools}
    assert "cas_evaluate" in tool_names
    # ...and nothing the class has not granted: no web research on an ungranted class.
    assert "search_web" not in tool_names
    assert assembly.toolless is False


def test_the_class_chat_surface_is_tool_less_on_a_known_incompatible_endpoint(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The fallback is part of the surface: a known tool-incompatible endpoint plans the
    tool-less surface at once - no tools on the wire, the tool-less note in the system
    prompt, the basic tutoring turn still answered - exactly as the route plans it."""
    from backend.core import sessions as sessions_core
    from backend.core.app_settings import TutorConfig
    from scripts import eval_tutor

    session_id = int(sessions_core.create_session(db, class_id)["id"])
    config = TutorConfig(
        endpoint_url="http://127.0.0.1:8000/v1",
        api_key=None,
        model="local-model",
        context_window=16384,
        tools_supported=False,
    )
    case = _class_chat_case()
    assembly = eval_tutor.class_chat_assembly(db, class_id, session_id, config, case)

    assert assembly.toolless is True
    assert assembly.tools == ()
    system = str(assembly.messages[0]["content"])
    assert "is not available in this conversation" in system
    # The full tutor contract is still there: tool-less is a smaller surface, not a
    # different conversation.
    assert "Mode: Guide." in system
    # The question still went out, tool-less: the basic tutoring turn is never refused
    # over the cost of optional capability.
    assert assembly.messages[-1] == {"role": "user", "content": case.user}


# ---------------------------------------------------------------------------
# PLA-401 final pass, items 1-2: the class_chat surface runs the PRODUCTION
# tool loop to its terminal answer, and the default run is deterministic -
# a disposable eval-only environment, never the student's database.
# ---------------------------------------------------------------------------


def test_the_eval_environment_is_deterministic_and_user_data_free() -> None:
    """The default run plans against a disposable database: one class, one session,
    nothing the student owns.

    No user data (no documents, no facts, no workspace, no audit history), no public-web
    grant (a fresh database's default), and the same identifiers on every run, so the
    corpus grades the product under identical class state and the run's artifacts never
    touch `data/lyra.db`.
    """
    first = eval_tutor.open_eval_environment(None)
    try:
        db_path, conn, class_id, session_id = first
        assert class_id == 1 and session_id == 1
        assert db_path.name == "eval.db"
        assert db_path.parent.name.startswith("lyra-eval-")
        # Fresh-database defaults: no web grant, no parallelism, no source content.
        settings = conn.execute(
            "select allow_web_research, parallel_requests, source_content_enabled"
            " from settings where id = 1"
        ).fetchone()
        assert tuple(settings) == (0, 0, 0)
        # Nothing of the student's: no material, no workspace, no prior history.
        assert conn.execute("select count(*) from documents").fetchone()[0] == 0
        assert conn.execute("select count(*) from profile_facts").fetchone()[0] == 0
        assert conn.execute("select count(*) from class_workspaces").fetchone()[0] == 0
        assert conn.execute("select count(*) from tool_audit_events").fetchone()[0] == 0
        assert (
            conn.execute(
                "select count(*) from messages where session_id = ?", (session_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        first[1].close()

    second = eval_tutor.open_eval_environment(None)
    try:
        # Deterministic: the same class and session every time ...
        assert second[2] == 1 and second[3] == 1
        # ... each run in its own disposable database, so runs never share state.
        assert second[0] != first[0]
    finally:
        second[1].close()


def test_the_eval_environment_accepts_an_explicit_path(tmp_path: Path) -> None:
    """`--eval-db` points the disposable environment at a caller-chosen database."""
    target = tmp_path / "target-eval.db"
    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(target)
    try:
        assert db_path == target
        assert class_id == 1 and session_id == 1
    finally:
        conn.close()


def test_the_eval_class_chat_surface_offers_only_what_a_fresh_class_grants() -> None:
    """The eval class offers what a fresh class offers: the case-evaluation compute
    surface and the workspace-access request, and nothing the class has not granted -
    no public web (the grant is off in the fresh database), no workspace tools (there is
    no workspace), and the executable registry is exactly the schemas on the wire.
    """
    from backend.core.app_settings import TutorConfig

    # A context-free case, as every shipped corpus case is: with `no_context`, the
    # assembly pins the eval class's empty retrieval - the product's no-context state.
    case = Case(
        id="explain-convolution",
        mode="guide",
        user="Explain convolution",
        history=(),
        context=(),
        must=("gives the overlap integral",),
        must_not=("withholds the requested explanation",),
        may=(),
        notes="The course defines convolution via the overlap integral.",
    )
    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(None)
    try:
        config = TutorConfig(
            endpoint_url="http://127.0.0.1:1234/v1",
            api_key=None,
            model="local-model",
            context_window=8192,
        )
        # The default run plans with no corpus context (every shipped case is
        # context-free), and the surface carries the production tool loop's inputs.
        assembly = eval_tutor.class_chat_assembly(
            conn, class_id, session_id, config, case, no_context=True
        )
        tool_names = {str(schema.get("function", {}).get("name")) for schema in assembly.tools}
        assert "cas_evaluate" in tool_names
        assert "request_workspace_access" in tool_names
        # Nothing the class has not granted.
        assert "search_web" not in tool_names
        assert "fetch_source" not in tool_names
        assert not any(
            name.startswith(("read_", "list_", "write_", "run_", "apply_")) for name in tool_names
        )
        # The registry is the executable face of the schemas the loop will send.
        assert set(assembly.registry) == tool_names
        # The default run carries no retrieved material, even for a case written with
        # some: the eval grades the product's no-context state truthfully.
        system = str(assembly.messages[0]["content"])
        assert "Retrieved context from the student's uploaded material:" not in system
        assert assembly.toolless is False
    finally:
        conn.close()


def test_the_eval_class_chat_surface_still_carries_corpus_context_when_asked() -> None:
    """Opting back into the case's context renders the block the route renders - the
    eval surface is the product surface with the environment pinned, not a different
    prompt.
    """
    from backend.core.app_settings import TutorConfig

    case = _class_chat_case()
    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(None)
    try:
        config = TutorConfig(
            endpoint_url="http://127.0.0.1:1234/v1",
            api_key=None,
            model="local-model",
            context_window=8192,
        )
        assembly = eval_tutor.class_chat_assembly(
            conn, class_id, session_id, config, case, no_context=False
        )
        system = str(assembly.messages[0]["content"])
        assert "Retrieved context from the student's uploaded material:" in system
        assert "Lecture 4.pdf" in system
        assert "page 12" in system
    finally:
        conn.close()


def test_a_case_that_calls_a_tool_grades_the_terminal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surface runs the PRODUCTION tool loop to its terminal answer.

    A model that checks its work with `cas_evaluate` before explaining is mid-
    conversation, not a failure: the loop feeds the result back and keeps going, and the
    case is graded on the answer the loop ends with. The calls ride along as metadata
    (`tool_calls`, `rounds`), and the production audit lands in the eval database.
    """
    from backend.core.app_settings import TutorConfig
    from backend.llm import client as llm_client
    from backend.llm import tools as llm_tools

    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(None)
    try:
        config = TutorConfig(
            endpoint_url="http://127.0.0.1:1234/v1",
            api_key=None,
            model="local-model",
            context_window=16384,
        )
        case = _class_chat_case()
        assembly = eval_tutor.class_chat_assembly(
            conn, class_id, session_id, config, case, no_context=True
        )

        async def scripted_model(endpoint, api_key, model, messages, tools, **kwargs):
            # Round one: the model verifies its work. Round two: the terminal answer.
            saw_tool_result = any(message.get("role") == "tool" for message in messages)
            if saw_tool_result:
                return llm_client.AssistantMessage(
                    content=(
                        "Convolution slides one signal past the other "
                        "and sums the overlap at each step."
                    )
                )
            return llm_client.AssistantMessage(
                content="",
                tool_calls=(
                    llm_client.ToolCall(
                        id="c1",
                        name="cas_evaluate",
                        arguments=json.dumps({"expression": "sin(x) + cos(x)"}),
                    ),
                ),
            )

        monkeypatch.setattr(llm_tools, "complete_with_tools", scripted_model)
        record = asyncio.run(
            eval_tutor._run_one_class_chat(
                config.endpoint_url, config.api_key, config.model, assembly
            )
        )

        # Graded on the terminal answer, with the verification as metadata.
        assert record["status"] == "ok"
        assert "slides one signal past the other" in str(record["response"])
        assert len(record["tool_calls"]) == 1
        call = record["tool_calls"][0]
        assert call["name"] == "cas_evaluate" and call["ok"] is True
        assert call["arguments"] == {"expression": "sin(x) + cos(x)"}
        assert json.loads(call["raw_arguments"]) == call["arguments"]
        assert call["result"]
        assert record["stopped"] == llm_tools.COMPLETED
        assert record["observed_tools_supported"] is True
        assert (
            record["generation_parameters"]["max_tokens"]
            == assembly.context_budget.generation_reserve
        )
        assert record["rounds"] == 2
        assert record["toolless"] is False
        # The production loop's audit row landed in the disposable environment.
        assert [row[0] for row in conn.execute("select state from tool_audit_events")] == [
            "succeeded"
        ]
    finally:
        conn.close()


def test_a_loop_that_never_reaches_a_terminal_answer_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a loop that never reaches a usable terminal answer fails, and it is named
    by the loop's own stop reason - an `incomplete` record, not a grade of a reply that
    was not produced.
    """
    from backend.core import errors
    from backend.core.app_settings import TutorConfig
    from backend.llm import tools as llm_tools

    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(None)
    try:
        config = TutorConfig(
            endpoint_url="http://127.0.0.1:1234/v1",
            api_key=None,
            model="local-model",
            context_window=16384,
        )
        case = _class_chat_case()
        assembly = eval_tutor.class_chat_assembly(
            conn, class_id, session_id, config, case, no_context=True
        )

        async def broken_model(endpoint, api_key, model, messages, tools, **kwargs):
            raise errors.UpstreamError("the model endpoint could not be reached")

        monkeypatch.setattr(llm_tools, "complete_with_tools", broken_model)
        record = asyncio.run(
            eval_tutor._run_one_class_chat(
                config.endpoint_url, config.api_key, config.model, assembly
            )
        )

        assert record["status"] == "incomplete"
        assert record["stopped"] == llm_tools.UPSTREAM_FAILED
        assert "could not be reached" in str(record["detail"])
        assert record["response"] == ""
        assert record["tool_calls"] == []
    finally:
        conn.close()


def test_an_unknown_endpoint_refusal_replans_toolless_and_reaches_the_terminal_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NO_TOOL_SUPPORT fallback through the case runner, deterministically.

    An endpoint whose tool support is UNKNOWN (`tools_supported=None`) refuses the turn's
    FIRST tools request. The product settles that pass as abandoned and re-plans the same
    turn tool-less; the case runner must mirror that - its `replan_toolless` is an actual
    zero-argument callable the loop invokes, not an already-evaluated assembly - so the
    case reaches the same tool-less terminal answer it does in production, recorded
    exactly as production settles it.
    """
    from backend.core import errors
    from backend.llm import client as llm_client
    from backend.llm import tools as llm_tools

    db_path, conn, class_id, session_id = eval_tutor.open_eval_environment(None)
    try:
        # Unknown tool support: the first request carries the tool schemas.
        config = TutorConfig(
            endpoint_url="http://127.0.0.1:1234/v1",
            api_key=None,
            model="local-model",
            context_window=16384,
            tools_supported=None,
        )
        case = Case(
            id="explain-convolution",
            mode="guide",
            user="Explain convolution",
            history=(),
            context=(),
            must=("gives the overlap integral",),
            must_not=("withholds the requested explanation",),
            may=(),
            notes="The course defines convolution via the overlap integral.",
        )

        tool_rounds: list[int] = []
        plain_calls: list[int] = []

        async def refusing_tools(
            endpoint: str,
            api_key: str | None,
            model: str | None,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            **kwargs: object,
        ):
            tool_rounds.append(len(tools))
            raise errors.ToolsUnsupportedError("the endpoint does not accept tool calls")

        async def plain_complete(*args: object, **kwargs: object) -> str:
            plain_calls.append(1)
            return "Convolution slides one signal past the other and sums the overlap."

        monkeypatch.setattr(llm_tools, "complete_with_tools", refusing_tools)
        monkeypatch.setattr(llm_client, "complete", plain_complete)

        cases_run: dict[str, object] = {}
        eval_tutor._run_class_chat_cases(
            [case], conn, class_id, session_id, config, cases_run, no_context=True
        )

        record = cases_run[case.id]
        # The run fell back tool-less and reached the terminal answer - it is recorded
        # as an ok tool-less turn, not as a failed or incomplete one.
        assert record["status"] == "ok"
        assert record["toolless"] is True
        assert "slides one signal past the other" in str(record["response"])
        # One tools round (the refused one) plus the one plain completion.
        assert record["rounds"] == 2
        assert len(tool_rounds) == 1
        assert len(plain_calls) == 1
        # The refusal carried the case's tool surface, and the fallback re-planned the
        # same turn through the runner's own callback.
        assert tool_rounds[0] > 0
    finally:
        conn.close()


def test_grade_fails_an_incomplete_turn_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn whose loop never reached a terminal answer is a fail that says the
    product did not answer: named by the loop's stop reason and the calls it made, not
    waved away as a finding.
    """
    from backend.llm import tools as llm_tools

    config = TutorConfig("http://127.0.0.1:1234/v1", None, "tutor-model", 16384)
    monkeypatch.setattr(eval_tutor, "_endpoint_config", lambda path: config)
    (tmp_path / "runs.json").write_text(
        json.dumps(
            {
                "cases": {
                    "explain-convolution": {
                        "status": "incomplete",
                        "stopped": llm_tools.UPSTREAM_FAILED,
                        "detail": "the model endpoint could not be reached",
                        "tool_calls": [{"name": "cas_evaluate", "ok": True}],
                        "rounds": 2,
                        "response": "",
                        "user": "Explain convolution",
                        "mode": "guide",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        corpus=str(CORPUS),
        workspace=str(tmp_path),
        source_db="/ignored",
        case=None,
        judge_source_db=None,
        judge_model=None,
    )
    assert eval_tutor.cmd_grade(args) == 0
    grades = json.loads((tmp_path / "grades.json").read_text(encoding="utf-8"))
    entry = grades["explain-convolution"]
    assert entry["verdict"] == "fail"
    note = str(entry["grading"]["note"])
    assert "no terminal answer" in note
    assert str(llm_tools.UPSTREAM_FAILED) in note
    assert "cas_evaluate" in note


def test_evidence_redacts_locations_and_credentials_without_losing_math() -> None:
    raw = {
        "response": "Use x/2 = 3; https://secret.example/v1 is private.",
        "tool_calls": [
            {
                "arguments": {"expression": "x/2"},
                "result": {"path": "/Users/person/private.txt", "api_key": "hidden"},
            }
        ],
        "detail": "opaque-credential",
    }
    safe = eval_tutor._safe_evidence(raw, "opaque-credential")
    encoded = json.dumps(safe)
    for private in ("secret.example", "/Users/person", "hidden", "opaque-credential"):
        assert private not in encoded
    assert "x/2 = 3" in encoded
    assert safe["tool_calls"][0]["arguments"]["expression"] == "x/2"


def test_provenance_distinguishes_configured_capabilities_from_observation() -> None:
    config = TutorConfig("https://private.example/v1", "private-key", "example-model", 16384)
    provenance = eval_tutor._provenance(CORPUS, config)
    assert provenance["capabilities"] == {"stored_tools_supported": None}
    assert provenance["context_window"] == 16384
    assert provenance["review"]["human"] == "not_run"
    assert len(provenance["hashes"]["agent_prompt_source"]) == 64
    assert "private.example" not in json.dumps(provenance)
    assert "private-key" not in json.dumps(provenance)
