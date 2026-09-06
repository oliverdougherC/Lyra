"""Reads and writes of the single-row `settings` table, plus the rules derived from it.

The table holds only non-secret configuration. The tutor API key lives in the keychain
(`backend.storage.secrets`) and is joined onto the row only inside `resolve_tutor_config`,
whose result must never be logged.
"""

import sqlite3
from dataclasses import dataclass, field

from backend.core.errors import ConfigurationError
from backend.llm.locality import is_local_endpoint
from backend.storage.secrets import get_api_key, get_tutor_credential

UPDATABLE_COLUMNS = frozenset(
    {
        "endpoint_url",
        "tutor_credential_id",
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
        "allow_web_research",
        "parallel_requests",
        "parallel_concurrency",
        "source_content_enabled",
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
        tools_supported: The endpoint's measured tool-calling verdict from the SAME settings
            read that produced the endpoint: True when probing measured it, False when the
            measurement (or a remembered first-request refusal) proved it away, None when
            unknown. Carried on the snapshot so a turn plans and sends from one read - an
            endpoint that changes between reads changes the verdict with it, and a caller
            must never pair one read's endpoint with a second read's verdict.
    """

    endpoint_url: str
    api_key: str | None = field(repr=False)
    model: str | None
    context_window: int
    tools_supported: bool | None = None
    credential_id: str | None = None


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


def invalidate_probe_results(conn: sqlite3.Connection) -> None:
    """Invalidate measurements before mutating a credential outside SQLite."""
    conn.execute(
        "update settings set probe_revision = probe_revision + 1, "
        "tools_supported = null, tools_message = null, "
        "vision_supported = null, vision_message = null where id = 1"
    )
    conn.commit()


def publish_probe_result(
    conn: sqlite3.Connection, revision: int, kind: str, ok: bool, message: str
) -> bool:
    """Validate and publish in one SQLite write, with no read/write race or secret."""
    if kind not in ("tools", "vision"):
        raise ValueError("Unknown probe kind")
    cursor = conn.execute(
        f"update settings set {kind}_supported = ?, {kind}_message = ? "  # noqa: S608
        "where id = 1 and probe_revision = ?",
        (int(ok), message, revision),
    )
    conn.commit()
    return cursor.rowcount == 1


def _changes_endpoint(conn: sqlite3.Connection, values: dict[str, object]) -> bool:
    """Whether this write points the tutor at a different server or model."""
    row = get_settings_row(conn)
    return any(column in values and values[column] != row[column] for column in _PROBE_INPUTS)


@dataclass(frozen=True)
class TutorAccess:
    """The tutor endpoint configuration and its document-text permission, from one read.

    The two are inseparable on purpose. `document_text_allowed` answers a question about a
    specific endpoint - is *this* destination one document text may be sent to - and the
    answer is worthless if the endpoint it was asked about is not the endpoint the request
    then goes to. Resolving the config and deriving the permission from a single
    `get_settings_row` closes the window in which the settings could change between "may I
    send to X?" and "send to Y": whatever `config` points at is exactly what
    `document_block` and `remote_ack` were evaluated against. A document-sending caller
    takes this snapshot once and uses it for the whole operation, so a settings change after
    the snapshot cannot split the authorization from the endpoint it authorized.

    Attributes:
        config: The resolved endpoint, or None only when no endpoint is configured (in which
            case `document_block` is `no_endpoint`). Populated even when document text is
            blocked, because the block is a fact about *this* endpoint.
        document_block: None when document text may be sent to `config`, otherwise the reason
            it may not (`extraction_disabled`, `no_endpoint`, or `remote_unacknowledged`).
        remote_ack: The acknowledgement flag from the same row, for callers (recognition)
            that pass it on to a lower layer whose re-check must agree with this snapshot.
    """

    config: TutorConfig | None
    document_block: str | None
    remote_ack: bool

    @property
    def document_allowed(self) -> bool:
        """Whether document-derived text may be sent to `config`."""
        return self.document_block is None


def _tutor_config_from_row(row: sqlite3.Row) -> TutorConfig | None:
    """The endpoint configuration described by `row`, or None when none is set."""
    endpoint_url = (row["endpoint_url"] or "").strip()
    if not endpoint_url:
        return None
    return TutorConfig(
        endpoint_url=endpoint_url,
        api_key=credential_for_row(row),
        credential_id=row["tutor_credential_id"] if "tutor_credential_id" in row.keys() else None,  # noqa: SIM118
        model=row["model"],
        context_window=int(row["context_window"]),
        tools_supported=_tool_support_from_row(row),
    )


def credential_for_row(row: sqlite3.Row) -> str | None:
    endpoint = (row["endpoint_url"] or "").strip() or None
    if "tutor_credential_id" in row.keys() and row["tutor_credential_id"]:  # noqa: SIM118
        return get_tutor_credential(row["tutor_credential_id"], endpoint)
    # Migration binds the pre-existing secret to its original endpoint. Endpoint-only
    # changes never inherit it, even if a reader resolves a retained old settings row.
    if "legacy_credential_endpoint" in row.keys():  # noqa: SIM118
        authorized = (row["legacy_credential_endpoint"] or "").strip() or None
        if authorized != endpoint:
            return None
    return get_api_key()


def _tool_support_from_row(row: sqlite3.Row) -> bool | None:
    """The row's tool-support verdict: None (unknown), False, or True.

    The column is absent from pre-migration-rows and from test rows shaped like this one
    but cut before it; both read as "unknown", which is the value that asks the next turn
    to decide rather than assume.
    """
    if "tools_supported" not in row.keys():  # noqa: SIM118
        return None
    stored = row["tools_supported"]
    return None if stored is None else bool(stored)


def _document_block_from_row(row: sqlite3.Row) -> str | None:
    """Why document text may not be sent to the endpoint `row` describes, or None if it may."""
    endpoint_url = (row["endpoint_url"] or "").strip()
    if not endpoint_url:
        return NO_ENDPOINT
    if not is_local_endpoint(endpoint_url) and not row["remote_ack"]:
        return REMOTE_UNACKNOWLEDGED
    return None


def _extraction_block_from_row(row: sqlite3.Row) -> str | None:
    """Profile extraction's own toggle, then the shared document-text rule, from one row."""
    if not row["extraction_enabled"]:
        return EXTRACTION_DISABLED
    return _document_block_from_row(row)


def resolve_tutor_config(conn: sqlite3.Connection) -> TutorConfig:
    """The tutor endpoint configuration, or a ConfigurationError when none is set."""
    config = _tutor_config_from_row(get_settings_row(conn))
    if config is None:
        raise ConfigurationError("No tutor endpoint is configured. Add one in Settings.")
    return config


def document_text_allowed(conn: sqlite3.Connection) -> str | None:
    """Whether document text may be sent to the tutor endpoint at all, or why it may not.

    The rule from the Inference Posture section of docs/architecture.md, written once.
    It is about **document text leaving the machine**, not about which feature is doing
    it: profile extraction and solver segmentation both send whole documents to the tutor
    model, so both ask here. A second copy of this rule per feature is a second place for
    one of them to quietly stop asking.

    A caller that then *uses* the endpoint - resolves a `TutorConfig` and sends the request -
    must not call this separately from resolving that config: two independent reads can
    straddle a settings change and authorize one endpoint while sending to another. Such a
    caller takes `resolve_tutor_access` instead, which answers this question and returns the
    config from the same snapshot. This function stands alone only for a caller that is
    deciding whether to proceed at all and resolves nothing (or resolves through
    `resolve_tutor_access`).

    Returns:
        None when document text may be sent, otherwise `no_endpoint` or
        `remote_unacknowledged`.
    """
    return _document_block_from_row(get_settings_row(conn))


def extraction_allowed(conn: sqlite3.Connection) -> str | None:
    """Whether profile extraction may run, or why it may not.

    Its own Settings toggle first, then the shared document-text rule above.

    Returns:
        None when extraction may run, otherwise the skip reason recorded on the
        document: `extraction_disabled`, `no_endpoint`, or `remote_unacknowledged`.
    """
    return _extraction_block_from_row(get_settings_row(conn))


def resolve_tutor_access(conn: sqlite3.Connection, *, for_extraction: bool = False) -> TutorAccess:
    """The tutor config and its document-text permission, from a single settings read.

    The atomic form of "resolve the endpoint, and check whether document text may be sent to
    it". Every document-sending caller - chat, solving, study, writing/review, segmentation,
    recognition, profile extraction/consolidation - takes this rather than calling
    `document_text_allowed` and `resolve_tutor_config` as two separate reads, so the endpoint
    a turn is authorized against is provably the endpoint it is sent to.

    Args:
        for_extraction: Apply profile extraction's Settings toggle as well, so an extraction
            caller gets `extraction_disabled` from the same snapshot instead of re-reading.

    Returns:
        A `TutorAccess` whose `document_block` is None exactly when the operation may send
        document text to its `config`.
    """
    row = get_settings_row(conn)
    block = _extraction_block_from_row(row) if for_extraction else _document_block_from_row(row)
    return TutorAccess(
        config=_tutor_config_from_row(row),
        document_block=block,
        remote_ack=bool(row["remote_ack"]),
    )
