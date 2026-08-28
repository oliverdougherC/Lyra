"""The durable attempt lifecycle for tutor-chat turns (PLA-306).

One durable user message is one logical turn. Each run of the model against that turn is
an *attempt* with an explicit state, recorded here before the loop runs and settled when
it ends. That is what makes a retry causal: the retry reuses the original user message
rather than appending a second copy, and a lost successful response is replayed from the
committed reply instead of running the model a second time.

Tutor turns have no durable tool effects, so there is no targets table and no link_target.
The four-state lifecycle (running/completed/failed/stopped) matches the agent pattern;
there is no planned state because a tutor turn has no tool setup phase between creation
and streaming.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.core.errors import NotFoundError

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"

_MAX_DETAIL_CHARS = 2_000

NO_TURN_TO_RETRY = "There is no tutor turn in this conversation to try again."


@dataclass(frozen=True)
class RetryTarget:
    """The turn a retry re-answers, resolved under the session claim.

    `latest` is the most recent attempt on that user message: a completed one means the
    turn already succeeded (a lost HTTP response is replayed from it, never re-run), and a
    failed or stopped one is what a retry runs again.
    """

    user_message_id: int
    content: str
    latest: dict[str, object]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    user_message_id: int,
    mode: str | None = None,
    document_id: int | None = None,
    operation_id: str | None = None,
) -> int:
    """Record a running attempt on one user message and return its id.

    Does NOT commit: the caller commits atomically with the user message insert, so
    the message and its attempt land together or not at all.
    """
    cursor = conn.execute(
        "insert into tutor_turn_attempts "
        "(session_id, user_message_id, state, mode, document_id, operation_id) "
        "values (?, ?, ?, ?, ?, ?)",
        (session_id, user_message_id, RUNNING, mode, document_id, operation_id),
    )
    return int(cursor.lastrowid or 0)


def find_by_operation_id(
    conn: sqlite3.Connection, session_id: int, operation_id: str
) -> dict[str, object] | None:
    """Find an existing attempt by its client-generated operation_id (PLA-313).

    Returns a dict with `user_message_id`, `attempt_id`, `state`,
    `assistant_message_id`, `mode`, and `document_id` when a prior attempt committed
    with the same operation_id in this session; None otherwise.
    """
    row = conn.execute(
        "select user_message_id, id as attempt_id, state, "
        "assistant_message_id, mode, document_id "
        "from tutor_turn_attempts "
        "where session_id = ? and operation_id = ? "
        "order by id desc limit 1",
        (session_id, operation_id),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_completed(conn: sqlite3.Connection, attempt_id: int, assistant_message_id: int) -> None:
    """Settle a running attempt as completed without committing, recording its reply.

    Left uncommitted so the route can commit it in the *same* transaction as the
    assistant message insert: the reply and the completed state land together or not at
    all.
    """
    cursor = conn.execute(
        "update tutor_turn_attempts set state = ?, assistant_message_id = ?, finished_at = ? "
        "where id = ? and state = ?",
        (COMPLETED, assistant_message_id, _timestamp(), attempt_id, RUNNING),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("The tutor attempt was not running when its reply was completed.")


def fail_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    stopped_reason: str,
    detail: str,
) -> None:
    """Settle a still-running attempt as failed, with a bounded stop reason and detail."""
    conn.execute(
        "update tutor_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (FAILED, stopped_reason, detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
    )
    conn.commit()


def stop_attempt(conn: sqlite3.Connection, attempt_id: int, *, detail: str) -> None:
    """Settle a still-running attempt as stopped (abandoned)."""
    conn.execute(
        "update tutor_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (STOPPED, "cancelled", detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
    )
    conn.commit()


def latest_attempt_for_message(
    conn: sqlite3.Connection, user_message_id: int
) -> dict[str, object] | None:
    """The most recent attempt on one user message, or None when it has none."""
    row = conn.execute(
        "select * from tutor_turn_attempts where user_message_id = ? order by id desc limit 1",
        (user_message_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def latest_attempts_by_message(
    conn: sqlite3.Connection, session_id: int
) -> dict[int, dict[str, object]]:
    """The latest attempt for every user message in a session that has one."""
    rows = conn.execute(
        "select a.* from tutor_turn_attempts a "
        "join (select user_message_id, max(id) as latest_id from tutor_turn_attempts "
        "      where session_id = ? group by user_message_id) newest "
        "  on a.id = newest.latest_id",
        (session_id,),
    ).fetchall()
    return {int(row["user_message_id"]): dict(row) for row in rows}


def resolve_retry_target(conn: sqlite3.Connection, session_id: int) -> RetryTarget:
    """The last user turn in a session that carries a tutor attempt.

    Read under the session claim so no concurrent turn can move the conversation between
    this read and the retry that acts on it.

    Raises:
        NotFoundError: the conversation has no question, or its newest question is not a
            tutor turn with a recorded attempt.
    """
    row = conn.execute(
        "select id, content from messages where session_id = ? and role = 'user' "
        "order by id desc limit 1",
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(NO_TURN_TO_RETRY)
    latest = latest_attempt_for_message(conn, int(row["id"]))
    if latest is None:
        raise NotFoundError(NO_TURN_TO_RETRY)
    return RetryTarget(
        user_message_id=int(row["id"]),
        content=str(row["content"]),
        latest=latest,
    )


def reconcile_running(conn: sqlite3.Connection) -> int:
    """Settle attempts left running by a crash as stopped, after restart."""
    finished_at = _timestamp()
    cursor = conn.execute(
        "update tutor_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where state = ?",
        (
            STOPPED,
            "abandoned",
            "This turn was interrupted before it finished. Try it again.",
            finished_at,
            RUNNING,
        ),
    )
    conn.commit()
    return int(cursor.rowcount)
