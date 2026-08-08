"""Single-use confirmation nonces for user-approved Phase 4 actions.

The model may propose a command or file-application action, but only the user can
confirm it. This module holds the storage contract for the confirmation token itself:

- at least 256 bits of randomness per token;
- only the token hash is stored;
- expiry defaults to 120 seconds;
- the token is bound to origin, class/session context, action kind, target id,
  current hash, and the exact canonical payload;
- consumption is atomic and single-use.

The migration lands later. For now the table contract is exposed as SQL so isolated
tests can create it without touching the shared migration stack.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.core.errors import ConflictError

DEFAULT_TTL_SECONDS = 120
TOKEN_BYTES = 32
ALLOWED_ACTION_KINDS = frozenset({"apply_change", "execute_command"})
INVALID_CONFIRMATION_MESSAGE = "That confirmation is invalid."
REPLAY_CONFIRMATION_MESSAGE = "That confirmation has already been used."
EXPIRED_CONFIRMATION_MESSAGE = "That confirmation expired. Refresh and try again."

TABLE_SQL = """
create table if not exists confirmation_nonces (
  id text primary key,
  token_hash text not null unique,
  origin text not null,
  class_id integer,
  session_id integer,
  action_kind text not null,
  target_id text not null,
  current_hash text,
  payload_hash text not null,
  expires_at text not null,
  consumed_at text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index if not exists confirmation_nonces_expires_idx
  on confirmation_nonces (expires_at);
"""


class ConfirmationError(ConflictError):
    """Base confirmation failure with a user-facing message."""


class ConfirmationExpiredError(ConfirmationError):
    """The nonce existed but no longer can be used."""


class ConfirmationReplayError(ConfirmationError):
    """The nonce already succeeded once and cannot be reused."""


@dataclass(frozen=True)
class IssuedConfirmation:
    """A newly issued confirmation token."""

    id: str
    token: str
    expires_at: str


@dataclass(frozen=True)
class ConsumedConfirmation:
    """A nonce successfully consumed once."""

    id: str
    consumed_at: str


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _require_text(value: str, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} cannot be blank")
    return clean


def canonical_payload(payload: object) -> str:
    """Deterministic payload bytes for binding and replay protection."""
    if isinstance(payload, str):
        return payload
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def payload_hash(payload: object) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_confirmation(
    conn: sqlite3.Connection,
    *,
    origin: str,
    class_id: int | None,
    session_id: int | None,
    action_kind: str,
    target_id: str,
    current_hash: str | None,
    payload: object,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> IssuedConfirmation:
    """Persist one fresh confirmation and return the plaintext token once."""
    if ttl_seconds <= 0 or ttl_seconds > DEFAULT_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be between 1 and {DEFAULT_TTL_SECONDS}")
    if action_kind not in ALLOWED_ACTION_KINDS:
        raise ValueError("Unknown confirmation action")
    if class_id is not None and class_id < 1:
        raise ValueError("class_id must be positive")
    if session_id is not None and session_id < 1:
        raise ValueError("session_id must be positive")
    issued_at = now or _now()
    token = secrets.token_hex(TOKEN_BYTES)
    record_id = uuid.uuid4().hex
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    try:
        conn.execute("begin immediate")
        conn.execute(
            "insert into confirmation_nonces ("
            "id, token_hash, origin, class_id, session_id, action_kind, target_id, "
            "current_hash, payload_hash, expires_at"
            ") values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                token_hash(token),
                _require_text(origin, "origin"),
                class_id,
                session_id,
                _require_text(action_kind, "action_kind"),
                _require_text(target_id, "target_id"),
                current_hash,
                payload_hash(payload),
                _timestamp(expires_at),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return IssuedConfirmation(id=record_id, token=token, expires_at=_timestamp(expires_at))


def consume_confirmation(
    conn: sqlite3.Connection,
    *,
    token: str,
    origin: str,
    class_id: int | None,
    session_id: int | None,
    action_kind: str,
    target_id: str,
    current_hash: str | None,
    payload: object,
    now: datetime | None = None,
) -> ConsumedConfirmation:
    """Atomically consume a matching confirmation token exactly once."""
    malformed_token = len(token) != TOKEN_BYTES * 2 or any(
        character not in "0123456789abcdef" for character in token
    )
    if malformed_token:
        raise ConfirmationError(INVALID_CONFIRMATION_MESSAGE)
    if action_kind not in ALLOWED_ACTION_KINDS:
        raise ConfirmationError(INVALID_CONFIRMATION_MESSAGE)
    compared_origin = _require_text(origin, "origin")
    compared_action = _require_text(action_kind, "action_kind")
    compared_target = _require_text(target_id, "target_id")
    compared_payload_hash = payload_hash(payload)
    compared_token_hash = token_hash(token)
    consumed_at = _timestamp(now)
    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select id, origin, class_id, session_id, action_kind, target_id, current_hash, "
            "payload_hash, expires_at, consumed_at "
            "from confirmation_nonces where token_hash = ?",
            (compared_token_hash,),
        ).fetchone()
        if row is None:
            raise ConfirmationError(INVALID_CONFIRMATION_MESSAGE)
        if row["consumed_at"] is not None:
            raise ConfirmationReplayError(REPLAY_CONFIRMATION_MESSAGE)
        if _timestamp(now) >= str(row["expires_at"]):
            raise ConfirmationExpiredError(EXPIRED_CONFIRMATION_MESSAGE)
        if (
            row["origin"] != compared_origin
            or row["class_id"] != class_id
            or row["session_id"] != session_id
            or row["action_kind"] != compared_action
            or row["target_id"] != compared_target
            or row["current_hash"] != current_hash
            or row["payload_hash"] != compared_payload_hash
        ):
            raise ConfirmationError(INVALID_CONFIRMATION_MESSAGE)
        cursor = conn.execute(
            "update confirmation_nonces set consumed_at = ? where id = ? and consumed_at is null",
            (consumed_at, row["id"]),
        )
        if cursor.rowcount != 1:
            raise ConfirmationReplayError(REPLAY_CONFIRMATION_MESSAGE)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ConsumedConfirmation(id=str(row["id"]), consumed_at=consumed_at)
