"""The section index's contracts.

The one that matters most is the round trip: splicing a section's own text back is the
identity, byte for byte, on every document in the corpus. Every tool and every pipeline
stage addresses the draft through these functions, so an off-by-one here is a corrupted
draft there.
"""

import pytest

from backend.core import sections

# A document exercising the whole grammar: a preamble, nesting, an empty TODO section,
# a fence hiding a fake heading, and a final section with no trailing newline.
DOC = (
    "Intro paragraph before any heading.\n"
    "\n"
    "# Introduction\n"
    "\n"
    "Opening prose, two sentences of it.\n"
    "\n"
    "## Background\n"
    "\n"
    "Some background words here.\n"
    "\n"
    "# Methods\n"
    "\n"
    "## Setup\n"
    "\n"
    "[TODO: describe the rig]\n"
    "\n"
    "## Procedure\n"
    "\n"
    "Steps described in order.\n"
    "\n"
    "```markdown\n"
    "# not a heading\n"
    "```\n"
    "\n"
    "# Results\n"
)


def test_parse_numbers_sections_hierarchically_in_document_order() -> None:
    parsed = sections.parse(DOC)

    assert [(s.number, s.title) for s in parsed] == [
        ("0", sections.PREAMBLE_TITLE),
        ("1", "Introduction"),
        ("1.1", "Background"),
        ("2", "Methods"),
        ("2.1", "Setup"),
        ("2.2", "Procedure"),
        ("3", "Results"),
    ]


def test_every_span_is_exact_and_top_level_spans_partition_the_document() -> None:
    parsed = sections.parse(DOC)

    for section in parsed:
        assert DOC[section.start : section.end] == section.text
    # The preamble and the level-1 sections tile the document with no gaps, which is
    # what makes span-based splicing safe at all.
    top = [s for s in parsed if s.level <= 1]
    assert "".join(s.text for s in top) == DOC


def test_a_heading_inside_a_fence_is_code_not_a_section() -> None:
    parsed = sections.parse(DOC)

    assert all(s.title != "not a heading" for s in parsed)
    # The fence belongs to the section that contains it.
    procedure = sections.extract(DOC, "2.2")
    assert procedure is not None
    assert "```markdown" in procedure.text


def test_a_todo_only_section_is_empty_and_a_prose_section_is_not() -> None:
    setup = sections.extract(DOC, "2.1")
    background = sections.extract(DOC, "1.1")

    assert setup is not None and setup.is_empty is True
    assert background is not None and background.is_empty is False


def test_a_parent_with_prose_only_in_its_children_is_not_empty() -> None:
    # "Methods" has no prose of its own, but replacing it would replace 2.2's prose, so
    # the direct-write rule must see it as occupied.
    methods = sections.extract(DOC, "2")

    assert methods is not None
    assert methods.is_empty is False


def test_word_count_excludes_heading_lines() -> None:
    background = sections.extract(DOC, "1.1")

    assert background is not None
    assert background.word_count == 4  # "Some background words here."


def test_extract_addresses_by_number_title_and_partial_title() -> None:
    assert sections.extract(DOC, "2.1").title == "Setup"  # type: ignore[union-attr]
    assert sections.extract(DOC, "2.").title == "Methods"  # type: ignore[union-attr]
    assert sections.extract(DOC, "methods").title == "Methods"  # type: ignore[union-attr]
    assert sections.extract(DOC, "proc").title == "Procedure"  # type: ignore[union-attr]
    assert sections.extract(DOC, "no such section") is None
    assert sections.extract(DOC, "  ") is None


def test_splicing_a_sections_own_text_back_is_the_identity() -> None:
    for section in sections.parse(DOC):
        assert sections.splice(DOC, section, section.text) == DOC


def test_splicing_a_replacement_touches_nothing_outside_the_section() -> None:
    background = sections.extract(DOC, "1.1")
    assert background is not None
    replacement = "## Background\n\nRewritten entirely.\n\n"

    spliced = sections.splice(DOC, background, replacement)

    assert "Some background words here." not in spliced
    assert "Rewritten entirely." in spliced
    # Everything outside the span survived byte for byte.
    assert spliced[: background.start] == DOC[: background.start]
    assert spliced.endswith(DOC[background.end :])


def test_splicing_a_parent_replaces_its_children_too() -> None:
    methods = sections.extract(DOC, "2")
    assert methods is not None

    spliced = sections.splice(DOC, methods, "# Methods\n\nOne flat section now.\n\n")

    parsed = sections.parse(spliced)
    assert [s.number for s in parsed if s.level == 2] == ["1.1"]
    assert "Steps described in order." not in spliced


def test_splicing_a_stale_section_raises_instead_of_corrupting() -> None:
    background = sections.extract(DOC, "1.1")
    assert background is not None
    moved = DOC.replace("Intro paragraph", "A longer intro paragraph")

    with pytest.raises(ValueError, match="stale"):
        sections.splice(moved, background, "anything")


def test_setext_headings_normalize_to_levels_one_and_two() -> None:
    body = "Title\n=====\n\nProse under the title.\n\nSub\n---\n\nMore prose.\n"

    parsed = sections.parse(body)

    assert [(s.number, s.title, s.level) for s in parsed] == [
        ("1", "Title", 1),
        ("1.1", "Sub", 2),
    ]
    # The underline is furniture, not body text.
    assert parsed[0].word_count == 4 + 2


def test_a_thematic_break_after_a_blank_line_is_not_a_heading() -> None:
    body = "# One\n\nProse.\n\n---\n\nMore prose.\n"

    parsed = sections.parse(body)

    assert [s.title for s in parsed] == ["One"]


def test_crlf_bodies_keep_exact_spans_and_round_trip() -> None:
    body = DOC.replace("\n", "\r\n")

    parsed = sections.parse(body)

    assert [s.number for s in parsed] == ["0", "1", "1.1", "2", "2.1", "2.2", "3"]
    for section in parsed:
        assert body[section.start : section.end] == section.text
        assert sections.splice(body, section, section.text) == body


def test_skipped_heading_levels_still_nest() -> None:
    body = "# Top\n\n### Deep\n\nProse.\n"

    parsed = sections.parse(body)

    assert [(s.number, s.level) for s in parsed] == [("1", 1), ("1.1", 3)]


def test_an_unclosed_fence_swallows_the_rest_of_the_document() -> None:
    body = "# One\n\n```\n# swallowed\n"

    parsed = sections.parse(body)

    assert [s.title for s in parsed] == ["One"]


def test_a_tilde_fence_is_not_closed_by_backticks() -> None:
    body = "# One\n\n~~~\n```\n# still code\n~~~\n\n# Two\n"

    parsed = sections.parse(body)

    assert [s.title for s in parsed] == ["One", "Two"]


def test_an_empty_body_has_no_sections_and_says_so_in_the_outline() -> None:
    assert sections.parse("") == []
    assert sections.outline("") == "The document is empty."
    assert sections.parse("   \n\n") == []


def test_an_unheaded_body_is_one_preamble_section() -> None:
    body = "Just prose, no headings at all.\n"

    parsed = sections.parse(body)

    assert len(parsed) == 1
    assert parsed[0].number == sections.PREAMBLE_NUMBER
    assert parsed[0].text == body


def test_outline_renders_numbers_indentation_and_empty_flags() -> None:
    rendered = sections.outline(DOC)
    lines = rendered.splitlines()

    assert lines[0] == f"0 {sections.PREAMBLE_TITLE} (5 words)"
    assert "1 Introduction (" in lines[1]
    assert lines[2].startswith("  1.1 Background")
    assert "2.1 Setup (empty)" in rendered
    assert lines[-1] == "3 Results (empty)"


def test_a_todo_escaped_by_the_editor_still_reads_as_empty() -> None:
    # Milkdown re-serializes `[TODO: ...]` as `\[TODO: ...]` (a bare bracket could open
    # a link), and a document that has been through the editor is every document.
    body = "# One\n\n\\[TODO: describe the rig]\n\n# Two\n\nProse.\n"

    parsed = sections.parse(body)

    assert parsed[0].is_empty is True
    assert parsed[1].is_empty is False


def test_heading_lines_are_the_document_text_not_the_outline() -> None:
    """What a reviewer quotes has to exist in the document.

    The outline is rendered for navigation - `1 Introduction (7 words)` is nowhere in the
    body - so a lens handed only the outline writes quotes that cannot anchor.
    """
    rendered = sections.heading_lines(DOC)
    lines = rendered.splitlines()

    assert "# Introduction" in lines
    assert "## Background" in lines
    # The preamble is not a heading, and nothing carries a number or a word count.
    assert sections.PREAMBLE_TITLE not in rendered
    assert "words)" not in rendered
    assert all(line.lstrip().startswith("#") for line in lines), lines
    # Every line is quotable against the body it came from.
    assert all(line in DOC for line in lines)
