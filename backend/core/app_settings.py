"""Reads and writes of the single-row `settings` table, plus the rules derived from it.

The table holds only non-secret configuration. The tutor API key lives in the keychain
(`backend.storage.secrets`) and is joined onto the row only inside `resolve_tutor_config`,
whose result must never be logged.
"""

import sqlite3
from dataclasses import dataclass

from backend.core.errors import ConfigurationError
from backend.llm.locality import is_local_endpoint
from backend.storage.secrets import get_api_key

UPDATABLE_COLUMNS = frozenset(
    {
        "endpoint_url",
        "model",
        "context_window",
        "extraction_enabled",
        "remote_ack",
        "embedding_model",
        "embedding_dim",
    }
)

EXTRACTION_DISABLED = "extraction_disabled"
NO_ENDPOINT = "no_endpoint"
REMOTE_UNACKNOWLEDGED = "remote_unacknowledged"


@dataclass(frozen=True)
class TutorConfig:
    """Everything needed to call the user's tutor endpoint.

    Attributes:
        endpoint_url: Base URL including its `/v1` suffix. Callers append only the path.
        api_key: The stored key, or None when the endpoint needs no authentication.
        model: The model identifier the user picked, or None to let the server choose.
        context_window: Token budget the endpoint is assumed to accept.
    """

    endpoint_url: str
    api_key: str | None
    model: str | None
    context_window: int


def get_settings_row(conn: sqlite3.Connection) -> sqlite3.Row:
    """The settings row. Migration 001 inserts it, so its absence is a bug, not input."""
    row = conn.execute("select * from settings where id = 1").fetchone()
    if row is None:
        raise RuntimeError("The settings row is missing. The database was not migrated.")
    return row


def update_settings_row(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    """Update the named settings columns. Raises ValueError on an unknown column."""
    if not values:
        return

    unknown = sorted(set(values) - UPDATABLE_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown settings column(s): {', '.join(unknown)}")

    # Only the column names reach the SQL text, and each has just been checked against
    # the allowlist above. Every value is bound.
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(
        f"update settings set {assignments} where id = 1",  # noqa: S608
        tuple(values.values()),
    )
    conn.commit()


def resolve_tutor_config(conn: sqlite3.Connection) -> TutorConfig:
    """The tutor endpoint configuration, or a ConfigurationError when none is set."""
    row = get_settings_row(conn)
    endpoint_url = (row["endpoint_url"] or "").strip()
    if not endpoint_url:
        raise ConfigurationError("No tutor endpoint is configured. Add one in Settings.")

    return TutorConfig(
        endpoint_url=endpoint_url,
        api_key=get_api_key(),
        model=row["model"],
        context_window=int(row["context_window"]),
    )


def extraction_allowed(conn: sqlite3.Connection) -> str | None:
    """Whether profile extraction may send document text out, or why it may not.

    Returns:
        None when extraction may run, otherwise the skip reason recorded on the
        document: `extraction_disabled`, `no_endpoint`, or `remote_unacknowledged`.
    """
    row = get_settings_row(conn)

    if not row["extraction_enabled"]:
        return EXTRACTION_DISABLED

    endpoint_url = (row["endpoint_url"] or "").strip()
    if not endpoint_url:
        return NO_ENDPOINT

    if not is_local_endpoint(endpoint_url) and not row["remote_ack"]:
        return REMOTE_UNACKNOWLEDGED

    return None
