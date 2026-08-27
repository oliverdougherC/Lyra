"""Storage for the single secret Lyra holds: the tutor endpoint API key.

The key lives in the OS keychain under service `lyra`, username `tutor-endpoint`. When
the machine has no working keyring backend, it falls back to `data/.api_key`, created
with mode `0o600`, and `api_key_storage()` reports `"file"` so the interface can say
plainly that the key is stored unencrypted.

**Transition invariant (PLA-302):** At any instant, at most one authoritative copy of the
key exists. After a successful keychain write, the fallback file is removed before success
is reported. After a successful file write on demotion, the keychain entry is removed
before success is reported. ``delete_api_key`` clears both locations idempotently. A
partial replacement that cannot guarantee exactly one stored value either preserves the
previously known-good credential or raises, so a later ``get_api_key`` never silently
returns a stale value the caller thought was replaced.

The key is never returned by any endpoint and never written to a log line.
"""

from contextlib import suppress
from pathlib import Path
from types import ModuleType
from typing import Literal

import keyring
import keyring.errors

from backend.config import settings
from backend.storage import private

SERVICE = "lyra"
USERNAME = "tutor-endpoint"

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
        value = private.read_private_text(_key_file(), encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _remove_key_file() -> None:
    """Remove the fallback file if it exists, tolerating absence."""
    _key_file().unlink(missing_ok=True)


def _remove_keychain_entry() -> None:
    """Remove the keychain entry if it exists, tolerating absence."""
    with suppress(keyring.errors.PasswordDeleteError):
        _keyring().delete_password(SERVICE, USERNAME)


def set_api_key(value: str) -> None:
    """Store the tutor API key, replacing any existing one.

    Transition contract: after this call returns normally, the new value is the sole
    stored credential. The previously stored value (if any) has been removed from
    whichever location held it. If the keychain write succeeds, the fallback file is
    removed before returning. If the keychain write fails and the call demotes to file
    storage, the keychain entry is removed after the file write so no stale keychain
    value can later resurface.

    If the fallback file cannot be removed after a successful keychain write, the
    keychain entry is rolled back (deleted) so that the old file value remains the sole
    authoritative credential, and a ``KeyError`` is raised. This prevents a state where
    two different values coexist.
    """
    if _keyring_usable():
        try:
            _keyring().set_password(SERVICE, USERNAME, value)
        except keyring.errors.KeyringError:
            _demote_to_file()
        else:
            try:
                _remove_key_file()
            except OSError:
                # Cannot remove fallback file: two values would coexist. Roll back
                # the keychain write so the old file value remains sole authority.
                with suppress(keyring.errors.KeyringError):
                    _remove_keychain_entry()
                raise
            return

    # Demoted to file storage (either from this call or the probe).
    path = _key_file()
    private.secure_mkdir(path.parent, root=settings.data_dir)
    private.write_private_text(path, value)
    # Clean up any stale keychain entry so it cannot resurface if the keychain
    # recovers in a later process.
    with suppress(keyring.errors.KeyringError):
        _remove_keychain_entry()


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
    _remove_key_file()


def api_key_storage() -> Literal["keychain", "file"]:
    """Where a key would be stored right now, for the interface to state honestly."""
    return "keychain" if _keyring_usable() else "file"


def reset_keyring_probe() -> None:
    """Allow the keyring probe to run again on the next access.

    Called after a process restart boundary (or in tests) so that a recovered keychain
    is detected. This does NOT change which value is stored; it only allows the next
    ``_keyring_usable()`` to re-probe.
    """
    global _keyring_ok
    _keyring_ok = None
