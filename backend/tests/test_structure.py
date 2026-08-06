"""Reading a document's own outline into something a chunk can be addressed by.

Every case here came off a real 608-page textbook, because the interesting behaviour is
all in what real outlines do rather than in what a tidy one would.
"""

from backend.rag.parse import OutlineEntry, ParsedDocument, ParsedPage
from backend.rag.structure import build_sections, section_for_page


def _document(pages: dict[int, str], outline: list[OutlineEntry]) -> ParsedDocument:
    """A parsed document with chosen page text and a chosen outline."""
    return ParsedDocument(
        pages=[ParsedPage(page_number=number, text=text) for number, text in sorted(pages.items())],
        pages_total=len(pages),
        pages_skipped=0,
        outline=outline,
    )


def test_a_document_with_no_outline_has_no_sections() -> None:
    """The normal outcome for a syllabus or a homework sheet, and not a failure."""
    assert build_sections(_document({1: "Course policies."}, [])) == []


def test_the_path_names_every_ancestor() -> None:
    parsed = _document(
        {1: "1 Matrices", 2: "1.1 Matrix Arithmetic", 3: "1.1.1 Addition of Matrices"},
        [
            OutlineEntry(1, "Matrices", 1),
            OutlineEntry(2, "Matrix Arithmetic", 2),
            OutlineEntry(3, "Addition of Matrices", 3),
        ],
    )

    assert [section.path for section in build_sections(parsed)] == [
        "Matrices",
        "Matrices / Matrix Arithmetic",
        "Matrices / Matrix Arithmetic / Addition of Matrices",
    ]


def test_returning_to_a_shallower_level_drops_every_level_below_it() -> None:
    """A chapter after a sub-subsection must not inherit it as an ancestor."""
    parsed = _document(
        {1: "text", 2: "text", 3: "text"},
        [
            OutlineEntry(1, "Matrices", 1),
            OutlineEntry(3, "Addition of Matrices", 2),
            OutlineEntry(1, "Determinants", 3),
        ],
    )

    assert [section.path for section in build_sections(parsed)][-1] == "Determinants"


def test_the_number_comes_off_the_page_because_the_outline_has_none() -> None:
    """This book's outline entries are bare titles; the numbers are printed as headings."""
    parsed = _document(
        {12: "4.9 The Cross Product\nRecall that the dot product..."},
        [OutlineEntry(1, "The Cross Product", 12)],
    )

    assert build_sections(parsed)[0].number == "4.9"


def test_a_destination_one_page_early_still_finds_its_number() -> None:
    """An outline destination lands where the heading sits, which can be the page before.

    In the reference book the LU Factorization entry points at page 110 and its heading is
    printed on 111, under the previous section's exercises.
    """
    parsed = _document(
        {110: "Exercise 2.1.58 Let A be...", 111: "2.2 LU Factorization\nAn LU factorization..."},
        [OutlineEntry(2, "LU Factorization", 110)],
    )

    assert build_sections(parsed)[0].number == "2.2"


def test_a_heading_the_text_layer_writes_differently_still_matches() -> None:
    """Ligatures and symbol variants, both found on the reference book.

    The outline says `2x2` and `Definite`; the page says `2x2` with a multiplication sign
    and `Definite` with an fi ligature. Six entries were missing a number over this.
    """
    parsed = _document(
        {
            5: "3.1.1. Cofactors and 2×2 Determinants\nbody",
            9: "7.4.3. Positive Deﬁnite Matrices\nbody",
        },
        [
            OutlineEntry(3, "Cofactors and 2x2 Determinants", 5),
            OutlineEntry(3, "Positive Definite Matrices", 9),
        ],
    )

    assert [section.number for section in build_sections(parsed)] == ["3.1.1", "7.4.3"]


def test_a_section_the_book_does_not_number_has_no_number() -> None:
    """Front matter, an index, and an unnumbered appendix genuinely have none."""
    parsed = _document({1: "Preface\nThis book grew out of..."}, [OutlineEntry(1, "Preface", 1)])

    assert build_sections(parsed)[0].number is None


def test_a_section_runs_to_the_next_entry_at_its_level_or_above() -> None:
    parsed = _document(
        dict.fromkeys(range(1, 21), "text"),
        [
            OutlineEntry(1, "Matrices", 1),
            OutlineEntry(2, "Matrix Arithmetic", 3),
            OutlineEntry(2, "LU Factorization", 8),
            OutlineEntry(1, "Determinants", 15),
        ],
    )

    ranges = {
        section.title: (section.first_page, section.last_page) for section in build_sections(parsed)
    }
    assert ranges["Matrix Arithmetic"] == (3, 8)
    assert ranges["Matrices"] == (1, 15)
    assert ranges["Determinants"] == (15, 20)


def test_the_deepest_section_covering_a_page_wins() -> None:
    """A page inside section 1.1 is also inside chapter 1, and the specific one is useful."""
    parsed = _document(
        dict.fromkeys(range(1, 11), "text"),
        [OutlineEntry(1, "Matrices", 1), OutlineEntry(2, "Matrix Arithmetic", 4)],
    )
    sections = build_sections(parsed)

    assert section_for_page(sections, 2).path == "Matrices"
    assert section_for_page(sections, 6).path == "Matrices / Matrix Arithmetic"


def test_a_page_before_the_first_entry_belongs_to_no_section() -> None:
    """Front matter precedes the outline, and saying so beats guessing."""
    parsed = _document(dict.fromkeys(range(1, 11), "text"), [OutlineEntry(1, "Matrices", 5)])

    assert section_for_page(build_sections(parsed), 2) is None
    assert section_for_page(build_sections(parsed), None) is None
