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
        "tools_supported",
        "tools_message",
        "vision_supported",
        "vision_message",
    }
)

# Changing either of these invalidates what was measured about the endpoint. Tool support
# and vision are both properties of the server and the model together, so a probe result
# from the previous pair says nothing about the new one and must not be carried over.
_PROBE_INPUTS = ("endpoint_url", "model")
_PROBE_RESULTS = ("tools_supported", "tools_message", "vision_supported", "vision_message")

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

    values = dict(values)
    if _changes_endpoint(conn, values):
        # Forgotten rather than re-probed here: probing is a network call and this is a
        # synchronous write on the settings screen. The next solve asks again.
        for column in _PROBE_RESULTS:
            values.setdefault(column, None)

    # Only the column names reach the SQL text, and each has just been checked against
    # the allowlist above. Every value is bound.
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(
        f"update settings set {assignments} where id = 1",  # noqa: S608
        tuple(values.values()),
    )
    conn.commit()


def _changes_endpoint(conn: sqlite3.Connection, values: dict[str, object]) -> bool:
    """Whether this write points the tutor at a different server or model."""
    row = get_settings_row(conn)
    return any(column in values and values[column] != row[column] for column in _PROBE_INPUTS)


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


def document_text_allowed(conn: sqlite3.Connection) -> str | None:
    """Whether document text may be sent to the tutor endpoint at all, or why it may not.

    The rule from the Inference Posture section of docs/architecture.md, written once.
    It is about **document text leaving the machine**, not about which feature is doing
    it: profile extraction and solver segmentation both send whole documents to the tutor
    model, so both ask here. A second copy of this rule per feature is a second place for
    one of them to quietly stop asking.

    Returns:
        None when document text may be sent, otherwise `no_endpoint` or
        `remote_unacknowledged`.
    """
    row = get_settings_row(conn)

    endpoint_url = (row["endpoint_url"] or "").strip()
    if not endpoint_url:
        return NO_ENDPOINT

    if not is_local_endpoint(endpoint_url) and not row["remote_ack"]:
        return REMOTE_UNACKNOWLEDGED

    return None


def extraction_allowed(conn: sqlite3.Connection) -> str | None:
    """Whether profile extraction may run, or why it may not.

    Its own Settings toggle first, then the shared document-text rule above.

    Returns:
        None when extraction may run, otherwise the skip reason recorded on the
        document: `extraction_disabled`, `no_endpoint`, or `remote_unacknowledged`.
    """
    if not get_settings_row(conn)["extraction_enabled"]:
        return EXTRACTION_DISABLED
    return document_text_allowed(conn)
