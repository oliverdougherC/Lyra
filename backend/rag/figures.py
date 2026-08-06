"""Figures: the diagrams a problem refers to, pulled out of the page.

Stage 2 of docs/rag-pipeline.md. Figures are the first pipeline output that is not text,
which is why `artifact_parts` has held `kind = 'figure'` since Phase 2 with nothing
producing one.

**Embedded images only, and that is a measured decision rather than a first instalment.**
The specification also called for rendered page regions, for figures drawn with vector
paths instead of embedded as a bitmap. PyMuPDF exposes `cluster_drawings()` for exactly
that, and over the reference course it does not work: on a 112-page slide deck it reduces
2522 vector paths to 112 clusters, and every one of those clusters is the whole page,
because each page carries a full-bleed background rectangle that swallows everything into
one region. The same is true of the lecture notes and the lab handouts, at three different
page sizes. Shipping it would file one junk figure per page of every deck a student owns.
The measurement is recorded in rag-pipeline.md and the door is left open; what is not left
open is a heuristic that is wrong on everything it was tested against.

Geometry is fractions of the page box rather than points, the convention `rag/locate.py`
and `artifact_provenance.bbox` already set, because pages render as images at whatever
width the pane has.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from backend.core.errors import LyraError
from backend.rag.parse import PAGE_MIMES, unreadable_message

logger = logging.getLogger(__name__)

# Smallest thing worth calling a figure, in square points.
#
# From the corpus rather than from taste: the smallest real figure in it is a block diagram
# on homework 3 at 252x21 points, which is 5292. Nothing decorative comes close - there are
# no bullets, rules, or logos among the 69 embedded images in the reference course - so this
# sits well below the real floor rather than being tuned to it.
MIN_FIGURE_AREA = 2000.0

# And no side thinner than this, so a hairline rule saved as an image cannot clear the area
# test by being very long.
MIN_FIGURE_SIDE = 12.0

# Above this share of the page, the image is the page.
#
# A scanned page is one bitmap covering the whole sheet, and every page of the scanned
# document in the corpus measures 100.2%. Real figures top out at 53%. The gap between those
# two numbers is the entire reason this threshold can be a constant rather than a judgment.
MAX_PAGE_COVERAGE = 0.9

# A caption names its figure. `Figure 5.21`, `Fig. 3`, `Table 2-1`, `Diagram 4`.
CAPTION = re.compile(
    r"^\s*((?:figure|fig\.?|diagram|table)\s*[0-9]+(?:[.\-][0-9]+)*)\s*[:.—-]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)

# How far below a figure a caption may sit and still belong to it, in points.
#
# Every caption in the corpus starts within one point of the image it names, so this is
# generous by an order of magnitude and still nowhere near the next paragraph. It is
# deliberately not widened further: the numbered list markers on homework 3 sit 14 points
# under their diagrams, and the only thing keeping those from being read as captions is that
# they do not match the pattern above.
CAPTION_GAP = 24.0


@dataclass(frozen=True)
class Figure:
    """One figure found on one page.

    Attributes:
        page_number: 1-based page it appears on.
        index: 1-based position among the figures on that page, in reading order. This is
            what identifies a figure with no caption, and it is stable for a given file
            because the page's image list is.
        bbox: `(x0, y0, x1, y1)` as fractions of the page box.
        label: `Figure 3` where a caption was found, otherwise None. None is the common
            case and is not a failure: five of the sixty-nine figures in the reference
            course carry a caption at all.
        caption: The caption's remaining text, where there is one.
    """

    page_number: int
    index: int
    bbox: tuple[float, float, float, float]
    label: str | None
    caption: str | None


def extract_figures(path: Path, mime: str) -> list[Figure]:
    """Find every figure in a document, in page order.

    Never raises on a page it cannot read. A figure is an enrichment, and a document that
    ingests perfectly well should not fail over one.

    Args:
        path: Location of the stored upload.
        mime: The document's stored mime. Only paged formats carry figures.

    Returns:
        Every figure found, in page then reading order. Empty for a document with none,
        which is most of them.

    Raises:
        LyraError: The file could not be opened at all.
    """
    if mime not in PAGE_MIMES:
        return []

    try:
        with pymupdf.open(path) as document:
            return [
                figure
                for number, page in enumerate(document, start=1)
                for figure in _page_figures(page, number)
            ]
    except Exception as exc:
        raise LyraError(unreadable_message(mime)) from exc


def _page_figures(page: "pymupdf.Page", page_number: int) -> list[Figure]:
    """Every figure on one page, in reading order, with captions where they exist."""
    try:
        rects = _figure_rects(page)
    except Exception:
        # One unreadable page must not cost the document its other figures.
        logger.warning("Could not read figures on page %s", page_number)
        return []

    captions = _caption_blocks(page)
    box = page.rect
    figures = []
    for index, rect in enumerate(rects, start=1):
        label, caption = _caption_for(rect, captions)
        figures.append(
            Figure(
                page_number=page_number,
                index=index,
                bbox=(
                    rect.x0 / box.width,
                    rect.y0 / box.height,
                    rect.x1 / box.width,
                    rect.y1 / box.height,
                ),
                label=label,
                caption=caption,
            )
        )
    return figures


def _figure_rects(page: "pymupdf.Page") -> list["pymupdf.Rect"]:
    """Where the embedded images sit, filtered down to the ones that are figures.

    One image can be placed more than once on a page, and the same image can appear on
    several pages, so placements are collected rather than xrefs.
    """
    page_area = page.rect.width * page.rect.height
    rects = []
    for xref, *_ in page.get_images(full=True):
        for rect in page.get_image_rects(xref):
            if _is_figure(rect, page_area):
                rects.append(rect)
    # Reading order, which for a figure means down the page and then across.
    rects.sort(key=lambda rect: (round(rect.y0, 1), round(rect.x0, 1)))
    return rects


def _is_figure(rect: "pymupdf.Rect", page_area: float) -> bool:
    """Whether a placed image is a figure rather than furniture or the page itself."""
    width, height = rect.width, rect.height
    if min(width, height) < MIN_FIGURE_SIDE or width * height < MIN_FIGURE_AREA:
        return False
    return not (page_area > 0 and (width * height) / page_area >= MAX_PAGE_COVERAGE)


def _caption_blocks(page: "pymupdf.Page") -> list[tuple["pymupdf.Rect", str, str]]:
    """Text blocks that open with a caption pattern, as (rect, label, rest)."""
    blocks = []
    for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
        match = CAPTION.match(" ".join(str(text).split()))
        if match is None:
            continue
        label = " ".join(match.group(1).split())
        blocks.append((pymupdf.Rect(x0, y0, x1, y1), label, match.group(2).strip()))
    return blocks


def _caption_for(
    rect: "pymupdf.Rect", captions: list[tuple["pymupdf.Rect", str, str]]
) -> tuple[str | None, str | None]:
    """The caption belonging to one figure, or nothing.

    Below the figure and horizontally overlapping it. Below rather than either side because
    that is where every caption in the reference course sits, and a rule that also looked
    above would start claiming the sentence that introduces the figure.

    Nothing is guessed when no caption matches. The alternative on offer was to name the
    figure after the nearest numbered item, and on the acceptance document that is
    confidently wrong: the list markers on homework 3 sit *below* their diagrams, so
    "nearest preceding marker" attaches every figure to the problem before its own.
    """
    best: tuple[float, str, str] | None = None
    for caption_rect, label, text in captions:
        gap = caption_rect.y0 - rect.y1
        if gap < 0 or gap > CAPTION_GAP:
            continue
        if min(caption_rect.x1, rect.x1) <= max(caption_rect.x0, rect.x0):
            continue
        if best is None or gap < best[0]:
            best = (gap, label, text)
    if best is None:
        return None, None
    return best[1], best[2] or None
