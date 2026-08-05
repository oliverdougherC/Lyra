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


def test_a_label_with_no_number_is_not_searched_for(tmp_path: Path) -> None:
    # Searching for a bare word would hit the first paragraph that happens to use it.
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 1, "Convolution") is None


def test_a_missing_file_costs_the_position_and_nothing_else(tmp_path: Path) -> None:
    # This drives a click target on a page image. A source that has been deleted must not
    # be able to raise into a solve.
    assert find_label(tmp_path / "gone.pdf", 1, "Problem 1") is None


def test_a_page_past_the_end_is_not_a_page(tmp_path: Path) -> None:
    sheet = _sheet(tmp_path / "homework.pdf")

    assert find_label(sheet, 40, "Problem 1 (Time Shift)") is None
