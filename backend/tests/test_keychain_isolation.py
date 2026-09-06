"""The default test boundary must protect the host even without a module-local fake."""

import os

import keyring

from backend.storage import secrets


def test_default_suite_storage_stays_in_fake_keychain(isolated_keychain):
    keyring.set_password("lyra", "synthetic-isolation", "synthetic-value")
    assert isolated_keychain[("lyra", "synthetic-isolation")] == "synthetic-value"
    assert keyring.get_password("lyra", "synthetic-isolation") == "synthetic-value"
    keyring.delete_password("lyra", "synthetic-isolation")
    assert ("lyra", "synthetic-isolation") not in isolated_keychain
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def test_new_forget_path_cannot_bypass_default_test_isolation(isolated_keychain):
    identity = secrets.stage_tutor_credential("https://synthetic.example/v1", "synthetic-value")
    assert isolated_keychain[(secrets.SERVICE, "tutor:" + identity)] == "synthetic-value"
    secrets.forget_tutor_credentials()
    assert (secrets.SERVICE, "tutor:" + identity) not in isolated_keychain


def test_direct_acceptance_harness_uses_private_fallback_in_child(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys
    from pathlib import Path

    code = """
import json, os, keyring, keyring.backend
from pathlib import Path
class ForbiddenBackend(keyring.backend.KeyringBackend):
    priority = 1
    def get_password(self, *args): raise AssertionError('Host credential read attempted')
    def set_password(self, *args): raise AssertionError('Host credential write attempted')
    def delete_password(self, *args): raise AssertionError('Host credential deletion attempted')
keyring.set_keyring(ForbiddenBackend())
import acceptance.backend_harness
from backend.storage import secrets
from backend.config import settings
root = Path(os.environ["LYRA_DATA_DIR"])
assert not settings.packaged_mode
assert all(path.is_relative_to(root) for path in (
    settings.db_path, settings.cache_dir, settings.logs_dir,
    settings.models_dir, settings.resource_root,
))
settings.ensure_directories()
identity = secrets.stage_tutor_credential('https://synthetic.example/v1', 'synthetic-only')
assert secrets.tutor_credential_storage(identity) == 'file'
assert secrets.get_tutor_credential(identity, 'https://synthetic.example/v1') == 'synthetic-only'
secrets.forget_tutor_credentials()
assert secrets.get_tutor_credential(identity, 'https://synthetic.example/v1') is None
print(json.dumps({'isolated_credential_roundtrip': True}))
"""
    # Earlier packaged-runtime tests (or the invoking shell) may leave selectors
    # behind. Deliberately seed that contamination so isolation is always exercised.
    outside = tmp_path / "must-not-touch"
    monkeypatch.setenv("LYRA_PACKAGED", "1")
    for name in ("DB_PATH", "CACHE_DIR", "LOGS_DIR", "MODELS_DIR", "RESOURCE_ROOT"):
        monkeypatch.setenv("LYRA_" + name, str(outside / name.lower()))
    monkeypatch.setenv("LYRA_SOURCE_DATA_DIR", str(outside))
    monkeypatch.setenv("ACCEPTANCE_HELPER_PORT", "invalid-inherited-port")
    root = tmp_path / "child-profile"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("LYRA_", "ACCEPTANCE_"))
        and name not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update(
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
        LYRA_PACKAGED="0",
        LYRA_DATA_DIR=str(root),
        LYRA_DB_PATH=str(root / "lyra.db"),
        LYRA_CACHE_DIR=str(root / "cache"),
        LYRA_LOGS_DIR=str(root / "logs"),
        LYRA_MODELS_DIR=str(root / "models"),
        LYRA_RESOURCE_ROOT=str(root / "resources"),
    )
    result = subprocess.run(  # noqa: S603 - fixed synthetic child; host backend is forbidden.
        [sys.executable, "-c", code],
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout)["isolated_credential_roundtrip"] is True
    assert not outside.exists()
