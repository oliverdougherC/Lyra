"""Independent storage for Lyra's tutor and Exa API credentials.

Each value has its own OS-keychain username and replace/delete lifecycle. When the
machine has no working keyring backend, the values fall back to separate owner-only files
inside the data directory; the settings response reports that fallback honestly.

**Authority invariant:** successful fallback writes/deletions during a Keychain outage
publish a durable authority marker, so a recovered older Keychain value never wins.
Reachable-keychain transitions retain the PLA-302 rollback behavior. Every Keychain
operation shares one bounded worker; a pending operation cannot launch a second worker.

Settings use immutable credential slots selected by a SQLite reference committed with
the endpoint and model. Failed settings commits leave the old reference untouched.
Historical slots remain resolvable by retained snapshots until explicit Forget revokes
all generations and removes fallback values. An inaccessible old Keychain entry can
remain encrypted in the OS until cleanup succeeds, but cannot become authoritative.

The key is never returned by any endpoint and never written to a log line.
"""

import asyncio
import os
import threading
from contextlib import suppress
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Literal

import keyring
import keyring.errors

from backend.config import settings
from backend.storage import private

SERVICE = "lyra"
USERNAME = "tutor-endpoint"
EXA_SERVICE = "lyra"
EXA_USERNAME = "exa-web-research"
EXA_KEY_FILENAME = ".exa_api_key"

_keyring_ok: bool | None = None


_credential_lock = threading.RLock()


def _serialized(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            with _credential_lock:
                return operation(*args, **kwargs)
        if not _credential_lock.acquire(blocking=False):
            from backend.core.errors import ConfigurationError

            raise ConfigurationError("Credentials are being saved. Retry shortly.")
        try:
            return operation(*args, **kwargs)
        finally:
            _credential_lock.release()

    return guarded


def _keyring() -> ModuleType:
    """The one point of keyring access, so tests can substitute a fake backend."""
    return keyring


def _key_file() -> Path:
    """Fallback location, resolved per call so a relocated `data_dir` is honoured."""
    return settings.data_dir / ".api_key"


def _exa_key_file() -> Path:
    """Fallback location for the Exa key, resolved per call."""
    return settings.data_dir / EXA_KEY_FILENAME


# A probe must never hold its caller: settings reads are on a hot path, and some
# keyring backends block instead of raising (a keychain entry whose access control
# the current process cannot satisfy). The probe therefore runs with a deadline; a
# hang demotes to file storage the same way a KeyringError does.
_PROBE_TIMEOUT_SECONDS = 5.0


_operation_lock = threading.Lock()
_operation_thread: threading.Thread | None = None
_operation_backend: object | None = None
_slot_read_cache: dict[tuple[int, str], str | None] = {}


class CredentialTimeout(keyring.errors.KeyringError):
    """A credential operation is still pending; no further Keychain mutation is started."""


def _keyring_call(method: str, *args: str):
    global _operation_thread, _operation_backend
    backend = _keyring()
    with _operation_lock:
        if _operation_backend is backend and _operation_thread and _operation_thread.is_alive():
            raise CredentialTimeout("Keychain is still responding; retry later.")
        outcome = {}
        invoke_method = getattr(backend, method)

        def invoke():
            try:
                outcome["value"] = invoke_method(*args)
                if len(args) >= 2 and args[1].startswith("tutor:"):
                    if method == "get_password":
                        _slot_read_cache[(id(backend), args[1])] = outcome["value"]
                    elif method == "set_password":
                        _slot_read_cache[(id(backend), args[1])] = args[2]
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=invoke, name="lyra-keyring-operation", daemon=True)
        _operation_backend, _operation_thread = backend, thread
        thread.start()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        thread.join(_PROBE_TIMEOUT_SECONDS)
    else:
        # Async callers never wait for a Keychain prompt. Immutable slot reads are
        # cached when this one worker finishes, so retry can use the result.
        thread.join(0)
    if thread.is_alive():
        raise CredentialTimeout("Keychain did not respond in time; retry later.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _keyring_usable() -> bool:
    global _keyring_ok
    if _keyring_ok is None:
        try:
            _keyring_call("get_password", SERVICE, USERNAME)
        except keyring.errors.KeyringError:
            _keyring_ok = False
        else:
            _keyring_ok = True
    return _keyring_ok


def _publish_durable(path: Path, value: str) -> None:
    private.publish_private_text(path, value)
    for target in (path, path.parent):
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _authority_path(path: Path) -> Path:
    return path.with_name(path.name + ".authority")


def _file_is_authoritative(path: Path) -> bool:
    # A marker is a durable decision, not an availability probe. Missing file means
    # deletion, including after a crash between tombstone publication and unlink.
    return _authority_path(path).exists()


def _mark_file_authority(path: Path, *, deleted: bool = False) -> None:
    private.secure_mkdir(path.parent, root=settings.data_dir)
    _publish_durable(_authority_path(path), "deleted" if deleted else "file")


def _deleted(path: Path) -> bool:
    if not _file_is_authoritative(path):
        return False
    return private.read_private_text(_authority_path(path), encoding="utf-8") == "deleted"


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
        _keyring_call("delete_password", SERVICE, USERNAME)


def _remove_exa_keychain_entry() -> None:
    """Remove the Exa keychain entry if it exists, tolerating absence."""
    with suppress(keyring.errors.PasswordDeleteError):
        _keyring_call("delete_password", EXA_SERVICE, EXA_USERNAME)


@_serialized
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
    authoritative credential, and the original ``OSError`` is raised. This prevents a
    state where two different values coexist.

    If the rollback delete itself also fails (the keychain refuses to delete), both the
    new keychain value and the old fallback file survive. Rather than suppress this
    ambiguity, a ``KeyError`` is raised so the caller knows credential authority is
    unresolved and can surface that to the operator.
    """
    keychain_was_reachable = _keyring_usable()
    if keychain_was_reachable:
        try:
            _keyring_call("set_password", SERVICE, USERNAME, value)
        except keyring.errors.KeyringError:
            _demote_to_file()
        else:
            try:
                _remove_key_file()
            except OSError:
                try:
                    _remove_keychain_entry()
                except keyring.errors.KeyringError:
                    raise KeyError(
                        "Credential authority is ambiguous: the new keychain value and "
                        "the old fallback file both survive because neither the file "
                        "unlink nor the keychain rollback delete succeeded. Inspect and "
                        "remove one location manually before retrying."
                    ) from None
                raise
            _authority_path(_key_file()).unlink(missing_ok=True)
            return

    path = _key_file()
    private.secure_mkdir(path.parent, root=settings.data_dir)
    private.write_private_text(path, value)
    if not keychain_was_reachable:
        _mark_file_authority(path)
    if keychain_was_reachable:
        try:
            _remove_keychain_entry()
        except keyring.errors.KeyringError:
            _remove_key_file()
            raise KeyError(
                "Cannot guarantee single-authority credential: keychain "
                "entry could not be removed after file demotion"
            ) from None


@_serialized
def get_api_key() -> str | None:
    """The stored tutor API key, or None when none is set."""
    if _deleted(_key_file()):
        return None
    if not _file_is_authoritative(_key_file()) and _keyring_usable():
        try:
            return _keyring_call("get_password", SERVICE, USERNAME) or None
        except keyring.errors.KeyringError:
            _demote_to_file()
    return _read_key_file()


def has_api_key() -> bool:
    """Whether a key is stored. This is all any response is allowed to reveal."""
    return get_api_key() is not None


@_serialized
def delete_api_key() -> None:
    """Forget the stored key. Idempotent: a missing key or file is not an error.

    Raises ``KeyError`` when the keychain entry cannot be deleted and may survive.
    The file credential is still removed so only the keychain ghost remains.
    """
    if not _keyring_usable():
        _mark_file_authority(_key_file(), deleted=True)
    keychain_entry_survived = False
    if _keyring_usable():
        try:
            _keyring_call("delete_password", SERVICE, USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError:
            keychain_entry_survived = True
            _demote_to_file()
    _remove_key_file()
    if keychain_entry_survived:
        raise KeyError(
            "Keychain entry could not be deleted; file credential removed "
            "but keychain secret may survive until the keychain recovers"
        )


def api_key_storage() -> Literal["keychain", "file"]:
    """Where a key would be stored right now, for the interface to state honestly."""
    return "keychain" if not _file_is_authoritative(_key_file()) and _keyring_usable() else "file"


@_serialized
def set_exa_api_key(value: str) -> None:
    """Store the Exa API key, replacing any existing one."""
    keychain_was_reachable = _keyring_usable()
    if keychain_was_reachable:
        try:
            _keyring_call("set_password", EXA_SERVICE, EXA_USERNAME, value)
        except keyring.errors.KeyringError:
            _demote_to_file()
        else:
            try:
                _exa_key_file().unlink(missing_ok=True)
            except OSError:
                try:
                    _remove_exa_keychain_entry()
                except keyring.errors.KeyringError:
                    raise KeyError("Credential authority is ambiguous for the Exa key.") from None
                raise
            _authority_path(_exa_key_file()).unlink(missing_ok=True)
            return

    path = _exa_key_file()
    private.secure_mkdir(path.parent, root=settings.data_dir)
    private.write_private_text(path, value)
    if not keychain_was_reachable:
        _mark_file_authority(path)
    if keychain_was_reachable:
        try:
            _remove_exa_keychain_entry()
        except keyring.errors.KeyringError:
            _exa_key_file().unlink(missing_ok=True)
            raise KeyError("Cannot guarantee single-authority Exa credential") from None


@_serialized
def get_exa_api_key() -> str | None:
    """The stored Exa API key, or None when none is set."""
    if _deleted(_exa_key_file()):
        return None
    if not _file_is_authoritative(_exa_key_file()) and _keyring_usable():
        try:
            return _keyring_call("get_password", EXA_SERVICE, EXA_USERNAME) or None
        except keyring.errors.KeyringError:
            _demote_to_file()
    try:
        value = private.read_private_text(_exa_key_file(), encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def has_exa_api_key() -> bool:
    """Whether an Exa key is stored."""
    return get_exa_api_key() is not None


@_serialized
def delete_exa_api_key() -> None:
    """Forget the stored Exa key. Idempotent."""
    if not _keyring_usable():
        _mark_file_authority(_exa_key_file(), deleted=True)
    keychain_entry_survived = False
    if _keyring_usable():
        try:
            _keyring_call("delete_password", EXA_SERVICE, EXA_USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError:
            keychain_entry_survived = True
            _demote_to_file()
    _exa_key_file().unlink(missing_ok=True)
    if keychain_entry_survived:
        raise KeyError("Exa keychain entry could not be deleted")


def exa_api_key_storage() -> Literal["keychain", "file"]:
    """Where the Exa key would be stored right now."""
    return (
        "keychain" if not _file_is_authoritative(_exa_key_file()) and _keyring_usable() else "file"
    )


def reset_keyring_probe() -> None:
    """Allow the keyring probe to run again on the next access.

    Called after a process restart boundary (or in tests) so that a recovered keychain
    is detected. This does NOT change which value is stored; it only allows the next
    ``_keyring_usable()`` to re-probe.
    """
    global _keyring_ok
    _keyring_ok = None


# Immutable slots let SQLite commit endpoint + credential identity atomically. A
# failed settings commit leaves the old slot authoritative; staged slots cannot be
# selected by guessing an endpoint, and are never returned by the settings API.
def stage_tutor_credential(endpoint: str | None, value: str | None) -> str:
    import json
    import uuid

    identity = uuid.uuid4().hex
    path = settings.data_dir / "credentials" / f"{identity}.json"
    private.secure_mkdir(path.parent, root=settings.data_dir)
    record = {"endpoint": endpoint, "storage": "none", "generation": _credential_generation()}
    if value:
        try:
            _keyring_call("set_password", SERVICE, "tutor:" + identity, value)
        except keyring.errors.KeyringError:
            record.update(storage="file", value=value)
        else:
            record["storage"] = "keychain"
    _publish_durable(path, json.dumps(record))
    return identity


def _credential_record(identity: str) -> dict:
    import json
    import re

    if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
        raise ValueError("The saved credential identity is invalid.")
    path = settings.data_dir / "credentials" / f"{identity}.json"
    return json.loads(private.read_private_text(path, encoding="utf-8"))


def get_tutor_credential(identity: str, endpoint: str | None) -> str | None:
    record = _credential_record(identity)
    if record.get("generation", "") != _credential_generation():
        return None
    if record["endpoint"] != endpoint:
        return None
    if record["storage"] == "file":
        return record["value"]
    if record["storage"] in ("none", "revoked"):
        return None
    username = "tutor:" + identity
    cache_key = (id(_keyring()), username)
    if cache_key in _slot_read_cache:
        return _slot_read_cache[cache_key]
    try:
        return _keyring_call("get_password", SERVICE, username)
    except keyring.errors.KeyringError as exc:
        from backend.core.errors import ConfigurationError

        raise ConfigurationError(
            "Keychain is still responding. Retry shortly or save the key again in Settings."
        ) from exc


def tutor_credential_storage(identity: str) -> Literal["keychain", "file"]:
    record = _credential_record(identity)
    return "keychain" if record["storage"] in ("keychain", "none") else "file"


def _credential_generation() -> str:
    path = settings.data_dir / ".tutor_credential_generation"
    try:
        return private.read_private_text(path, encoding="utf-8")
    except FileNotFoundError:
        return ""


def forget_tutor_credentials() -> None:
    """Revoke historical references before best-effort physical Keychain cleanup.

    An inaccessible old Keychain entry may remain encrypted in the OS store, but no
    retained settings row can resolve it after the durable revocation decision.
    """
    import json
    import uuid

    _publish_durable(settings.data_dir / ".tutor_credential_generation", uuid.uuid4().hex)
    _mark_file_authority(_key_file(), deleted=True)
    _remove_key_file()
    # The pre-slot legacy username is shared by every profile. This profile does
    # not own it: deleting it would revoke another installed/dev profile's key.
    # The local deletion tombstone above revokes its use here. Only UUID slots
    # recorded in this profile are eligible for physical Keychain cleanup.
    for path in (settings.data_dir / "credentials").glob("*.json"):
        record = _credential_record(path.stem)
        was_keychain = record["storage"] in ("keychain", "revoked")
        record.pop("value", None)
        record["storage"] = "revoked" if was_keychain else "none"
        _publish_durable(path, json.dumps(record))
        _slot_read_cache.pop((id(_keyring()), "tutor:" + path.stem), None)
        if was_keychain:
            with suppress(keyring.errors.KeyringError):
                _keyring_call("delete_password", SERVICE, "tutor:" + path.stem)
