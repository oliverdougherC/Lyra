"""Durable tool-audit rows for Phase 4 side effects.

The assistant message is not the source of truth for tool activity. Dispatch is
persisted before work begins, then finished with a terminal state. In-flight rows can be
reconciled to `abandoned` after restart.

This module intentionally stores bounded, redacted JSON rather than raw arguments or
results. Secrets and whole content blobs do not belong in audit storage.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

MAX_DEPTH = 4
MAX_ITEMS = 16
MAX_STRING_CHARS = 240

STARTED = "started"
ABANDONED = "abandoned"
TERMINAL_STATES = frozenset(
    {"succeeded", "refused", "failed", "timed_out", ABANDONED, "stale", "rejected"}
)

TABLE_SQL = """
create table if not exists tool_audit_events (
  id text primary key,
  caller_kind text not null,
  caller_id text,
  class_id integer,
  session_id integer,
  artifact_id integer,
  attempt_id integer,
  tool text not null,
  capability text not null,
  effect text not null,
  arguments_json text not null,
  target_kind text,
  target_id text,
  policy_decision text not null,
  state text not null,
  result_summary_json text,
  error_message text,
  abandonment_reason text,
  started_at text not null,
  finished_at text,
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index if not exists tool_audit_events_state_idx
  on tool_audit_events (state);
"""

_SECRET_KEY = re.compile(
    r"(^|[_-])(token|secret|password|authorization|api[_-]?key|cookie|credential)([_-]|$)",
    re.IGNORECASE,
)
_BULKY_KEY = re.compile(
    r"(^|[_-])(content|body|snapshot|stdout|stderr|html|text|prompt|response|diff|patch|raw)"
    r"([_-]|$)",
    re.IGNORECASE,
)
_SENSITIVE_MESSAGE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*\S+|"
    r"https?://\S+\?\S+)"
)


@dataclass(frozen=True)
class StartedAuditEvent:
    id: str
    started_at: str


@dataclass(frozen=True)
class FinishedAuditEvent:
    id: str
    state: str
    finished_at: str


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _require_text(value: str, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} cannot be blank")
    return clean


def _summarize_string(value: str, *, reason: str) -> dict[str, object]:
    return {
        "__redacted__": reason,
        "chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _sanitize_text(value: str, *, key: str | None = None) -> object:
    if key and _SECRET_KEY.search(key):
        return _summarize_string(value, reason="secret")
    if key and _BULKY_KEY.search(key):
        return _summarize_string(value, reason="bulky_text")
    if len(value) <= MAX_STRING_CHARS:
        return value
    return {
        "__truncated__": True,
        "chars": len(value),
        "prefix": value[:MAX_STRING_CHARS],
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _sanitize(value: object, *, key: str | None = None, depth: int = 0) -> object:
    if depth >= MAX_DEPTH:
        return {"__truncated__": True, "reason": "depth"}
    if isinstance(value, str):
        return _sanitize_text(value, key=key)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes):
        return {"__redacted__": "bytes", "bytes": len(value)}
    if isinstance(value, Mapping):
        limited = list(value.items())
        payload: dict[str, object] = {}
        for name, item in limited[:MAX_ITEMS]:
            payload[str(name)] = _sanitize(item, key=str(name), depth=depth + 1)
        if len(limited) > MAX_ITEMS:
            payload["__truncated_items__"] = len(limited) - MAX_ITEMS
        return payload
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = [_sanitize(item, depth=depth + 1) for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            return {
                "__type__": "list",
                "items": items,
                "__truncated_items__": len(value) - MAX_ITEMS,
            }
        return items
    return _sanitize_text(repr(value), key=key)


def _json_text(value: object) -> str:
    return json.dumps(_sanitize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded_message(value: str | None) -> str | None:
    if value is None:
        return None
    if _SENSITIVE_MESSAGE.search(value):
        return json.dumps(
            _summarize_string(value, reason="sensitive_message"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    sanitized = _sanitize_text(value)
    if isinstance(sanitized, str):
        return sanitized
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def start_event(
    conn: sqlite3.Connection,
    *,
    caller_kind: str,
    tool: str,
    capability: str,
    effect: str,
    arguments: object,
    policy_decision: str,
    caller_id: str | None = None,
    class_id: int | None = None,
    session_id: int | None = None,
    artifact_id: int | None = None,
    attempt_id: int | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    now: datetime | None = None,
) -> StartedAuditEvent:
    """Persist one started tool event before the effectful work runs."""
    record_id = uuid.uuid4().hex
    started_at = _timestamp(now)
    try:
        conn.execute("begin immediate")
        conn.execute(
            "insert into tool_audit_events ("
            "id, caller_kind, caller_id, class_id, session_id, artifact_id, attempt_id, tool, "
            "capability, effect, arguments_json, target_kind, target_id, policy_decision, state, "
            "started_at"
            ") values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                _require_text(caller_kind, "caller_kind"),
                caller_id,
                class_id,
                session_id,
                artifact_id,
                attempt_id,
                _require_text(tool, "tool"),
                _require_text(capability, "capability"),
                _require_text(effect, "effect"),
                _json_text(arguments),
                target_kind,
                target_id,
                _require_text(policy_decision, "policy_decision"),
                STARTED,
                started_at,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return StartedAuditEvent(id=record_id, started_at=started_at)


def finish_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    state: str,
    result_summary: object | None = None,
    error_message: str | None = None,
    abandonment_reason: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    now: datetime | None = None,
) -> FinishedAuditEvent:
    """Mark a started event terminal exactly once, attaching any result target.

    Proposal ids are generally allocated by the action, after ``start_event`` has already
    committed. Persisting the target here gives successful proposal rows one durable join
    through ``(target_kind, target_id)`` to this event and its agent ``attempt_id``.
    Existing targets supplied at start are never overwritten.
    """
    finished_at = _timestamp(now)
    terminal_state = _require_text(state, "state")
    if terminal_state not in TERMINAL_STATES:
        raise ValueError("finish_event needs a known terminal state")
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select state, finished_at from tool_audit_events where id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("That audit event does not exist")
        if row["finished_at"] is not None or row["state"] != STARTED:
            raise ValueError("That audit event is already terminal")
        conn.execute(
            "update tool_audit_events set state = ?, result_summary_json = ?, error_message = ?, "
            "abandonment_reason = ?, target_kind = coalesce(target_kind, ?), "
            "target_id = coalesce(target_id, ?), finished_at = ?, updated_at = ? where id = ?",
            (
                terminal_state,
                None if result_summary is None else _json_text(result_summary),
                _bounded_message(error_message),
                _bounded_message(abandonment_reason),
                target_kind,
                target_id,
                finished_at,
                finished_at,
                event_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return FinishedAuditEvent(id=event_id, state=terminal_state, finished_at=finished_at)


def reconcile_inflight(
    conn: sqlite3.Connection,
    *,
    reason: str = "startup_reconcile",
    now: datetime | None = None,
) -> int:
    """Convert any still-started rows into abandoned rows after restart."""
    finished_at = _timestamp(now)
    clean_reason = _bounded_message(reason)
    try:
        conn.execute("begin immediate")
        cursor = conn.execute(
            "update tool_audit_events set state = ?, abandonment_reason = ?, finished_at = ?, "
            "updated_at = ? where state = ? and finished_at is null",
            (ABANDONED, clean_reason, finished_at, finished_at, STARTED),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(cursor.rowcount)


def get_event(conn: sqlite3.Connection, event_id: str) -> dict[str, object]:
    """Read one audit row with decoded JSON fields for tests and integration."""
    row = conn.execute("select * from tool_audit_events where id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("That audit event does not exist")
    event = dict(row)
    event["arguments"] = json.loads(str(event.pop("arguments_json")))
    result_json = event.pop("result_summary_json")
    event["result_summary"] = None if result_json is None else json.loads(str(result_json))
    return event


def list_events(
    conn: sqlite3.Connection,
    *,
    class_id: int,
    session_id: int,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return recent durable activity for one class conversation, oldest first."""
    if limit < 1 or limit > 200:
        raise ValueError("Audit event limit must be between 1 and 200")
    rows = conn.execute(
        "select id from tool_audit_events where class_id = ? and session_id = ? "
        "order by started_at desc, rowid desc limit ?",
        (class_id, session_id, limit),
    ).fetchall()
    return [get_event(conn, str(row["id"])) for row in reversed(rows)]
