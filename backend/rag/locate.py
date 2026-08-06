"""Finding where on a page a problem begins.

The solutions pane and the source page sit side by side, and until this existed the only
thing joining them was a page number: the reader could see the sheet, and could see the
solution, and had to match them up by eye. This gives the page image something to be
clicked on.

What is found is deliberately modest: the line the problem's own label sits on, and
nothing else. A full outline of a problem's region would mean deciding where a problem
ends, which is exactly the judgement segmentation already makes from the text, and making
it a second time from geometry would let the two disagree. The frontend runs a band from
one marker to the next instead, so the answer to "where does problem 3 end" is always
"where problem 4 starts".

A page is searched for all of its labels at once rather than one at a time, because a
marker is only unique in the context of the ones around it: a sheet with two sections both
numbered from 1 writes `1.` twice, and there is nothing in the label itself to say which
of them a given problem means. `find_labels` is therefore the interface, and `find_label`
is the single-problem reading of it.
"""

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

# Coordinates are fractions of the page box, because pages are rendered as images at
# whatever width the pane happens to have.
Rect = tuple[float, float, float, float]

# A label as the sheet writes it: `Problem 3`, `3.`, `Q4)`. Only the leading marker is
# searched for, because the rest of a label is the model's reading of the title and may
# not be on the page verbatim.
_MARKER = re.compile(r"^\s*((?:problem|exercise|question|q|p)?\s*\.?\s*\d+[a-z]?)", re.IGNORECASE)

# One trailing parenthetical, which a model adds to say what a problem is about.
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")

# The shortest free text worth searching a page for. Below this a label is not specific
# enough to be sure the first hit is the heading rather than a word inside a sentence.
_MIN_TEXT_MARKER = 8

# How far apart two hits must be vertically, in points, before one is *below* the other
# rather than beside it. A point is far less than a line and far more than the rounding in
# a glyph box, so two markers on one line are ordered left to right and two markers on
# consecutive lines are ordered down the page.
_SAME_LINE = 1.0


def _marker_of(label: str) -> str | None:
    """The searchable part of a problem label, or None when there is nothing to search."""
    match = _MARKER.match(label)
    if not match:
        return None
    marker = " ".join(match.group(1).split())
    return marker or None


def _searches_for(label: str) -> list[str]:
    """What to look for on the page, most specific first.

    A numbered sheet is searched by its marker, which is precise and short. A sheet whose
    problems are *named* rather than numbered has no marker at all, and until this existed
    it got no position and nothing on its page image to click: `Linearity and Time-Invariance`
    matches nothing in a pattern that requires a digit. For those the label is the heading,
    written on the page exactly as segmentation recorded it, so the label itself is the
    thing to search for.

    The title is tried after the marker rather than instead of it, so a numbered sheet keeps
    the precise short match it already had and only falls through when that misses.
    """
    cleaned = " ".join(label.split())
    if not cleaned:
        return []

    searches: list[str] = []
    marker = _marker_of(cleaned)
    if marker:
        searches.append(marker)
        # A sheet that writes "Problem 3" matches the first; one that writes "3." only
        # matches this. The bare number is never searched on its own, because "3" appears
        # in every equation on the page and the first hit would be one of those.
        if " " in marker:
            searches.append(marker.split()[-1] + ".")

    # The label as written, then without the parenthetical gloss a model tends to append.
    for candidate in (cleaned, _TRAILING_PARENTHETICAL.sub("", cleaned)):
        if len(candidate) >= _MIN_TEXT_MARKER and candidate not in searches:
            searches.append(candidate)
    return searches


def _reading_order(rect: "pymupdf.Rect") -> tuple[float, float]:
    """A hit's place in the order a page is read, down first and then across."""
    return (rect.y0, rect.x0)


def _is_after(rect: "pymupdf.Rect", cursor: "pymupdf.Rect") -> bool:
    """Whether `rect` comes later on the page than `cursor`."""
    if rect.y0 > cursor.y0 + _SAME_LINE:
        return True
    return abs(rect.y0 - cursor.y0) <= _SAME_LINE and rect.x0 > cursor.x0 + _SAME_LINE


def find_labels(path: Path, page_number: int, labels: Sequence[str]) -> list[Rect | None]:
    """Where each of one page's problem labels sits, as fractions of the page box.

    A page's labels are resolved together, in the order the document puts them in, and
    each one takes the first occurrence of its marker that sits *after* the last label
    placed. Resolving them one at a time cannot work, and the failure is not exotic: a
    sheet whose second section numbers from 1 again writes `1.` twice, so both sections'
    first problem took the first `1.` on the page and the solver drew its highlight band
    at the wrong marker. On the acceptance homework that collapsed twelve problems onto
    nine positions, and it is what stopped figures being paired to the problems they
    belong to, because pairing needs each problem to have a position of its own.

    A label whose marker appears only above the cursor falls back to the topmost hit and
    leaves the cursor where it was. That is the answer the old rule gave, so a page this
    cannot walk in order is never worse off than before, and one label the sheet writes in
    an order nobody expected cannot drag the rest of the page down with it.

    Args:
        path: The PDF on disk.
        page_number: 1-based page these problems start on.
        labels: What the sheet calls each problem, in document order, as segmentation
            recorded them.

    Returns:
        One entry per label, in the same order: `(x0, y0, x1, y1)` in 0..1, or None where
        the page, the file, or that marker could not be found. Never raises: a source page
        that cannot be searched costs a convenience, and losing the solution over it would
        be absurd.
    """
    if not labels:
        return []

    try:
        with pymupdf.open(path) as document:
            if not 1 <= page_number <= document.page_count:
                return [None] * len(labels)
            page = document[page_number - 1]
            box = page.rect
            if not box.width or not box.height:
                return [None] * len(labels)
            found = _walk_page(page, labels)
    except Exception:
        logger.warning(
            "Could not search %s page %d for %d labels", path.name, page_number, len(labels)
        )
        return [None] * len(labels)

    return [
        None
        if rect is None
        else (
            max(0.0, rect.x0 / box.width),
            max(0.0, rect.y0 / box.height),
            min(1.0, rect.x1 / box.width),
            min(1.0, rect.y1 / box.height),
        )
        for rect in found
    ]


def _walk_page(page: "pymupdf.Page", labels: Sequence[str]) -> list["pymupdf.Rect | None"]:
    """Place each label on the page in turn, never going back up the page to do it."""
    found: list[pymupdf.Rect | None] = []
    cursor: pymupdf.Rect | None = None

    for label in labels:
        hits = _hits_for(page, label)
        if not hits:
            found.append(None)
            continue
        ahead = [hit for hit in hits if cursor is None or _is_after(hit, cursor)]
        if ahead:
            cursor = ahead[0]
            found.append(cursor)
        else:
            # Nothing left below the cursor, so this is the old answer: the topmost hit.
            # A marker repeated further down the page is a reference back to the problem
            # rather than the problem itself.
            found.append(hits[0])
    return found


def _hits_for(page: "pymupdf.Page", label: str) -> list["pymupdf.Rect"]:
    """Every place one label could be on the page, in reading order.

    The searches are tried most specific first and the first one that matches anything
    wins, so a numbered sheet is placed by its short precise marker and only a sheet with
    no marker at all falls through to matching its title.
    """
    for search in _searches_for(label):
        hits = page.search_for(search)
        if hits:
            return sorted(hits, key=_reading_order)
    return []


def find_label(path: Path, page_number: int, label: str) -> Rect | None:
    """Where one problem's label sits on its page, as fractions of the page box.

    The single-label reading of `find_labels`, which is all a caller holding one problem
    needs. A caller holding a whole page's worth should use `find_labels` instead, so that
    two problems numbered the same do not both land on the first of them.
    """
    return find_labels(path, page_number, [label])[0]
