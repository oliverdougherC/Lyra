"""Tests for the packaged Python smoke helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "packaged_python_smoke.py"
_SPEC = importlib.util.spec_from_file_location("packaged_python_smoke", _MODULE_PATH)
smoke = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
sys.modules[_SPEC.name] = smoke
_SPEC.loader.exec_module(smoke)


def test_run_smoke_reports_imports_and_resources() -> None:
    payload = smoke.run_smoke()

    assert "backend.main" in payload["modules"]
    assert payload["migration_count"] >= 1
    assert str(payload["latest_migration"]).endswith(".sql")
    assert payload["prompts_module"] == "prompts.py"
