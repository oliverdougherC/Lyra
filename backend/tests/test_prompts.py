"""Contract tests for prompt construction."""

import json
import re
import sqlite3

import pytest

from backend.llm import prompts
from backend.llm.prompts import (
    EXTRACTION_PROFILES,
    MAX_FACTS_PER_KIND,
    PLAN_ARGUMENT_SCHEMA,
    build_consolidation_prompt,
    build_extraction_prompt,
    build_section_prompt,
    build_segmentation_prompt,
    build_skeptic_prompt,
    build_solve_prompt,
    build_system_prompt,
    build_writer_chat_prompt,
    extraction_schema,
    format_brief_block,
    format_context_block,
    format_ledger_block,
    format_plan_block,
)


def _insert_fact(
    db: sqlite3.Connection,
    class_id: int,
    kind: str,
    label: str,
    value: str,
) -> None:
    """Add one high-confidence fact, mirroring what an accepted extraction leaves behind."""
    db.execute(
        "insert into profile_facts (class_id, kind, label, value, confidence, confirmed) "
        "values (?, ?, ?, ?, 'high', 1)",
        (class_id, kind, label, value),
    )
    db.commit()


def _active_facts(db: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    """Stand in for select_active_facts: the prompt builder takes rows, not dicts."""
    return list(
        db.execute(
            "select kind, label, value, confidence, confirmed from profile_facts "
            "where class_id = ? and rejected = 0 order by id",
            (class_id,),
        )
    )


def _normalized(text: str) -> str:
    """Collapse wrapping so an assertion does not depend on where a line breaks."""
    return re.sub(r"\s+", " ", text).lower()


def test_plan_and_ledger_context_bind_drafting_to_stable_citation_ids() -> None:
    plan = {
        "thesis": "Length controls period.",
        "sections": [
            {
                "section_ref": "1.1",
                "title": "Results",
                "job": "Establish the relationship",
                "claim": "Period rises with length",
                "source_ids": [7],
                "word_budget": 300,
            },
            {"section_ref": "1.2", "title": "Conclusion", "job": "Close"},
        ],
    }
    plan_block = format_plan_block(plan, "1.1")
    ledger_block = format_ledger_block([{"id": 7, "title": "Lab handout", "source_type": "course"}])
    messages = build_section_prompt(
        "Pendulum",
        "1.1 Results",
        "## Results\n\n[TODO: write results]",
        None,
        None,
        None,
        "",
        "",
        "",
        plan_block=plan_block,
        ledger_block=ledger_block,
    )
    text = messages[-1]["content"]

    assert "Establish the relationship" in text
    assert "Conclusion" not in plan_block
    assert "[@lyra:<ID>]" in text
    assert "invent, renumber" in text


def test_the_structured_skeptic_reads_the_section_job_and_ledger() -> None:
    messages = build_skeptic_prompt(
        "Essay",
        "## Evidence\n\nA claim [@lyra:3].",
        "Persistent writing plan: do the evidence job",
        "Source ledger: id 3",
    )
    rendered = "\n".join(message["content"] for message in messages)

    assert "performs its planned job" in rendered
    assert "do the evidence job" in rendered
    assert "Source ledger: id 3" in rendered


def test_argument_map_schema_matches_the_public_list_contract() -> None:
    assert PLAN_ARGUMENT_SCHEMA.schema["type"] == "array"
    assert PLAN_ARGUMENT_SCHEMA.schema["items"]["required"] == ["id", "claim", "supports"]


def test_claims_review_requires_ledger_verification_for_web_and_course_sources() -> None:
    messages = prompts.build_review_claims_prompt(
        "Essay",
        "## Evidence\n\nA claim [@lyra:3].",
        "",
        ledger_block="Source ledger: web source 3",
    )
    rendered = "\n".join(message["content"] for message in messages)

    assert "cannot be checked here" not in rendered
    assert "cited ledger entry" in rendered


def test_guide_withholds_the_answer_and_show_does_not() -> None:
    guide = build_system_prompt("guide", [], [])
    show = build_system_prompt("show", [], [])

    assert guide != show
    assert "do not give the final answer immediately" in _normalized(guide)
    assert "do not withhold the answer" in _normalized(show)
    assert "$$...$$ on its own line for a displayed equation" in _normalized(guide)
    assert "$...$ for a quantity inside a line of text" in _normalized(guide)


def test_the_prompt_forbids_opening_every_reply_by_citing_the_material() -> None:
    prompt = _normalized(build_system_prompt("guide", [], []))

    assert "according to the course materials" in prompt
    assert "never open by narrating where your information came from" in prompt
    # Citing a source is still wanted where the citation carries information.
    assert "name a source only when the citation is part of the answer" in prompt


def test_facts_render_one_heading_per_kind(db: sqlite3.Connection, class_id: int) -> None:
    _insert_fact(db, class_id, "deadline", "Midterm 1", "2026-03-04")
    _insert_fact(db, class_id, "topic", "Series", "Convergence tests")

    prompt = build_system_prompt("guide", [], _active_facts(db, class_id))

    assert "Deadlines:\n- Midterm 1: 2026-03-04" in prompt
    assert "Topics:\n- Series: Convergence tests" in prompt
    assert "Grading:" not in prompt


def test_a_topic_is_not_printed_under_a_label_that_repeats_its_heading(
    db: sqlite3.Connection, class_id: int
) -> None:
    """`Topics:` then `- Topic: Convolution` spends tokens to say the same word twice."""
    _insert_fact(db, class_id, "topic", "Topic", "Convolution")

    prompt = build_system_prompt("guide", [], _active_facts(db, class_id))

    assert "Topics:\n- Convolution" in prompt


def test_each_kind_is_capped_so_a_large_course_cannot_crowd_out_the_prompt(
    db: sqlite3.Connection, class_id: int
) -> None:
    for index in range(MAX_FACTS_PER_KIND + 8):
        _insert_fact(db, class_id, "topic", "Topic", f"Topic number {index:02d}")

    prompt = build_system_prompt("guide", [], _active_facts(db, class_id))

    assert prompt.count("- Topic number") == MAX_FACTS_PER_KIND
    # The cap falls on the tail, so the rows the caller ordered first are the ones kept.
    assert "Topic number 00" in prompt
    assert f"Topic number {MAX_FACTS_PER_KIND + 7:02d}" not in prompt


def test_no_facts_renders_no_fact_section() -> None:
    prompt = build_system_prompt("guide", [], [])

    assert "about this class" not in _normalized(prompt)
    assert "Deadlines:" not in prompt


def test_a_syllabus_is_asked_for_every_kind_and_for_bare_json() -> None:
    messages = build_extraction_prompt("Syllabus text", "syllabus")

    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    for field in ("deadlines", "topics", "professor_info", "grading", "prerequisites", "notes"):
        assert f'"{field}"' in system
    assert '"quote"' in system
    assert "no code fence" in system
    assert messages[1] == {"role": "user", "content": "Syllabus text"}


@pytest.mark.parametrize(
    ("doc_type", "forbidden"),
    [
        ("homework", ("grading", "professor_info", "prerequisites")),
        ("solutions", ("deadlines", "grading", "professor_info", "prerequisites")),
        ("exam", ("deadlines", "grading", "professor_info", "prerequisites")),
        ("textbook", ("deadlines", "grading", "professor_info", "prerequisites")),
        ("generic", ("deadlines", "grading", "professor_info", "prerequisites")),
    ],
)
def test_a_field_a_document_cannot_honestly_fill_is_never_asked_for(
    doc_type: str, forbidden: tuple[str, ...]
) -> None:
    """The fix for instructor names on problem sets, and it is a subtraction.

    The old prompt asked every document for all six kinds and then spent a paragraph
    asking for restraint. A field list is a much stronger signal to a small model than a
    paragraph is, so asking a homework sheet for `professor_info` was an instruction to go
    and find one. The word now appears nowhere in the prompt at all.
    """
    system = build_extraction_prompt("text", doc_type)[0]["content"]

    for field in forbidden:
        assert field not in system, f"{doc_type} must not be asked for {field}"
    assert '"topics"' in system


def test_a_reused_document_is_told_its_dates_and_names_belong_to_another_course() -> None:
    """Practice exams and answer keys are the documents most likely to be someone else's."""
    for doc_type in ("solutions", "exam"):
        system = build_extraction_prompt("text", doc_type)[0]["content"]
        assert "reused between terms and between courses" in system
        assert "Record none of them" in system


def test_an_unknown_document_type_gets_the_careful_profile_not_the_permissive_one() -> None:
    """Textbook and generic used to fall through to a prompt with no guidance at all."""
    unknown = build_extraction_prompt("text", "something-nobody-has-heard-of")[0]["content"]

    assert unknown == build_extraction_prompt("text", "generic")[0]["content"]
    assert "could not be determined" in unknown


@pytest.mark.parametrize("doc_type", list(EXTRACTION_PROFILES))
def test_every_example_is_valid_json_shaped_exactly_like_the_schema(doc_type: str) -> None:
    """The example is generated from the field list, so it cannot drift from the schema.

    A hand-written example that shows a field the schema forbids is worse than no example:
    under constrained decoding the model is shown one shape and permitted another.
    """
    system = build_extraction_prompt("text", doc_type)[0]["content"]
    example = json.loads(system.split("An example of a well-formed reply:", 1)[1].split("\n\n")[1])
    schema = extraction_schema(doc_type).schema

    assert set(example) == set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False
    for field, entries in example.items():
        item = schema["properties"][field]["items"]
        assert item["additionalProperties"] is False
        assert "quote" in item["required"]
        for entry in entries:
            assert set(entry) == set(item["required"])


@pytest.mark.parametrize("doc_type", list(EXTRACTION_PROFILES))
def test_every_document_type_is_told_an_empty_list_is_a_real_answer(doc_type: str) -> None:
    """Models fill fields. Being shown [] is what makes returning it thinkable."""
    system = build_extraction_prompt("text", doc_type)[0]["content"]

    assert "An empty list is a correct and expected answer" in system
    assert '"notes": []' in system


def test_extraction_prompt_names_the_class_and_the_document_type() -> None:
    course = {"name": "Continuous-Signal Processing", "code": "ECE203", "semester": "Winter 2026"}

    system = build_extraction_prompt("Sheet text", "homework", course)[0]["content"]

    assert "ECE203" in system
    assert "Continuous-Signal Processing" in system
    assert "Winter 2026" in system
    assert "do not report any part of it back as a fact you found" in system
    assert "homework assignment" in system

    # An unknown type and an unnamed class add nothing rather than an empty sentence.
    bare = build_extraction_prompt("Sheet text", "generic", None)[0]["content"]
    assert "already recorded this class" not in bare


def test_consolidation_prompt_numbers_the_entries_and_forbids_renaming() -> None:
    messages = build_consolidation_prompt(["[topic] Time Shift", "[topic] Time-Shift Property"])

    system = messages[0]["content"]
    assert '"duplicates"' in system
    assert '"not_about_the_course"' in system
    assert "Do not invent entries, do not rename anything" in system
    assert messages[1]["content"] == "1. [topic] Time Shift\n2. [topic] Time-Shift Property"


def test_segmentation_asks_for_latex_without_licensing_a_rewrite() -> None:
    """The two halves of this instruction have to arrive together.

    PDF extraction flattens exponents, so a statement copied character for character is
    already not what the sheet says, and the review gate is where the student compares the
    two. Asking for LaTeX restores that. Asking for it without repeating the verbatim rule
    invites a tidied-up paraphrase instead, which is the failure the gate exists to catch.
    """
    messages = build_segmentation_prompt("Q1. Compute X(jw).", "homework_5.pdf")

    # Collapsed, because the prompt is hard-wrapped and which words share a line is not
    # part of the contract.
    system = re.sub(r"\s+", " ", messages[0]["content"])
    assert "$...$" in system
    assert "$$...$$" in system
    assert "Copy statements verbatim" in system
    assert "change nothing else" in system
    assert messages[1]["content"].startswith("File: homework_5.pdf")


def test_empty_context_block_is_empty_string() -> None:
    assert format_context_block([]) == ""


def test_context_block_labels_source_page_and_problem() -> None:
    block = format_context_block(
        [
            {
                "content": "Evaluate the integral.",
                "filename": "hw3.pdf",
                "page_number": 2,
                "section_title": None,
                "problem_number": "4",
            }
        ]
    )

    assert "hw3.pdf" in block
    assert "page 2" in block
    assert "problem 4" in block
    assert "Evaluate the integral." in block


def test_segmentation_asks_how_a_problems_parts_relate() -> None:
    """The field that decides whether a section is one problem or five.

    Both readings have to be described in the sheet's own terms, because the model is
    being asked to recognise a shape rather than to apply a rule about part counts.
    """
    system = re.sub(r"\s+", " ", build_segmentation_prompt("Q1.", "hw.pdf")[0]["content"])

    assert '"parts_relation"' in system
    assert '"separate" when each part is its own question with its own final answer' in system
    assert '"one_solution" when the parts build a single solution' in system
    assert "Five parts can be one derivation and two parts can be two questions." in system


def test_a_part_solved_alone_is_sent_the_sentence_that_asks_something_of_it() -> None:
    """`(b) $y(t) = x^2(t)$` is not a question. Under its stem it is."""
    stem = "For each system below, determine whether the system is linear."
    messages = build_solve_prompt(
        "$y(t) = x^2(t)$", "Linearity and Time-Invariance (b)", preamble=stem
    )

    turn = messages[1]["content"]
    assert stem in turn
    assert "$y(t) = x^2(t)$" in turn
    # And told to answer for itself alone: its neighbours are other turns, and a solution
    # that answered all five would be printed five times over, once under each part.
    assert "Solve this part only." in turn


def test_a_whole_problem_is_sent_exactly_as_it_always_was() -> None:
    messages = build_solve_prompt("Find $X(j\\omega)$.", "Problem 4")

    assert messages[1]["content"] == "Problem 4\n\nFind $X(j\\omega)$."


def test_brief_block_renders_fields_and_flags_an_unconfirmed_guess() -> None:
    block = format_brief_block(
        {
            "assignment_type": "lab report",
            "summary": "Pendulum period vs length.",
            "audience": "",
            "length_target": "5 pages",
            "status": "proposed",
        }
    )

    assert block.startswith("What this document is:")
    assert "- Assignment: lab report" in block
    assert "- Length: 5 pages" in block
    assert "Audience" not in block
    assert "not confirmed" in block


def test_a_confirmed_brief_carries_no_caveat() -> None:
    block = format_brief_block({"summary": "An essay.", "status": "confirmed"})

    assert "not confirmed" not in block


def test_no_brief_and_an_all_blank_brief_render_as_nothing() -> None:
    assert format_brief_block(None) == ""
    assert format_brief_block({"summary": "  ", "status": "proposed"}) == ""


def test_writer_chat_prompt_orients_grounds_and_never_hands_over_the_pen() -> None:
    prompt = build_writer_chat_prompt(
        "Lab 3",
        format_brief_block({"summary": "Pendulum lab.", "status": "confirmed"}),
        "1 Introduction (12 words)\n2 Methods (empty)",
        "",
    )

    assert '"Lab 3"' in prompt
    assert "Pendulum lab." in prompt
    assert "2 Methods (empty)" in prompt
    # The two behaviors everything else hangs off: proposals not edits, and the brief.
    assert "never say you changed the document" in _normalized(prompt)
    assert "save_brief" in prompt
    # The craft bar rides along, same one the drafting prompts hold to.
    assert "Cut surplusage" in prompt


def test_writer_chat_prompt_omits_empty_blocks() -> None:
    prompt = build_writer_chat_prompt("Essay", "", "The document is empty.", "")

    assert "What this document is:" not in prompt
    assert "about this class" not in prompt
    assert "The document right now:\nThe document is empty." in prompt
