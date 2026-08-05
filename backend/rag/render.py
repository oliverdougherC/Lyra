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
import os
from pathlib import Path

import pymupdf

from backend.config import settings
from backend.core.errors import LyraError, NotFoundError
from backend.rag.parse import PDF_MIME, UNREADABLE_PDF_MESSAGE

logger = logging.getLogger(__name__)

# 144 dpi, twice the PDF default of 72. Sharp on a HiDPI display at the width the source
# pane gives it, and a page lands in the low hundreds of kilobytes rather than megabytes.
RENDER_DPI = 144

NOT_A_PAGE = "That page does not exist in this document."
NOT_RENDERABLE = "This document has no pages to show."


def pages_dir(document_id: int) -> Path:
    """Where one document's rendered pages are cached."""
    return settings.pages_dir / str(document_id)


def page_path(document_id: int, page_number: int) -> Path:
    """The cache path for one rendered page."""
    return pages_dir(document_id) / f"{page_number}.png"


def render_page(document_id: int, source: Path, mime: str, page_number: int) -> Path:
    """Render one page to PNG, returning the cached file.

    Args:
        document_id: Document the page belongs to, which names its cache directory.
        source: Path to the stored upload.
        mime: The document's stored mime. Only PDFs rasterize.
        page_number: 1-based page number as the reader sees it.

    Returns:
        Path to the PNG on disk.

    Raises:
        LyraError: The document is not a PDF, or the file could not be opened.
        NotFoundError: The document has no such page.
    """
    if mime != PDF_MIME:
        # TXT and MD have no pages to draw. The interface serves their extracted text
        # instead, which is the same anchor with a different surface.
        raise LyraError(NOT_RENDERABLE)
    if page_number < 1:
        raise NotFoundError(NOT_A_PAGE)

    cached = page_path(document_id, page_number)
    if cached.exists():
        return cached

    try:
        with pymupdf.open(source) as document:
            if page_number > document.page_count:
                raise NotFoundError(NOT_A_PAGE)
            pixmap = document[page_number - 1].get_pixmap(dpi=RENDER_DPI)
            cached.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and moved into place, because the cache is trusted
            # on the strength of the file existing. A process killed partway through a
            # direct write would leave a truncated PNG that `cached.exists()` then serves
            # for good, and nothing short of re-ingesting the document would clear it.
            # `replace` is atomic within a directory, so the name only ever appears once
            # the bytes are all there.
            partial = cached.with_name(f"{cached.name}.{os.getpid()}.partial")
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
        raise LyraError(UNREADABLE_PDF_MESSAGE) from exc

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
