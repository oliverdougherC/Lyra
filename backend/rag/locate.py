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


def _marker_of(label: str) -> str | None:
    """The searchable part of a problem label, or None when there is nothing to search."""
    match = _MARKER.match(label)
    if not match:
        return None
    marker = " ".join(match.group(1).split())
    return marker or None


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
    marker = _marker_of(label)
    if marker is None:
        return None

    try:
        with pymupdf.open(path) as document:
            if not 1 <= page_number <= document.page_count:
                return None
            page = document[page_number - 1]
            box = page.rect
            if not box.width or not box.height:
                return None
            # Tried whole first, then without the word: a sheet that writes "Problem 3"
            # matches the first, and one that writes "3." only matches the second. The
            # bare number is not searched on its own, because "3" appears in every
            # equation on the page and the first hit would be one of those.
            hits = page.search_for(marker)
            if not hits and " " in marker:
                hits = page.search_for(marker.split()[-1] + ".")
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
