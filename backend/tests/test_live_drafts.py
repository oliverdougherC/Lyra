"""Contract tests for the live structured draft suggestion store."""

import sqlite3

import pytest

from backend.core import artifacts, live_drafts, suggestions
from backend.core.errors import ConflictError

BASE = (
    "# Pendulum Lab\n"
    "\n"
    "## Introduction\n"
    "\n"
    "We measured the pendulum period.\n"
    "\n"
    "## Results\n"
    "\n"
    "The period grew with length.\n"
)


def _draft(db: sqlite3.Connection, class_id: int, content: str = BASE) -> tuple[int, int]:
    created = artifacts.create_artifact(db, class_id, "Pendulum Lab", [], kind=artifacts.KIND_DRAFT)
    artifact_id = int(created["id"])
    part_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id, part_id


def test_create_and_read_the_latest_live_suggestion(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(
        db,
        artifact_id,
        run_id=17,
        stage="drafting",
        status="running",
        detail="Drafting the structure",
        version=3,
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        section_ref="1.1",
        paragraph_ordinal=1,
        heading="Introduction",
        content="A sharper introduction.",
        summary="Explain the setup.",
        context={"prompt": "Open the lab."},
        metadata={"source": "model"},
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-1",
        section_ref="1.2",
        paragraph_ordinal=2,
        heading="Results",
        content="The period increased with length.",
        status="complete",
        target_words=120,
    )

    latest = live_drafts.get_latest_live_suggestion(db, artifact_id)

    assert latest is not None
    assert latest["run_id"] == 17
    assert latest["stage"] == "drafting"
    assert latest["status"] == "running"
    assert latest["detail"] == "Drafting the structure"
    assert latest["stage_detail"] == "Drafting the structure"
    assert latest["version"] == 3
    assert latest["base_content"] == BASE
    assert latest["base_hash"]
    assert [block["stable_key"] for block in latest["blocks"]] == ["intro-1", "results-1"]
    assert [block["block_key"] for block in latest["blocks"]] == ["intro-1", "results-1"]
    assert [block["ordinal"] for block in latest["blocks"]] == [1, 2]
    assert latest["blocks"][0]["context"] == {"prompt": "Open the lab."}
    assert latest["blocks"][0]["metadata"] == {"source": "model"}
    assert latest["blocks"][1]["target_words"] == 120


def test_lookup_by_run_and_update_bump_the_version(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(
        db,
        artifact_id,
        run_id=23,
        stage="gathering",
        status="running",
        detail="Gathering evidence",
        version=2,
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        metadata={"phase": "initial"},
    )

    updated = live_drafts.update_live_suggestion(
        db,
        int(suggestion["id"]),
        stage="outlining",
        status="running",
        detail="Building the outline",
        metadata={"checkpoint": "outline"},
    )
    fetched = live_drafts.get_live_suggestion_for_run(db, 23)

    assert updated["version"] == 3
    assert updated["stage"] == "outlining"
    assert updated["detail"] == "Building the outline"
    assert updated["blocks"][0]["metadata"] == {"phase": "initial", "checkpoint": "outline"}
    assert fetched is not None
    assert fetched["id"] == suggestion["id"]
    assert fetched["version"] == 3


def test_user_patch_requires_the_expected_revision(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=3)
    block = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="A first draft.",
    )

    edited = live_drafts.patch_block(
        db,
        int(block["id"]),
        expected_revision=int(block["revision"]),
        content="My own first draft.",
        status="editing",
    )

    assert edited["content"] == "My own first draft."
    assert edited["status"] == "editing"
    assert edited["revision"] == block["revision"] + 1
    assert edited["user_revision"] == edited["revision"]
    with pytest.raises(ConflictError, match="changed since it was fetched"):
        live_drafts.patch_block(
            db,
            int(block["id"]),
            expected_revision=int(block["revision"]),
            content="A stale overwrite.",
        )


def test_model_updates_preserve_user_content_but_streaming_can_extend_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=5)
    block = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="A first draft.",
    )
    block = live_drafts.patch_block(
        db,
        int(block["id"]),
        expected_revision=int(block["revision"]),
        content="My own first draft.",
    )

    preserved = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="A different model draft.",
    )
    extended = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="My own first draft. With a streamed ending.",
    )

    assert preserved["content"] == "My own first draft."
    assert preserved["user_revision"] == block["user_revision"]
    assert extended["content"] == "My own first draft. With a streamed ending."
    assert extended["revision"] > preserved["revision"]


def test_append_block_supports_incremental_streaming(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=9)

    first = live_drafts.append_block_text(
        db,
        int(suggestion["id"]),
        "methods-1",
        "First sentence. ",
        paragraph_ordinal=2,
        heading="Methods",
    )
    second = live_drafts.append_block_text(
        db,
        int(suggestion["id"]),
        "methods-1",
        "Second sentence.",
    )

    assert first["content"] == "First sentence. "
    assert second["content"] == "First sentence. Second sentence."
    assert second["revision"] > first["revision"]


def test_user_patch_preserves_a_suffix_streamed_after_editing_started(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=10)
    started = live_drafts.append_block_text(
        db,
        int(suggestion["id"]),
        "intro-1",
        "The old opening",
        paragraph_ordinal=1,
    )
    live_drafts.append_block_text(db, int(suggestion["id"]), "intro-1", " keeps streaming.")

    merged = live_drafts.patch_block(
        db,
        int(started["id"]),
        expected_revision=int(started["revision"]),
        base_content=str(started["content"]),
        content="The student's opening",
    )

    assert merged["content"] == "The student's opening keeps streaming."
    assert merged["user_revision"] == merged["revision"]


def test_user_patch_rejects_a_concurrent_non_append_rewrite(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=12)
    started = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="The old opening.",
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        paragraph_ordinal=1,
        content="A wholly rewritten opening.",
    )

    with pytest.raises(ConflictError, match="changed since it was fetched"):
        live_drafts.patch_block(
            db,
            int(started["id"]),
            expected_revision=int(started["revision"]),
            base_content=str(started["content"]),
            content="The student's opening.",
        )


def test_assemble_markdown_is_deterministic(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=11)
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-2",
        section_ref="1.2",
        paragraph_ordinal=3,
        heading="Results",
        content="The second results paragraph.",
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        section_ref="1.1",
        paragraph_ordinal=1,
        heading="Introduction",
        content="The first introduction paragraph.",
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-1",
        section_ref="1.2",
        paragraph_ordinal=2,
        heading="Results",
        content="The first results paragraph.",
    )

    assembled = live_drafts.assemble_markdown(db, int(suggestion["id"]))

    assert assembled == (
        "## Introduction\n\n"
        "The first introduction paragraph.\n\n"
        "## Results\n\n"
        "The first results paragraph.\n\n"
        "The second results paragraph.\n"
    )


def test_noop_finalize_does_not_delete_an_unrelated_pending_edit(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    pending = suggestions.propose(db, part_id, BASE + "\nA separate suggestion.\n", "other")
    suggestion = live_drafts.create_live_suggestion(
        db,
        artifact_id,
        run_id=13,
        base_content="",
    )

    result = live_drafts.finalize_to_pending_edit(db, int(suggestion["id"]))
    preserved = db.execute(
        "select id, proposed_content from pending_edits where part_id = ?", (part_id,)
    ).fetchone()

    assert result is None
    assert pending is not None
    assert preserved is not None
    assert preserved["id"] == pending.id
    assert preserved["proposed_content"].endswith("A separate suggestion.\n")


def test_finalize_updates_one_pending_edit_against_the_original_base(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=13)
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "intro-1",
        section_ref="1.1",
        paragraph_ordinal=1,
        heading="Introduction",
        content="A stronger introduction.",
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-1",
        section_ref="1.2",
        paragraph_ordinal=2,
        heading="Results",
        content="A stronger results section.",
    )

    artifacts.set_part_content(
        db,
        part_id,
        BASE + "\nA student note.\n",
        origin=artifacts.USER_CORRECTED,
        record_revision=False,
    )
    first = live_drafts.finalize_to_pending_edit(db, int(suggestion["id"]), note="Refine the draft")
    pending_row = db.execute(
        "select id, base_content, base_hash, proposed_content, note "
        "from pending_edits where part_id = ?",
        (part_id,),
    ).fetchone()

    assert first is not None
    assert str(artifacts.get_part(db, part_id)["content"]).endswith("A student note.\n")
    assert pending_row is not None
    assert pending_row["base_content"] == BASE
    assert pending_row["base_hash"] == suggestion["base_hash"]
    assert pending_row["proposed_content"] == (
        "## Introduction\n\nA stronger introduction.\n\n## Results\n\nA stronger results section.\n"
    )
    assert pending_row["note"] == "Refine the draft"

    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-1",
        section_ref="1.2",
        paragraph_ordinal=2,
        heading="Results",
        content="An even stronger results section.",
    )
    second = live_drafts.finalize_to_pending_edit(
        db, int(suggestion["id"]), note="Refine the draft"
    )
    refreshed = db.execute(
        "select id, proposed_content from pending_edits where part_id = ?",
        (part_id,),
    ).fetchone()

    assert second is not None
    assert refreshed is not None
    assert refreshed["id"] == pending_row["id"]
    assert "An even stronger results section." in refreshed["proposed_content"]


def test_assemble_markdown_canonicalizes_accidental_indentation_and_math_tokens(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(
        db, artifact_id, run_id=17, stage="drafting", status="running"
    )
    live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "results-1",
        section_ref="1.2",
        paragraph_ordinal=1,
        heading="Results",
        content=(
            "    X_1 converges to R^n while \\dots represents the omitted intermediate "
            "eigenvectors throughout this complete basis argument for the transformed space.\n"
            "The resulting scale is \\frac{a}{b}."
        ),
    )

    assembled = live_drafts.assemble_markdown(db, int(suggestion["id"]))

    assert assembled == (
        "## Results\n\n"
        "$X_1$ converges to $R^n$ while $\\dots$ represents the omitted intermediate "
        "eigenvectors throughout this complete basis argument for the transformed space.\n"
        "The resulting scale is $\\frac{a}{b}$.\n"
    )
