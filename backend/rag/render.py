"""Rasterizing one page of a source document, cached on disk.

The solver's source pane shows the page a problem came from beside its solution. It
renders page images rather than embedding a PDF viewer, and that is a deliberate choice
with three payoffs: exact anchoring, so a region of a page can be outlined; identical
rendering in both themes and every browser; and no new frontend dependency. Phase 3 needs
the same rasterization for figure extraction and text recognition, so this is built once.

Rendering is not free, so every page is cached under the configured cache root's
`pages/{document_id}/` and rendered only when the cache misses. Source runs default to
the data root; packaged runs keep this cache separate from durable application data.
The cache is disposable: deleting it costs the next viewer a second, and nothing else
reads it.
"""

import logging
import math
from pathlib import Path

import pymupdf

from backend.config import settings
from backend.core import ownership
from backend.core.errors import LyraError, NotFoundError
from backend.rag.parse import PAGE_MIMES, unreadable_message
from backend.storage import private

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


def pages_dir(document_id: int) -> Path:
    """Where one document's rendered pages are cached."""
    return settings.pages_dir / str(document_id)


def page_path(document_id: int, page_number: int, dpi: int = RENDER_DPI) -> Path:
    """The cache path for one rendered page at one resolution."""
    return pages_dir(document_id) / f"{page_number}@{dpi}.png"


def render_page(
    document_id: int,
    source: Path,
    mime: str,
    page_number: int,
    dpi: int = RENDER_DPI,
    *,
    created_at: str,
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
        created_at: The document row's `created_at` as the caller read it. Rasterization
            can outlive the document - a delete is allowed mid-render - so the publication
            is guarded: the cache file appears only if the row still exists with this
            identity at the moment of the rename (docs/storage-consistency.md).

    Returns:
        Path to the PNG on disk.

    Raises:
        LyraError: The document has no pages to draw, or the file could not be opened.
        NotFoundError: The document has no such page, or was deleted or replaced while
            the page was being rendered.
    """
    if mime not in PAGE_MIMES:
        # TXT and MD have no pages to draw. The interface serves their extracted text
        # instead, which is the same anchor with a different surface.
        raise LyraError(NOT_RENDERABLE)
    if page_number < 1:
        raise NotFoundError(NOT_A_PAGE)

    cached = page_path(document_id, page_number, dpi)
    if private.regular_file_present(cached):
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
            private.secure_mkdir(cached.parent, root=settings.cache_dir or settings.data_dir)
            # Staged beside the target and moved into place, because the cache is trusted
            # on the strength of the file existing: a process killed partway through a
            # direct write would leave a truncated PNG that `exists()` then serves for
            # good. `tobytes` is used rather than `Pixmap.save(path)` so the actual
            # pathname write goes through the O_NOFOLLOW `0o600` writer from its first
            # byte; `save` cannot offer that guarantee and may follow a planted symlink.
            # The raster envelope bounds this encoding's memory: the pixmap already
            # occupies up to ~525 MB and its PNG bytes are a bounded derivative of it.
            # Publication is conditional on the document still existing unchanged: the
            # rasterization above can outlast a delete, and its cache file must not
            # reappear after the delete's cleanup already ran.
            if not ownership.publish_current_document(
                document_id, created_at, cached, pixmap.tobytes("png")
            ):
                raise NotFoundError("That document does not exist.")
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
    *,
    created_at: str,
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
        created_at: The document row's `created_at` as the caller read it; the crop is
            published only if the row still exists with this identity, exactly as a
            rendered page is.

    Returns:
        Path to the PNG on disk.

    Raises:
        LyraError: The document has no pages to draw, or the file could not be opened.
        NotFoundError: The document has no such page, or was deleted or replaced while
            the figure was being rendered.
    """
    if mime not in PAGE_MIMES:
        raise LyraError(NOT_RENDERABLE)

    cached = figure_path(document_id, figure_id)
    if private.regular_file_present(cached):
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
            private.secure_mkdir(cached.parent, root=settings.cache_dir or settings.data_dir)
            if not ownership.publish_current_document(
                document_id, created_at, cached, pixmap.tobytes("png")
            ):
                raise NotFoundError("That document does not exist.")
    except (LyraError, NotFoundError):
        raise
    except Exception as exc:
        logger.warning("Could not render figure %s of document %s", figure_id, document_id)
        raise LyraError(unreadable_message(mime)) from exc

    return cached


def discard_pages(document_id: int) -> bool:
    """Drop a document's rendered pages, reporting whether the cache is provably gone.

    Called when the document is deleted or re-ingested. A stale image is worse than a
    missing one: it would show the reader a page from a file that is no longer there.

    This runs from the delete path and from startup recovery, so it is held to the same
    owned-path/no-follow contract as the rest of private storage: the cache directory is
    reached by O_NOFOLLOW descent from the configured cache root, a symlink planted where the
    directory belongs is removed as a link - its target is never entered, globbed, or
    touched - and an entry inside the directory is unlinked only when it is a regular
    file or a link (which `unlink` removes as a link). Staged `*.partial` files go too:
    they are derived from the same file the pages were, and a leftover from a killed
    writer would otherwise pin the directory forever. Nothing here can be steered at
    files outside the Lyra cache tree.

    Returns:
        True when the cache directory is gone afterward - the goal state of a durable
        delete. False when anything remains: an entry that could not be inspected or
        removed, an unexpected subdirectory (skipped rather than recursed into, and
        left deliberately visible), or a directory that would not go away. A caller on
        the durable delete path must treat False as incomplete cleanup and keep its
        storage intent; the re-ingest path may log it and continue, because a page
        being rendered right now must not fail a re-ingest.

    Raises:
        PrivacyContractError: a symlink or non-directory blocks the owned path down to
            the cache directory; nothing was touched.
        OSError: the tree down to the cache directory could not be inspected, so
            whether the cache exists is unknown; nothing was touched, and a durable
            caller keeps its intent rather than settling on a guess.
    """
    return private.clear_owned_dir(
        pages_dir(document_id),
        root=settings.cache_dir or settings.data_dir,
        patterns=("*.png", f"*{private.PARTIAL_SUFFIX}"),
    )
