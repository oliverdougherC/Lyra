"""Page rasterization for the solver's source pane.

Rendered against a real PDF built in the test, because the point of this module is that
PyMuPDF produces a readable image and the cache does not lie about which file it came
from. A fake would test neither.
"""

import threading
from pathlib import Path

import pymupdf
import pytest

from backend.core.errors import LyraError, NotFoundError
from backend.rag import parse, render


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


def test_two_threads_rendering_the_same_page_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FastAPI serves requests from a threadpool, so two renders of one page share a pid.

    The partial file was named by pid alone, so both writers used one name: the first
    request's cleanup deleted the file out from under the second's `replace`, and one
    viewer got a spurious "could not be opened" for a page that renders fine. The barrier
    holds both threads at the end of their writes, so both are provably mid-render at
    once and the collision is deterministic rather than a race the test might miss.
    """
    source = _pdf(tmp_path / "hw4.pdf")
    whole = pymupdf.Pixmap.save
    barrier = threading.Barrier(2, timeout=10)

    def synchronized_save(self: pymupdf.Pixmap, filename: object, **kwargs: object) -> None:
        whole(self, filename, **kwargs)
        barrier.wait()

    monkeypatch.setattr(pymupdf.Pixmap, "save", synchronized_save)
    results: list[object] = []

    def render_one() -> None:
        try:
            results.append(render.render_page(1, source, "application/pdf", 1))
        except Exception as exc:  # noqa: BLE001 - the failure mode under test is an exception
            results.append(exc)

    threads = [threading.Thread(target=render_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [render.page_path(1, 1), render.page_path(1, 1)]
    assert render.page_path(1, 1).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert list(render.pages_dir(1).glob("*.partial")) == []


def test_reading_and_recognition_resolutions_cache_separately(tmp_path: Path) -> None:
    """Two different artifacts that must never satisfy each other's request.

    A page rendered for the source pane quietly answering a transcription request would
    degrade recognition with nothing on screen to say so, and a 300 dpi page served to the
    pane would cost several times the bytes for a picture nobody looks at that closely.
    """
    source = _pdf(tmp_path / "book.pdf")

    reading = render.render_page(1, source, "application/pdf", 1, render.RENDER_DPI)
    recognition = render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    assert reading != recognition
    assert reading.exists() and recognition.exists()
    # The higher resolution is the bigger file, which is the whole reason for asking for it.
    assert recognition.stat().st_size > reading.stat().st_size


def test_discarding_pages_clears_every_resolution(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "book.pdf")
    render.render_page(1, source, "application/pdf", 1, render.RENDER_DPI)
    render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    render.discard_pages(1)

    assert not list(render.pages_dir(1).glob("*.png"))


def test_an_uploaded_image_draws_as_its_only_page(tmp_path: Path) -> None:
    """A photographed page is a one-page document, and the source pane shows it like one.

    PyMuPDF opens a PNG or a JPG directly, so an image upload needs no separate path here
    or in the parser: it is a document whose single page happens to have no text.
    """
    source = _pdf(tmp_path / "seed.pdf", pages=1)
    image = tmp_path / "whiteboard.png"
    with pymupdf.open(source) as document:
        document[0].get_pixmap(dpi=100).save(image, output="png")

    rendered = render.render_page(1, image, "image/png", 1)

    assert rendered.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(NotFoundError):
        render.render_page(1, image, "image/png", 2)


def test_a_damaged_image_says_it_is_an_image_that_would_not_open(tmp_path: Path) -> None:
    """Not the PDF message, which offers a password as the likely cause.

    An image cannot be password protected, and sending someone to look for one they do not
    have is worse than saying nothing.
    """
    broken = tmp_path / "scan.png"
    broken.write_bytes(b"not a png")

    with pytest.raises(LyraError) as caught:
        render.render_page(1, broken, "image/png", 1)

    assert caught.value.message == parse.UNREADABLE_IMAGE_MESSAGE
