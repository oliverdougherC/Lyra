"""Writer depth, durable plans, ledger, and capability storage."""

import sqlite3

import pytest

from backend.core import artifacts, source_ledger, writer_budgets, writer_plans
from backend.llm import prompts


def _draft(db: sqlite3.Connection, class_id: int) -> int:
    created = artifacts.create_artifact(
        db, class_id, "Research essay", [], kind=artifacts.KIND_DRAFT
    )
    artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content="",
        status=artifacts.PART_COMPLETE,
    )
    return int(created["id"])


def _document(db: sqlite3.Connection, class_id: int, filename: str = "reading.pdf") -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, 'application/pdf', 1, 'ready')",
        (class_id, filename, filename),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def test_depth_profiles_are_validated_and_scale_one_immutable_mapping() -> None:
    quick = writer_budgets.get_budget(" QUICK ")
    standard = writer_budgets.get_budget("standard")
    deep = writer_budgets.get_budget("deep")

    assert quick.max_critique_rounds < standard.max_critique_rounds < deep.max_critique_rounds
    assert quick.wall_clock_seconds < standard.wall_clock_seconds < deep.wall_clock_seconds
    assert quick.max_revise_rounds == quick.max_critique_rounds
    with pytest.raises(ValueError, match="depth"):
        writer_budgets.get_budget("exhaustive")
    with pytest.raises(TypeError):
        writer_budgets.BUDGETS["quick"] = deep  # type: ignore[index]


def test_writer_capability_defaults_are_off_serial_and_class_overrides_are_tri_state(
    db: sqlite3.Connection, class_id: int
) -> None:
    settings = db.execute(
        "select allow_web_research, parallel_requests, parallel_concurrency "
        "from settings where id = 1"
    ).fetchone()
    assert tuple(settings) == (0, 0, 1)
    assert writer_budgets.get_writer_capabilities(
        db, class_id
    ) == writer_budgets.WriterCapabilities(
        allow_web_research=False,
        parallel_requests=False,
        parallel_concurrency=1,
        firecrawl_base_url="http://127.0.0.1:3002",
        firecrawl_scrape_enabled=False,
    )

    effective = writer_budgets.update_class_capability_overrides(
        db,
        class_id,
        allow_web_research=True,
        parallel_requests=True,
        parallel_concurrency=3,
    )
    assert effective.allow_web_research is True
    assert effective.parallel_concurrency == 3

    # Null returns each setting to inheritance. Change the global values to prove this
    # is not merely another spelling of false/one.
    db.execute("update settings set allow_web_research = 1, parallel_concurrency = 5 where id = 1")
    db.commit()
    inherited = writer_budgets.update_class_capability_overrides(
        db, class_id, allow_web_research=None, parallel_concurrency=None
    )
    assert inherited.allow_web_research is True
    assert inherited.parallel_concurrency == 5


def test_create_update_and_replan_preserve_version_history(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    first = writer_plans.create_plan(
        db,
        draft_id,
        brief_analysis="Compare the two accounts.",
        thesis="The archive complicates the standard account.",
        argument_map={"claims": [{"id": "c1", "supports": []}]},
        sections=[
            {
                "section_ref": "1",
                "title": "The standard account",
                "job": "Establish the view being challenged.",
                "claim": "The standard account omits the archive.",
                "evidence": ["Textbook chapter 2"],
                "source_ids": [],
                "word_budget": 450,
            },
            {
                "section_ref": "2",
                "title": "The archive",
                "job": "Present the counterevidence.",
                "word_budget": 650,
            },
        ],
    )
    assert first["version"] == 1
    assert first["active"] is True
    assert first["sections"][0]["evidence"] == ["Textbook chapter 2"]

    section = writer_plans.update_plan_section(
        db, int(first["id"]), "2", research_notes="Archive box 14 is decisive."
    )
    assert section["research_notes"] == "Archive box 14 is decisive."

    second = writer_plans.new_plan_version(
        db, draft_id, thesis="The archive overturns the standard account."
    )
    assert second["version"] == 2
    assert second["sections"][1]["research_notes"] == "Archive box 14 is decisive."
    history = writer_plans.list_plan_versions(db, draft_id)
    assert [item["version"] for item in history] == [2, 1]
    assert [item["active"] for item in history] == [True, False]
    with pytest.raises(ValueError, match="read-only"):
        writer_plans.update_plan(db, int(first["id"]), thesis="Rewrite history")


def test_invalid_replan_leaves_the_prior_plan_active(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)
    first = writer_plans.create_plan(
        db, draft_id, thesis="Keep me", sections=[{"section_ref": "1"}]
    )

    with pytest.raises(ValueError, match="unique"):
        writer_plans.create_plan(
            db,
            draft_id,
            thesis="Bad plan",
            sections=[{"section_ref": "same"}, {"section_ref": "same"}],
        )

    active = writer_plans.get_active_plan(db, draft_id)
    assert active is not None
    assert active["id"] == first["id"]


def test_source_ledger_treats_course_and_web_sources_uniformly_and_course_first(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    db.execute(
        "insert into chunks "
        "(document_id, class_id, content, token_count, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, 'The assigned author''s claim.', 5, 'reading', 'test', 4)",
        (document_id, class_id),
    )
    db.commit()
    web = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="A public source",
        url="https://example.com/report#methods",
        snapshot="A frozen page. The reported finding.",
        excerpts=[{"section_ref": "2", "excerpt": "The reported finding."}],
    )
    course = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.COURSE,
        title="Assigned reading",
        document_id=document_id,
        snapshot="Extracted course text.",
        excerpts=["The assigned author's claim."],
    )

    listed = source_ledger.list_sources(db, class_id)
    assert [item["source_type"] for item in listed] == ["course", "web"]
    assert web["url"] == "https://example.com/report"
    assert course["document_id"] == document_id
    assert listed[1]["excerpts"][0]["section_ref"] == "2"

    refreshed = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Updated title",
        url="https://example.com/report",
    )
    assert refreshed["id"] == web["id"]
    assert refreshed["snapshot"] == "A frozen page. The reported finding."
    assert refreshed["excerpts"] == web["excerpts"]


def test_course_snapshot_survives_document_deletion(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="course",
        title="Reading",
        document_id=document_id,
        snapshot="Durable snapshot",
    )

    db.execute("delete from documents where id = ?", (document_id,))
    db.commit()

    retained = source_ledger.get_source(db, int(source["id"]))
    assert retained["document_id"] is None
    assert retained["snapshot"] == "Durable snapshot"


def test_idempotent_course_registration_preserves_its_access_date(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    first = source_ledger.upsert_source(
        db,
        class_id,
        source_type="course",
        title="Reading",
        document_id=document_id,
        accessed_at="2026-01-02T03:04:05+00:00",
    )

    repeated = source_ledger.upsert_source(
        db,
        class_id,
        source_type="course",
        title="Reading",
        document_id=document_id,
    )

    assert repeated["id"] == first["id"]
    assert repeated["accessed_at"] == "2026-01-02T03:04:05+00:00"


def test_ledger_listing_omits_audit_snapshots_but_keeps_exact_relied_on_excerpts(
    db: sqlite3.Connection, class_id: int
) -> None:
    snapshot = "the exact relied-on sentence\n" + ("never-put-this-audit-blob-in-a-prompt" * 55_000)
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Large audited page",
        url="https://example.com/large",
        snapshot=snapshot,
        excerpts=[{"section_ref": "3", "excerpt": "the exact relied-on sentence"}],
    )

    listed = source_ledger.list_sources(db, class_id)
    assert "snapshot" not in listed[0]
    assert listed[0]["excerpts"][0]["excerpt"] == "the exact relied-on sentence"
    rendered = source_ledger.format_sources_for_prompt(listed)
    assert "never-put-this-audit-blob-in-a-prompt" not in rendered
    assert "the exact relied-on sentence" in rendered
    actual_prompt_block = prompts.format_ledger_block(listed)
    assert "never-put-this-audit-blob-in-a-prompt" not in actual_prompt_block
    assert "the exact relied-on sentence" in actual_prompt_block

    audited = source_ledger.get_source(db, int(source["id"]))
    assert audited["snapshot"] == snapshot
    assert "never-put-this-audit-blob-in-a-prompt" not in source_ledger.format_sources_for_prompt(
        [audited]
    )


def test_relied_on_web_excerpt_must_be_an_exact_snapshot_passage(
    db: sqlite3.Connection, class_id: int
) -> None:
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Study",
        url="https://example.com/study",
        snapshot="The measured effect was exactly twelve percent.",
    )

    with pytest.raises(ValueError, match="exact passage"):
        source_ledger.add_relied_on_excerpt(
            db, int(source["id"]), "The effect was about twelve percent."
        )

    recorded = source_ledger.add_relied_on_excerpt(
        db,
        int(source["id"]),
        "measured effect was exactly twelve percent",
        section_ref="2",
    )
    assert recorded["section_ref"] == "2"


def test_web_refresh_preserves_revisions_and_binds_excerpts_to_the_current_snapshot(
    db: sqlite3.Connection, class_id: int
) -> None:
    first = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Study",
        url="https://example.com/study",
        final_url="https://example.com/study?v=1",
        content_type="text/markdown",
        snapshot="First durable passage.",
    )
    first_revision = first["current_revision_id"]
    source_ledger.add_relied_on_excerpt(db, int(first["id"]), "First durable passage.")

    refreshed = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Study refreshed",
        url="https://example.com/study",
        final_url="https://example.com/study?v=2",
        content_type="text/markdown",
        snapshot="Second durable passage.",
        truncated=True,
    )

    assert refreshed["id"] == first["id"]
    assert refreshed["current_revision_id"] != first_revision
    assert refreshed["revision"]["final_url"] == "https://example.com/study?v=2"
    assert refreshed["revision"]["truncated"] is True
    assert refreshed["excerpts"][0]["source_revision_id"] == first_revision
    assert (
        db.execute(
            "select count(*) from writer_source_revisions where source_id = ?", (first["id"],)
        ).fetchone()[0]
        == 2
    )


def test_relied_on_course_excerpt_must_exist_in_that_documents_chunks(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    db.execute(
        "insert into chunks "
        "(document_id, class_id, content, token_count, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, 8, 'reading', 'test', 4)",
        (document_id, class_id, "Longer pendulums have a longer measured period."),
    )
    db.commit()
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="course",
        title="Reading",
        document_id=document_id,
    )

    with pytest.raises(ValueError, match="exact passage"):
        source_ledger.add_relied_on_excerpt(db, int(source["id"]), "Mass controls the period.")
    source_ledger.add_relied_on_excerpt(
        db, int(source["id"]), "pendulums have a longer measured period"
    )

    assert (
        source_ledger.get_source(db, int(source["id"]))["excerpts"][0]["excerpt"]
        == "pendulums have a longer measured period"
    )
