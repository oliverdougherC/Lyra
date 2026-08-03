"""Chat session and message management.

Chat is session-scoped rather than class-scoped so a conversation has one unambiguous
owner: messages cascade from the session, and deleting a session takes its history with
it. The Guide/Show mode is stored on the session rather than read from each request, so
the toggle survives a reload and the next turn continues in the mode the user chose.
"""

import sqlite3
from typing import Literal

from backend.core.errors import NotFoundError

ChatMode = Literal["guide", "show"]
MessageRole = Literal["user", "assistant"]

MODES: tuple[str, ...] = ("guide", "show")

_MESSAGE_SQL = """
select id, session_id, role, content, retrieval_trimmed, omitted_document_count, created_at
from messages
where session_id = ?
order by id
"""


def create_session(
    conn: sqlite3.Connection, class_id: int, title: str | None = None
) -> dict[str, object]:
    """Open a conversation on a class and return it.

    Mode is written explicitly rather than left to the column default, because the
    default is a schema detail and the starting mode should be readable here.
    """
    cursor = conn.execute(
        "insert into chat_sessions (class_id, title, mode) values (?, ?, 'guide')",
        (class_id, title),
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
        "select id, class_id, title, mode, created_at from chat_sessions where id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That conversation does not exist.")
    return dict(row)


def list_sessions(conn: sqlite3.Connection, class_id: int) -> list[dict[str, object]]:
    """Every session in a class, newest first, which is the order the sidebar shows."""
    rows = conn.execute(
        "select id, class_id, title, mode, created_at from chat_sessions "
        "where class_id = ? order by created_at desc, id desc",
        (class_id,),
    )
    return [dict(row) for row in rows]


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


def add_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: MessageRole,
    content: str,
    retrieval_trimmed: bool = False,
    omitted_document_count: int = 0,
) -> int:
    """Append one message to a conversation and return its id.

    Args:
        conn: Open database connection.
        session_id: Conversation the message belongs to.
        role: `user` or `assistant`.
        content: Message text. A reply cut short by a disconnect is stored as it stands.
        retrieval_trimmed: Whether this turn's retrieval was cut by more than half.
        omitted_document_count: Distinct documents that trim dropped.

    Returns:
        The id of the inserted message.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    cursor = conn.execute(
        "insert into messages "
        "(session_id, role, content, retrieval_trimmed, omitted_document_count) "
        "values (?, ?, ?, ?, ?)",
        (session_id, role, content, int(retrieval_trimmed), omitted_document_count),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def list_messages(conn: sqlite3.Connection, session_id: int) -> list[dict[str, object]]:
    """A conversation's messages, oldest first, which is both reading and prompt order.

    Raises:
        NotFoundError: when no session carries that id.
    """
    get_session(conn, session_id)
    return [dict(row) for row in conn.execute(_MESSAGE_SQL, (session_id,))]
