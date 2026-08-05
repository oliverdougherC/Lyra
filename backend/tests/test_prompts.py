"""Contract tests for prompt construction."""

import re
import sqlite3

from backend.llm.prompts import (
    MAX_FACTS_PER_KIND,
    build_consolidation_prompt,
    build_extraction_prompt,
    build_segmentation_prompt,
    build_solve_prompt,
    build_system_prompt,
    format_context_block,
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


def test_guide_withholds_the_answer_and_show_does_not() -> None:
    guide = build_system_prompt("guide", [], [])
    show = build_system_prompt("show", [], [])

    assert guide != show
    assert "do not give the final answer immediately" in _normalized(guide)
    assert "do not withhold the answer" in _normalized(show)
    assert "$$...$$ on their own line for display math" in _normalized(guide)
    assert "reserve $...$ for short inline quantities" in _normalized(guide)


def test_the_prompt_forbids_opening_every_reply_by_citing_the_material() -> None:
    prompt = _normalized(build_system_prompt("guide", [], []))

    assert "according to the course materials" in prompt
    assert "do not open a reply by narrating where your information came from" in prompt
    # Citing a source is still wanted where the citation carries information.
    assert "cite a source when the citation is itself part of the answer" in prompt


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


def test_extraction_prompt_asks_for_every_kind_and_for_bare_json() -> None:
    messages = build_extraction_prompt("Syllabus text")

    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    for field in ("deadlines", "topics", "professor_info", "grading", "prerequisites", "notes"):
        assert f'"{field}"' in system
    assert '"confidence"' in system
    assert "no code fence" in system
    assert messages[1] == {"role": "user", "content": "Syllabus text"}


def test_extraction_prompt_rules_out_what_every_document_repeats() -> None:
    """The exclusions are the point: sixteen uploads must not propose the course code
    sixteen times."""
    system = build_extraction_prompt("Syllabus text")[0]["content"]

    assert "Leave out anything that describes the document rather than the course" in system
    for excluded in ("its assignment number", "its course code", "how many problems"):
        assert excluded in system


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
    assert "Do not invent entries and do not rename anything." in system
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
