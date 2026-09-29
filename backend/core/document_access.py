"""Bounded, class-scoped reads of uploaded course material for class chat.

These reads use the indexed database evidence, never an arbitrary workspace path. A
selected document remains the only permitted source throughout a turn, and every call
rechecks the live row so a moved or deleted document cannot be read from a stale tool.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from backend.config import settings
from backend.core import ownership
from backend.core.errors import LyraError, NotFoundError
from backend.rag import render
from backend.rag.chunk import chunk_document
from backend.rag.parse import PAGE_MIMES, ParsedDocument, ParsedPage, parse_document
from backend.storage import private
from backend.storage.database import connect

MAX_RESULTS = 8
MAX_EXCERPT_CHARS = 2600
MAX_PAGE_CHARS = 7000
MAX_VISUAL_PAGES = 3
MAX_VISUAL_BYTES = 3 * 1024 * 1024
_PAGE_REFERENCE = re.compile(r"\b(?:page|p\.)\s*(\d{1,5})\b", re.IGNORECASE)
_PROBLEM_REFERENCE = re.compile(r"\b(?:problem|question|exercise|#)\s*(\d+[a-z]?)\b", re.IGNORECASE)


def _revision(conn: sqlite3.Connection, class_id: int, selected_id: int | None) -> str:
    """A cheap fingerprint of every row that a continuation is allowed to read."""
    digest = hashlib.sha256()
    sql = (
        "select d.id, d.class_id, d.filename, d.created_at, d.state, d.refresh_state, "
        "(select count(*) from chunks c where c.document_id = d.id), "
        "coalesce((select max(c.id) from chunks c where c.document_id = d.id), 0), "
        "coalesce((select max(p.generation) from document_read_pages p "
        "where p.document_id = d.id), '') from documents d "
        "where d.class_id = ?"
    )
    args: list[object] = [class_id]
    if selected_id is not None:
        sql += " and d.id = ?"
        args.append(selected_id)
    sql += " order by d.id"
    for row in conn.execute(sql, args):
        digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()[:24]


def _encode_cursor(context: list[object], revision: str, index: int, offset: int) -> str:
    payload = [1, context, revision, index, offset]
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return encoded.decode().rstrip("=")


def _decode_cursor(cursor: str | None, context: list[object], revision: str) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        version, saved_context, saved_revision, index, offset = payload
        if (
            version != 1
            or saved_context != context
            or saved_revision != revision
            or type(index) is not int
            or type(offset) is not int
            or index < 0
            or offset < 0
        ):
            raise ValueError
        return index, offset
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or expired document cursor. Start this read again.") from exc


def _page_result(
    conn: sqlite3.Connection,
    sql: str,
    args: list[object],
    context: list[object],
    revision: str,
    cursor: str | None,
    *,
    limit: int = MAX_RESULTS,
    chars: int = MAX_PAGE_CHARS,
) -> dict[str, object]:
    """Read a bounded window, including the rest of a long source on later calls."""
    index, offset = _decode_cursor(cursor, context, revision)
    count = int(conn.execute(f"select count(*) from ({sql})", args).fetchone()[0])  # noqa: S608
    if index > count or (cursor is not None and index == count) or (index == count and offset):
        raise ValueError("Invalid or expired document cursor. Start this read again.")
    rows = conn.execute(f"{sql} limit ? offset ?", [*args, limit + 1, index]).fetchall()
    return _slice_rows(rows, context, revision, index, offset, count, limit=limit, chars=chars)


def _slice_rows(
    rows: list[sqlite3.Row] | list[dict[str, object]],
    context: list[object],
    revision: str,
    index: int,
    offset: int,
    count: int,
    *,
    limit: int = MAX_RESULTS,
    chars: int = MAX_PAGE_CHARS,
) -> dict[str, object]:
    sources: list[dict[str, object]] = []
    used = 0
    for row in rows[:limit]:
        content = str(row["content"])
        if offset >= len(content):
            raise ValueError("Invalid or expired document cursor. Start this read again.")
        end = min(len(content), offset + MAX_EXCERPT_CHARS, offset + chars - used)
        if end <= offset:
            break
        item = _source(row, offset, end)
        sources.append(item)
        used += end - offset
        if end < len(content):
            offset = end
            break
        index += 1
        offset = 0
        if used >= chars:
            break
    has_more = index < count or offset > 0
    return {
        "sources": sources,
        "truncated": has_more,
        "has_more": has_more,
        "next_cursor": _encode_cursor(context, revision, index, offset) if has_more else None,
    }


def _scope(conn: sqlite3.Connection, class_id: int, selected_id: int | None) -> None:
    if selected_id is None:
        return
    row = conn.execute(
        "select 1 from documents where id = ? and class_id = ?", (selected_id, class_id)
    ).fetchone()
    if row is None:
        raise NotFoundError(
            "The selected document is no longer in this class. Choose a source again."
        )


def _target(selected_id: int | None, document_id: int) -> int:
    if selected_id is not None and document_id != selected_id:
        raise ValueError("This conversation is limited to the selected document.")
    return document_id


def inventory(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    *,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    limit = max(1, min(limit, 30))
    revision = _revision(conn, class_id, selected_id)
    context: list[object] = ["inventory", class_id, selected_id]
    index, offset = _decode_cursor(cursor, context, revision)
    if offset:
        raise ValueError("Invalid document cursor.")
    if cursor is not None:
        total_sql = "select count(*) from documents where class_id = ?"
        total_args: list[object] = [class_id]
        if selected_id is not None:
            total_sql += " and id = ?"
            total_args.append(selected_id)
        if index >= int(conn.execute(total_sql, total_args).fetchone()[0]):
            raise ValueError("Invalid or expired document cursor. Start this read again.")
    sql = (
        "select d.id, d.filename, d.state, d.pages_total, d.pages_skipped, "
        "(select count(*) from chunks c where c.document_id = d.id) as chunks "
        "from documents d where d.class_id = ?"
    )
    args: list[object] = [class_id]
    if selected_id is not None:
        sql += " and d.id = ?"
        args.append(selected_id)
    sql += " order by d.id desc limit ? offset ?"
    args.extend((limit + 1, index))
    rows = conn.execute(sql, args).fetchall()
    documents: list[dict[str, object]] = []
    for row in rows[:limit]:
        page_text = bool(
            conn.execute(
                "select 1 from document_read_pages where document_id = ? limit 1",
                (int(row["id"]),),
            ).fetchone()
        )
        semantic_ready = row["state"] == "ready" and bool(row["chunks"])
        missing = conn.execute(
            "select page_number from document_pages where document_id = ? "
            "and (state = 'failed' or "
            "(state = 'scanned' and coalesce(skip_reason, '') != 'blank')) "
            "order by page_number limit 21",
            (int(row["id"]),),
        ).fetchall()
        documents.append(
            {
                "document_id": int(row["id"]),
                "filename": str(row["filename"]),
                "state": str(row["state"]),
                "pages_total": row["pages_total"],
                "pages_needing_recognition": ",".join(str(item[0]) for item in missing[:20]),
                "coverage_truncated": len(missing) > 20,
                "searchable": page_text or semantic_ready,
                "text_readable": page_text or semantic_ready,
                "semantic_ready": semantic_ready,
            }
        )
    more = len(rows) > limit
    return {
        "documents": documents,
        "truncated": more,
        "has_more": more,
        "next_cursor": _encode_cursor(context, revision, index + limit, 0) if more else None,
    }


def _source(row: sqlite3.Row | dict[str, object], start: int, end: int) -> dict[str, object]:
    page = row["page_number"]
    problem = row["problem_number"]
    label = str(row["filename"])
    if page is not None:
        label += f", p. {page}"
    if problem:
        label += f", problem {problem}"
    return {
        "document_id": int(row["document_id"]),
        "filename": str(row["filename"]),
        "page_number": page,
        "problem_number": problem,
        "citation": label,
        "text": str(row["content"])[start:end],
        "text_start": start,
        "text_end": end,
        "text_length": len(str(row["content"])),
        "truncated": end < len(str(row["content"])),
    }


def search(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    query: str,
    *,
    limit: int = MAX_RESULTS,
    cursor: str | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    terms = [word.replace('"', "") for word in query.split()[:12] if word.strip('"')]
    if not terms:
        raise ValueError("Search text cannot be blank.")
    limit = max(1, min(limit, MAX_RESULTS))
    sql = (
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename, "
        "0 as tier, bm25(chunks_fts) as score, c.id as source_order "
        "from chunks_fts join chunks c on c.id = chunks_fts.rowid "
        "join documents d on d.id = c.document_id "
        "where chunks_fts match ? and c.class_id = ? and d.state = 'ready'"
    )
    args: list[object] = [" OR ".join(f'"{term}"' for term in terms), class_id]
    if selected_id is not None:
        sql += " and d.id = ?"
        args.append(selected_id)
    # A first upload can have page-native text while its vectors are unavailable.
    # Keep this lexical path usable without claiming semantic readiness.
    direct = (
        "select p.document_id, p.content, p.page_number, null as problem_number, "
        "d.filename, 1 as tier, 0 as score, p.page_number as source_order "
        "from document_read_pages p join documents d on d.id = p.document_id "
        "where d.class_id = ? and d.state != 'ready' and ("
        + " or ".join("instr(lower(p.content), lower(?)) > 0" for _ in terms)
        + ")"
    )
    args.extend([class_id, *terms])
    if selected_id is not None:
        direct += " and d.id = ?"
        args.append(selected_id)
    combined = (
        f"select * from ({sql} union all {direct}) "  # noqa: S608
        "order by tier, score, document_id, source_order"
    )
    context: list[object] = ["search", class_id, selected_id, query]
    # One SQLite read snapshot covers ownership, revision, ranking, and excerpts. A
    # concurrent ingestion can commit in WAL, but cannot split these observations.
    conn.execute("savepoint document_search_read")
    try:
        _scope(conn, class_id, selected_id)
        # All FTS5 statistics (including other classes) advance with chunk writes.
        # The generation and the bounded page query share this read snapshot.
        generation = conn.execute(
            "select generation from document_search_generation where id = 1"
        ).fetchone()[0]
        revision = hashlib.sha256(
            f"{_revision(conn, class_id, selected_id)}:{generation}".encode()
        ).hexdigest()[:24]
        return _page_result(conn, combined, args, context, revision, cursor, limit=limit)
    finally:
        conn.execute("release savepoint document_search_read")


def read_page(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    page_number: int,
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    if page_number < 1:
        raise ValueError("Page number must be positive.")
    doc = conn.execute(
        "select id, class_id, filename, state, pages_total, stored_path, mime, created_at "
        "from documents where id = ? and class_id = ?",
        (document_id, class_id),
    ).fetchone()
    if doc is None:
        raise NotFoundError("That document is no longer in this class.")
    if doc["pages_total"] is not None and page_number > int(doc["pages_total"]):
        raise NotFoundError("That page does not exist in the document.")
    _backfill_read_pages(conn, doc)
    coverage = conn.execute(
        "select p.state, p.error_message, p.skip_reason, "
        "i.page_number is not null as indexed from document_pages p "
        "left join document_index_pages i on i.document_id = p.document_id "
        "and i.page_number = p.page_number "
        "where p.document_id = ? and p.page_number = ?",
        (document_id, page_number),
    ).fetchone()
    native = conn.execute(
        "select 1 from document_read_pages where document_id = ? and page_number = ?",
        (document_id, page_number),
    ).fetchone()
    sql = (
        "select p.document_id, p.content, p.page_number, null as problem_number, "
        "d.filename from document_read_pages p join documents d on d.id = p.document_id "
        "where p.document_id = ? and d.class_id = ? and p.page_number = ? "
        "order by p.page_number"
    )
    revision = _revision(conn, class_id, selected_id)
    result = _page_result(
        conn,
        sql,
        [document_id, class_id, page_number],
        ["page", class_id, selected_id, document_id, page_number],
        revision,
        cursor,
    )
    output = result["sources"]
    coverage_state = "not_attempted"
    if coverage is not None:
        if coverage["state"] == "failed":
            coverage_state = "recognition_failed"
        elif coverage["state"] == "scanned" and coverage["skip_reason"] == "blank":
            coverage_state = "blank"
        elif coverage["state"] in ("text", "recognized"):
            coverage_state = "readable"
    return {
        **result,
        "document_id": document_id,
        "page_number": page_number,
        "coverage": coverage_state,
        "indexed": bool(coverage["indexed"]) if coverage else False,
        "reason": str(coverage["skip_reason"] or "")[:80] if coverage else "",
        "semantic_ready": doc["state"] == "ready",
        "page_attribution": "physical" if native else "unavailable",
        "needs_image": coverage_state != "blank"
        and (not output or any("[figure]" in str(item["text"]) for item in output)),
    }


def _backfill_read_pages(conn: sqlite3.Connection, doc: sqlite3.Row) -> None:
    """Lazily recover physical pages from an older index without discarding it."""
    document_id = int(doc["id"])
    if (
        doc["state"] != "ready"
        or conn.execute(
            "select 1 from document_read_pages where document_id = ? limit 1", (document_id,)
        ).fetchone()
    ):
        return
    path = Path(str(doc["stored_path"]))
    if not path.is_file():
        return
    try:
        parsed = parse_document(path, str(doc["mime"]))
    except Exception:
        # A legacy index can remain useful for search/problem reads even if the
        # retained source no longer parses. Exact page reads stay unavailable.
        return
    recognized = [
        ParsedPage(int(row["page_number"]), str(row["text"]))
        for row in conn.execute(
            "select page_number, text from document_pages where document_id = ? "
            "and state = 'recognized' and length(trim(coalesce(text, ''))) > 0",
            (document_id,),
        )
    ]
    pages = sorted(
        {page.page_number: page for page in [*parsed.pages, *recognized]}.values(),
        key=lambda page: page.page_number,
    )
    if not pages:
        return
    if conn.in_transaction:
        conn.execute("savepoint read_page_backfill")
        nested = True
    else:
        conn.execute("begin immediate")
        nested = False
    current = conn.execute(
        "select created_at, stored_path, class_id from documents where id = ? and state = 'ready'",
        (document_id,),
    ).fetchone()
    if current is None or (str(current[0]), str(current[1]), int(current[2])) != (
        str(doc["created_at"]),
        str(path),
        int(doc["class_id"]),
    ):
        if nested:
            conn.execute("rollback to read_page_backfill")
            conn.execute("release read_page_backfill")
        else:
            conn.rollback()
        return
    if not conn.execute(
        "select 1 from document_read_pages where document_id = ? limit 1", (document_id,)
    ).fetchone():
        generation = (
            "legacy-" + hashlib.sha256((str(doc["created_at"]) + str(path)).encode()).hexdigest()
        )
        conn.executemany(
            "insert into document_read_pages values (?, ?, ?, ?)",
            [(document_id, page.page_number, generation, page.text) for page in pages],
        )
    if nested:
        conn.execute("release read_page_backfill")
    else:
        conn.commit()


def read_problem(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    problem_number: str,
    page_number: int | None = None,
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    doc = conn.execute(
        "select id, class_id, filename, state, stored_path, mime, created_at "
        "from documents where id = ? and class_id = ?",
        (document_id, class_id),
    ).fetchone()
    if doc is None:
        raise NotFoundError("That document is no longer in this class.")
    _backfill_read_pages(conn, doc)
    context: list[object] = [
        "problem",
        class_id,
        selected_id,
        document_id,
        problem_number,
        page_number,
    ]
    revision = _revision(conn, class_id, selected_id)
    generation = conn.execute(
        "select generation from document_read_pages where document_id = ? limit 1",
        (document_id,),
    ).fetchone()
    if generation is not None and (
        doc["state"] != "ready" or str(generation[0]).startswith("legacy-")
    ):
        return _page_problem(
            conn, doc, document_id, problem_number, page_number, context, revision, cursor
        )
    if doc["state"] != "ready":
        raise NotFoundError("That document has no readable text for this problem yet.")
    sql = (
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.document_id = ? and c.class_id = ? and c.problem_number = ? "
    )
    args: list[object] = [document_id, class_id, problem_number]
    if page_number is not None:
        sql += "and c.page_number = ? "
        args.append(page_number)
    sql += "order by c.page_number, c.id"
    result = _page_result(
        conn,
        sql,
        args,
        context,
        revision,
        cursor,
    )
    return {
        **result,
        "found": bool(result["sources"]),
        "page_attribution": "physical" if generation is not None else "legacy_chunk_start",
    }


def _page_problem(
    conn: sqlite3.Connection,
    doc: sqlite3.Row,
    document_id: int,
    problem_number: str,
    page_number: int | None,
    context: list[object],
    revision: str,
    cursor: str | None,
) -> dict[str, object]:
    """Split directly readable pages into cited problem parts without vectors."""
    pages = [
        ParsedPage(int(row["page_number"]), str(row["content"]))
        for row in conn.execute(
            "select page_number, content from document_read_pages "
            "where document_id = ? order by page_number",
            (document_id,),
        )
    ]
    doc_type_row = conn.execute(
        "select doc_type from chunks where document_id = ? order by id limit 1", (document_id,)
    ).fetchone()
    doc_type = str(doc_type_row[0]) if doc_type_row else "homework"
    parsed = ParsedDocument(pages, max(page.page_number for page in pages), 0)
    pieces = chunk_document(parsed, doc_type)
    rows: list[dict[str, object]] = [
        {
            "document_id": document_id,
            "content": part.content,
            "page_number": part.page_number,
            "problem_number": part.problem_number,
            "filename": str(doc["filename"]),
        }
        for part in pieces
        if part.problem_number == problem_number
        and (page_number is None or part.page_number == page_number)
    ]
    index, offset = _decode_cursor(cursor, context, revision)
    if (
        index > len(rows)
        or (cursor is not None and index == len(rows))
        or (index == len(rows) and offset)
    ):
        raise ValueError("Invalid or expired document cursor. Start this read again.")
    result = _slice_rows(
        rows[index : index + MAX_RESULTS + 1], context, revision, index, offset, len(rows)
    )
    return {**result, "found": bool(result["sources"]), "page_attribution": "physical"}


def read_section(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    section_number: str,
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    if not re.fullmatch(r"[A-Za-z]?\.?\d+(?:\.\d+)*", section_number):
        raise ValueError("Section number is invalid.")
    sql = (
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.document_id = ? and c.class_id = ? and d.state = 'ready' "
        "and (c.section_number = ? or c.section_number like ?) "
        "order by c.page_number, c.id"
    )
    result = _page_result(
        conn,
        sql,
        [document_id, class_id, section_number.upper(), section_number.upper() + ".%"],
        ["section", class_id, selected_id, document_id, section_number],
        _revision(conn, class_id, selected_id),
        cursor,
    )
    return {**result, "found": bool(result["sources"])}


def visual_pages(
    conn: sqlite3.Connection, class_id: int, selected_id: int | None, question: str
) -> tuple[list[tuple[int, bytes]], int]:
    """Render at most three relevant selected-source pages for a vision-capable tutor.

    A generic request on a long book does not trigger a blind scan. A short worksheet
    can send its missing scanned pages, while an explicit page/problem selects exactly
    that evidence. The row identity is checked again by ``render_page`` at publication.
    """
    _scope(conn, class_id, selected_id)
    if selected_id is None:
        return [], 0
    doc = conn.execute(
        "select stored_path, mime, created_at, pages_total from documents "
        "where id = ? and class_id = ? and state in ('ready', 'unsupported')",
        (selected_id, class_id),
    ).fetchone()
    if doc is None:
        return [], 0
    pages = {int(match.group(1)) for match in _PAGE_REFERENCE.finditer(question)}
    for match in _PROBLEM_REFERENCE.finditer(question):
        rows = conn.execute(
            "select distinct page_number from chunks where document_id = ? and problem_number = ? "
            "and page_number is not null limit 4",
            (selected_id, match.group(1)),
        ).fetchall()
        pages.update(int(row[0]) for row in rows)
    visual = re.search(r"\b(diagram|figure|graph|circuit|matrix|table)\b", question, re.I)
    if not pages and visual:
        rows = conn.execute(
            "select distinct page_number from document_figures "
            "where document_id = ? order by page_number limit 4",
            (selected_id,),
        ).fetchall()
        pages.update(int(row[0]) for row in rows)
        if not pages:
            rows = conn.execute(
                "select distinct page_number from chunks where document_id = ? "
                "and (content like '%[figure]%' or lower(content) like ?) "
                "and page_number is not null order by page_number limit 4",
                (selected_id, f"%{visual.group(1).lower()}%"),
            ).fetchall()
            pages.update(int(row[0]) for row in rows)
    if not pages and doc["pages_total"] is not None and int(doc["pages_total"]) <= 4:
        rows = conn.execute(
            "select page_number from document_pages where document_id = ? "
            "and (state = 'failed' or "
            "(state = 'scanned' and coalesce(skip_reason, '') != 'blank')) "
            "order by page_number limit 4",
            (selected_id,),
        ).fetchall()
        pages.update(int(row[0]) for row in rows)
    selected = sorted(pages)[:MAX_VISUAL_PAGES]
    images: list[tuple[int, bytes]] = []
    for number in selected:
        path = render.render_page(
            selected_id,
            Path(str(doc["stored_path"])),
            str(doc["mime"]),
            number,
            created_at=str(doc["created_at"]),
        )
        if path.stat().st_size <= MAX_VISUAL_BYTES:
            image = path.read_bytes()
            _scope(conn, class_id, selected_id)
            images.append((number, image))
    return images, max(0, len(pages) - len(images))


def image_page(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    page_number: int,
) -> bytes:
    """Return one bounded page image after a fresh ownership and generation check.

    The caller owns endpoint consent and session authorization. This primitive also
    validates the selected source on every call, including a later tool round. Rendering
    happens before taking the lifecycle mutex because cache publication takes that mutex;
    the final identity check and bounded cache read run under it together.
    """
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    if page_number < 1:
        raise ValueError("Page number must be positive.")
    doc = conn.execute(
        "select id, class_id, stored_path, mime, created_at, pages_total "
        "from documents where id = ? and class_id = ?",
        (document_id, class_id),
    ).fetchone()
    if doc is None:
        raise NotFoundError("That document is no longer in this class.")
    if str(doc["mime"]) not in PAGE_MIMES:
        raise LyraError(render.NOT_RENDERABLE)
    if doc["pages_total"] is not None and page_number > int(doc["pages_total"]):
        raise NotFoundError(render.NOT_A_PAGE)
    path = render.render_page(
        document_id,
        Path(str(doc["stored_path"])),
        str(doc["mime"]),
        page_number,
        created_at=str(doc["created_at"]),
    )
    with ownership.lifecycle_mutation():
        fresh = connect()
        try:
            current = fresh.execute(
                "select class_id, stored_path, mime, created_at from documents where id = ?",
                (document_id,),
            ).fetchone()
            if current is None or tuple(current) != (
                class_id,
                str(doc["stored_path"]),
                str(doc["mime"]),
                str(doc["created_at"]),
            ):
                raise NotFoundError("That document is no longer in this class.")
            try:
                return private.read_owned_bytes(
                    path,
                    root=settings.cache_dir or settings.data_dir,
                    max_bytes=MAX_VISUAL_BYTES,
                )
            except ValueError as exc:
                raise LyraError("That page image is too large to send to the tutor.") from exc
            except (OSError, private.PrivacyContractError) as exc:
                raise NotFoundError("That page image is no longer available.") from exc
        finally:
            fresh.close()
