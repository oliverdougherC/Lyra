"""The tutor semantic eval harness, tested without a model.

An evaluation harness that grades wrongly is worse than none: it reports a number nobody
can check, and the prompt work gets tuned against it. So the parts that do not need a
model are tested the same way the product is - the corpus contract, the turn assembly,
and the verdict arithmetic - with the model calls left to `scripts/eval_tutor.py` at run
time.

Nothing here reaches an endpoint or the student's own database.
"""

import argparse
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
    assert "endpoint down" in record["error"]

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
    # The agent capability layer, appended on top of it.
    assert "You are Lyra's class agent" in system
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
