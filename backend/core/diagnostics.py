"""A structured, redacted snapshot of a Lyra installation, safe to paste into a bug report.

A student reporting a problem needs to say what state their install is in without handing
over their coursework, their tutor key, or the shape of their home directory. This module
gathers exactly the facts a maintainer needs - schema currency, tutor and web-research
configuration, which optional models are present, how much content exists as counts, and
the platform - and nothing a bug report must not carry.

Three rules hold everything here to that line:

- No document text, prompts, or model output. Content is reported as counts only.
- No secrets. The tutor key is reported as present-or-absent and where it is stored, never
  its value, and the endpoint URL is reduced to whether it is local.
- No private paths. Absolute paths are redacted to a known anchor (`<lyra>`, `~`) so a
  username or directory tree never travels with the bundle.

`build_diagnostics` is the whole bundle; `redact_path` is the path rule on its own, pure
and tested in isolation because it is the part a regression would most quietly break.
"""

from __future__ import annotations

import platform
import re
import sqlite3
from pathlib import Path

from backend.config import settings
from backend.llm.locality import is_local_endpoint
from backend.storage import secrets
from backend.storage.database import MIGRATIONS_DIR

# The bundle format. Bumped when a field is added or its meaning changes, so a maintainer
# reading a pasted bundle knows which fields to expect from it.
BUNDLE_VERSION = 1
_STARTUP_LOG_NAME = "desktop-startup.log"
_STARTUP_LOG_MAX_LINES = 20
_STARTUP_LOG_MAX_CHARS = 1_000
_SENSITIVE_STARTUP_LINE = re.compile(
    r"(?i)(authorization|api[_ -]?key|token|secret|password|bearer\s+\S+|"
    r"(?:sk|exa)-[a-z0-9_-]{8,}|/(?:users|home|private)/|/var/folders/|"
    r"content\s*[=:]|body\s*[=:]|prompt\s*[=:]|response\s*[=:]|html\s*[=:]|text\s*[=:]|"
    r"document\s*[=:])"
)


def redact_path(value: str | Path, *, root: Path, home: Path) -> str:
    """An absolute path reduced to a known anchor, so it carries no private tree.

    A path under the checkout reads as `<lyra>/...`, one under the home directory as
    `~/...` with the username gone, and anything else keeps only its final component behind
    `.../` so a directory tree outside both anchors is never disclosed either.
    """
    path = Path(value)
    for anchor, base in (("<lyra>", root), ("~", home)):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        return anchor if str(relative) == "." else f"{anchor}/{relative.as_posix()}"
    return f".../{path.name}"


def _latest_migration_version() -> int:
    """The highest migration number on disk: the schema version a current install carries."""
    versions = [
        int(path.name.partition("_")[0])
        for path in MIGRATIONS_DIR.glob("*.sql")
        if path.name.partition("_")[0].isdigit()
    ]
    if not versions:
        raise RuntimeError("No database migrations are available.")
    return max(versions)


def _count(conn: sqlite3.Connection, table: str) -> int:
    """How many rows a table holds, or -1 when it cannot be read.

    A count, never content: the number of documents is a useful datum and their titles are
    not. -1 rather than a raise so one unreadable table does not sink the whole bundle.
    """
    try:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
    except sqlite3.Error:
        return -1


def _schema_section(conn: sqlite3.Connection) -> dict[str, object]:
    """Where the database schema sits against what this build expects."""
    current = int(conn.execute("pragma user_version").fetchone()[0])
    latest = _latest_migration_version()
    return {"version": current, "latest": latest, "current": current == latest}


def _tutor_section(row: sqlite3.Row) -> dict[str, object]:
    """The tutor endpoint's shape, with the URL reduced to whether it is local.

    The raw endpoint URL is deliberately withheld: it is a private detail, and reducing it
    to local-or-remote is all a bug report needs to reason about where course text would
    travel.
    """
    endpoint = row["endpoint_url"]
    configured = bool(endpoint)
    return {
        "endpoint_configured": configured,
        "endpoint_is_local": is_local_endpoint(str(endpoint)) if configured else None,
        "model": row["model"],
        "context_window": row["context_window"],
        "remote_acknowledged": bool(row["remote_ack"]),
    }


def _web_research_section(row: sqlite3.Row) -> dict[str, object]:
    """Redacted Exa configuration without credentials or provider traffic."""
    return {
        "provider": "exa",
        "source_content_enabled": bool(row["source_content_enabled"]),
        "exa_key_present": secrets.has_exa_api_key(),
        "exa_key_storage": secrets.exa_api_key_storage(),
    }


def _desktop_section(*, root: Path, home: Path) -> dict[str, object]:
    logs_dir = settings.logs_dir
    if logs_dir is None:
        return {"startup_log_present": False, "startup_log_path": None, "startup_log_tail": None}
    path = logs_dir / _STARTUP_LOG_NAME
    return {
        "startup_log_present": path.is_file(),
        "startup_log_path": redact_path(path, root=root, home=home),
        "startup_log_tail": _read_startup_log_tail(path, root=root, home=home),
    }


def _read_startup_log_tail(path: Path, *, root: Path, home: Path) -> str | None:
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return None
    safe_lines = [_sanitize_startup_log_line(line, root=root, home=home) for line in lines]
    tail = "\n".join(safe_lines[-_STARTUP_LOG_MAX_LINES:])
    safe = tail.replace(str(root), "<lyra>").replace(str(home), "~")
    return safe[-_STARTUP_LOG_MAX_CHARS:] or None


def _sanitize_startup_log_line(line: str, *, root: Path, home: Path) -> str:
    safe = line.replace(str(root), "<lyra>").replace(str(home), "~")
    if _SENSITIVE_STARTUP_LINE.search(safe):
        return "<redacted sensitive startup diagnostics>"
    return safe


def build_diagnostics(conn: sqlite3.Connection, *, home: Path | None = None) -> dict[str, object]:
    """Assemble the redacted diagnostics bundle from an open database connection.

    Offline by construction: it reads the database and the local filesystem but makes no
    network call, so it is deterministic and cannot itself fail on a slow endpoint. Live
    upstream readiness is what `/api/health/ready` is for; this is configuration and state.
    """
    from backend.core.app_settings import get_settings_row

    home_dir = home if home is not None else Path.home()
    root = Path(__file__).resolve().parent.parent.parent
    row = get_settings_row(conn)

    return {
        "bundle_version": BUNDLE_VERSION,
        "platform": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "schema": _schema_section(conn),
        "tutor": _tutor_section(row),
        "web_research": _web_research_section(row),
        "embedding": {"model": row["embedding_model"], "dim": row["embedding_dim"]},
        "optional_models": {
            "rerank_installed": settings.rerank_installed,
            "ocr_installed": settings.ocr_installed,
        },
        "desktop": _desktop_section(root=root, home=home_dir),
        "api_key": {"present": secrets.has_api_key(), "storage": secrets.api_key_storage()},
        "content": {
            "classes": _count(conn, "classes"),
            "documents": _count(conn, "documents"),
            "artifacts": _count(conn, "artifacts"),
        },
        "paths": {
            "data_dir": redact_path(settings.data_dir, root=root, home=home_dir),
            "database": redact_path(str(settings.db_path), root=root, home=home_dir),
        },
    }
