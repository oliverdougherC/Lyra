"""Regression tests for the active-reference absence scan."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_active_references.py"
_SPEC = importlib.util.spec_from_file_location("check_active_references", _MODULE_PATH)
scan = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
sys.modules[_SPEC.name] = scan
_SPEC.loader.exec_module(scan)


def test_scan_text_reports_retired_runtime_references(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_text("Lyra still ships Next.js through Firecrawl.\n", encoding="utf-8")

    findings = scan.scan_text(path, path.read_text(encoding="utf-8"))

    assert any("Next.js" in finding for finding in findings)
    assert any("Firecrawl" in finding for finding in findings)


def test_scan_paths_passes_for_clean_active_text(tmp_path: Path) -> None:
    first = tmp_path / "README.md"
    second = tmp_path / "workflow.yml"
    first.write_text("Lyra uses Vite, Exa, and packaged Python.\n", encoding="utf-8")
    second.write_text("CI runs acceptance and resource reports.\n", encoding="utf-8")

    assert scan.scan_paths([first, second]) == []


def test_main_uses_default_active_paths(monkeypatch) -> None:
    monkeypatch.setattr(scan, "ACTIVE_FILES", (Path("README.md"),))
    monkeypatch.setattr(
        scan, "scan_paths", lambda paths: [] if paths == [Path("README.md")] else ["bad"]
    )

    assert scan.main([]) == 0
