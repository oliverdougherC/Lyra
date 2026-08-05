"""Finding a problem's marker on its source page.

The contract from docs/ui-phase-2.md: the page image beside a solution is clickable, and
what makes that possible is knowing where each problem starts. Everything here is built
with PyMuPDF at test time rather than committed as a fixture, so the geometry under test
is geometry this machine produced.
"""

from pathlib import Path

import pymupdf

from backend.rag.locate import find_label


def _sheet(path: Path) -> Path:
    """A two-page problem sheet with markers at known heights."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 100), "Problem 1 (Time Shift)", fontsize=11)
    first.insert_text((72, 400), "Problem 2 (Scaling)", fontsize=11)
    second = document.new_page()
    second.insert_text((72, 120), "3. Convolution", fontsize=11)
    document.save(path)
    document.close()
    return path


def _named_sheet(path: Path) -> Path:
    """A sheet whose problems are titled rather than numbered, which many are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "Linearity and Time-Invariance", fontsize=11)
    page.insert_text((72, 300), "Properties of LTI Systems", fontsize=11)
    page.insert_text((72, 500), "Continuous-Time Graphical Convolution", fontsize=11)
    document.save(path)
    document.close()
    return path


def test_a_named_marker_is_found_where_it_sits(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "homework.pdf")

    first = find_label(sheet, 1, "Problem 1 (Time Shift)")
    second = find_label(sheet, 1, "Problem 2 (Scaling)")

    assert first is not None
    assert second is not None
    # Fractions of the page box, so the frontend can lay them over an image rendered at
    # whatever width the pane has.
    assert all(0.0 <= value <= 1.0 for value in first)
    # Problem 2 is lower down the page than problem 1, which is the whole point.
    assert second[1] > first[1]


def test_a_bare_numbered_marker_is_found(tmp_path: Path) -> None:
    # A sheet that writes `3.` rather than `Problem 3`. The label segmentation recorded
    # still leads with the number, so the search falls back to the number with its stop.
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 2, "3. Convolution") is not None


def test_a_marker_that_is_not_on_the_page_is_not_invented(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 2, "Problem 9 (Nowhere)") is None


def test_a_sheet_titled_rather_than_numbered_is_still_found(tmp_path: Path) -> None:
    """The labels here carry no digit anywhere, so a pattern needing one found nothing.

    A whole homework set came out with no position for any of its problems, so its page
    image had no bands and nothing to click, because the marker rule required a number and
    "Linearity and Time-Invariance" has none. The title is the heading on these sheets, so
    the title is what to search for.
    """
    sheet = _named_sheet(tmp_path / "named.pdf")

    first = find_label(sheet, 1, "Linearity and Time-Invariance")
    second = find_label(sheet, 1, "Properties of LTI Systems")
    third = find_label(sheet, 1, "Continuous-Time Graphical Convolution")

    assert first is not None and second is not None and third is not None
    assert first[1] < second[1] < third[1]


def test_a_label_too_short_to_be_sure_of_is_not_searched_for(tmp_path: Path) -> None:
    # Free text is only searched when it is long enough that the first hit is the heading
    # rather than a word inside a sentence.
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 2, "Conv") is None


def test_a_numbered_sheet_still_matches_on_its_marker(tmp_path: Path) -> None:
    """The title is tried after the marker, never instead of it.

    Here the sheet writes "Problem 1 (Time Shift)" and segmentation recorded a different
    gloss. The marker still matches, which is the precise short match that was already
    working and must not be traded away for the fallback.
    """
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 1, "Problem 1 (Something Else Entirely)") is not None


def test_a_missing_file_costs_the_position_and_nothing_else(tmp_path: Path) -> None:
    # This drives a click target on a page image. A source that has been deleted must not
    # be able to raise into a solve.
    assert find_label(tmp_path / "gone.pdf", 1, "Problem 1") is None


def test_a_page_past_the_end_is_not_a_page(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 40, "Problem 1 (Time Shift)") is None
