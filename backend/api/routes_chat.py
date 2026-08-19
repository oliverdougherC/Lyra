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
from starlette.types import Receive, Scope, Send

from backend.core import artifacts, sessions
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorAccess,
    TutorConfig,
    resolve_tutor_access,
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

# An absolute ceiling on one question, measured in Unicode characters (code points, not
# bytes), so the boundary is the same whatever alphabet the student writes in. It is a
# sanity limit, not the working limit: a normal question is a fraction of this, and the
# context-window fit check below is what a long-but-reasonable paste actually meets. The
# ceiling exists so a runaway paste cannot be persisted or budgeted at arbitrary size,
# independent of whatever context window the endpoint is configured with. At four chars a
# token this is about 4000 tokens, which already exceeds the question share of any window
# small enough to matter.
MAX_QUESTION_CHARS = 16_000

SSE_HEADERS = {
    # Buffering defeats streaming, and both browser caches and reverse proxies buffer by
    # default. `x-accel-buffering` is the nginx opt-out and is ignored elsewhere.
    "cache-control": "no-cache",
    "x-accel-buffering": "no",
}

_UNEXPECTED_ERROR = "Something went wrong while answering. Try again."

# Why a tutor turn may not run, in the words the student needs to act on. The rule itself
# lives in `app_settings` (`document_text_allowed`); these are its consequences for chat,
# which grounds every reply in retrieved course material and so is bound by the same rule as
# solving, writing, and study. The `no_endpoint` case is normally caught earlier by
# resolving the config, but the mapping is complete so the gate is safe wherever it runs.
_BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then chat.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and a tutor reply has to send it your "
        "course material. Allow that in Settings, then chat."
    ),
}


def require_document_allowed(access: TutorAccess) -> None:
    """Refuse the turn when document text may not be sent to the *resolved* endpoint.

    Takes the same snapshot the turn will be sent through, so the endpoint this authorizes is
    exactly the endpoint the reply goes to - a settings change cannot slip between the check
    and the send to authorize a local endpoint while the turn leaves for a remote one.

    A tutor reply is grounded in retrieved course material - ordinary class retrieval, a
    document the question is scoped to, or the solution step a conversation is anchored to -
    so chat is bound by the same locality/acknowledgement rule as solving, writing, and
    study. The class-agent route shares this gate for the same reason: its turns carry the
    conversation history and, on later tool-loop rounds, workspace file contents and tool
    results. Applied when the turn opens, before the student's message is persisted and before
    any upstream request, and re-derived on every turn and regeneration: changing the
    endpoint or revoking the acknowledgement takes effect on the very next turn, a refusal
    puts nothing on the wire, and it leaves no orphaned question behind to answer later.

    Raises:
        LyraError: document text may not be sent to the resolved endpoint.
    """
    if access.document_block is not None:
        raise LyraError(
            _BLOCKED_MESSAGES.get(access.document_block, _BLOCKED_MESSAGES[NO_ENDPOINT])
        )


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
    """Body of `POST /api/sessions/{session_id}/chat`.

    `content` is capped at `MAX_QUESTION_CHARS`. The cap is on characters, so an
    over-length paste is rejected as a 422 before it is stripped, persisted, or budgeted,
    whatever the configured context window. The narrower, window-relative limit lives in
    `_require_turn_fits`.
    """

    content: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
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
class TurnInput:
    """Validated input used after the HTTP persistence boundary.

    Fresh messages reach this type only after `ChatRequest` has enforced the absolute
    character cap. Regeneration may construct it from an already-persisted question, so a
    legacy row is governed by the context-window fit check rather than retroactively by a
    validator introduced for new messages.
    """

    content: str
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
class HistoryMessage:
    """The immutable part of a persisted message that can enter a prompt."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class TurnCost:
    """The unavoidable cost of one turn, and the room it leaves for optional context.

    Assembled once, before a fresh message is persisted or a regeneration mutates the
    conversation, from the same estimator the prompt is later measured with. Everything
    counted here is material the turn cannot trim away: the generation reserve, the system
    prompt, the pinned solution step, the current question, and the newest history
    `trim_history` is obliged to keep whatever the budget.
    `_require_turn_fits` refuses the turn when those do not fit the window; `_prepare_turn`
    spends whatever room is left on optional older history and retrieval. Both read this one
    object, so the inequality the preflight refuses on is the inequality preparation obeys -
    they cannot drift into disagreeing about what fits.

    Attributes:
        context_window: The endpoint's configured window, the ceiling the turn must fit.
        budget: The window split into the four Stage 7 buckets.
        class_id: The class this session belongs to, resolved here so `_prepare_turn` need
            not read it again.
        system_prompt: The assembled system instructions for this turn's mode.
        anchor: The pinned solution step, or None for an ordinary conversation.
        earlier: Immutable history candidates in chronological order, already stripped of
            the current question and any superseded reply.
        question_tokens: The estimated cost of the current question, appended and never
            trimmed.
    """

    context_window: int
    budget: TurnBudget
    class_id: int
    system_prompt: str
    anchor: str | None
    earlier: tuple[HistoryMessage, ...]
    question_tokens: int

    @property
    def system_tokens(self) -> int:
        """The system message before retrieval: instruction and pinned subject, never trimmed.

        Measured on the same joined string `_build_turn` assembles from the system prompt and
        the anchor, not on the two estimated apart, so the cost charged here is the cost the
        prompt actually carries - the join's separator cannot make the assembled message a
        token or two larger than the preflight allowed for.
        """
        return estimate_tokens(_join_blocks(self.system_prompt, self.anchor))

    @property
    def mandatory_history_tokens(self) -> int:
        """The newest messages `trim_history` keeps whatever the budget, charged in full.

        `trim_history` retains at least `MINIMUM_HISTORY_MESSAGES`, so the newest that many
        messages are as non-negotiable as the question: their cost is charged up front rather
        than left to overflow the window after retrieval has already been clamped to nothing.
        """
        kept = self.earlier[-MINIMUM_HISTORY_MESSAGES:]
        return sum(estimate_tokens(message.content) for message in kept)

    @property
    def reserved(self) -> int:
        """Every token the turn cannot avoid spending, measured against the window."""
        return (
            self.budget.generation
            + self.system_tokens
            + self.mandatory_history_tokens
            + self.question_tokens
        )

    @property
    def prompt_room(self) -> int:
        """Window left for history and retrieval once the reserve, system, and question go.

        Non-negative exactly when the turn fits, and then it is the room the mandatory
        history and everything optional after it must share.
        """
        return (
            self.context_window - self.budget.generation - self.system_tokens - self.question_tokens
        )

    @property
    def fits(self) -> bool:
        """Whether the reserve plus all non-trimmable material fits the window."""
        return self.reserved <= self.context_window


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


def _plan_turn_cost(
    conn: sqlite3.Connection,
    session_id: int,
    mode: ChatMode,
    question: str,
    excluded: frozenset[int],
    config: TutorConfig,
) -> TurnCost:
    """Cost a turn before it is persisted: the budget, the fixed prompt material, and the
    newest history that cannot be trimmed.

    Called once by the preflight; the immutable result is then carried into preparation, so
    a single set of token estimates and fixed prompt inputs drives both. Reads only what a
    turn already reads - the session's class, the profile facts, the pinned step, and the
    prior messages - and touches no settings row, so it never resolves the endpoint a second
    time behind the consent snapshot the caller already took.
    """
    class_id = int(sessions.get_session(conn, session_id)["class_id"])
    system_prompt = build_system_prompt(
        mode, select_user_facts(conn), select_active_facts(conn, class_id)
    )
    anchor = sessions.anchored_context(conn, session_id)
    earlier = tuple(
        HistoryMessage(role=message["role"], content=str(message["content"]))
        for message in sessions.list_messages(conn, session_id)
        if int(message["id"]) not in excluded
    )
    return TurnCost(
        context_window=config.context_window,
        budget=plan_budget(config.context_window),
        class_id=class_id,
        system_prompt=system_prompt,
        anchor=anchor,
        earlier=earlier,
        question_tokens=estimate_tokens(question),
    )


def _require_turn_fits(cost: TurnCost) -> None:
    """Refuse a turn whose non-trimmable material cannot fit the configured window.

    The current question is appended to every turn and never trimmed, and it does not stand
    alone: the generation reserve, the system prompt, the pinned solution step, and the
    newest history `trim_history` always keeps are all non-negotiable too. When their sum
    exceeds the window, no amount of trimming history or retrieval can bring the prompt back
    under it - `trim_history` would keep its mandatory pair regardless, and retrieval only
    clamps to zero - so the turn could reach the endpoint only by overrunning the window.
    It is refused here instead, before the question is persisted and before any upstream
    call, the way a missing endpoint is.

    Raises:
        LyraError: the turn cannot fit the window with the reserves intact.
    """
    if not cost.fits:
        raise LyraError(
            "That message is too long to answer within the tutor's context window. "
            "Shorten it and send it again."
        )


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


class TurnStreamingResponse(StreamingResponse):
    """A streaming response that owns the release of its session's turn claim.

    The claim is taken before the route returns, but the stream generator's own `finally`
    can run only if the body iterator is ever started - and Python closes a never-started
    generator without executing a single line of it. Between the route returning and the
    first frame sits a real window: a transport that dies immediately, or a cancellation
    delivered before the body task first resumes the generator, ends the response with the
    generator unstarted and would leave the session claimed for the life of the process.

    So release also lives here, in a `finally` around the entire ASGI send cycle, which
    Starlette enters unconditionally once the route returns: streamed to completion,
    failed mid-frame, cancelled before the first frame - every ending passes through it.
    `end_turn` is idempotent and token-owned, so this and the generator's release can
    never double-free, and neither can free a claim a newer turn has since taken.
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


def _turn_response(
    session_id: int,
    turn_token: int,
    stream: AsyncIterator[str],
) -> TurnStreamingResponse:
    """Wrap a claimed turn's stream so the claim cannot outlive the response.

    If even constructing the response fails, the claim is released here - after
    `_open_turn`/`_open_regeneration` return, this is the only failure point left between
    the claim and the response owning it.
    """
    try:
        return TurnStreamingResponse(
            session_id,
            turn_token,
            stream,
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    except BaseException:
        sessions.end_turn(session_id, turn_token)
        raise


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


def _finish_opening(opener: asyncio.Future, session_id: int, turn_token: int) -> None:
    """Release an abandoned claim, but never while its opener may still be running.

    Called when the route can no longer use the opener's result: a cancellation landing
    on the `await`, or the opener itself failing. Cancelling the route does not stop a
    thread `asyncio.to_thread` has already started, so the claim must stay held until
    that worker has definitely returned - releasing earlier would let a second request
    claim the session and overlap the still-running worker's reads and writes. If the
    opener has already finished, nothing can still be mutating the turn and the claim is
    released here, before the caller re-raises. Otherwise the release is attached to the
    opener's completion, the one point at which the worker has provably stopped.

    `end_turn` is token-owned and idempotent, so this can never free a claim a newer
    turn has since taken, and an opener that failed and already released internally
    makes this a no-op.
    """

    def _release(task: asyncio.Future) -> None:
        if not task.cancelled() and task.exception() is not None:
            # Retrieved deliberately: an abandoned opener that failed after the caller
            # stopped listening would otherwise be logged as a never-retrieved task
            # exception. Its release already ran inside the opener itself.
            logger.debug("Abandoned turn opener for session %s failed", session_id)
        sessions.end_turn(session_id, turn_token)

    if opener.done():
        _release(opener)
    else:
        opener.add_done_callback(_release)


@router.post("/sessions/{session_id}/chat")
async def send_chat(session_id: int, payload: ChatRequest, conn: DbConn) -> StreamingResponse:
    """Persist the student's message, then stream the reply as SSE.

    Everything that can fail before a byte is written happens here, where it can still
    become an ordinary error response: an unknown session, a blank message, and above all
    a missing tutor endpoint, which the composer renders as the reason it is disabled.
    Once the first frame is out the status line is gone, so every later failure has to
    travel as an `error` frame instead.
    """
    request = TurnInput(content=payload.content, mode=payload.mode, document_id=payload.document_id)
    # The claim is taken here, synchronously, before the coroutine's first suspension
    # point: cancellation can only land on an `await`, so from the moment `begin_turn`
    # returns this coroutine owns the token with no window in which a worker could hold
    # a claim nobody knows about. The opener then borrows the claimed session; release
    # on any failure goes through `_finish_opening`, which never frees the claim while
    # the worker might still be mutating the turn.
    turn_token = sessions.begin_turn(session_id)
    opener = asyncio.ensure_future(
        asyncio.to_thread(_open_turn, conn, session_id, request, turn_token)
    )
    try:
        config, plan, cost = await asyncio.shield(opener)
    except BaseException:
        _finish_opening(opener, session_id, turn_token)
        raise
    return _turn_response(
        session_id, turn_token, _stream_turn(session_id, request, config, plan, cost, turn_token)
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
    # Claimed synchronously before the first `await`, exactly as in `send_chat`: the
    # coroutine owns the token before cancellation can be delivered, and release on
    # failure waits for the opener worker to stop before freeing the session.
    turn_token = sessions.begin_turn(session_id)
    opener = asyncio.ensure_future(
        asyncio.to_thread(_open_regeneration, conn, session_id, payload, turn_token)
    )
    try:
        config, plan, request, cost = await asyncio.shield(opener)
    except BaseException:
        _finish_opening(opener, session_id, turn_token)
        raise
    return _turn_response(
        session_id, turn_token, _stream_turn(session_id, request, config, plan, cost, turn_token)
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
    conn: sqlite3.Connection, session_id: int, request: TurnInput, turn_token: int
) -> tuple[TutorConfig, TurnPlan, TurnCost]:
    """Validate the turn and persist the user's message before any streaming starts.

    Runs in a worker thread against a session the route already claimed: `begin_turn`
    happens in the route coroutine, before its first suspension point, so an overlapping
    send or regeneration - two tabs, a duplicate submit, a direct API caller - was
    already refused with a deterministic 409 before this function starts, and there is
    no instant at which this worker holds a claim its caller does not know about. The
    claim is released by `_stream_turn` however the turn ends, immediately below if
    opening the turn fails, or by `_finish_opening` once this worker has returned if the
    caller was cancelled while it ran.

    Raises:
        NotFoundError: no session carries that id.
        LyraError: no tutor endpoint is configured, document text may not be sent to the
            configured one (an unacknowledged remote endpoint), or the question cannot fit
            the configured context window beside the reserves it may not trim.
    """
    try:
        session = sessions.get_session(conn, session_id)
        _refuse_writer_session(session)
        # One snapshot for the endpoint and its consent, so the endpoint authorized below
        # is the exact endpoint this turn is later streamed to. Taken before the question
        # is stored and before any retrieval or upstream call, so an unacknowledged remote
        # endpoint refuses the turn without persisting an orphaned question and without a
        # byte of course material leaving the machine.
        access = resolve_tutor_access(conn)
        require_document_allowed(access)
        config = access.config
        # The privacy gate above proves the endpoint is one document text may reach; this
        # proves the turn fits it. Both run before the message is stored and before any
        # retrieval or upstream call, so a question too large for the window - once the
        # generation reserve, system prompt, pinned step, and the history Lyra always
        # keeps are set aside - refuses cleanly instead of being persisted and then
        # forcing context to be truncated past the window. The current question is not yet
        # persisted, so `excluded` is empty and the history it costs is exactly the prior
        # conversation.
        cost = _plan_turn_cost(conn, session_id, request.mode, request.content, frozenset(), config)
        _require_turn_fits(cost)
        # Persisted on the session, not just used for this turn, so the toggle survives a
        # reload and the next turn continues in the mode the student picked.
        sessions.set_session_mode(conn, session_id, request.mode)
        # Before the message is stored, so "first message" means this one.
        sessions.set_session_title_if_unset(conn, session_id, request.content)
        user_message_id = sessions.add_message(conn, session_id, "user", request.content)
        sessions.bind_turn(session_id, turn_token, user_message_id)
        touch_class(conn, int(session["class_id"]))
    except BaseException:
        # A turn that never starts streaming must not hold the session: the refusal is the
        # end of this turn, and the next request gets a fresh claim.
        sessions.end_turn(session_id, turn_token)
        raise
    return config, TurnPlan(user_message_id=user_message_id), cost


def _open_regeneration(
    conn: sqlite3.Connection, session_id: int, request: RegenerateRequest, turn_token: int
) -> tuple[TutorConfig, TurnPlan, TurnInput, TurnCost]:
    """Plan a retry of the conversation's last question.

    Nothing is deleted here. The reply being retried is only named, so that a turn which
    never produces a replacement leaves the conversation exactly as it found it.

    The claim - taken by the route before this worker starts, exactly as for
    `_open_turn` - is what makes that naming trustworthy: the last question and the
    superseded suffix are read while this turn holds the session, so no concurrent send
    can slip a newer question in between the read and the eventual replacement. The
    replacement itself then deletes only the ids named here, so even a path around the
    claim could not take a newer turn as collateral damage.

    Raises:
        NotFoundError: no session carries that id, or it holds no question yet.
        LyraError: no tutor endpoint is configured, document text may not be sent to the
            configured one (an unacknowledged remote endpoint), or the window has been
            reconfigured too small for the question to fit beside the reserves.
    """
    try:
        session = sessions.get_session(conn, session_id)
        _refuse_writer_session(session)
        # A fresh snapshot, so a retry is gated exactly like a first turn against the
        # endpoint it will actually use: the endpoint may have gone remote, or the
        # acknowledgement been revoked, since the question was first asked.
        access = resolve_tutor_access(conn)
        require_document_allowed(access)
        config = access.config
        question = sessions.last_user_message(conn, session_id)
        user_message_id = int(question["id"])
        sessions.bind_turn(session_id, turn_token, user_message_id)
        superseded = tuple(
            int(message["id"])
            for message in sessions.list_messages(conn, session_id)
            if int(message["id"]) > user_message_id
        )
        plan = TurnPlan(user_message_id=user_message_id, superseded=superseded)
        turn_input = TurnInput(
            content=str(question["content"]),
            mode=request.mode,
            document_id=request.document_id,
        )
        # Gated exactly like a fresh turn: the window may have been reconfigured smaller
        # since the question was first asked, and a retry must not send what a first turn
        # would not. Checked before anything is mutated and before any upstream call, and
        # since the reply being retried is deleted only when its replacement is written, a
        # refusal here leaves the existing answer untouched. The question is already
        # persisted, so it and any superseded reply are excluded from the history it is
        # charged against.
        cost = _plan_turn_cost(
            conn,
            session_id,
            turn_input.mode,
            turn_input.content,
            plan.excluded,
            config,
        )
        _require_turn_fits(cost)
        sessions.set_session_mode(conn, session_id, request.mode)
        touch_class(conn, int(session["class_id"]))
    except BaseException:
        sessions.end_turn(session_id, turn_token)
        raise
    return config, plan, turn_input, cost


async def _stream_turn(
    session_id: int,
    request: TurnInput,
    config: TutorConfig,
    plan: TurnPlan,
    cost: TurnCost,
    turn_token: int,
) -> AsyncIterator[str]:
    """Stream one turn and persist the assistant's message however the turn ends.

    The connection is opened here rather than injected, because this generator runs after
    the request-scoped dependency has already been closed. It is opened *inside* the
    claim-releasing structure, deliberately: the claim was taken back in the route,
    before `_open_turn` / `_open_regeneration` ran, so from the first statement here
    there is nothing left between
    a failure and a permanently wedged session except the `finally` below. Release is
    therefore the outermost cleanup of the whole generator - it runs whether the
    connection failed to open (before any frame), the stream failed or was cancelled
    mid-reply, or the connection failed to close after the last frame - and it is
    idempotent alongside `TurnStreamingResponse`'s release, which covers the one ending
    this generator cannot see: a response whose body is never started at all.
    """
    received: list[str] = []
    thought: list[str] = []
    thinking_ms = 0
    retrieval = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        yield _frame(type="start", message_id=plan.user_message_id)
        yield _frame(type="status", stage="prompt_processing")
        preparation = await asyncio.to_thread(_prepare_turn, cost)

        yield _frame(type="status", stage="reviewing_documents")
        result = await asyncio.to_thread(
            retrieve,
            conn,
            preparation.class_id,
            request.content,
            preparation.retrieval_budget,
            document_id=request.document_id,
        )
        result = _fit_retrieval_to_prompt(preparation, result)
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
        if conn is not None and (received or thought):
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
        # Nested so a connection that fails to close still releases the claim: the close
        # is bookkeeping, the release is what keeps the session usable.
        try:
            if conn is not None:
                conn.close()
        finally:
            sessions.end_turn(session_id, turn_token)


def _prepare_turn(
    cost: TurnCost,
) -> TurnPreparation:
    """Prepare system instructions, history, and the retrieval budget for one turn.

    Reads the same `TurnCost` the preflight refused on, so an accepted turn's assembled
    prompt provably fits the window: the mandatory pieces already fit (`_require_turn_fits`),
    and everything allocated here is drawn from the room they leave behind.
    """
    budget = cost.budget

    # History keeps its own share, with the question charged against it first so a long
    # question shrinks history before anything else, but never more than the room the fixed
    # material actually leaves. That second bound only binds when the system prompt or pinned
    # step is itself outsized: then history shrinks toward its mandatory pair too, rather than
    # holding its full share and pushing the prompt past the window once retrieval has already
    # clamped to nothing.
    history_budget = max(0, min(budget.history - cost.question_tokens, cost.prompt_room))
    history, history_used = trim_history(
        [{"role": message.role, "content": message.content} for message in cost.earlier],
        history_budget,
    )
    # Retrieval spends only what the window still holds once the generation reserve, the
    # system prompt, the pinned step, the question, and the history actually kept are all set
    # aside. Unused history budget is lent to retrieval this way; the reverse never happens,
    # and neither may touch the generation reserve. This is `_require_turn_fits`'s inequality
    # rearranged, so `system + history + question + retrieval + generation <= context_window`
    # holds by construction for every accepted turn.
    retrieval_budget = max(0, cost.prompt_room - history_used)
    return TurnPreparation(
        class_id=cost.class_id,
        system_prompt=cost.system_prompt,
        history=history,
        retrieval_budget=retrieval_budget,
        anchor=cost.anchor,
    )


def _join_blocks(*blocks: str | None) -> str:
    """Join the non-empty prompt blocks with the blank line that separates them.

    The one place the system message's shape is defined, so `_build_turn` (which assembles
    it) and `TurnCost` (which budgets it) measure the same string and cannot disagree about
    what the separators cost.
    """
    return "\n\n".join(block for block in blocks if block)


def _fit_retrieval_to_prompt(
    preparation: TurnPreparation, result: RetrievalResult
) -> RetrievalResult:
    """Keep the ranked retrieval prefix whose rendered block fits its exact remainder.

    Retrieval ranks and initially trims chunk bodies against `retrieval_budget`. The prompt
    also labels every kept chunk with its source and adds a context heading, so this final
    boundary check charges that formatting rather than letting it sit outside the window.
    Chunks are removed from the end, preserving the same highest-ranked-first policy.
    """
    base_system = _join_blocks(preparation.system_prompt, preparation.anchor)
    base_tokens = estimate_tokens(base_system)
    chunks = result.chunks
    kept_count = len(chunks)
    while kept_count:
        context_block = format_context_block(
            [_context_entry(chunk) for chunk in chunks[:kept_count]]
        )
        rendered_tokens = estimate_tokens(_join_blocks(base_system, context_block)) - base_tokens
        if rendered_tokens <= preparation.retrieval_budget:
            break
        kept_count -= 1

    if kept_count == len(chunks):
        return result

    dropped = chunks[kept_count:]
    newly_omitted = frozenset(chunk.document_id for chunk in dropped)
    omitted_document_ids = result.omitted_document_ids | newly_omitted
    if result.omitted_document_ids:
        omitted_document_count = len(omitted_document_ids)
    else:
        # Hand-built results from integrations predating the id set may carry only a count.
        # `max` avoids inventing duplicates when their identities are unavailable; live
        # retrieval always supplies the exact set above.
        omitted_document_count = max(result.omitted_document_count, len(newly_omitted))
    return RetrievalResult(
        chunks=chunks[:kept_count],
        trimmed=result.trimmed or len(dropped) * 2 > len(chunks),
        omitted_document_count=omitted_document_count,
        omitted_document_ids=omitted_document_ids,
    )


def _build_turn(preparation: TurnPreparation, request: TurnInput, result: RetrievalResult) -> Turn:
    """Assemble the final prompt after retrieval has returned."""
    context_block = format_context_block([_context_entry(chunk) for chunk in result.chunks])
    # The anchor sits above retrieved material, because it is what the question is about
    # and the retrieval is background for it.
    system_content = _join_blocks(preparation.system_prompt, preparation.anchor, context_block)

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
    that dies upstream costs them nothing. The delete names exact message ids - the ones
    the plan observed when the turn opened - never "everything after the question", so a
    newer independent turn can never be collateral damage of a retry, whatever the timing.
    """
    if plan.superseded:
        sessions.delete_messages(conn, session_id, plan.superseded)
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
