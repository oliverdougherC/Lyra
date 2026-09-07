"""Durable live structured draft suggestions, separate from pending edit review.

The writer can stage a structured suggestion block by block while a run is still active.
This module stores that live shape, lets the student patch individual blocks with CAS,
and finalizes the assembled markdown into the existing pending-edit review surface
without mutating the document body.
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from backend.core import artifacts, mathnorm
from backend.core.errors import ConflictError, NotFoundError

NOT_A_LIVE_SUGGESTION_MESSAGE = "That live suggestion does not exist."
NOT_A_LIVE_BLOCK_MESSAGE = "That live suggestion block does not exist."
BLOCK_RACE_MESSAGE = "That block changed since it was fetched. Re-fetch the live suggestion."
STAGES = (
    "gathering",
    "outlining",
    "drafting",
    "transitions",
    "reviewing",
    "finalizing",
    "completed",
)

_UNSET = object()


def _require_draft(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError("That draft does not exist.")
    return artifact


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise NotFoundError("This draft has no body.")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _validate_stage(stage: str) -> str:
    if stage and stage not in STAGES:
        raise ValueError(f"Unknown live suggestion stage: {stage}")
    return stage


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: object) -> object:
    if value in (None, ""):
        return {}
    return json.loads(str(value))


def _suggestion_row(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row:
    row = conn.execute(
        "select * from live_draft_suggestions where id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(NOT_A_LIVE_SUGGESTION_MESSAGE)
    return row


def _require_model_ownership(conn: sqlite3.Connection, suggestion_id: int) -> None:
    """Fence model effects under the caller's SQLite writer lock.

    Legacy suggestions without a durable run retain their old storage contract.
    HTTP suggestions always have a run, whose cancellation/terminal commit wins over
    any later callback, including one arriving after a successor has started.
    """
    run = conn.execute(
        "select r.status, r.id, r.artifact_id from writer_runs r "
        "join live_draft_suggestions s on s.run_id = r.id where s.id = ?",
        (suggestion_id,),
    ).fetchone()
    if run is not None and (
        run["status"] not in ("queued", "running")
        or conn.execute(
            "select 1 from writer_runs where artifact_id = ? and id > ?",
            (run["artifact_id"], run["id"]),
        ).fetchone()
    ):
        raise ConflictError("This writing run no longer owns the suggestion. Saved text was kept.")


def _block_row(conn: sqlite3.Connection, block_id: int) -> sqlite3.Row:
    row = conn.execute("select * from live_draft_blocks where id = ?", (block_id,)).fetchone()
    if row is None:
        raise NotFoundError(NOT_A_LIVE_BLOCK_MESSAGE)
    return row


def _block_row_for_key(
    conn: sqlite3.Connection, suggestion_id: int, stable_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "select * from live_draft_blocks where suggestion_id = ? and stable_key = ?",
        (suggestion_id, stable_key),
    ).fetchone()


def _block_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "suggestion_id": int(row["suggestion_id"]),
        "stable_key": str(row["stable_key"]),
        "block_key": str(row["stable_key"]),
        "section_ref": row["section_ref"],
        "paragraph_ordinal": int(row["paragraph_ordinal"]),
        "ordinal": int(row["paragraph_ordinal"]),
        "kind": str(row["kind"]),
        "heading": row["heading"],
        "content": str(row["content"]),
        "status": str(row["status"]),
        "target_words": row["target_words"],
        "summary": row["summary"],
        "context": _decode_json(row["context_json"]),
        "metadata": _decode_json(row["metadata_json"]),
        "revision": int(row["revision"]),
        "user_revision": int(row["user_revision"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _blocks_for_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        "select * from live_draft_blocks where suggestion_id = ? order by paragraph_ordinal, id",
        (suggestion_id,),
    ).fetchall()
    return [_block_dict(row) for row in rows]


def _suggestion_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "artifact_id": int(row["artifact_id"]),
        "run_id": int(row["run_id"]),
        "stage": str(row["stage"]),
        "status": str(row["status"]),
        "detail": row["detail"],
        "stage_detail": row["detail"],
        "version": int(row["version"]),
        "base_content": str(row["base_content"]),
        "base_hash": str(row["base_hash"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "blocks": _blocks_for_suggestion(conn, int(row["id"])),
    }


def _touch_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> None:
    conn.execute(
        "update live_draft_suggestions set updated_at = datetime('now') where id = ?",
        (suggestion_id,),
    )


def create_live_suggestion(
    conn: sqlite3.Connection,
    artifact_id: int,
    run_id: int,
    *,
    stage: str = "",
    status: str = "pending",
    detail: str | None = None,
    version: int = 1,
    base_content: str | None = None,
    base_hash: str | None = None,
) -> dict[str, object]:
    """Create or refresh the one live suggestion row for an artifact/run pair."""
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    base = str(part["content"]) if base_content is None else base_content
    observed_hash = _sha256(base) if base_hash is None else base_hash
    _validate_stage(stage)

    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select id from live_draft_suggestions where artifact_id = ? and run_id = ?",
            (artifact_id, run_id),
        ).fetchone()
        if row is None:
            suggestion_id = int(
                conn.execute(
                    "insert into live_draft_suggestions "
                    "(artifact_id, run_id, stage, status, detail, version, "
                    "base_content, base_hash) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (artifact_id, run_id, stage, status, detail, version, base, observed_hash),
                ).lastrowid
                or 0
            )
        else:
            suggestion_id = int(row["id"])
            conn.execute(
                "update live_draft_suggestions set stage = ?, status = ?, detail = ?, "
                "version = ?, base_content = ?, base_hash = ?, updated_at = datetime('now') "
                "where id = ?",
                (stage, status, detail, version, base, observed_hash, suggestion_id),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return get_live_suggestion(conn, suggestion_id)


def get_live_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> dict[str, object]:
    return _suggestion_dict(conn, _suggestion_row(conn, suggestion_id))


def get_live_suggestion_for_run(conn: sqlite3.Connection, run_id: int) -> dict[str, object] | None:
    row = conn.execute(
        "select * from live_draft_suggestions where run_id = ? order by id desc limit 1",
        (run_id,),
    ).fetchone()
    return _suggestion_dict(conn, row) if row is not None else None


def get_latest_live_suggestion(
    conn: sqlite3.Connection, artifact_id: int
) -> dict[str, object] | None:
    _require_draft(conn, artifact_id)
    row = conn.execute(
        "select * from live_draft_suggestions where artifact_id = ? order by id desc limit 1",
        (artifact_id,),
    ).fetchone()
    return _suggestion_dict(conn, row) if row is not None else None


def update_live_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: int,
    *,
    stage: str | None = None,
    status: str | None = None,
    detail: str | None = None,
    version_bump: int = 1,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Update suggestion metadata and bump its version.

    `metadata` is merged into every block's `metadata` payload, which gives the pipeline
    a simple way to stamp shared run details across the seeded outline.
    """
    if stage is not None:
        _validate_stage(stage)
    try:
        conn.execute("begin immediate")
        if status not in {"cancelled", "failed"}:
            _require_model_ownership(conn, suggestion_id)
        current = get_live_suggestion(conn, suggestion_id)
        next_version = int(current["version"]) + max(0, version_bump)
        conn.execute(
            "update live_draft_suggestions set stage = ?, status = ?, detail = ?, "
            "version = ?, updated_at = datetime('now') where id = ?",
            (
                str(current["stage"]) if stage is None else stage,
                str(current["status"]) if status is None else status,
                current["detail"] if detail is None else detail,
                next_version,
                suggestion_id,
            ),
        )
        if metadata is not None:
            for block in _blocks_for_suggestion(conn, suggestion_id):
                merged = dict(block["metadata"]) if isinstance(block["metadata"], dict) else {}
                merged.update(metadata)
                conn.execute(
                    "update live_draft_blocks set metadata_json = ?, updated_at = datetime('now') "
                    "where id = ?",
                    (_json(merged), int(block["id"])),
                )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return get_live_suggestion(conn, suggestion_id)


def _strict_model_merge(current: str, proposed: str, *, user_revision: int) -> str:
    if user_revision <= 0:
        return proposed
    if proposed.startswith(current) and len(proposed) > len(current):
        return proposed
    if proposed == current:
        return current
    return current


def _coerce_json(value: object, fallback: object) -> object:
    return fallback if value is _UNSET else value


def _insert_block(
    conn: sqlite3.Connection,
    suggestion_id: int,
    stable_key: str,
    *,
    section_ref: str | None,
    paragraph_ordinal: int,
    kind: str,
    heading: str | None,
    content: str,
    status: str,
    target_words: int | None,
    summary: str | None,
    context: object,
    metadata: object,
    revision: int = 1,
    user_revision: int = 0,
) -> dict[str, object]:
    block_id = int(
        conn.execute(
            "insert into live_draft_blocks "
            "(suggestion_id, stable_key, section_ref, paragraph_ordinal, kind, heading, content, "
            "status, target_words, summary, context_json, metadata_json, revision, user_revision) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                suggestion_id,
                stable_key,
                section_ref,
                paragraph_ordinal,
                kind,
                heading,
                content,
                status,
                target_words,
                summary,
                _json(context),
                _json(metadata),
                revision,
                user_revision,
            ),
        ).lastrowid
        or 0
    )
    return _block_dict(_block_row(conn, block_id))


def _update_block_row(
    conn: sqlite3.Connection,
    current: dict[str, object],
    *,
    section_ref: object = _UNSET,
    paragraph_ordinal: object = _UNSET,
    kind: object = _UNSET,
    heading: object = _UNSET,
    content: object = _UNSET,
    status: object = _UNSET,
    target_words: object = _UNSET,
    summary: object = _UNSET,
    context: object = _UNSET,
    metadata: object = _UNSET,
    user_edit: bool = False,
) -> dict[str, object]:
    next_values = {
        "section_ref": current["section_ref"] if section_ref is _UNSET else section_ref,
        "paragraph_ordinal": (
            current["paragraph_ordinal"] if paragraph_ordinal is _UNSET else paragraph_ordinal
        ),
        "kind": current["kind"] if kind is _UNSET else kind,
        "heading": current["heading"] if heading is _UNSET else heading,
        "content": current["content"] if content is _UNSET else content,
        "status": current["status"] if status is _UNSET else status,
        "target_words": current["target_words"] if target_words is _UNSET else target_words,
        "summary": current["summary"] if summary is _UNSET else summary,
        "context": current["context"] if context is _UNSET else context,
        "metadata": current["metadata"] if metadata is _UNSET else metadata,
    }
    changed = any(next_values[key] != current[key] for key in next_values)
    if not changed:
        return current

    next_revision = int(current["revision"]) + 1
    next_user_revision = next_revision if user_edit else int(current["user_revision"])
    conn.execute(
        "update live_draft_blocks set section_ref = ?, paragraph_ordinal = ?, kind = ?, "
        "heading = ?, content = ?, status = ?, target_words = ?, summary = ?, context_json = ?, "
        "metadata_json = ?, revision = ?, user_revision = ?, updated_at = datetime('now') "
        "where id = ?",
        (
            next_values["section_ref"],
            next_values["paragraph_ordinal"],
            next_values["kind"],
            next_values["heading"],
            next_values["content"],
            next_values["status"],
            next_values["target_words"],
            next_values["summary"],
            _json(next_values["context"]),
            _json(next_values["metadata"]),
            next_revision,
            next_user_revision,
            int(current["id"]),
        ),
    )
    _touch_suggestion(conn, int(current["suggestion_id"]))
    return _block_dict(_block_row(conn, int(current["id"])))


def model_update_block(
    conn: sqlite3.Connection,
    suggestion_id: int,
    stable_key: str,
    *,
    section_ref: str | None = None,
    paragraph_ordinal: int | None = None,
    kind: str = "paragraph",
    heading: str | None = None,
    content: str | None = None,
    status: str = "pending",
    target_words: int | None = None,
    summary: str | None = None,
    context: object = _UNSET,
    metadata: object = _UNSET,
) -> dict[str, object]:
    """Create or replace a model-owned block, preserving user-edited content."""
    prepared_context = _coerce_json(context, {})
    prepared_metadata = _coerce_json(metadata, {})
    try:
        # Every block mutation takes the SQLite writer lock before reading. That makes
        # the read/merge/write one atomic operation relative to user PATCH requests and
        # streamed appends from another connection.
        conn.execute("begin immediate")
        _suggestion_row(conn, suggestion_id)
        _require_model_ownership(conn, suggestion_id)
        current_row = _block_row_for_key(conn, suggestion_id, stable_key)
        if current_row is None:
            block = _insert_block(
                conn,
                suggestion_id,
                stable_key,
                section_ref=section_ref,
                paragraph_ordinal=paragraph_ordinal or 0,
                kind=kind,
                heading=heading,
                content=content or "",
                status=status,
                target_words=target_words,
                summary=summary,
                context=prepared_context,
                metadata=prepared_metadata,
            )
        else:
            current = _block_dict(current_row)
            next_content = current["content"]
            if content is not None:
                next_content = _strict_model_merge(
                    str(current["content"]), content, user_revision=int(current["user_revision"])
                )
            block = _update_block_row(
                conn,
                current,
                section_ref=section_ref if section_ref is not None else _UNSET,
                paragraph_ordinal=paragraph_ordinal if paragraph_ordinal is not None else _UNSET,
                kind=kind,
                heading=heading if heading is not None else _UNSET,
                content=next_content,
                status=status,
                target_words=target_words if target_words is not None else _UNSET,
                summary=summary if summary is not None else _UNSET,
                context=prepared_context if context is not _UNSET else _UNSET,
                metadata=prepared_metadata if metadata is not _UNSET else _UNSET,
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return block


def append_block_text(
    conn: sqlite3.Connection,
    suggestion_id: int,
    stable_key: str,
    text: str,
    *,
    section_ref: str | None = None,
    paragraph_ordinal: int | None = None,
    kind: str = "paragraph",
    heading: str | None = None,
    status: str | None = None,
    target_words: int | None = None,
    summary: str | None = None,
    context: object = _UNSET,
    metadata: object = _UNSET,
) -> dict[str, object]:
    """Append streamed text to a block without rewriting its current prefix."""
    prepared_context = _coerce_json(context, {})
    prepared_metadata = _coerce_json(metadata, {})
    try:
        conn.execute("begin immediate")
        _suggestion_row(conn, suggestion_id)
        _require_model_ownership(conn, suggestion_id)
        current_row = _block_row_for_key(conn, suggestion_id, stable_key)
        if current_row is None:
            block = _insert_block(
                conn,
                suggestion_id,
                stable_key,
                section_ref=section_ref,
                paragraph_ordinal=paragraph_ordinal or 0,
                kind=kind,
                heading=heading,
                content=text,
                status=status or "pending",
                target_words=target_words,
                summary=summary,
                context=prepared_context,
                metadata=prepared_metadata,
            )
        else:
            current = _block_dict(current_row)
            block = _update_block_row(
                conn,
                current,
                section_ref=section_ref if section_ref is not None else _UNSET,
                paragraph_ordinal=(paragraph_ordinal if paragraph_ordinal is not None else _UNSET),
                kind=kind,
                heading=heading if heading is not None else _UNSET,
                content=str(current["content"]) + text,
                status=status if status is not None else _UNSET,
                target_words=target_words if target_words is not None else _UNSET,
                summary=summary if summary is not None else _UNSET,
                context=prepared_context if context is not _UNSET else _UNSET,
                metadata=prepared_metadata if metadata is not _UNSET else _UNSET,
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return block


def patch_block(
    conn: sqlite3.Connection,
    block_id: int,
    *,
    expected_revision: int,
    base_content: str | None = None,
    section_ref: str | None = None,
    paragraph_ordinal: int | None = None,
    kind: str | None = None,
    heading: str | None = None,
    content: str | None = None,
    status: str | None = None,
    target_words: int | None = None,
    summary: str | None = None,
    context: object = _UNSET,
    metadata: object = _UNSET,
) -> dict[str, object]:
    """Apply a user edit and preserve model text appended while they were typing.

    Without ``base_content`` this is a strict CAS update. The live editor includes the
    content it started from, which lets us merge a concurrently streamed suffix onto the
    student's replacement. Any non-append model rewrite is still a conflict.
    """
    try:
        conn.execute("begin immediate")
        current = _block_dict(_block_row(conn, block_id))
        next_content: object = content if content is not None else _UNSET
        revision_changed = int(current["revision"]) != expected_revision
        if revision_changed:
            if base_content is None or content is None:
                raise ConflictError(BLOCK_RACE_MESSAGE)
            current_content = str(current["content"])
            if current_content == base_content:
                next_content = content
            elif current_content.startswith(base_content):
                next_content = content + current_content[len(base_content) :]
            else:
                raise ConflictError(BLOCK_RACE_MESSAGE)
        prepared_context = _coerce_json(context, current["context"])
        prepared_metadata = _coerce_json(metadata, current["metadata"])
        updated = _update_block_row(
            conn,
            current,
            section_ref=section_ref if section_ref is not None else _UNSET,
            paragraph_ordinal=paragraph_ordinal if paragraph_ordinal is not None else _UNSET,
            kind=kind if kind is not None else _UNSET,
            heading=heading if heading is not None else _UNSET,
            content=next_content,
            status=status if status is not None else _UNSET,
            target_words=target_words if target_words is not None else _UNSET,
            summary=summary if summary is not None else _UNSET,
            context=prepared_context if context is not _UNSET else _UNSET,
            metadata=prepared_metadata if metadata is not _UNSET else _UNSET,
            user_edit=True,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return updated


def assemble_markdown(conn: sqlite3.Connection, suggestion_id: int) -> str:
    """Render the live block list into deterministic markdown."""
    _suggestion_row(conn, suggestion_id)
    blocks = _blocks_for_suggestion(conn, suggestion_id)
    parts: list[str] = []
    current_heading_key: tuple[str, str] | None = None
    for block in blocks:
        kind = str(block["kind"])
        heading = str(block["heading"] or "").strip()
        section_ref = str(block["section_ref"] or "").strip()
        content = str(block["content"]).strip()
        if kind == "heading":
            line = content or heading
            if line:
                parts.append(line if line.startswith("#") else f"## {line}")
            current_heading_key = (section_ref, heading)
            continue
        heading_key = (section_ref, heading)
        if heading and heading_key != current_heading_key:
            parts.append(f"## {heading}")
            current_heading_key = heading_key
        elif heading:
            current_heading_key = heading_key
        if content:
            parts.append(content)
    if not parts:
        return ""
    return mathnorm.normalize("\n\n".join(parts).rstrip() + "\n")


def finalize_to_pending_edit(
    conn: sqlite3.Connection,
    suggestion_id: int,
    *,
    note: str | None = None,
    model_owned: bool = False,
) -> dict[str, object] | None:
    """Create or update the pending edit row from the live suggestion, without writing.

    The pending edit is anchored to the live suggestion's original base, not whatever the
    draft body happens to say now. The existing review flow may later rebase or mark it
    stale on read.
    """
    try:
        conn.execute("begin immediate")
        if model_owned:
            _require_model_ownership(conn, suggestion_id)
        suggestion = get_live_suggestion(conn, suggestion_id)
        artifact_id = int(suggestion["artifact_id"])
        part = _body_part(conn, artifact_id)
        proposed = assemble_markdown(conn, suggestion_id)
        if proposed == str(suggestion["base_content"]):
            # A no-op must not erase an unrelated proposal.
            conn.commit()
            return None
        stale = int(_sha256(str(part["content"])) != str(suggestion["base_hash"]))
        existing = conn.execute(
            "select id from pending_edits where part_id = ?",
            (int(part["id"]),),
        ).fetchone()
        if existing is None:
            edit_id = int(
                conn.execute(
                    "insert into pending_edits "
                    "(part_id, base_content, base_hash, proposed_content, stale, note) "
                    "values (?, ?, ?, ?, ?, ?)",
                    (
                        int(part["id"]),
                        str(suggestion["base_content"]),
                        str(suggestion["base_hash"]),
                        proposed,
                        stale,
                        note,
                    ),
                ).lastrowid
                or 0
            )
        else:
            edit_id = int(existing["id"])
            conn.execute(
                "update pending_edits set base_content = ?, base_hash = ?, proposed_content = ?, "
                "stale = ?, note = ?, updated_at = datetime('now') where id = ?",
                (
                    str(suggestion["base_content"]),
                    str(suggestion["base_hash"]),
                    proposed,
                    stale,
                    note,
                    edit_id,
                ),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    row = conn.execute("select * from pending_edits where id = ?", (edit_id,)).fetchone()
    return dict(row) if row is not None else None


def block_belongs_to_artifact(conn: sqlite3.Connection, artifact_id: int, block_id: int) -> bool:
    row = conn.execute(
        "select 1 from live_draft_blocks b join live_draft_suggestions s on s.id = b.suggestion_id "
        "where b.id = ? and s.artifact_id = ?",
        (block_id, artifact_id),
    ).fetchone()
    return row is not None
