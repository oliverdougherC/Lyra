"""Text extraction for the formats Phase 1 accepts, with scanned-page detection.

Phase 1 reads text-based PDFs, TXT, and MD. A page that yields almost no text is an
image of text rather than text, and it is dropped here rather than embedded as an empty
chunk: a scanned PDF would otherwise ingest "successfully" and then answer nothing.
Callers distinguish "some pages were scanned" from "all of them were" by comparing
`pages` against `pages_total`, which is the count before anything was dropped.

OCR is Phase 3. When it lands it becomes an additional parse path here; nothing in this
module assumes it exists.
"""

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from backend.core.errors import LyraError

# A page below this many non-whitespace characters counts as scanned. Real pages of prose
# clear it by two orders of magnitude, and a scanned page usually extracts nothing at all,
# so the exact value only has to separate "a stray page number" from "a paragraph".
SCANNED_PAGE_MIN_CHARS = 20

PDF_MIME = "application/pdf"
TEXT_MIMES = frozenset({"text/plain", "text/markdown"})
SUPPORTED_MIMES = frozenset({PDF_MIME, *TEXT_MIMES})

# Pages join with a blank line so that `full_text` reads as one document and a page break
# never fuses the last line of one page onto the first line of the next.
PAGE_SEPARATOR = "\n\n"

UNSUPPORTED_MESSAGE = "Unsupported file type. Upload a PDF, TXT, or MD file."
UNREADABLE_PDF_MESSAGE = "This PDF could not be opened. It may be damaged or password protected."
UNREADABLE_FILE_MESSAGE = "This file could not be read."


@dataclass(frozen=True)
class ParsedPage:
    """One page that yielded text.

    Attributes:
        page_number: 1-based page number as the reader sees it, preserved through
            chunking so a retrieved chunk can be cited back to a page.
        text: The page's extracted text, unmodified.
    """

    page_number: int
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    """The readable part of a document, plus what was left behind.

    Attributes:
        pages: Pages that yielded text, in order. Empty when every page was scanned.
        pages_total: Pages the file contained, counted before scanned pages were dropped.
        pages_skipped: Pages dropped for lack of extractable text.
    """

    pages: list[ParsedPage]
    pages_total: int
    pages_skipped: int

    @property
    def full_text(self) -> str:
        """Every readable page joined into one document."""
        return PAGE_SEPARATOR.join(page.text for page in self.pages)


def parse_document(path: Path, mime: str) -> ParsedDocument:
    """Extract text from a supported file, dropping pages that have none.

    Args:
        path: Location of the stored upload.
        mime: Mime type resolved at upload time from the file extension.

    Returns:
        The readable pages, plus the total and skipped page counts.

    Raises:
        LyraError: The mime type is not one Phase 1 reads, or the file could not be
            opened. The message is written for the user and never names the path.
    """
    if mime == PDF_MIME:
        pages = _read_pdf(path)
    elif mime in TEXT_MIMES:
        pages = _read_text_file(path)
    else:
        raise LyraError(UNSUPPORTED_MESSAGE)

    return _drop_scanned_pages(pages)


def is_scanned_page(text: str) -> bool:
    """Whether a page's extracted text is too sparse to be text at all."""
    return len("".join(text.split())) < SCANNED_PAGE_MIN_CHARS


def _read_pdf(path: Path) -> list[tuple[int, str]]:
    """Every PDF page as (1-based number, text)."""
    try:
        with pymupdf.open(path) as document:
            return [(number, page.get_text()) for number, page in enumerate(document, start=1)]
    except Exception as exc:
        # PyMuPDF raises several unrelated types here (FileDataError, FileNotFoundError,
        # RuntimeError out of the C layer) and puts the absolute path in every message, so
        # the whole call is converted rather than filtered.
        raise LyraError(UNREADABLE_PDF_MESSAGE) from exc


def _read_text_file(path: Path) -> list[tuple[int, str]]:
    """A plain-text or Markdown file as a single page numbered 1.

    Undecodable bytes are replaced rather than raising: one stray byte in an otherwise
    readable file is not a reason to refuse the document.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LyraError(UNREADABLE_FILE_MESSAGE) from exc
    return [(1, text)]


def _drop_scanned_pages(pages: list[tuple[int, str]]) -> ParsedDocument:
    """Keep the pages that carry text, and count the ones that do not."""
    kept = [
        ParsedPage(page_number=number, text=text)
        for number, text in pages
        if not is_scanned_page(text)
    ]
    return ParsedDocument(
        pages=kept,
        pages_total=len(pages),
        pages_skipped=len(pages) - len(kept),
    )
