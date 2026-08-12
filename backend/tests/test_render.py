"""Page rasterization for the solver's source pane.

Rendered against a real PDF built in the test, because the point of this module is that
PyMuPDF produces a readable image and the cache does not lie about which file it came
from. A fake would test neither.
"""

import math
import struct
import threading
import zlib
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


def _sized_pdf(path: Path, width_pt: float, height_pt: float) -> Path:
    """A one-page PDF whose page box is exactly `width_pt` x `height_pt` points.

    The point of these fixtures is geometry: a compact file can declare an enormous page,
    which is what a raster bound has to defend against.
    """
    document = pymupdf.open()
    document.new_page(width=width_pt, height=height_pt)
    document.save(path)
    document.close()
    return path


@pytest.mark.parametrize(
    ("width_pt", "height_pt"),
    [(612, 792), (595, 842)],  # US Letter and A4, the ordinary case at the highest dpi.
)
def test_normal_pages_render_at_recognition_dpi(
    tmp_path: Path, width_pt: float, height_pt: float
) -> None:
    source = _sized_pdf(tmp_path / "normal.pdf", width_pt, height_pt)

    rendered = render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    assert rendered.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_an_extreme_mediabox_is_refused_and_leaves_no_cache(tmp_path: Path) -> None:
    """The PDF format permits a 14400pt (200in) page, which at 300 dpi is ~3.6 billion px.

    That is a multi-gigabyte native allocation from a tiny file: it spikes memory or kills
    the process before Python can react, and the upload-byte limit cannot see it because the
    geometry, not the file, is large. It is refused before `get_pixmap`.
    """
    source = _sized_pdf(tmp_path / "poster.pdf", 14400, 14400)

    with pytest.raises(LyraError) as caught:
        render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    assert caught.value.message == render.TOO_LARGE_TO_RENDER
    # The message names no path, because it reaches the browser like every other render
    # error here.
    assert str(tmp_path) not in caught.value.message
    # Refused before any write, so nothing is left to serve or to poison the next request:
    # the cache directory is never even created for this document.
    assert not render.page_path(1, 1, render.RECOGNITION_DPI).exists()
    assert not render.pages_dir(1).exists()


def test_a_needle_thin_page_is_refused_on_its_long_side(tmp_path: Path) -> None:
    """A page whose area is modest but whose one dimension is enormous.

    200 x 20000 pt is under the pixel-area ceiling at 300 dpi but ~83000 px tall, so it is
    the per-side cap, not the area cap, that has to catch it.
    """
    source = _sized_pdf(tmp_path / "strip.pdf", 200, 20000)

    with pytest.raises(LyraError) as caught:
        render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    assert caught.value.message == render.TOO_LARGE_TO_RENDER


def test_a_page_far_larger_than_a_poster_is_refused_by_area(tmp_path: Path) -> None:
    """5000 x 5000 pt is under the per-side cap at 300 dpi but ~434 megapixels.

    So this is the area ceiling doing the work that the dimension cap does not.
    """
    source = _sized_pdf(tmp_path / "huge.pdf", 5000, 5000)

    with pytest.raises(LyraError) as caught:
        render.render_page(1, source, "application/pdf", 1, render.RECOGNITION_DPI)

    assert caught.value.message == render.TOO_LARGE_TO_RENDER


def test_a_figure_crop_is_bounded_by_the_crop_not_the_whole_page(tmp_path: Path) -> None:
    """A small crop of a pathological page renders; a full crop of it is refused.

    The clipped pixmap allocates only the crop, so the bound is on the clip. A tiny corner
    of a 5000pt page is a cheap render, while the whole page would be far past the envelope,
    which is exactly the distinction the crop-level check has to make.
    """
    source = _sized_pdf(tmp_path / "huge.pdf", 5000, 5000)

    corner = render.render_figure(1, source, "application/pdf", 1, 1, (0.0, 0.0, 0.1, 0.1))
    assert corner.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    with pytest.raises(LyraError) as caught:
        render.render_figure(1, source, "application/pdf", 1, 2, (0.0, 0.0, 1.0, 1.0))
    assert caught.value.message == render.TOO_LARGE_TO_RENDER


def _image_of_decoded_size(path: Path, width_px: int, height_px: int, *, gray: int = 210) -> Path:
    """A valid grayscale PNG of exactly `width_px` x `height_px`, written a row at a time.

    The builder never materializes the whole bitmap, and a solid image compresses to almost
    nothing, so the file on disk stays tiny while PyMuPDF reads the IHDR and reports a page
    rectangle of the full decoded size. That is the image analogue of a compact PDF
    declaring an enormous MediaBox, and it is precisely how an image-backed upload reaches
    the raster bound: the file is small, the geometry is not. Building a real bitmap of
    these dimensions would consume the memory the bound exists to protect, which is what the
    ticket warns against.
    """

    def _chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + body + crc

    row = b"\x00" + bytes([gray]) * width_px  # a filter byte, then one 8-bit-gray scanline
    compressor = zlib.compressobj(6)
    with path.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", width_px, height_px, 8, 0, 0, 0, 0)))
        buffer = b""
        for _ in range(height_px):
            buffer += compressor.compress(row)
            if len(buffer) > (1 << 20):
                handle.write(_chunk(b"IDAT", buffer))
                buffer = b""
        buffer += compressor.flush()
        if buffer:
            handle.write(_chunk(b"IDAT", buffer))
        handle.write(_chunk(b"IEND", b""))
    return path


def test_a_pathological_image_document_is_refused_before_get_pixmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image-backed upload cannot bypass the raster bound merely by not being a PDF.

    PyMuPDF opens a standalone image as a one-page document whose page rectangle is the
    decoded pixel size (scaled to points at 96 dpi), so a compact file that decodes to an
    enormous frame is the image analogue of an extreme MediaBox. This fixture is 60000 x 8
    px - kilobytes on disk, never a real bitmap here - and its 60000 px side is past the
    envelope at every dpi Lyra renders. The bound must refuse it before `get_pixmap` is
    asked to allocate anything, which is the security property the whole change exists for.
    """
    source = _image_of_decoded_size(tmp_path / "wall.png", 60000, 8)

    # Proof rather than inference: if the bound ever let this through, `get_pixmap` is the
    # next thing render would call, so make that call fail loudly instead of allocating.
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("get_pixmap was reached for a pathological image")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", forbidden)

    with pytest.raises(LyraError) as caught:
        render.render_page(1, source, "image/png", 1, render.RECOGNITION_DPI)

    assert caught.value.message == render.TOO_LARGE_TO_RENDER
    # The refusal names no path and leaves nothing behind, exactly like the PDF case.
    assert str(tmp_path) not in caught.value.message
    assert not render.pages_dir(1).exists()


def test_an_ordinary_jpeg_page_still_renders(tmp_path: Path) -> None:
    """The common image upload - a photographed page - is unaffected by the bound.

    A JPEG input renders through the same path as a PDF page and lands as a PNG, so the
    ordinary case keeps working while the pathological one above is refused.
    """
    seed = _pdf(tmp_path / "seed.pdf", pages=1)
    photo = tmp_path / "worksheet.jpg"
    with pymupdf.open(seed) as document:
        document[0].get_pixmap(dpi=110).save(photo, output="jpg")

    rendered = render.render_page(1, photo, "image/jpeg", 1)

    assert rendered.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_raster_bound_fails_closed_on_degenerate_geometry() -> None:
    """The helper defends against more than large positive dimensions.

    `page.rect` is always a normalized, finite rectangle, but a figure clip is built from
    model-supplied bbox fractions, and an inverted or empty bbox produces a zero-area or
    (before PyMuPDF normalizes it) negative rectangle. None of these describe a page to
    render, so each must read as out of bounds rather than as a small, safe allocation.
    """
    within = render._raster_within_bounds
    assert within(pymupdf.Rect(0, 0, 612, 792), render.RECOGNITION_DPI) is True

    # Zero-area and inverted rectangles: unrenderable, not "small".
    assert within(pymupdf.Rect(0, 0, 0, 500), 300) is False
    assert within(pymupdf.Rect(0, 0, 500, 0), 300) is False
    assert within(pymupdf.Rect(100, 100, 40, 200), 300) is False
    # PyMuPDF's infinite rectangle: a non-finite side must fail closed, not multiply out to
    # nan and slip past a comparison.
    assert within(pymupdf.Rect(-math.inf, -math.inf, math.inf, math.inf), 300) is False
    # A nonsensical dpi cannot resolve to a zero or negative allocation that reads as safe.
    assert within(pymupdf.Rect(0, 0, 612, 792), 0) is False
    assert within(pymupdf.Rect(0, 0, 612, 792), -300) is False


def test_the_raster_bound_is_exact_at_its_edges() -> None:
    """Boundary cases immediately below and above each ceiling.

    Checked at 72 dpi, where one point renders to one pixel, so the rectangle's point
    dimensions are its pixel dimensions and the arithmetic is legible.
    """
    within = render._raster_within_bounds
    # Area ceiling: 175 megapixels. 13000^2 = 169M is inside, 13300^2 = 176.9M is not, and
    # both sides stay under the per-side cap so it is the area check being exercised.
    assert within(pymupdf.Rect(0, 0, 13000, 13000), 72) is True
    assert within(pymupdf.Rect(0, 0, 13300, 13300), 72) is False
    # Per-side ceiling: 30000 px. A side exactly at the cap is allowed; one past it is not,
    # even though the area here is trivial.
    assert within(pymupdf.Rect(0, 0, render.MAX_RASTER_DIMENSION, 10), 72) is True
    assert within(pymupdf.Rect(0, 0, render.MAX_RASTER_DIMENSION + 1, 10), 72) is False


# The representative document classes Lyra validates against, recorded so the envelope can
# be checked against real course material rather than paper sizes alone. PDFs carry page
# points; images carry decoded pixels, because PyMuPDF renders an image from a page
# rectangle of px*0.75pt and the recognition path then scales that by 300/72 - a 3.125x
# upscale a PDF of the same page never takes. docs/raster-envelope.md is the prose table
# with the per-dpi pixel counts and margins; this test is what keeps that table honest.
_CORPUS_WITHIN = [
    ("US Letter (PDF)", "pdf", 612, 792),
    ("A4 (PDF)", "pdf", 595, 842),
    ("A3 (PDF)", "pdf", 842, 1191),
    ("Tabloid/Ledger (PDF)", "pdf", 792, 1224),
    ("16:9 lecture slide (PDF)", "pdf", 960, 540),
    ("A1 poster (PDF)", "pdf", 1684, 2384),
    ("A0 poster (PDF)", "pdf", 2384, 3370),
    ("12MP phone photo (image)", "image", 4032, 3024),
    ("16MP phone photo (image)", "image", 5312, 2988),
    ("300-dpi Letter scan (image)", "image", 2550, 3300),
    ("1080p screenshot (image)", "image", 1920, 1080),
]

# Legitimately unusual or outright pathological material that must be refused. A 24MP photo
# is the first ordinary-looking input past the ceiling, and the rest are the geometry a
# hostile or broken file describes.
_CORPUS_REFUSED = [
    ("24MP photo (image)", "image", 6000, 4000),
    ("5000pt page (PDF)", "pdf", 5000, 5000),
    ("Extreme MediaBox (PDF)", "pdf", 14400, 14400),
]


def _page_rect(tmp_path: Path, kind: str, width: int, height: int) -> pymupdf.Rect:
    """The page rectangle Lyra would render for one corpus entry, through the real open."""
    if kind == "pdf":
        source = _sized_pdf(tmp_path / "corpus.pdf", width, height)
    else:
        source = _image_of_decoded_size(tmp_path / "corpus.png", width, height)
    with pymupdf.open(source) as document:
        return document[0].rect


@pytest.mark.parametrize(("label", "kind", "width", "height"), _CORPUS_WITHIN)
def test_the_envelope_accepts_the_representative_corpus(
    tmp_path: Path, label: str, kind: str, width: int, height: int
) -> None:
    """Every representative document renders at reading and recognition dpi.

    Recognition (300 dpi) is the binding case: it is the largest raster of the two full-page
    resolutions and the one an image's 3.125x upscale applies to.
    """
    rect = _page_rect(tmp_path, kind, width, height)

    assert render._raster_within_bounds(rect, render.RENDER_DPI), label
    assert render._raster_within_bounds(rect, render.RECOGNITION_DPI), label


@pytest.mark.parametrize(("label", "kind", "width", "height"), _CORPUS_REFUSED)
def test_the_envelope_refuses_pathological_material(
    tmp_path: Path, label: str, kind: str, width: int, height: int
) -> None:
    rect = _page_rect(tmp_path, kind, width, height)

    assert not render._raster_within_bounds(rect, render.RECOGNITION_DPI), label


def test_the_common_phone_photo_has_headroom_and_larger_ones_sit_near_the_edge() -> None:
    """The measured margins the doc claims, around the design point that sets the ceiling.

    Images are the binding case because recognition upscales them 3.125x (px -> px*300/96),
    so the envelope is really sized around the standard 12MP phone photo. If a future change
    narrowed that headroom, or quietly started admitting 24MP photos, this fails.
    """
    scale = render.RECOGNITION_DPI / 96.0  # an image's px -> rendered px at recognition dpi

    def raster(width_px: int, height_px: int) -> float:
        return width_px * height_px * scale * scale

    twelve_mp = raster(4032, 3024)  # ~119M px: the standard phone photo, and the design point
    sixteen_mp = raster(5312, 2988)  # ~155M px: still admitted, but near the edge
    twenty_four_mp = raster(6000, 4000)  # ~234M px: past the ceiling

    # The design point clears the ceiling with room - about a third of the budget spare.
    assert twelve_mp < render.MAX_RASTER_PIXELS
    assert (render.MAX_RASTER_PIXELS - twelve_mp) / render.MAX_RASTER_PIXELS > 0.25
    # A 16MP phone still renders, but it is the edge of what the envelope admits.
    assert sixteen_mp < render.MAX_RASTER_PIXELS
    assert (render.MAX_RASTER_PIXELS - sixteen_mp) / render.MAX_RASTER_PIXELS < 0.2
    # A 24MP photo is past it: that is where the envelope draws the line for images.
    assert twenty_four_mp > render.MAX_RASTER_PIXELS
