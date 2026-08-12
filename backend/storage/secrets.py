"""Storage for the single secret Lyra holds: the tutor endpoint API key.

The key lives in the OS keychain under service `lyra`, username `tutor-endpoint`. When
the machine has no working keyring backend, it falls back to `data/.api_key`, created
with mode `0o600`, and `api_key_storage()` reports `"file"` so the interface can say
plainly that the key is stored unencrypted.

The key is never returned by any endpoint and never written to a log line.
"""

import os
from pathlib import Path
from types import ModuleType
from typing import Literal

import keyring
import keyring.errors

from backend.config import settings
from backend.storage import private

SERVICE = "lyra"
USERNAME = "tutor-endpoint"

# None until the backend has been probed once. Read it through `api_key_storage()`.
_keyring_ok: bool | None = None


def _keyring() -> ModuleType:
    """The one point of keyring access, so tests can substitute a fake backend."""
    return keyring


def _key_file() -> Path:
    """Fallback location, resolved per call so a relocated `data_dir` is honoured."""
    return settings.data_dir / ".api_key"


def _keyring_usable() -> bool:
    """Probe the keyring once and cache whether this machine has a working backend."""
    global _keyring_ok
    if _keyring_ok is None:
        try:
            _keyring().get_password(SERVICE, USERNAME)
        except keyring.errors.KeyringError:
            _keyring_ok = False
        else:
            _keyring_ok = True
    return _keyring_ok


def _demote_to_file() -> None:
    """Record that the keyring failed after the probe, so later calls skip it."""
    global _keyring_ok
    _keyring_ok = False


def _read_key_file() -> str | None:
    """Read the fallback file, treating an absent or empty file as no key."""
    try:
        value = _key_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def set_api_key(value: str) -> None:
    """Store the tutor API key, replacing any existing one."""
    if _keyring_usable():
        try:
            _keyring().set_password(SERVICE, USERNAME, value)
        except keyring.errors.KeyringError:
            _demote_to_file()
        else:
            return

    path = _key_file()
    # `0o700`, so the key file sits in an owner-only directory even if the data tree was
    # somehow created before the permissions contract existed.
    private.secure_mkdir(path.parent)
    # The mode belongs on the `open` call: setting it afterwards leaves a window in
    # which the key is world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def get_api_key() -> str | None:
    """The stored tutor API key, or None when none is set."""
    if _keyring_usable():
        try:
            return _keyring().get_password(SERVICE, USERNAME) or None
        except keyring.errors.KeyringError:
            _demote_to_file()
    return _read_key_file()


def has_api_key() -> bool:
    """Whether a key is stored. This is all any response is allowed to reveal."""
    return get_api_key() is not None


def delete_api_key() -> None:
    """Forget the stored key. Idempotent: a missing key or file is not an error."""
    if _keyring_usable():
        try:
            _keyring().delete_password(SERVICE, USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError:
            _demote_to_file()
    # Always clear the fallback too, so a file left over from a keyring-less run can
    # never resurface as the active key.
    _key_file().unlink(missing_ok=True)


def api_key_storage() -> Literal["keychain", "file"]:
    """Where a key would be stored right now, for the interface to state honestly."""
    return "keychain" if _keyring_usable() else "file"
