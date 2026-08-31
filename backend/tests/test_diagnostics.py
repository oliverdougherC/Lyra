"""The diagnostics bundle, and the three lines it must never cross.

A bundle is meant to be pasted into a bug report, so the tests here are as much about what
is absent - the tutor key, the endpoint URL, a document's name, a home path - as about what
is present. `redact_path` is tested on its own because it is the rule a regression would
most quietly break.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import diagnostics
from backend.core.app_settings import update_settings_row
from backend.storage import secrets


@pytest.fixture(autouse=True)
def _known_key_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed key-storage answer, so no test reaches the real OS keychain."""
    monkeypatch.setattr(secrets, "has_api_key", lambda: False)
    monkeypatch.setattr(secrets, "api_key_storage", lambda: "keychain")
    monkeypatch.setattr(secrets, "has_exa_api_key", lambda: False)
    monkeypatch.setattr(secrets, "exa_api_key_storage", lambda: "keychain")


def test_redact_path_anchors_a_checkout_path_to_the_repo(tmp_path: Path) -> None:
    root = tmp_path / "lyra"
    assert diagnostics.redact_path(root / "data" / "lyra.db", root=root, home=tmp_path) == (
        "<lyra>/data/lyra.db"
    )
    assert diagnostics.redact_path(root, root=root, home=tmp_path) == "<lyra>"


def test_redact_path_hides_the_username_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home" / "student"
    root = tmp_path / "checkout"
    assert diagnostics.redact_path(home / "Lyra" / "data", root=root, home=home) == "~/Lyra/data"


def test_redact_path_keeps_only_the_basename_outside_known_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "checkout"
    # An absolute path under neither anchor discloses only its final component.
    assert diagnostics.redact_path("/private/secrets/course.pdf", root=root, home=home) == (
        ".../course.pdf"
    )


def test_build_diagnostics_reports_schema_as_current_on_a_migrated_database(
    db: sqlite3.Connection,
) -> None:
    bundle = diagnostics.build_diagnostics(db)

    schema = bundle["schema"]
    assert schema["version"] == schema["latest"]
    assert schema["current"] is True
    assert bundle["bundle_version"] == diagnostics.BUNDLE_VERSION


def test_build_diagnostics_reduces_the_endpoint_to_local_or_remote_and_leaks_neither_url_nor_key(
    db: sqlite3.Connection,
) -> None:
    update_settings_row(
        db,
        {"endpoint_url": "https://tutor.example.com/v1?token=SUPERSECRET", "model": "qwen-max"},
    )

    bundle = diagnostics.build_diagnostics(db)

    tutor = bundle["tutor"]
    assert tutor["endpoint_configured"] is True
    assert tutor["endpoint_is_local"] is False
    assert tutor["model"] == "qwen-max"
    serialised = json.dumps(bundle)
    # The URL and its embedded token are private; only the local-or-remote flag is reported.
    assert "SUPERSECRET" not in serialised
    assert "tutor.example.com" not in serialised


def test_build_diagnostics_reports_key_presence_and_storage_only(db: sqlite3.Connection) -> None:
    bundle = diagnostics.build_diagnostics(db)

    # Exactly two facts about the key, and neither is its value.
    assert bundle["api_key"] == {"present": False, "storage": "keychain"}
    assert bundle["web_research"]["exa_key_present"] is False
    assert bundle["web_research"]["exa_key_storage"] == "keychain"


def test_build_diagnostics_counts_content_without_naming_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'midterm-answers.pdf', '/x', 'application/pdf', 1, 'ready')",
        (class_id,),
    )
    db.commit()

    bundle = diagnostics.build_diagnostics(db)

    assert bundle["content"]["classes"] == 1
    assert bundle["content"]["documents"] == 1
    serialised = json.dumps(bundle)
    # Counts travel; the names behind them do not.
    assert "midterm-answers.pdf" not in serialised
    assert "Calculus II" not in serialised


def test_build_diagnostics_redacts_a_home_data_directory(db: sqlite3.Connection) -> None:
    # data_dir is the per-test dir under tmp_path; treating its parent as home makes the
    # bundle report it home-relative, the way a real install under the student's home would.
    home = settings.data_dir.parent

    bundle = diagnostics.build_diagnostics(db, home=home)

    assert bundle["paths"]["data_dir"] == "~/data"
    assert str(settings.data_dir) not in json.dumps(bundle)


def test_build_diagnostics_includes_a_redacted_startup_log_tail(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home" / "student"
    logs_dir = home / "logs"
    logs_dir.mkdir(parents=True)
    startup_log = logs_dir / "desktop-startup.log"
    startup_log.write_text(
        f"booting\n{home}/logs/desktop-startup.log\n<secret>\n"
        "provider failed with sk-proj-sensitivevalue\n"
        "/private/tmp/another-user/course.db\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "logs_dir", logs_dir)

    bundle = diagnostics.build_diagnostics(db, home=home)

    assert bundle["desktop"]["startup_log_present"] is True
    assert bundle["desktop"]["startup_log_path"] == "~/logs/desktop-startup.log"
    assert "~/logs/desktop-startup.log" in bundle["desktop"]["startup_log_tail"]
    assert "<redacted sensitive startup diagnostics>" in bundle["desktop"]["startup_log_tail"]
    assert "sk-proj-sensitivevalue" not in bundle["desktop"]["startup_log_tail"]
    assert "another-user" not in bundle["desktop"]["startup_log_tail"]
    assert str(home) not in json.dumps(bundle)
