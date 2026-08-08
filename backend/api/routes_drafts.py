"""Draft endpoints: the writing workspace.

A draft is an artifact with exactly one body part, so revisions, provenance, and
step-scoped chat all work unchanged. Creation is synchronous (a draft is born empty and
`ready`; there is no job to queue). The AI surfaces are `/write` - a streamed inline
passage, stateless, which lives in the client until accepted - `/pass` - the queued
draft pipeline, structure then sections, landing direct only on empty sections and
otherwise as one pending edit reviewed hunk by hunk - `/review` - the queued four-lens
review, filing margin comments - and the writer conversation. `/export` typesets the
document to PDF through Pandoc and Typst.

Handlers are sync `def` (sqlite3 blocks; FastAPI threadpools them), except `/write`,
which is `async def` because the turn is driven by awaited httpx reads.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.api.routes_chat import plan_budget, trim_history
from backend.core import (
    artifacts,
    briefs,
    comments,
    exporting,
    review_pipeline,
    sections,
    sessions,
    source_ledger,
    suggestions,
    writer_intent,
    writer_pipeline,
    writer_runs,
    writer_tools,
)
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.classes import get_class, touch_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.core.writer_budgets import Depth, validate_depth
from backend.llm import client, prompts
from backend.llm.tools import RecordedCall, ToolDefinition, ToolLoopResult, run_tool_loop
from backend.rag.retrieve import retrieve
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

NOT_A_DRAFT_MESSAGE = "That draft does not exist."
NOT_A_SUGGESTION_MESSAGE = "That suggestion does not exist."
NO_DOCUMENT_MESSAGE = "This draft has no body."
ALREADY_RUNNING_MESSAGE = "This draft already has a run in flight."

# The stage details a queued job wears until its worker picks it up. The review's has to
# keep the "Reviewing" prefix from its first moment: that prefix is the whole contract
# telling the workspace a review never writes the document, so the student keeps the pen
# while one runs (`core/review_pipeline` module docstring, `page.tsx` reviewRunning).
QUEUED_PASS_DETAIL = "Queued"  # noqa: S105 - a stage banner, not a credential.
QUEUED_REVIEW_DETAIL = "Reviewing (queued)"
PASS_JOB_KIND = "pass"  # noqa: S105 - a job kind, not a credential.
REVIEW_JOB_KIND = "review"

WRITE_RETRIEVAL_BUDGET = 2_000

BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then write.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and writing has to send it your "
        "course material. Allow that in Settings, then write."
    ),
}

_WRITE_ERROR_MESSAGE = "Something went wrong while writing. Try again."

SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


class DraftCreate(BaseModel):
    """Body of `POST /api/classes/{class_id}/drafts`."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A draft name cannot be blank.")
        return cleaned


class DraftRename(BaseModel):
    """Body of `PATCH /api/drafts/{artifact_id}`."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A draft name cannot be blank.")
        return cleaned


class BodyUpdate(BaseModel):
    """Body of `PATCH /api/drafts/{artifact_id}/body` - the autosave path.

    Default writes no revision: a debounced autosave every 1.5 seconds would bury the
    meaningful ones. `snapshot: true` is the explicit history point.
    """

    content: str
    snapshot: bool = False
    note: str | None = None


class WriteRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/write`.

    `heading`, `selection`, and `nearby` are what the editor gathered around the caret;
    whichever exist ground the passage where it will land.
    """

    instruction: str = Field(min_length=1)
    heading: str | None = None
    selection: str | None = None
    nearby: str | None = None

    @field_validator("instruction")
    @classmethod
    def _check_instruction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("An instruction cannot be blank.")
        return cleaned


class PassRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/pass`.

    Everything is optional on purpose: an empty body is the full draft pass, an
    instruction is a lens over it, and `sections` filters it to named sections.
    """

    instruction: str | None = None
    sections: list[str] = Field(default_factory=list)
    depth: Depth = "quick"
    pause_at_plan: bool = False
    address_comment_id: int | None = Field(default=None, ge=1)

    @field_validator("instruction")
    @classmethod
    def _clean_instruction(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    @field_validator("sections")
    @classmethod
    def _clean_sections(cls, value: list[str]) -> list[str]:
        return [ref.strip() for ref in value if ref.strip()]


class HunkRef(BaseModel):
    """The `{index, hash}` echo that pins a hunk against races."""

    index: int
    hash: str


class AcceptRequest(BaseModel):
    """Body of `POST /api/pending-edits/{edit_id}/accept`."""

    hunk: HunkRef | None = None
    force: bool = False


class RejectRequest(BaseModel):
    """Body of `POST /api/pending-edits/{edit_id}/reject`."""

    hunk: HunkRef | None = None


def _require_draft(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The artifact, when it is a draft. 404 either way otherwise."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError(NOT_A_DRAFT_MESSAGE)
    return artifact


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise NotFoundError(NO_DOCUMENT_MESSAGE)


def _begin_run(
    conn: sqlite3.Connection,
    artifact_id: int,
    stage_detail: str,
    job_kind: str,
    depth: Depth,
    started_at: str,
    request_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Mark a queued draft job pending *here*, in the request, before it is enqueued.

    The workspace polls `/status` and stops the moment the artifact is neither pending
    nor generating. A job that only leaves `ready` when the worker picks it up therefore
    races that first poll and usually loses it: the poll sees `ready`, stops for good,
    and the run becomes invisible - findings landing into a Comments tab that will never
    refetch. Every other pipeline marks its artifact pending inside the request for this
    reason (`routes_solutions.start_solve`); these two did not, which is why the review
    that filed four comments looked like a button that does nothing.

    Marking here also makes the wait visible: both residents share one worker thread, so
    a job queued behind another sits in `pending` with its own stage detail rather than
    looking like nothing happened.

    Raises:
        ConflictError: when a run is already in flight on this draft.
    """
    try:
        # A reserved write lock plus the conditional update makes ready -> pending a
        # single claim. A concurrent HTTP request or chat tool waits for this commit,
        # then observes rowcount zero instead of enqueueing a second job.
        conn.execute("begin immediate")
        cursor = conn.execute(
            "update artifacts set state = ?, stage_detail = ?, error_message = null, "
            "writer_job_kind = ?, writer_job_depth = ?, writer_job_started_at = ?, "
            "writer_job_completed_at = null, updated_at = datetime('now') "
            "where id = ? and kind = ? and state not in (?, ?)",
            (
                artifacts.PENDING,
                stage_detail,
                job_kind,
                depth,
                started_at,
                artifact_id,
                artifacts.KIND_DRAFT,
                artifacts.PENDING,
                artifacts.GENERATING,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "select kind, state from artifacts where id = ?", (artifact_id,)
            ).fetchone()
            conn.rollback()
            if row is None or row["kind"] != artifacts.KIND_DRAFT:
                raise NotFoundError(NOT_A_DRAFT_MESSAGE)
            raise ConflictError(ALREADY_RUNNING_MESSAGE)
        writer_runs.create_run(
            conn,
            artifact_id,
            job_kind,
            depth,
            request=request_payload,
            started_at=started_at,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return artifacts.get_artifact(conn, artifact_id)


def begin_writer_run(
    conn: sqlite3.Connection,
    artifact_id: int,
    job_kind: str,
    depth: str,
    *,
    request_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Atomically expose a queued writer job before either HTTP or chat enqueues it.

    This is exported for the writer tools so tool-started jobs obey the same busy guard
    and polling contract as button-started jobs.
    """
    chosen_depth = validate_depth(depth)
    if job_kind == PASS_JOB_KIND:
        detail = QUEUED_PASS_DETAIL
    elif job_kind == REVIEW_JOB_KIND:
        detail = QUEUED_REVIEW_DETAIL
    else:
        raise ValueError(f"Unknown writer job kind: {job_kind}")
    started_at = datetime.now(UTC).isoformat()
    return _begin_run(
        conn,
        artifact_id,
        detail,
        job_kind,
        chosen_depth,
        started_at,
        request_payload=request_payload,
    )


@router.post(
    "/classes/{class_id}/drafts",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
def create_draft(class_id: int, payload: DraftCreate, conn: DbConn) -> dict[str, object]:
    """A draft is born empty and ready; the first AI pass comes through `/pass`."""
    get_class(conn, class_id)
    created = artifacts.create_artifact(
        conn, class_id, payload.title, [], kind=artifacts.KIND_DRAFT
    )
    artifacts.create_part(
        conn,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content="",
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(conn, int(created["id"]), artifacts.READY)
    touch_class(conn, class_id)
    return artifacts.get_artifact(conn, int(created["id"]))


@router.get("/classes/{class_id}/drafts", response_model=None)
def list_drafts(class_id: int, conn: DbConn) -> list[dict[str, object]]:
    """Every draft in the class, most recently worked on first."""
    get_class(conn, class_id)
    rows = conn.execute(
        "select id, class_id, kind, title, state, stage_detail, problems_total, "
        "problems_done, error_message, created_at, updated_at from artifacts "
        "where class_id = ? and kind = ? order by updated_at desc",
        (class_id, artifacts.KIND_DRAFT),
    ).fetchall()
    return [dict(row) for row in rows]


@router.get("/drafts/{artifact_id}", response_model=None)
def read_draft(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """The artifact, its body, and whether a suggestion is pending review."""
    artifact = _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    pending = suggestions.pending_for_part(conn, int(part["id"]))
    return {
        **artifact,
        "part_id": part["id"],
        "body": part["content"],
        "pending": pending is not None,
    }


@router.patch("/drafts/{artifact_id}", response_model=None)
def rename_draft(artifact_id: int, payload: DraftRename, conn: DbConn) -> dict[str, object]:
    _require_draft(conn, artifact_id)
    return artifacts.rename_artifact(conn, artifact_id, payload.title)


@router.delete("/drafts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft(artifact_id: int, conn: DbConn) -> None:
    _require_draft(conn, artifact_id)
    artifacts.delete_artifact(conn, artifact_id)


@router.patch("/drafts/{artifact_id}/body", response_model=None)
def update_body(artifact_id: int, payload: BodyUpdate, conn: DbConn) -> dict[str, object]:
    """The autosave path. See `BodyUpdate` for the revision rule."""
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    artifacts.set_part_content(
        conn,
        int(part["id"]),
        payload.content,
        origin=artifacts.USER_CORRECTED,
        note=(payload.note or "snapshot") if payload.snapshot else None,
        record_revision=payload.snapshot,
    )
    return {"part_id": part["id"], "saved": True}


@router.post("/drafts/{artifact_id}/write")
async def write_inline(artifact_id: int, payload: WriteRequest, conn: DbConn) -> StreamingResponse:
    """Stream one drafted passage. Stateless: nothing here touches the document."""
    config, messages = await asyncio.to_thread(_open_write, conn, artifact_id, payload)
    return StreamingResponse(
        _stream_write(config, messages),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _open_write(
    conn: sqlite3.Connection, artifact_id: int, payload: WriteRequest
) -> tuple[TutorConfig, list[dict[str, str]]]:
    """Everything fallible, before the first byte: guards, grounding, and the prompt."""
    artifact = _require_draft(conn, artifact_id)
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)
    class_id = int(artifact["class_id"])

    query = payload.instruction + " " + (payload.selection or payload.heading or "")
    result = retrieve(conn, class_id, query, WRITE_RETRIEVAL_BUDGET)
    context_block = prompts.format_context_block([vars(chunk) for chunk in result.chunks])
    facts_block = prompts.format_facts_block(select_active_facts(conn, class_id))
    brief_block = prompts.format_brief_block(briefs.get_brief(conn, artifact_id))
    messages = prompts.build_write_prompt(
        payload.instruction,
        payload.heading,
        payload.selection,
        payload.nearby,
        context_block,
        facts_block,
        brief_block,
    )
    return config, messages


async def _stream_write(config: TutorConfig, messages: list[dict[str, str]]) -> AsyncIterator[str]:
    """Token frames for the answer channel, then done. Errors arrive as frames."""
    try:
        async for delta in client.stream_chat(
            config.endpoint_url, config.api_key, config.model, messages
        ):
            if delta.channel == "answer":
                yield _frame(type="token", text=delta.text)
    except LyraError as exc:
        yield _frame(type="error", message=exc.message)
        return
    except Exception:
        logger.exception("Write stream failed")
        yield _frame(type="error", message=_WRITE_ERROR_MESSAGE)
        return
    yield _frame(type="done")


def _frame(**payload: object) -> str:
    """One SSE frame: a single JSON object on a `data:` line, then the blank line."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post(
    "/drafts/{artifact_id}/pass",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_pass(artifact_id: int, payload: PassRequest, conn: DbConn) -> dict[str, object]:
    """Queue a draft pass: the full draft with no body, or an instruction-lens pass.

    This replaced `/suggest`'s one-shot whole-document call: the pipeline's
    section-scoped stages are the same student-visible contract - direct writes only
    into empty sections, everything else one reviewable pending edit - at a size that
    does not stop working when the document grows.
    """
    if payload.address_comment_id is not None:
        _require_comment_on_draft(conn, payload.address_comment_id, artifact_id)
    queued = begin_writer_run(
        conn,
        artifact_id,
        PASS_JOB_KIND,
        payload.depth,
        request_payload={
            "instruction": payload.instruction,
            "section_refs": [*payload.sections],
            "pause_at_plan": payload.pause_at_plan,
            "address_comment_id": payload.address_comment_id,
        },
    )
    run = writer_runs.active_run(conn, artifact_id)
    writer_pipeline.enqueue(
        writer_tools._compatible_job(
            writer_pipeline.PassJob,
            artifact_id,
            instruction=payload.instruction,
            section_refs=tuple(payload.sections),
            depth=payload.depth,
            pause_at_plan=payload.pause_at_plan,
            address_comment_id=payload.address_comment_id,
            run_id=int(run["id"]) if run is not None else None,
        )
    )
    return queued


class ReviewRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/review`."""

    depth: Depth = "quick"


@router.post(
    "/drafts/{artifact_id}/review",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_review(
    artifact_id: int, conn: DbConn, payload: ReviewRequest | None = None
) -> dict[str, object]:
    """Queue the review pass: four lenses, findings filed as margin comments.

    The review never writes the document, so the workspace leaves the editor live
    while it runs; the status poll shows the lens in flight, and the Comments tab
    fills as findings land.
    """
    request = payload or ReviewRequest()
    queued = begin_writer_run(
        conn,
        artifact_id,
        REVIEW_JOB_KIND,
        request.depth,
        request_payload={},
    )
    run = writer_runs.active_run(conn, artifact_id)
    review_pipeline.enqueue(
        writer_tools._compatible_job(
            review_pipeline.ReviewJob,
            artifact_id,
            depth=request.depth,
            run_id=int(run["id"]) if run is not None else None,
        )
    )
    return queued


@router.post("/drafts/{artifact_id}/cancel", response_model=None)
def cancel_draft_run(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Request cancellation of the active writer run at its next safe boundary."""
    _require_draft(conn, artifact_id)
    run = writer_runs.request_cancel(conn, artifact_id)
    if run is None:
        raise ConflictError("This draft does not have a run in flight.")
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["state"] in (artifacts.PENDING, artifacts.GENERATING):
        conn.execute(
            "update artifacts set stage_detail = ?, updated_at = datetime('now') where id = ?",
            ("Cancelling after the current step", artifact_id),
        )
        conn.commit()
    return draft_status(artifact_id, conn)


@router.get("/drafts/{artifact_id}/comments", response_model=None)
def list_comments(artifact_id: int, conn: DbConn) -> list[dict[str, object]]:
    """Every comment thread on the draft, anchored against the body as it stands.

    Resolved and orphaned threads ride along flagged rather than filtered: the rail
    decides what to show, and an orphaned finding is still a finding.
    """
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    return comments.list_threads(conn, int(part["id"]), str(part["content"]))


class ReplyWrite(BaseModel):
    """Body of `POST /api/comments/{comment_id}/replies` - the student's reply."""

    body: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A reply cannot be blank.")
        return cleaned


class ResolveWrite(BaseModel):
    """Body of `POST /api/comments/{comment_id}/resolve`: resolve, or reopen."""

    resolved: bool = True


NOT_A_COMMENT_MESSAGE = "That comment does not exist."


def _require_draft_comment(conn: sqlite3.Connection, comment_id: int) -> None:
    """404 unless the comment sits on a draft's body, mirroring the pending-edit guard."""
    row = conn.execute("select part_id from draft_comments where id = ?", (comment_id,)).fetchone()
    if row is None:
        raise NotFoundError(NOT_A_COMMENT_MESSAGE)
    part = artifacts.get_part(conn, int(row["part_id"]))
    artifact = artifacts.get_artifact(conn, int(part["artifact_id"]))
    if artifact["kind"] != artifacts.KIND_DRAFT or part["kind"] != artifacts.DRAFT_BODY:
        raise NotFoundError(NOT_A_COMMENT_MESSAGE)


def _require_comment_on_draft(conn: sqlite3.Connection, comment_id: int, artifact_id: int) -> None:
    """404 unless a root comment belongs to the draft starting an address pass."""
    row = conn.execute(
        "select p.artifact_id, c.parent_id from draft_comments c "
        "join artifact_parts p on p.id = c.part_id where c.id = ?",
        (comment_id,),
    ).fetchone()
    if row is None or row["parent_id"] is not None or int(row["artifact_id"]) != artifact_id:
        raise NotFoundError(NOT_A_COMMENT_MESSAGE)


@router.post(
    "/comments/{comment_id}/replies",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
def reply_to_comment(comment_id: int, payload: ReplyWrite, conn: DbConn) -> dict[str, object]:
    """The student's reply under a thread root. The writer replies through its tool."""
    _require_draft_comment(conn, comment_id)
    return comments.add_reply(conn, comment_id, comments.STUDENT, payload.body)


@router.post("/comments/{comment_id}/resolve", response_model=None)
def resolve_comment(comment_id: int, payload: ResolveWrite, conn: DbConn) -> dict[str, object]:
    """Resolve or reopen one thread. Root only; resolution is the student's gesture."""
    _require_draft_comment(conn, comment_id)
    return comments.set_resolved(conn, comment_id, payload.resolved)


@router.get("/drafts/{artifact_id}/pending", response_model=None)
def read_pending(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    """The pending edit for the draft, refreshed against the body, or null."""
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    return suggestions.pending_for_part(conn, int(part["id"]))


def _require_draft_edit(conn: sqlite3.Connection, edit_id: int) -> int:
    """The edit's id, when it belongs to a draft's body. 404 either way otherwise."""
    row = conn.execute("select part_id from pending_edits where id = ?", (edit_id,)).fetchone()
    if row is None:
        raise NotFoundError(NOT_A_SUGGESTION_MESSAGE)
    part = artifacts.get_part(conn, int(row["part_id"]))
    artifact = artifacts.get_artifact(conn, int(part["artifact_id"]))
    if artifact["kind"] != artifacts.KIND_DRAFT or part["kind"] != artifacts.DRAFT_BODY:
        raise NotFoundError(NOT_A_SUGGESTION_MESSAGE)
    return edit_id


@router.post("/pending-edits/{edit_id}/accept", response_model=None)
def accept_edit(edit_id: int, payload: AcceptRequest, conn: DbConn) -> dict[str, object]:
    """Accept all of a suggestion, one hunk of it, or force-replace on a stale one."""
    _require_draft_edit(conn, edit_id)
    linked_comments = _linked_comments_for_edit(conn, edit_id)
    result = suggestions.accept(
        conn,
        edit_id,
        hunk=payload.hunk.model_dump() if payload.hunk else None,
        force=payload.force,
    )
    if linked_comments:
        part_id = int(linked_comments[0]["part_id"])
        current = str(artifacts.get_part(conn, part_id)["content"])
        for link in linked_comments:
            if _linked_section_landed(link, current):
                comments.set_resolved(conn, int(link["comment_id"]), True)
    return result


def _linked_comments_for_edit(conn: sqlite3.Connection, edit_id: int) -> list[dict[str, object]]:
    """Address findings linked to an edit, captured before accept may cascade them."""
    rows = conn.execute(
        "select l.comment_id, c.section_ref, c.hint, e.part_id, "
        "e.base_content, e.proposed_content "
        "from pending_edit_comment_links l "
        "join pending_edits e on e.id = l.edit_id "
        "join draft_comments c on c.id = l.comment_id and c.part_id = e.part_id "
        "where l.edit_id = ? and c.parent_id is null",
        (edit_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _linked_section_landed(link: dict[str, object], current: str) -> bool:
    """Whether the addressed section, not merely some hunk, now matches the proposal."""
    base = str(link["base_content"])
    proposed = str(link["proposed_content"])
    ref = str(link.get("section_ref") or "").strip()
    base_target = sections.extract(base, ref) if ref else None
    if base_target is None and link.get("hint") is not None:
        hint = int(link["hint"])
        base_target = next(
            (section for section in sections.parse(base) if section.start <= hint < section.end),
            None,
        )
    if base_target is None:
        return current == proposed and current != base
    proposed_target = sections.extract(proposed, base_target.number)
    current_target = sections.extract(current, base_target.number)
    return (
        proposed_target is not None
        and current_target is not None
        and proposed_target.text != base_target.text
        and current_target.text == proposed_target.text
    )


@router.post("/pending-edits/{edit_id}/reject", response_model=None)
def reject_edit(edit_id: int, payload: RejectRequest, conn: DbConn) -> dict[str, object]:
    """Reject all of a suggestion, or one hunk out of it. Never writes the draft."""
    _require_draft_edit(conn, edit_id)
    return suggestions.reject(
        conn, edit_id, hunk=payload.hunk.model_dump() if payload.hunk else None
    )


@router.get("/export/availability", response_model=None)
def export_availability(conn: DbConn) -> dict[str, object]:
    """Whether PDF export can run here, and why not when it cannot.

    The workspace asks once and shows Export or keeps Print accordingly - the honest
    fallback for a machine without the binaries, rather than a button that fails.
    """
    message = exporting.export_available()
    return {"available": message is None, "message": message}


@router.post("/drafts/{artifact_id}/export")
def export_draft(artifact_id: int, conn: DbConn) -> Response:
    """The draft as a typeset PDF, through Pandoc and Typst in a sandboxed temp dir."""
    artifact = _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    course = get_class(conn, int(artifact["class_id"]))
    sources = source_ledger.list_sources(conn, int(artifact["class_id"]))
    pdf = exporting.render_pdf(
        str(part["content"]),
        str(artifact["title"]),
        str(course["name"]),
        sources,
    )
    filename = (
        "".join(char for char in str(artifact["title"]) if char.isalnum() or char in " -_").strip()
        or "draft"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="{filename}.pdf"'},
    )


@router.get("/drafts/{artifact_id}/status", response_model=None)
def draft_status(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """The polled suggestion-run state. Skinny on purpose: the workspace polls it.

    The counters ride along because both residents run long enough that a stage name on
    its own reads as a hang: a pass counts sections, a review counts its four lenses, and
    "Reviewing prose (3/4)" is the difference between waiting and wondering.
    """
    artifact = _require_draft(conn, artifact_id)
    active = artifact["state"] in (artifacts.PENDING, artifacts.GENERATING)
    run = writer_runs.active_run(conn, artifact_id) or writer_runs.latest_run(conn, artifact_id)
    metadata: dict[str, object] = {}
    if run is not None:
        metadata = {
            "run_id": run["id"],
            "job_kind": run["job_kind"],
            "depth": run["depth"],
            "started_at": run["started_at"],
            "run_status": run["status"],
            "cancel_requested": run["status"] == writer_runs.CANCEL_REQUESTED,
            "cancel_requested_at": run["cancel_requested_at"],
            "finished_at": run["finished_at"],
            "warnings": run["warnings"],
        }
    else:
        try:
            stored = conn.execute(
                "select writer_job_kind, writer_job_depth, writer_job_started_at "
                "from artifacts where id = ?",
                (artifact_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            stored = None
        if stored is not None:
            metadata = {
                "run_id": None,
                "job_kind": stored["writer_job_kind"],
                "depth": stored["writer_job_depth"],
                "started_at": stored["writer_job_started_at"],
                "run_status": "legacy",
                "cancel_requested": False,
                "cancel_requested_at": None,
                "finished_at": None,
                "warnings": [],
            }
    if active and not metadata.get("job_kind"):
        # Runs created before migration 025 remain honestly partial except for the
        # established review-stage prefix.
        detail = str(artifact["stage_detail"] or "")
        metadata["job_kind"] = REVIEW_JOB_KIND if detail.startswith("Reviewing") else PASS_JOB_KIND
    return {
        "state": artifact["state"],
        "stage_detail": artifact["stage_detail"],
        "error_message": artifact["error_message"],
        "problems_total": artifact["problems_total"],
        "problems_done": artifact["problems_done"],
        "run_id": metadata.get("run_id"),
        "job_kind": metadata.get("job_kind"),
        "depth": metadata.get("depth"),
        "started_at": metadata.get("started_at"),
        "run_status": metadata.get("run_status"),
        "cancel_requested": metadata.get("cancel_requested", False),
        "cancel_requested_at": metadata.get("cancel_requested_at"),
        "finished_at": metadata.get("finished_at"),
        "warnings": metadata.get("warnings", []),
    }


# ---------------------------------------------------------------------------------
# The brief: what the document is. Proposed by the writer, confirmed by the student;
# the PUT is the student's own edit, and saving your own words is agreeing with them.
# ---------------------------------------------------------------------------------


class BriefWrite(BaseModel):
    """Body of `PUT /api/drafts/{artifact_id}/brief` - the student's edit."""

    assignment_type: str = ""
    summary: str = ""
    audience: str = ""
    length_target: str = ""
    source_document_id: int | None = None


@router.get("/drafts/{artifact_id}/brief", response_model=None)
def read_brief(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    """The draft's brief, or null before one has been proposed."""
    _require_draft(conn, artifact_id)
    return briefs.get_brief(conn, artifact_id)


@router.put("/drafts/{artifact_id}/brief", response_model=None)
def write_brief(artifact_id: int, payload: BriefWrite, conn: DbConn) -> dict[str, object]:
    """The student writes the brief in their own words, which lands it confirmed."""
    _require_draft(conn, artifact_id)
    return briefs.save_brief(
        conn,
        artifact_id,
        assignment_type=payload.assignment_type,
        summary=payload.summary,
        audience=payload.audience,
        length_target=payload.length_target,
        source_document_id=payload.source_document_id,
        status=briefs.CONFIRMED,
    )


@router.post("/drafts/{artifact_id}/brief/confirm", response_model=None)
def confirm_brief(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Confirm the proposed brief as it stands."""
    _require_draft(conn, artifact_id)
    return briefs.confirm_brief(conn, artifact_id)


# ---------------------------------------------------------------------------------
# The writer conversation. A chat session in `writer` mode, anchored to the draft's
# body part; the turn runs the tool loop and narrates it as activity frames.
# ---------------------------------------------------------------------------------

# Rounds a conversational turn may spend on tools. A turn is one question, not a
# verification pass: eight rounds is already "read the brief, read two sections, search
# twice, propose" with room to spare, and a model that needs more has lost the thread.
WRITER_CHAT_MAX_DEPTH = 8

# Wall clock for one writer turn, matching the tool loop's own ceiling: a local model
# can spend minutes on a round, and the student watches progress through the activity
# frames rather than a spinner.
WRITER_CHAT_TIMEOUT_SECONDS = 600.0

_WRITER_TURN_ERROR = "Something went wrong while working on this. Try again."

WRONG_SESSION_MESSAGE = "That conversation does not belong to this draft."


class WriterChatRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/chat/{session_id}`."""

    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Message cannot be blank.")
        return cleaned


@router.get("/drafts/{artifact_id}/sessions", response_model=None)
def list_writer_sessions(artifact_id: int, conn: DbConn) -> list[dict[str, object]]:
    """The draft's writer conversations, newest first. The rail opens the newest."""
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    return sessions.writer_sessions_for_part(conn, int(part["id"]))


@router.post(
    "/drafts/{artifact_id}/sessions",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
def create_writer_session(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Open a writer conversation on the draft's body."""
    artifact = _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    return sessions.create_session(
        conn,
        int(artifact["class_id"]),
        artifact_part_id=int(part["id"]),
        mode=sessions.WRITER,
    )


@router.post("/drafts/{artifact_id}/chat/{session_id}")
async def writer_chat(
    artifact_id: int, session_id: int, payload: WriterChatRequest, conn: DbConn
) -> StreamingResponse:
    """One writer turn: persist the question, then stream the tool loop as it works.

    Everything fallible before the first byte happens in the open, where it can still be
    an ordinary error response. After that, failures travel as `error` frames.
    """
    opened = await asyncio.to_thread(_open_writer_turn, conn, artifact_id, session_id, payload)
    return StreamingResponse(
        _stream_writer_turn(artifact_id, session_id, opened, payload.content),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@dataclass(frozen=True)
class _WriterTurn:
    """What the open validated and wrote, handed to the stream."""

    config: TutorConfig
    user_message_id: int


def _open_writer_turn(
    conn: sqlite3.Connection,
    artifact_id: int,
    session_id: int,
    payload: WriterChatRequest,
) -> _WriterTurn:
    """Guards, then the user message, before any streaming starts."""
    artifact = _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    session = sessions.get_session(conn, session_id)
    if session["mode"] != sessions.WRITER or session["artifact_part_id"] != part["id"]:
        raise NotFoundError(WRONG_SESSION_MESSAGE)
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)
    sessions.set_session_title_if_unset(conn, session_id, payload.content)
    user_message_id = sessions.add_message(conn, session_id, "user", payload.content)
    touch_class(conn, int(artifact["class_id"]))
    return _WriterTurn(config=config, user_message_id=user_message_id)


def _prepare_writer_turn(
    conn: sqlite3.Connection, artifact_id: int, session_id: int, turn: _WriterTurn, content: str
) -> tuple[
    list[dict[str, object]],
    dict[str, ToolDefinition],
    writer_tools.RunEffects,
    str,
]:
    """Messages, registry, and the effects record for one writer turn.

    Built together and off the event loop, because all of it reads the database. The
    budget arithmetic follows the tutor's, with one difference: the retrieval share is
    lent to history whole, because the writer retrieves through its search tool inside
    the loop rather than up front. The tool's own budget bounds what a search brings
    back.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    class_id = int(artifact["class_id"])
    intent = writer_intent.classify(content)
    system_prompt = prompts.build_writer_chat_prompt(
        str(artifact["title"]),
        prompts.format_brief_block(briefs.get_brief(conn, artifact_id)),
        sections.outline(str(part["content"])),
        prompts.format_facts_block(select_active_facts(conn, class_id)),
    )
    system_prompt = "\n\n".join((system_prompt, writer_intent.prompt_contract(intent)))
    budget = plan_budget(turn.config.context_window)
    overrun = max(0, estimate_tokens(system_prompt) - budget.system)
    earlier = [
        message
        for message in sessions.list_messages(conn, session_id)
        if int(message["id"]) != turn.user_message_id
    ]
    history, _ = trim_history(earlier, max(0, budget.history + budget.retrieval - overrun))

    messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    messages += [
        {"role": str(message["role"]), "content": str(message["content"])} for message in history
    ]
    messages.append({"role": "user", "content": content})
    private_context = tuple(str(message["content"]) for message in history) + (content,)
    registry, effects = writer_tools.build_registry(
        conn,
        artifact_id,
        writer_tools.CHAT,
        private_context=private_context,
    )
    return messages, registry, effects, intent


async def _stream_writer_turn(
    artifact_id: int, session_id: int, turn: _WriterTurn, content: str
) -> AsyncIterator[str]:
    """Drive the tool loop, narrating each call as it lands, then deliver the answer.

    The final text arrives as one frame rather than token by token: the loop's turns are
    not streamable (parsing tool calls out of an SSE stream is the fragility Phase 2
    declined), so liveness comes from the activity frames and the interface's own
    reveal. See the run-model note in docs/writer-overhaul.md.

    The connection is opened here because this generator outlives the request-scoped one.
    """
    conn = connect()
    activity: list[dict[str, object]] = []
    frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    loop_task: asyncio.Task[ToolLoopResult] | None = None
    try:
        yield _frame(type="start", message_id=turn.user_message_id)
        yield _frame(type="status", stage="prompt_processing")
        messages, registry, effects, intent = await asyncio.to_thread(
            _prepare_writer_turn, conn, artifact_id, session_id, turn, content
        )

        def on_call(recorded: RecordedCall) -> None:
            # Runs on the event loop thread, between rounds; put_nowait cannot block.
            entry = writer_tools.activity_entry(recorded)
            activity.append(entry)
            frames.put_nowait(entry)

        loop_task = asyncio.create_task(
            run_tool_loop(
                turn.config.endpoint_url,
                turn.config.api_key,
                turn.config.model,
                messages,
                max_depth=WRITER_CHAT_MAX_DEPTH,
                timeout_seconds=WRITER_CHAT_TIMEOUT_SECONDS,
                registry=registry,
                on_call=on_call,
            )
        )
        while True:
            getter = asyncio.ensure_future(frames.get())
            done, _ = await asyncio.wait({loop_task, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                entry = getter.result()
                yield _frame(type="activity", **entry)
                continue
            getter.cancel()
            break
        # The loop has returned; whatever it narrated after the last await is drained
        # in order before the answer, so the trail never arrives out of sequence.
        while not frames.empty():
            yield _frame(type="activity", **frames.get_nowait())
        result = loop_task.result()

        if effects.brief_saved:
            yield _frame(type="brief")
        if effects.proposed_edit_id is not None:
            yield _frame(type="proposed", edit_id=effects.proposed_edit_id)
        if effects.pass_started:
            yield _frame(type="pass")
        if effects.review_started:
            yield _frame(type="review")
        if effects.replied_to_comments:
            yield _frame(type="comments")

        if not result.complete:
            # The turn did not finish; its side effects above are real and reported, but
            # a partial answer is not an answer, so nothing is persisted and the student
            # is told to try again. Same rule as the tutor's failed turns.
            yield _frame(type="error", message=result.detail or _WRITER_TURN_ERROR)
            return
        answer = result.content
        contract = writer_intent.validate(
            intent,
            (entry["tool"] for entry in activity if entry.get("ok")),
            complete=result.complete,
            pass_started=effects.pass_started,
            review_started=effects.review_started,
            proposed_edit_id=effects.proposed_edit_id,
        )
        if not contract.satisfied and contract.failure_message:
            logger.warning(
                "Writer turn for draft %s violated %s contract; observed tools=%s",
                artifact_id,
                contract.kind,
                ", ".join(contract.observed_tools) or "(none)",
            )
            answer = contract.failure_message
        yield _frame(type="token", text=answer)
        message_id = sessions.add_message(
            conn, session_id, "assistant", answer, tool_activity=activity
        )
        yield _frame(type="done", message_id=message_id)
    except (asyncio.CancelledError, GeneratorExit):
        # The reader went away. The loop must not keep burning the endpoint for a turn
        # nobody is reading; its completed side effects (a proposal, a saved brief) are
        # durable rows the rail will show on next load either way.
        raise
    except LyraError as exc:
        yield _frame(type="error", message=exc.message)
    except Exception:
        logger.exception("Writer turn failed for draft %s", artifact_id)
        yield _frame(type="error", message=_WRITER_TURN_ERROR)
    finally:
        if loop_task is not None and not loop_task.done():
            loop_task.cancel()
        conn.close()
