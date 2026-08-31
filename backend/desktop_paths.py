"""Platform-specific mutable-path and packaged-resource discovery for desktop builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Lyra"


def platform_application_support_dir(app_name: str = APP_NAME) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / app_name
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return base / app_name
    base = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return base / app_name


def platform_cache_dir(app_name: str = APP_NAME) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / app_name
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / app_name / "Cache"
    base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return base / app_name


def platform_logs_dir(app_name: str = APP_NAME) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Logs" / app_name
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / app_name / "Logs"
    base = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return base / app_name / "logs"


def default_resource_root() -> Path:
    configured = os.environ.get("LYRA_RESOURCE_ROOT")
    if configured:
        return Path(configured).expanduser()
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def source_data_candidates(resource_root: Path) -> tuple[Path, ...]:
    roots: list[Path] = [resource_root]
    parents = list(resource_root.parents[:2])
    roots.extend(parents)
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in roots:
        candidate = root / "data"
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return tuple(candidates)
