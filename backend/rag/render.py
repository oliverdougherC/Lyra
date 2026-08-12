"""Rasterizing one page of a source document, cached on disk.

The solver's source pane shows the page a problem came from beside its solution. It
renders page images rather than embedding a PDF viewer, and that is a deliberate choice
with three payoffs: exact anchoring, so a region of a page can be outlined; identical
rendering in both themes and every browser; and no new frontend dependency. Phase 3 needs
the same rasterization for figure extraction and text recognition, so this is built once.

Rendering is not free, so every page is cached under `data/pages/{document_id}/` and
rendered only when the cache misses. The cache is disposable: deleting it costs the next
viewer a second, and nothing else reads it.
"""

import logging
import math
import os
import threading
from pathlib import Path

import pymupdf

from backend.config import settings
from backend.core.errors import LyraError, NotFoundError
from backend.rag.parse import PAGE_MIMES, unreadable_message

logger = logging.getLogger(__name__)

# 144 dpi, twice the PDF default of 72. Sharp on a HiDPI display at the width the source
# pane gives it, and a page lands in the low hundreds of kilobytes rather than megabytes.
RENDER_DPI = 144

# What text recognition reads at, per Stage 2b of docs/rag-pipeline.md.
#
# These are two different artifacts and they must not share a cache entry. A page rendered
# for reading that quietly satisfied a recognition request would degrade transcription with
# nothing on screen to say so, and a 300 dpi page served to the source pane would cost
# several times the bytes for a picture nobody looks at that closely. So the dpi is part of
# the filename.
RECOGNITION_DPI = 300

# What a cropped figure is rasterized at.
#
# Between the two above, and for a reason neither of them has: a figure occupies a fraction
# of a page and is then shown at the full width of the reading column, so its pixels are
# stretched over several times the area a page image's are. 220 keeps a block diagram's
# labels legible at that magnification without the file size of a full recognition render.
FIGURE_DPI = 220

NOT_A_PAGE = "That page does not exist in this document."
NOT_RENDERABLE = "This document has no pages to show."
TOO_LARGE_TO_RENDER = "This page is too large to render."

# The largest raster Lyra will ask PyMuPDF to allocate for one page or crop, as a pixel
# count and a per-side ceiling.
#
# `get_pixmap` scales the page rectangle (in points, 72 to the inch) by dpi/72, so a compact
# PDF can describe an enormous page - the format permits a MediaBox up to 14400pt (200in) a
# side - and ask for a single native allocation of many gigabytes at the 300 dpi recognition
# path. That spikes memory or kills the process before Python-level error handling can help,
# and the upload-byte limit does not protect against it: the geometry is what is large, not
# the file (a 40000x40000 solid PNG is under 2MB and a 14400pt PDF is a few KB). So the
# raster size is computed and checked before every `get_pixmap`.
#
# The ceiling is measured against Lyra's representative course material rather than paper
# sizes alone (docs/raster-envelope.md records the table and the measurement test that
# reproduces it). Two facts set it:
#
#   - A `get_pixmap` pixmap is 3 bytes per pixel (DeviceRGB, no alpha), so 175 megapixels is
#     ~525MB of native buffer - a few hundred megabytes, not the multi-gigabyte spike a
#     pathological page would take.
#   - PyMuPDF opens a standalone image as a one-page document whose page rectangle is the
#     decoded pixels scaled to points at 96 dpi (`rect_pt = px * 0.75`). Recognition then
#     renders that page at 300 dpi, i.e. `px * 300/96 = px * 3.125`, so an image is upscaled
#     3.125x on the recognition path where a PDF of the same visual page is not. A standard
#     12-megapixel phone photo (4032x3024) - the single most common image Lyra ingests - is
#     therefore ~119 megapixels at 300 dpi, and a 100-megapixel ceiling would refuse it.
#
# 175 megapixels admits that photo with margin, admits 16-megapixel phones (~155M) and
# A0-scale PDFs (~140M at 300 dpi), and still refuses a 24-megapixel photo (~234M), a
# 5000pt page (~434M), and the 14400pt worst case (~3.6 billion). US Letter/A4 land near
# 8.4M px at 300 dpi, A3/tabloid near 17M, A1 near 70M - all far inside. The 30000 px
# per-side cap additionally catches a needle-thin page whose area is modest but whose one
# dimension would still allocate a degenerate buffer; the largest legitimate single side
# measured (a 16-megapixel photo at 300 dpi, ~16600 px) sits well under it.
MAX_RASTER_PIXELS = 175_000_000
MAX_RASTER_DIMENSION = 30_000


def _raster_within_bounds(rect: pymupdf.Rect, dpi: int) -> bool:
    """Whether rasterizing `rect` at `dpi` stays inside the supported raster envelope.

    Mirrors what `get_pixmap` does to size its buffer - scale the rectangle from points to
    pixels at dpi/72 - so the check is against the exact allocation it would make, for a
    full page (`page.rect`) or a figure crop (the clip rectangle) alike.

    Fails closed on any geometry that does not describe a real, positive extent: a
    non-finite or non-positive side, or a non-positive dpi. `page.rect` is always a
    normalized, finite rectangle, but a figure clip is built from model-supplied bbox
    fractions and an inverted or empty bbox yields a zero-area or negative-width rectangle;
    treating that as "within bounds" would hand `get_pixmap` a rectangle nothing checked.
    """
    if dpi <= 0:
        return False
    scale = dpi / 72.0
    width = rect.width * scale
    height = rect.height * scale
    if not (math.isfinite(width) and math.isfinite(height)):
        return False
    if width <= 0 or height <= 0:
        return False
    if width > MAX_RASTER_DIMENSION or height > MAX_RASTER_DIMENSION:
        return False
    return width * height <= MAX_RASTER_PIXELS


def _partial_path(cached: Path) -> Path:
    """A writer-private name for the bytes on their way to `cached`.

    The pid alone is not private enough: FastAPI serves requests from a threadpool, so
    two concurrent renders of the same page share a pid, wrote the same partial name, and
    one request's cleanup deleted the file out from under the other's `replace`, which
    surfaced as a spurious "could not be opened". The thread id is what actually
    distinguishes two writers in this process, and the pid still keeps two processes
    (a dev server and a test run, say) out of each other's way.
    """
    return cached.with_name(f"{cached.name}.{os.getpid()}.{threading.get_ident()}.partial")


def pages_dir(document_id: int) -> Path:
    """Where one document's rendered pages are cached."""
    return settings.pages_dir / str(document_id)


def page_path(document_id: int, page_number: int, dpi: int = RENDER_DPI) -> Path:
    """The cache path for one rendered page at one resolution."""
    return pages_dir(document_id) / f"{page_number}@{dpi}.png"


def render_page(
    document_id: int, source: Path, mime: str, page_number: int, dpi: int = RENDER_DPI
) -> Path:
    """Render one page to PNG, returning the cached file.

    Args:
        document_id: Document the page belongs to, which names its cache directory.
        source: Path to the stored upload.
        mime: The document's stored mime. PDFs and uploaded images rasterize; text does
            not.
        page_number: 1-based page number as the reader sees it.
        dpi: Resolution to rasterize at. `RENDER_DPI` for reading, `RECOGNITION_DPI` for
            transcription. Each resolution caches separately, so the two never collide.

    Returns:
        Path to the PNG on disk.

    Raises:
        LyraError: The document has no pages to draw, or the file could not be opened.
        NotFoundError: The document has no such page.
    """
    if mime not in PAGE_MIMES:
        # TXT and MD have no pages to draw. The interface serves their extracted text
        # instead, which is the same anchor with a different surface.
        raise LyraError(NOT_RENDERABLE)
    if page_number < 1:
        raise NotFoundError(NOT_A_PAGE)

    cached = page_path(document_id, page_number, dpi)
    if cached.exists():
        # A cached page is served without re-checking the envelope, and that is safe even if
        # the envelope has since tightened: the file exists only because a prior render
        # already allocated its pixmap and completed the atomic write below, so the
        # allocation this bound guards against has already happened and cannot happen again
        # by reading bytes back. The write is atomic (partial then rename) and a refused
        # render never reaches it, so `exists()` never sees a partial file either. Nothing
        # is gained by invalidating caches on a limit change, so they are left in place.
        return cached

    try:
        with pymupdf.open(source) as document:
            if page_number > document.page_count:
                raise NotFoundError(NOT_A_PAGE)
            page = document[page_number - 1]
            # Checked before the allocation, and before any cache file is created, so a
            # pathological page is refused cleanly rather than crashing the process and
            # leaves nothing behind to poison a later request for the same page.
            if not _raster_within_bounds(page.rect, dpi):
                raise LyraError(TOO_LARGE_TO_RENDER)
            pixmap = page.get_pixmap(dpi=dpi)
            cached.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved into place, because the cache is trusted
            # on the strength of the file existing. A process killed partway through a
            # direct write would leave a truncated PNG that `cached.exists()` then serves
            # for good, and nothing short of re-ingesting the document would clear it.
            # `replace` is atomic within a directory, so the name only ever appears once
            # the bytes are all there.
            partial = _partial_path(cached)
            try:
                # `output` is named rather than left to the extension: PyMuPDF picks the
                # format from the filename, and the temporary name does not end in `.png`.
                pixmap.save(partial, output="png")
                partial.replace(cached)
            finally:
                partial.unlink(missing_ok=True)
    except (LyraError, NotFoundError):
        raise
    except Exception as exc:
        # PyMuPDF raises several unrelated types and puts the absolute path in every
        # message, so the whole call is converted rather than filtered.
        logger.warning("Could not render page %s of document %s", page_number, document_id)
        raise LyraError(unreadable_message(mime)) from exc

    return cached


def figure_path(document_id: int, figure_id: int) -> Path:
    """The cache path for one cropped figure."""
    return pages_dir(document_id) / f"figure-{figure_id}.png"


def render_figure(
    document_id: int,
    source: Path,
    mime: str,
    page_number: int,
    figure_id: int,
    bbox: tuple[float, float, float, float],
) -> Path:
    """Crop one figure out of its page and cache it as a PNG.

    Rendered from the page rather than pulled out with `extract_image`, deliberately. The
    stored image is the figure's own pixels at its own resolution, which sounds better and
    is worse: a diagram embedded as a 2638x219 bitmap and placed 252 points wide arrives
    enormous, a figure drawn as several stacked images arrives in pieces, and a figure with
    a transparent background arrives unreadable on a dark surface. Cropping the composed
    page gives what the page shows.

    Args:
        document_id: Document the figure belongs to, which names its cache directory.
        source: Path to the stored upload.
        mime: The document's stored mime.
        page_number: 1-based page the figure sits on.
        figure_id: Row id, which names the cache entry.
        bbox: `(x0, y0, x1, y1)` as fractions of the page box.

    Returns:
        Path to the PNG on disk.

    Raises:
        LyraError: The document has no pages to draw, or the file could not be opened.
        NotFoundError: The document has no such page.
    """
    if mime not in PAGE_MIMES:
        raise LyraError(NOT_RENDERABLE)

    cached = figure_path(document_id, figure_id)
    if cached.exists():
        # Served without re-checking the envelope, for the reason `render_page` documents:
        # the crop's pixmap was already allocated when this file was written, so reading it
        # back allocates nothing this bound needs to guard.
        return cached

    try:
        with pymupdf.open(source) as document:
            if page_number < 1 or page_number > document.page_count:
                raise NotFoundError(NOT_A_PAGE)
            page = document[page_number - 1]
            box = page.rect
            clip = pymupdf.Rect(
                box.x0 + bbox[0] * box.width,
                box.y0 + bbox[1] * box.height,
                box.x0 + bbox[2] * box.width,
                box.y0 + bbox[3] * box.height,
            )
            # A clipped pixmap allocates only the crop, so the bound is on the clip rather
            # than the whole page - but a pathological page makes even a fractional crop
            # enormous, so the same envelope applies here.
            if not _raster_within_bounds(clip, FIGURE_DPI):
                raise LyraError(TOO_LARGE_TO_RENDER)
            # Higher than the page pane reads at, because a figure is shown at the width of
            # the reading column while occupying a fraction of a page: the same pixels
            # stretched over several times the area.
            pixmap = page.get_pixmap(dpi=FIGURE_DPI, clip=clip)
            cached.parent.mkdir(parents=True, exist_ok=True)
            partial = _partial_path(cached)
            try:
                pixmap.save(partial, output="png")
                partial.replace(cached)
            finally:
                partial.unlink(missing_ok=True)
    except (LyraError, NotFoundError):
        raise
    except Exception as exc:
        logger.warning("Could not render figure %s of document %s", figure_id, document_id)
        raise LyraError(unreadable_message(mime)) from exc

    return cached


def discard_pages(document_id: int) -> None:
    """Drop a document's rendered pages.

    Called when the document is deleted or re-ingested. A stale image is worse than a
    missing one: it would show the reader a page from a file that is no longer there.
    """
    directory = pages_dir(document_id)
    if not directory.exists():
        return
    for page in directory.glob("*.png"):
        page.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        # Something is still in there: a page being rendered right now, or a partial file
        # from a write that was interrupted. The pages themselves are gone, which is what
        # this function is for, and the empty directory costs nothing. Deleting a document
        # while its source pane is loading a page must not fail the delete, which has
        # already been committed by the time this runs.
        logger.debug("Left the page cache directory for document %s in place", document_id)
