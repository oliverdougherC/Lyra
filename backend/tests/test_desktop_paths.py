"""Packaged desktop path defaults and discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import desktop_paths
from backend.config import Settings


def test_packaged_defaults_use_platform_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_paths, "platform_application_support_dir", lambda: Path("/app"))
    monkeypatch.setattr(desktop_paths, "platform_cache_dir", lambda: Path("/cache"))
    monkeypatch.setattr(desktop_paths, "platform_logs_dir", lambda: Path("/logs"))
    monkeypatch.setattr(desktop_paths, "default_resource_root", lambda: Path("/resources"))

    settings = Settings(packaged_mode=True)

    assert settings.data_dir == Path("/app")
    assert settings.cache_dir == Path("/cache")
    assert settings.logs_dir == Path("/logs")
    assert settings.pages_dir == Path("/cache/pages")
    assert settings.models_dir == Path("/app/models")
    assert settings.resource_root == Path("/resources")


def test_dev_defaults_preserve_checkout_relative_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_paths, "default_resource_root", lambda: Path("/repo"))

    settings = Settings(packaged_mode=False)

    assert settings.data_dir == Path("data")
    # None keeps the development cache following a test/dev data_dir override instead
    # of freezing the original relative path at Settings construction time.
    assert settings.cache_dir is None
    assert settings.logs_dir == Path("logs")
    assert settings.pages_dir == Path("data/pages")
    assert settings.models_dir == Path("data/models")
    assert settings.resource_root == Path("/repo")


def test_explicit_models_override_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_paths, "default_resource_root", lambda: Path("/repo"))

    settings = Settings(packaged_mode=True, models_dir_override=Path("/models"))

    assert settings.models_dir == Path("/models")
