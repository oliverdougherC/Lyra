"""Chat endpoints: sessions, message history, and the streamed turn.

Handlers are sync `def`, because `sqlite3` blocks and FastAPI runs sync handlers in a
threadpool. The streaming handler is the exception: it is `async def` because the turn it
returns is driven by awaited `httpx` reads from the tutor endpoint.

The stream is SSE over POST, one of seven JSON frame shapes per `data:` line. The frame types
are the contract, so there is no `[DONE]` sentinel: a client reads `type` and dispatches on it.
A reasoning model's thought arrives on `reasoning` frames, entirely separate from the `token`
frames carrying the answer, so the interface can show the two apart and never splice a thought
into a reply.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.core import artifacts, sessions
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.classes import get_class, touch_class
from backend.core.errors import LyraError
from backend.core.profiles import select_active_facts, select_user_facts
from backend.llm.budget import GENERATION_SHARE
from backend.llm.client import stream_chat
from backend.llm.prompts import ChatMode, build_system_prompt, format_context_block
from backend.rag.retrieve import RetrievalResult, RetrievedChunk, retrieve
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

# The four buckets of the context budget table in docs/rag-pipeline.md. They sum to 1.0,
# and an 8192 window divides into 2048 generation, 1229 system, 1638 history, and 3277
# retrieval. The generation share is shared with the background pipelines, which reserve
# the same quarter without assembling any of the other three.
SYSTEM_SHARE = 0.15
HISTORY_SHARE = 0.20
RETRIEVAL_SHARE = 0.40

# The latest exchange survives any budget. A reply to a question whose question is gone
# reads as a non sequitur, which is worse than overrunning an estimate by one exchange.
MINIMUM_HISTORY_MESSAGES = 2

SSE_HEADERS = {
    # Buffering defeats streaming, and both browser caches and reverse proxies buffer by
    # default. `x-accel-buffering` is the nginx opt-out and is ignored elsewhere.
    "cache-control": "no-cache",
    "x-accel-buffering": "no",
}

_UNEXPECTED_ERROR = "Something went wrong while answering. Try again."

# Why a tutor turn may not run, in the words the student needs to act on. The rule itself
# lives in `app_settings.document_text_allowed`; these are its consequences for chat, which
# grounds every reply in retrieved course material and so is bound by the same rule as
# solving, writing, and study. The `no_endpoint` case is normally caught earlier by
# `resolve_tutor_config`, but the mapping is complete so the gate is safe wherever it runs.
_BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then chat.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and a tutor reply has to send it your "
        "course material. Allow that in Settings, then chat."
    ),
}


def _require_document_text_allowed(conn: sqlite3.Connection) -> None:
    """Refuse the turn when document text may not be sent to the configured endpoint.

    A tutor reply is grounded in retrieved course material - ordinary class retrieval, a
    document the question is scoped to, or the solution step a conversation is anchored to -
    so chat is bound by the same locality/acknowledgement rule as solving, writing, and
    study. Checked when the turn opens, before the student's message is persisted and before
    any upstream request, and re-checked on every turn and regeneration: changing the
    endpoint or revoking the acknowledgement takes effect on the very next turn, a refusal
    puts nothing on the wire, and it leaves no orphaned question behind to answer later.

    Raises:
        LyraError: document text may not be sent to the current endpoint.
    """
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(_BLOCKED_MESSAGES.get(blocked, _BLOCKED_MESSAGES[NO_ENDPOINT]))


class SessionCreate(BaseModel):
    """Body of `POST /api/classes/{class_id}/sessions`."""

    title: str | None = None
    # Set when the conversation was opened by clicking a step of a solution. That step is
    # pinned into every turn, and the session is otherwise an ordinary conversation: same
    # composer, same streaming, same place in the sidebar.
    artifact_part_id: int | None = None


class SessionRename(BaseModel):
    """Body of `PATCH /api/sessions/{session_id}`."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A conversation name cannot be blank.")
        return cleaned


class SessionRead(BaseModel):
    """A conversation as the interface sees it.

    Mode is the session module's three-value kind, not the tutor's two: message history
    is shared with the writer, and this shape must be able to describe what it lists.
    """

    id: int
    class_id: int
    title: str | None
    mode: sessions.ChatMode
    artifact_part_id: int | None
    created_at: str


class MessageRead(BaseModel):
    """One persisted message, carrying its reasoning and what retrieval had to leave out."""

    id: int
    session_id: int
    role: Literal["user", "assistant"]
    content: str
    thinking: str
    thinking_ms: int
    retrieval_trimmed: bool
    omitted_document_count: int
    tool_activity: list[dict[str, object]]
    created_at: str


class ChatRequest(BaseModel):
    """Body of `POST /api/sessions/{session_id}/chat`."""

    content: str = Field(min_length=1)
    mode: ChatMode
    document_id: int | None = None

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Message cannot be blank.")
        return cleaned


class RegenerateRequest(BaseModel):
    """Body of `POST /api/sessions/{session_id}/regenerate`.

    It carries no content: the question is the one already in the conversation. Mode is
    sent because the student may have switched Guide to Show precisely in order to ask for
    the same thing a different way.
    """

    mode: ChatMode
    document_id: int | None = None


@dataclass(frozen=True)
class TurnBudget:
    """One turn's context window split into tokens per bucket."""

    generation: int
    system: int
    history: int
    retrieval: int


@dataclass(frozen=True)
class TurnPlan:
    """Which messages this turn answers, and which it is about to replace.

    `superseded` is empty for an ordinary turn and holds the discarded reply on a retry.
    Those messages are excluded from the prompt immediately but deleted only once there is
    a new reply to put in their place.
    """

    user_message_id: int
    superseded: tuple[int, ...] = ()

    @property
    def excluded(self) -> frozenset[int]:
        """Message ids that must not appear in this turn's history."""
        return frozenset({self.user_message_id, *self.superseded})


@dataclass(frozen=True)
class TurnPreparation:
    """Prompt and budget work completed before the blocking retrieval call.

    `anchor` is the step this conversation is about, present only for a session opened
    from a solution. It is pinned rather than retrieved, because the student is looking at
    it: retrieval might or might not surface the exact step they clicked, and "might" is
    not good enough for the subject of the question.
    """

    class_id: int
    system_prompt: str
    history: list[dict[str, object]]
    retrieval_budget: int
    anchor: str | None = None


@dataclass(frozen=True)
class Turn:
    """The assembled prompt plus what retrieval had to say about itself."""

    messages: list[dict[str, str]]
    retrieval: RetrievalResult


def plan_budget(context_window: int) -> TurnBudget:
    """Split a context window into the four buckets of the Stage 7 table.

    The generation reserve is taken off the top and never lent out, so the three prompt
    buckets together are all the prompt can ever occupy. For an 8192 window that is
    2048 reserved and 6144 for system, history, and retrieval.
    """
    return TurnBudget(
        generation=round(context_window * GENERATION_SHARE),
        system=round(context_window * SYSTEM_SHARE),
        history=round(context_window * HISTORY_SHARE),
        retrieval=round(context_window * RETRIEVAL_SHARE),
    )


@router.post(
    "/classes/{class_id}/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_session(class_id: int, payload: SessionCreate, conn: DbConn) -> dict[str, object]:
    # Checked first so an unknown class is a 404 rather than a foreign-key failure.
    get_class(conn, class_id)
    if payload.artifact_part_id is not None:
        # Checked here rather than left to the foreign key, so a stale part id is a 404
        # naming what is missing instead of a 500 out of sqlite3.
        artifacts.get_part(conn, payload.artifact_part_id)
    return sessions.create_session(conn, class_id, payload.title, payload.artifact_part_id)


@router.get("/classes/{class_id}/sessions", response_model=list[SessionRead])
def read_sessions(class_id: int, conn: DbConn) -> list[dict[str, object]]:
    get_class(conn, class_id)
    return sessions.list_sessions(conn, class_id)


@router.get("/sessions/{session_id}/messages", response_model=list[MessageRead])
def read_messages(session_id: int, conn: DbConn) -> list[dict[str, object]]:
    return sessions.list_messages(conn, session_id)


@router.patch("/sessions/{session_id}", response_model=SessionRead)
def rename_session(session_id: int, payload: SessionRename, conn: DbConn) -> dict[str, object]:
    """Name a conversation by hand.

    A conversation is named after its first message, which is a guess at what it turned
    out to be about. Once a class holds a term of them, being able to correct that guess
    is the difference between a list and an archive.
    """
    return sessions.rename_session(conn, session_id, payload.title)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: int, conn: DbConn) -> None:
    sessions.delete_session(conn, session_id)


@router.post("/sessions/{session_id}/chat")
async def send_chat(session_id: int, payload: ChatRequest, conn: DbConn) -> StreamingResponse:
    """Persist the student's message, then stream the reply as SSE.

    Everything that can fail before a byte is written happens here, where it can still
    become an ordinary error response: an unknown session, a blank message, and above all
    a missing tutor endpoint, which the composer renders as the reason it is disabled.
    Once the first frame is out the status line is gone, so every later failure has to
    travel as an `error` frame instead.
    """
    config, plan = await asyncio.to_thread(_open_turn, conn, session_id, payload)
    return StreamingResponse(
        _stream_turn(session_id, payload, config, plan),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/sessions/{session_id}/regenerate")
async def regenerate_chat(
    session_id: int, payload: RegenerateRequest, conn: DbConn
) -> StreamingResponse:
    """Answer the conversation's last question again, replacing the reply it already has.

    A retry is not the same act as asking twice: the reply being retried is removed rather
    than joined by a second one, and the model is never shown its own discarded attempt as
    history. The removal happens at the moment the new reply is written, not when the turn
    opens, so a retry that fails upstream leaves the student with the answer they already
    had instead of nothing at all.
    """
    config, plan, content = await asyncio.to_thread(_open_regeneration, conn, session_id, payload)
    request = ChatRequest(content=content, mode=payload.mode, document_id=payload.document_id)
    return StreamingResponse(
        _stream_turn(session_id, request, config, plan),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _refuse_writer_session(session: dict[str, object]) -> None:
    """The tutor never answers in a writer conversation.

    Sending a tutor turn here would flip the session's mode and put a Socratic reply in
    the middle of a working transcript. Nothing in the interface offers this; the guard
    exists so a stale client or a hand-written request cannot do it either.
    """
    if session["mode"] == sessions.WRITER:
        raise LyraError("That conversation belongs to a draft. Open it from the draft.")


def _open_turn(
    conn: sqlite3.Connection, session_id: int, request: ChatRequest
) -> tuple[TutorConfig, TurnPlan]:
    """Validate the turn and persist the user's message before any streaming starts.

    Raises:
        NotFoundError: no session carries that id.
        ConfigurationError: no tutor endpoint is configured.
    """
    session = sessions.get_session(conn, session_id)
    _refuse_writer_session(session)
    config = resolve_tutor_config(conn)
    # Before the question is stored and before any retrieval or upstream call, so an
    # unacknowledged remote endpoint refuses the turn without persisting an orphaned
    # question and without a byte of course material leaving the machine.
    _require_document_text_allowed(conn)
    # Persisted on the session, not just used for this turn, so the toggle survives a
    # reload and the next turn continues in the mode the student picked.
    sessions.set_session_mode(conn, session_id, request.mode)
    # Before the message is stored, so "first message" means this one.
    sessions.set_session_title_if_unset(conn, session_id, request.content)
    user_message_id = sessions.add_message(conn, session_id, "user", request.content)
    touch_class(conn, int(session["class_id"]))
    return config, TurnPlan(user_message_id=user_message_id)


def _open_regeneration(
    conn: sqlite3.Connection, session_id: int, request: RegenerateRequest
) -> tuple[TutorConfig, TurnPlan, str]:
    """Plan a retry of the conversation's last question.

    Nothing is deleted here. The reply being retried is only named, so that a turn which
    never produces a replacement leaves the conversation exactly as it found it.

    Raises:
        NotFoundError: no session carries that id, or it holds no question yet.
        ConfigurationError: no tutor endpoint is configured.
    """
    session = sessions.get_session(conn, session_id)
    _refuse_writer_session(session)
    config = resolve_tutor_config(conn)
    # Re-checked here so a retry is gated exactly like a fresh turn: the endpoint may have
    # gone remote, or the acknowledgement been revoked, since the question was first asked.
    _require_document_text_allowed(conn)
    question = sessions.last_user_message(conn, session_id)
    user_message_id = int(question["id"])
    superseded = tuple(
        int(message["id"])
        for message in sessions.list_messages(conn, session_id)
        if int(message["id"]) > user_message_id
    )
    sessions.set_session_mode(conn, session_id, request.mode)
    touch_class(conn, int(session["class_id"]))
    plan = TurnPlan(user_message_id=user_message_id, superseded=superseded)
    return config, plan, str(question["content"])


async def _stream_turn(
    session_id: int,
    request: ChatRequest,
    config: TutorConfig,
    plan: TurnPlan,
) -> AsyncIterator[str]:
    """Stream one turn and persist the assistant's message however the turn ends.

    The connection is opened here rather than injected, because this generator runs after
    the request-scoped dependency has already been closed.
    """
    conn = connect()
    received: list[str] = []
    thought: list[str] = []
    thinking_ms = 0
    retrieval = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)
    try:
        yield _frame(type="start", message_id=plan.user_message_id)
        yield _frame(type="status", stage="prompt_processing")
        preparation = await asyncio.to_thread(
            _prepare_turn, conn, session_id, request, config, plan
        )

        yield _frame(type="status", stage="reviewing_documents")
        result = await asyncio.to_thread(
            retrieve,
            conn,
            preparation.class_id,
            request.content,
            preparation.retrieval_budget,
            document_id=request.document_id,
        )
        retrieval = result
        turn = _build_turn(preparation, request, result)
        if retrieval.trimmed:
            yield _frame(
                type="notice",
                retrieval_trimmed=True,
                omitted_document_count=retrieval.omitted_document_count,
            )

        yield _frame(type="status", stage="composing_answer")
        # Thinking is timed from its first token to the answer's first token. Measuring it
        # here rather than in the browser keeps it out of the render loop and lets it be
        # stored, so a reopened conversation still says how long the model took.
        thinking_started = 0.0
        async for delta in stream_chat(
            config.endpoint_url, config.api_key, config.model, turn.messages
        ):
            if delta.channel == "reasoning":
                if not thought:
                    thinking_started = time.monotonic()
                thought.append(delta.text)
                yield _frame(type="reasoning", text=delta.text)
            else:
                if thinking_started and not received:
                    thinking_ms = round((time.monotonic() - thinking_started) * 1000)
                received.append(delta.text)
                yield _frame(type="token", text=delta.text)
        # A turn that thought and then said nothing still spent that time thinking.
        if thinking_started and not received:
            thinking_ms = round((time.monotonic() - thinking_started) * 1000)

        message_id = _commit_reply(
            conn, session_id, plan, received, thought, retrieval, thinking_ms
        )
        yield _frame(type="done", message_id=message_id)
    except (asyncio.CancelledError, GeneratorExit):
        # The reader went away mid-answer. Keep what did arrive, so the conversation is
        # not left holding a question with no reply, then let the cancellation continue.
        # A turn cut off while still thinking has a thought and no answer, and that is
        # worth keeping too: it is the only record of what the model was doing.
        if received or thought:
            _commit_reply(conn, session_id, plan, received, thought, retrieval, thinking_ms)
        raise
    except LyraError as exc:
        # Deliberately not persisted: a turn that failed partway is a turn to retry, and
        # a stored fragment would read on reload as if it were the whole answer.
        yield _frame(type="error", message=exc.message)
    except Exception:
        logger.exception("Chat turn failed for session %s", session_id)
        yield _frame(type="error", message=_UNEXPECTED_ERROR)
    finally:
        conn.close()


def _prepare_turn(
    conn: sqlite3.Connection,
    session_id: int,
    request: ChatRequest,
    config: TutorConfig,
    plan: TurnPlan,
) -> TurnPreparation:
    """Prepare system instructions, history, and the retrieval budget for one turn."""
    class_id = int(sessions.get_session(conn, session_id)["class_id"])
    budget = plan_budget(config.context_window)

    system_prompt = build_system_prompt(
        request.mode, select_user_facts(conn), select_active_facts(conn, class_id)
    )
    anchor = sessions.anchored_context(conn, session_id)
    # The system prompt is instruction, not material, so it is never trimmed. An overrun
    # is charged to retrieval rather than to the generation reserve. The pinned step is
    # charged the same way: it is the subject of the question, so it is never the thing
    # that gets dropped to make room.
    system_overrun = max(
        0, estimate_tokens(system_prompt) + estimate_tokens(anchor or "") - budget.system
    )

    # The question itself is appended last, and a reply being retried is on its way out, so
    # neither belongs in the history the model is shown.
    excluded = plan.excluded
    earlier = [
        message
        for message in sessions.list_messages(conn, session_id)
        if int(message["id"]) not in excluded
    ]
    history, history_used = trim_history(earlier, budget.history)
    # Unused history budget is lent to retrieval. The reverse never happens: retrieval
    # cannot borrow history's share, and neither may touch the generation reserve.
    retrieval_budget = max(0, budget.retrieval + budget.history - history_used - system_overrun)
    return TurnPreparation(
        class_id=class_id,
        system_prompt=system_prompt,
        history=history,
        retrieval_budget=retrieval_budget,
        anchor=anchor,
    )


def _build_turn(
    preparation: TurnPreparation, request: ChatRequest, result: RetrievalResult
) -> Turn:
    """Assemble the final prompt after retrieval has returned."""
    context_block = format_context_block([_context_entry(chunk) for chunk in result.chunks])
    # The anchor sits above retrieved material, because it is what the question is about
    # and the retrieval is background for it.
    system_content = "\n\n".join(
        block for block in (preparation.system_prompt, preparation.anchor, context_block) if block
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    messages += [
        {"role": str(message["role"]), "content": str(message["content"])}
        for message in preparation.history
    ]
    messages.append({"role": "user", "content": request.content})
    return Turn(messages=messages, retrieval=result)


def trim_history(
    messages: list[dict[str, object]], budget_tokens: int
) -> tuple[list[dict[str, object]], int]:
    """Keep the newest messages that fit, dropping oldest first.

    Returns:
        The kept messages in chronological order, and the tokens they cost.
    """
    kept: list[dict[str, object]] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(str(message["content"]))
        if used + cost > budget_tokens and len(kept) >= MINIMUM_HISTORY_MESSAGES:
            break
        used += cost
        kept.append(message)
    kept.reverse()
    return kept, used


def _context_entry(chunk: RetrievedChunk) -> dict[str, object]:
    """The dict shape `format_context_block` labels a retrieved chunk from."""
    return {
        "content": chunk.content,
        "filename": chunk.filename,
        "page_number": chunk.page_number,
        "section_title": chunk.section_title,
        "section_path": chunk.section_path,
        "section_number": chunk.section_number,
        "problem_number": chunk.problem_number,
    }


def _commit_reply(
    conn: sqlite3.Connection,
    session_id: int,
    plan: TurnPlan,
    received: list[str],
    thought: list[str],
    retrieval: RetrievalResult,
    thinking_ms: int = 0,
) -> int:
    """Swap in this turn's reply, dropping whatever it supersedes.

    The order matters and is the whole point of deferring the delete: a reply the student
    can already read is only removed once there is a new one to take its place, so a retry
    that dies upstream costs them nothing.
    """
    if plan.superseded:
        sessions.delete_messages_after(conn, session_id, plan.user_message_id)
    return sessions.add_message(
        conn,
        session_id,
        "assistant",
        "".join(received),
        retrieval_trimmed=retrieval.trimmed,
        omitted_document_count=retrieval.omitted_document_count,
        thinking="".join(thought),
        thinking_ms=thinking_ms,
    )


def _frame(**payload: object) -> str:
    """One SSE frame: a single JSON object on a `data:` line, then the blank line."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
