"""The junk-text-layer gate: a photographed page must not ingest as readable text.

The roadmap's case, built as a real fixture: a phone screenshot of a page whose text
layer extracts as a Gmail URL used to count as readable, ingest `ready`, and index text
the page does not say. The gate requires BOTH almost no alphabetic text after URLs are
stripped AND a raster image covering most of the page, and the tests here are mostly
about the pages that must NOT trip it: sparse title pages and digit-heavy text pages
carry little alphabetic text too, and only the image half of the gate keeps them in.
"""

from pathlib import Path

import pymupdf

from backend.rag.parse import parse_document

PDF = "application/pdf"


def _photo_pixmap() -> pymupdf.Pixmap:
    """A rendered raster to stand in for a phone photograph of a page."""
    seed = pymupdf.open()
    page = seed.new_page(width=200, height=200)
    page.insert_text((20, 100), "the photographed page's real words")
    return page.get_pixmap(dpi=72)


def _pdf(path: Path, build: list[tuple[str | None, bool]]) -> Path:
    """A PDF with one page per (text, full_page_image) instruction."""
    document = pymupdf.open()
    for text, image in build:
        page = document.new_page()
        if image:
            # TRAP, learned the hard way: `insert_image` preserves the pixmap's aspect
            # ratio by default and silently shrinks it inside the rect, so a "full page"
            # fixture image quietly was not. `keep_proportion=False` makes it fill.
            page.insert_image(page.rect, pixmap=_photo_pixmap(), keep_proportion=False)
        if text is not None:
            page.insert_text((72, 40), text)
    document.save(path)
    document.close()
    return path


def test_a_photographed_page_with_a_junk_text_layer_is_unreadable(tmp_path: Path) -> None:
    """The roadmap's case: the page is a picture, and its "text" is a URL.

    The URL clears the plain character count, so before the gate this page ingested as
    readable and its index held a Gmail URL instead of the page. It belongs to the
    scanned flow, where recognition can actually read it.
    """
    source = _pdf(
        tmp_path / "photo.pdf",
        [("https://mail.google.com/mail/u/0/#inbox/FMfcgzGxRdyq ready", True)],
    )

    parsed = parse_document(source, PDF)

    assert parsed.pages == []
    assert parsed.pages_total == 1
    assert parsed.pages_skipped == 1


def test_a_sparse_title_page_with_no_image_stays_readable(tmp_path: Path) -> None:
    """Little alphabetic text alone must not trip the gate; the image half is required."""
    # Over 20 non-whitespace characters, under 15 alphabetic ones, and no image at all.
    title = "MATH 201 2026-08-06 10:30 Rm 4-163 v2.1.3"
    source = _pdf(tmp_path / "title.pdf", [(title, False)])

    parsed = parse_document(source, PDF)

    assert parsed.pages_skipped == 0
    assert "MATH 201" in parsed.pages[0].text


def test_a_page_of_lone_digits_with_no_image_stays_readable(tmp_path: Path) -> None:
    """A matrix-heavy page is nearly letterless, and it is still text, not a photograph."""
    matrix = "1 0 0 2\n0 1 0 3\n0 0 1 4\n2 3 4 1\n5 6 7 8\n9 8 7 6"
    source = _pdf(tmp_path / "matrices.pdf", [(matrix, False)])

    parsed = parse_document(source, PDF)

    assert parsed.pages_skipped == 0


def test_a_normal_page_keeps_its_text_even_beside_a_large_image(tmp_path: Path) -> None:
    """A figure-heavy but genuinely readable page: real words override the image half."""
    prose = "The determinant of a triangular matrix is the product of its diagonal."
    source = _pdf(tmp_path / "figure.pdf", [(prose, True)])

    parsed = parse_document(source, PDF)

    assert parsed.pages_skipped == 0
    assert "triangular" in parsed.pages[0].text


def test_a_blank_scan_is_still_dropped(tmp_path: Path) -> None:
    """The gate extends the scanned-page rule; it must not have replaced it."""
    source = _pdf(tmp_path / "scan.pdf", [(None, True), ("A" + " word" * 20, False)])

    parsed = parse_document(source, PDF)

    assert parsed.pages_total == 2
    assert parsed.pages_skipped == 1
    assert parsed.pages[0].page_number == 2
