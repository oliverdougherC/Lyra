"""The durable attempt lifecycle for agent-chat turns (PLA-295).

One durable user message is one logical turn. Each run of the model against that turn is
an *attempt* with an explicit state, recorded here before the loop runs and settled when
it ends. That is what makes a retry causal: the retry reuses the original user message
rather than appending a second copy, the tool audit rows an attempt wrote stay tied to
that attempt, and a lost successful response is replayed from the committed reply instead
of running the model a second time.

This is deliberately the smallest durable contract the retry needs, not a generic job
system: no scheduler, no queue, no cross-process coordination. Serialization of the runs
themselves is the per-session turn claim in `sessions`; this module only records what each
run was and how it ended.
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

# The bounded detail stored for a failed/stopped attempt is written by the caller from the
# tool loop's own privacy-safe constants; this cap is a backstop so a future caller cannot
# store an unbounded blob here.
_MAX_DETAIL_CHARS = 2_000

NO_TURN_TO_RETRY = "There is no agent turn in this conversation to try again."


@dataclass(frozen=True)
class RetryTarget:
    """The turn a retry re-answers, resolved under the session claim.

    `latest` is the most recent attempt on that user message: a completed one means the
    turn already succeeded (a lost HTTP response is replayed from it, never re-run), and a
    failed or stopped one is what a retry runs again.
    """

    user_message_id: int
    content: str
    profile: str
    latest: dict[str, object]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    user_message_id: int,
    profile: str,
    mode: str | None = None,
    document_id: int | None = None,
    operation_id: str | None = None,
    commit: bool = True,
) -> int:
    """Record a running attempt on one user message and return its id.

    Called after the user message is persisted (or, on a retry, resolved) and before the
    tool loop runs, so every run of the model is bracketed by a durable row.

    `mode` and `document_id` persist the turn context the attempt was asked under (the
    Guide/Show choice and the selected source scope), so a retry or a just-in-time
    continuation can re-run the turn with the scope it was originally asked with. Since
    migration 042 a stored `document_id` of None is a real value ("All material"), not an
    absence, so `scope_persisted` records that this row's mode/document_id were written by
    the modern path: retry and regenerate read the persisted scope - null included - only
    from a row that carries the flag, and fall back to request-provided scope for the
    pre-flag legacy rows instead.
    `operation_id` is the client-generated idempotency key (PLA-313), bound to this session
    by a unique index: a fresh send stores it, and a retry attempt on an existing message
    stores None, exactly like the tutor's attempt lifecycle.

    `commit=False` leaves the insert uncommitted so the caller can land it in the same
    transaction as the user message insert (the fresh-send path); retry and legacy callers
    keep the default, which commits the attempt row on its own.
    """
    # `scope_persisted=1` is always written by the modern path: this row's mode and
    # document_id are authoritative, a stored null document included ("All material").
    # Pre-flag rows (the column's default) are the legacy scope that retry backstops from
    # the request instead of from the row.
    cursor = conn.execute(
        "insert into agent_turn_attempts "
        "(session_id, user_message_id, profile, state, mode, document_id, operation_id, "
        "scope_persisted) "
        "values (?, ?, ?, ?, ?, ?, ?, 1)",
        (session_id, user_message_id, profile, RUNNING, mode, document_id, operation_id),
    )
    if commit:
        conn.commit()
    return int(cursor.lastrowid or 0)


def find_by_operation_id(
    conn: sqlite3.Connection, session_id: int, operation_id: str
) -> dict[str, object] | None:
    """Find an existing attempt by its client-generated operation_id (PLA-313).

    Returns a dict with `user_message_id`, `attempt_id`, `state`,
    `assistant_message_id`, `mode`, `document_id`, and `scope_persisted` when a prior
    attempt committed with the same operation_id in this session; None otherwise. Mirrors
    the tutor's `tutor_attempts.find_by_operation_id`.
    """
    row = conn.execute(
        "select user_message_id, id as attempt_id, state, "
        "assistant_message_id, mode, document_id, scope_persisted "
        "from agent_turn_attempts "
        "where session_id = ? and operation_id = ? "
        "order by id desc limit 1",
        (session_id, operation_id),
    ).fetchone()
    return dict(row) if row is not None else None


def find_completed_attempt(
    conn: sqlite3.Connection, user_message_id: int
) -> dict[str, object] | None:
    """The most recent completed attempt on a user message, or None.

    Scans the whole lineage, not just the attempt that carries the operation_id: a retry
    attempt (`operation_id=None`) may have completed after the original failed, and the
    replay must hand back that completed reply. Mirrors the tutor's
    `tutor_attempts.find_completed_attempt`.
    """
    row = conn.execute(
        "select * from agent_turn_attempts "
        "where user_message_id = ? and state = ? "
        "order by id desc limit 1",
        (user_message_id, COMPLETED),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_completed(conn: sqlite3.Connection, attempt_id: int, assistant_message_id: int) -> None:
    """Settle a running attempt as completed without committing, recording its reply.

    Left uncommitted so the agent route can commit it in the *same* transaction as the
    assistant message insert: the reply and the `completed` state land together or not at
    all, so a crash can never leave a stored reply beside an attempt still reading as
    running (which a later retry would re-run, double-answering). Only a still-running row
    is settled, so a second settle cannot rewrite a terminal one.
    """
    cursor = conn.execute(
        "update agent_turn_attempts set state = ?, assistant_message_id = ?, finished_at = ? "
        "where id = ? and state = ?",
        (COMPLETED, assistant_message_id, _timestamp(), attempt_id, RUNNING),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("The agent attempt was not running when its reply was completed.")


def complete_attempt(conn: sqlite3.Connection, attempt_id: int, assistant_message_id: int) -> None:
    """Settle an attempt as completed and commit, for callers not composing a transaction."""
    mark_completed(conn, attempt_id, assistant_message_id)
    conn.commit()


def fail_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    stopped_reason: str,
    detail: str,
) -> None:
    """Settle a still-running attempt as failed, with a bounded stop reason and detail."""
    conn.execute(
        "update agent_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (FAILED, stopped_reason, detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
    )
    conn.commit()


def stop_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    detail: str,
    stopped_reason: str = "cancelled",
) -> None:
    """Settle a still-running attempt as stopped (abandoned).

    Used when a turn is cancelled or the client disconnects mid-run: the claim is released,
    but the attempt must not read as forever in flight. Stopped is a truthful, retryable
    terminal state, the same one a restart reconciles an interrupted attempt to. Only a
    still-running row is settled, so a settle racing an ordinary completion is a no-op.

    `stopped_reason` names what stopped it: "cancelled" for a Stop or disconnect,
    "abandoned" for a restart's reconciliation, and the loop's own stop reasons (for
    example "no_tool_support", when the turn's tool pass was abandoned in favor of the
    tool-less answer of the same logical turn) so the attempt ledger stays truthful.
    """
    conn.execute(
        "update agent_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
        "finished_at = ? where id = ? and state = ?",
        (STOPPED, stopped_reason, detail[:_MAX_DETAIL_CHARS], _timestamp(), attempt_id, RUNNING),
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

    This helper deliberately does not commit: proposal storage calls it after inserting
    the target and before committing that same transaction. ``insert or ignore`` preserves
    the original owner when an idempotent retry encounters an existing source, excerpt, or
    fact instead of creating a new row.

    Returns the target's durable owner, or ``None`` for non-agent callers.
    """
    if attempt_id is None:
        return None
    conn.execute(
        "insert or ignore into agent_attempt_targets (attempt_id, target_kind, target_id) "
        "values (?, ?, ?)",
        (attempt_id, target_kind, target_id),
    )
    owner = conn.execute(
        "select attempt_id from agent_attempt_targets where target_kind = ? and target_id = ?",
        (target_kind, target_id),
    ).fetchone()
    if owner is None:  # pragma: no cover - same transaction inserted or found the row.
        raise RuntimeError("The agent proposal ownership link disappeared.")
    return int(owner["attempt_id"])


def target_owner(conn: sqlite3.Connection, *, target_kind: str, target_id: int) -> int | None:
    """Return the attempt that originally produced one durable target, if agent-created."""
    row = conn.execute(
        "select attempt_id from agent_attempt_targets where target_kind = ? and target_id = ?",
        (target_kind, target_id),
    ).fetchone()
    return int(row["attempt_id"]) if row is not None else None


def latest_attempt_for_message(
    conn: sqlite3.Connection, user_message_id: int
) -> dict[str, object] | None:
    """The most recent attempt on one user message, or None when it has none."""
    row = conn.execute(
        "select * from agent_turn_attempts where user_message_id = ? order by id desc limit 1",
        (user_message_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def latest_attempts_by_message(
    conn: sqlite3.Connection, session_id: int
) -> dict[int, dict[str, object]]:
    """The latest attempt for every user message in a session that has one.

    One query for the whole conversation, so the message-list endpoint can annotate each
    user turn with its attempt state without a lookup per message.

    Each row also carries `lineage_operation_id`: the operation ID the logical send that
    created this message's attempt lineage minted - the id on the lineage's ROOT attempt,
    not the latest one. Internal attempts carry no id of their own (a retry, a
    regeneration, and the tool-less continuation of an endpoint that refused the first
    tools request all create attempts with `operation_id = NULL`), so the latest attempt's
    own id can be NULL even though the send itself has a durable identity. The readback
    exposes the root's non-null id, which is the identity a lost-response reconciliation
    must match: the unique index keeps at most one non-null operation id per session, so
    the lineage's first id-carrying attempt is the root by construction, and the id is
    surfaced in readback rather than duplicated onto the later attempts.
    """
    rows = conn.execute(
        "select a.*, root.operation_id as lineage_operation_id "
        "from agent_turn_attempts a "
        "join (select user_message_id, max(id) as latest_id from agent_turn_attempts "
        "      where session_id = ? group by user_message_id) newest "
        "  on a.id = newest.latest_id "
        "left join (select user_message_id, min(id) as root_id "
        "      from agent_turn_attempts "
        "      where session_id = ? and operation_id is not null "
        "      group by user_message_id) rooted "
        "  on rooted.user_message_id = a.user_message_id "
        "left join agent_turn_attempts root on root.id = rooted.root_id",
        (session_id, session_id),
    ).fetchall()
    return {int(row["user_message_id"]): dict(row) for row in rows}


def resolve_retry_target(conn: sqlite3.Connection, session_id: int) -> RetryTarget:
    """The last user turn in a session that carries an agent attempt.

    Read under the session claim so no concurrent turn can move the conversation between
    this read and the retry that acts on it. Retrying targets the newest user message; if
    that message is not an agent turn (a tutor turn, or a fresh conversation), there is
    nothing here to retry and the caller refuses.

    Raises:
        NotFoundError: the conversation has no question, or its newest question is not an
            agent turn with a recorded attempt.
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
        profile=str(latest["profile"]),
        latest=latest,
    )


def reconcile_running(conn: sqlite3.Connection) -> int:
    """Settle attempts left running by a crash as stopped, after restart.

    A turn cannot outlive the single backend process that was streaming it, so a row still
    `running` at startup is one whose process died mid-attempt. It is marked stopped - a
    truthful, retryable terminal state - rather than left to read forever as in flight.
    """
    finished_at = _timestamp()
    cursor = conn.execute(
        "update agent_turn_attempts set state = ?, stopped_reason = ?, detail = ?, "
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
