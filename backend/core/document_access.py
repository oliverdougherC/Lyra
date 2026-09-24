"""Bounded, class-scoped reads of uploaded course material for class chat.

These reads use the indexed database evidence, never an arbitrary workspace path. A
selected document remains the only permitted source throughout a turn, and every call
rechecks the live row so a moved or deleted document cannot be read from a stale tool.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from backend.core.errors import NotFoundError
from backend.rag import render

MAX_RESULTS = 8
MAX_EXCERPT_CHARS = 2600
MAX_PAGE_CHARS = 7000
MAX_VISUAL_PAGES = 3
MAX_VISUAL_BYTES = 3 * 1024 * 1024
_PAGE_REFERENCE = re.compile(r"\b(?:page|p\.)\s*(\d{1,5})\b", re.IGNORECASE)
_PROBLEM_REFERENCE = re.compile(r"\b(?:problem|question|exercise|#)\s*(\d+[a-z]?)\b", re.IGNORECASE)


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
    conn: sqlite3.Connection, class_id: int, selected_id: int | None, *, limit: int = 30
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    limit = max(1, min(limit, 30))
    sql = (
        "select d.id, d.filename, d.state, d.pages_total, d.pages_skipped, "
        "(select count(*) from chunks c where c.document_id = d.id) as chunks "
        "from documents d where d.class_id = ?"
    )
    args: list[object] = [class_id]
    if selected_id is not None:
        sql += " and d.id = ?"
        args.append(selected_id)
    sql += " order by d.id desc limit ?"
    args.append(limit + 1)
    rows = conn.execute(sql, args).fetchall()
    documents: list[dict[str, object]] = []
    for row in rows[:limit]:
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
                "searchable": bool(row["chunks"]),
            }
        )
    return {"documents": documents, "truncated": len(rows) > limit}


def _source(row: sqlite3.Row) -> dict[str, object]:
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
        "text": str(row["content"])[:MAX_EXCERPT_CHARS],
        "truncated": len(str(row["content"])) > MAX_EXCERPT_CHARS,
    }


def search(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    query: str,
    *,
    limit: int = MAX_RESULTS,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    terms = [f'"{word.replace(chr(34), "")}"' for word in query.split()[:12] if word.strip('"')]
    if not terms:
        raise ValueError("Search text cannot be blank.")
    limit = max(1, min(limit, MAX_RESULTS))
    sql = (
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks_fts join chunks c on c.id = chunks_fts.rowid "
        "join documents d on d.id = c.document_id "
        "where chunks_fts match ? and c.class_id = ? and d.state = 'ready'"
    )
    args: list[object] = [" OR ".join(terms), class_id]
    if selected_id is not None:
        sql += " and d.id = ?"
        args.append(selected_id)
    sql += " order by bm25(chunks_fts), c.id limit ?"
    args.append(limit + 1)
    rows = conn.execute(sql, args).fetchall()
    return {"sources": [_source(row) for row in rows[:limit]], "truncated": len(rows) > limit}


def read_page(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    page_number: int,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    if page_number < 1:
        raise ValueError("Page number must be positive.")
    doc = conn.execute(
        "select id, filename, state, pages_total from documents where id = ? and class_id = ?",
        (document_id, class_id),
    ).fetchone()
    if doc is None:
        raise NotFoundError("That document is no longer in this class.")
    if doc["pages_total"] is not None and page_number > int(doc["pages_total"]):
        raise NotFoundError("That page does not exist in the document.")
    coverage = conn.execute(
        "select p.state, p.error_message, p.skip_reason, "
        "i.page_number is not null as indexed from document_pages p "
        "left join document_index_pages i on i.document_id = p.document_id "
        "and i.page_number = p.page_number "
        "where p.document_id = ? and p.page_number = ?",
        (document_id, page_number),
    ).fetchone()
    rows = conn.execute(
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.document_id = ? and c.class_id = ? and c.page_number = ? "
        "and d.state = 'ready' order by c.id",
        (document_id, class_id, page_number),
    ).fetchall()
    output: list[dict[str, object]] = []
    used = 0
    for row in rows:
        if used >= MAX_PAGE_CHARS:
            break
        item = _source(row)
        item["text"] = str(item["text"])[: MAX_PAGE_CHARS - used]
        used += len(str(item["text"]))
        output.append(item)
    coverage_state = "not_attempted"
    if coverage is not None:
        if coverage["state"] == "failed":
            coverage_state = "recognition_failed"
        elif coverage["state"] == "scanned" and coverage["skip_reason"] == "blank":
            coverage_state = "blank"
        elif coverage["state"] in ("text", "recognized"):
            coverage_state = "readable"
    return {
        "document_id": document_id,
        "page_number": page_number,
        "coverage": coverage_state,
        "indexed": bool(coverage["indexed"]) if coverage else False,
        "reason": str(coverage["skip_reason"] or "")[:80] if coverage else "",
        "sources": output,
        "truncated": len(output) < len(rows),
        "needs_image": coverage_state != "blank"
        and (not output or any("[figure]" in str(item["text"]) for item in output)),
    }


def read_problem(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    problem_number: str,
    page_number: int | None = None,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    doc = conn.execute(
        "select 1 from documents where id = ? and class_id = ? and state = 'ready'",
        (document_id, class_id),
    ).fetchone()
    if doc is None:
        raise NotFoundError("That document is not currently searchable in this class.")
    sql = (
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.document_id = ? and c.class_id = ? and c.problem_number = ? "
    )
    args: list[object] = [document_id, class_id, problem_number]
    if page_number is not None:
        sql += "and c.page_number = ? "
        args.append(page_number)
    sql += "order by c.page_number, c.id limit 9"
    rows = conn.execute(sql, args).fetchall()
    return {
        "sources": [_source(row) for row in rows[:8]],
        "truncated": len(rows) > 8,
        "found": bool(rows),
    }


def read_section(
    conn: sqlite3.Connection,
    class_id: int,
    selected_id: int | None,
    document_id: int,
    section_number: str,
) -> dict[str, object]:
    _scope(conn, class_id, selected_id)
    document_id = _target(selected_id, document_id)
    if not re.fullmatch(r"[A-Za-z]?\.?\d+(?:\.\d+)*", section_number):
        raise ValueError("Section number is invalid.")
    rows = conn.execute(
        "select c.document_id, c.content, c.page_number, c.problem_number, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.document_id = ? and c.class_id = ? and d.state = 'ready' "
        "and (c.section_number = ? or c.section_number like ?) "
        "order by c.page_number, c.id limit 9",
        (document_id, class_id, section_number.upper(), section_number.upper() + ".%"),
    ).fetchall()
    return {
        "sources": [_source(row) for row in rows[:8]],
        "truncated": len(rows) > 8,
        "found": bool(rows),
    }


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
