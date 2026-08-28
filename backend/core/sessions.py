"""Chat session and message management.

Chat is session-scoped rather than class-scoped so a conversation has one unambiguous
owner: messages cascade from the session, and deleting a session takes its history with
it. The Guide/Show mode is stored on the session rather than read from each request, so
the toggle survives a reload and the next turn continues in the mode the user chose.

The third mode, `writer`, is the draft workspace's conversation. It rides everything
here unchanged - transcript, title, deletion, the body-part anchor - and differs only in
who answers: the writer's tool-using turn rather than the tutor's. The tutor routes
refuse writer sessions and the sidebar does not list them, so the two surfaces share
storage without sharing a doorway.
"""

import itertools
import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Literal

from backend.core.errors import ConflictError, NotFoundError
from backend.llm.prompts import format_step_context

ChatMode = Literal["guide", "show", "writer"]
MessageRole = Literal["user", "assistant"]

MODES: tuple[str, ...] = ("guide", "show", "writer")
WRITER = "writer"

BUSY_MESSAGE = (
    "This conversation is already answering. Wait for the reply to finish, then try again."
)

# Title length: long enough to tell two conversations apart in a 260px rail, short enough
# that most titles survive without an ellipsis.
_TITLE_MAX_CHARS = 48
_TITLE_MIN_CHARS = 24

_MESSAGE_SQL = """
select id, session_id, role, content, thinking, thinking_ms, retrieval_trimmed,
       omitted_document_count, tool_activity, created_at
from messages
where session_id = ?
order by id
"""


_SESSION_COLUMNS = "id, class_id, title, mode, artifact_part_id, created_at"


# --- The per-session turn claim ------------------------------------------------------
#
# Tutor chat correctness must not depend on the frontend never overlapping requests: two
# tabs, a duplicate submit, a retry racing a new question, or a direct API caller can all
# aim two mutating turns at one session at once. The claim below is the server-side rule
# that makes overlap impossible rather than merely unlikely: at most one mutating or
# generating turn holds a session at a time, a second one is refused with a deterministic
# 409 before it persists or sends anything, and the claim is released however the turn
# ends - completion, upstream failure, cancellation, or client disconnect.
#
# The registry is in-memory on purpose. Lyra runs one backend process (the in-memory
# ingestion queue and the launcher's ownership model already assume it), so process memory
# is exactly the lifetime an in-flight turn has: a turn cannot outlive the process that is
# streaming it, and a restart therefore cannot leave a stale claim behind to wedge the
# session - the failure mode a persisted marker would have to reconcile away at startup.
#
# The claim serializes; it does not own the destructive step. Regeneration deletes only
# the message ids its plan named when the turn opened (`delete_messages`), so even a
# hypothetical path around the claim could not take a newer turn as collateral damage.


@dataclass(frozen=True)
class TurnClaim:
    """The identity of the one in-flight turn a session may have.

    `token` is unique for the process lifetime and is what `end_turn` must present, so a
    stale release can never free a claim it does not own. `user_message_id` is the
    question this turn answers, bound once it is known: None for a fresh send between
    claiming and persisting the message, then the persisted id; for a regeneration, the
    existing question being re-answered.
    """

    token: int
    user_message_id: int | None = None


_turns_lock = threading.Lock()
_active_turns: dict[int, TurnClaim] = {}
_turn_tokens = itertools.count(1)


def begin_turn(session_id: int) -> int:
    """Claim a session's single in-flight turn slot, or refuse deterministically.

    Called before anything is persisted and before any upstream request, so the refused
    turn leaves no trace: no orphaned question, no title claimed, nothing on the wire.

    Returns:
        The claim token `bind_turn` and `end_turn` require.

    Raises:
        ConflictError: another turn already holds this session (409).
    """
    with _turns_lock:
        if session_id in _active_turns:
            raise ConflictError(BUSY_MESSAGE)
        token = next(_turn_tokens)
        _active_turns[session_id] = TurnClaim(token=token)
        return token


def bind_turn(session_id: int, token: int, user_message_id: int) -> None:
    """Record which question the claimed turn answers, once that id is known."""
    with _turns_lock:
        claim = _active_turns.get(session_id)
        if claim is not None and claim.token == token:
            _active_turns[session_id] = TurnClaim(token=token, user_message_id=user_message_id)


def end_turn(session_id: int, token: int) -> None:
    """Release a claim, if `token` still owns it.

    Idempotent, and a no-op for a token that does not hold the claim: an error path that
    releases twice, or a stale release arriving after another turn has claimed the
    session, can never free a turn it does not own.
    """
    with _turns_lock:
        claim = _active_turns.get(session_id)
        if claim is not None and claim.token == token:
            del _active_turns[session_id]


def active_turn(session_id: int) -> TurnClaim | None:
    """The claim currently holding this session, or None. For tests and diagnostics."""
    with _turns_lock:
        return _active_turns.get(session_id)


def create_session(
    conn: sqlite3.Connection,
    class_id: int,
    title: str | None = None,
    artifact_part_id: int | None = None,
    mode: ChatMode = "guide",
) -> dict[str, object]:
    """Open a conversation on a class and return it.

    Mode is written explicitly rather than left to the column default, because the
    default is a schema detail and the starting mode should be readable here.

    Args:
        conn: Open database connection.
        class_id: Class the conversation belongs to.
        title: Name it opens with, or None to be named from its first message.
        artifact_part_id: The step of a solution this conversation is about, when it was
            opened by clicking one - or, for a writer session, the draft body it works
            on. It pins that step into every turn and is why dropping from Solve to
            Guide is one product rather than two.
        mode: `guide` for a tutor conversation, `writer` for a draft's. `show` is never
            a starting mode; it is a toggle a tutor conversation moves to.

    Raises:
        ValueError: when `mode` is outside the allowed set.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown chat mode: {mode}")
    cursor = conn.execute(
        "insert into chat_sessions (class_id, title, mode, artifact_part_id) values (?, ?, ?, ?)",
        (class_id, title, mode, artifact_part_id),
    )
    conn.commit()
    return get_session(conn, int(cursor.lastrowid or 0))


def get_session(conn: sqlite3.Connection, session_id: int) -> dict[str, object]:
    """One session.

    Raises:
        NotFoundError: when no session carries that id. Every other function here routes
            its lookup through this one, so the message is written once.
    """
    row = conn.execute(
        f"select {_SESSION_COLUMNS} from chat_sessions where id = ?",  # noqa: S608
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That conversation does not exist.")
    return dict(row)


def list_sessions(conn: sqlite3.Connection, class_id: int) -> list[dict[str, object]]:
    """Every tutor session in a class, newest first, which is the order the sidebar shows.

    Writer sessions are excluded on purpose: they belong to a draft's rail, they open
    from the draft, and a sidebar entry for one would be a second doorway into a
    conversation whose subject is not on screen.
    """
    rows = conn.execute(
        f"select {_SESSION_COLUMNS} from chat_sessions "  # noqa: S608
        "where class_id = ? and mode != ? order by created_at desc, id desc",
        (class_id, WRITER),
    )
    return [dict(row) for row in rows]


def writer_sessions_for_part(
    conn: sqlite3.Connection, artifact_part_id: int
) -> list[dict[str, object]]:
    """A draft body's writer sessions, newest first. The newest is the one the rail opens."""
    rows = conn.execute(
        f"select {_SESSION_COLUMNS} from chat_sessions "  # noqa: S608
        "where artifact_part_id = ? and mode = ? order by created_at desc, id desc",
        (artifact_part_id, WRITER),
    )
    return [dict(row) for row in rows]


def anchored_context(conn: sqlite3.Connection, session_id: int) -> str | None:
    """The step this conversation is anchored to, rendered for the system prompt.

    Returns None for an ordinary conversation, and also for one whose step has since been
    deleted: the column is `on delete set null` precisely so losing the anchor does not
    cost the student the transcript.
    """
    part_id = get_session(conn, session_id)["artifact_part_id"]
    if part_id is None:
        return None

    row = conn.execute(
        "select p.label, p.content, parent.content as problem_statement, "
        "parent.label as problem_label from artifact_parts p "
        "left join artifact_parts parent on parent.id = p.parent_part_id "
        "where p.id = ?",
        (part_id,),
    ).fetchone()
    if row is None:
        return None

    # A step read without its question is ambiguous, and the student clicked it while
    # looking at both. A part with no parent is its own subject, so it stands alone.
    problem = str(row["problem_statement"] or row["content"])
    return format_step_context(
        problem,
        str(row["content"]),
        str(row["label"]) if row["label"] else None,
        str(row["problem_label"]) if row["problem_label"] else None,
    )


def delete_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Delete a session and its messages.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    # messages cascade from chat_sessions.
    conn.execute("delete from chat_sessions where id = ?", (session_id,))
    conn.commit()


def rename_session(conn: sqlite3.Connection, session_id: int, title: str) -> dict[str, object]:
    """Give a conversation a name the student chose, and return the updated row.

    A title written by hand is not a title waiting to be filled in, so this sets the same
    column `set_session_title_if_unset` writes to, which stops naming a session once it
    carries anything at all.

    Raises:
        NotFoundError: when no session carries that id.
        ValueError: when the title is blank. Clearing a name would put the conversation
            back to being named by its first message, which has already been sent.
    """
    get_session(conn, session_id)
    cleaned = " ".join(title.split())
    if not cleaned:
        raise ValueError("A conversation name cannot be blank.")
    conn.execute(
        "update chat_sessions set title = ? where id = ?",
        (cleaned[:_TITLE_MAX_CHARS], session_id),
    )
    conn.commit()
    return get_session(conn, session_id)


def discard_empty_sessions(conn: sqlite3.Connection) -> int:
    """Delete every conversation that holds no messages, and say how many went.

    A conversation with nothing in it is not history, it is a click. The frontend no
    longer opens one until the first message is sent, so this exists for the ones already
    stored from when it did: browsing six steps of a solution used to leave six untitled
    chats in the rail, and no student is going to clear those out by hand.

    Safe to run at startup precisely because nothing is in flight then. A live sweep would
    race a panel that has opened its conversation but not yet sent into it.
    """
    cursor = conn.execute(
        "delete from chat_sessions where id not in (select distinct session_id from messages)"
    )
    conn.commit()
    return cursor.rowcount


def set_session_mode(
    conn: sqlite3.Connection, session_id: int, mode: ChatMode
) -> dict[str, object]:
    """Persist the Guide/Show choice on the session and return the updated row.

    Raises:
        NotFoundError: when no session carries that id.
        ValueError: when `mode` is outside the allowed set. The column carries the same
            check constraint, but failing here names the offending value.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown chat mode: {mode}")
    get_session(conn, session_id)
    conn.execute("update chat_sessions set mode = ? where id = ?", (mode, session_id))
    conn.commit()
    return get_session(conn, session_id)


def title_from_message(content: str) -> str:
    """Condense a first message into a sidebar-length conversation title.

    Cut at a word boundary rather than mid-word, and keep it short enough that the rail
    truncates rarely. Newlines collapse so a pasted problem statement does not become a
    title with a line break in it.
    """
    cleaned = " ".join(content.split())
    if len(cleaned) <= _TITLE_MAX_CHARS:
        return cleaned
    head = cleaned[:_TITLE_MAX_CHARS]
    cut = head.rfind(" ")
    # A single word longer than the limit has no boundary to cut at; take the hard slice.
    return f"{head[:cut] if cut > _TITLE_MIN_CHARS else head}..."


def set_session_title_if_unset(conn: sqlite3.Connection, session_id: int, content: str) -> None:
    """Name an untitled conversation after its first message.

    A conversation is identified by what it is about, not by its position in a list:
    numbering renumbers as conversations come and go, so the same chat changes name.
    Only the first message titles a session; later ones leave the name alone.
    """
    row = conn.execute("select title from chat_sessions where id = ?", (session_id,)).fetchone()
    if row is None or (row["title"] or "").strip():
        return
    conn.execute(
        "update chat_sessions set title = ? where id = ?",
        (title_from_message(content), session_id),
    )
    conn.commit()


def add_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: MessageRole,
    content: str,
    retrieval_trimmed: bool = False,
    omitted_document_count: int = 0,
    thinking: str = "",
    thinking_ms: int = 0,
    tool_activity: list[dict[str, object]] | None = None,
) -> int:
    """Append one message to a conversation and return its id.

    Args:
        conn: Open database connection.
        session_id: Conversation the message belongs to.
        role: `user` or `assistant`.
        content: Message text. A reply cut short by a disconnect is stored as it stands.
        retrieval_trimmed: Whether this turn's retrieval was cut by more than half.
        omitted_document_count: Distinct documents that trim dropped.
        thinking: The model's reasoning for this turn, empty for a model that does not
            think or a server that does not expose it. Stored beside the answer, never
            inside it, and never replayed back to the model as history.
        thinking_ms: How long that reasoning took, zero when there was none.
        tool_activity: What a writer turn did on the way to its answer, one entry per
            tool call. Stored beside the answer like `thinking`, and for the same
            reason: the record of the work belongs to the message it produced.

    Returns:
        The id of the inserted message.

    Raises:
        NotFoundError: when no session carries that id.
    """
    message_id = insert_message(
        conn,
        session_id,
        role,
        content,
        retrieval_trimmed=retrieval_trimmed,
        omitted_document_count=omitted_document_count,
        thinking=thinking,
        thinking_ms=thinking_ms,
        tool_activity=tool_activity,
    )
    conn.commit()
    return message_id


def insert_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: MessageRole,
    content: str,
    retrieval_trimmed: bool = False,
    omitted_document_count: int = 0,
    thinking: str = "",
    thinking_ms: int = 0,
    tool_activity: list[dict[str, object]] | None = None,
) -> int:
    """Append one message without committing, and return its id.

    The body of `add_message` minus the commit, so a caller composing a larger transaction
    - the agent route committing an assistant reply and its attempt's completion together,
    so a crash can never leave a stored reply beside an attempt still reading as running -
    can write the message inside its own `begin`/`commit`. `add_message` remains the
    committing entry point every other caller uses.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    cursor = conn.execute(
        "insert into messages (session_id, role, content, thinking, thinking_ms, "
        "retrieval_trimmed, omitted_document_count, tool_activity) "
        "values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            role,
            content,
            thinking,
            thinking_ms,
            int(retrieval_trimmed),
            omitted_document_count,
            json.dumps(tool_activity or [], separators=(",", ":")),
        ),
    )
    return int(cursor.lastrowid or 0)


def last_user_message(conn: sqlite3.Connection, session_id: int) -> dict[str, object]:
    """The most recent question in a conversation, which is the one a retry re-answers.

    Raises:
        NotFoundError: when no session carries that id, or when the conversation holds no
            question yet. There is nothing to retry in an empty conversation, and saying
            so is better than regenerating against a prompt that does not exist.
    """
    get_session(conn, session_id)
    row = conn.execute(
        "select id, content from messages where session_id = ? and role = 'user' "
        "order by id desc limit 1",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("There is no question in this conversation to try again.")
    return dict(row)


def remove_messages(
    conn: sqlite3.Connection, session_id: int, message_ids: tuple[int, ...]
) -> None:
    """Drop exactly the named messages without committing.

    The caller owns the transaction. This is the non-committing primitive that
    ``delete_messages`` delegates to; use it directly inside an explicit
    ``begin immediate`` / ``commit`` block when the delete must land atomically
    with other mutations (e.g. regeneration's delete-then-insert).
    """
    if not message_ids:
        return
    placeholders = ", ".join("?" for _ in message_ids)
    conn.execute(
        f"delete from messages where session_id = ? and id in ({placeholders})",  # noqa: S608
        (session_id, *message_ids),
    )


def delete_messages(
    conn: sqlite3.Connection, session_id: int, message_ids: tuple[int, ...]
) -> None:
    """Drop exactly the named messages from a conversation.

    This is what makes a retry a retry rather than a second question: the reply being
    replaced is removed before the new one is stored. It deletes by explicit id, never by
    "everything newer than X": the ids are the messages the regeneration observed when its
    turn opened, so the destructive step can only ever take what that turn is entitled to
    replace. A newer independent question or reply - one committed by a turn this plan
    never saw - is not in the list and is untouchable, whatever the request timing.
    """
    remove_messages(conn, session_id, message_ids)
    if message_ids:
        conn.commit()


def list_messages(conn: sqlite3.Connection, session_id: int) -> list[dict[str, object]]:
    """A conversation's messages, oldest first, which is both reading and prompt order.

    `tool_activity` is decoded here, so callers and the API always see the array and the
    JSON encoding stays a storage detail of this module.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    messages = [dict(row) for row in conn.execute(_MESSAGE_SQL, (session_id,))]
    for message in messages:
        message["tool_activity"] = json.loads(str(message["tool_activity"] or "[]"))
    return messages
