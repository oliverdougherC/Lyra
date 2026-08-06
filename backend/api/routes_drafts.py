"""Draft endpoints: the writing workspace.

A draft is an artifact with exactly one body part, so revisions, provenance, and
step-scoped chat all work unchanged. Creation is synchronous (a draft is born empty and
`ready`; there is no job to queue). The two AI surfaces are `/write` - a streamed inline
passage, stateless, which lives in the client until accepted - and `/suggest` - a queued
whole-document revision that lands as a pending edit reviewed hunk by hunk.

Handlers are sync `def` (sqlite3 blocks; FastAPI threadpools them), except `/write`,
which is `async def` because the turn is driven by awaited httpx reads.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.core import artifacts, drafting, suggestions
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.classes import get_class, touch_class
from backend.core.errors import LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.llm import client, prompts
from backend.rag.retrieve import retrieve
from backend.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

NOT_A_DRAFT_MESSAGE = "That draft does not exist."
NOT_A_SUGGESTION_MESSAGE = "That suggestion does not exist."
NO_DOCUMENT_MESSAGE = "This draft has no body."

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


class SuggestRequest(BaseModel):
    """Body of `POST /api/drafts/{artifact_id}/suggest`."""

    instruction: str = Field(min_length=1)

    @field_validator("instruction")
    @classmethod
    def _check_instruction(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("An instruction cannot be blank.")
        return cleaned


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


@router.post(
    "/classes/{class_id}/drafts",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
def create_draft(class_id: int, payload: DraftCreate, conn: DbConn) -> dict[str, object]:
    """A draft is born empty and ready; the first AI pass comes through `/suggest`."""
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
    messages = prompts.build_write_prompt(
        payload.instruction,
        payload.heading,
        payload.selection,
        payload.nearby,
        context_block,
        facts_block,
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
    "/drafts/{artifact_id}/suggest",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
def suggest(artifact_id: int, payload: SuggestRequest, conn: DbConn) -> dict[str, object]:
    """Queue a whole-document revision. The proposal lands as a pending edit."""
    _require_draft(conn, artifact_id)
    drafting.enqueue(drafting._Job(artifact_id, payload.instruction))
    return artifacts.get_artifact(conn, artifact_id)


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
    return suggestions.accept(
        conn,
        edit_id,
        hunk=payload.hunk.model_dump() if payload.hunk else None,
        force=payload.force,
    )


@router.post("/pending-edits/{edit_id}/reject", response_model=None)
def reject_edit(edit_id: int, payload: RejectRequest, conn: DbConn) -> dict[str, object]:
    """Reject all of a suggestion, or one hunk out of it. Never writes the draft."""
    _require_draft_edit(conn, edit_id)
    return suggestions.reject(
        conn, edit_id, hunk=payload.hunk.model_dump() if payload.hunk else None
    )


@router.get("/drafts/{artifact_id}/status", response_model=None)
def draft_status(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """The polled suggestion-run state. Skinny on purpose: the workspace polls it."""
    artifact = _require_draft(conn, artifact_id)
    return {
        "state": artifact["state"],
        "stage_detail": artifact["stage_detail"],
        "error_message": artifact["error_message"],
    }
