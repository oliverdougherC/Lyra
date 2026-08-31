"""Shared copy primitives for the explicit desktop import flow."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backend import desktop_migration


def test_legacy_automatic_migration_entrypoint_is_removed() -> None:
    assert not hasattr(desktop_migration, "migrate_source_data_if_needed")


def test_copy_regular_file_refuses_a_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation is not generally available on Windows")
    source = tmp_path / "source"
    source.write_text("private", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(source)

    with pytest.raises(RuntimeError, match="refused non-regular"):
        desktop_migration.copy_regular_file(link, tmp_path / "destination")


def test_verify_sqlite_accepts_a_consistent_database(tmp_path: Path) -> None:
    database = tmp_path / "lyra.db"
    with sqlite3.connect(database) as conn:
        conn.execute("create table sample (id integer primary key, value text not null)")
        conn.execute("insert into sample (value) values ('kept')")

    desktop_migration.verify_sqlite(database)
