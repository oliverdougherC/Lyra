"""Model citations and bounded saved-source context retain actual evidence identity."""

import pytest

from backend.core import classes, exporting, source_ledger
from backend.core.errors import NotFoundError


@pytest.fixture
def source(db, class_id):
    return source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic transport memo",
        url="https://synthetic.invalid/memo",
        snapshot="There were 240 boardings. Boardings count trips, not unique people.",
    )


def test_normalized_model_shorthand_resolves_through_existing_exporter(source):
    body = f"Trips are not unique riders [@{source['id']}]."
    assert exporting.render_citations(body, [source]) == body
    normalized = source_ledger.normalize_model_citations(body, [source["id"]])
    assert normalized == f"Trips are not unique riders [@lyra:{source['id']}]."
    rendered = exporting.render_citations(normalized, [source])
    assert "Synthetic transport memo" in rendered
    assert "[@" not in rendered
    assert body == f"Trips are not unique riders [@{source['id']}]."


@pytest.mark.parametrize(
    "text",
    [
        "Claim [@999].",
        "Claim [@lyra:999].",
        "Claim [@Smith2024].",
        "Claim [@lyra:word].",
        "Claim [@lyra:1",
        "Claim [@1\nnext line",
    ],
)
def test_unknown_model_citations_fail(text):
    with pytest.raises(ValueError):
        source_ledger.normalize_model_citations(text, [1])


def test_known_canonical_is_preserved_and_uncited_text_unchanged():
    assert source_ledger.normalize_model_citations("Claim [@lyra:1].", [1]) == "Claim [@lyra:1]."
    assert (
        source_ledger.normalize_model_citations("Uncited student voice.", [])
        == "Uncited student voice."
    )


def test_context_contains_nearby_caveat_without_mutating_ledger(db, class_id, source):
    source_ledger.add_relied_on_excerpt(db, source["id"], "There were 240 boardings.")
    before = list(db.iterdump())
    context = source_ledger.saved_source_context(db, class_id, [source["id"]])
    assert "not unique people" in context[0]["content"]
    assert context[0]["source_revision_id"] == source["current_revision_id"]
    assert context[0]["revision"] == 1
    assert not context[0]["omitted"]
    assert not context[0]["evidence_unavailable"]
    assert list(db.iterdump()) == before
    assert "snapshot" not in source_ledger.list_sources(db, class_id)[0]


def test_context_current_revision_is_bounded_and_never_relabels_old_excerpt(db, class_id, source):
    old_id = source["current_revision_id"]
    source_ledger.add_relied_on_excerpt(db, source["id"], "There were 240 boardings.")
    newer = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic transport memo",
        url="https://synthetic.invalid/memo",
        snapshot="New observation " * 500,
        truncated=True,
    )
    item = source_ledger.saved_source_context(db, class_id, [source["id"]])[0]
    assert len(item["content"]) == 4000
    assert item["omitted"] and item["snapshot_truncated"]
    assert item["source_revision_id"] == newer["current_revision_id"] != old_id
    assert item["revision"] == 2
    assert source_ledger.get_source(db, source["id"])["excerpts"][0]["source_revision_id"] == old_id


def test_missing_current_snapshot_does_not_fall_back_to_old_material(db, class_id, source):
    source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic transport memo",
        url="https://synthetic.invalid/memo",
        snapshot="",
    )
    item = source_ledger.saved_source_context(db, class_id, [source["id"]])[0]
    assert item["evidence_unavailable"]
    assert item["content"] == ""
    assert item["revision"] == 2


def test_context_refuses_other_class_source(db, class_id, source):
    other = classes.create_class(db, "Other synthetic class")
    with pytest.raises(NotFoundError):
        source_ledger.saved_source_context(db, other["id"], [source["id"]])
