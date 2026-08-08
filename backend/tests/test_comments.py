"""The comments substrate: quote anchoring and the threaded store.

The anchoring contract is kuhn's, ported: the store is position-dumb, `resolve_quote`
finds a verbatim quote (nearest the hint when repeated), falls back to a
whitespace-normalized scan so reflow does not orphan a finding, and returns None when
the passage is gone - at which point the comment is flagged orphaned, kept, and comes
back the moment the passage does.
"""

import sqlite3

import pytest

from backend.core import artifacts, comments
from backend.core.errors import NotFoundError

DOC = "Intro line.\n\nThe cohort was assembled from claims data.\nMore text follows here.\n"


def _part(db: sqlite3.Connection, class_id: int, body: str = DOC) -> int:
    """A draft's body part holding `body`. Returns the part id."""
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    return artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=body,
        status=artifacts.PART_COMPLETE,
    )


# --- resolve_quote: the anchoring algorithm, kuhn's edge cases kept ---------------


def test_an_exact_quote_resolves_to_its_span() -> None:
    anchor = comments.resolve_quote(DOC, "assembled from claims data")

    assert anchor is not None
    assert anchor.exact is True
    assert DOC[anchor.start : anchor.end] == "assembled from claims data"


def test_an_absent_quote_resolves_to_none() -> None:
    assert comments.resolve_quote(DOC, "randomized controlled trial") is None
    assert comments.resolve_quote(DOC, "") is None
    assert comments.resolve_quote("", "anything") is None


def test_a_repeated_quote_picks_the_occurrence_nearest_the_hint() -> None:
    doc = "alpha beta\nfiller\nalpha beta\n"
    second = doc.rindex("alpha beta")

    near_second = comments.resolve_quote(doc, "alpha beta", hint=second - 2)
    unhinted = comments.resolve_quote(doc, "alpha beta")

    assert near_second is not None and near_second.start == second
    assert unhinted is not None and unhinted.start == 0


def test_reflowed_whitespace_still_resolves_through_the_normalized_fallback() -> None:
    # The stored quote has doubled spaces the document never had; the words match.
    anchor = comments.resolve_quote(DOC, "claims data.  More   text")

    assert anchor is not None
    assert anchor.exact is False
    assert DOC[anchor.start : anchor.end] == "claims data.\nMore text"


def test_case_and_unicode_punctuation_do_not_break_an_anchor() -> None:
    doc = "The evidence—despite its limits—supports the cohort’s conclusion."

    anchor = comments.resolve_quote(
        doc, "the evidence-despite its limits-supports the cohort's conclusion."
    )

    assert anchor is not None and anchor.exact is False
    assert doc[anchor.start : anchor.end] == doc


def test_a_small_copy_error_can_resolve_but_a_short_reversal_cannot() -> None:
    doc = "The cohort was assembled from carefully reviewed administrative claims records."

    close = comments.resolve_quote(
        doc, "The cohort was assembled from carefully reviewed administrative claim records."
    )
    reversed_claim = comments.resolve_quote("Numbers went up.", "Numbers went down.")

    assert close is not None
    assert doc[close.start : close.end] == doc.removesuffix(".")
    assert reversed_claim is None


def test_fuzzy_resolution_can_be_scoped_to_one_section() -> None:
    doc = "First similar passage has enough words here.\nSecond similar passage has enough words."
    second = doc.index("Second")

    anchor = comments.resolve_quote(
        doc,
        "second similar passage has enough word",
        scope_start=second,
        scope_end=len(doc),
    )

    assert anchor is not None and anchor.start >= second


def test_an_edit_above_the_anchor_moves_nothing_the_quote_cannot_absorb() -> None:
    # The hint is stale after an insertion above, but the quote is unique, so it wins.
    edited = "A new opening paragraph.\n\n" + DOC

    anchor = comments.resolve_quote(edited, "assembled from claims data", hint=20)

    assert anchor is not None
    assert edited[anchor.start : anchor.end] == "assembled from claims data"


# --- the store: threads, replies, resolution, orphan maintenance ------------------


def test_a_root_files_with_its_severity_quote_and_hint(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)

    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "This claim needs a source.",
        severity="major",
        quote="assembled from claims data",
        hint=DOC.index("assembled"),
    )

    assert root["parent_id"] is None
    assert root["severity"] == "major"
    assert root["resolved"] == 0
    assert root["orphaned"] == 0


def test_authors_and_severities_outside_the_sets_are_caller_bugs(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)

    with pytest.raises(ValueError, match="author"):
        comments.add_comment(db, part_id, "editor", "x")
    with pytest.raises(ValueError, match="severity"):
        comments.add_comment(db, part_id, comments.REVIEWER, "x", severity="fatal")


def test_replies_nest_under_their_root_and_never_under_a_reply(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Finding.")

    reply = comments.add_reply(db, int(root["id"]), comments.WRITER, "On it.")

    threads = comments.list_threads(db, part_id, DOC)
    assert len(threads) == 1
    replies = threads[0]["replies"]
    assert isinstance(replies, list)
    assert [entry["body"] for entry in replies] == ["On it."]
    with pytest.raises(NotFoundError, match="root"):
        comments.add_reply(db, int(reply["id"]), comments.STUDENT, "nested")


def test_resolution_applies_to_the_root_only(db: sqlite3.Connection, class_id: int) -> None:
    part_id = _part(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Finding.")
    reply = comments.add_reply(db, int(root["id"]), comments.STUDENT, "Fixed.")

    resolved = comments.set_resolved(db, int(root["id"]), True)
    assert resolved["resolved"] == 1
    reopened = comments.set_resolved(db, int(root["id"]), False)
    assert reopened["resolved"] == 0
    with pytest.raises(NotFoundError, match="root"):
        comments.set_resolved(db, int(reply["id"]), True)


def test_listing_anchors_each_root_against_the_body_as_it_stands(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    offset = DOC.index("assembled")
    comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Needs a source.",
        quote="assembled from claims data",
        hint=offset,
    )

    # An edit above the anchor: the stored hint is stale, the anchor is not.
    edited = "A new opening.\n\n" + DOC
    [thread] = comments.list_threads(db, part_id, edited)

    anchor = thread["anchor"]
    assert isinstance(anchor, dict)
    assert edited[int(anchor["start"]) : int(anchor["end"])] == "assembled from claims data"


def test_a_deleted_passage_orphans_its_comment_and_its_return_unorphans_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    root = comments.add_comment(
        db, part_id, comments.REVIEWER, "Finding.", quote="assembled from claims data"
    )

    [orphaned] = comments.list_threads(db, part_id, "The passage is gone.\n")
    assert orphaned["anchor"] is None
    assert orphaned["orphaned"] == 1
    stored = db.execute(
        "select orphaned from draft_comments where id = ?", (root["id"],)
    ).fetchone()
    assert stored["orphaned"] == 1

    [restored] = comments.list_threads(db, part_id, DOC)
    assert restored["orphaned"] == 0
    assert restored["anchor"] is not None


def test_a_quoteless_root_is_unanchored_but_never_orphaned(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    comments.add_comment(
        db, part_id, comments.REVIEWER, "The assignment's second question goes unanswered."
    )

    [thread] = comments.list_threads(db, part_id, "Anything at all.\n")

    assert thread["anchor"] is None
    assert thread["orphaned"] == 0


def test_unresolved_threads_excludes_the_settled_ones(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    settled = comments.add_comment(db, part_id, comments.REVIEWER, "Done already.")
    comments.add_comment(db, part_id, comments.REVIEWER, "Still open.")
    comments.set_resolved(db, int(settled["id"]), True)

    open_threads = comments.unresolved_threads(db, part_id, DOC)

    assert [thread["body"] for thread in open_threads] == ["Still open."]


def test_threads_keep_filing_order_and_scope_to_their_part(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    other_part = _part(db, class_id)
    comments.add_comment(db, part_id, comments.REVIEWER, "First.")
    comments.add_comment(db, part_id, comments.REVIEWER, "Second.")
    comments.add_comment(db, other_part, comments.REVIEWER, "Elsewhere.")

    threads = comments.list_threads(db, part_id, DOC)

    assert [thread["body"] for thread in threads] == ["First.", "Second."]


def test_deleting_the_draft_cascades_roots_and_replies(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _part(db, class_id)
    root = comments.add_comment(db, part_id, comments.REVIEWER, "Finding.")
    comments.add_reply(db, int(root["id"]), comments.STUDENT, "Reply.")
    artifact_id = int(
        db.execute("select artifact_id from artifact_parts where id = ?", (part_id,)).fetchone()[
            "artifact_id"
        ]
    )

    artifacts.delete_artifact(db, artifact_id)

    remaining = db.execute(
        "select count(*) as n from draft_comments where part_id = ?", (part_id,)
    ).fetchone()
    assert remaining["n"] == 0
