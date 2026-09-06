"""Tests for the packaged desktop soak harness skeleton."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "packaged_soak_harness.py"
_SPEC = importlib.util.spec_from_file_location("packaged_soak_harness", _MODULE_PATH)
harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(harness)


def test_prepare_run_creates_disposable_profile_and_versioned_plan(tmp_path: Path) -> None:
    app_root = tmp_path / "Lyra.app"
    app_root.mkdir()

    plan_path = harness.prepare_run(app_root, tmp_path / "runs", "pla-147-smoke")

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    run_root = plan_path.parent
    assert payload["schema_version"] == 1
    assert payload["scenario"] == "pla-147-packaged-desktop"
    assert payload["run_id"] == "pla-147-smoke"
    assert payload["app"]["name"] == "Lyra.app"
    assert (run_root / "profile").is_dir()
    assert (run_root / "artifacts").is_dir()
    assert (run_root / "logs").is_dir()
    assert payload["steps"][0]["id"] == "prepare-disposable-profile"
    assert payload["steps"][0]["executor"] == "harness"
    assert payload["steps"][2]["executor"] == "physical"


def test_prepare_run_refuses_to_overwrite_existing_run_directory(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "existing-run"
    root.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        harness.prepare_run(tmp_path / "Lyra.app", tmp_path / "runs", "existing-run")


def test_launch_environment_is_consumed_by_packaged_settings_outside_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.config import Settings

    monkeypatch.chdir(tmp_path)
    plan_path = harness.prepare_run(Path("Lyra.app"), Path("runs"), "isolated")
    payload = json.loads(plan_path.read_text())
    root = plan_path.parent.resolve()
    # Ambient source-checkout settings must not override the disposable profile.
    monkeypatch.setenv("LYRA_DB_PATH", str(tmp_path / "student.db"))
    for name, value in payload["launch_environment"].items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path.parent)
    settings = Settings(packaged_mode=True)
    assert settings.data_dir == root / "profile"
    assert settings.db_path == root / "profile" / "lyra.db"
    assert settings.cache_dir == root / "cache"
    assert settings.logs_dir == root / "logs"
    assert settings.models_dir == root / "profile" / "models"
    settings.ensure_directories()
    assert settings.uploads_dir.is_dir()
    assert settings.pages_dir.is_dir()


def test_update_step_records_manual_outcome_without_changing_execution_ownership(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "Lyra.app"
    app_root.mkdir()
    plan_path = harness.prepare_run(app_root, tmp_path / "runs", "manual-pass")

    harness.update_step(
        plan_path,
        "launch-packaged-app",
        "completed",
        "Launched successfully with disposable profile.",
        "artifacts/launch.png",
    )

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    launch = next(step for step in payload["steps"] if step["id"] == "launch-packaged-app")
    assert launch["executor"] == "physical"
    assert launch["status"] == "completed"
    assert launch["notes"] == "Launched successfully with disposable profile."
    assert launch["artifacts"] == ["artifacts/launch.png"]


def test_load_plan_rejects_unsupported_schema(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema_version": 999, "steps": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported plan schema"):
        harness.load_plan(plan)
