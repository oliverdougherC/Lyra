"""Semantic chunking, with a hard token ceiling every strategy is held to.

Chunking respects document structure: it does not split a homework problem, a section,
or a paragraph in half if it can avoid it. Each document type gets its own boundary,
target size, and overlap, and every path then passes through one final ceiling check, so
no strategy can leak a chunk the embedding server would silently truncate.

Sizes are measured with `estimate_tokens`, the same approximation the context budget
uses. It is consistently wrong in the same direction everywhere, which is what matters.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from itertools import groupby

from backend.rag.parse import PAGE_SEPARATOR, ParsedDocument
from backend.rag.tokens import CHARS_PER_TOKEN, estimate_tokens

# Retrieval budgeting assumes small targeted chunks, and an oversized chunk would be
# silently truncated at embedding time, so this ceiling is absolute.
MAX_CHUNK_TOKENS = 2048

HOMEWORK = "homework"
TEXTBOOK = "textbook"
LECTURE_NOTES = "lecture_notes"
SYLLABUS = "syllabus"
GENERIC = "generic"

PROBLEM_BOUNDARY = "problem"
HEADING_BOUNDARY = "heading"
PARAGRAPH_BOUNDARY = "paragraph"

# `1.`, `2)`, `3:`, `Problem 4.`, `Q5.` at the start of a line. The empty third
# alternative is what admits a bare `1.`, and the lookahead on `Q` keeps it from eating
# the first letter of an ordinary word.
PROBLEM_MARKER = re.compile(r"^\s*(?:Problem\s+|Q(?=\d)|)(\d+)[.):]\s", re.MULTILINE)

# Sub-parts of one problem: `(a)`, `a.`, `a)`, `(ii)`, `iii.`. Deliberately lowercase
# only. `A.` and `I.` would match the first word of far too many ordinary sentences.
SUBPART_MARKER = re.compile(
    r"^[ \t]*(?:\((?:[a-z]|[ivx]{1,4})\)|(?:[a-z]|[ivx]{1,4})[.)])[ \t]+(?=\S)",
    re.MULTILINE,
)

# A heading is a whole line: a Markdown ATX heading, a numbered section, a shouted
# all-caps line, or a label line ending in a colon. Anything with prose after it is body
# text, not a boundary.
SECTION_HEADING = re.compile(
    r"""
    ^[ \t]*(?:
        \#{1,6}[ \t]+\S[^\n]*             # ## Continuity
      | \d+(?:\.\d+)*[.)]?[ \t]+\S[^\n]*  # 2.3 Continuity
      | [A-Z][A-Z0-9][A-Z0-9 ,&'()/-]*    # GRADING POLICY
      | [A-Z][^\n:]{2,60}:                # Office Hours:
    )[ \t]*$
    """,
    re.MULTILINE | re.VERBOSE,
)

MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_WHITESPACE = re.compile(r"\s")

PARAGRAPH_JOIN = "\n\n"

# Filename beats content, and is matched case-insensitively as a substring: a file called
# `hw3.pdf` is homework even when its problems are numbered in a way nothing detects.
FILENAME_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("hw", "homework", "problem"), HOMEWORK),
    (("syllabus",), SYLLABUS),
    (("notes", "lecture"), LECTURE_NOTES),
)

MIN_PROBLEM_MARKERS = 3
MIN_MARKDOWN_HEADINGS = 2

# Problems do not overlap each other, but the paragraph fallback inside one oversized
# problem does, so a step split across two parts is readable in both.
HOMEWORK_PART_OVERLAP_TOKENS = 100


@dataclass(frozen=True)
class ChunkRule:
    """How one document type is cut up.

    Attributes:
        boundary: What the split follows: problems, headings, or paragraphs.
        target_tokens: Size chunks are packed towards, always at or below the ceiling.
        overlap_tokens: Tokens repeated from the previous chunk when one unit of
            structure has to be split.
    """

    boundary: str
    target_tokens: int
    overlap_tokens: int


# The strategy table from rag-pipeline.md, Stage 3. `textbook` has no detection rule and
# is only reached when a caller names it, but the strategy is defined so that it can be.
CHUNK_RULES: dict[str, ChunkRule] = {
    HOMEWORK: ChunkRule(PROBLEM_BOUNDARY, MAX_CHUNK_TOKENS, 0),
    TEXTBOOK: ChunkRule(HEADING_BOUNDARY, 2000, 100),
    LECTURE_NOTES: ChunkRule(HEADING_BOUNDARY, 1500, 75),
    SYLLABUS: ChunkRule(HEADING_BOUNDARY, 1000, 50),
    GENERIC: ChunkRule(PARAGRAPH_BOUNDARY, 1500, 100),
}


@dataclass(frozen=True)
class Chunk:
    """One unit of retrievable text and where it came from.

    Attributes:
        content: The chunk text, stripped, without any embedding task prefix.
        token_count: `estimate_tokens(content)`, never above `MAX_CHUNK_TOKENS`.
        page_number: Page the chunk's own content starts on, when pages are known.
        section_title: Heading the chunk sits under, when the split was heading-driven.
        problem_number: Homework problem this chunk belongs to.
        part_index: 0-based position among the parts of a split problem, or None when
            the problem fits in a single chunk.
    """

    content: str
    token_count: int
    page_number: int | None = None
    section_title: str | None = None
    problem_number: str | None = None
    part_index: int | None = None


@dataclass
class _Draft:
    """A chunk under construction, before stripping, capping, and part numbering."""

    content: str
    page_number: int | None = None
    section_title: str | None = None
    problem_number: str | None = None
    part_index: int | None = None


@dataclass(frozen=True)
class _Flat:
    """The document as one string, plus where each page begins in it."""

    text: str
    offsets: list[int] = field(default_factory=list)
    numbers: list[int] = field(default_factory=list)

    def page_at(self, offset: int) -> int | None:
        """The page number covering `offset`, or None for a document without pages."""
        if not self.offsets:
            return None
        index = bisect_right(self.offsets, offset) - 1
        return self.numbers[max(index, 0)]


def detect_doc_type(filename: str, text: str) -> str:
    """Classify a document so it can be chunked the way its structure wants.

    Filename patterns are checked first and are decisive, because a name the student
    chose is better evidence than a heuristic over text an extractor produced. Content
    heuristics only run when the name says nothing.

    Args:
        filename: Original upload filename, matched case-insensitively.
        text: Full extracted document text.

    Returns:
        One of `homework`, `syllabus`, `lecture_notes`, or `generic`.
    """
    lowered = filename.lower()
    for patterns, doc_type in FILENAME_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return doc_type

    if len(PROBLEM_MARKER.findall(text)) >= MIN_PROBLEM_MARKERS:
        return HOMEWORK
    if len(MARKDOWN_HEADING.findall(text)) >= MIN_MARKDOWN_HEADINGS:
        return LECTURE_NOTES
    return GENERIC


def chunk_document(parsed: ParsedDocument, doc_type: str) -> list[Chunk]:
    """Split a parsed document into chunks under the rule for its type.

    Args:
        parsed: Readable pages, in order.
        doc_type: A key of `CHUNK_RULES`. An unknown type is chunked as `generic`.

    Returns:
        Chunks in document order. Every one is non-empty, carries a `token_count` equal
        to `estimate_tokens(content)`, and sits at or below `MAX_CHUNK_TOKENS`.
    """
    flat = _flatten(parsed)
    rule = CHUNK_RULES.get(doc_type, CHUNK_RULES[GENERIC])

    if rule.boundary == PROBLEM_BOUNDARY:
        drafts = _chunk_problems(flat)
    elif rule.boundary == HEADING_BOUNDARY:
        drafts = _chunk_sections(flat, rule)
    else:
        drafts = _chunk_paragraphs(flat, rule)

    return _finalize(drafts)


def _flatten(parsed: ParsedDocument) -> _Flat:
    """Join the pages into one string, recording where each one starts."""
    texts: list[str] = []
    offsets: list[int] = []
    numbers: list[int] = []
    position = 0
    for page in parsed.pages:
        texts.append(page.text)
        offsets.append(position)
        numbers.append(page.page_number)
        position += len(page.text) + len(PAGE_SEPARATOR)
    return _Flat(PAGE_SEPARATOR.join(texts), offsets, numbers)


def _chunk_problems(flat: _Flat) -> list[_Draft]:
    """One chunk per homework problem, splitting a problem only when it is too big."""
    matches = list(PROBLEM_MARKER.finditer(flat.text))
    if not matches:
        # Named like homework but with no detectable problem markers. Paragraph grouping
        # is the documented fallback for a document with no structure to follow.
        return _chunk_paragraphs(flat, CHUNK_RULES[GENERIC])

    drafts: list[_Draft] = []
    preamble = flat.text[: matches[0].start()]
    if preamble.strip():
        # Course header, due date, instructions: it belongs to no problem.
        drafts.append(_Draft(content=preamble, page_number=flat.page_at(0)))

    for position, match in enumerate(matches):
        start = match.start()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(flat.text)
        drafts.extend(_split_problem(flat, start, flat.text[start:end], match.group(1)))
    return drafts


def _split_problem(flat: _Flat, start: int, body: str, number: str) -> list[_Draft]:
    """One problem as chunks: whole if it fits, then sub-parts, then paragraphs."""
    if estimate_tokens(body) <= MAX_CHUNK_TOKENS:
        return [_Draft(content=body, page_number=flat.page_at(start), problem_number=number)]

    parts = _split_subparts(body, start)
    if not parts or any(estimate_tokens(text) > MAX_CHUNK_TOKENS for _, text in parts):
        parts = _pack_with_overlap(
            _paragraphs(body, start), MAX_CHUNK_TOKENS, HOMEWORK_PART_OVERLAP_TOKENS, flat
        )

    return [
        _Draft(
            content=text,
            page_number=flat.page_at(offset),
            problem_number=number,
            part_index=index,
        )
        for index, (offset, text) in enumerate(parts)
    ]


def _split_subparts(body: str, offset: int) -> list[tuple[int, str]]:
    """Cut a problem at its lettered or numbered sub-parts, stem first."""
    matches = list(SUBPART_MARKER.finditer(body))
    if not matches:
        return []

    pieces: list[tuple[int, str]] = []
    stem = body[: matches[0].start()]
    if stem.strip():
        pieces.append((offset, stem))
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        pieces.append((offset + match.start(), body[match.start() : end]))
    return pieces


def _chunk_sections(flat: _Flat, rule: ChunkRule) -> list[_Draft]:
    """Pack whole sections up to the target, splitting only a section that overruns.

    Sections are first cut at page boundaries and a group never spans pages, because a
    chunk names exactly one page and must not silently hold another page's text.
    """
    drafts: list[_Draft] = []
    group: list[str] = []
    group_offset = 0
    group_title: str | None = None
    group_tokens = 0

    def flush() -> None:
        nonlocal group, group_title, group_tokens
        if group:
            drafts.append(
                _Draft(
                    content="".join(group),
                    page_number=flat.page_at(group_offset),
                    section_title=group_title,
                )
            )
        group, group_title, group_tokens = [], None, 0

    for offset, title, body in _sections(flat.text):
        for piece_offset, piece in _split_at_pages(flat, offset, body):
            tokens = estimate_tokens(piece)
            if tokens > rule.target_tokens:
                flush()
                for packed_offset, packed in _pack_with_overlap(
                    _paragraphs(piece, piece_offset),
                    rule.target_tokens,
                    rule.overlap_tokens,
                    flat,
                ):
                    drafts.append(
                        _Draft(
                            content=packed,
                            page_number=flat.page_at(packed_offset),
                            section_title=title,
                        )
                    )
                continue

            if group and (
                flat.page_at(piece_offset) != flat.page_at(group_offset)
                or group_tokens + tokens > rule.target_tokens
            ):
                flush()
            if not group:
                group_offset, group_title = piece_offset, title
            group.append(piece)
            group_tokens += tokens

    flush()
    return drafts


def _chunk_paragraphs(flat: _Flat, rule: ChunkRule) -> list[_Draft]:
    """Group paragraphs towards the target, repeating the overlap across the seam."""
    return [
        _Draft(content=text, page_number=flat.page_at(offset))
        for offset, text in _pack_with_overlap(
            _paragraphs(flat.text, 0), rule.target_tokens, rule.overlap_tokens, flat
        )
    ]


def _sections(text: str) -> list[tuple[int, str | None, str]]:
    """Heading-delimited sections as (offset, title, body), body including the heading.

    Text before the first heading is a section with no title, so a document that opens
    with prose does not lose it.
    """
    matches = list(SECTION_HEADING.finditer(text))
    if not matches:
        return [(0, None, text)]

    sections: list[tuple[int, str | None, str]] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append((0, None, preamble))

    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        sections.append((match.start(), _heading_title(match.group(0)), text[match.start() : end]))
    return sections


def _heading_title(line: str) -> str:
    """The readable part of a heading line, without its markers."""
    return line.strip().lstrip("#").strip().rstrip(":").strip()


def _paragraphs(text: str, offset: int) -> list[tuple[int, str]]:
    """Non-empty paragraphs of `text` with their absolute start offsets."""
    pieces: list[tuple[int, str]] = []
    position = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        piece = text[position : match.start()]
        if piece.strip():
            pieces.append((offset + position, piece))
        position = match.end()
    tail = text[position:]
    if tail.strip():
        pieces.append((offset + position, tail))
    return pieces


def _pack_with_overlap(
    items: list[tuple[int, str]],
    target_tokens: int,
    overlap_tokens: int,
    flat: _Flat | None = None,
) -> list[tuple[int, str]]:
    """Group items into pieces near the target, each repeating the previous one's tail.

    A piece is only emitted once it holds new content, so the repeated tail can never
    become a chunk of its own. The offset reported is that of the first new item, so a
    chunk's page number points at where its own content starts rather than at the repeat.

    When `flat` is given, a page break is a hard boundary with no overlap across it: a
    chunk names exactly one page, so it may not hold another page's text, including the
    repeated tail.
    """
    pieces: list[tuple[int, str]] = []
    current: list[str] = []
    current_offset = 0
    current_tokens = 0
    has_new = False

    for offset, text in items:
        tokens = estimate_tokens(text)
        crosses_page = (
            flat is not None and has_new and flat.page_at(offset) != flat.page_at(current_offset)
        )
        if has_new and (crosses_page or current_tokens + tokens > target_tokens):
            body = PARAGRAPH_JOIN.join(current)
            pieces.append((current_offset, body))
            carry = "" if crosses_page else _tail(body, overlap_tokens)
            current = [carry] if carry else []
            current_tokens = estimate_tokens(carry) if carry else 0
            has_new = False
        if not has_new:
            current_offset = offset
        current.append(text)
        current_tokens += tokens
        has_new = True

    if has_new:
        pieces.append((current_offset, PARAGRAPH_JOIN.join(current)))
    return pieces


def _split_at_pages(flat: _Flat, offset: int, body: str) -> list[tuple[int, str]]:
    """Cut a section body at page boundaries so no chunk carries another page's text."""
    cuts = [page_start for page_start in flat.offsets if offset < page_start < offset + len(body)]
    if not cuts:
        return [(offset, body)]

    pieces: list[tuple[int, str]] = []
    start = 0
    for cut in cuts:
        pieces.append((offset + start, body[start : cut - offset]))
        start = cut - offset
    pieces.append((offset + start, body[start:]))
    return pieces


def _tail(text: str, overlap_tokens: int) -> str:
    """The last `overlap_tokens` worth of text, snapped forward to a word boundary."""
    if overlap_tokens <= 0:
        return ""
    width = overlap_tokens * CHARS_PER_TOKEN
    if len(text) <= width:
        return text.strip()

    tail = text[-width:]
    boundary = _WHITESPACE.search(tail)
    return (tail[boundary.end() :] if boundary else tail).strip()


def _finalize(drafts: list[_Draft]) -> list[Chunk]:
    """Strip, drop the empties, hold every path to the ceiling, then number the parts."""
    trimmed: list[_Draft] = []
    for draft in drafts:
        draft.content = draft.content.strip()
        if draft.content:
            trimmed.append(draft)

    capped: list[_Draft] = []
    for draft in trimmed:
        capped.extend(_enforce_ceiling(draft))

    _number_parts(capped)
    return [
        Chunk(
            content=draft.content,
            token_count=estimate_tokens(draft.content),
            page_number=draft.page_number,
            section_title=draft.section_title,
            problem_number=draft.problem_number,
            part_index=draft.part_index,
        )
        for draft in capped
    ]


def _enforce_ceiling(draft: _Draft) -> list[_Draft]:
    """The last line of defence: no strategy may emit a chunk above the ceiling."""
    if estimate_tokens(draft.content) <= MAX_CHUNK_TOKENS:
        return [draft]
    return [replace(draft, content=piece) for piece in _hard_split(draft.content)]


def _hard_split(text: str) -> list[str]:
    """Cut text into ceiling-sized pieces at the last whitespace below the limit."""
    width = MAX_CHUNK_TOKENS * CHARS_PER_TOKEN
    pieces: list[str] = []
    remaining = text

    while estimate_tokens(remaining) > MAX_CHUNK_TOKENS:
        head = remaining[:width]
        cut = max(head.rfind("\n"), head.rfind(" "))
        if cut <= 0:
            # A single unbroken run of characters, so there is nowhere better to cut.
            cut = width
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    pieces.append(remaining)
    return [piece for piece in pieces if piece]


def _number_parts(drafts: list[_Draft]) -> None:
    """Index the parts of each split problem; a problem that fits keeps no index.

    Grouping is over consecutive runs rather than the whole document, so a second list
    that restarts at `1.` is never mistaken for more parts of the first problem.
    """
    for _, group in groupby(drafts, key=lambda draft: draft.problem_number):
        parts = list(group)
        if parts[0].problem_number is None:
            continue
        if len(parts) == 1:
            parts[0].part_index = None
            continue
        for index, part in enumerate(parts):
            part.part_index = index
