"""Storing the figures found in a document, and finding them again per page.

The extraction itself is `backend.rag.figures`, which touches no database. This is the
thin layer between it and the three things that ask: ingestion writes, the documents API
lists, and the solver looks for the figures on the pages a problem occupies.

That last one is where the association actually happens, and it is on purpose. An
uncaptioned figure is not given an owner at extraction time, because the page geometry does
not reliably say who owns it: on the acceptance document the numbered list markers sit
below their diagrams, so the obvious "nearest preceding marker" rule attaches every figure
to the problem before its own. What does know is the solver, which has already been told
which pages a problem occupies by segmentation. So the figure is matched to a problem by
the page it is on, using information the extractor never had.
"""

import json
import sqlite3

from backend.rag.figures import Figure

_INSERT_SQL = (
    "insert into document_figures (document_id, page_number, figure_index, bbox, label, caption) "
    "values (?, ?, ?, ?, ?, ?)"
)

_COLUMNS = "id, document_id, page_number, figure_index, bbox, label, caption"


def store_figures(conn: sqlite3.Connection, document_id: int, figures: list[Figure]) -> None:
    """Replace this document's figures with the ones just found. The caller commits.

    Replacing rather than appending, for the reason `delete_chunks` does: a re-ingest reads
    the same file again, and two runs must not leave two copies of every diagram.
    """
    conn.execute("delete from document_figures where document_id = ?", (document_id,))
    conn.executemany(
        _INSERT_SQL,
        [
            (
                document_id,
                figure.page_number,
                figure.index,
                json.dumps([round(value, 6) for value in figure.bbox]),
                figure.label,
                figure.caption,
            )
            for figure in figures
        ],
    )


def list_figures(
    conn: sqlite3.Connection, document_id: int, pages: list[int] | None = None
) -> list[dict[str, object]]:
    """This document's figures, in page then reading order.

    Args:
        conn: Open database connection.
        document_id: Document to read.
        pages: Restrict to these 1-based page numbers. None means every page. An empty
            list means no pages, which returns nothing rather than everything: a problem
            whose pages are unknown gets no figures rather than all of them.

    Returns:
        One dict per figure, with `bbox` decoded and a `name` written for a reader.
    """
    if pages is None:
        rows = conn.execute(
            f"select {_COLUMNS} from document_figures where document_id = ? "  # noqa: S608
            "order by page_number, figure_index",
            (document_id,),
        ).fetchall()
    elif not pages:
        return []
    else:
        placeholders = ", ".join("?" for _ in pages)
        rows = conn.execute(
            f"select {_COLUMNS} from document_figures where document_id = ? "  # noqa: S608
            f"and page_number in ({placeholders}) order by page_number, figure_index",
            (document_id, *pages),
        ).fetchall()

    return [_read(row) for row in rows]


def get_figure(conn: sqlite3.Connection, figure_id: int) -> dict[str, object] | None:
    """One figure by id, or None. Carries the source document's path, mime, and identity
    (`created_at`) for rendering, whose publication is guarded on that identity."""
    row = conn.execute(
        f"select f.{_COLUMNS.replace(', ', ', f.')}, d.stored_path, d.mime, "  # noqa: S608
        "d.created_at as document_created_at "
        "from document_figures f join documents d on d.id = f.document_id where f.id = ?",
        (figure_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        **_read(row),
        "stored_path": row["stored_path"],
        "mime": row["mime"],
        "document_created_at": row["document_created_at"],
    }


def figure_name(label: object, page_number: object, index: object) -> str:
    """What to call a figure on screen.

    Its caption's label where it has one. Otherwise the page and position it was found at,
    which is a location rather than a claim: naming an uncaptioned diagram "Figure 2" would
    invent a number the document does not use and that its own text may already mean
    something else by.
    """
    if isinstance(label, str) and label.strip():
        return label.strip()
    return f"Page {page_number}, figure {index}"


def _read(row: sqlite3.Row) -> dict[str, object]:
    """One stored row as the rest of the app wants it."""
    return {
        "id": int(row["id"]),
        "document_id": int(row["document_id"]),
        "page_number": int(row["page_number"]),
        "figure_index": int(row["figure_index"]),
        "bbox": _read_bbox(row["bbox"]),
        "label": row["label"],
        "caption": row["caption"],
        "name": figure_name(row["label"], row["page_number"], row["figure_index"]),
    }


def _read_bbox(raw: object) -> list[float]:
    """The stored rectangle, or the whole page when it cannot be read.

    A malformed value falls back to the full page rather than raising. The figure then
    renders as its page, which is wrong but readable, and no solution fails to load over a
    crop.
    """
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and len(parsed) == 4:
            return [float(value) for value in parsed]
    return [0.0, 0.0, 1.0, 1.0]
