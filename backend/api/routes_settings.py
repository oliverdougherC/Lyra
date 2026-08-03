"""Settings endpoints: stored configuration, plus live checks against the tutor endpoint.

The API key is write-only across this boundary. It is accepted on `PUT`, handed to the
keychain, and never read back: responses carry only `api_key_set` and `api_key_storage`.
Theme is not stored server-side at all; it lives in the browser.
"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.core.app_settings import (
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


class SettingsUpdate(BaseModel):
    """A partial update. Only the fields actually sent are applied."""

    endpoint_url: str | None = None
    model: str | None = None
    context_window: int | None = Field(default=None, ge=512)
    extraction_enabled: bool | None = None
    remote_ack: bool | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    api_key: str | None = Field(
        default=None,
        description="Routed to the keychain, never stored in the database. Empty string deletes.",
    )


class ConnectionTestResult(BaseModel):
    """Outcome of a live probe against the configured tutor endpoint."""

    ok: bool
    model_count: int
    message: str


class ModelList(BaseModel):
    """Models the configured tutor endpoint advertises."""

    models: list[str]


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
    )


@router.get("/settings", response_model=SettingsRead)
def read_settings(conn: DbConn) -> SettingsRead:
    return _settings_response(get_settings_row(conn))


@router.put("/settings", response_model=SettingsRead)
def write_settings(payload: SettingsUpdate, conn: DbConn) -> SettingsRead:
    current = get_settings_row(conn)
    values = payload.model_dump(exclude_unset=True)

    if "api_key" in values:
        api_key = values.pop("api_key")
        if api_key == "":
            # The interface sends an empty string for "forget my key". A null means
            # "leave whatever is stored alone", which is the no-op below.
            secrets.delete_api_key()
        elif api_key is not None:
            secrets.set_api_key(api_key)

    if "endpoint_url" in values:
        endpoint_url = _normalize_endpoint(values["endpoint_url"])
        values["endpoint_url"] = endpoint_url
        changed = endpoint_url != _normalize_endpoint(current["endpoint_url"])
        if changed and "remote_ack" not in values:
            # The acknowledgement is consent to send document text to one specific
            # host, so repointing the endpoint withdraws it rather than letting it
            # carry over silently to a new destination. A `remote_ack` sent in this
            # same request is a deliberate acknowledgement of the incoming host and
            # is honoured instead.
            values["remote_ack"] = 0

    update_settings_row(conn, values)
    return _settings_response(get_settings_row(conn))


@router.post("/settings/test-connection", response_model=ConnectionTestResult)
async def test_endpoint_connection(conn: DbConn) -> ConnectionTestResult:
    config = resolve_tutor_config(conn)
    result = await client.test_connection(config.endpoint_url, config.api_key)
    return ConnectionTestResult(
        ok=result.ok, model_count=result.model_count, message=result.message
    )


@router.get("/settings/models", response_model=ModelList)
async def read_endpoint_models(conn: DbConn) -> ModelList:
    config = resolve_tutor_config(conn)
    return ModelList(models=await client.list_models(config.endpoint_url, config.api_key))
