"""Process liveness and dependency-aware readiness probes.

The launcher uses these endpoints to distinguish a running HTTP process from an
application that can safely serve requests. Firecrawl is reported separately because
Lyra remains usable for local documents when optional web research is unavailable.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from backend.core.app_settings import get_settings_row
from backend.core.firecrawl import (
    FirecrawlClient,
    FirecrawlError,
    FirecrawlMisconfiguredError,
    FirecrawlTransientError,
)
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
    firecrawl_base_url: str | None = None
    firecrawl_scrape_enabled: bool | None = None


@router.get("/live", response_model=LiveHealth)
def live() -> LiveHealth:
    """Prove only that the FastAPI process can answer HTTP requests."""
    return LiveHealth(status="ok")


@router.get(
    "/ready",
    response_model=ReadyHealth,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyHealth}},
)
def ready(response: Response) -> ReadyHealth:
    """Report required database readiness and optional Firecrawl availability."""
    database = _check_database()
    components = {"database": database.component}

    if database.firecrawl_base_url is None:
        components["firecrawl"] = ComponentHealth(
            status="skipped",
            required=False,
            message="Firecrawl was not checked because the database is not ready.",
        )
        components["web_scrape"] = ComponentHealth(
            status="skipped",
            required=False,
            message="The web scrape policy was not checked because the database is not ready.",
        )
    else:
        components["firecrawl"] = _check_firecrawl(database.firecrawl_base_url)
        enabled = database.firecrawl_scrape_enabled is True
        components["web_scrape"] = ComponentHealth(
            status="ready" if enabled else "not_ready",
            required=False,
            message=(
                "Web scraping is enabled."
                if enabled
                else "Web scraping remains disabled until the redirect-safety gate passes."
            ),
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
            firecrawl_base_url=str(settings_row["firecrawl_base_url"]),
            firecrawl_scrape_enabled=bool(settings_row["firecrawl_scrape_enabled"]),
        )
    except Exception as exc:
        # Health responses and logs intentionally omit exception text: sqlite errors commonly
        # contain absolute paths, which this unauthenticated loopback API must not disclose.
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


def _check_firecrawl(base_url: str) -> ComponentHealth:
    """Probe optional web research without allowing it to fail Lyra readiness."""
    try:
        FirecrawlClient(base_url=base_url).check_readiness()
    except (FirecrawlMisconfiguredError, ValueError):
        return ComponentHealth(
            status="misconfigured",
            required=False,
            message="Firecrawl is misconfigured; web research is disabled.",
        )
    except (FirecrawlTransientError, FirecrawlError):
        return ComponentHealth(
            status="temporarily_unavailable",
            required=False,
            message="Firecrawl is temporarily unavailable; web research is disabled.",
        )
    except Exception as exc:
        logger.warning("Firecrawl readiness probe failed (%s)", type(exc).__name__)
        return ComponentHealth(
            status="temporarily_unavailable",
            required=False,
            message="Firecrawl is temporarily unavailable; web research is disabled.",
        )
    return ComponentHealth(
        status="available",
        required=False,
        message="Firecrawl is available.",
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
