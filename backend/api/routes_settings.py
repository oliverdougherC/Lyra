"""Settings endpoints: stored configuration, plus explicit tutor and Exa checks.

Secret values are write-only across this boundary. They are accepted on `PUT`, handed to
the keychain abstraction, and never read back: responses carry only `*_key_set` and
`*_key_storage`. Theme is not stored server-side at all; it lives in the browser.
"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.core import exa
from backend.core.app_settings import (
    TutorConfig,
    get_settings_row,
    resolve_tutor_config,
    update_settings_row,
)
from backend.llm import client
from backend.llm.locality import hostname_of, is_local_endpoint
from backend.storage import secrets
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["settings"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]


class SettingsRead(BaseModel):
    """Everything the interface may know about the current configuration."""

    endpoint_url: str | None
    model: str | None
    context_window: int
    extraction_enabled: bool
    remote_ack: bool
    api_key_set: bool
    api_key_storage: Literal["keychain", "file"]
    endpoint_is_local: bool | None
    endpoint_host: str | None
    embedding_model: str | None
    embedding_dim: int | None
    # Null means nobody has asked this endpoint yet, which is distinct from asked and no.
    # The screen renders all three, because "not checked" and "cannot check" cost the
    # student different things.
    tools_supported: bool | None
    tools_message: str | None
    # The same three states, for the same reason. Without vision, scanned documents cannot
    # be read at all, and the interface says so plainly instead of offering an action that
    # would fail one page at a time.
    vision_supported: bool | None
    vision_message: str | None
    allow_web_research: bool
    parallel_requests: bool
    parallel_concurrency: int
    exa_api_key_set: bool
    exa_api_key_storage: Literal["keychain", "file"]


class SettingsUpdate(BaseModel):
    """A partial update. Only the fields actually sent are applied."""

    endpoint_url: str | None = None
    model: str | None = None
    context_window: int | None = Field(default=None, ge=512)
    extraction_enabled: bool | None = None
    remote_ack: bool | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    allow_web_research: bool | None = None
    parallel_requests: bool | None = None
    parallel_concurrency: int | None = Field(default=None, ge=1)
    exa_api_key: str | None = Field(
        default=None,
        description="Routed to the keychain, never stored in the database. Empty string deletes.",
    )
    api_key: str | None = Field(
        default=None,
        description="Routed to the keychain, never stored in the database. Empty string deletes.",
    )


class ConnectionTestResult(BaseModel):
    """Outcome of a live probe against the configured tutor endpoint."""

    ok: bool
    model_count: int
    message: str


class ToolSupportResult(BaseModel):
    """Whether this endpoint can run the checks Lyra verifies solutions with."""

    ok: bool
    message: str


class VisionSupportResult(BaseModel):
    """Whether this endpoint can read an image, which is what recognition needs."""

    ok: bool
    message: str


class ModelList(BaseModel):
    """Models the configured tutor endpoint advertises."""

    models: list[str]


class ExaTestResult(BaseModel):
    """Outcome of an explicit Exa provider probe."""

    ok: bool
    status: Literal[
        "available",
        "missing_key",
        "invalid_key",
        "permission_denied",
        "quota_exhausted",
        "rate_limited",
        "timeout",
        "offline",
        "malformed_response",
        "temporarily_unavailable",
    ]
    message: str


def _normalize_endpoint(value: object) -> str | None:
    """Blank and whitespace-only endpoints mean 'not configured'."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _settings_response(row: sqlite3.Row) -> SettingsRead:
    """Build the response, deriving locality from the stored endpoint."""
    endpoint_url = _normalize_endpoint(row["endpoint_url"])
    return SettingsRead(
        endpoint_url=endpoint_url,
        model=row["model"],
        context_window=int(row["context_window"]),
        extraction_enabled=bool(row["extraction_enabled"]),
        remote_ack=bool(row["remote_ack"]),
        api_key_set=secrets.has_api_key(),
        api_key_storage=secrets.api_key_storage(),
        endpoint_is_local=is_local_endpoint(endpoint_url) if endpoint_url else None,
        endpoint_host=hostname_of(endpoint_url) if endpoint_url else None,
        embedding_model=row["embedding_model"],
        embedding_dim=row["embedding_dim"],
        tools_supported=None if row["tools_supported"] is None else bool(row["tools_supported"]),
        tools_message=row["tools_message"],
        vision_supported=None if row["vision_supported"] is None else bool(row["vision_supported"]),
        vision_message=row["vision_message"],
        allow_web_research=bool(row["allow_web_research"]),
        parallel_requests=bool(row["parallel_requests"]),
        parallel_concurrency=int(row["parallel_concurrency"]),
        exa_api_key_set=secrets.has_exa_api_key(),
        exa_api_key_storage=secrets.exa_api_key_storage(),
    )


@router.get("/settings", response_model=SettingsRead)
def read_settings(conn: DbConn) -> SettingsRead:
    return _settings_response(get_settings_row(conn))


@router.put("/settings", response_model=SettingsRead)
def write_settings(payload: SettingsUpdate, conn: DbConn) -> SettingsRead:
    current = get_settings_row(conn)
    values = payload.model_dump(exclude_unset=True)
    for column in ("allow_web_research", "parallel_requests", "parallel_concurrency"):
        if values.get(column) is None:
            values.pop(column, None)

    key_changed = False
    if "api_key" in values:
        api_key = values.pop("api_key")
        key_changed = api_key is not None and (api_key or None) != secrets.get_api_key()
        if api_key == "":
            # The interface sends an empty string for "forget my key". A null means
            # "leave whatever is stored alone", which is the no-op below.
            secrets.delete_api_key()
        elif api_key is not None:
            secrets.set_api_key(api_key)

    if "exa_api_key" in values:
        exa_api_key = values.pop("exa_api_key")
        if exa_api_key == "":
            secrets.delete_exa_api_key()
        elif exa_api_key is not None:
            secrets.set_exa_api_key(exa_api_key)

    endpoint_changed = False
    if "endpoint_url" in values:
        endpoint_url = _normalize_endpoint(values["endpoint_url"])
        values["endpoint_url"] = endpoint_url
        endpoint_changed = endpoint_url != _normalize_endpoint(current["endpoint_url"])
        if endpoint_changed and "remote_ack" not in values:
            # The acknowledgement is consent to send document text to one specific
            # host, so repointing the endpoint withdraws it rather than letting it
            # carry over silently to a new destination. A `remote_ack` sent in this
            # same request is a deliberate acknowledgement of the incoming host and
            # is honoured instead.
            values["remote_ack"] = 0

    model_changed = "model" in values and values["model"] != current["model"]
    if key_changed:
        # Credentials can select different capabilities even at the same endpoint/model.
        values.update(
            tools_supported=None, tools_message=None, vision_supported=None, vision_message=None
        )
    if endpoint_changed or model_changed or key_changed:
        # The client remembers, per (endpoint, model), which `response_format` an
        # endpoint refused, and only ever demotes. A settings change is the one moment
        # the user is telling us the configuration is different - commonly the same URL
        # in front of a restarted or upgraded server - so the record is wiped rather
        # than left to cap constrained decoding against whatever answers now.
        client.reset_json_support()

    update_settings_row(conn, values)
    return _settings_response(get_settings_row(conn))


@router.post("/settings/test-connection", response_model=ConnectionTestResult)
async def test_endpoint_connection(conn: DbConn) -> ConnectionTestResult:
    config = resolve_tutor_config(conn)
    result = await client.test_connection(config.endpoint_url, config.api_key)
    return ConnectionTestResult(
        ok=result.ok, model_count=result.model_count, message=result.message
    )


def _probe_configuration_unchanged(conn: sqlite3.Connection, config: TutorConfig) -> bool:
    """Do not attach an old in-flight measurement to a newly selected setup."""
    current = get_settings_row(conn)
    return (
        _normalize_endpoint(current["endpoint_url"]) == config.endpoint_url
        and current["model"] == config.model
        and secrets.get_api_key() == config.api_key
    )


@router.post("/settings/test-tools", response_model=ToolSupportResult)
async def test_endpoint_tools(conn: DbConn) -> ToolSupportResult:
    """Ask the endpoint for one trivial tool call, and record what happened.

    A real inference call, because an OpenAI-compatible server advertises nothing about
    tool calling and several accept the field and then ignore it. The result is stored so
    the next solve does not have to ask again, and so the screen can state plainly what
    this endpoint costs the student: without tool support, solutions are still produced
    and every one of them carries the verdict `Not checked`.
    """
    config = resolve_tutor_config(conn)
    support = await client.probe_tool_support(config.endpoint_url, config.api_key, config.model)
    if not _probe_configuration_unchanged(conn, config):
        return ToolSupportResult(
            ok=False, message="Connection settings changed. Test tool support again."
        )
    update_settings_row(
        conn, {"tools_supported": int(support.ok), "tools_message": support.message}
    )
    return ToolSupportResult(ok=support.ok, message=support.message)


@router.post("/settings/test-vision", response_model=VisionSupportResult)
async def test_endpoint_vision(conn: DbConn) -> VisionSupportResult:
    """Send the endpoint one small rendered image and check it read the number on it.

    A real inference call, and it has to be one that can be marked right or wrong. A server
    with no vision path still accepts a content-part array and answers from the text half,
    so "the request did not fail" proves nothing at all. The probe draws a five-digit number
    and asks for it back.

    Stored, so recognition can offer or withhold itself without a network round trip on
    every document row.
    """
    config = resolve_tutor_config(conn)
    support = await client.probe_vision_support(config.endpoint_url, config.api_key, config.model)
    if not _probe_configuration_unchanged(conn, config):
        return VisionSupportResult(
            ok=False, message="Connection settings changed. Test image support again."
        )
    update_settings_row(
        conn, {"vision_supported": int(support.ok), "vision_message": support.message}
    )
    return VisionSupportResult(ok=support.ok, message=support.message)


@router.get("/settings/models", response_model=ModelList)
async def read_endpoint_models(conn: DbConn) -> ModelList:
    config = resolve_tutor_config(conn)
    return ModelList(models=await client.list_models(config.endpoint_url, config.api_key))


@router.post("/settings/test-exa", response_model=ExaTestResult)
def test_exa(conn: DbConn) -> ExaTestResult:
    """Probe Exa explicitly; never as part of application startup."""
    del conn
    key = secrets.get_exa_api_key()
    if key is None:
        return ExaTestResult(
            ok=False,
            status="missing_key",
            message="No Exa API key is configured.",
        )
    try:
        exa.ExaClient(api_key=key).check_readiness()
    except exa.ExaQuotaExceededError:
        return ExaTestResult(
            ok=False,
            status="quota_exhausted",
            message="The Exa account or API key has exhausted its budget.",
        )
    except exa.ExaRateLimitError:
        return ExaTestResult(
            ok=False,
            status="rate_limited",
            message="Exa rate limited the request. Retry shortly.",
        )
    except exa.ExaAuthError:
        return ExaTestResult(
            ok=False,
            status="invalid_key",
            message="The Exa API key is invalid or not authorized.",
        )
    except exa.ExaPermissionError:
        return ExaTestResult(
            ok=False,
            status="permission_denied",
            message="The Exa API key cannot access the required search capability.",
        )
    except exa.ExaTimeoutError:
        return ExaTestResult(
            ok=False,
            status="timeout",
            message="Exa timed out before responding.",
        )
    except exa.ExaOfflineError:
        return ExaTestResult(
            ok=False,
            status="offline",
            message="Exa could not be reached.",
        )
    except exa.ExaSchemaError:
        return ExaTestResult(
            ok=False,
            status="malformed_response",
            message="Exa returned a malformed response.",
        )
    except (
        exa.ExaConnectionInterruptedError,
        exa.ExaTransientError,
        exa.ExaError,
    ):
        return ExaTestResult(
            ok=False,
            status="temporarily_unavailable",
            message="Exa is temporarily unavailable; web research is disabled.",
        )
    return ExaTestResult(ok=True, status="available", message="Exa is available.")
