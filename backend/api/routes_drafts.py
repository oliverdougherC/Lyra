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
from starlette.types import Receive, Scope, Send

from backend.core import (
    artifacts,
    briefs,
    comments,
    exporting,
    live_drafts,
    review_pipeline,
    sections,
    sessions,
    source_ledger,
    suggestions,
    writer_attempts,
    writer_intent,
    writer_pipeline,
    writer_runs,
    writer_tools,
)
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    resolve_tutor_access,
)
from backend.core.classes import get_class, touch_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.core.writer_budgets import Depth, validate_depth
from backend.llm import client, prompts
from backend.llm.tools import (
    ContextBudget,
    RecordedCall,
    ToolDefinition,
    ToolLoopResult,
    conversation_tokens,
    run_tool_loop,
    schema_tokens,
    tool_schemas,
)
from backend.llm.turn_budget import (
    CONTEXT_SAFETY_MARGIN,
    HistoryMessage,
    TurnReserve,
    input_ceiling,
    mandatory_history_tokens,
    plan_budget,
    trim_history,
)
from backend.rag.retrieve import retrieve
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

NOT_A_DRAFT_MESSAGE = "That draft does not exist."
NOT_A_SUGGESTION_MESSAGE = "That suggestion does not exist."
NOT_A_LIVE_BLOCK_MESSAGE = "That live suggestion block does not exist."
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

EDITED_SINCE_CHECK = "This was edited after it was checked, so the earlier check no longer applies."
RESTORED_NOTE = "Restored version {revision}."
PRE_RESTORE_NOTE = "Body before restore to version {revision}."
DRAFT_RESTORE_NEEDS_VERSION = (
    "A draft restore requires expected_version to guarantee you are restoring what you see."
)

WRITE_RETRIEVAL_BUDGET = 2_000

BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then write.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and writing has to send it your "
        "course material. Allow that in Settings, then write."
    ),
}

_WRITE_ERROR_MESSAGE = "Something went wrong while writing. Try again."

# Said, and only this, when an inline-writing turn cannot fit the configured window even
# after every optional block has been trimmed away. Bounded and privacy-safe: it names no
# endpoint, path, or part of the prompt - the student needs to act, not to see internals.
_WRITE_TOO_LARGE_MESSAGE = (
    "This writing request is too large for the tutor's context window, even after trimming "
    "optional context. Shorten your instruction or the selected text, then try again."
)

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

    `expected_version` is the optimistic-concurrency token (PLA-289): the `body_version`
    the client last read. The write lands only if the stored body still carries it, so a
    stale autosave or a second tab cannot silently overwrite newer writing. It is required
    - a body write with no version to check is exactly the last-writer-wins race this
    endpoint exists to close.
    """

    content: str
    expected_version: int = Field(ge=0)
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


class DraftRestoreRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/parts/{part_id}/restore`."""

    revision: int = Field(ge=1)
    expected_version: int = Field(ge=0)


class AcceptRequest(BaseModel):
    """Body of `POST /api/pending-edits/{edit_id}/accept`.

    `expected_body_version` is the draft body `content_version` the student reviewed the
    suggestion against (PLA-289). The accept is refused if the stored body has moved past it
    - a second tab, a concurrent pass, or an autosave that landed after the student last
    looked - rather than silently overwriting the newer body.

    It is **required**. This endpoint only ever accepts a draft body (`_require_draft_edit`
    rejects everything else), so there is no versionless surface to stay compatible with, and
    a draft accept with no version to check is exactly the stale-write race PLA-289 closes.
    A missing token is a 422 before any mutation runs - a stale bundle or a direct caller
    cannot force-replace a draft body without naming the version it saw.
    """

    hunk: HunkRef | None = None
    force: bool = False
    expected_body_version: int = Field(ge=0)


class RejectRequest(BaseModel):
    """Body of `POST /api/pending-edits/{edit_id}/reject`."""

    hunk: HunkRef | None = None


class LiveSuggestionBlockPatch(BaseModel):
    """Body of `PATCH /api/drafts/{artifact_id}/live-suggestion/blocks/{block_id}`."""

    expected_revision: int = Field(ge=1)
    base_content: str | None = None
    section_ref: str | None = None
    ordinal: int | None = Field(default=None, ge=0)
    kind: str | None = None
    heading: str | None = None
    content: str | None = None
    status: str | None = None
    target_words: int | None = Field(default=None, ge=0)
    summary: str | None = None
    context: dict[str, object] | list[object] | str | int | float | bool | None = None
    metadata: dict[str, object] | list[object] | str | int | float | bool | None = None


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
        if conn.in_transaction:
            conn.commit()
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
        # The optimistic-concurrency token the workspace echoes back on every autosave.
        "body_version": part["content_version"],
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
    """The autosave path. See `BodyUpdate` for the revision and version rules.

    Writes through the compare-and-swap so a stale request cannot overwrite a newer body:
    a version mismatch raises `StaleContentError`, which the shared handler renders as a
    409 carrying the current version and stored body for the workspace to reconcile. The
    successful response returns the new authoritative `version`.
    """
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    result = artifacts.compare_and_set_part_content(
        conn,
        int(part["id"]),
        payload.content,
        artifacts.USER_CORRECTED,
        expected_version=payload.expected_version,
        note=(payload.note or "snapshot") if payload.snapshot else None,
        record_revision=payload.snapshot,
    )
    return {"part_id": part["id"], "saved": True, "version": result["version"]}


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
    """Everything fallible, before the first byte: guards, grounding, and the prompt.

    The prompt is budgeted against the resolved `TutorConfig.context_window` (PLA-300), so
    an inline write never knowingly sends more than the window holds. The writing system
    prompt and the student's instruction are mandatory, and so is the selected text when
    there is any: it is the subject of the operation ("rewrite this", "tighten this"), so
    dropping it would silently change what was asked. If those mandatory pieces cannot fit
    beside the reply reserve, the turn is refused here, locally, before retrieval and before
    any upstream request - the path stays stateless, touching nothing on a refusal.

    The optional context is added in a fixed priority order - current heading, surrounding
    text, retrieved course material, the draft brief, then class facts - each included only
    while the whole prompt still fits the window minus the reserve, and otherwise dropped.
    Retrieval is sized to the room left after the higher-priority local context, rather than
    to a fixed budget, so a large selection or a large nearby block shrinks what is fetched
    instead of overrunning the window.
    """
    artifact = _require_draft(conn, artifact_id)
    # One snapshot: the endpoint checked for consent is the endpoint the turn is sent to.
    access = resolve_tutor_access(conn)
    if access.document_block is not None:
        raise LyraError(BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = access.config
    class_id = int(artifact["class_id"])

    # The reply reserve comes off the top and is never lent to the prompt. The remaining
    # input room carries the same PLA-290 safety margin as every guarded request: the
    # four-characters-per-token estimator is still approximate for a plain streamed turn,
    # even though this path has no tool schema or growing transcript.
    generation_reserve = plan_budget(config.context_window).generation
    ceiling = input_ceiling(config.context_window, generation_reserve)

    selection = payload.selection or None
    heading = payload.heading or None
    nearby = payload.nearby or None

    kept_heading: str | None = None
    kept_nearby: str | None = None

    def assemble(
        *, context_block: str = "", facts_block: str = "", brief_block: str = ""
    ) -> list[dict[str, str]]:
        return prompts.build_write_prompt(
            payload.instruction,
            kept_heading,
            selection,
            kept_nearby,
            context_block,
            facts_block,
            brief_block,
        )

    def fits(messages: list[dict[str, str]]) -> bool:
        return conversation_tokens(messages) <= ceiling

    # Mandatory floor: system prompt + instruction + selected text. If even this cannot fit
    # beside the reserve, no trimming of optional context can help, so refuse before any
    # retrieval or upstream call.
    if not fits(assemble()):
        raise LyraError(_WRITE_TOO_LARGE_MESSAGE)

    # Optional local context, highest priority first. Each is kept only if the whole prompt
    # still fits once it is added; a block that does not fit is dropped and the next, smaller
    # one is still tried.
    if heading is not None:
        kept_heading = heading
        if not fits(assemble()):
            kept_heading = None
    if nearby is not None:
        kept_nearby = nearby
        if not fits(assemble()):
            kept_nearby = None

    # Retrieval is sized to the room left after the mandatory and local context, capped at
    # the ordinary write budget. A zero budget means there is no room, so the embedding
    # search is skipped entirely rather than run only to be dropped.
    used = conversation_tokens(assemble())
    retrieval_room = max(0, min(WRITE_RETRIEVAL_BUDGET, ceiling - used))
    context_block = ""
    if retrieval_room > 0:
        query = payload.instruction + " " + (selection or heading or "")
        result = retrieve(conn, class_id, query, retrieval_room)
        candidate = prompts.format_context_block([vars(chunk) for chunk in result.chunks])
        if candidate and fits(assemble(context_block=candidate)):
            context_block = candidate

    brief_block = prompts.format_brief_block(briefs.get_brief(conn, artifact_id))
    if brief_block and not fits(assemble(context_block=context_block, brief_block=brief_block)):
        brief_block = ""

    facts_block = prompts.format_facts_block(select_active_facts(conn, class_id))
    if facts_block and not fits(
        assemble(context_block=context_block, brief_block=brief_block, facts_block=facts_block)
    ):
        facts_block = ""

    return config, assemble(
        context_block=context_block, brief_block=brief_block, facts_block=facts_block
    )


async def _stream_write(config: TutorConfig, messages: list[dict[str, str]]) -> AsyncIterator[str]:
    """Token frames for the answer channel, then done. Errors arrive as frames."""
    try:
        async for delta in client.stream_chat(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            max_tokens=plan_budget(config.context_window).generation,
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
    if run is not None and not payload.sections and payload.address_comment_id is None:
        live_drafts.create_live_suggestion(
            conn,
            artifact_id,
            int(run["id"]),
            stage="gathering",
            status="pending",
            detail="Queued to understand the assignment",
        )
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


@router.get("/drafts/{artifact_id}/parts/{part_id}/revisions", response_model=None)
def read_draft_revisions(artifact_id: int, part_id: int, conn: DbConn) -> list[dict[str, object]]:
    _require_draft(conn, artifact_id)
    part = artifacts.get_part(conn, part_id)
    if int(part["artifact_id"]) != artifact_id or part["kind"] != artifacts.DRAFT_BODY:
        raise NotFoundError(NO_DOCUMENT_MESSAGE)
    return artifacts.list_revisions(conn, part_id)


@router.post("/drafts/{artifact_id}/parts/{part_id}/restore", response_model=None)
def restore_draft_revision(
    artifact_id: int, part_id: int, payload: DraftRestoreRequest, conn: DbConn
) -> dict[str, object]:
    """Put an earlier version of a draft body back, with CAS on expected_version."""
    _require_draft(conn, artifact_id)
    part = artifacts.get_part(conn, part_id)
    if int(part["artifact_id"]) != artifact_id or part["kind"] != artifacts.DRAFT_BODY:
        raise NotFoundError(NO_DOCUMENT_MESSAGE)
    revision = artifacts.get_revision(conn, part_id, payload.revision)
    result = artifacts.compare_and_restore_part_content(
        conn,
        part_id,
        str(revision["content"]),
        artifacts.USER_CORRECTED,
        expected_version=payload.expected_version,
        restored_note=RESTORED_NOTE.format(revision=payload.revision),
        preserved_origin=artifacts.USER_CORRECTED,
        preserved_note=PRE_RESTORE_NOTE.format(revision=payload.revision),
    )
    restored_part = artifacts.get_part(conn, part_id)
    return {
        **restored_part,
        "body_version": result["version"],
    }


@router.get("/drafts/{artifact_id}/pending", response_model=None)
def read_pending(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    """The pending edit for the draft, refreshed against the body, or null."""
    _require_draft(conn, artifact_id)
    part = _body_part(conn, artifact_id)
    return suggestions.pending_for_part(conn, int(part["id"]))


@router.get("/drafts/{artifact_id}/live-suggestion", response_model=None)
def read_live_suggestion(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    """The latest structured live suggestion for the draft, blocks included."""
    _require_draft(conn, artifact_id)
    return live_drafts.get_latest_live_suggestion(conn, artifact_id)


def _require_live_block_on_draft(conn: sqlite3.Connection, artifact_id: int, block_id: int) -> None:
    _require_draft(conn, artifact_id)
    if not live_drafts.block_belongs_to_artifact(conn, artifact_id, block_id):
        raise NotFoundError(NOT_A_LIVE_BLOCK_MESSAGE)


@router.patch("/drafts/{artifact_id}/live-suggestion/blocks/{block_id}", response_model=None)
def patch_live_suggestion_block(
    artifact_id: int, block_id: int, payload: LiveSuggestionBlockPatch, conn: DbConn
) -> dict[str, object]:
    """User-edit one live suggestion block with CAS protection."""
    _require_live_block_on_draft(conn, artifact_id, block_id)
    fields = payload.model_dump(exclude_unset=True)
    update: dict[str, object] = {"expected_revision": int(fields.pop("expected_revision"))}
    if "base_content" in fields:
        update["base_content"] = fields.pop("base_content")
    if "section_ref" in fields:
        update["section_ref"] = fields["section_ref"]
    if "ordinal" in fields:
        update["paragraph_ordinal"] = fields["ordinal"]
    for key in ("kind", "heading", "content", "status", "target_words", "summary"):
        if key in fields:
            update[key] = fields[key]
    if "context" in fields:
        update["context"] = fields["context"]
    if "metadata" in fields:
        update["metadata"] = fields["metadata"]
    return live_drafts.patch_block(conn, block_id, **update)


@router.post("/drafts/{artifact_id}/live-suggestion/finalize", response_model=None)
def finalize_live_suggestion(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    """Publish the completed live artifact into ordinary pending-edit review."""
    _require_draft(conn, artifact_id)
    live = live_drafts.get_latest_live_suggestion(conn, artifact_id)
    if live is None:
        raise NotFoundError(live_drafts.NOT_A_LIVE_SUGGESTION_MESSAGE)
    if live["status"] != "ready":
        raise ConflictError("The live draft suggestion is still being written.")
    live_drafts.finalize_to_pending_edit(
        conn,
        int(live["id"]),
        note="agentic long-form draft",
    )
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
        expected_version=payload.expected_body_version,
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
#
# Three PLA contracts govern this section:
#
# PLA-308 -- per-session turn claim serialises writer-chat turns.
# PLA-309 -- a read-only preflight (`_plan_writer_turn`) charges the COMPLETE
#   mandatory first-request payload (intent contract, frozen tool schemas, message
#   framing, serialisation overhead, safety margin, generation reserve) before any
#   mutation.  `run_tool_loop` deliberately skips its context-overflow check for
#   round zero because its contract assumes the caller already proved the first
#   request fits, so the request assembled here must be <= the same
#   `ContextBudget.message_ceiling` the loop enforces later.
# PLA-310 -- the user message AND a planned attempt are persisted atomically
#   (one committed transaction), closing the window where a durable question could
#   exist without a corresponding attempt.  Every durable effect produced by a
#   writer tool is attributable to its attempt via `writer_attempt_targets`.  The
#   assistant reply and the completed attempt are committed together; the error
#   path rolls back before settling the attempt in a fresh transaction.
# ---------------------------------------------------------------------------------

WRITER_CHAT_MAX_DEPTH = 8

WRITER_CHAT_TIMEOUT_SECONDS = 600.0

_WRITER_TURN_ERROR = "Something went wrong while working on this. Try again."
_WRITER_TOO_LARGE = (
    "This question is too long to fit the writing assistant's context window with the "
    "draft, brief, and conversation history that it needs. Try a shorter message."
)
_WRITER_PERSISTENCE_STOPPED = (
    "This turn was interrupted before its reply could be saved. Try it again."
)
_WRITER_PERSISTENCE_FAILED = "The writer reply could not be saved. Try it again."

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


# ---------------------------------------------------------------------------
# Writer-turn planning: the read-only preflight (PLA-309)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterTurnPlan:
    """Everything an accepted writer turn needs, computed before any mutation.

    The preflight proves the first request fits the margin-reduced window with the
    exact tool schemas, the complete system prompt (intent contract included), and
    the generation reserve. A probe registry is built to measure schema tokens; the
    stream rebuilds the real registry with its own connection so tool handlers close
    over a connection that outlives the request.
    """

    config: TutorConfig
    content: str
    intent: str
    system_prompt: str
    messages: list[dict[str, object]]
    private_context: tuple[str, ...]
    context_budget: ContextBudget
    tool_tokens: int


def _plan_writer_turn(
    conn: sqlite3.Connection,
    artifact_id: int,
    session_id: int,
    config: TutorConfig,
    content: str,
    *,
    exclude_message_ids: frozenset[int] = frozenset(),
) -> WriterTurnPlan:
    """Cost, fit-check, and assemble one writer turn without mutating anything.

    `exclude_message_ids` names messages that must not enter this turn's history. It
    is empty for a fresh send (the current message is not persisted yet); on a retry
    it holds the reused user message's id so it appears exactly once as the current
    message and never again as history.

    Read-only by construction: it inspects the session, the tool definitions the class
    grants, and the prior history, then either raises (an oversized turn, refused before
    any mutation) or returns the assembled first request, the executable registry, and
    the budget the loop guards with.

    The capability state is read once and frozen. The registry built here is the
    registry the loop will run; the tool schemas budgeted here are the schemas sent.
    A capability change landing mid-turn waits for the next turn.
    """
    budget = plan_budget(config.context_window)

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

    probe_registry, probe_effects = writer_tools.build_registry(
        conn,
        artifact_id,
        writer_tools.CHAT,
        private_context=(),
    )
    tool_tokens = schema_tokens(tool_schemas(probe_registry))

    earlier = tuple(
        HistoryMessage(role=str(msg["role"]), content=str(msg["content"]))
        for msg in sessions.list_messages(conn, session_id)
        if int(msg["id"]) not in exclude_message_ids
    )

    system_tokens = estimate_tokens(system_prompt)
    question_tokens = estimate_tokens(content)
    reserve = TurnReserve(
        context_window=config.context_window,
        generation=budget.generation,
        fixed_tokens=system_tokens + tool_tokens,
        question_tokens=question_tokens,
        mandatory_history_tokens=mandatory_history_tokens(earlier),
    )
    if not reserve.fits:
        raise LyraError(_WRITER_TOO_LARGE)

    ceiling = input_ceiling(config.context_window, budget.generation)
    message_ceiling = ceiling - tool_tokens

    system_message: dict[str, object] = {"role": "system", "content": system_prompt}
    user_message: dict[str, object] = {"role": "user", "content": content}
    kept: list[dict[str, object]] = []
    for msg in reversed(earlier):
        rendered: dict[str, object] = {"role": msg.role, "content": msg.content}
        candidate = [system_message, rendered, *kept, user_message]
        if conversation_tokens(candidate) > message_ceiling and len(kept) >= 2:
            break
        kept.insert(0, rendered)
    messages = [system_message, *kept, user_message]

    if conversation_tokens(messages) + tool_tokens > ceiling:
        raise LyraError(_WRITER_TOO_LARGE)

    private_context = (
        tuple(str(msg.content) for msg in earlier[-len(kept) :]) + (content,)
        if kept
        else (content,)
    )

    context_budget = ContextBudget(
        context_window=config.context_window,
        generation_reserve=budget.generation,
        tool_tokens=tool_tokens,
        safety_margin=CONTEXT_SAFETY_MARGIN,
    )
    return WriterTurnPlan(
        config=config,
        content=content,
        intent=intent,
        system_prompt=system_prompt,
        messages=messages,
        private_context=private_context,
        context_budget=context_budget,
        tool_tokens=tool_tokens,
    )


# ---------------------------------------------------------------------------
# Session and route endpoints
# ---------------------------------------------------------------------------


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
    """One writer turn: plan, persist atomically, then stream the tool loop.

    The session claim is acquired synchronously before the first suspension point, so a
    competing writer turn on the same session gets a deterministic 409 before it can
    mutate history or execute tools.  The preflight proves the first request fits
    before anything is persisted.  The user message and its planned attempt are
    committed in a single transaction.
    """
    turn_token = sessions.begin_turn(session_id)
    opener = asyncio.ensure_future(
        asyncio.to_thread(_open_writer_turn, conn, artifact_id, session_id, payload, turn_token)
    )
    try:
        opened = await asyncio.shield(opener)
    except BaseException:
        _finish_writer_opening(opener, session_id, turn_token)
        raise
    return _writer_turn_response(
        session_id,
        turn_token,
        _stream_writer_turn(artifact_id, session_id, opened, turn_token),
    )


@router.post("/drafts/{artifact_id}/chat/{session_id}/retry")
async def writer_chat_retry(artifact_id: int, session_id: int, conn: DbConn) -> StreamingResponse:
    """Retry the most recent writer turn: replay a completed one, re-run a failed one.

    The claim is acquired synchronously, exactly as in ``writer_chat``.
    """
    turn_token = sessions.begin_turn(session_id)
    opener = asyncio.ensure_future(
        asyncio.to_thread(_open_writer_retry, conn, artifact_id, session_id, turn_token)
    )
    try:
        opened = await asyncio.shield(opener)
    except BaseException:
        _finish_writer_opening(opener, session_id, turn_token)
        raise
    if opened.replay_content is not None:
        return _writer_turn_response(
            session_id,
            turn_token,
            _replay_writer_turn(opened),
        )
    return _writer_turn_response(
        session_id,
        turn_token,
        _stream_writer_turn(artifact_id, session_id, opened, turn_token),
    )


# ---------------------------------------------------------------------------
# Streaming response that owns the session claim
# ---------------------------------------------------------------------------


class _WriterTurnStreamingResponse(StreamingResponse):
    """A streaming response that releases the session claim on every exit path.

    Mirrors ``TurnStreamingResponse`` in ``routes_chat``: ``end_turn`` is idempotent
    and token-owned, so this and the generator's own release can never double-free,
    and neither can free a claim a newer turn has since taken.
    """

    def __init__(
        self,
        session_id: int,
        turn_token: int,
        content: AsyncIterator[str],
        **kwargs: object,
    ) -> None:
        super().__init__(content, **kwargs)  # type: ignore[arg-type]
        self._session_id = session_id
        self._turn_token = turn_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            sessions.end_turn(self._session_id, self._turn_token)


def _writer_turn_response(
    session_id: int,
    turn_token: int,
    stream: AsyncIterator[str],
) -> _WriterTurnStreamingResponse:
    try:
        return _WriterTurnStreamingResponse(
            session_id,
            turn_token,
            stream,
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    except BaseException:
        sessions.end_turn(session_id, turn_token)
        raise


def _finish_writer_opening(opener: asyncio.Future, session_id: int, turn_token: int) -> None:
    """Release an abandoned claim once the opener worker has stopped."""

    def _release(task: asyncio.Future) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.debug("Abandoned writer opener for session %s failed", session_id)
        sessions.end_turn(session_id, turn_token)

    if opener.done():
        _release(opener)
    else:
        opener.add_done_callback(_release)


# ---------------------------------------------------------------------------
# Turn open / prepare / stream
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _WriterTurn:
    """What the open validated and wrote, handed to the stream."""

    config: TutorConfig
    user_message_id: int
    attempt_id: int
    plan: WriterTurnPlan
    content: str = ""
    replay_content: str | None = None
    replay_message_id: int | None = None


def _open_writer_turn(
    conn: sqlite3.Connection,
    artifact_id: int,
    session_id: int,
    payload: WriterChatRequest,
    turn_token: int,
) -> _WriterTurn:
    """Plan, then atomically persist user message + planned attempt.

    The preflight (`_plan_writer_turn`) runs entirely read-only, proving the first
    request fits before anything is written. On success the user message and a
    `planned` attempt are committed in a single transaction, closing the gap where
    a durable question could exist without an attempt.
    """
    try:
        artifact = _require_draft(conn, artifact_id)
        part = _body_part(conn, artifact_id)
        session = sessions.get_session(conn, session_id)
        if session["mode"] != sessions.WRITER or session["artifact_part_id"] != part["id"]:
            raise NotFoundError(WRONG_SESSION_MESSAGE)
        access = resolve_tutor_access(conn)
        if access.document_block is not None:
            raise LyraError(
                BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT])
            )
        config = access.config

        plan = _plan_writer_turn(conn, artifact_id, session_id, config, payload.content)

        sessions.set_session_title_if_unset(conn, session_id, payload.content)
        conn.execute("begin immediate")
        user_message_id = sessions.insert_message(conn, session_id, "user", payload.content)
        attempt_id = writer_attempts.create_attempt(
            conn,
            session_id=session_id,
            user_message_id=user_message_id,
            intent=plan.intent,
        )
        conn.commit()

        sessions.bind_turn(session_id, turn_token, user_message_id)
        touch_class(conn, int(artifact["class_id"]))
    except BaseException:
        sessions.end_turn(session_id, turn_token)
        raise
    return _WriterTurn(
        config=config,
        user_message_id=user_message_id,
        attempt_id=attempt_id,
        plan=plan,
        content=payload.content,
    )


def _open_writer_retry(
    conn: sqlite3.Connection,
    artifact_id: int,
    session_id: int,
    turn_token: int,
) -> _WriterTurn:
    """Resolve the retry target under the session claim.

    A completed attempt is replayed without re-running the model. A failed or
    stopped attempt with NO durable effects gets a new attempt. A failed or stopped
    attempt WITH durable effects is surfaced for student decision.
    """
    try:
        _require_draft(conn, artifact_id)
        part = _body_part(conn, artifact_id)
        session = sessions.get_session(conn, session_id)
        if session["mode"] != sessions.WRITER or session["artifact_part_id"] != part["id"]:
            raise NotFoundError(WRONG_SESSION_MESSAGE)
        access = resolve_tutor_access(conn)
        if access.document_block is not None:
            raise LyraError(
                BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT])
            )
        config = access.config
        target = writer_attempts.resolve_retry_target(conn, session_id)
        sessions.bind_turn(session_id, turn_token, target.user_message_id)
        if target.latest["state"] == writer_attempts.COMPLETED:
            assistant_msg_id = target.latest.get("assistant_message_id")
            if assistant_msg_id is not None:
                row = conn.execute(
                    "select content from messages where id = ?", (assistant_msg_id,)
                ).fetchone()
                if row is not None:
                    return _WriterTurn(
                        config=config,
                        user_message_id=target.user_message_id,
                        attempt_id=int(target.latest["id"]),
                        plan=WriterTurnPlan(
                            config=config,
                            content=target.content,
                            intent=target.intent,
                            system_prompt="",
                            messages=[],
                            private_context=(),
                            context_budget=ContextBudget(
                                context_window=0,
                                generation_reserve=0,
                                tool_tokens=0,
                            ),
                            tool_tokens=0,
                        ),
                        content=target.content,
                        replay_content=str(row["content"]),
                        replay_message_id=int(assistant_msg_id),
                    )

        if writer_attempts.has_durable_effects(conn, int(target.latest["id"])):
            raise ConflictError(
                "The previous attempt made changes (a proposal, comment, or brief) before "
                "it failed. Review what landed, then send a new message.",
            )

        plan = _plan_writer_turn(
            conn,
            artifact_id,
            session_id,
            config,
            target.content,
            exclude_message_ids=frozenset({target.user_message_id}),
        )

        conn.execute("begin immediate")
        attempt_id = writer_attempts.create_attempt(
            conn,
            session_id=session_id,
            user_message_id=target.user_message_id,
            intent=plan.intent,
        )
        conn.commit()
    except BaseException:
        sessions.end_turn(session_id, turn_token)
        raise
    return _WriterTurn(
        config=config,
        user_message_id=target.user_message_id,
        attempt_id=attempt_id,
        plan=plan,
        content=target.content,
    )


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


async def _replay_writer_turn(turn: _WriterTurn) -> AsyncIterator[str]:
    """Replay a completed turn's reply without re-running the model."""
    yield _frame(type="start", message_id=turn.user_message_id)
    yield _frame(type="token", text=turn.replay_content or "")
    yield _frame(type="done", message_id=turn.replay_message_id or turn.user_message_id)


async def _stream_writer_turn(
    artifact_id: int,
    session_id: int,
    turn: _WriterTurn,
    turn_token: int,
) -> AsyncIterator[str]:
    """Drive the tool loop, narrating each call, then deliver the answer.

    The connection is opened here because this generator outlives the request-scoped
    one.  The attempt was created in `planned` state by the opener; it is promoted to
    `running` just before the loop starts, so a crash during preparation is
    distinguishable from one during execution.

    Crash-safe completion mirrors the agent-chat pattern:
    - assistant message + COMPLETED attempt in one explicit transaction
    - rollback on any failure
    - best-effort settlement in a fresh transaction
    - conditional WHERE predicates prevent double-settle on ambiguous commits
    - startup reconciliation as final fallback
    """
    conn = connect()
    activity: list[dict[str, object]] = []
    frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    loop_task: asyncio.Task[ToolLoopResult] | None = None
    attempt_id: int | None = turn.attempt_id
    plan = turn.plan
    try:
        yield _frame(type="start", message_id=turn.user_message_id)
        yield _frame(type="status", stage="prompt_processing")

        writer_attempts.promote_to_running(conn, attempt_id)

        registry, effects = writer_tools.build_registry(
            conn,
            artifact_id,
            writer_tools.CHAT,
            private_context=plan.private_context,
        )
        effects.attempt_id = attempt_id

        def on_call(recorded: RecordedCall) -> None:
            entry = writer_tools.activity_entry(recorded)
            activity.append(entry)
            frames.put_nowait(entry)

        loop_task = asyncio.create_task(
            run_tool_loop(
                plan.config.endpoint_url,
                plan.config.api_key,
                plan.config.model,
                plan.messages,
                max_depth=WRITER_CHAT_MAX_DEPTH,
                timeout_seconds=WRITER_CHAT_TIMEOUT_SECONDS,
                registry=registry,
                on_call=on_call,
                context_budget=plan.context_budget,
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
            detail = result.detail or _WRITER_TURN_ERROR
            writer_attempts.fail_attempt(
                conn, attempt_id, stopped_reason=result.stopped or "incomplete", detail=detail
            )
            attempt_id = None
            yield _frame(type="error", message=detail)
            return
        answer = result.content
        if not answer.strip():
            writer_attempts.fail_attempt(
                conn,
                attempt_id,
                stopped_reason="empty",
                detail="The writer returned an empty response. Try it again.",
            )
            attempt_id = None
            yield _frame(
                type="error", message="The writer returned an empty response. Try it again."
            )
            return
        contract = writer_intent.validate(
            plan.intent,
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

        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("begin immediate")
            message_id = sessions.insert_message(
                conn, session_id, "assistant", answer, tool_activity=activity
            )
            writer_attempts.mark_completed(conn, attempt_id, message_id)
            conn.commit()
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            try:
                if isinstance(exc, asyncio.CancelledError | GeneratorExit):
                    writer_attempts.stop_attempt(
                        conn,
                        attempt_id,
                        detail=_WRITER_PERSISTENCE_STOPPED,
                    )
                else:
                    writer_attempts.fail_attempt(
                        conn,
                        attempt_id,
                        stopped_reason="persistence_failed",
                        detail=_WRITER_PERSISTENCE_FAILED,
                    )
            except Exception:
                logger.warning(
                    "Could not settle writer attempt %s after final reply persistence "
                    "failed; startup reconciliation remains the fallback",
                    attempt_id,
                )
            raise
        attempt_id = None
        yield _frame(type="done", message_id=message_id)
    except (asyncio.CancelledError, GeneratorExit):
        if attempt_id is not None:
            try:
                writer_attempts.stop_attempt(
                    conn,
                    attempt_id,
                    detail="This turn was interrupted before it finished. Try it again.",
                )
            except Exception:
                logger.debug("Could not settle writer attempt %s after cancellation", attempt_id)
        raise
    except LyraError as exc:
        if attempt_id is not None:
            try:
                writer_attempts.fail_attempt(
                    conn, attempt_id, stopped_reason="error", detail=exc.message
                )
            except Exception:
                logger.debug("Could not settle writer attempt %s after LyraError", attempt_id)
            attempt_id = None
        yield _frame(type="error", message=exc.message)
    except Exception:
        logger.exception("Writer turn failed for draft %s", artifact_id)
        if attempt_id is not None:
            try:
                writer_attempts.fail_attempt(
                    conn, attempt_id, stopped_reason="error", detail=_WRITER_TURN_ERROR
                )
            except Exception:
                logger.debug("Could not settle writer attempt %s after exception", attempt_id)
            attempt_id = None
        yield _frame(type="error", message=_WRITER_TURN_ERROR)
    finally:
        if loop_task is not None and not loop_task.done():
            loop_task.cancel()
        conn.close()
