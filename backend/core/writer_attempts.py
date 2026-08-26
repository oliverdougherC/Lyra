"""The durable attempt lifecycle for writer-chat turns (PLA-310).

Mirrors `agent_attempts` for the writer conversation. One durable user message is
one logical turn. Each run of the model against that turn is an *attempt* with an
explicit state, recorded here before the loop runs and settled when it ends. That
is what makes a retry causal: the retry reuses the original user message rather
than appending a second copy, and a lost successful response is replayed from the
committed reply instead of running the model a second time.

Writer turns carry an `intent` (the writer-intent classifier's label) instead of
the agent turn's `profile`, and their durable targets are proposals, briefs, and
comments rather than sources and facts.
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

NO_TURN_TO_RETRY = "There is no writer turn in this conversation to try again."


@dataclass(frozen=True)
class RetryTarget:
    """The turn a retry re-answers, resolved under the session claim.

    `latest` is the most recent attempt on that user message: a completed one means
    the turn already succeeded (a lost HTTP response is replayed from it, never
    re-run), and a failed or stopped one is what a retry runs again.
    """

    user_message_id: int
    content: str
    intent: str
    latest: dict[str, object]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    user_message_id: int,
    intent: str,
) -> int:
    """Record a running attempt on one user message and return its id.

    Called after the user message is persisted (or, on a retry, resolved) and before
    the tool loop runs, so every run of the model is bracketed by a durable row.
    """
    cursor = conn.execute(
        "insert into writer_turn_attempts (session_id, user_message_id, intent, state) "
        "values (?, ?, ?, ?)",
        (session_id, user_message_id, intent, RUNNING),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def mark_completed(conn: sqlite3.Connection, attempt_id: int, assistant_message_id: int) -> None:
    """Settle a running attempt as completed without committing, recording its reply.

    Left uncommitted so the writer route can commit it in the *same* transaction as
    the assistant message insert: the reply and the `completed` state land together
    or not at all.
    """
    cursor = conn.execute(
        "update writer_turn_attempts set state = ?, assistant_message_id = ?, finished_at = ? "
        "where id = ? and state = ?",
        (COMPLETED, assistant_message_id, _timestamp(), attempt_id, RUNNING),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("The writer attempt was not running when its reply was completed.")


def fail_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    stopped_reason: str,
    detail: str,
) -> None:
    """Settle a still-running attempt as failed, with a bounded stop reason and detail."""
    conn.execute(
        "update writer_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (FAILED, stopped_reason, detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
    )
    conn.commit()


def stop_attempt(conn: sqlite3.Connection, attempt_id: int, *, detail: str) -> None:
    """Settle a still-running attempt as stopped (abandoned).

    Used when a turn is cancelled or the client disconnects mid-run.
    """
    conn.execute(
        "update writer_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (STOPPED, "cancelled", detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
    )
    conn.commit()


def link_target(
    conn: sqlite3.Connection,
    attempt_id: int | None,
    *,
    target_kind: str,
    target_id: int,
) -> int | None:
    """Atomically bind a newly-created durable target to its producing attempt.

    Does not commit: the caller commits with the target insert. ``insert or ignore``
    preserves the original owner on an idempotent retry.
    """
    if attempt_id is None:
        return None
    conn.execute(
        "insert or ignore into writer_attempt_targets (attempt_id, target_kind, target_id) "
        "values (?, ?, ?)",
        (attempt_id, target_kind, target_id),
    )
    owner = conn.execute(
        "select attempt_id from writer_attempt_targets where target_kind = ? and target_id = ?",
        (target_kind, target_id),
    ).fetchone()
    if owner is None:  # pragma: no cover
        raise RuntimeError("The writer proposal ownership link disappeared.")
    return int(owner["attempt_id"])


def latest_attempt_for_message(
    conn: sqlite3.Connection, user_message_id: int
) -> dict[str, object] | None:
    """The most recent attempt on one user message, or None when it has none."""
    row = conn.execute(
        "select * from writer_turn_attempts where user_message_id = ? order by id desc limit 1",
        (user_message_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def latest_attempts_by_message(
    conn: sqlite3.Connection, session_id: int
) -> dict[int, dict[str, object]]:
    """The latest attempt for every user message in a session that has one.

    One query for the whole conversation, so the message-list endpoint can annotate
    each user turn with its attempt state without a lookup per message.
    """
    rows = conn.execute(
        "select a.* from writer_turn_attempts a "
        "join (select user_message_id, max(id) as latest_id from writer_turn_attempts "
        "      where session_id = ? group by user_message_id) newest "
        "  on a.id = newest.latest_id",
        (session_id,),
    ).fetchall()
    return {int(row["user_message_id"]): dict(row) for row in rows}


def resolve_retry_target(conn: sqlite3.Connection, session_id: int) -> RetryTarget:
    """The last user turn in a session that carries a writer attempt.

    Read under the session claim so no concurrent turn can move the conversation
    between this read and the retry that acts on it.

    Raises:
        NotFoundError: the conversation has no question, or its newest question has
            no recorded writer attempt.
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
        intent=str(latest["intent"]),
        latest=latest,
    )


def reconcile_running(conn: sqlite3.Connection) -> int:
    """Settle writer attempts left running by a crash as stopped, after restart."""
    finished_at = _timestamp()
    cursor = conn.execute(
        "update writer_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
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
