"""Text extraction for the formats Lyra accepts, with scanned-page detection.

Phase 1 read text-based PDFs, TXT, and MD. A page that yields almost no text is an image
of text rather than text, and it is dropped here rather than embedded as an empty chunk: a
scanned PDF would otherwise ingest "successfully" and then answer nothing. Callers
distinguish "some pages were scanned" from "all of them were" by comparing `pages` against
`pages_total`, which is the count before anything was dropped.

Phase 3 adds PNG and JPG uploads, which PyMuPDF opens as one-page documents, so they need
no parse path of their own. They land here as a single page with no text at all, which is
exactly what a scan is, and recognition picks them up from there.

Recognition itself is deliberately not a parse path. It is minutes of model time rather
than a decode, it is opt-in per document, and its results are spliced back in by
`backend.core.recognition`. Nothing in this module knows it exists.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from backend.core.errors import LyraError

logger = logging.getLogger(__name__)

# A page below this many non-whitespace characters counts as scanned. Real pages of prose
# clear it by two orders of magnitude, and a scanned page usually extracts nothing at all,
# so the exact value only has to separate "a stray page number" from "a paragraph".
SCANNED_PAGE_MIN_CHARS = 20

# The junk-text-layer gate, for a photographed page whose "text" is garbage rather than
# absent. The roadmap's case: a phone screenshot of a page ingested `ready` because its
# text layer extracted as a Gmail URL, which cleared the character count above while
# saying nothing the page says. Both halves of the gate are required together, and that
# conjunction is the whole design: a sparse title page carries little text but no
# page-filling image, and a matrix-heavy page of lone digits carries little *alphabetic*
# text but no page-filling image either, so each stays readable. Only a page that is
# mostly one raster image *and* has almost no real words joins the scanned flow, where
# recognition can read it properly.
#
# Alphabetic characters, counted after URLs are stripped, because a URL is exactly the
# junk this gate exists for and is stuffed with letters. Fifteen is under half of the
# shortest real caption line, and far above what a stray watermark leaves behind.
_JUNK_TEXT_MAX_ALPHA = 15
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

# How much of the page one raster image has to cover before the page counts as being that
# image. A photographed page is the image edge to edge, or nearly; a text page with a
# large figure rarely gives one image more than half the page, and its caption alone
# clears the alphabetic floor anyway.
_PHOTO_MIN_IMAGE_SHARE = 0.8

PDF_MIME = "application/pdf"
TEXT_MIMES = frozenset({"text/plain", "text/markdown"})

# Images PyMuPDF decodes, checked against 1.28.0 rather than taken from its format list.
#
# WebP is deliberately absent, and this is a measured absence rather than an oversight.
# ui-phase-3.md listed it among the accepted types; MuPDF's build here refuses a real
# `cwebp` file outright, and the only way to accept one would be a new image dependency
# carried for a format a student's scan is very unlikely to be in. The dropzone copy names
# what actually works.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg"})

# Every mime that rasterizes into pages, which is the same set `rag.render` will draw.
PAGE_MIMES = frozenset({PDF_MIME, *IMAGE_MIMES})
SUPPORTED_MIMES = frozenset({*PAGE_MIMES, *TEXT_MIMES})

# Pages join with a blank line so that `full_text` reads as one document and a page break
# never fuses the last line of one page onto the first line of the next.
PAGE_SEPARATOR = "\n\n"

UNSUPPORTED_MESSAGE = "Unsupported file type. Upload a PDF, TXT, MD, PNG, or JPG file."
UNREADABLE_PDF_MESSAGE = "This PDF could not be opened. It may be damaged or password protected."
UNREADABLE_IMAGE_MESSAGE = "This image could not be opened. It may be damaged."
UNREADABLE_FILE_MESSAGE = "This file could not be read."


def unreadable_message(mime: str) -> str:
    """What to tell the user when a file of this kind will not open.

    A PDF that will not open may be password protected; an image cannot be, and telling
    someone their PNG might need a password sends them looking for one.
    """
    return UNREADABLE_IMAGE_MESSAGE if mime in IMAGE_MIMES else UNREADABLE_PDF_MESSAGE


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
class OutlineEntry:
    """One entry of a PDF's own table of contents.

    Attributes:
        depth: Nesting level as the outline records it, 1 for a chapter.
        title: The entry's text, exactly as the outline writes it. Commercial textbooks
            frequently leave the section number out of it, so this is a title and not a
            label.
        page_number: 1-based page the entry points at. Worth distrusting by one: a
            destination lands where the heading sits, which is often partway down a page
            the previous section is still using.
    """

    depth: int
    title: str
    page_number: int


@dataclass(frozen=True)
class ParsedDocument:
    """The readable part of a document, plus what was left behind.

    Attributes:
        pages: Pages that yielded text, in order. Empty when every page was scanned.
        pages_total: Pages the file contained, counted before scanned pages were dropped.
        pages_skipped: Pages dropped for lack of extractable text.
        outline: The document's own table of contents, empty when it has none. Plenty of
            legitimate documents have none, so an empty outline is a fact rather than a
            failure.
    """

    pages: list[ParsedPage]
    pages_total: int
    pages_skipped: int
    outline: list[OutlineEntry] = field(default_factory=list)

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
    outline: list[OutlineEntry] = []
    if mime in PAGE_MIMES:
        pages, outline = _read_pages(path, mime)
    elif mime in TEXT_MIMES:
        pages = _read_text_file(path)
    else:
        raise LyraError(UNSUPPORTED_MESSAGE)

    return _drop_scanned_pages(pages, outline)


def is_scanned_page(text: str) -> bool:
    """Whether a page's extracted text is too sparse to be text at all."""
    return len("".join(text.split())) < SCANNED_PAGE_MIN_CHARS


def _is_photographed_page(page: "pymupdf.Page", text: str) -> bool:
    """Whether a page is a photograph wearing a junk text layer.

    The cheap textual half runs first, so the display list is only ever consulted for
    pages that are nearly wordless - which on a real book is almost none of them.
    """
    stripped = _URL.sub("", text)
    if sum(character.isalpha() for character in stripped) >= _JUNK_TEXT_MAX_ALPHA:
        return False
    return _largest_image_share(page) >= _PHOTO_MIN_IMAGE_SHARE


def _largest_image_share(page: "pymupdf.Page") -> float:
    """The fraction of the page its biggest raster image covers, 0.0 when unknowable.

    The largest single image rather than a union of all of them, deliberately: the gate
    is after a photograph placed as one image, and summing many small decorations could
    only create false positives.
    """
    try:
        infos = page.get_image_info()
    except Exception:
        # Failing to inspect a page's images must never fail its parse; without the
        # measurement the gate simply does not fire and the page stays readable.
        return 0.0

    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0

    share = 0.0
    for info in infos:
        x0, y0, x1, y1 = info.get("bbox", (0.0, 0.0, 0.0, 0.0))
        share = max(share, max(x1 - x0, 0.0) * max(y1 - y0, 0.0) / page_area)
    return share


def _read_pages(path: Path, mime: str) -> tuple[list[tuple[int, str, bool]], list[OutlineEntry]]:
    """Every page as (1-based number, text, photographed), plus the document's outline.

    Serves images as well as PDFs, because PyMuPDF opens a PNG or a JPG as a one-page
    document and asking it for the text of that page correctly returns nothing. An image
    has no outline, and `get_toc` says so rather than failing.

    The photographed flag is measured here rather than in `_drop_scanned_pages` because
    it needs the open page object, and only page-based formats have one.
    """
    try:
        with pymupdf.open(path) as document:
            pages: list[tuple[int, str, bool]] = []
            for number, page in enumerate(document, start=1):
                text = page.get_text()
                pages.append((number, text, _is_photographed_page(page, text)))
            return pages, _read_outline(document)
    except Exception as exc:
        # PyMuPDF raises several unrelated types here (FileDataError, FileNotFoundError,
        # RuntimeError out of the C layer) and puts the absolute path in every message, so
        # the whole call is converted rather than filtered.
        raise LyraError(unreadable_message(mime)) from exc


def _read_text_file(path: Path) -> list[tuple[int, str, bool]]:
    """A plain-text or Markdown file as a single page numbered 1.

    Undecodable bytes are replaced rather than raising: one stray byte in an otherwise
    readable file is not a reason to refuse the document. A text file cannot carry a
    raster image, so it can never be a photographed page.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LyraError(UNREADABLE_FILE_MESSAGE) from exc
    return [(1, text, False)]


def _read_outline(document: "pymupdf.Document") -> list[OutlineEntry]:
    """The PDF's table of contents, or nothing at all.

    Never raises. An outline is a convenience that makes a book addressable by section,
    and a malformed one is not a reason to refuse a document that reads perfectly well.
    """
    try:
        toc = document.get_toc()
    except Exception:
        logger.warning("Could not read the outline of a document that otherwise parsed")
        return []

    entries: list[OutlineEntry] = []
    for row in toc:
        # (depth, title, page). A page of 0 or below means the entry points at nothing,
        # which PyMuPDF reports for a link it could not resolve.
        if len(row) < 3 or int(row[2]) < 1:
            continue
        title = str(row[1]).strip()
        if title:
            entries.append(OutlineEntry(depth=int(row[0]), title=title, page_number=int(row[2])))
    return entries


def _drop_scanned_pages(
    pages: list[tuple[int, str, bool]], outline: list[OutlineEntry]
) -> ParsedDocument:
    """Keep the pages that carry real text, and count the ones that do not.

    A photographed page with a junk text layer is dropped the same way a blank scan is:
    both are pictures of text, and both belong to the recognition flow rather than to the
    index. Keeping the junk would be worse than keeping nothing - it embeds and retrieves
    text the page does not say.
    """
    kept = [
        ParsedPage(page_number=number, text=text)
        for number, text, photographed in pages
        if not (is_scanned_page(text) or photographed)
    ]
    return ParsedDocument(
        pages=kept,
        pages_total=len(pages),
        pages_skipped=len(pages) - len(kept),
        outline=outline,
    )
