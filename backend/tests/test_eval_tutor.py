"""The tutor semantic eval harness, tested without a model.

An evaluation harness that grades wrongly is worse than none: it reports a number nobody
can check, and the prompt work gets tuned against it. So the parts that do not need a
model are tested the same way the product is - the corpus contract, the turn assembly,
and the verdict arithmetic - with the model calls left to `scripts/eval_tutor.py` at run
time.

Nothing here reaches an endpoint or the student's own database.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import routes_chat  # noqa: E402
from backend.llm.prompts import TUTOR_PROMPT_CONTRACT_VERSION, build_system_prompt  # noqa: E402
from backend.rag.retrieve import RetrievalResult, RetrievedChunk  # noqa: E402
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
