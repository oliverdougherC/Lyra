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


def bundled_runtime_candidates(resource_root: Path) -> tuple[Path, ...]:
    """Where the application bundle stages the pinned llama runtime.

    The Tauri build copies `resources/llama` next to `resources/lyra-backend`, so the
    runtime's staging directory sits beside the frozen backend's onedir. The backend's
    own `resource_root` is that onedir in the old PyInstaller layout, or its `_internal`
    subdirectory in the current one, so both ancestors are candidates; the ones that
    actually exist in the bundle are the ones searched. On macOS the result is
    `Lyra.app/Contents/Resources/.../llama`; the same shape holds on Windows and Linux.

    The runtime stays inside the app bundle: it is signed and notarized with the rest of
    the application, and a clean install therefore has it on disk with no download step.
    """
    candidates = (
        resource_root.parent / "llama",
        resource_root.parent.parent / "llama",
    )
    return tuple(candidate for candidate in candidates if candidate.is_dir())


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
    return _dedupe_data_candidates(root / "data" for root in roots)


def selected_source_data_candidates(selected_root: Path) -> tuple[Path, ...]:
    """Likely Lyra data roots for a user-picked folder.

    A migration prompt may point at the checkout root (`Lyra/`) or directly at the data
    directory (`Lyra/data/`). Both are legitimate user choices; the import flow checks the
    direct pick first so a real data directory never gets shadowed by its `data/data`
    child.
    """

    return _dedupe_data_candidates((selected_root, selected_root / "data"))


def _dedupe_data_candidates(candidates: object) -> tuple[Path, ...]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        if not isinstance(candidate, Path):
            candidate = Path(candidate)
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return tuple(ordered)
