"""Margin comments: anchored findings on a draft, threaded, position-dumb.

The store keeps what kuhn's proved out: a thread root holds the canonical matched passage
and the char offset it sat at when filed - never a live position. Every consumer
re-resolves the quote against the text it is actually looking at, through
`resolve_quote`: exact occurrences first, nearest the hint; then case, whitespace, and
Unicode punctuation normalization; then a conservative best-substring match. Else None,
at which point the comment is flagged orphaned, kept, and listed unanchored. The finding
is still worth reading - only its pin is lost - and a later edit that brings the passage
back un-orphans it.

A root may also carry no quote at all: a document-wide finding ("there is no section
that answers the assignment's second question") has no single passage to sit on, and
inventing one would anchor the comment to the wrong thing. Quoteless roots are never
orphaned - they were never anchored.

Replies hang off the root via `parent_id` and only roots carry severity, anchor, and
the resolved flag. Resolution state is the student's; the reviewer only files.
"""

import difflib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from backend.core.errors import NotFoundError

REVIEWER = "reviewer"
WRITER = "writer"
STUDENT = "student"
AUTHORS: tuple[str, ...] = (REVIEWER, WRITER, STUDENT)

# Kuhn's scale, kept with its definitions (they live in the reviewer prompt): critical
# invalidates, major weakens, minor is surface, note is a suggestion.
SEVERITIES: tuple[str, ...] = ("critical", "major", "minor", "note")

# Occurrence cap when scanning for a quote. Beyond this the quote is too generic to
# anchor meaningfully anyway.
_MAX_OCCURRENCES = 50

_NO_COMMENT_MESSAGE = "That comment does not exist."
_NOT_A_ROOT_MESSAGE = "Replies and resolution apply to the thread root."

_WORD = re.compile(r"\w+(?:['-]\w+)*", re.UNICODE)

# NFKC does not fold typographic quotation marks or dashes. Those are the punctuation
# differences small models most often introduce while copying a passage, so make them
# equivalent before attempting the conservative fuzzy fallback.
_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2026": "...",
    }
)

_MIN_FUZZY_WORDS = 4
_MIN_FUZZY_CHARS = 20
_MIN_FUZZY_RATIO = 0.82
_MIN_TOKEN_COVERAGE = 0.75
_AMBIGUOUS_MARGIN = 0.025


@dataclass(frozen=True)
class Anchor:
    """Where a quote sits in one specific text, in raw character offsets.

    `exact` is False when the match came through the whitespace-normalized fallback,
    which a renderer may care about: the span covers the same words, but not the same
    bytes as the stored quote.
    """

    start: int
    end: int
    exact: bool


def resolve_quote(
    content: str,
    quote: str,
    hint: int | None = None,
    *,
    scope_start: int = 0,
    scope_end: int | None = None,
) -> Anchor | None:
    """Locate `quote` in `content`, or None when it is not there.

    Exact occurrences win, nearest the hint when there are several - the hint is where
    the quote sat when the comment was filed, so after an edit above the anchor the
    nearest survivor is almost always the right one. When nothing matches exactly, both
    sides are whitespace-collapsed and scanned again with a map back to raw offsets, so
    a paragraph reflowed by the editor keeps its comments.
    """
    if not quote or not content:
        return None
    end_bound = len(content) if scope_end is None else min(len(content), scope_end)
    start_bound = max(0, scope_start)
    if start_bound >= end_bound:
        return None
    scoped = content[start_bound:end_bound]
    local_hint = hint - start_bound if hint is not None else None

    exact = _occurrences(scoped, quote)
    if exact:
        start = _nearest(exact, local_hint) + start_bound
        return Anchor(start=start, end=start + len(quote), exact=True)

    norm, offsets = _canonical_with_map(scoped)
    norm_quote = _canonical(quote)
    if not norm_quote:
        return None
    fallback = _occurrences(norm, norm_quote)
    if fallback:
        norm_start = _nearest(fallback, local_hint)
        start = start_bound + offsets[norm_start]
        end = start_bound + offsets[norm_start + len(norm_quote) - 1] + 1
        return Anchor(start=start, end=end, exact=False)

    fuzzy = _best_substring(norm, norm_quote, offsets, local_hint)
    if fuzzy is None:
        return None
    start, end = fuzzy
    return Anchor(start=start_bound + start, end=start_bound + end, exact=False)


def _occurrences(haystack: str, needle: str) -> list[int]:
    """Every start offset of `needle`, capped at `_MAX_OCCURRENCES`."""
    found: list[int] = []
    position = 0
    while len(found) < _MAX_OCCURRENCES:
        index = haystack.find(needle, position)
        if index == -1:
            break
        found.append(index)
        position = index + 1
    return found


def _nearest(occurrences: list[int], hint: int | None) -> int:
    """The occurrence closest to the hint, or the first when there is no hint."""
    if hint is None or len(occurrences) == 1:
        return occurrences[0]
    return min(occurrences, key=lambda offset: abs(offset - hint))


def _canonical(text: str) -> str:
    """Case-, whitespace-, and common Unicode-punctuation-normalized text."""
    normalized, _ = _canonical_with_map(text)
    return normalized.strip()


def _canonical_with_map(content: str) -> tuple[str, list[int]]:
    """Canonical text, with ``map[i]`` equal to its raw source offset."""
    norm: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(content):
        folded = unicodedata.normalize("NFKC", char).translate(_PUNCTUATION).casefold()
        for output in folded:
            if output.isspace():
                in_space = True
                continue
            if in_space and norm:
                norm.append(" ")
                offsets.append(max(0, index - 1))
            in_space = False
            norm.append(output)
            offsets.append(index)
    return "".join(norm), offsets


def _best_substring(
    content: str, quote: str, offsets: list[int], hint: int | None
) -> tuple[int, int] | None:
    """A conservative approximate quote match, returned in raw scoped offsets.

    Short quotes are deliberately excluded: changing one word in "numbers went up"
    can reverse the finding while still producing a high character-similarity score.
    Longer passages may tolerate one copied word or punctuation error, but must retain
    both high sequence similarity and most of the quote's word tokens.
    """
    quote_words = [match.group(0) for match in _WORD.finditer(quote)]
    words = list(_WORD.finditer(content))
    if len(quote_words) < _MIN_FUZZY_WORDS or len(quote) < _MIN_FUZZY_CHARS or not words:
        return None

    quote_set = set(quote_words)
    spread = max(1, round(len(quote_words) * 0.15))
    candidates: list[tuple[float, int, int, int]] = []
    for size in range(max(1, len(quote_words) - spread), len(quote_words) + spread + 1):
        for index in range(0, len(words) - size + 1):
            first = words[index]
            last = words[index + size - 1]
            candidate = content[first.start() : last.end()]
            candidate_words = [match.group(0) for match in words[index : index + size]]
            coverage = len(quote_set.intersection(candidate_words)) / len(quote_set)
            if coverage < _MIN_TOKEN_COVERAGE:
                continue
            ratio = difflib.SequenceMatcher(None, quote, candidate, autojunk=False).ratio()
            if ratio < _MIN_FUZZY_RATIO:
                continue
            raw_start = offsets[first.start()]
            raw_end = offsets[last.end() - 1] + 1
            distance = abs(raw_start - hint) if hint is not None else raw_start
            candidates.append((ratio, -distance, raw_start, raw_end))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0]
    if (
        len(candidates) > 1
        and hint is None
        and best[0] - candidates[1][0] < _AMBIGUOUS_MARGIN
        and (best[2], best[3]) != (candidates[1][2], candidates[1][3])
    ):
        # Two equally plausible fuzzy anchors are worse than an honest unanchored finding.
        return None
    return best[2], best[3]


def add_comment(
    conn: sqlite3.Connection,
    part_id: int,
    author: str,
    body: str,
    *,
    severity: str | None = None,
    quote: str | None = None,
    hint: int | None = None,
    section_ref: str | None = None,
    orphaned: bool = False,
) -> dict[str, object]:
    """File one thread root and return it.

    The anchor fields are stored as given: the caller resolves the quote against the
    text it read and passes the canonical matched text plus its offset. A caller may
    also keep a hopeless mismatch with no hint; it remains a useful unanchored finding.

    Raises:
        ValueError: on an author or severity outside the sets. A caller bug - model
            input is validated at the tool layer, where a failure travels back as a
            result the model can read.
    """
    if author not in AUTHORS:
        raise ValueError(f"Not a comment author: {author}")
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"Not a severity: {severity}")
    if _supports_section_ref(conn):
        cursor = conn.execute(
            "insert into draft_comments "
            "(part_id, author, severity, quote, hint, body, section_ref, orphaned) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (part_id, author, severity, quote, hint, body, section_ref, int(orphaned)),
        )
    else:
        cursor = conn.execute(
            "insert into draft_comments "
            "(part_id, author, severity, quote, hint, body, orphaned) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (part_id, author, severity, quote, hint, body, int(orphaned)),
        )
    conn.commit()
    return _get(conn, int(cursor.lastrowid or 0))


def add_reply(conn: sqlite3.Connection, root_id: int, author: str, body: str) -> dict[str, object]:
    """Append one reply to a thread root and return it.

    Raises:
        NotFoundError: when the root does not exist, or names a reply - threads are one
            level deep on purpose, so a reply to a reply is refused rather than nested.
        ValueError: on an author outside the set.
    """
    if author not in AUTHORS:
        raise ValueError(f"Not a comment author: {author}")
    root = _get(conn, root_id)
    if root["parent_id"] is not None:
        raise NotFoundError(_NOT_A_ROOT_MESSAGE)
    cursor = conn.execute(
        "insert into draft_comments (part_id, parent_id, author, body) values (?, ?, ?, ?)",
        (root["part_id"], root_id, author, body),
    )
    conn.commit()
    return _get(conn, int(cursor.lastrowid or 0))


def set_resolved(conn: sqlite3.Connection, comment_id: int, resolved: bool) -> dict[str, object]:
    """Resolve or reopen one thread, root only, and return the root.

    Raises:
        NotFoundError: when the comment does not exist or is a reply.
    """
    root = _get(conn, comment_id)
    if root["parent_id"] is not None:
        raise NotFoundError(_NOT_A_ROOT_MESSAGE)
    conn.execute(
        "update draft_comments set resolved = ? where id = ?",
        (1 if resolved else 0, comment_id),
    )
    conn.commit()
    return _get(conn, comment_id)


def list_threads(conn: sqlite3.Connection, part_id: int, body: str) -> list[dict[str, object]]:
    """Every thread on one part, in filing order, anchored against `body` now.

    Each root carries `anchor`: an `{start, end, exact}` mapping when its quote resolves
    against the body as it stands, else None. Resolution is also anchor maintenance: a
    root whose quote stopped resolving is flagged orphaned in the store, and one whose
    quote resolves again is un-flagged, so the orphan state the interface shows is the
    state the store holds.
    """
    if _supports_section_ref(conn):
        rows = conn.execute(
            "select id, part_id, parent_id, author, severity, quote, hint, body, "
            "resolved, orphaned, created_at, section_ref from draft_comments "
            "where part_id = ? order by created_at, id",
            (part_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "select id, part_id, parent_id, author, severity, quote, hint, body, "
            "resolved, orphaned, created_at from draft_comments "
            "where part_id = ? order by created_at, id",
            (part_id,),
        ).fetchall()

    threads: list[dict[str, object]] = []
    roots: dict[int, dict[str, object]] = {}
    flips: list[tuple[int, int]] = []
    for row in rows:
        comment = dict(row)
        comment.setdefault("section_ref", None)
        if comment["parent_id"] is None:
            anchor = None
            if comment["quote"]:
                resolved = resolve_quote(
                    body,
                    str(comment["quote"]),
                    int(comment["hint"]) if comment["hint"] is not None else None,
                )
                if resolved is not None:
                    anchor = {
                        "start": resolved.start,
                        "end": resolved.end,
                        "exact": resolved.exact,
                    }
                orphaned = 0 if resolved is not None else 1
                if orphaned != comment["orphaned"]:
                    flips.append((orphaned, int(comment["id"])))
                    comment["orphaned"] = orphaned
            comment["anchor"] = anchor
            comment["replies"] = []
            roots[int(comment["id"])] = comment
            threads.append(comment)
        else:
            root = roots.get(int(comment["parent_id"]))
            if root is not None:
                replies = root["replies"]
                assert isinstance(replies, list)  # noqa: S101 - set four lines up.
                replies.append(comment)
    if flips:
        conn.executemany("update draft_comments set orphaned = ? where id = ?", flips)
        conn.commit()
    return threads


def unresolved_threads(
    conn: sqlite3.Connection, part_id: int, body: str
) -> list[dict[str, object]]:
    """The threads still open, for the `read_comments` tool and the review summary."""
    return [thread for thread in list_threads(conn, part_id, body) if not thread["resolved"]]


def _get(conn: sqlite3.Connection, comment_id: int) -> dict[str, object]:
    """One comment row.

    Raises:
        NotFoundError: when it does not exist.
    """
    if _supports_section_ref(conn):
        row = conn.execute(
            "select id, part_id, parent_id, author, severity, quote, hint, body, "
            "resolved, orphaned, created_at, section_ref "
            "from draft_comments where id = ?",
            (comment_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "select id, part_id, parent_id, author, severity, quote, hint, body, "
            "resolved, orphaned, created_at from draft_comments where id = ?",
            (comment_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(_NO_COMMENT_MESSAGE)
    result = dict(row)
    result.setdefault("section_ref", None)
    return result


def _supports_section_ref(conn: sqlite3.Connection) -> bool:
    """Whether a newer comments migration supplied the optional section hint."""
    return any(
        str(row["name"]) == "section_ref"
        for row in conn.execute("pragma table_info(draft_comments)")
    )
