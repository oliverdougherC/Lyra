"""Contract tests for prompt construction."""

import re
import sqlite3

from backend.llm.prompts import build_extraction_prompt, build_system_prompt, format_context_block


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


def test_no_facts_renders_no_fact_section() -> None:
    prompt = build_system_prompt("guide", [], [])

    assert "about this class" not in _normalized(prompt)
    assert "Deadlines:" not in prompt


def test_extraction_prompt_quotes_the_specified_block_and_asks_for_bare_json() -> None:
    messages = build_extraction_prompt("Syllabus text")

    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert (
        "Return JSON with these fields: deadlines[], topics[], professor_info{}, grading{},\n"
        "prerequisites[], notes[]" in system
    )
    assert '"confidence"' in system
    assert "no code fence" in system
    assert messages[1] == {"role": "user", "content": "Syllabus text"}


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
