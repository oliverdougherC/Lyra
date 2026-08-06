"""A document's own structure, turned into something a chunk can be addressed by.

Stage 2a of docs/rag-pipeline.md. A PDF outline says how a book is organized, and until
this existed Lyra threw that away and re-derived a far worse answer from a regex over
flattened text. Measured over a 608-page textbook, the regex labelled 595 of 596 chunks
and most of the labels were things like `Sn` and table-of-contents dot leaders.

Two things come out of here and they answer different questions. `path` is the hierarchy,
for showing a reader where a claim came from. `number` is the label the book prints, for
resolving "use the result from section 4.11" as a lookup instead of a similarity search.

The number is not in the outline. Commercial outlines routinely carry bare titles, and
this one carries no numbers at all, so it is recovered from the page the entry points at,
where the book prints it as a heading. That also means an entry can have a path and no
number, which is correct rather than a gap: front matter, an index, and an unnumbered
appendix genuinely have no section number.
"""

import re
import unicodedata
from dataclasses import dataclass

from backend.rag.parse import OutlineEntry, ParsedDocument

PATH_SEPARATOR = " / "

# A printed heading: `4.9 The Cross Product`, `3.1.1. Cofactors`, `A.2 Well Ordering`.
# The trailing delimiter is optional because books differ on it within one volume.
#
# The optional ATX prefix is for transcribed pages. Recognition writes its headings as
# Markdown (`# C.1 Basic Fourier Series Pairs`), and without the prefix a recognized page
# in an outlined document could never yield a section number: the entry matched the title
# and this regex refused the line it was printed on.
_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?([A-Z]?\.?\d+(?:\.\d+)*)\.?[ \t]+(\S[^\n]*)$", re.MULTILINE
)

# How much of a title has to agree before a printed heading is taken to be that entry.
# A prefix rather than the whole thing, because a page can wrap or hyphenate a long title.
_TITLE_PREFIX_CHARS = 24

# Characters a PDF text layer writes differently from the outline string for the same
# heading. All of these were found on one book: `2x2` in the outline against `2×2` on the
# page, `Definite` against the `ﬁ` ligature, and a straight apostrophe against a curly one.
# NFKC handles the ligature; the rest it leaves alone.
_FOLD = {
    "×": "x",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
}


@dataclass(frozen=True)
class Section:
    """One addressable part of a document, and the pages it covers.

    Attributes:
        path: Titles from the outermost ancestor inwards, joined with `PATH_SEPARATOR`.
        number: The section number the book prints, or None where it prints none.
        depth: Outline nesting level, 1 for a chapter.
        first_page: 1-based page the section starts on.
        last_page: 1-based page it runs to, inclusive. The boundary page belongs to both
            this section and the next, because an outline destination lands where the
            heading sits and that is often partway down a page the previous section is
            still using.
    """

    path: str
    number: str | None
    depth: int
    first_page: int
    last_page: int

    @property
    def title(self) -> str:
        """The innermost title, which is what a citation names."""
        return self.path.rsplit(PATH_SEPARATOR, 1)[-1]


def build_sections(parsed: ParsedDocument) -> list[Section]:
    """Read a document's outline into sections, in document order.

    Args:
        parsed: A parsed document. One with no outline produces no sections, which is
            the normal outcome for a syllabus or a homework sheet.

    Returns:
        Every outline entry as a `Section`, ordered as the outline orders them.
    """
    if not parsed.outline:
        return []

    text_by_page = {page.page_number: page.text for page in parsed.pages}
    last_page = max(text_by_page, default=0)
    ancestry: list[str] = []
    sections: list[Section] = []

    for index, entry in enumerate(parsed.outline):
        # `depth - 1` ancestors are kept, so a jump from depth 3 back to depth 1 discards
        # both of the levels below it rather than leaving a stale grandparent in the path.
        del ancestry[entry.depth - 1 :]
        ancestry.append(entry.title)
        sections.append(
            Section(
                path=PATH_SEPARATOR.join(ancestry),
                number=_number_of(entry, text_by_page),
                depth=entry.depth,
                first_page=entry.page_number,
                last_page=_end_page(parsed.outline, index, last_page),
            )
        )
    return sections


def section_for_page(sections: list[Section], page_number: int | None) -> Section | None:
    """The most specific section covering a page.

    Two rules, applied in order, and the order is the point. A section that *starts* on
    the page wins first, because a boundary page is credited to the section whose heading
    the page announces (the rule `_place_in_sections` in `rag/chunk.py` documents). Only
    then does deepest win, because a page inside section 4.9 is also inside chapter 4 and
    the specific answer is the useful one. Comparing depth alone got the boundary case
    backwards: on a page where subsection 4.9.3 ends and chapter 5 begins, the outgoing
    depth-3 section beat the incoming chapter. Within either rule, ties break towards the
    later section.

    Args:
        sections: What `build_sections` returned.
        page_number: The page to place, or None for a document without pages.

    Returns:
        The section, or None when nothing covers the page. Front matter before the first
        outline entry legitimately lands here.
    """
    if page_number is None:
        return None

    covering = [
        section for section in sections if section.first_page <= page_number <= section.last_page
    ]
    # A page mid-section has no starter and falls through to deepest-covering, which is
    # what keeps a page inside 4.9.3 resolving to 4.9.3 rather than to its chapter.
    starting = [section for section in covering if section.first_page == page_number]

    best: Section | None = None
    for section in starting or covering:
        if best is None or section.depth >= best.depth:
            best = section
    return best


def _end_page(outline: list[OutlineEntry], index: int, last_page: int) -> int:
    """Where an entry's section stops: the next entry at its level or above."""
    depth = outline[index].depth
    for later in outline[index + 1 :]:
        if later.depth <= depth:
            return later.page_number
    return last_page


def _number_of(entry: OutlineEntry, text_by_page: dict[int, str]) -> str | None:
    """The section number the book prints for this entry, if it prints one.

    The destination page is searched first and then the one after it, because an outline
    destination can land on the page before the heading: in the reference book the LU
    Factorization entry points at page 110 and its heading is printed on 111.
    """
    wanted = _normalize(entry.title)[:_TITLE_PREFIX_CHARS]
    if not wanted:
        return None

    for candidate in (entry.page_number, entry.page_number + 1):
        text = text_by_page.get(candidate)
        if text is None:
            continue
        for match in _HEADING.finditer(text):
            if _normalize(match.group(2)).startswith(wanted):
                return match.group(1)
    return None


def _normalize(text: str) -> str:
    """Fold a heading to the form the outline and the page text can be compared in."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(_FOLD.get(character, character) for character in folded)
    return " ".join(folded.split()).casefold()
