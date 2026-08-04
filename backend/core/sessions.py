"""Chat session and message management.

Chat is session-scoped rather than class-scoped so a conversation has one unambiguous
owner: messages cascade from the session, and deleting a session takes its history with
it. The Guide/Show mode is stored on the session rather than read from each request, so
the toggle survives a reload and the next turn continues in the mode the user chose.
"""

import sqlite3
from typing import Literal

from backend.core.errors import NotFoundError
from backend.llm.prompts import format_step_context

ChatMode = Literal["guide", "show"]
MessageRole = Literal["user", "assistant"]

MODES: tuple[str, ...] = ("guide", "show")

# Title length: long enough to tell two conversations apart in a 260px rail, short enough
# that most titles survive without an ellipsis.
_TITLE_MAX_CHARS = 48
_TITLE_MIN_CHARS = 24

_MESSAGE_SQL = """
select id, session_id, role, content, thinking, thinking_ms, retrieval_trimmed,
       omitted_document_count, created_at
from messages
where session_id = ?
order by id
"""


_SESSION_COLUMNS = "id, class_id, title, mode, artifact_part_id, created_at"


def create_session(
    conn: sqlite3.Connection,
    class_id: int,
    title: str | None = None,
    artifact_part_id: int | None = None,
) -> dict[str, object]:
    """Open a conversation on a class and return it.

    Mode is written explicitly rather than left to the column default, because the
    default is a schema detail and the starting mode should be readable here.

    Args:
        conn: Open database connection.
        class_id: Class the conversation belongs to.
        title: Name it opens with, or None to be named from its first message.
        artifact_part_id: The step of a solution this conversation is about, when it was
            opened by clicking one. It pins that step into every turn and is why dropping
            from Solve to Guide is one product rather than two.
    """
    cursor = conn.execute(
        "insert into chat_sessions (class_id, title, mode, artifact_part_id) "
        "values (?, ?, 'guide', ?)",
        (class_id, title, artifact_part_id),
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
    """Every session in a class, newest first, which is the order the sidebar shows."""
    rows = conn.execute(
        f"select {_SESSION_COLUMNS} from chat_sessions "  # noqa: S608
        "where class_id = ? order by created_at desc, id desc",
        (class_id,),
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

    Returns:
        The id of the inserted message.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    cursor = conn.execute(
        "insert into messages (session_id, role, content, thinking, thinking_ms, "
        "retrieval_trimmed, omitted_document_count) values (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            role,
            content,
            thinking,
            thinking_ms,
            int(retrieval_trimmed),
            omitted_document_count,
        ),
    )
    conn.commit()
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


def delete_messages_after(conn: sqlite3.Connection, session_id: int, message_id: int) -> None:
    """Drop every message in a conversation newer than `message_id`.

    This is what makes a retry a retry rather than a second question: the previous answer
    is removed before the new one is generated, so the conversation ends up with one reply
    to the question rather than two.
    """
    conn.execute("delete from messages where session_id = ? and id > ?", (session_id, message_id))
    conn.commit()


def list_messages(conn: sqlite3.Connection, session_id: int) -> list[dict[str, object]]:
    """A conversation's messages, oldest first, which is both reading and prompt order.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    return [dict(row) for row in conn.execute(_MESSAGE_SQL, (session_id,))]
