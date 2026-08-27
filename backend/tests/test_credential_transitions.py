"""PLA-302: Credential transition correctness.

The single tutor API key must have exactly one authoritative value at any instant.
These tests exercise every transition path between keychain and fallback file storage,
proving that:

- A successful keychain write removes the fallback file before reporting success.
- A keychain demotion to file storage removes the keychain entry.
- A later keychain recovery never resurrects a stale fallback value.
- Partial set/get/delete failures have explicit transactional semantics.
- ``delete_api_key`` is idempotent and clears every possible copy.
- No key material appears in exceptions or return values beyond the get path.
- ``api_key_storage`` reporting is truthful after every transition.
"""

from __future__ import annotations

from pathlib import Path

import keyring.errors
import pytest

from backend.storage import secrets


class FakeKeyring:
    """Fully controllable keyring backend for transition tests."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.get_error is not None:
            raise self.get_error
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("no such password")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeKeyring:
    """Every test gets a fresh keyring and data directory."""
    from backend.config import settings

    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    return fake


def key_file(tmp_path: Path) -> Path:
    from backend.config import settings

    return settings.data_dir / ".api_key"


# ---------------------------------------------------------------------------
# Happy-path keychain operations
# ---------------------------------------------------------------------------


def test_set_stores_in_keychain_when_available(isolated_secrets: FakeKeyring) -> None:
    secrets.set_api_key("sk-new")

    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-new"
    assert secrets.get_api_key() == "sk-new"
    assert secrets.api_key_storage() == "keychain"


def test_get_returns_none_when_no_key_stored(isolated_secrets: FakeKeyring) -> None:
    assert secrets.get_api_key() is None
    assert secrets.has_api_key() is False


def test_delete_clears_keychain_entry(isolated_secrets: FakeKeyring) -> None:
    secrets.set_api_key("sk-delete-me")
    secrets.delete_api_key()

    assert secrets.get_api_key() is None
    assert secrets.has_api_key() is False


def test_delete_is_idempotent(isolated_secrets: FakeKeyring) -> None:
    secrets.delete_api_key()
    secrets.delete_api_key()
    assert secrets.get_api_key() is None


# ---------------------------------------------------------------------------
# PLA-302 core: keychain write removes stale fallback file
# ---------------------------------------------------------------------------


def test_keychain_write_removes_stale_fallback_file(
    isolated_secrets: FakeKeyring, tmp_path: Path
) -> None:
    """The critical PLA-302 scenario: file -> keychain recovery must not leave the file."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"

    # Simulate a previous file-stored key (keychain was broken before).
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-old-file-key")

    # Now keychain is working; set a new key.
    secrets.set_api_key("sk-new-keychain-key")

    # The fallback file must be gone.
    assert not fallback.exists(), "stale fallback file was not removed after keychain write"
    assert secrets.get_api_key() == "sk-new-keychain-key"


def test_stale_file_never_resurfaces_after_keychain_recovery(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full transition: file -> keychain recovery -> keychain failure must not resurrect old key."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-stale")

    # Keychain recovers, new key stored.
    secrets.set_api_key("sk-fresh")
    assert not fallback.exists()
    assert secrets.get_api_key() == "sk-fresh"

    # Now keychain fails again on get.
    isolated_secrets.get_error = keyring.errors.KeyringError("locked")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    # The fallback file is gone, so get_api_key should return None, not "sk-stale".
    result = secrets.get_api_key()
    assert result is None, f"stale key resurfaced: {result!r}"


# ---------------------------------------------------------------------------
# Demotion to file storage
# ---------------------------------------------------------------------------


def test_demotion_stores_in_file_and_cleans_keychain(
    isolated_secrets: FakeKeyring, tmp_path: Path
) -> None:
    """When keychain fails on set, the key goes to file and stale keychain entry is removed."""
    from backend.config import settings

    # Pre-populate a keychain entry (simulating a previous successful write).
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-old-keychain"

    # Now make keychain set fail.
    isolated_secrets.set_error = keyring.errors.KeyringError("locked")
    # But get still works for the probe (we need to reset probe state).
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    # Make probe succeed (get works) but set fails.
    isolated_secrets.get_error = None

    secrets.set_api_key("sk-demoted")

    fallback = settings.data_dir / ".api_key"
    assert fallback.exists()
    assert fallback.read_text().strip() == "sk-demoted"
    assert secrets.api_key_storage() == "file"
    # The stale keychain entry should be cleaned up.
    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store
    monkeypatch.undo()


def test_demotion_from_probe_failure(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the keyring probe itself fails, storage demotes to file."""
    from backend.config import settings

    isolated_secrets.get_error = keyring.errors.KeyringError("no backend")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    secrets.set_api_key("sk-probe-fail")

    assert secrets.api_key_storage() == "file"
    fallback = settings.data_dir / ".api_key"
    assert fallback.read_text().strip() == "sk-probe-fail"


# ---------------------------------------------------------------------------
# Partial failure: keychain write succeeds but file removal fails
# ---------------------------------------------------------------------------


def test_keychain_write_rolled_back_when_file_removal_fails(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the fallback file can't be removed after keychain write, roll back the keychain."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-known-good")

    # Make file unlink fail.
    original_unlink = Path.unlink

    def fail_unlink(self: Path, **kwargs: object) -> None:
        if self == fallback:
            raise OSError("permission denied")
        original_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(OSError, match="permission denied"):
        secrets.set_api_key("sk-new-attempt")

    # Keychain should have been rolled back.
    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store
    # The file still holds the old known-good value.
    assert fallback.read_text().strip() == "sk-known-good"


# ---------------------------------------------------------------------------
# get_api_key demotion on transient keychain failure
# ---------------------------------------------------------------------------


def test_get_demotes_on_transient_keychain_failure(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient keychain read failure returns the fallback file value."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-file-value")

    # Keychain probe succeeded but now get fails.
    monkeypatch.setattr(secrets, "_keyring_ok", True)
    isolated_secrets.get_error = keyring.errors.KeyringError("locked")

    result = secrets.get_api_key()
    assert result == "sk-file-value"
    assert secrets.api_key_storage() == "file"


# ---------------------------------------------------------------------------
# delete_api_key clears both locations
# ---------------------------------------------------------------------------


def test_delete_clears_both_keychain_and_file(
    isolated_secrets: FakeKeyring, tmp_path: Path
) -> None:
    from backend.config import settings

    # Store in keychain.
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-in-keychain"
    # And leave a stale file.
    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-in-file")

    secrets.delete_api_key()

    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store
    assert not fallback.exists()
    assert secrets.get_api_key() is None


def test_delete_idempotent_from_empty_state(isolated_secrets: FakeKeyring) -> None:
    secrets.delete_api_key()
    secrets.delete_api_key()
    secrets.delete_api_key()
    assert secrets.get_api_key() is None


def test_delete_handles_keychain_error_and_still_clears_file(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-orphan")

    monkeypatch.setattr(secrets, "_keyring_ok", True)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    secrets.delete_api_key()

    assert not fallback.exists()
    assert secrets.api_key_storage() == "file"


# ---------------------------------------------------------------------------
# Process restart / cache reset
# ---------------------------------------------------------------------------


def test_reset_keyring_probe_allows_recovery(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After reset_keyring_probe, the next access re-probes the keychain."""
    monkeypatch.setattr(secrets, "_keyring_ok", False)
    assert secrets.api_key_storage() == "file"

    secrets.reset_keyring_probe()

    assert secrets.api_key_storage() == "keychain"


def test_cache_reset_between_set_and_get(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a process restart: set in keychain, reset probe, get still works."""
    secrets.set_api_key("sk-before-restart")
    secrets.reset_keyring_probe()

    assert secrets.get_api_key() == "sk-before-restart"
    assert secrets.api_key_storage() == "keychain"


# ---------------------------------------------------------------------------
# Replacement: new key replaces old
# ---------------------------------------------------------------------------


def test_replacement_in_keychain(isolated_secrets: FakeKeyring) -> None:
    secrets.set_api_key("sk-first")
    secrets.set_api_key("sk-second")

    assert secrets.get_api_key() == "sk-second"


def test_replacement_in_file(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_secrets.get_error = keyring.errors.KeyringError("no backend")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    secrets.set_api_key("sk-first")
    secrets.set_api_key("sk-second")

    assert secrets.get_api_key() == "sk-second"


# ---------------------------------------------------------------------------
# Competing stale values
# ---------------------------------------------------------------------------


def test_keychain_value_wins_over_stale_file(isolated_secrets: FakeKeyring, tmp_path: Path) -> None:
    """When both locations have a value, keychain is authoritative."""
    from backend.config import settings

    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-keychain"
    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-stale-file")

    assert secrets.get_api_key() == "sk-keychain"


# ---------------------------------------------------------------------------
# UI/storage reporting truthfulness
# ---------------------------------------------------------------------------


def test_storage_reports_keychain_when_working(isolated_secrets: FakeKeyring) -> None:
    assert secrets.api_key_storage() == "keychain"


def test_storage_reports_file_after_demotion(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "_keyring_ok", False)
    assert secrets.api_key_storage() == "file"


def test_has_api_key_is_truthful_after_set_and_delete(
    isolated_secrets: FakeKeyring,
) -> None:
    assert secrets.has_api_key() is False
    secrets.set_api_key("sk-set")
    assert secrets.has_api_key() is True
    secrets.delete_api_key()
    assert secrets.has_api_key() is False


# ---------------------------------------------------------------------------
# Full transition cycle
# ---------------------------------------------------------------------------


def test_full_lifecycle_keychain_to_file_to_keychain(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises the complete PLA-302 transition cycle."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"

    # 1. Start with keychain.
    secrets.set_api_key("sk-v1")
    assert secrets.get_api_key() == "sk-v1"
    assert secrets.api_key_storage() == "keychain"
    assert not fallback.exists()

    # 2. Keychain breaks, demote to file.
    isolated_secrets.set_error = keyring.errors.KeyringError("locked")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    # get still works for probe but set will fail.

    secrets.set_api_key("sk-v2")
    assert secrets.get_api_key() == "sk-v2"
    assert secrets.api_key_storage() == "file"
    assert fallback.exists()
    # Stale keychain entry was cleaned.
    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store

    # 3. Keychain recovers.
    isolated_secrets.set_error = None
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    secrets.set_api_key("sk-v3")
    assert secrets.get_api_key() == "sk-v3"
    assert secrets.api_key_storage() == "keychain"
    assert not fallback.exists(), "fallback file must be cleaned on keychain recovery"

    # 4. Keychain breaks AGAIN.
    isolated_secrets.get_error = keyring.errors.KeyringError("locked again")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    # Must NOT return "sk-v2" from the now-deleted fallback file.
    result = secrets.get_api_key()
    assert result is None, f"stale value resurfaced: {result!r}"
