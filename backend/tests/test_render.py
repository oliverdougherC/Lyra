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


def test_discarding_pages_survives_something_else_in_the_cache(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "hw4.pdf")
    render.render_page(1, source, "application/pdf", 1)
    # A page being rendered right now, or a partial file left by an interrupted write.
    # Deleting a document while its source pane is loading must not fail the delete, which
    # the caller has already committed by the time this runs.
    (render.pages_dir(1) / "2.png.partial").write_bytes(b"half a page")

    render.discard_pages(1)

    assert not (render.pages_dir(1) / "1.png").exists()


def test_a_write_that_dies_partway_leaves_nothing_the_cache_will_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _pdf(tmp_path / "hw4.pdf")
    whole = pymupdf.Pixmap.save
    failed_once = False

    def die_partway(self: pymupdf.Pixmap, filename: object, **kwargs: object) -> None:
        # Only the first write dies, so the re-render below runs for real. Undoing the
        # patch instead would undo the autouse fixture holding `data_dir` inside tmp_path
        # with it, and point every path in this test at the developer's own data directory.
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            Path(str(filename)).write_bytes(b"\x89PNG\r\n\x1a\n truncated")
            raise OSError("no space left on device")
        whole(self, filename, **kwargs)

    monkeypatch.setattr(pymupdf.Pixmap, "save", die_partway)
    with pytest.raises(LyraError):
        render.render_page(1, source, "application/pdf", 1)

    # The cache is trusted on the strength of the file existing, so a half-written page at
    # the final name would be served as that page for good: nothing re-renders a path that
    # is already there, and only re-ingesting the document would clear it.
    assert not render.page_path(1, 1).exists()
    assert list(render.pages_dir(1).glob("*.partial")) == []
    redone = render.render_page(1, source, "application/pdf", 1)
    assert redone.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
