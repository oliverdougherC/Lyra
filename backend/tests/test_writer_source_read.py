"""Saved-source reading is bounded, revision-specific, class-scoped, and read-only."""

import pytest

from backend.core import classes, review_pipeline, source_ledger, writer_tools
from backend.tests.test_writer_tools import _draft


@pytest.fixture
def source_reader(db, class_id):
    artifact_id, _ = _draft(db, class_id)
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic story",
        url="https://example.invalid/story",
        snapshot="Mara set the blue cup on the table.",
    )
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)
    return registry, source, artifact_id


def test_read_saved_snapshot_beyond_selected_excerpt_without_mutation(db, source_reader):
    registry, source, _ = source_reader
    source_ledger.add_relied_on_excerpt(db, source["id"], "blue cup")
    before = list(db.iterdump())
    result = registry["read_source"].handler(source_id=source["id"])
    assert result.ok
    assert result.value["content"] == "Mara set the blue cup on the table."
    assert result.value["source_revision_id"] == source["current_revision_id"]
    assert result.value["revision"] == 1
    assert result.value["next_offset"] is None
    assert not result.value["omitted"]
    assert list(db.iterdump()) == before


def test_historical_snapshot_never_falls_forward(db, class_id, source_reader):
    registry, source, _ = source_reader
    old_id = source["current_revision_id"]
    source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic story",
        url="https://example.invalid/story",
        snapshot="A gate remained open.",
    )
    historical = registry["read_source"].handler(source_id=source["id"], source_revision_id=old_id)
    current = registry["read_source"].handler(source_id=source["id"])
    assert historical.ok and "Mara" in historical.value["content"]
    assert historical.value["source_revision_id"] == old_id
    assert current.ok and current.value["revision"] == 2
    assert "Mara" not in current.value["content"]
    missing = registry["read_source"].handler(source_id=source["id"], source_revision_id=999999)
    assert not missing.ok and not missing.value


def test_pagination_pins_revision_and_reports_original_truncation(db, class_id, source_reader):
    registry, source, _ = source_reader
    content = "word " * 1700
    saved = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic story",
        url="https://example.invalid/story",
        snapshot=content,
        truncated=True,
    )
    read = registry["read_source"].handler
    page = read(source_id=source["id"])
    assert page.ok and len(page.value["content"]) == 4000
    assert page.value["omitted"] and page.value["snapshot_truncated"]
    assert page.value["next_offset"] == 4000
    rest = read(
        source_id=source["id"], source_revision_id=saved["current_revision_id"], offset=4000
    )
    assert rest.ok and rest.value["content"] == content[4000:8000]
    assert rest.value["total_chars"] == len(content)
    assert not read(source_id=source["id"], offset=len(content) + 1).ok


@pytest.mark.parametrize(
    "arguments",
    [
        {"source_id": True},
        {"source_id": -1},
        {"source_id": "1"},
        {"offset": -1},
        {"offset": 1.5},
        {"source_revision_id": False},
        {"source_revision_id": "1"},
    ],
)
def test_invalid_arguments_are_tool_failures(source_reader, arguments):
    registry, source, _ = source_reader
    assert not registry["read_source"].handler(**{"source_id": source["id"], **arguments}).ok


def test_foreign_class_and_foreign_revision_fail(db, source_reader):
    registry, source, _ = source_reader
    other_class = classes.create_class(db, "Other synthetic class")
    other = source_ledger.upsert_source(
        db,
        other_class["id"],
        source_type=source_ledger.WEB,
        title="Private other course",
        url="https://example.invalid/other",
        snapshot="Other class private material.",
    )
    read = registry["read_source"].handler
    assert not read(source_id=other["id"]).ok
    assert not read(source_id=source["id"], source_revision_id=other["current_revision_id"]).ok


def test_missing_snapshot_does_not_use_mutable_source_copy(db, source_reader):
    registry, source, _ = source_reader
    db.execute(
        "update writer_source_revisions set snapshot = '' where id = ?",
        (source["current_revision_id"],),
    )
    db.commit()
    assert not registry["read_source"].handler(source_id=source["id"]).ok


def test_source_reader_granted_to_every_profile_and_captured_reviewer(db, class_id, source_reader):
    _, _, artifact_id = source_reader
    for profile in writer_tools.PROFILES:
        registry, _ = writer_tools.build_registry(db, artifact_id, profile)
        assert "read_source" in registry
    registry, _, _ = review_pipeline._capture_registry(db, artifact_id, class_id)
    assert "read_source" in registry


def test_saved_course_snapshot_is_honestly_unversioned(db, class_id, source_reader):
    registry, _, _ = source_reader
    document_id = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, ?, ?, ?) returning id",
        (class_id, "fixture.txt", "synthetic-fixture", "text/plain", 1, "ready"),
    ).fetchone()[0]
    db.commit()
    course = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.COURSE,
        title="Retained course text",
        document_id=document_id,
        snapshot="The saved course passage remains readable.",
    )
    db.execute("delete from documents where id = ?", (document_id,))
    db.commit()
    result = registry["read_source"].handler(source_id=course["id"])
    assert result.ok
    assert result.value["provenance"] == "unversioned_saved_course_snapshot"
    assert result.value["source_revision_id"] is None
    assert result.value["snapshot_truncated"] is None
    assert not registry["read_source"].handler(source_id=course["id"], source_revision_id=99999).ok
