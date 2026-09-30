"""Uploaded-document evidence stays bounded, cited, and inside the selected class."""

from __future__ import annotations

import sqlite3

import pymupdf
import pytest

from backend.config import settings
from backend.core import (
    agent_store,
    agent_tools,
    app_settings,
    classes,
    document_access,
    sessions,
    tool_audit,
)
from backend.core.errors import LyraError, NotFoundError
from backend.rag import retrieve as retrieval
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect


def _document(db: sqlite3.Connection, class_id: int, name: str, *, state: str = "ready") -> int:
    cursor = db.execute(
        "insert into documents "
        "(class_id, filename, stored_path, mime, byte_size, state, pages_total) "
        "values (?, ?, ?, 'application/pdf', 100, ?, 3)",
        (class_id, name, f"unused/{name}", state),
    )
    db.commit()
    return int(cursor.lastrowid)


def _chunk(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    page: int,
    text: str,
    problem: str | None = None,
) -> None:
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "problem_number, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, ?, ?, ?, 'generic', 'test', 768)",
        (document_id, class_id, text, estimate_tokens(text), page, problem),
    )
    db.commit()


def test_nickname_is_visible_to_scoped_tools_and_citations(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "LADW_2026_08-31.pdf")
    _chunk(db, class_id, selected, 2, "A vector space is closed under addition.")
    same_alias = _document(db, class_id, "other.pdf")
    _chunk(db, class_id, same_alias, 1, "Different material")
    other_class = int(classes.create_class(db, name="Other")["id"])
    foreign = _document(db, other_class, "foreign.pdf")
    _chunk(db, other_class, foreign, 1, "Private material")
    db.execute(
        "update documents set nickname = 'Textbook' where id in (?, ?, ?)",
        (selected, same_alias, foreign),
    )
    db.commit()

    listed = document_access.inventory(db, class_id, selected)["documents"]
    assert [row["document_id"] for row in listed] == [selected]
    assert listed[0]["display_name"] == "Textbook"
    assert listed[0]["filename"] == "Textbook"
    assert listed[0]["original_filename"] == "LADW_2026_08-31.pdf"
    by_content = document_access.search(db, class_id, selected, "vector space")["sources"]
    assert by_content[0]["citation"] == "Textbook, p. 2"
    assert by_content[0]["document_id"] == selected
    assert (
        document_access.search(db, class_id, selected, "Textbook")["sources"][0]["document_id"]
        == selected
    )
    assert (
        document_access.search(db, class_id, selected, "LADW_2026_08-31")["sources"][0][
            "document_id"
        ]
        == selected
    )
    db.execute(
        "insert into document_read_pages (document_id, page_number, generation, content) "
        "values (?, 2, 'fixture', 'A vector space is closed under addition.')",
        (selected,),
    )
    db.commit()
    db.executescript(agent_store.TABLE_SQL)
    db.executescript(tool_audit.TABLE_SQL)
    app_settings.update_settings_row(db, {"endpoint_url": "http://127.0.0.1:8080/v1"})
    session = int(sessions.create_session(db, class_id)["id"])
    registry, _ = agent_tools.build_agent_registry(
        db,
        class_id,
        session,
        "agent",
        selected_document_id=selected,
        document_endpoint="http://127.0.0.1:8080/v1",
    )
    tool_list = registry["list_documents"].handler().value["documents"]
    assert tool_list[0]["filename"] == "Textbook"
    tool_read = registry["read_document_page"].handler(document_id=selected, page_number=2)
    assert tool_read.ok
    assert tool_read.value["sources"][0]["citation"] == "Textbook, p. 2"

    _chunk(db, class_id, selected, 3, "Another vector space fact")
    cursor = document_access.search(db, class_id, selected, "vector", limit=1)["next_cursor"]
    assert cursor is not None
    db.execute("update documents set nickname = 'Textbook II' where id = ?", (selected,))
    db.commit()
    with pytest.raises(ValueError, match="expired"):
        document_access.search(db, class_id, selected, "vector", limit=1, cursor=cursor)


def test_selected_document_tools_refuse_other_class_and_other_selected_file(
    db: sqlite3.Connection, class_id: int
) -> None:
    db.executescript(agent_store.TABLE_SQL)
    db.executescript(tool_audit.TABLE_SQL)
    app_settings.update_settings_row(db, {"endpoint_url": "http://127.0.0.1:8080/v1"})
    selected = _document(db, class_id, "worksheet.pdf")
    _chunk(db, class_id, selected, 3, "Problem 9: solve x + 4 = 7", "9")
    other = _document(db, class_id, "private-key.pdf")
    _chunk(db, class_id, other, 1, "another document")
    other_class = int(classes.create_class(db, name="Other")["id"])
    foreign = _document(db, other_class, "foreign.pdf")
    session = int(sessions.create_session(db, class_id)["id"])
    registry, _ = agent_tools.build_agent_registry(
        db,
        class_id,
        session,
        "agent",
        selected_document_id=selected,
        document_endpoint="http://127.0.0.1:8080/v1",
    )

    assert {
        "list_documents",
        "search_documents",
        "read_document_page",
        "read_document_problem",
    } <= set(registry)
    found = registry["read_document_problem"].handler(document_id=selected, problem_number="9")
    assert found.ok and "x + 4 = 7" in str(found.value)
    assert "p. 3" in str(found.value)
    assert not registry["read_document_page"].handler(document_id=other, page_number=1).ok
    assert not registry["read_document_page"].handler(document_id=foreign, page_number=1).ok
    assert [
        item["document_id"] for item in registry["list_documents"].handler().value["documents"]
    ] == [selected]

    db.execute("update documents set class_id = ? where id = ?", (other_class, selected))
    db.commit()
    assert (
        not registry["read_document_problem"].handler(document_id=selected, problem_number="9").ok
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(RuntimeError("helper stopped"), id="stopped"),
        pytest.param(ConnectionError("helper cold"), id="cold"),
        pytest.param(ValueError("corrupt embedding response"), id="corrupt"),
    ],
)
def test_embedding_outage_returns_cited_lexical_evidence(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    selected = _document(db, class_id, "worksheet.pdf")
    _chunk(db, class_id, selected, 3, "Problem 9: the circuit uses a 12 ohm resistor.", "9")

    def unavailable(_: str) -> list[float]:
        raise failure

    monkeypatch.setattr(retrieval, "embed_query", unavailable)
    result = retrieval.retrieve(db, class_id, "circuit resistor", 500, document_id=selected)
    assert result.lexical_fallback
    assert len(result.chunks) == 1
    assert result.chunks[0].page_number == 3
    assert "12 ohm" in result.chunks[0].content


def test_page_coverage_distinguishes_unreadable_from_missing_problem(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "mixed.pdf")
    _chunk(db, class_id, selected, 1, "Instructions: complete all problems.")
    db.execute(
        "insert into document_pages (document_id, page_number, state) values (?, 2, 'scanned')",
        (selected,),
    )
    db.commit()
    page = document_access.read_page(db, class_id, selected, selected, 2)
    assert page["coverage"] == "not_attempted"
    assert page["sources"] == []
    assert page["needs_image"] is True


def test_numbered_section_read_is_bounded_and_cannot_cross_selection(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "notes.pdf")
    other = _document(db, class_id, "other.pdf")
    _chunk(db, class_id, selected, 7, "Section 4.2: integrate by substitution")
    _chunk(db, class_id, other, 7, "Section 4.2: unrelated secret")
    db.execute("update chunks set section_number = '4.2' where page_number = 7")
    db.commit()
    result = document_access.read_section(db, class_id, selected, selected, "4.2")
    assert result["found"] is True
    assert len(result["sources"]) == 1
    assert "substitution" in str(result["sources"])
    with pytest.raises(ValueError, match="selected document"):
        document_access.read_section(db, class_id, selected, other, "4.2")


def test_page_and_problem_continuation_reaches_every_character(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "long.pdf")
    pieces = ["α" * 2500, "β" * 2500, "γ" * 2500]
    for piece in pieces:
        _chunk(db, class_id, selected, 1, piece, "1")
    db.execute(
        "insert into document_read_pages values (?, 1, 'fixture', ?)",
        (selected, "".join(pieces)),
    )
    db.commit()
    for reader, args in (
        (document_access.read_page, (selected, 1)),
        (document_access.read_problem, (selected, "1")),
    ):
        cursor = None
        seen = []
        while True:
            result = reader(db, class_id, selected, *args, cursor=cursor)
            seen.extend(str(source["text"]) for source in result["sources"])
            assert result["truncated"] == result["has_more"]
            if not result["has_more"]:
                break
            cursor = result["next_cursor"]
        assert "".join(seen) == "".join(pieces)


def test_inventory_continuation_is_scoped_and_rejects_invalid_cursor(
    db: sqlite3.Connection, class_id: int
) -> None:
    for number in range(35):
        _document(db, class_id, f"document-{number}.pdf")
    first = document_access.inventory(db, class_id, None)
    assert len(first["documents"]) == 30
    second = document_access.inventory(db, class_id, None, cursor=first["next_cursor"])
    assert len(second["documents"]) == 5
    assert not second["has_more"]
    assert not set(item["document_id"] for item in first["documents"]) & set(
        item["document_id"] for item in second["documents"]
    )
    with pytest.raises(ValueError, match="cursor"):
        document_access.inventory(db, class_id, None, cursor="invalid")


def test_long_unicode_source_and_stale_cursor_are_explicit(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "unicode.pdf")
    text = "🧪α" * 4000
    _chunk(db, class_id, selected, 1, text, "2")
    first = document_access.read_problem(db, class_id, selected, selected, "2")
    assert first["sources"][0]["text_start"] == 0
    assert first["sources"][0]["text_end"] == 2600
    assert first["sources"][0]["text_length"] == len(text)
    assert first["sources"][0]["truncated"]
    seen = [str(first["sources"][0]["text"])]
    cursor = first["next_cursor"]
    while cursor:
        result = document_access.read_problem(db, class_id, selected, selected, "2", cursor=cursor)
        seen.extend(str(source["text"]) for source in result["sources"])
        cursor = result["next_cursor"]
    assert "".join(seen) == text
    db.execute("delete from chunks where document_id = ?", (selected,))
    db.commit()
    with pytest.raises(ValueError, match="cursor"):
        document_access.read_problem(
            db, class_id, selected, selected, "2", cursor=first["next_cursor"]
        )


def test_search_continuation_reaches_matches_after_eight(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "many.pdf")
    for number in range(12):
        _chunk(db, class_id, selected, 1, f"needle sentinel {number}")
    cursor = None
    found = []
    while True:
        result = document_access.search(db, class_id, selected, "needle", cursor=cursor)
        found.extend(source["text"] for source in result["sources"])
        if not result["has_more"]:
            break
        cursor = result["next_cursor"]
    assert len(found) == 12
    assert len(set(found)) == 12


def test_search_continuation_reaches_every_character(db: sqlite3.Connection, class_id: int) -> None:
    selected = _document(db, class_id, "long-search.pdf")
    content = "needle " + "α" * 9000
    _chunk(db, class_id, selected, 1, content)
    cursor = None
    pieces = []
    while True:
        result = document_access.search(db, class_id, selected, "needle", cursor=cursor)
        pieces.extend(str(source["text"]) for source in result["sources"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert "".join(pieces) == content


def test_search_cursor_expires_after_page_native_text_changes(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "pending.pdf", state="extracting")
    db.executemany(
        "insert into document_read_pages values (?, ?, 'fixture', ?)",
        ((selected, number, f"needle page {number}") for number in range(1, 10)),
    )
    db.commit()
    first = document_access.search(db, class_id, selected, "needle")
    assert first["next_cursor"] is not None
    db.execute(
        "update document_read_pages set content = 'needle revised' "
        "where document_id = ? and page_number = 9",
        (selected,),
    )
    db.commit()
    with pytest.raises(ValueError, match="expired document cursor"):
        document_access.search(db, class_id, selected, "needle", cursor=first["next_cursor"])


def test_search_cursor_expires_when_other_class_changes_bm25_order(
    db: sqlite3.Connection, class_id: int
) -> None:
    other_class = int(classes.create_class(db, name="Other")["id"])
    wanted = []
    for number in range(1, 10):
        document_id = _document(db, class_id, f"source-{number}.pdf")
        wanted.append(document_id)
        _chunk(db, class_id, document_id, 1, "alpha" if number == 1 else "beta")
    for number in range(100):
        document_id = _document(db, other_class, f"other-alpha-{number}.pdf")
        _chunk(db, other_class, document_id, 1, "alpha")

    first = document_access.search(db, class_id, None, "alpha beta")
    assert [source["document_id"] for source in first["sources"]] == wanted[1:]
    assert first["next_cursor"] is not None

    for number in range(300):
        document_id = _document(db, other_class, f"other-beta-{number}.pdf")
        _chunk(db, other_class, document_id, 1, "beta")
    with pytest.raises(ValueError, match="expired document cursor"):
        document_access.search(db, class_id, None, "alpha beta", cursor=first["next_cursor"])


def test_search_cursor_expires_after_same_class_replacement_and_deletion(
    db: sqlite3.Connection, class_id: int
) -> None:
    documents = [_document(db, class_id, f"source-{number}.pdf") for number in range(9)]
    for document_id in documents:
        _chunk(db, class_id, document_id, 1, "needle")
    first = document_access.search(db, class_id, None, "needle")
    assert first["next_cursor"] is not None
    db.execute(
        "update chunks set content = 'needle revised' where document_id = ?", (documents[8],)
    )
    db.commit()
    with pytest.raises(ValueError, match="expired document cursor"):
        document_access.search(db, class_id, None, "needle", cursor=first["next_cursor"])

    second = document_access.search(db, class_id, None, "needle")
    db.execute("delete from documents where id = ?", (documents[8],))
    db.commit()
    with pytest.raises(ValueError, match="expired document cursor"):
        document_access.search(db, class_id, None, "needle", cursor=second["next_cursor"])


def test_search_revision_and_query_share_one_read_snapshot(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = [_document(db, class_id, f"source-{number}.pdf") for number in range(9)]
    for document_id in documents:
        _chunk(db, class_id, document_id, 1, "needle")
    original_revision = document_access._revision

    def mutate_after_revision(
        conn: sqlite3.Connection, scoped_class: int, selected: int | None
    ) -> str:
        revision = original_revision(conn, scoped_class, selected)
        writer = connect()
        try:
            writer.execute("delete from documents where id = ?", (documents[8],))
            writer.commit()
        finally:
            writer.close()
        return revision

    monkeypatch.setattr(document_access, "_revision", mutate_after_revision)
    first = document_access.search(db, class_id, None, "needle")
    assert len(first["sources"]) == 8
    assert first["has_more"] is True
    monkeypatch.setattr(document_access, "_revision", original_revision)
    with pytest.raises(ValueError, match="expired document cursor"):
        document_access.search(db, class_id, None, "needle", cursor=first["next_cursor"])


def test_provider_search_tool_can_continue_and_expires_after_other_class_ingestion(
    db: sqlite3.Connection, class_id: int
) -> None:
    db.executescript(agent_store.TABLE_SQL)
    db.executescript(tool_audit.TABLE_SQL)
    app_settings.update_settings_row(db, {"endpoint_url": "http://127.0.0.1:8080/v1"})
    selected = _document(db, class_id, "selected.pdf")
    for number in range(9):
        _chunk(db, class_id, selected, number + 1, f"needle {number}")
    session = int(sessions.create_session(db, class_id)["id"])
    registry, _ = agent_tools.build_agent_registry(
        db,
        class_id,
        session,
        "agent",
        selected_document_id=selected,
        document_endpoint="http://127.0.0.1:8080/v1",
    )
    search = registry["search_documents"]
    assert search.parameters["properties"]["cursor"]["maxLength"] == 2048
    first = search.handler(query="needle")
    assert first.ok
    assert len(first.value["sources"]) == 8
    second = search.handler(query="needle", cursor=first.value["next_cursor"])
    assert second.ok
    assert len(second.value["sources"]) == 1
    assert second.value["sources"][0]["document_id"] == selected
    audit = db.execute(
        "select arguments_json from tool_audit_events "
        "where tool = 'search_documents' and arguments_json like '%cursor_sha256%' limit 1"
    ).fetchone()[0]
    assert first.value["next_cursor"] not in audit
    assert '"cursor_sha256"' in audit

    other_class = int(classes.create_class(db, name="Other")["id"])
    foreign = _document(db, other_class, "foreign.pdf")
    _chunk(db, other_class, foreign, 1, "needle")
    expired = search.handler(query="needle", cursor=first.value["next_cursor"])
    assert not expired.ok
    assert "expired document cursor" in str(expired.error)


def test_continuation_rechecks_selected_document_ownership(
    db: sqlite3.Connection, class_id: int
) -> None:
    selected = _document(db, class_id, "selected.pdf")
    _chunk(db, class_id, selected, 1, "x" * 4000, "1")
    cursor = document_access.read_problem(db, class_id, selected, selected, "1")["next_cursor"]
    other_class = int(classes.create_class(db, name="Other")["id"])
    db.execute("update documents set class_id = ? where id = ?", (other_class, selected))
    db.commit()
    with pytest.raises(NotFoundError):
        document_access.read_problem(db, class_id, selected, selected, "1", cursor=cursor)


def test_image_page_rechecks_scope_and_bounds(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = settings.uploads_dir / "diagram.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    pdf = pymupdf.open()
    pdf.new_page().insert_text((50, 50), "Diagram page")
    pdf.save(source)
    pdf.close()
    selected = _document(db, class_id, "diagram.pdf")
    db.execute(
        "update documents set stored_path = ?, pages_total = 1 where id = ?",
        (str(source), selected),
    )
    db.commit()
    image = document_access.image_page(db, class_id, selected, selected, 1)
    assert image.startswith(b"\x89PNG")
    other_class = int(classes.create_class(db, name="Moved")["id"])
    real_render = document_access.render.render_page

    def move_during_render(*args: object, **kwargs: object):
        path = real_render(*args, **kwargs)
        moved = sqlite3.connect(settings.db_path)
        try:
            moved.execute("update documents set class_id = ? where id = ?", (other_class, selected))
            moved.commit()
        finally:
            moved.close()
        return path

    with monkeypatch.context() as patch:
        patch.setattr(document_access.render, "render_page", move_during_render)
        with pytest.raises(NotFoundError):
            document_access.image_page(db, class_id, selected, selected, 1)
    db.execute("update documents set class_id = ? where id = ?", (class_id, selected))
    db.commit()
    with pytest.raises(NotFoundError):
        document_access.image_page(db, class_id, selected, selected, 2)
    monkeypatch.setattr(document_access, "MAX_VISUAL_BYTES", 10)
    with pytest.raises(LyraError, match="too large"):
        document_access.image_page(db, class_id, selected, selected, 1)
    db.execute("update documents set class_id = ? where id = ?", (other_class, selected))
    db.commit()
    with pytest.raises(NotFoundError):
        document_access.image_page(db, class_id, selected, selected, 1)
