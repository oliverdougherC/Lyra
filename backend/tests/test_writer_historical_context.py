"""Explicit historical prompt context preserves revision identity and isolation."""

import pytest

from backend.core import classes, source_ledger
from backend.core.errors import NotFoundError


def save(db, class_id, url, content, **kwargs):
    return source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic memo",
        url=url,
        snapshot=content,
        **kwargs,
    )


def test_explicit_history_is_bounded_distinct_current_first_and_read_only(db, class_id):
    old = save(db, class_id, "https://synthetic.invalid/a", "Historical " * 500, truncated=True)
    current = save(db, class_id, "https://synthetic.invalid/a", "Counts withdrawn.")
    second = save(db, class_id, "https://synthetic.invalid/b", "Other current.")
    old_id = old["current_revision_id"]
    before = list(db.iterdump())
    result = source_ledger.saved_source_context(
        db,
        class_id,
        [old["id"], second["id"]],
        supporting_revision_ids={old["id"]: [old_id, old_id, current["current_revision_id"]]},
    )
    assert [r["source_revision_id"] for r in result] == [
        current["current_revision_id"],
        second["current_revision_id"],
        old_id,
    ]
    historical = result[2]
    assert historical["revision"] == 1
    assert historical["provenance"] == "immutable_revision"
    assert len(historical["content"]) == 4000
    assert historical["omitted"] and historical["snapshot_truncated"]
    assert not historical["evidence_unavailable"]
    assert "Historical" in historical["note"]
    assert historical["content"] != result[0]["content"]
    assert list(db.iterdump()) == before
    assert source_ledger.saved_source_context(db, class_id, [old["id"]]) == result[:1]


def test_missing_or_foreign_revision_never_falls_forward(db, class_id):
    first = save(db, class_id, "https://synthetic.invalid/a", "Current private facts.")
    other = save(db, class_id, "https://synthetic.invalid/b", "Other source secrets.")
    result = source_ledger.saved_source_context(
        db,
        class_id,
        [first["id"]],
        supporting_revision_ids={first["id"]: [999999, other["current_revision_id"]]},
    )
    assert len(result) == 3
    for entry, requested in zip(result[1:], [999999, other["current_revision_id"]], strict=True):
        assert entry["source_revision_id"] == requested
        assert entry["evidence_unavailable"]
        assert entry["content"] == ""
        assert entry["revision"] is None
        assert entry["accessed_at"] is None
        assert entry["provenance"] == "historical_revision_unavailable"


def test_requested_history_respects_class_and_selected_sources(db, class_id):
    other_class = classes.create_class(db, "Other synthetic course")
    foreign = save(db, other_class["id"], "https://synthetic.invalid/foreign", "Foreign facts.")
    own = save(db, class_id, "https://synthetic.invalid/own", "Own facts.")
    with pytest.raises(NotFoundError):
        source_ledger.saved_source_context(
            db,
            class_id,
            [foreign["id"]],
            supporting_revision_ids={foreign["id"]: [foreign["current_revision_id"]]},
        )
    result = source_ledger.saved_source_context(
        db,
        class_id,
        [own["id"]],
        supporting_revision_ids={foreign["id"]: [foreign["current_revision_id"]]},
    )
    assert len(result) == 1
    assert result[0]["source_id"] == own["id"]
    missing = source_ledger.saved_source_context(
        db,
        class_id,
        [own["id"]],
        supporting_revision_ids={own["id"]: [foreign["current_revision_id"]]},
    )[-1]
    assert missing["evidence_unavailable"] and missing["content"] == ""
