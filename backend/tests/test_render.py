"""Page rasterization for the solver's source pane.

Rendered against a real PDF built in the test, because the point of this module is that
PyMuPDF produces a readable image and the cache does not lie about which file it came
from. A fake would test neither.
"""

from pathlib import Path

import pymupdf
import pytest

from backend.core.errors import LyraError, NotFoundError
from backend.rag import render


def _pdf(path: Path, pages: int = 3) -> Path:
    document = pymupdf.open()
    for number in range(1, pages + 1):
        page = document.new_page()
        page.insert_text((72, 100), f"Page {number}")
    document.save(path)
    document.close()
    return path


def test_a_page_renders_to_a_png_and_is_cached(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "hw4.pdf")

    first = render.render_page(1, source, "application/pdf", 2)
    stamped = first.stat().st_mtime_ns
    second = render.render_page(1, source, "application/pdf", 2)

    assert first.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # The second call is served from the cache: rendering is not free and a rendered page
    # of a stored file never changes.
    assert second == first
    assert second.stat().st_mtime_ns == stamped


def test_a_page_past_the_end_is_a_404_rather_than_a_blank_image(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "hw4.pdf")

    with pytest.raises(NotFoundError):
        render.render_page(1, source, "application/pdf", 9)


def test_a_text_source_has_no_page_to_draw(tmp_path: Path) -> None:
    # TXT and MD render as their extracted text instead, which is the same anchor with a
    # different surface.
    with pytest.raises(LyraError):
        render.render_page(1, tmp_path / "notes.md", "text/markdown", 1)


def test_a_damaged_file_reports_without_naming_a_path(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")

    with pytest.raises(LyraError) as caught:
        render.render_page(1, broken, "application/pdf", 1)

    # PyMuPDF puts the absolute path in every message it raises, and these reach the
    # browser.
    assert str(tmp_path) not in caught.value.message


def test_discarding_pages_removes_the_cache(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "hw4.pdf")
    render.render_page(1, source, "application/pdf", 1)

    render.discard_pages(1)

    # A stale image is worse than a missing one: it shows a page from a file that is no
    # longer there.
    assert not render.pages_dir(1).exists()
    assert render.render_page(1, source, "application/pdf", 1).exists()
