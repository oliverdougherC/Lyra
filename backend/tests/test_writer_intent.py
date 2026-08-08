"""Deterministic checks for writer-chat intent routing and tool contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backend.core import writer_intent

CORPUS = Path(__file__).with_name("evals") / "writer_turn_contract.v1.json"


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    request: str
    intent: str
    tools: tuple[str, ...]
    failed_tools: tuple[str, ...]
    satisfied: bool
    complete: bool = True


def _load_cases() -> list[Case]:
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))
    raw = payload.get("cases")
    assert isinstance(raw, list) and raw
    return [
        Case(
            name=str(entry["name"]),
            request=str(entry["request"]),
            intent=str(entry["intent"]),
            tools=tuple(str(tool) for tool in entry.get("tools", ())),
            failed_tools=tuple(str(tool) for tool in entry.get("failed_tools", ())),
            satisfied=bool(entry["satisfied"]),
            complete=bool(entry.get("complete", True)),
        )
        for entry in raw
    ]


def test_writer_turn_contract_corpus_is_classified_and_scored_as_declared() -> None:
    cases = _load_cases()

    for case in cases:
        assert writer_intent.classify(case.request) == case.intent, case.name
        checked = writer_intent.validate(case.intent, case.tools, complete=case.complete)
        assert checked.satisfied is case.satisfied, case.name
        assert not set(case.tools).intersection(case.failed_tools), case.name


def test_contract_prompt_names_the_expected_tool_path() -> None:
    assert "start_draft_pass" in writer_intent.prompt_contract(writer_intent.DRAFT)
    assert "propose_revision" in writer_intent.prompt_contract(writer_intent.REVISE)
    assert "start_review" in writer_intent.prompt_contract(writer_intent.REVIEW)
    assert "search the course material" in writer_intent.prompt_contract(writer_intent.RESEARCH)
    assert "Answer directly in chat" in writer_intent.prompt_contract(writer_intent.QUESTION)


def test_revise_contract_accepts_a_background_pass_for_whole_document_polish() -> None:
    checked = writer_intent.validate(
        writer_intent.REVISE,
        (),
        pass_started=True,
    )

    assert checked.satisfied is True


def test_failed_tools_do_not_satisfy_a_research_contract_when_only_successes_are_passed() -> None:
    checked = writer_intent.validate(writer_intent.RESEARCH, ())

    assert checked.satisfied is False


def test_classifier_handles_section_draft_and_interrogative_edge_cases() -> None:
    assert writer_intent.classify("Write a conclusion for this essay.") == writer_intent.DRAFT
    assert writer_intent.classify("Write the discussion section.") == writer_intent.DRAFT
    assert writer_intent.classify("Can you write the conclusion?") == writer_intent.DRAFT
    assert writer_intent.classify("Can you draft the discussion section?") == writer_intent.DRAFT
    assert writer_intent.classify("Could you write the abstract?") == writer_intent.DRAFT
    assert writer_intent.classify("Can you revise the conclusion?") == writer_intent.REVISE
    assert writer_intent.classify("How can I improve the conclusion?") == writer_intent.QUESTION
    assert (
        writer_intent.classify("What does peer review mean in this class?")
        == writer_intent.QUESTION
    )
    assert (
        writer_intent.classify("Research and draft the literature review.") == writer_intent.DRAFT
    )
