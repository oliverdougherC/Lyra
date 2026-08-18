"""Finding the diagrams in a document, and refusing to guess who they belong to.

Built against real PDFs rather than fakes: what is under test is whether PyMuPDF's image
list, filtered, corresponds to what a reader would call a figure, and a stub would only
test the filter against itself.
"""

import sqlite3
from pathlib import Path

import pymupdf
import pytest

from backend.core import figures as store
from backend.core.errors import LyraError
from backend.rag import figures, render


def _pdf(
    path: Path,
    blocks: list[tuple[float, float, float, float]],
    caption: str = "",
    markers: list[tuple[str, float]] | None = None,
) -> Path:
    """A one-page PDF carrying an image at each given rect, and an optional caption.

    `markers` writes the sheet's own problem numbers at given baselines, which is what
    `locate` reads to give each problem a position. A test about pairing needs them; a test
    about extraction does not, and leaving them off is how it says so.
    """
    document = pymupdf.open()
    # US Letter, so the rects below are the ones the reference corpus actually uses.
    page = document.new_page(width=612, height=792)
    for index, (x0, y0, x1, y1) in enumerate(blocks):
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
        pixmap.set_rect(pixmap.irect, (30 * index + 40, 90, 140))
        # `keep_proportion=False`, or PyMuPDF letterboxes the pixmap inside the rect and
        # the placed image is not the shape the test is about.
        page.insert_image(pymupdf.Rect(x0, y0, x1, y1), pixmap=pixmap, keep_proportion=False)
    if caption:
        page.insert_text((72, 300), caption, fontsize=9)
    for text, baseline in markers or []:
        page.insert_text((72, baseline), text, fontsize=11)
    document.save(path)
    document.close()
    return path


def test_a_block_diagram_wider_than_it_is_tall_is_still_a_figure(tmp_path: Path) -> None:
    """The acceptance case's shape, and the one a naive size filter drops.

    The three diagrams on the reference homework are 252 by 21 points. A minimum-height
    rule of any useful size discards all of them, so the floor is area with a thin-side
    guard rather than a box.
    """
    source = _pdf(tmp_path / "hw.pdf", [(181, 179, 433, 200)])

    found = figures.extract_figures(source, "application/pdf")

    assert len(found) == 1
    assert found[0].page_number == 1
    assert found[0].label is None


def test_an_image_covering_the_page_is_the_page_rather_than_a_figure(tmp_path: Path) -> None:
    """A scanned page is one bitmap over the whole sheet.

    Filing it as a figure would give every page of a scanned document a "diagram" that is
    just the page again, which is how the scanned handout in the reference course behaves.
    """
    source = _pdf(tmp_path / "scan.pdf", [(0, 0, 612, 792)])

    assert figures.extract_figures(source, "application/pdf") == []


def test_something_too_small_to_be_a_diagram_is_left_alone(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "bullets.pdf", [(72, 72, 84, 84)])

    assert figures.extract_figures(source, "application/pdf") == []


def test_a_caption_beneath_a_figure_names_it(tmp_path: Path) -> None:
    # Every caption in the reference course starts within a point of the image it names.
    source = _pdf(
        tmp_path / "notes.pdf", [(100, 200, 400, 290)], caption="Figure 3: Low pass filter"
    )

    found = figures.extract_figures(source, "application/pdf")

    assert (found[0].label, found[0].caption) == ("Figure 3", "Low pass filter")


def test_a_numbered_list_marker_under_a_diagram_is_not_a_caption(tmp_path: Path) -> None:
    """The fault this refuses to have.

    On the acceptance homework the list markers sit below their diagrams, well within any
    distance a caption rule would allow. Only the caption pattern keeps "2." from being
    read as this figure's name, and from there as the problem it belongs to.
    """
    source = _pdf(tmp_path / "hw.pdf", [(100, 200, 400, 290)], caption="2.")

    found = figures.extract_figures(source, "application/pdf")

    assert found[0].label is None
    assert found[0].caption is None


def test_figures_come_back_in_reading_order(tmp_path: Path) -> None:
    source = _pdf(
        tmp_path / "hw.pdf",
        [(181, 331, 433, 382), (181, 179, 433, 200), (181, 239, 433, 290)],
    )

    found = figures.extract_figures(source, "application/pdf")

    assert [f.index for f in found] == [1, 2, 3]
    assert [round(f.bbox[1], 2) for f in found] == sorted(round(f.bbox[1], 2) for f in found)


def test_geometry_is_fractions_of_the_page_rather_than_points(tmp_path: Path) -> None:
    """The convention `locate.py` and `artifact_provenance.bbox` already set.

    A page renders as an image at whatever width the pane has, so points would have to be
    converted by every reader.
    """
    source = _pdf(tmp_path / "hw.pdf", [(153, 198, 459, 396)])

    bbox = figures.extract_figures(source, "application/pdf")[0].bbox

    assert [round(v, 2) for v in bbox] == [0.25, 0.25, 0.75, 0.5]


def test_a_text_document_has_no_figures_rather_than_an_error(tmp_path: Path) -> None:
    assert figures.extract_figures(tmp_path / "notes.md", "text/markdown") == []


def test_a_damaged_file_reports_without_naming_a_path(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")

    with pytest.raises(LyraError) as caught:
        figures.extract_figures(broken, "application/pdf")

    assert str(tmp_path) not in caught.value.message


def test_an_uncaptioned_figure_is_named_for_where_it_was_found() -> None:
    """Not "Figure 2". That would invent a number the document does not use, and that its
    own text may already mean something else by."""
    assert store.figure_name(None, 4, 2) == "Page 4, figure 2"
    assert store.figure_name("Figure 3", 4, 2) == "Figure 3"


def _document(db: sqlite3.Connection, class_id: int, path: Path) -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, 'application/pdf', 10, 'ready')",
        (class_id, path.name, str(path)),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def test_storing_replaces_rather_than_accumulates(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """A re-ingest reads the same file again, and must not leave two copies of a diagram."""
    source = _pdf(tmp_path / "hw.pdf", [(181, 179, 433, 200), (181, 239, 433, 290)])
    document_id = _document(db, class_id, source)
    found = figures.extract_figures(source, "application/pdf")

    store.store_figures(db, document_id, found)
    store.store_figures(db, document_id, found)
    db.commit()

    assert len(store.list_figures(db, document_id)) == 2


def test_figures_are_looked_up_by_the_pages_a_problem_occupies(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """How an uncaptioned figure finally gets an owner, using what segmentation knows."""
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
        pixmap.set_rect(pixmap.irect, (40, 90, 140))
        page.insert_image(pymupdf.Rect(100, 200, 400, 290), pixmap=pixmap, keep_proportion=False)
    source = tmp_path / "two.pdf"
    document.save(source)
    document.close()

    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()

    assert [f["page_number"] for f in store.list_figures(db, document_id, [2])] == [2]
    # A problem whose page is unknown gets no figures rather than every figure in the file.
    assert store.list_figures(db, document_id, []) == []


def test_a_figure_renders_as_a_crop_of_its_page(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """Cropped from the composed page rather than pulled out with `extract_image`.

    The stored bitmap is the figure's own pixels at its own scale, which arrives enormous,
    in pieces, or transparent depending on how it was made. The page shows what the page
    shows.
    """
    source = _pdf(tmp_path / "hw.pdf", [(100, 200, 400, 290)])
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    figure = store.list_figures(db, document_id)[0]
    bbox = figure["bbox"]

    created_at = str(
        db.execute("select created_at from documents where id = ?", (document_id,)).fetchone()[
            "created_at"
        ]
    )
    path = render.render_figure(
        document_id,
        source,
        "application/pdf",
        1,
        int(figure["id"]),
        tuple(bbox),
        created_at=created_at,
    )

    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with pymupdf.open(path) as rendered:
        page = rendered[0]
        # The crop, not the page: the figure's own 300-by-90 shape rather than the
        # 612-by-792 sheet it was cut from.
        assert 3.0 < page.rect.width / page.rect.height < 3.7
    # Discarding a document's rendered pages takes its figures too.
    render.discard_pages(document_id)
    assert not render.figure_path(document_id, int(figure["id"])).exists()


def _segmented(label: str, statement: str, document_id: int, page: int):
    """One problem as segmentation hands it to the writer."""
    from backend.core.segmentation import SegmentedProblem

    return SegmentedProblem(
        label=label,
        number=label.split()[-1],
        statement=statement,
        document_id=document_id,
        page_number=page,
    )


def _artifact(db: sqlite3.Connection, class_id: int) -> int:
    cursor = db.execute(
        "insert into artifacts (class_id, kind, title, state) "
        "values (?, 'solution_set', 'Homework 3', 'awaiting_review')",
        (class_id,),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _figure_parts(db: sqlite3.Connection, artifact_id: int) -> dict[str, list[str]]:
    """Which figures ended up under which problem, by label."""
    rows = db.execute(
        "select p.label as problem, f.label as figure from artifact_parts f "
        "join artifact_parts p on p.id = f.parent_part_id "
        "where f.artifact_id = ? and f.kind = 'figure' order by f.id",
        (artifact_id,),
    ).fetchall()
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(str(row["problem"]), []).append(str(row["figure"]))
    return found


def test_a_problem_alone_on_its_page_takes_the_figures_on_it(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """Not a guess: one problem and the page's figures is an unambiguous pairing."""
    from backend.core import solver

    source = _pdf(tmp_path / "lab.pdf", [(100, 200, 400, 290)])
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db, artifact_id, [_segmented("Problem 1", "Find the transfer function.", document_id, 1)]
    )

    assert _figure_parts(db, artifact_id) == {"Problem 1": ["Page 1, figure 1"]}


def test_a_crowded_page_with_no_captions_files_nothing_under_a_problem(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """The fault the first version had, kept out by a test.

    Attaching every figure on a page to every problem on it gave the acceptance homework
    twenty-one attachments of which twelve were wrong: four Fourier-series problems each
    received three block diagrams belonging to other questions. Showing a student a diagram
    that answers a different question is worse than showing them none, because the page
    image beside the solution shows it anyway.
    """
    from backend.core import solver

    source = _pdf(
        tmp_path / "hw.pdf", [(181, 179, 433, 200), (181, 239, 433, 290), (181, 331, 433, 382)]
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [
            _segmented("Problem 1", "Determine h(t).", document_id, 1),
            _segmented("Problem 2", "Determine z(t).", document_id, 1),
        ],
    )

    assert _figure_parts(db, artifact_id) == {}


def test_a_page_that_reads_as_a_list_pairs_each_diagram_with_its_own_problem(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """The acceptance layout, measured off the real sheet.

    On homework 3 three block diagrams sit at the top of a page of seven questions, each
    with its list marker *underneath* it, and then four more questions with no diagrams at
    all. Nothing about distance decides this: what decides it is that the page alternates
    diagram, marker, diagram, marker, which is the shape of a list rather than a guess. The
    four questions left over once the diagrams run out get nothing, which is correct.
    """
    from backend.core import solver

    source = _pdf(
        tmp_path / "hw.pdf",
        [(181, 179, 433, 200), (181, 239, 433, 290), (181, 331, 433, 382)],
        markers=[("1.", 215), ("2.", 305), ("3.", 397), ("4.", 460), ("5.", 500)],
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [_segmented(f"Problem {number}", f"{number}.", document_id, 1) for number in range(1, 6)],
    )

    assert _figure_parts(db, artifact_id) == {
        "Problem 1": ["Page 1, figure 1"],
        "Problem 2": ["Page 1, figure 2"],
        "Problem 3": ["Page 1, figure 3"],
    }


def test_a_sheet_that_numbers_above_its_diagrams_pairs_the_other_way(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """The other common layout, and the reason the direction is read rather than assumed.

    Here every marker sits above the diagram it introduces. Pairing by "nearest preceding
    marker" would be right on this sheet and wrong on the one above, which is exactly why
    neither is hard-coded: the page says which way round it is by which kind comes first.
    """
    from backend.core import solver

    source = _pdf(
        tmp_path / "notes.pdf",
        [(181, 200, 433, 260), (181, 340, 433, 400)],
        markers=[("1.", 190), ("2.", 330)],
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [_segmented(f"Problem {number}", f"{number}.", document_id, 1) for number in (1, 2)],
    )

    assert _figure_parts(db, artifact_id) == {
        "Problem 1": ["Page 1, figure 1"],
        "Problem 2": ["Page 1, figure 2"],
    }


def test_one_diagram_among_several_problems_is_still_nobody_s(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """The ambiguous case, which alternation must not be allowed to swallow.

    A single figure has a marker beside it on both sides, because on a page of questions
    everything has a marker beside it. There is no repetition to read, so there is no
    layout, and a lone diagram between two questions belongs to neither until something
    says otherwise.
    """
    from backend.core import solver

    source = _pdf(
        tmp_path / "hw.pdf",
        [(181, 239, 433, 290)],
        markers=[("1.", 190), ("2.", 320), ("3.", 400)],
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [_segmented(f"Problem {number}", f"{number}.", document_id, 1) for number in (1, 2, 3)],
    )

    assert _figure_parts(db, artifact_id) == {}


def test_two_diagrams_with_nothing_between_them_pair_with_nothing(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """A run that is not alternating is not a list, and half a pairing is not on offer."""
    from backend.core import solver

    source = _pdf(
        tmp_path / "hw.pdf",
        [(181, 200, 433, 250), (181, 255, 433, 305), (181, 400, 433, 450)],
        markers=[("1.", 330), ("2.", 470)],
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [_segmented(f"Problem {number}", f"{number}.", document_id, 1) for number in (1, 2)],
    )

    assert _figure_parts(db, artifact_id) == {}


def test_a_problem_whose_marker_is_not_on_the_page_stops_the_pairing(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """An unplaced problem cannot take part in an ordering, so the page gets none.

    Refusing the whole page rather than pairing around the gap: a problem missing from the
    sequence shifts every diagram after it onto the wrong question, which is worse than
    attaching nothing.
    """
    from backend.core import solver

    source = _pdf(
        tmp_path / "hw.pdf",
        [(181, 179, 433, 200), (181, 239, 433, 290)],
        markers=[("1.", 215), ("2.", 305)],
    )
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [
            _segmented("Problem 1", "1.", document_id, 1),
            _segmented("Problem 2", "2.", document_id, 1),
            # Segmentation found a third problem the sheet does not number anywhere.
            _segmented("Problem 9", "9.", document_id, 1),
        ],
    )

    assert _figure_parts(db, artifact_id) == {}


def test_a_problem_with_an_unplaced_neighbour_is_not_alone_on_its_page(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """The single-problem shortcut may only fire when the census behind it is complete.

    "The only problem on its page" is computed over the problems whose pages resolved. A
    page really holding problems 3 and 4, where only 4's page survived segmentation,
    would otherwise compute 4 as alone and hand it every figure on the page - including
    3's. With any problem of the document unplaced, the shortcut stands down and the
    naming and alternation rules, which refuse rather than guess, are all that remain.
    """
    from backend.core import solver
    from backend.core.segmentation import SegmentedProblem

    source = _pdf(tmp_path / "hw.pdf", [(100, 200, 400, 290)])
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [
            SegmentedProblem(
                label="Problem 3",
                number="3",
                statement="Determine h(t).",
                document_id=document_id,
                page_number=None,
            ),
            _segmented("Problem 4", "Determine z(t).", document_id, 1),
        ],
    )

    assert _figure_parts(db, artifact_id) == {}


def test_a_figure_named_inside_a_sub_part_is_attached(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """ "See Figure 2" lives inside part (b) as often as in the stem.

    The naming rule used to read only the problem's own statement, so a reference printed
    in a sub-part did not fire it - and on a crowded page that meant no attachment at all,
    or worse, left the field to a shortcut that guessed.
    """
    from backend.core import solver
    from backend.core.segmentation import SegmentedPart, SegmentedProblem

    source = _pdf(tmp_path / "set.pdf", [(100, 200, 400, 290)], caption="Figure 3: Low pass filter")
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [
            SegmentedProblem(
                label="Problem 1",
                number="1",
                statement="Consider the filter below.",
                document_id=document_id,
                page_number=1,
                parts=(
                    SegmentedPart("(a)", "Sketch its impulse response."),
                    SegmentedPart("(b)", "Find the cutoff of the system in figure 3."),
                ),
            ),
            _segmented("Problem 2", "State the Nyquist rate.", document_id, 1),
        ],
    )

    assert _figure_parts(db, artifact_id) == {"Problem 1": ["Figure 3"]}


def test_a_problem_that_names_a_figure_gets_that_one(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """Exact rather than geometric. "The system in Figure 3" is a reference, not a guess,
    and it works on a crowded page where nothing else does."""
    from backend.core import solver

    source = _pdf(tmp_path / "set.pdf", [(100, 200, 400, 290)], caption="Figure 3: Low pass filter")
    document_id = _document(db, class_id, source)
    store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    artifact_id = _artifact(db, class_id)

    solver.write_problems(
        db,
        artifact_id,
        [
            _segmented("Problem 1", "Find the cutoff of the filter in figure 3.", document_id, 1),
            _segmented("Problem 2", "State the Nyquist rate.", document_id, 1),
        ],
    )

    # Only the problem that named it, and case-insensitively, because a caption reads
    # `Figure 3` and a sentence reads `figure 3`.
    assert _figure_parts(db, artifact_id) == {"Problem 1": ["Figure 3"]}
