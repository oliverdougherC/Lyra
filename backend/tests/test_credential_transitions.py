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


def exa_key_file(tmp_path: Path) -> Path:
    from backend.config import settings

    return settings.data_dir / secrets.EXA_KEY_FILENAME


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


def test_blocking_probe_demotes_within_deadline(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A keyring backend that blocks instead of raising must not hold the caller:
    the probe hits its deadline, storage demotes to file, and the call returns."""
    import time

    from backend.config import settings

    def block_forever(service: str, username: str) -> str | None:
        time.sleep(30)
        return None

    isolated_secrets.get_password = block_forever  # type: ignore[method-assign]
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    monkeypatch.setattr(secrets, "_PROBE_TIMEOUT_SECONDS", 0.25)

    started = time.monotonic()
    assert secrets._keyring_usable() is False
    # Returned at the deadline, not after the backend's own (nonexistent) answer.
    assert time.monotonic() - started < 10

    # And the demoted storage works: the value lands in the file fallback.
    secrets.set_api_key("sk-after-block")
    assert secrets.api_key_storage() == "file"
    assert (settings.data_dir / ".api_key").read_text().strip() == "sk-after-block"


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


def test_delete_raises_when_keychain_entry_survives(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """delete_api_key must raise KeyError when the keychain entry cannot be removed."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-orphan")
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-in-keychain"

    monkeypatch.setattr(secrets, "_keyring_ok", True)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    with pytest.raises(KeyError, match="Keychain entry could not be deleted"):
        secrets.delete_api_key()

    assert not fallback.exists(), "file credential must still be removed"
    assert secrets.api_key_storage() == "file"
    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-in-keychain"


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


# ---------------------------------------------------------------------------
# Finding 3: demotion path must not suppress keychain cleanup failure
# ---------------------------------------------------------------------------


def test_demotion_raises_when_stale_keychain_entry_cannot_be_removed(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If keychain delete fails after file demotion, the file is rolled back and KeyError raised."""
    from backend.config import settings

    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-old-keychain"
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked for deletes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None

    with pytest.raises(KeyError, match="single-authority credential"):
        secrets.set_api_key("sk-new-demoted")

    fallback = settings.data_dir / ".api_key"
    assert not fallback.exists(), "file must be rolled back after failed cleanup"
    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-old-keychain"


def test_demotion_raises_and_get_returns_old_keychain_value_after_recovery(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a failed demotion, recovering the keychain returns the old value."""
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-original"
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked for deletes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None

    with pytest.raises(KeyError):
        secrets.set_api_key("sk-attempted")

    isolated_secrets.set_error = None
    isolated_secrets.delete_error = None
    secrets.reset_keyring_probe()

    assert secrets.get_api_key() == "sk-original"


def test_demotion_skips_cleanup_when_keychain_was_never_reachable(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the probe itself fails, no stale entry exists, so no cleanup is needed."""
    from backend.config import settings

    isolated_secrets.get_error = keyring.errors.KeyringError("no backend")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.delete_error = keyring.errors.KeyringError("would fail if called")

    secrets.set_api_key("sk-file-only")

    fallback = settings.data_dir / ".api_key"
    assert fallback.exists()
    assert secrets.get_api_key() == "sk-file-only"


def test_demotion_with_password_delete_error_succeeds(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PasswordDeleteError (nothing to delete) is not a real failure."""
    from backend.config import settings

    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None

    secrets.set_api_key("sk-demoted-clean")

    fallback = settings.data_dir / ".api_key"
    assert fallback.exists()
    assert secrets.get_api_key() == "sk-demoted-clean"


def test_demotion_with_successful_cleanup_clears_keychain(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Demotion with successful keychain cleanup leaves only file credential."""
    from backend.config import settings

    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-stale"
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None

    secrets.set_api_key("sk-demoted-v2")

    fallback = settings.data_dir / ".api_key"
    assert fallback.exists()
    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store
    assert secrets.get_api_key() == "sk-demoted-v2"


def test_demotion_cleanup_failure_preserves_known_good_keychain_value(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After rollback, get_api_key returns the old keychain value, not stale file."""
    from backend.config import settings

    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-known-good"
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked for deletes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None

    with pytest.raises(KeyError):
        secrets.set_api_key("sk-new-attempt")

    fallback = settings.data_dir / ".api_key"
    assert not fallback.exists()

    isolated_secrets.set_error = None
    isolated_secrets.delete_error = None
    secrets.reset_keyring_probe()
    assert secrets.get_api_key() == "sk-known-good"
    assert secrets.api_key_storage() == "keychain"


# ---------------------------------------------------------------------------
# Finding 4: delete_api_key must raise when keychain entry survives
# ---------------------------------------------------------------------------


def test_delete_idempotent_when_nothing_to_delete_in_keychain(
    isolated_secrets: FakeKeyring,
) -> None:
    """PasswordDeleteError means nothing to delete — that's fine, not a failure."""
    secrets.delete_api_key()
    assert secrets.get_api_key() is None


def test_delete_clears_both_when_keychain_works(
    isolated_secrets: FakeKeyring, tmp_path: Path
) -> None:
    from backend.config import settings

    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-kc"
    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-file")

    secrets.delete_api_key()

    assert (secrets.SERVICE, secrets.USERNAME) not in isolated_secrets.store
    assert not fallback.exists()
    assert secrets.get_api_key() is None


def test_delete_removes_file_even_when_keychain_fails(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The file is always removed; the KeyError only fires after file cleanup."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-file-val")
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-kc-val"

    monkeypatch.setattr(secrets, "_keyring_ok", True)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    with pytest.raises(KeyError):
        secrets.delete_api_key()

    assert not fallback.exists()
    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-kc-val"


def test_delete_keychain_ghost_survives_and_resurfaces(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a failed delete, the keychain ghost resurfaces when the keychain recovers."""
    isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] = "sk-ghost"

    monkeypatch.setattr(secrets, "_keyring_ok", True)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    with pytest.raises(KeyError):
        secrets.delete_api_key()

    isolated_secrets.delete_error = None
    secrets.reset_keyring_probe()
    assert secrets.get_api_key() == "sk-ghost"


def test_delete_succeeds_when_keychain_not_usable(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When keychain was never usable, only file matters, and delete succeeds."""
    from backend.config import settings

    isolated_secrets.get_error = keyring.errors.KeyringError("no backend")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-file-only")

    secrets.delete_api_key()

    assert not fallback.exists()
    assert secrets.get_api_key() is None


# ---------------------------------------------------------------------------
# PLA-302: rollback-of-rollback — both file unlink AND keychain delete fail
# ---------------------------------------------------------------------------


def test_rollback_of_rollback_raises_ambiguous_authority(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When keychain write succeeds, file unlink fails, AND keychain rollback delete
    also fails, the caller must receive an explicit ambiguous-authority error — not
    the bare OSError from the file unlink, and not a silent success.
    """
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-old-file")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, **kwargs: object) -> None:
        if self == fallback:
            raise OSError("permission denied")
        original_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    isolated_secrets.delete_error = keyring.errors.KeyringError("keychain locked for delete")

    with pytest.raises(KeyError, match="ambiguous"):
        secrets.set_api_key("sk-new-attempt")


def test_rollback_of_rollback_leaves_both_values_surviving(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a rollback-of-rollback failure, both the new keychain value and the old
    fallback file coexist.  get_api_key returns the keychain value (keychain is
    authoritative when reachable), and the file still holds the old value.
    """
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-old-file")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, **kwargs: object) -> None:
        if self == fallback:
            raise OSError("permission denied")
        original_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    with pytest.raises(KeyError, match="ambiguous"):
        secrets.set_api_key("sk-new")

    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-new"
    assert fallback.read_text().strip() == "sk-old-file"

    assert secrets.get_api_key() == "sk-new"


def test_rollback_of_rollback_then_reprobe_returns_keychain_value(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a rollback-of-rollback, a process restart (probe reset) still returns
    the keychain value since the keychain is reachable for reads.
    """
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-old-file")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, **kwargs: object) -> None:
        if self == fallback:
            raise OSError("permission denied")
        original_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    with pytest.raises(KeyError, match="ambiguous"):
        secrets.set_api_key("sk-new")

    isolated_secrets.delete_error = None
    secrets.reset_keyring_probe()

    assert secrets.get_api_key() == "sk-new"
    assert secrets.api_key_storage() == "keychain"


def test_rollback_of_rollback_no_false_success_path(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rollback-of-rollback path must never return normally — it must always raise."""
    from backend.config import settings

    fallback = settings.data_dir / ".api_key"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("sk-old")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, **kwargs: object) -> None:
        if self == fallback:
            raise OSError("disk error")
        original_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    isolated_secrets.delete_error = keyring.errors.KeyringError("locked")

    raised = False
    try:
        secrets.set_api_key("sk-attempt")
    except (KeyError, OSError):
        raised = True

    assert raised, "set_api_key must raise when both file unlink and keychain rollback fail"


def test_exa_key_uses_a_separate_keychain_entry(isolated_secrets: FakeKeyring) -> None:
    secrets.set_api_key("sk-tutor")
    secrets.set_exa_api_key("exa-secret")

    assert isolated_secrets.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-tutor"
    assert isolated_secrets.store[(secrets.EXA_SERVICE, secrets.EXA_USERNAME)] == "exa-secret"
    assert secrets.get_api_key() == "sk-tutor"
    assert secrets.get_exa_api_key() == "exa-secret"


def test_exa_key_demotion_uses_its_own_fallback_file(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)

    secrets.set_exa_api_key("exa-file-secret")

    assert exa_key_file(tmp_path).read_text().strip() == "exa-file-secret"
    assert not key_file(tmp_path).exists()
    assert secrets.get_exa_api_key() == "exa-file-secret"
    assert secrets.exa_api_key_storage() == "file"


def test_deleting_exa_key_does_not_delete_the_tutor_key(
    isolated_secrets: FakeKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets.set_api_key("sk-tutor")
    isolated_secrets.set_error = keyring.errors.KeyringError("locked for writes")
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    isolated_secrets.get_error = None
    secrets.set_exa_api_key("exa-file-secret")

    secrets.delete_exa_api_key()

    isolated_secrets.set_error = None
    isolated_secrets.get_error = None
    secrets.reset_keyring_probe()
    assert secrets.get_api_key() == "sk-tutor"
    assert secrets.get_exa_api_key() is None


@pytest.mark.parametrize("exa", [False, True])
@pytest.mark.parametrize("delete", [False, True])
def test_preoperation_outage_never_resurrects_old_key(isolated_secrets, monkeypatch, exa, delete):
    username = secrets.EXA_USERNAME if exa else secrets.USERNAME
    setter = secrets.set_exa_api_key if exa else secrets.set_api_key
    getter = secrets.get_exa_api_key if exa else secrets.get_api_key
    deleter = secrets.delete_exa_api_key if exa else secrets.delete_api_key
    isolated_secrets.store[(secrets.SERVICE, username)] = "old-synthetic"
    monkeypatch.setattr(secrets, "_keyring_ok", False)
    if delete:
        deleter()
        deleter()
    else:
        setter("new-synthetic")
    secrets.reset_keyring_probe()
    assert getter() == (None if delete else "new-synthetic")


@pytest.mark.parametrize("exa", [False, True])
@pytest.mark.parametrize("operation", ["get_password", "set_password", "delete_password"])
def test_postprobe_timeout_is_bounded_and_late_operation_cannot_win(
    isolated_secrets, monkeypatch, exa, operation
):
    import threading
    import time

    username = secrets.EXA_USERNAME if exa else secrets.USERNAME
    setter = secrets.set_exa_api_key if exa else secrets.set_api_key
    getter = secrets.get_exa_api_key if exa else secrets.get_api_key
    deleter = secrets.delete_exa_api_key if exa else secrets.delete_api_key
    isolated_secrets.store[(secrets.SERVICE, username)] = "old-synthetic"
    assert secrets._keyring_usable()
    release = threading.Event()
    entered = threading.Event()
    original = getattr(isolated_secrets, operation)

    def blocked(*args):
        entered.set()
        release.wait(3)
        return original(*args)

    monkeypatch.setattr(isolated_secrets, operation, blocked)
    monkeypatch.setattr(secrets, "_PROBE_TIMEOUT_SECONDS", 0.03)
    started = time.monotonic()
    try:
        try:
            if operation == "set_password":
                setter("late-synthetic")
            elif operation == "delete_password":
                deleter()
            else:
                getter()
        except KeyError:
            pass  # A pending mutation truthfully reports failure.
        assert entered.is_set()
        worker = secrets._operation_thread
        for _ in range(10):
            setter("new-synthetic")
            assert secrets._operation_thread is worker
        assert time.monotonic() - started < 1
    finally:
        release.set()
        secrets._operation_thread.join(1)
    secrets.reset_keyring_probe()
    assert getter() == "new-synthetic"


def test_immutable_slot_read_does_not_block_event_loop(isolated_secrets, monkeypatch):
    import asyncio
    import threading
    import time

    from backend.core.errors import ConfigurationError

    identity = secrets.stage_tutor_credential("http://localhost/v1", "synthetic-slot-key")
    secrets._slot_read_cache.clear()  # New process: no in-memory credential yet.
    release = threading.Event()
    original = isolated_secrets.get_password

    def slow(*args):
        release.wait(2)
        return original(*args)

    monkeypatch.setattr(isolated_secrets, "get_password", slow)

    async def read():
        started = time.monotonic()
        with pytest.raises(ConfigurationError, match="Keychain is still responding"):
            secrets.get_tutor_credential(identity, "http://localhost/v1")
        assert time.monotonic() - started < 0.2
        await asyncio.sleep(0)

    try:
        asyncio.run(read())
    finally:
        release.set()
        secrets._operation_thread.join(1)
    assert secrets.get_tutor_credential(identity, "http://localhost/v1") == "synthetic-slot-key"
