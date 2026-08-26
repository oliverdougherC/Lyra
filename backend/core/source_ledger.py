"""Per-class source ledger shared by research, drafting, critique, and review."""

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from backend.core import agent_attempts, classes
from backend.core.errors import NotFoundError

COURSE = "course"
WEB = "web"
SOURCE_TYPES = (COURSE, WEB)

# Snapshots may be megabytes and remain intact for audit. Everything that can enter a
# prompt is either bounded metadata or a passage explicitly recorded as relied on.
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 4_096
MAX_ACCESS_DATE_CHARS = 128
MAX_SECTION_REF_CHARS = 200
MAX_RELIED_EXCERPT_CHARS = 8_000

_SOURCE_COLUMNS = (
    "id, class_id, source_type, document_id, url, title, accessed_at, snapshot, "
    "current_revision_id, created_at, updated_at"
)
_PROMPT_SOURCE_COLUMNS = (
    "id, class_id, source_type, document_id, url, title, accessed_at, current_revision_id"
)


def _normalize_url(url: str) -> str:
    value = url.strip()
    if len(value) > MAX_URL_CHARS:
        raise ValueError("Web source URLs are too long")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Web sources require an http or https URL")
    # Fragments identify a place in one snapshot, not a different source.
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def _require_document(conn: sqlite3.Connection, class_id: int, document_id: int) -> None:
    row = conn.execute("select class_id from documents where id = ?", (document_id,)).fetchone()
    if row is None or int(row["class_id"]) != class_id:
        raise NotFoundError("That course document does not exist in this class.")


def _decode_source(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    source = dict(row)
    revision_id = source.get("current_revision_id")
    revision = None
    if revision_id is not None:
        revision_row = conn.execute(
            "select id, source_id, revision, final_url, content_type, snapshot_hash, truncated, "
            "accessed_at, created_at from writer_source_revisions where id = ?",
            (revision_id,),
        ).fetchone()
        if revision_row is not None:
            revision = dict(revision_row)
            revision["truncated"] = bool(revision["truncated"])
    source["revision"] = revision
    source["excerpts"] = [
        dict(excerpt)
        for excerpt in conn.execute(
            "select id, source_id, source_revision_id, section_ref, excerpt, created_at "
            "from writer_source_excerpts where source_id = ? order by id",
            (source["id"],),
        )
    ]
    return source


def _bounded(value: object, ceiling: int) -> str:
    text = str(value or "")
    return text if len(text) <= ceiling else f"{text[: ceiling - 1]}…"


def prompt_source(source: Mapping[str, object]) -> dict[str, object]:
    """Project one audit row into the only ledger shape allowed in model context.

    Deliberately absent: ``snapshot``, internal timestamps, and any other accidental
    columns a future migration may add. Excerpt text is not truncated: storage accepts
    it only after checking it is an exact, bounded passage from the underlying source.
    """
    excerpts: list[dict[str, object]] = []
    raw_excerpts = source.get("excerpts")
    if isinstance(raw_excerpts, Sequence) and not isinstance(raw_excerpts, (str, bytes)):
        for raw in raw_excerpts:
            if not isinstance(raw, Mapping):
                continue
            excerpts.append(
                {
                    "id": raw.get("id"),
                    "source_revision_id": raw.get("source_revision_id"),
                    "section_ref": (
                        _bounded(raw.get("section_ref"), MAX_SECTION_REF_CHARS)
                        if raw.get("section_ref")
                        else None
                    ),
                    "excerpt": str(raw.get("excerpt") or ""),
                }
            )
    return {
        "id": source.get("id"),
        "class_id": source.get("class_id"),
        "source_type": source.get("source_type"),
        "document_id": source.get("document_id"),
        "url": _bounded(source.get("url"), MAX_URL_CHARS) if source.get("url") else None,
        "title": _bounded(source.get("title"), MAX_TITLE_CHARS),
        "accessed_at": _bounded(source.get("accessed_at"), MAX_ACCESS_DATE_CHARS),
        "revision": source.get("revision"),
        "excerpts": excerpts,
    }


def get_source(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    class_id: int | None = None,
) -> dict[str, object]:
    """Read a source and its relied-on excerpts, optionally enforcing class scope."""
    sql = f"select {_SOURCE_COLUMNS} from writer_sources where id = ?"  # noqa: S608
    values: tuple[object, ...] = (source_id,)
    if class_id is not None:
        sql += " and class_id = ?"
        values = (source_id, class_id)
    row = conn.execute(sql, values).fetchone()
    if row is None:
        raise NotFoundError("That source does not exist.")
    return _decode_source(conn, row)


def list_sources(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    source_type: str | None = None,
) -> list[dict[str, object]]:
    """List prompt-safe ledger entries, with course readings before web pages.

    Full snapshots are audit material and are available only through ``get_source``.
    Keeping them out of this common list path makes every drafter/reviewer prompt safe
    by construction rather than relying on each caller to remember to delete a field.
    """
    classes.get_class(conn, class_id)
    if source_type is not None and source_type not in SOURCE_TYPES:
        raise ValueError(f"Unknown source type: {source_type}")
    sql = f"select {_PROMPT_SOURCE_COLUMNS} from writer_sources where class_id = ?"  # noqa: S608
    values: tuple[object, ...] = (class_id,)
    if source_type is not None:
        sql += " and source_type = ?"
        values = (class_id, source_type)
    sql += " order by case source_type when 'course' then 0 else 1 end, id"
    return [prompt_source(_decode_source(conn, row)) for row in conn.execute(sql, values)]


def _supports_exact_excerpt(conn: sqlite3.Connection, source_id: int, excerpt: str) -> bool:
    source = conn.execute(
        "select source_type, document_id, snapshot from writer_sources where id = ?",
        (source_id,),
    ).fetchone()
    if source is None:
        raise NotFoundError("That source does not exist.")
    # An existing relied-on passage stays auditable even if its course upload was later
    # deleted. This also lets replace_excerpts retain previously verified entries.
    existing = conn.execute(
        "select 1 from writer_source_excerpts where source_id = ? and excerpt = ? limit 1",
        (source_id, excerpt),
    ).fetchone()
    if existing is not None:
        return True
    if source["source_type"] == WEB:
        return excerpt in str(source["snapshot"] or "")
    document_id = source["document_id"]
    if document_id is None:
        return False
    row = conn.execute(
        "select 1 from chunks where document_id = ? and instr(content, ?) > 0 limit 1",
        (document_id, excerpt),
    ).fetchone()
    return row is not None


def _validated_excerpt(conn: sqlite3.Connection, source_id: int, excerpt: object) -> str:
    clean = str(excerpt).strip()
    if not clean:
        raise ValueError("Source excerpts cannot be empty")
    if len(clean) > MAX_RELIED_EXCERPT_CHARS:
        raise ValueError(f"A relied-on excerpt cannot exceed {MAX_RELIED_EXCERPT_CHARS} characters")
    if not _supports_exact_excerpt(conn, source_id, clean):
        raise ValueError("A relied-on excerpt must be an exact passage from its source")
    return clean


def _replace_excerpts(
    conn: sqlite3.Connection,
    source_id: int,
    excerpts: Sequence[str | Mapping[str, object]],
) -> None:
    revision_row = conn.execute(
        "select current_revision_id from writer_sources where id = ?", (source_id,)
    ).fetchone()
    revision_id = revision_row["current_revision_id"] if revision_row is not None else None
    payloads: list[tuple[int, int | None, str | None, str]] = []
    for item in excerpts:
        if isinstance(item, str):
            excerpt = item
            section_ref = None
        else:
            excerpt = item.get("excerpt", "")
            raw_ref = item.get("section_ref")
            section_ref = str(raw_ref).strip() if raw_ref is not None else None
            section_ref = section_ref or None
        if section_ref and len(section_ref) > MAX_SECTION_REF_CHARS:
            raise ValueError("Source excerpt section_ref is too long")
        payloads.append(
            (source_id, revision_id, section_ref, _validated_excerpt(conn, source_id, excerpt))
        )
    conn.execute("delete from writer_source_excerpts where source_id = ?", (source_id,))
    conn.executemany(
        "insert into writer_source_excerpts "
        "(source_id, source_revision_id, section_ref, excerpt) values (?, ?, ?, ?)",
        payloads,
    )


def upsert_source(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    source_type: str,
    title: str,
    document_id: int | None = None,
    url: str | None = None,
    accessed_at: str | None = None,
    snapshot: str | None = None,
    final_url: str | None = None,
    content_type: str | None = None,
    snapshot_hash: str | None = None,
    truncated: bool = False,
    excerpts: Sequence[str | Mapping[str, object]] | None = None,
    attempt_id: int | None = None,
    commit: bool = True,
) -> dict[str, object]:
    """Insert or refresh a course/web source and optionally replace its excerpts.

    Course identity is ``document_id``; web identity is the normalized URL. Passing
    ``snapshot=None`` preserves an existing snapshot while an explicit empty string
    clears it.

    When ``commit=False`` the caller owns the transaction boundary (PLA-310 atomicity).
    """
    classes.get_class(conn, class_id)
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Unknown source type: {source_type}")
    clean_title = title.strip()[:MAX_TITLE_CHARS]
    if not clean_title:
        raise ValueError("A source needs a title")
    normalized_url: str | None = None
    if source_type == COURSE:
        if document_id is None:
            raise ValueError("A course source needs a document_id")
        _require_document(conn, class_id, document_id)
        row = conn.execute(
            "select id from writer_sources "
            "where class_id = ? and source_type = 'course' and document_id = ?",
            (class_id, document_id),
        ).fetchone()
    else:
        if url is None:
            raise ValueError("A web source needs a URL")
        normalized_url = _normalize_url(url)
        if document_id is not None:
            raise ValueError("A web source cannot name a course document")
        row = conn.execute(
            "select id from writer_sources where class_id = ? and source_type = 'web' and url = ?",
            (class_id, normalized_url),
        ).fetchone()

    access_value = accessed_at or datetime.now(UTC).isoformat(timespec="seconds")
    if len(access_value) > MAX_ACCESS_DATE_CHARS:
        raise ValueError("Source access dates are too long")
    try:
        conn.execute("begin immediate")
        if row is None:
            cursor = conn.execute(
                "insert into writer_sources "
                "(class_id, source_type, document_id, url, title, accessed_at, snapshot) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (
                    class_id,
                    source_type,
                    document_id,
                    normalized_url,
                    clean_title,
                    access_value,
                    snapshot or "",
                ),
            )
            source_id = int(cursor.lastrowid or 0)
            agent_attempts.link_target(
                conn,
                attempt_id,
                target_kind="source",
                target_id=source_id,
            )
        else:
            source_id = int(row["id"])
            assignments = "title = ?, updated_at = datetime('now')"
            values: list[object] = [clean_title]
            # Registering a course document before each review is an idempotent lookup,
            # not a fresh access. Keeping its original date also keeps serial and parallel
            # prompts byte-for-byte identical when their runs cross a clock second.
            if accessed_at is not None or snapshot is not None:
                assignments += ", accessed_at = ?"
                values.append(access_value)
            if snapshot is not None:
                assignments += ", snapshot = ?"
                values.append(snapshot)
            conn.execute(
                f"update writer_sources set {assignments} where id = ?",  # noqa: S608
                (*values, source_id),
            )
        if source_type == WEB and snapshot is not None:
            clean_final_url = _normalize_url(final_url or normalized_url or "")
            digest = snapshot_hash or hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("snapshot_hash must be a lowercase SHA-256 digest")
            latest = conn.execute(
                "select id, snapshot_hash, final_url, truncated from writer_source_revisions "
                "where source_id = ? order by revision desc limit 1",
                (source_id,),
            ).fetchone()
            if (
                latest is not None
                and latest["snapshot_hash"] == digest
                and latest["final_url"] == clean_final_url
                and bool(latest["truncated"]) == bool(truncated)
            ):
                revision_id = int(latest["id"])
            else:
                next_revision = int(
                    conn.execute(
                        "select coalesce(max(revision), 0) + 1 from writer_source_revisions "
                        "where source_id = ?",
                        (source_id,),
                    ).fetchone()[0]
                )
                revision_cursor = conn.execute(
                    "insert into writer_source_revisions "
                    "(source_id, revision, final_url, content_type, snapshot, snapshot_hash, "
                    "truncated, accessed_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        next_revision,
                        clean_final_url,
                        content_type,
                        snapshot,
                        digest,
                        int(truncated),
                        access_value,
                    ),
                )
                revision_id = int(revision_cursor.lastrowid or 0)
                agent_attempts.link_target(
                    conn,
                    attempt_id,
                    target_kind="source_revision",
                    target_id=revision_id,
                )
            conn.execute(
                "update writer_sources set current_revision_id = ? where id = ?",
                (revision_id, source_id),
            )
        if excerpts is not None:
            _replace_excerpts(conn, source_id, excerpts)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_source(conn, source_id, class_id=class_id)


def add_excerpt(
    conn: sqlite3.Connection,
    source_id: int,
    excerpt: str,
    *,
    section_ref: str | None = None,
    attempt_id: int | None = None,
    commit: bool = True,
) -> dict[str, object]:
    """Record one passage actually relied on and return it.

    When ``commit=False`` the caller owns the transaction boundary (PLA-310 atomicity).
    """
    clean_excerpt = _validated_excerpt(conn, source_id, excerpt)
    clean_ref = section_ref.strip() if section_ref else None
    if clean_ref and len(clean_ref) > MAX_SECTION_REF_CHARS:
        raise ValueError("Source excerpt section_ref is too long")
    existing = conn.execute(
        "select id, source_id, source_revision_id, section_ref, excerpt, created_at "
        "from writer_source_excerpts "
        "where source_id = ? and section_ref is ? and excerpt = ? limit 1",
        (source_id, clean_ref, clean_excerpt),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    try:
        conn.execute("begin immediate")
        cursor = conn.execute(
            "insert into writer_source_excerpts "
            "(source_id, source_revision_id, section_ref, excerpt) "
            "select id, current_revision_id, ?, ? from writer_sources where id = ?",
            (clean_ref, clean_excerpt, source_id),
        )
        excerpt_id = int(cursor.lastrowid or 0)
        agent_attempts.link_target(
            conn,
            attempt_id,
            target_kind="source_excerpt",
            target_id=excerpt_id,
        )
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "select id, source_id, source_revision_id, section_ref, excerpt, created_at "
        "from writer_source_excerpts where id = ?",
        (excerpt_id,),
    ).fetchone()
    if row is None:  # pragma: no cover - same connection, row was inserted above.
        raise RuntimeError("The source excerpt disappeared after insertion.")
    return dict(row)


def replace_excerpts(
    conn: sqlite3.Connection,
    source_id: int,
    excerpts: Sequence[str | Mapping[str, object]],
) -> dict[str, object]:
    """Replace the auditable relied-on set while leaving the snapshot unchanged."""
    get_source(conn, source_id)
    try:
        conn.execute("begin immediate")
        _replace_excerpts(conn, source_id, excerpts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_source(conn, source_id)


def format_sources_for_prompt(sources: Sequence[Mapping[str, object]]) -> str:
    """Compact, deterministic ledger context for drafter and reviewer prompts."""
    blocks: list[str] = []
    for untrusted_source in sources:
        source = prompt_source(untrusted_source)
        label = f"[source:{source['id']}] {source['title']} ({source['source_type']})"
        location = source.get("url")
        if location:
            label += f"\nURL: {location}"
        excerpts = source.get("excerpts", [])
        lines = [label]
        if isinstance(excerpts, Sequence) and not isinstance(excerpts, (str, bytes)):
            for item in excerpts:
                if not isinstance(item, Mapping):
                    continue
                ref = item.get("section_ref")
                prefix = f"§{ref}: " if ref else ""
                lines.append(f"- {prefix}{item.get('excerpt', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# Writer-tool-friendly aliases.
upsert = upsert_source
list_for_class = list_sources
get = get_source
add_relied_on_excerpt = add_excerpt
