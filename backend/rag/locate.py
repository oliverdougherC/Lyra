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
"""

import logging
import re
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


def find_label(path: Path, page_number: int, label: str) -> Rect | None:
    """Where a problem's label sits on its page, as fractions of the page box.

    Args:
        path: The PDF on disk.
        page_number: 1-based page the problem starts on.
        label: What the sheet calls the problem, as segmentation recorded it.

    Returns:
        `(x0, y0, x1, y1)` in 0..1, or None when the page, the file, or the marker could
        not be found. Never raises: a source page that cannot be searched costs a
        convenience, and losing the solution over it would be absurd.
    """
    searches = _searches_for(label)
    if not searches:
        return None

    try:
        with pymupdf.open(path) as document:
            if not 1 <= page_number <= document.page_count:
                return None
            page = document[page_number - 1]
            box = page.rect
            if not box.width or not box.height:
                return None
            hits: list[pymupdf.Rect] = []
            for search in searches:
                hits = page.search_for(search)
                if hits:
                    break
            if not hits:
                return None
            # The topmost hit. A marker repeated further down the page is a reference back
            # to the problem, not the problem itself.
            found = min(hits, key=lambda rect: rect.y0)
    except Exception:
        logger.warning("Could not search %s page %d for %r", path.name, page_number, label)
        return None

    return (
        max(0.0, found.x0 / box.width),
        max(0.0, found.y0 / box.height),
        min(1.0, found.x1 / box.width),
        min(1.0, found.y1 / box.height),
    )
