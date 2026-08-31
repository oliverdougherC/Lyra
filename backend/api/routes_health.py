"""Process liveness plus configuration-only readiness probes.

The launcher uses these endpoints to distinguish a running HTTP process from an
application that can safely serve requests. Optional Exa web research is reported from
local configuration only: Lyra never probes Exa during launch or readiness checks.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from backend.core.app_settings import get_settings_row
from backend.core.diagnostics import build_diagnostics
from backend.storage.database import MIGRATIONS_DIR, connect

logger = logging.getLogger("lyra.health")

router = APIRouter(prefix="/api/health", tags=["health"])


class LiveHealth(BaseModel):
    status: Literal["ok"]


class ComponentHealth(BaseModel):
    status: Literal[
        "ready",
        "available",
        "temporarily_unavailable",
        "unavailable",
        "misconfigured",
        "not_ready",
        "skipped",
    ]
    required: bool
    message: str


class ReadyHealth(BaseModel):
    status: Literal["ready", "not_ready"]
    components: dict[str, ComponentHealth]


@dataclass(frozen=True)
class _DatabaseProbe:
    component: ComponentHealth
    allow_web_research: bool | None = None


@router.get("/live", response_model=LiveHealth)
def live() -> LiveHealth:
    """Prove only that the FastAPI process can answer HTTP requests."""
    return LiveHealth(status="ok")


@router.get("/diagnostics")
def diagnostics() -> dict[str, object]:
    """A structured, redacted snapshot of this install, safe to paste into a bug report."""
    conn = connect()
    try:
        return build_diagnostics(conn)
    finally:
        conn.close()


@router.get(
    "/ready",
    response_model=ReadyHealth,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyHealth}},
)
def ready(response: Response) -> ReadyHealth:
    """Report required database readiness and optional web-research configuration."""
    database = _check_database()
    components = {"database": database.component}

    if database.allow_web_research is None:
        components["web_research"] = ComponentHealth(
            status="skipped",
            required=False,
            message="Web research was not checked because the database is not ready.",
        )
    else:
        components["web_research"] = _web_research_component(
            allow_web_research=database.allow_web_research
        )

    if database.component.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyHealth(status="not_ready", components=components)
    return ReadyHealth(status="ready", components=components)


def _check_database() -> _DatabaseProbe:
    """Verify access, migration currency, and the required singleton settings row."""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        conn.execute("select 1").fetchone()
        current_version = int(conn.execute("pragma user_version").fetchone()[0])
        expected_version = _latest_migration_version()
        if current_version != expected_version:
            return _DatabaseProbe(
                ComponentHealth(
                    status="not_ready",
                    required=True,
                    message="Database migrations are not current.",
                )
            )
        settings_row = get_settings_row(conn)
        return _DatabaseProbe(
            ComponentHealth(
                status="ready",
                required=True,
                message="Database is ready.",
            ),
            allow_web_research=bool(settings_row["allow_web_research"]),
        )
    except Exception as exc:
        logger.warning("Database readiness probe failed (%s)", type(exc).__name__)
        return _DatabaseProbe(
            ComponentHealth(
                status="unavailable",
                required=True,
                message="Database is unavailable.",
            )
        )
    finally:
        if conn is not None:
            conn.close()


def _web_research_component(*, allow_web_research: bool) -> ComponentHealth:
    if not allow_web_research:
        return ComponentHealth(
            status="not_ready",
            required=False,
            message="Web research is configured but currently disabled in Settings.",
        )
    return ComponentHealth(
        status="available",
        required=False,
        message=(
            "Web research is enabled. Credential presence and connectivity are checked only "
            "when Settings or an explicit Exa action requests them."
        ),
    )


def _latest_migration_version() -> int:
    versions: list[int] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        prefix, separator, _ = path.name.partition("_")
        if not separator or not prefix.isdigit():
            raise RuntimeError("A database migration filename is invalid.")
        versions.append(int(prefix))
    if not versions:
        raise RuntimeError("No database migrations are available.")
    return max(versions)
