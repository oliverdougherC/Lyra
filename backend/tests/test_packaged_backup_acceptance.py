"""Safety regressions for the tracked frozen-backup acceptance driver."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "packaged_backup_acceptance",
    Path(__file__).resolve().parents[2] / "scripts" / "verify_packaged_backup.py",
)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def test_isolated_environment_scrubs_ambient_credentials_and_paths(tmp_path, monkeypatch):
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "LYRA_SOURCE_DATA_DIR",
        "LYRA_RESOURCE_ROOT",
        "OPENAI_API_KEY",
        "EXA_API_KEY",
        "HF_TOKEN",
        "VIRTUAL_ENV",
    ):
        monkeypatch.setenv(name, "must-not-inherit")
    env = harness.isolated_environment(tmp_path)
    harness.assert_child_isolation(tmp_path, env)
    assert "must-not-inherit" not in env.values()
    # Real child resolves the same effective packaged paths and keyring without
    # writing credentials or importing the host-selected credential backend.
    repo = str(Path(__file__).resolve().parents[2])
    code = f"""import sys, json
sys.path.insert(0, {repo!r})
from backend.config import Settings
import keyring
settings = Settings()
print(json.dumps({{"data": str(settings.data_dir), "db": str(settings.db_path),
    "cache": str(settings.cache_dir), "logs": str(settings.logs_dir),
    "models": str(settings.models_dir), "keyring": type(keyring.get_keyring()).__module__}}))
"""
    completed = subprocess.run(  # noqa: S603 - fixed synthetic isolated child
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    observed = json.loads(completed.stdout)
    assert observed == {
        "data": env["LYRA_DATA_DIR"],
        "db": env["LYRA_DB_PATH"],
        "cache": env["LYRA_CACHE_DIR"],
        "logs": env["LYRA_LOGS_DIR"],
        "models": env["LYRA_MODELS_DIR"],
        "keyring": "keyring.backends.null",
    }


@pytest.mark.parametrize(
    "selector",
    [
        "LYRA_DATA_DIR",
        "LYRA_DB_PATH",
        "LYRA_CACHE_DIR",
        "LYRA_LOGS_DIR",
        "LYRA_MODELS_DIR",
        "HF_HOME",
    ],
)
def test_every_child_refuses_escaping_mutable_path(tmp_path, monkeypatch, selector):
    env = harness.isolated_environment(tmp_path)
    env[selector] = str(tmp_path.parent / "student-profile")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Spawned unsafe child"))
    for operation in (
        lambda: harness.start_and_stop(Path("/fake-backend"), tmp_path, env),
        lambda: harness.helper(
            Path("/fake-backend"), "restore", tmp_path / "archive", tmp_path, env, 0
        ),
    ):
        with pytest.raises(ValueError, match="escapes"):
            operation()


def test_recovery_profile_keeps_credential_boundary(tmp_path):
    env = harness.isolated_environment(tmp_path)
    relocated = tmp_path / "relocated"
    env.update(LYRA_DATA_DIR=str(relocated), LYRA_DB_PATH=str(relocated / "lyra.db"))
    harness.assert_child_isolation(tmp_path, env)
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.macOS.Keyring"
    with pytest.raises(ValueError, match="host Keychain"):
        harness.assert_child_isolation(tmp_path, env)


def test_partial_readiness_cannot_escape_deadline():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"status":')
        with os.fdopen(read_fd, "rb") as stream, pytest.raises(RuntimeError, match="deadline"):
            harness.read_readiness(stream, timeout=0.02)
    finally:
        os.close(write_fd)


def test_readiness_is_size_bounded():
    read_fd, write_fd = os.pipe()
    try:
        # Keep this below the pipe capacity so a same-process writer cannot block.
        with os.fdopen(read_fd, "rb") as stream:
            os.write(write_fd, b'{"status":"ready"}\n')
            assert harness.read_readiness(stream, timeout=0.1) == {"status": "ready"}
    finally:
        os.close(write_fd)


def test_profile_refuses_symlink_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink ancestors"):
        harness.profile_context(alias / "new-profile", False)


def test_frozen_smoke_overrides_inherited_database_before_spawn(tmp_path, monkeypatch):
    smoke_spec = importlib.util.spec_from_file_location(
        "frozen_smoke", Path(__file__).resolve().parents[2] / "scripts/frozen_backend_smoke.py"
    )
    assert smoke_spec and smoke_spec.loader
    smoke = importlib.util.module_from_spec(smoke_spec)
    smoke_spec.loader.exec_module(smoke)
    outside = tmp_path / "must-not-touch"
    for name in (
        "LYRA_DB_PATH",
        "LYRA_SOURCE_DATA_DIR",
        "LYRA_RESOURCE_ROOT",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        monkeypatch.setenv(name, str(outside))
    captured = {}

    class SpawnInterceptedError(Exception):
        pass

    def intercept(*args, **kwargs):
        captured.update(kwargs["env"])
        raise SpawnInterceptedError

    monkeypatch.setattr(smoke.subprocess, "Popen", intercept)
    with pytest.raises(SpawnInterceptedError):
        smoke.run_smoke(Path(sys.executable))
    root = Path(captured["LYRA_DATA_DIR"])
    try:
        assert Path(captured["LYRA_DB_PATH"]) == root / "lyra.db"
        assert captured["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
        assert not any(
            name in captured
            for name in ("LYRA_SOURCE_DATA_DIR", "LYRA_RESOURCE_ROOT", "PYTHONPATH", "PYTHONHOME")
        )
        assert not outside.exists()
    finally:
        # Popen intentionally never ran, so remove only the harness-generated empty root.
        root.parent.rmdir()
