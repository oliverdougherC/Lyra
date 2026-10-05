"""Service boundary regressions using synthetic responses and disposable storage."""

import math
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from backend.core.errors import UpstreamError
from backend.llm import client
from backend.rag import embed, figures, rerank
from backend.storage import database


@pytest.mark.parametrize(
    "value", [None, "0.5", True, math.nan, math.inf, -math.inf, 10**1000, 1e300]
)
def test_embedding_rejects_invalid_components(value: object) -> None:
    vector = [0.0] * embed.EMBEDDING_DIM
    vector[0] = value
    with pytest.raises(UpstreamError):
        embed._parse_vectors({"data": [{"index": 0, "embedding": vector}]}, 1)


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf, 10**1000])
def test_reranker_discards_invalid_scores(value: object) -> None:
    assert rerank._scores({"results": [{"index": 0, "relevance_score": value}]}, 1) is None


@pytest.mark.parametrize("boundary", ["embedding", "rerank"])
def test_boolean_indices_are_not_valid_positions(boundary: str) -> None:
    if boundary == "embedding":
        with pytest.raises(UpstreamError):
            embed._parse_vectors(
                {"data": [{"index": False, "embedding": [0.0] * embed.EMBEDDING_DIM}]}, 1
            )
    else:
        assert rerank._scores({"results": [{"index": False, "relevance_score": 1}]}, 1) is None


@pytest.mark.parametrize("choices", [True, "bad", {"0": {}}, [None], ["bad"], [1]])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_malformed_completion_choices_have_upstream_error(
    choices: object, with_tools: bool
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": choices}))
    with pytest.raises(UpstreamError):
        if with_tools:
            await client.complete_with_tools(
                "http://localhost/v1", None, None, [], [], transport=transport
            )
        else:
            await client.complete("http://localhost/v1", None, None, [], transport=transport)


@pytest.mark.parametrize("message", [True, "bad", ["bad"]])
async def test_malformed_completion_message_has_upstream_error(message: object) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": message}]})
    )
    with pytest.raises(UpstreamError):
        await client.complete("http://localhost/v1", None, None, [], transport=transport)


def test_failed_database_initialization_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[sqlite3.Connection] = []
    original_connect = database.sqlite3.connect

    def capture_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", capture_connection)
    monkeypatch.setattr(
        database.sqlite_vec, "load", Mock(side_effect=RuntimeError("extension failed"))
    )
    with pytest.raises(RuntimeError, match="extension failed"):
        database.connect()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("select 1")


def test_unreadable_caption_does_not_discard_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    import pymupdf

    page = Mock(rect=pymupdf.Rect(0, 0, 600, 800), rotation_matrix=pymupdf.Matrix(1, 1))
    page.get_text.side_effect = RuntimeError("broken caption text")
    monkeypatch.setattr(figures, "_figure_rects", lambda _: [pymupdf.Rect(60, 80, 180, 240)])
    found = figures._page_figures(page, 1)
    assert len(found) == 1
    assert found[0].bbox == (0.1, 0.1, 0.3, 0.3)
    assert found[0].caption is None


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_source_geometry_matches_rendered_page(tmp_path: Path, rotation: int) -> None:
    import pymupdf

    from backend.rag import locate

    source = tmp_path / "rotated.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((60, 120), "Problem 1", fontsize=12)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
        pixmap.set_rect(pixmap.irect, (40, 90, 140))
        page.insert_image(pymupdf.Rect(60, 200, 180, 280), pixmap=pixmap)
        page.set_rotation(rotation)
        document.save(source)

    label = locate.find_label(source, 1, "Problem 1")
    figure = figures.extract_figures(source, "application/pdf")[0]
    with pymupdf.open(source) as document:
        page = document[0]
        label_box = page.search_for("Problem 1")[0] * page.rotation_matrix
        expected_label = (
            label_box.x0 / page.rect.width,
            label_box.y0 / page.rect.height,
            label_box.x1 / page.rect.width,
            label_box.y1 / page.rect.height,
        )
        assert label == pytest.approx(expected_label)
        # Inspect actual rendered pixels, so the test catches a box that is plausible
        # numerically but crops the wrong portion of the rotated source.
        x0, y0, x1, y1 = figure.bbox
        crop = page.get_pixmap(
            clip=pymupdf.Rect(
                x0 * page.rect.width,
                y0 * page.rect.height,
                x1 * page.rect.width,
                y1 * page.rect.height,
            )
        )
        assert crop.pixel(crop.width // 2, crop.height // 2) == (40, 90, 140)


def test_helper_status_returns_while_cold_start_owns_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from backend.llm.rerank_server import RerankServer

    server = RerankServer()
    loading = Event()
    release = Event()
    monkeypatch.setattr(server, "_find_binary", lambda: tmp_path / "fake-llama-server")
    monkeypatch.setattr(server, "_ensure_weights", lambda: None)
    monkeypatch.setattr(server, "_healthy", lambda: False)
    monkeypatch.setattr(server, "_check_installed", lambda: None)

    def await_health(_binary: Path) -> None:
        loading.set()
        assert release.wait(5), "test did not release the simulated cold start"

    monkeypatch.setattr(server, "_spawn_and_await", await_health)

    def start() -> None:
        with server._lock:
            server._start_locked()

    with ThreadPoolExecutor(max_workers=2) as pool:
        startup = pool.submit(start)
        try:
            assert loading.wait(2)
            snapshot = pool.submit(server.status).result(timeout=2)
            assert snapshot.state == "loading"
            assert snapshot.owned is True
            assert not startup.done()
        finally:
            release.set()
        startup.result(timeout=2)
    assert server.status().state == "stopped"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("marker_offset", [-30, 100])
def test_rotated_figures_stay_paired_with_their_problems(
    db: sqlite3.Connection, class_id: int, tmp_path: Path, rotation: int, marker_offset: int
) -> None:
    import pymupdf

    from backend.core import figures as figure_store
    from backend.core import solver
    from backend.core.segmentation import SegmentedProblem

    source = tmp_path / "paired.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        for index, top in enumerate((150, 450), start=1):
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
            pixmap.set_rect(pixmap.irect, (40 * index, 90, 140))
            page.insert_image(pymupdf.Rect(60, top, 180, top + 80), pixmap=pixmap)
            page.insert_text((60, top + marker_offset), f"Problem {index}", fontsize=12)
        page.set_rotation(rotation)
        document.save(source)

    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'paired.pdf', ?, 'application/pdf', 10, 'ready')",
            (class_id, str(source)),
        ).lastrowid
    )
    artifact_id = int(
        db.execute(
            "insert into artifacts (class_id, kind, title, state) "
            "values (?, 'solution_set', 'Rotation regression', 'awaiting_review')",
            (class_id,),
        ).lastrowid
    )
    figure_store.store_figures(db, document_id, figures.extract_figures(source, "application/pdf"))
    db.commit()
    expected = figure_store.list_figures(db, document_id)
    solver.write_problems(
        db,
        artifact_id,
        [
            SegmentedProblem(
                label=f"Problem {index}",
                number=str(index),
                statement="Find the result.",
                document_id=document_id,
                page_number=1,
            )
            for index in (1, 2)
        ],
    )
    paired = db.execute(
        "select p.ordinal, f.content from artifact_parts f "
        "join artifact_parts p on p.id = f.parent_part_id "
        "where f.artifact_id = ? and f.kind = 'figure' order by p.ordinal",
        (artifact_id,),
    ).fetchall()
    assert [(row[0], row[1]) for row in paired] == [
        (index, str(figure["id"])) for index, figure in enumerate(expected)
    ]


def test_reused_image_has_one_figure_per_placement(tmp_path: Path) -> None:
    import pymupdf

    source = tmp_path / "reused-image.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
        pixmap.set_rect(pixmap.irect, (40, 90, 140))
        xref = page.insert_image(pymupdf.Rect(60, 150, 180, 230), pixmap=pixmap)
        page.insert_image(pymupdf.Rect(60, 450, 180, 530), xref=xref)
        document.save(source)

    found = figures.extract_figures(source, "application/pdf")
    assert len(found) == 2
    assert [figure.index for figure in found] == [1, 2]
    assert len({figure.bbox for figure in found}) == 2
