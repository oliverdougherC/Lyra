"""The section index: pure functions that let a draft be addressed by section.

Everything the writer does - reading, drafting, reviewing - is section-scoped, because
the model's context is modest and a long draft must never need to fit in it. This module
is the address scheme: parse a markdown body into sections with exact character spans,
render the outline, extract one section by number or title, and splice a replacement in
byte-exact.

No database, no LLM, no I/O. The body is the only input, and a `Section` is only valid
against the body it was parsed from - `splice` checks, because splicing against a body
that moved would corrupt the draft silently.

Heading model: ATX headings open sections; setext headings are normalized to levels 1
and 2. A section's span runs from its heading line to the next heading at its level or
above, so a parent's span contains its children and replacing "2" replaces 2.1 with it.
A `#` inside a fenced code block is code, not a heading.
"""

import re
from dataclasses import dataclass

__all__ = [
    "Section",
    "parse",
    "outline",
    "heading_lines",
    "has_todo",
    "prose",
    "seams",
    "extract",
    "splice",
]

PREAMBLE_NUMBER = "0"
PREAMBLE_TITLE = "(before the first heading)"

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# A TODO marker: the line's content, past any list or blockquote furniture, is a TODO.
# These lines do not count against emptiness - a skeleton section holding only its
# intent marker is still a section the drafter may write directly. The optional
# backslash matters: a document that has round-tripped through the Milkdown editor
# comes back with `[TODO:` escaped to `\[TODO:` (a bare bracket could open a link),
# and an unmatched marker would silently turn every skeleton section "occupied".
_TODO_LINE = re.compile(r"^(?:[ \t]*(?:[-*+>]|\d+[.)]))*[ \t]*(?:\\)?\[?\s*TODO\b", re.IGNORECASE)


@dataclass(frozen=True)
class Section:
    """One section of a markdown body, span-exact against that body.

    Attributes:
        number: Hierarchical number, "2.1". The preamble, when text precedes the first
            heading, is number "0".
        title: The heading text, stripped of `#` furniture. The preamble has a fixed one.
        level: Heading level 1-6; 0 for the preamble.
        start: Char offset of the heading line's first byte (or the preamble's).
        end: Char offset one past the section's last byte, children included.
        text: The exact span `body[start:end]`, heading line included, so a splice of an
            unmodified section is the identity.
        word_count: Words in the span, heading lines excluded, children included.
        is_empty: True when the span holds nothing but headings, whitespace, and TODO
            markers - the state in which a drafter may write directly.
    """

    number: str
    title: str
    level: int
    start: int
    end: int
    text: str
    word_count: int
    is_empty: bool


@dataclass(frozen=True)
class _Heading:
    """One heading found by the scan: where, how deep, and what it says."""

    line_index: int
    level: int
    title: str
    # Setext headings occupy two lines; the underline must not count as body text.
    underline_index: int | None = None


def _scan_headings(lines: list[str]) -> list[_Heading]:
    """Every heading in the body, fence-aware, setext normalized to levels 1 and 2."""
    headings: list[_Heading] = []
    fence_marker: str | None = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        fence = _FENCE.match(content)
        if fence_marker is not None:
            # Inside a fence everything is code. Only the closing fence gets a look:
            # same character, at least as long, nothing else on the line.
            if (
                fence
                and fence.group(1)[0] == fence_marker[0]
                and len(fence.group(1)) >= len(fence_marker)
                and content.strip() == fence.group(1)
            ):
                fence_marker = None
            continue
        if fence:
            fence_marker = fence.group(1)
            continue

        atx = _ATX.match(content)
        if atx:
            title = _CLOSING_HASHES.sub("", atx.group(2) or "").strip()
            headings.append(_Heading(index, len(atx.group(1)), title))
            continue

        # A setext underline promotes the line above it - which must be a plain text
        # line. That check is also what separates a heading's `---` from a thematic
        # break's: CommonMark reads text-then-dashes as a heading, and dashes after a
        # blank line as a break, which is exactly what `above_is_text` decides.
        if index > 0 and _SETEXT.match(content):
            above = lines[index - 1].rstrip("\r\n")
            above_is_text = (
                above.strip() != ""
                and not _ATX.match(above)
                and not _FENCE.match(above)
                and not _SETEXT.match(above)
                and (not headings or headings[-1].line_index != index - 1)
            )
            if above_is_text:
                level = 1 if content.strip().startswith("=") else 2
                headings.append(_Heading(index - 1, level, above.strip(), underline_index=index))
    return headings


def _numbers(headings: list[_Heading]) -> list[str]:
    """Hierarchical numbers for headings in order, tolerating skipped levels."""
    numbers: list[str] = []
    # The path is the stack of (level, ordinal) from the document root down to the
    # current heading's parent chain. A new heading pops everything at its level or
    # deeper, then joins as a child of what remains.
    path: list[tuple[int, int]] = []
    counters: dict[int, int] = {}
    for heading in headings:
        while path and path[-1][0] >= heading.level:
            path.pop()
        # Entering level L resets every deeper counter: a new "2" makes the next
        # subsection 2.1, not 1.3.
        for level in list(counters):
            if level > heading.level:
                del counters[level]
        counters[heading.level] = counters.get(heading.level, 0) + 1
        path.append((heading.level, counters[heading.level]))
        numbers.append(".".join(str(ordinal) for _, ordinal in path))
    return numbers


def parse(body: str) -> list[Section]:
    """Every section of the body in document order, spans exact, parents before children.

    Includes a preamble section (number "0") when non-blank text precedes the first
    heading. An unheaded but non-blank body is therefore one preamble section; an empty
    body is no sections at all.
    """
    lines = body.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    offsets.append(position)  # One past the end, so spans can close on the last line.

    headings = _scan_headings(lines)
    numbers = _numbers(headings)
    heading_lines = {heading.line_index for heading in headings}
    heading_lines.update(
        heading.underline_index for heading in headings if heading.underline_index is not None
    )

    sections: list[Section] = []
    preamble_end = headings[0].line_index if headings else len(lines)
    preamble_text = body[: offsets[preamble_end]]
    if preamble_text.strip():
        sections.append(
            _section(
                PREAMBLE_NUMBER, PREAMBLE_TITLE, 0, 0, preamble_end, body, offsets, heading_lines
            )
        )

    for position_index, heading in enumerate(headings):
        end_line = len(lines)
        for later in headings[position_index + 1 :]:
            if later.level <= heading.level:
                end_line = later.line_index
                break
        sections.append(
            _section(
                numbers[position_index],
                heading.title,
                heading.level,
                heading.line_index,
                end_line,
                body,
                offsets,
                heading_lines,
            )
        )
    return sections


def _section(
    number: str,
    title: str,
    level: int,
    start_line: int,
    end_line: int,
    body: str,
    offsets: list[int],
    heading_lines: set[int],
) -> Section:
    """Build one section from its line span."""
    start, end = offsets[start_line], offsets[end_line]
    lines = body.splitlines(keepends=True)
    prose = [
        line
        for index, line in enumerate(lines[start_line:end_line], start=start_line)
        if index not in heading_lines
    ]
    words = sum(len(line.split()) for line in prose)
    meaningful = [line for line in prose if not _TODO_LINE.match(line)]
    return Section(
        number=number,
        title=title,
        level=level,
        start=start,
        end=end,
        text=body[start:end],
        word_count=words,
        is_empty=all(not line.strip() for line in meaningful),
    )


def outline(body: str) -> str:
    """The numbered outline, rendered for prompts and for the `read_outline` tool.

    One line per section: number, title, and either a word count or an empty flag. The
    counts are what lets a model pick a section worth reading instead of reading all of
    them.
    """
    sections = parse(body)
    if not sections:
        return "The document is empty."
    rows = []
    for section in sections:
        indent = "  " * max(section.level - 1, 0)
        state = "empty" if section.is_empty else f"{section.word_count} words"
        rows.append(f"{indent}{section.number} {section.title} ({state})")
    return "\n".join(rows)


def heading_lines(body: str) -> str:
    """Every heading exactly as it is written in the document, one per line.

    For a reviewer that has to quote what it judges. The outline renders headings with
    their numbers and word counts, which is the right shape for choosing what to read and
    the wrong shape for quoting: a lens given only `1.1 Introduction (12 words)` has never
    seen the literal `## Introduction`, so every quote it writes is a reconstruction that
    `resolve_quote` refuses. The first live review filed a finding whose quote was the
    single character "T" for exactly this reason.
    """
    rendered = [
        section.text.partition("\n")[0].strip() for section in parse(body) if section.level > 0
    ]
    return "\n".join(line for line in rendered if line)


def has_todo(text: str) -> bool:
    """Whether any line of `text` is a TODO marker.

    The marker is line-anchored (and tolerates the editor's `\\[TODO:` escaping), so this
    tests lines rather than searching the blob - "the TODO list" in a sentence of prose is
    not a marker, and treating it as one would send a finished section back for a rewrite.
    """
    return any(_TODO_LINE.match(line) for line in text.splitlines())


def prose(section: Section) -> str:
    """A section's text without its heading line, which is furniture in most readings."""
    if section.level > 0:
        return section.text.partition("\n")[2].strip()
    return section.text.strip()


def seams(targets: list[Section], tail_words: int, head_words: int) -> str:
    """Every handoff between consecutive sections: tail of one, head of the next.

    Both the reviewer's argument lens and the drafting pass's revise stage judge a
    document by its joins, and the join is what neither section contains on its own.
    """
    rendered: list[str] = []
    for before, after in zip(targets, targets[1:], strict=False):
        tail = " ".join(prose(before).split()[-tail_words:])
        head = " ".join(prose(after).split()[:head_words])
        rendered.append(
            f"{before.number} {before.title} ends:\n{tail}\n\n"
            f"{after.number} {after.title} begins:\n{head}"
        )
    return "\n\n---\n\n".join(rendered)


def extract(body: str, ref: str) -> Section | None:
    """One section by number ("2.1") or by title, or None.

    Kuhn's addressing rule, kept: a ref starting with a digit matches by number, all
    others by title - exact match first, case-insensitive, then substring, first in
    document order. The trailing dot of a spoken "section 2." is tolerated.
    """
    wanted = ref.strip()
    if not wanted:
        return None
    sections = parse(body)
    if wanted[0].isdigit():
        number = wanted.rstrip(".")
        return next((s for s in sections if s.number == number), None)
    lowered = wanted.lower()
    exact = next((s for s in sections if s.title.lower() == lowered), None)
    if exact is not None:
        return exact
    return next((s for s in sections if lowered in s.title.lower()), None)


def splice(body: str, section: Section, new_text: str) -> str:
    """The body with one section's span replaced, byte-exact outside it.

    Raises:
        ValueError: when the section did not come from this body. The body moved since
            the parse - the caller must re-extract against what is there now, because a
            span-based splice into moved text would corrupt the draft without a sound.
    """
    if body[section.start : section.end] != section.text:
        raise ValueError("That section is stale: the document changed since it was parsed.")
    return body[: section.start] + new_text + body[section.end :]
