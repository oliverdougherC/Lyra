"""The writer's tools, driven directly against a real draft in a temporary database.

The two contracts that matter most: a proposal over an existing proposal coalesces into
one reviewable edit, and a direct write re-checks emptiness at write time - both are
what protect the student's prose from the model that is trying to help with it.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from backend.core import (
    artifacts,
    briefs,
    comments,
    errors,
    query_guard,
    sessions,
    source_ledger,
    suggestions,
    writer_attempts,
    writer_tools,
)
from backend.llm.tools import RecordedCall
from backend.storage.database import connect
from backend.tools.result import ToolResult

BODY = (
    "# Introduction\n"
    "\n"
    "Opening prose the student wrote.\n"
    "\n"
    "# Methods\n"
    "\n"
    "[TODO: describe the rig]\n"
    "\n"
    "# Results\n"
    "\n"
    "Numbers went up.\n"
)


def _draft(db: sqlite3.Connection, class_id: int, content: str = BODY) -> tuple[int, int]:
    """A draft with one body. Returns (artifact_id, part_id)."""
    created = artifacts.create_artifact(db, class_id, "Lab 3", [], kind=artifacts.KIND_DRAFT)
    part_id = artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, int(created["id"]), artifacts.READY)
    return int(created["id"]), part_id


def _call(registry: dict[str, object], name: str, **arguments: object) -> ToolResult:
    """Invoke one tool's handler the way the loop would, arguments already parsed."""
    return registry[name].handler(**arguments)


def test_grants_differ_by_profile(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)

    chat, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)
    drafter, _ = writer_tools.build_registry(db, artifact_id, writer_tools.DRAFTER)
    reviewer, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    assert "propose_revision" in chat and "write_section" not in chat
    assert "save_brief" in chat and "save_brief" not in drafter
    assert "write_section" in drafter
    # The reviewer reads and files comments; it never touches the document.
    assert "add_comment" in reviewer
    assert "add_comment" not in chat and "add_comment" not in drafter
    assert "write_section" not in reviewer and "propose_revision" not in reviewer
    assert "read_plan" in chat and "record_source_excerpt" in reviewer
    assert "search_web" not in chat and "fetch_source" not in reviewer


def test_web_tools_are_gated_then_snapshot_and_record_evidence(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    snapshot = "The measured effect was twelve percent." + (" audit-only" * 2_000)
    db.execute("update settings set allow_web_research = 1 where id = 1")
    db.commit()
    monkeypatch.setattr(
        writer_tools.web_research,
        "search_web",
        lambda query, *, allowed, firecrawl_base_url, private_context=(): [
            {"title": "Study", "url": "https://example.test/study"}
        ],
    )
    monkeypatch.setattr(
        writer_tools.web_research,
        "fetch_source",
        lambda url, *, allowed, firecrawl_base_url, scrape_enabled: {
            "url": url,
            "final_url": url,
            "title": "Study",
            "accessed_at": "2026-08-07T12:00:00+00:00",
            "snapshot": snapshot,
            "content_type": "text/html",
            "truncated": False,
        },
    )
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    assert _call(registry, "search_web", query="measured effect").ok
    fetched = _call(registry, "fetch_source", url="https://example.test/study")
    assert "snapshot" not in fetched.value
    assert len(str(fetched.value["content_preview"])) == writer_tools._FETCH_PREVIEW_CHARS
    assert str(fetched.value["content_preview"]).startswith(
        "The measured effect was twelve percent."
    )
    source_id = int(fetched.value["source_id"])
    invented = _call(
        registry,
        "record_source_excerpt",
        source_id=source_id,
        excerpt="The effect was thirteen percent.",
        section_ref="2",
    )
    recorded = _call(
        registry,
        "record_source_excerpt",
        source_id=source_id,
        excerpt="The measured effect was twelve percent.",
        section_ref="2",
    )

    assert fetched.ok and recorded.ok and invented.ok is False
    assert recorded.value["citation"] == f"[@lyra:{source_id}]"
    source = source_ledger.get_source(db, source_id, class_id=class_id)
    assert source["snapshot"] == snapshot
    assert source["excerpts"][0]["section_ref"] == "2"


def test_web_search_refuses_overlap_with_private_draft_and_comment_context(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "# Introduction\n\n"
        "The unpublished assignment describes the weighted residual method proof for "
        "nonlinear boundary conditions.\n"
    )
    artifact_id, part_id = _draft(db, class_id, content=body)
    db.execute("update settings set allow_web_research = 1 where id = 1")
    db.commit()

    def guarded_search(
        query: str, *, allowed: bool, firecrawl_base_url: str, private_context: tuple[str, ...]
    ) -> list[dict[str, str]]:
        guarded = query_guard.guard_web_query(query, private_context=private_context)
        if isinstance(guarded, query_guard.QueryRefusal):
            raise ValueError(guarded.message)
        return []

    monkeypatch.setattr(writer_tools.web_research, "search_web", guarded_search)
    comment = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "The unpublished assignment describes the weighted residual method proof for "
        "nonlinear boundary conditions.",
        severity="major",
    )
    db.commit()

    chat, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)
    reviewer, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    draft_overlap = _call(
        chat,
        "search_web",
        query="weighted residual method proof for nonlinear boundary conditions",
    )
    assert draft_overlap.ok is False
    assert "overlaps too closely" in str(draft_overlap.error)

    assert _call(reviewer, "read_comments").ok
    comment_overlap = _call(
        reviewer,
        "search_web",
        query="weighted residual method proof for nonlinear boundary conditions",
    )
    assert comment_overlap.ok is False
    assert "overlaps too closely" in str(comment_overlap.error)
    assert int(comment["id"]) > 0


def test_course_search_registers_sources_without_claiming_every_result_was_relied_on(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'reading.pdf', 'reading.pdf', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()
    chunk = SimpleNamespace(
        document_id=document_id,
        filename="reading.pdf",
        section_title="Evidence",
        section_path="Evidence",
        page_number=4,
        content="A candidate passage returned by retrieval.",
    )
    monkeypatch.setattr(
        writer_tools,
        "retrieve",
        lambda conn, class_id, query, budget: SimpleNamespace(chunks=[chunk]),
    )
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(registry, "search_course_material", query="candidate")

    assert result.ok
    source_id = int(result.value["results"][0]["source_id"])
    assert source_ledger.get_source(db, source_id)["excerpts"] == []


def test_an_unknown_profile_is_a_caller_bug(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)

    with pytest.raises(ValueError, match="profile"):
        writer_tools.build_registry(db, artifact_id, "editor")


def test_read_tools_report_the_draft(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    outline = _call(registry, "read_outline")
    section = _call(registry, "read_section", ref="Methods")
    missing = _call(registry, "read_section", ref="Discussion")

    assert outline.ok and "2 Methods (empty)" in str(outline.value["outline"])
    assert section.ok and section.value["number"] == "2"
    assert "[TODO: describe the rig]" in str(section.value["text"])
    assert missing.ok is False and "read_outline" in missing.error


def test_read_brief_hints_at_discernment_until_one_exists(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    empty = _call(registry, "read_brief")
    briefs.save_brief(db, artifact_id, summary="Pendulum lab report.")
    full = _call(registry, "read_brief")

    assert empty.ok and empty.value["brief"] is None
    assert "save_brief" in str(empty.value["note"])
    assert full.ok and full.value["brief"]["summary"] == "Pendulum lab report."
    assert full.value["brief"]["status"] == briefs.PROPOSED


def test_save_brief_proposes_and_marks_the_effects(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(registry, "save_brief", summary="An essay on entropy.", audience="the TA")

    assert result.ok and result.value["status"] == briefs.PROPOSED
    assert effects.brief_saved is True
    stored = briefs.get_brief(db, artifact_id)
    assert stored is not None and stored["audience"] == "the TA"
    assert stored["status"] == briefs.PROPOSED


def test_propose_revision_lands_a_pending_edit_and_reports_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(
        registry,
        "propose_revision",
        section="Results",
        replacement="# Results\n\nNumbers went up, and here is by how much.\n",
    )

    assert result.ok and result.value["proposed"] is True
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert effects.proposed_edit_id == pending["id"]
    # The document itself is untouched: the proposal is a row, not a write.
    assert str(artifacts.get_part(db, part_id)["content"]) == BODY


def test_two_proposals_coalesce_into_one_reviewable_edit(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    _call(
        registry,
        "propose_revision",
        section="Results",
        replacement="# Results\n\nRevised results.\n",
    )
    _call(
        registry,
        "propose_revision",
        section="Introduction",
        replacement="# Introduction\n\nRevised introduction.\n",
    )

    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    proposed = str(pending["proposed_content"])
    # Both revisions are in the one proposal, and what neither touched survives.
    assert "Revised results." in proposed
    assert "Revised introduction." in proposed
    assert "[TODO: describe the rig]" in proposed
    rows = db.execute("select count(*) as n from pending_edits").fetchone()
    assert rows["n"] == 1


def test_a_proposal_matching_the_document_is_not_a_suggestion(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(
        registry,
        "propose_revision",
        section="Results",
        replacement="# Results\n\nNumbers went up.\n",
    )

    assert result.ok and result.value["proposed"] is False
    assert suggestions.pending_for_part(db, part_id) is None
    assert effects.proposed_edit_id is None


def test_write_section_fills_an_empty_section_with_a_revision(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.DRAFTER)

    result = _call(
        registry,
        "write_section",
        section="Methods",
        content="# Methods\n\nThe rig, described.\n",
    )

    assert result.ok and result.value["section"] == "2"
    assert effects.wrote_sections == ["2"]
    body = str(artifacts.get_part(db, part_id)["content"])
    assert "The rig, described." in body
    assert "[TODO: describe the rig]" not in body
    # One revision snapshot per direct write, so history reads as the pass proceeded.
    row = db.execute(
        "select origin, note from artifact_part_revisions where part_id = ? "
        "order by id desc limit 1",
        (part_id,),
    ).fetchone()
    assert row["origin"] == artifacts.GENERATED
    assert "Methods" in str(row["note"])


def test_write_section_refuses_occupied_prose_at_write_time(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.DRAFTER)
    # The student types into the empty section after the registry was built and while
    # the model is thinking - the re-check at write time is what protects it.
    artifacts.set_part_content(
        db,
        part_id,
        BODY.replace("[TODO: describe the rig]", "My own methods paragraph."),
        origin=artifacts.USER_CORRECTED,
        record_revision=False,
    )

    result = _call(
        registry,
        "write_section",
        section="Methods",
        content="# Methods\n\nModel prose that must not land.\n",
    )

    assert result.ok is False and "propose" in result.error.lower()
    assert effects.wrote_sections is None
    assert "My own methods paragraph." in str(artifacts.get_part(db, part_id)["content"])


def test_search_and_documents_answer_from_the_class(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'lab3-handout.pdf', 'p', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    # The document's kind lives on its chunks, one kind per document.
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) values (?, ?, 'Measure the period.', 4, "
        "'homework', 'test', 4)",
        (document_id, class_id),
    )
    db.commit()
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    documents = _call(registry, "list_class_documents")

    assert documents.ok
    assert documents.value["documents"] == [
        {"filename": "lab3-handout.pdf", "kind": "homework", "state": "ready"}
    ]


def test_activity_labels_narrate_in_words(db: sqlite3.Connection, class_id: int) -> None:
    def recorded(name: str, arguments: dict[str, object]) -> RecordedCall:
        return RecordedCall(name=name, arguments=arguments, raw_arguments="", ok=True, result={})

    assert writer_tools.activity_label(recorded("read_section", {"ref": "Methods"})) == (
        'Reading section "Methods"'
    )
    assert (
        writer_tools.activity_label(
            recorded("search_course_material", {"query": "pendulum period"})
        )
        == "Searching the course material"
    )
    assert (
        writer_tools.activity_label(
            recorded("search_web", {"query": "private draft sentence goes here"})
        )
        == "Searching the web"
    )
    assert writer_tools.activity_label(recorded("propose_revision", {"section": "2"})) == (
        'Proposing a revision to "2"'
    )
    # A tool this module has never heard of still narrates as something.
    assert writer_tools.activity_label(recorded("mystery", {})) == "mystery"


def test_activity_entry_carries_tool_label_and_outcome() -> None:
    call = RecordedCall(name="read_outline", arguments={}, raw_arguments="{}", ok=False, result={})

    assert writer_tools.activity_entry(call) == {
        "tool": "read_outline",
        "label": "Reading the outline",
        "ok": False,
    }


def test_start_draft_pass_queues_and_marks_the_effect(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import writer_pipeline

    artifact_id, _ = _draft(db, class_id)
    queued: list[writer_pipeline.PassJob] = []
    monkeypatch.setattr(writer_pipeline, "enqueue", queued.append)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(
        registry,
        "start_draft_pass",
        instruction="  tighten  ",
        section_refs=["2", " "],
        depth="deep",
        pause_at_plan=True,
    )

    assert result.ok and result.value["queued"] is True
    assert effects.pass_started is True
    assert len(queued) == 1
    assert queued[0].artifact_id == artifact_id
    assert queued[0].instruction == "tighten"
    assert queued[0].section_refs == ("2",)
    assert queued[0].depth == "deep"
    assert queued[0].pause_at_plan is True
    assert queued[0].run_id is not None
    # The drafter and reviewer do not get to start passes; the student's chat does.
    drafter, _ = writer_tools.build_registry(db, artifact_id, writer_tools.DRAFTER)
    assert "start_draft_pass" not in drafter


def test_start_review_queues_and_marks_the_effect(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import review_pipeline

    artifact_id, _ = _draft(db, class_id)
    queued: list[review_pipeline.ReviewJob] = []
    monkeypatch.setattr(review_pipeline, "enqueue", queued.append)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(registry, "start_review", depth="standard")

    assert result.ok and result.value["queued"] is True
    assert effects.review_started is True
    assert len(queued) == 1 and queued[0].artifact_id == artifact_id
    assert queued[0].run_id is not None
    stored = db.execute(
        "select writer_job_kind, writer_job_depth from artifacts where id = ?", (artifact_id,)
    ).fetchone()
    assert (stored["writer_job_kind"], stored["writer_job_depth"]) == ("review", "standard")
    # Only the student's chat starts reviews; the reviewer profile runs inside one.
    reviewer, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)
    assert "start_review" not in reviewer


def test_add_comment_resolves_the_quote_and_files_with_its_offset(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    result = _call(
        registry,
        "add_comment",
        body="This claims a result without saying by how much.",
        severity="major",
        quote="Numbers went up.",
    )

    assert result.ok and result.value["filed"] is True
    assert effects.filed_comment_ids == [result.value["comment_id"]]
    [thread] = comments.list_threads(db, part_id, BODY)
    assert thread["severity"] == "major"
    assert thread["hint"] == BODY.index("Numbers went up.")
    assert thread["author"] == comments.REVIEWER


def test_add_comment_keeps_a_hopeless_mismatch_unanchored(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    result = _call(
        registry,
        "add_comment",
        body="A finding drafted from a stale read.",
        severity="minor",
        quote="Numbers went down.",
    )

    assert result.ok and result.value["anchored"] is False
    assert effects.filed_comment_ids == [result.value["comment_id"]]
    from backend.core import comments

    [thread] = comments.list_threads(db, part_id, BODY)
    assert thread["anchor"] is None
    assert thread["orphaned"] == 1


def test_add_comment_fuzzy_matches_inside_the_named_section_and_stores_canonical_text(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    content = (
        "# Introduction\n\nThe cohort was assembled from administrative claims data.\n\n"
        "# Results\n\nThe cohort was assembled from carefully reviewed claims records.\n"
    )
    artifact_id, part_id = _draft(db, class_id, content)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    result = _call(
        registry,
        "add_comment",
        body="Name the source for this cohort definition.",
        severity="major",
        quote="The cohort was assembled from carefully-reviewed claims records.",
        section_ref="Results",
    )

    assert result.ok and result.value["anchored"] is True
    [thread] = comments.list_threads(db, part_id, content)
    assert thread["quote"] == "The cohort was assembled from carefully reviewed claims records"
    assert thread["section_ref"] == "Results"
    assert thread["hint"] == content.index("The cohort was assembled", content.index("# Results"))


def test_add_comment_without_a_quote_is_a_whole_document_finding(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    result = _call(
        registry,
        "add_comment",
        body="There is no section that states the conclusion.",
        severity="note",
    )

    assert result.ok
    [thread] = comments.list_threads(db, part_id, BODY)
    assert thread["quote"] is None
    assert thread["anchor"] is None


def test_reply_to_comment_files_as_the_writer_and_marks_the_effect(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Vague.", severity="major")
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(
        registry,
        "reply_to_comment",
        comment_id=int(root["id"]),
        body="Proposed a tighter opening in the suggestion.",
    )

    assert result.ok and result.value["replied"] is True
    assert effects.replied_to_comments is True
    [thread] = comments.list_threads(db, part_id, BODY)
    replies = thread["replies"]
    assert isinstance(replies, list)
    assert replies[0]["author"] == comments.WRITER
    # Only the student's chat replies; the reviewer files roots, never replies.
    reviewer, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)
    assert "reply_to_comment" not in reviewer


def test_reply_to_comment_refuses_strangers_and_reply_targets(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    other_artifact, other_part = _draft(db, class_id)
    elsewhere = comments.add_comment(db, other_part, comments.REVIEWER, "Other draft.")
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Here.")
    reply = comments.add_reply(db, int(root["id"]), comments.STUDENT, "A reply.")
    registry, effects = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    missing = _call(registry, "reply_to_comment", comment_id=999_999, body="x")
    foreign = _call(registry, "reply_to_comment", comment_id=int(elsewhere["id"]), body="x")
    nested = _call(registry, "reply_to_comment", comment_id=int(reply["id"]), body="x")

    assert missing.ok is False and "read_comments" in missing.error
    assert foreign.ok is False
    assert nested.ok is False and "root" in nested.error
    assert effects.replied_to_comments is False


def test_add_comment_refuses_refiling_the_same_open_finding(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)
    first = _call(
        registry, "add_comment", body="Vague claim.", severity="major", quote="Numbers went up."
    )
    assert first.ok

    # The stutter the live model produced: same passage, same severity, reworded body.
    repeat = _call(
        registry,
        "add_comment",
        body="This claim is vague.",
        severity="major",
        quote="Numbers went up.",
    )
    assert repeat.ok is False and "already" in repeat.error.lower()

    # A different severity on the same passage is a different finding, and resolving
    # the first reopens the passage for a fresh one.
    other = _call(
        registry,
        "add_comment",
        body="Also unsupported.",
        severity="critical",
        quote="Numbers went up.",
    )
    assert other.ok
    comments.set_resolved(db, int(first.value["comment_id"]), True)
    refiled = _call(
        registry,
        "add_comment",
        body="Still vague after the edit.",
        severity="major",
        quote="Numbers went up.",
    )
    assert refiled.ok


def test_add_comment_refuses_a_severity_outside_the_scale(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.REVIEWER)

    result = _call(registry, "add_comment", body="x", severity="fatal")

    assert result.ok is False and "critical" in result.error


def test_read_comments_reports_open_threads_with_their_replies(
    db: sqlite3.Connection, class_id: int
) -> None:
    from backend.core import comments

    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Needs a source.",
        severity="major",
        quote="Numbers went up.",
    )
    comments.add_reply(db, int(root["id"]), comments.STUDENT, "Which source would fit?")
    settled = comments.add_comment(db, part_id, comments.REVIEWER, "Old finding.")
    comments.set_resolved(db, int(settled["id"]), True)
    registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.CHAT)

    result = _call(registry, "read_comments")

    assert result.ok
    [thread] = result.value["comments"]
    assert thread["severity"] == "major"
    assert thread["quote"] == "Numbers went up."
    assert thread["replies"] == [{"author": "student", "body": "Which source would fit?"}]


# ---------------------------------------------------------------------------
# write_section vs autosave concurrency (PLA-308 round 5)
# ---------------------------------------------------------------------------


class _BeginBarrier:
    """Wraps a connection to run a callback before the first BEGIN IMMEDIATE.

    This is the deterministic seam for the concurrency tests: the callback
    commits a student autosave on a *separate* connection, simulating the
    race window between entering ``write_section`` and acquiring the write
    lock. Because the callback fires synchronously before ``begin immediate``
    executes, the ordering is fully deterministic with no timing sleeps.
    """

    def __init__(self, real: sqlite3.Connection, before_lock):
        self._real = real
        self._callback = before_lock
        self._armed = True

    def execute(self, sql, parameters=()):
        if self._armed and "begin" in sql.lower():
            self._armed = False
            self._callback()
        return self._real.execute(sql, parameters)

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def __getattr__(self, name):
        return getattr(self._real, name)


def _attempt_id(db: sqlite3.Connection, class_id: int, part_id: int) -> int:
    """Create a writer session, user message, and planned attempt for ownership tests."""
    session = sessions.create_session(db, class_id, artifact_part_id=part_id, mode=sessions.WRITER)
    msg_id = int(
        db.execute(
            "insert into messages (session_id, role, content) values (?, 'user', 'Fill it')",
            (int(session["id"]),),
        ).lastrowid
        or 0
    )
    db.commit()
    attempt = writer_attempts.create_attempt(
        db,
        session_id=int(session["id"]),
        user_message_id=msg_id,
        intent="draft",
    )
    db.commit()
    return attempt


class TestWriteSectionAutosaveRace:
    """Regression tests for the stale-read / whole-body overwrite race between
    ``write_section`` and the draft autosave endpoint.

    Each test uses two genuine SQLite connections to the same on-disk database
    so the locking and WAL visibility semantics match production exactly.
    ``_BeginBarrier`` injects the autosave at the precise instant between entering
    the tool and acquiring the write lock, with no timing sleeps.
    """

    def test_autosave_before_writer_lock_preserves_student_edit(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """A student autosave that commits before the writer's BEGIN IMMEDIATE
        is visible under the lock: the student's edit elsewhere in the document
        survives byte-for-byte, the empty target section is filled, and
        content_version advances for both writes."""
        artifact_id, part_id = _draft(db, class_id)
        attempt = _attempt_id(db, class_id, part_id)
        v0 = int(artifacts.get_part(db, part_id)["content_version"])

        conn2 = connect()
        try:
            edited_intro = BODY.replace(
                "Opening prose the student wrote.",
                "Opening prose the student revised while AI was thinking.",
            )

            def autosave_on_conn2():
                artifacts.compare_and_set_part_content(
                    conn2,
                    part_id,
                    edited_intro,
                    artifacts.USER_CORRECTED,
                    expected_version=v0,
                )

            proxy = _BeginBarrier(db, autosave_on_conn2)
            registry, effects = writer_tools.build_registry(
                proxy, artifact_id, writer_tools.DRAFTER
            )
            effects.attempt_id = attempt

            result = _call(
                registry,
                "write_section",
                section="Methods",
                content="# Methods\n\nThe rig, described.\n",
            )

            assert result.ok
            assert result.value["section"] == "2"
            body = str(artifacts.get_part(db, part_id)["content"])
            assert "the student revised while AI was thinking" in body
            assert "The rig, described." in body
            assert "[TODO: describe the rig]" not in body

            version = int(artifacts.get_part(db, part_id)["content_version"])
            assert version == v0 + 2

            targets = db.execute(
                "select * from writer_attempt_targets "
                "where attempt_id = ? and target_kind = 'section_write'",
                (attempt,),
            ).fetchall()
            assert len(targets) == 1
        finally:
            conn2.close()

    def test_autosave_fills_section_before_writer_lock(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """If a student autosave fills the previously empty target section before
        the writer acquires the lock, write_section returns the occupied-section
        failure and the student's content remains untouched."""
        artifact_id, part_id = _draft(db, class_id)
        attempt = _attempt_id(db, class_id, part_id)
        v0 = int(artifacts.get_part(db, part_id)["content_version"])

        conn2 = connect()
        try:
            student_methods = BODY.replace(
                "[TODO: describe the rig]", "Student's own methods paragraph."
            )

            def autosave_fills_section():
                artifacts.compare_and_set_part_content(
                    conn2,
                    part_id,
                    student_methods,
                    artifacts.USER_CORRECTED,
                    expected_version=v0,
                )

            proxy = _BeginBarrier(db, autosave_fills_section)
            registry, effects = writer_tools.build_registry(
                proxy, artifact_id, writer_tools.DRAFTER
            )
            effects.attempt_id = attempt

            result = _call(
                registry,
                "write_section",
                section="Methods",
                content="# Methods\n\nAI prose that must not land.\n",
            )

            assert result.ok is False
            assert "propose" in result.error.lower()
            body = str(artifacts.get_part(db, part_id)["content"])
            assert "Student's own methods paragraph." in body
            assert "AI prose that must not land" not in body

            version = int(artifacts.get_part(db, part_id)["content_version"])
            assert version == v0 + 1

            targets = db.execute(
                "select * from writer_attempt_targets "
                "where attempt_id = ? and target_kind = 'section_write'",
                (attempt,),
            ).fetchall()
            assert len(targets) == 0
        finally:
            conn2.close()

    def test_stale_autosave_after_writer_gets_stale_content_error(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """When write_section wins the lock and commits generated content, a
        stale autosave built on the pre-writer version gets StaleContentError
        and the generated content is not overwritten."""
        artifact_id, part_id = _draft(db, class_id)
        v0 = int(artifacts.get_part(db, part_id)["content_version"])

        registry, _ = writer_tools.build_registry(db, artifact_id, writer_tools.DRAFTER)
        result = _call(
            registry,
            "write_section",
            section="Methods",
            content="# Methods\n\nGenerated methods content.\n",
        )
        assert result.ok

        conn2 = connect()
        try:
            stale_body = BODY.replace(
                "Opening prose the student wrote.",
                "Student edit built on pre-writer snapshot.",
            )
            with pytest.raises(errors.StaleContentError):
                artifacts.compare_and_set_part_content(
                    conn2,
                    part_id,
                    stale_body,
                    artifacts.USER_CORRECTED,
                    expected_version=v0,
                )

            body = str(artifacts.get_part(db, part_id)["content"])
            assert "Generated methods content." in body
            assert "pre-writer snapshot" not in body
        finally:
            conn2.close()
