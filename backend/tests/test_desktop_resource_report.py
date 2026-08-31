"""Tests for the packaged desktop resource inventory helper."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "desktop_resource_report.py"
_SPEC = importlib.util.spec_from_file_location("desktop_resource_report", _MODULE_PATH)
report = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(report)


def test_build_report_uses_relative_paths_and_redacts_absolute_symlink_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Lyra.app"
    (root / "Contents" / "MacOS").mkdir(parents=True)
    binary = root / "Contents" / "MacOS" / "Lyra"
    binary.write_bytes(b"binary")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    absolute_target = tmp_path / "outside.dylib"
    absolute_target.write_bytes(b"lib")
    (root / "Contents" / "Frameworks").mkdir()
    (root / "Contents" / "Frameworks" / "liblyra.dylib").symlink_to(absolute_target)

    payload = report.build_report([("app", root)])
    root_report = payload["roots"][0]
    entries = root_report["entries"]

    assert payload["privacy"]["absolute_paths_emitted"] is False
    assert all(str(tmp_path) not in json.dumps(entry, sort_keys=True) for entry in entries)
    assert entries[0]["path"] == "Contents/Frameworks/liblyra.dylib"
    assert entries[0]["target"] == "<absolute-target-redacted>"
    assert entries[0]["target_redacted"] is True
    assert entries[1]["path"] == "Contents/MacOS/Lyra"
    assert entries[1]["category"] == "executable"


def test_collect_root_classifies_migrations_frontend_assets_and_python_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    (root / "backend" / "storage" / "migrations").mkdir(parents=True)
    (root / "frontend" / "assets").mkdir(parents=True)
    (root / "python").mkdir()
    (root / "backend" / "storage" / "migrations" / "001_init.sql").write_text("select 1;")
    (root / "frontend" / "assets" / "app.js").write_text("console.log('lyra')")
    (root / "python" / "python312.zip").write_bytes(b"zip")

    payload = report.build_report([("bundle", root)])
    categories = payload["roots"][0]["categories"]

    assert categories["sqlite-migration"] == 1
    assert categories["frontend-asset"] == 1
    assert categories["python-runtime"] == 1


def test_main_writes_sorted_deterministic_json(tmp_path: Path) -> None:
    root = tmp_path / "Lyra.app"
    root.mkdir()
    out = tmp_path / "report.json"

    exit_code = report.main(["--root", f"app={root}", "--output", str(out)])

    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["tool"] == "desktop_resource_report"
    assert payload["roots"][0]["root_name"] == "Lyra.app"
