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


def test_direct_acceptance_harness_uses_private_fallback_in_child(tmp_path):
    import json
    import subprocess
    import sys

    code = """
import json, keyring, keyring.backend
class ForbiddenBackend(keyring.backend.KeyringBackend):
    priority = 1
    def get_password(self, *args): raise AssertionError('Host credential read attempted')
    def set_password(self, *args): raise AssertionError('Host credential write attempted')
    def delete_password(self, *args): raise AssertionError('Host credential deletion attempted')
keyring.set_keyring(ForbiddenBackend())
import acceptance.backend_harness
from backend.storage import secrets
from backend.config import settings
settings.ensure_directories()
identity = secrets.stage_tutor_credential('https://synthetic.example/v1', 'synthetic-only')
assert secrets.tutor_credential_storage(identity) == 'file'
assert secrets.get_tutor_credential(identity, 'https://synthetic.example/v1') == 'synthetic-only'
secrets.forget_tutor_credentials()
assert secrets.get_tutor_credential(identity, 'https://synthetic.example/v1') is None
print(json.dumps({'isolated_credential_roundtrip': True}))
"""
    environment = dict(os.environ, LYRA_DATA_DIR=str(tmp_path / "child-profile"))
    result = subprocess.run(  # noqa: S603 - fixed synthetic child; host backend is forbidden.
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout)["isolated_credential_roundtrip"] is True
