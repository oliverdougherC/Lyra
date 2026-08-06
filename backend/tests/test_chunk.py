"""Guards for chunk boundaries, the token ceiling, and oversized-problem splitting.

The ceiling is the load-bearing one. An oversized chunk raises nothing: the embedding
server truncates it, the vector silently describes only part of the text, and retrieval
gets worse with no symptom. Every strategy is therefore checked against it here, not
just the ones with obvious splitting logic.
"""

import pytest

from backend.rag.chunk import (
    CHUNK_RULES,
    GENERIC,
    HOMEWORK,
    LECTURE_NOTES,
    MAX_CHUNK_TOKENS,
    PROBLEM_MARKER,
    SYLLABUS,
    TEXTBOOK,
    Chunk,
    chunk_document,
    detect_doc_type,
)
from backend.rag.parse import OutlineEntry, ParsedDocument, ParsedPage
from backend.rag.tokens import CHARS_PER_TOKEN, estimate_tokens


def _parsed(*pages: str) -> ParsedDocument:
    """A parsed document whose pages are numbered from 1, nothing skipped."""
    return ParsedDocument(
        pages=[ParsedPage(page_number=number, text=text) for number, text in enumerate(pages, 1)],
        pages_total=len(pages),
        pages_skipped=0,
    )


def _book(pages: int = 200, entries: int = 40, depth: int = 3) -> ParsedDocument:
    """A document shaped like a textbook: long, with a deep outline over many entries."""
    outline = [
        OutlineEntry(depth=(index % depth) + 1, title=f"Section {index}", page_number=index + 1)
        for index in range(entries)
    ]
    return ParsedDocument(
        pages=[ParsedPage(page_number=number, text=f"page {number}") for number in range(1, 4)],
        pages_total=pages,
        pages_skipped=0,
        outline=outline,
    )


def _filler(tokens: int, seed: int = 0) -> str:
    """Deterministic prose of roughly `tokens` estimated tokens, on one line."""
    words: list[str] = []
    length = 0
    index = 0
    while length < tokens * CHARS_PER_TOKEN:
        word = f"w{(index + seed) % 97:02d}"
        words.append(word)
        length += len(word) + 1
        index += 1
    return " ".join(words)


def _overlap_chars(first: str, second: str) -> int:
    """Length of the longest suffix of `first` that `second` begins with."""
    for width in range(min(len(first), len(second)), 0, -1):
        if second.startswith(first[-width:]):
            return width
    return 0


def _assert_well_formed(chunks: list[Chunk]) -> None:
    """Every invariant `chunk_document` promises, regardless of document type."""
    assert chunks
    for chunk in chunks:
        assert chunk.content == chunk.content.strip() != ""
        assert chunk.token_count == estimate_tokens(chunk.content)
        assert chunk.token_count <= MAX_CHUNK_TOKENS


def test_oversized_homework_problem_splits_into_capped_numbered_parts() -> None:
    subparts = "".join(
        f"({letter}) {_filler(950, seed=index)}\n\n" for index, letter in enumerate("abcdef")
    )
    problem = f"Problem 3. Consider the following system.\n\n{subparts}"
    assert estimate_tokens(problem) > 5500

    chunks = chunk_document(_parsed(problem), HOMEWORK)

    _assert_well_formed(chunks)
    assert {chunk.problem_number for chunk in chunks} == {"3"}
    assert [chunk.part_index for chunk in chunks] == list(range(len(chunks)))
    # It split on the sub-parts rather than mid-sentence, so each part opens with one.
    openers = [chunk.content[:3] for chunk in chunks]
    assert openers[1:] == ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)"]


def test_oversized_problem_without_subparts_falls_back_to_overlapping_paragraphs() -> None:
    body = "\n\n".join(_filler(300, seed=index) for index in range(20))
    problem = f"1. Prove each statement below.\n\n{body}"
    assert estimate_tokens(problem) > 5500

    chunks = chunk_document(_parsed(problem), HOMEWORK)

    _assert_well_formed(chunks)
    assert {chunk.problem_number for chunk in chunks} == {"1"}
    assert [chunk.part_index for chunk in chunks] == list(range(len(chunks)))
    # The paragraph fallback repeats 100 tokens across the seam so a step split between
    # two parts is still readable in both.
    overlap = _overlap_chars(chunks[0].content, chunks[1].content)
    assert 80 * CHARS_PER_TOKEN <= overlap <= 100 * CHARS_PER_TOKEN


def test_a_problem_that_fits_is_one_chunk_with_no_part_index() -> None:
    homework = (
        "MATH 201 Homework 3\nDue Friday.\n\n"
        "1. Differentiate x squared.\n\n"
        "2) Integrate sin x over the unit interval.\n\n"
        "Problem 3. Prove the intermediate value theorem.\n\n"
        "Q4: State the definition of a limit.\n"
    )

    chunks = chunk_document(_parsed(homework), HOMEWORK)

    _assert_well_formed(chunks)
    # The header belongs to no problem, then one chunk per problem, none of them parts.
    assert [chunk.problem_number for chunk in chunks] == [None, "1", "2", "3", "4"]
    assert all(chunk.part_index is None for chunk in chunks)
    assert chunks[2].content.startswith("2) Integrate")


def test_a_problem_numbered_without_a_full_stop_is_still_a_problem() -> None:
    """`Problem 1 (Time Shift)` is how a large share of real sheets number their problems.

    Requiring the delimiter made every problem on such a sheet invisible: measured against
    a real signals course, two of eight sets came back with no markers at all, and
    segmentation fell back to paragraph grouping for the whole document.
    """
    homework = (
        "ECE203 Homework 7\nFourier Transform Properties\n\n"
        "Problem 1 (Time Shift + Differentiation)\nFind the transform of x(t).\n\n"
        "Problem 2 (Scaling)\nFind the transform of y(t).\n\n"
        "Exercise 3.14 Prove the shifting property.\n"
    )

    chunks = chunk_document(_parsed(homework), HOMEWORK)

    _assert_well_formed(chunks)
    assert [chunk.problem_number for chunk in chunks] == [None, "1", "2", "3.14"]


def test_numbered_sub_items_under_a_named_problem_stay_with_it() -> None:
    """`Problem 1` above five numbered questions is one problem, not six.

    Both are numbered markers, and taking every one of them split a real sampling problem
    into six chunks each holding one line of a question meant to be answered as a whole.
    """
    homework = (
        "Sampling\n\n"
        "Problem 1\nA signal x(t) = cos(200 pi t) is sampled.\n"
        "1. Determine the highest frequency present.\n"
        "2. Find the Nyquist rate.\n\n"
        "Problem 2\nA second signal is sampled.\n"
        "1. Determine its frequency.\n"
    )

    chunks = chunk_document(_parsed(homework), HOMEWORK)

    _assert_well_formed(chunks)
    assert [chunk.problem_number for chunk in chunks] == [None, "1", "2"]
    assert "Nyquist" in str(chunks[1].content)


def test_a_sheet_that_mixes_both_notations_keeps_both() -> None:
    # A sheet that opens with `1.` and later writes `Problem 3` is using two notations for
    # the same kind of thing. Only a bare number sitting *under* a named one is a sub-item.
    homework = "1. Differentiate.\n\n2) Integrate.\n\nProblem 3. Prove it.\n"

    chunks = chunk_document(_parsed(homework), HOMEWORK)

    assert [chunk.problem_number for chunk in chunks] == ["1", "2", "3"]


@pytest.mark.parametrize(
    "line",
    [
        "2.5 kg of salt dissolves in the tank\n",
        "15 minutes after the switch closes\n",
        "1\n",
        "5 points\n",
    ],
)
def test_a_loose_number_at_the_start_of_a_line_is_not_a_problem(line: str) -> None:
    # The delimiter stays required of a bare number. A line opening with a quantity is far
    # more common than one opening with an unpunctuated problem number.
    assert PROBLEM_MARKER.search(line) is None


def test_syllabus_is_detected_by_filename_and_chunked_at_its_target_and_overlap() -> None:
    schedule = "\n\n".join(_filler(300, seed=week) for week in range(12))
    text = (
        "MATH 201 Course Syllabus\n\n"
        f"## Course Description\n\n{_filler(300)}\n\n"
        f"## Grading\n\n{_filler(200, seed=7)}\n\n"
        f"## Weekly Schedule\n\n{schedule}\n\n"
        f"## Office Hours\n\n{_filler(120, seed=11)}\n"
    )

    assert detect_doc_type("MATH201-Syllabus.pdf", text) == SYLLABUS
    chunks = chunk_document(_parsed(text), SYLLABUS)

    _assert_well_formed(chunks)
    # Target 1000 tokens: no chunk overruns it, even though the ceiling is twice that.
    assert max(chunk.token_count for chunk in chunks) <= 1000
    assert chunks[-1].section_title == "Office Hours"

    # Overlap 50 tokens, applied where one section had to be split across chunks.
    weekly = [chunk for chunk in chunks if chunk.section_title == "Weekly Schedule"]
    assert len(weekly) > 1
    for first, second in zip(weekly, weekly[1:], strict=False):
        assert 40 * CHARS_PER_TOKEN <= _overlap_chars(first.content, second.content) <= 200


@pytest.mark.parametrize("doc_type", [*CHUNK_RULES, "something-unrecognized"])
def test_no_document_type_can_emit_a_chunk_over_the_ceiling(doc_type: str) -> None:
    pathological = _parsed(
        # One unbroken run with nowhere to cut, one huge problem with no sub-parts, and
        # one huge section with no paragraph breaks: the worst case for every strategy.
        "x" * 30_000,
        "1. " + _filler(4000, seed=3),
        "## Chapter One\n\n" + _filler(5000, seed=5),
    )

    chunks = chunk_document(pathological, doc_type)

    _assert_well_formed(chunks)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("hw2.pdf", HOMEWORK),
        ("HOMEWORK-3.PDF", HOMEWORK),
        ("problem-set-1.pdf", HOMEWORK),
        ("Syllabus.PDF", SYLLABUS),
        ("week1-notes.md", LECTURE_NOTES),
        ("Lecture 4.pdf", LECTURE_NOTES),
        ("scan.txt", GENERIC),
    ],
)
def test_detect_doc_type_reads_the_filename_first(filename: str, expected: str) -> None:
    # Content that says nothing, so only the name can decide.
    assert detect_doc_type(filename, "some plain prose about the course") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1. first\n2. second\n3. third\n", HOMEWORK),
        ("Problem 1. a\n\nQ2. b\n\n3) c\n", HOMEWORK),
        ("1. first\n2. second\n", GENERIC),
        ("# Week 1\n\ntext\n\n## Limits\n\nmore text\n", LECTURE_NOTES),
        ("# Week 1\n\ntext\n", GENERIC),
        ("just a paragraph of prose\n", GENERIC),
    ],
)
def test_detect_doc_type_falls_back_to_content_heuristics(text: str, expected: str) -> None:
    # A filename with no pattern in it, so only the content can decide.
    assert detect_doc_type("scan-01.pdf", text) == expected


def test_chunks_carry_the_page_they_start_on_and_the_heading_above_them() -> None:
    parsed = _parsed(
        f"## Limits\n\n{_filler(700, seed=1)}",
        f"## Derivatives\n\n{_filler(700, seed=2)}",
        f"## Integrals\n\n{_filler(700, seed=3)}",
    )

    chunks = chunk_document(parsed, LECTURE_NOTES)

    _assert_well_formed(chunks)
    # A chunk names exactly one page, so packing stops at every page break even though
    # two sections would fit inside the 1500-token target: page 1's chunk must not
    # silently hold page 2's text.
    assert [(chunk.page_number, chunk.section_title) for chunk in chunks] == [
        (1, "Limits"),
        (2, "Derivatives"),
        (3, "Integrals"),
    ]


def test_textbook_packs_to_a_larger_target_than_lecture_notes() -> None:
    text = "\n\n".join(f"## Section {index}\n\n{_filler(400, index)}" for index in range(12))
    parsed = _parsed(text)

    textbook = chunk_document(parsed, TEXTBOOK)
    notes = chunk_document(parsed, LECTURE_NOTES)

    _assert_well_formed(textbook)
    _assert_well_formed(notes)
    assert max(chunk.token_count for chunk in textbook) <= 2000
    assert max(chunk.token_count for chunk in notes) <= 1500
    # The same document yields fewer, larger chunks under the textbook rule.
    assert len(textbook) < len(notes)


def test_a_long_structured_document_is_a_textbook_not_homework() -> None:
    """The fault a 608-page linear algebra textbook found.

    `detect_doc_type` had no textbook rule at all, so a book of numbered exercises fell
    through to the problem-marker heuristic, tripped it thousands of times, and was cut at
    every numbered line in it: 1312 fragments averaging 162 tokens with no structure
    recorded. The structural test therefore runs ahead of the marker count, which a book
    of exercises will always win.
    """
    exercises = "\n".join(f"Exercise {number}. Compute the determinant." for number in range(40))

    assert detect_doc_type("Kuttler-LinearAlgebra-2017A.pdf", exercises, _book()) == TEXTBOOK
    # The same text without the structure is still homework, which is the point of the test.
    assert detect_doc_type("Kuttler-LinearAlgebra-2017A.pdf", exercises) == HOMEWORK


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (_book(pages=20), "a short document is not a book however it is organized"),
        (_book(entries=5), "a handful of bookmarks is not a table of contents"),
        (_book(depth=1), "a flat outline is a list of slides, not a structure"),
    ],
)
def test_ordinary_material_is_not_read_as_a_textbook(document: ParsedDocument, reason: str) -> None:
    assert detect_doc_type("week-3.pdf", "some prose about the course", document) != TEXTBOOK, (
        reason
    )


def test_a_filename_still_beats_the_textbook_test() -> None:
    """A name the student chose is better evidence than any heuristic over the file."""
    assert detect_doc_type("homework-4.pdf", "prose", _book()) == HOMEWORK


def test_an_outline_replaces_the_heading_regex_it_beats() -> None:
    """The regex guesses at structure the document states outright, and guesses badly.

    Forced over the reference book it labelled 595 of 596 chunks with things like `Sn` and
    table-of-contents lines complete with their dot leaders. Where there is an outline, it
    wins.
    """
    parsed = ParsedDocument(
        pages=[
            ParsedPage(1, "GRADING POLICY\nSome prose about matrices that the regex would label."),
            ParsedPage(2, "4.9 The Cross Product\nRecall that the dot product is one of two."),
        ],
        pages_total=2,
        pages_skipped=0,
        outline=[OutlineEntry(1, "Rn", 1), OutlineEntry(2, "The Cross Product", 2)],
    )

    chunks = chunk_document(parsed, TEXTBOOK)

    second = next(chunk for chunk in chunks if chunk.page_number == 2)
    assert second.section_title == "The Cross Product"
    assert second.section_path == "Rn / The Cross Product"
    assert second.section_number == "4.9"


def test_a_document_with_no_outline_carries_no_path() -> None:
    """Null is the honest answer, and it is what tells retrieval a document predates this."""
    chunks = chunk_document(_parsed("## Office Hours\nTuesdays at two."), SYLLABUS)

    assert all(chunk.section_path is None for chunk in chunks)
    assert all(chunk.section_number is None for chunk in chunks)
