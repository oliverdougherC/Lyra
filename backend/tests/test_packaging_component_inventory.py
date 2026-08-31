"""The packaged backend inventory names the runtime-critical frozen components."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "packaging" / "component_inventory.py"
_SPEC = importlib.util.spec_from_file_location("component_inventory", _MODULE_PATH)
inventory = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(inventory)


def test_inventory_includes_runtime_critical_packages() -> None:
    payload = inventory.build_inventory()

    assert payload["entry_module"] == "backend.desktop_entry"
    assert "backend/storage/migrations/*.sql" in payload["data_globs"]
    assert "sqlite_vec" in payload["dynamic_lib_packages"]
    assert "pymupdf" in payload["dynamic_lib_packages"]
    assert "keyring.backends" in payload["hiddenimport_packages"]
    assert "sympy" in payload["hiddenimport_packages"]


def test_main_writes_deterministic_json(tmp_path: Path) -> None:
    output = tmp_path / "inventory.json"

    assert inventory.main(["--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["spec_path"] == "packaging/lyra_backend.spec"
