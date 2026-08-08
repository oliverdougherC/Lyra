"""The brief lifecycle: proposed by the assistant, confirmed only by the student.

The contract under test is propose-never-assert at the row level: a re-proposal over a
confirmed brief demotes it, and confirmation is an explicit, separate gesture.
"""

import sqlite3

import pytest

from backend.core import artifacts, briefs
from backend.core.errors import NotFoundError


def _draft(db: sqlite3.Connection, class_id: int) -> int:
    """A draft artifact. Returns the artifact id."""
    created = artifacts.create_artifact(db, class_id, "Lab 3", [], kind=artifacts.KIND_DRAFT)
    artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content="",
        status=artifacts.PART_COMPLETE,
    )
    return int(created["id"])


def _document(db: sqlite3.Connection, class_id: int) -> int:
    """A minimal ready document row, for the source cross-reference."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'lab3-handout.pdf', 'lab3-handout.pdf', 'application/pdf', 1, 'ready')",
        (class_id,),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def test_a_draft_starts_with_no_brief(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)

    assert briefs.get_brief(db, draft_id) is None


def test_save_proposes_and_get_reads_back(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)

    saved = briefs.save_brief(
        db,
        draft_id,
        assignment_type="lab report",
        summary="Measure the pendulum period against length.",
        audience="the TA",
        length_target="5 pages",
    )

    assert saved["status"] == briefs.PROPOSED
    assert saved["assignment_type"] == "lab report"
    assert briefs.get_brief(db, draft_id) == saved


def test_save_records_the_handout_it_was_discerned_from(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    document_id = _document(db, class_id)

    saved = briefs.save_brief(
        db, draft_id, summary="From the handout.", source_document_id=document_id
    )

    assert saved["source_document_id"] == document_id


def test_deleting_the_source_document_leaves_the_brief(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    document_id = _document(db, class_id)
    briefs.save_brief(db, draft_id, summary="From the handout.", source_document_id=document_id)

    db.execute("delete from documents where id = ?", (document_id,))
    db.commit()

    brief = briefs.get_brief(db, draft_id)
    assert brief is not None
    assert brief["source_document_id"] is None
    assert brief["summary"] == "From the handout."


def test_confirm_flips_the_status(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)
    briefs.save_brief(db, draft_id, summary="An essay on the delta function.")

    confirmed = briefs.confirm_brief(db, draft_id)

    assert confirmed["status"] == briefs.CONFIRMED


def test_a_resave_demotes_a_confirmed_brief_back_to_proposed(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    briefs.save_brief(db, draft_id, summary="First guess.")
    briefs.confirm_brief(db, draft_id)

    resaved = briefs.save_brief(db, draft_id, summary="A changed guess.")

    assert resaved["status"] == briefs.PROPOSED
    assert resaved["summary"] == "A changed guess."


def test_the_students_own_edit_may_save_confirmed_directly(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)

    saved = briefs.save_brief(db, draft_id, summary="My essay.", status=briefs.CONFIRMED)

    assert saved["status"] == briefs.CONFIRMED


def test_a_resave_preserves_created_at_and_advances_updated_at(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    first = briefs.save_brief(db, draft_id, summary="First.")
    # datetime('now') has one-second resolution, so nudge the stored row into the past
    # rather than sleeping through a real second.
    db.execute(
        "update draft_briefs set created_at = datetime('now', '-1 hour'), "
        "updated_at = datetime('now', '-1 hour') where artifact_id = ?",
        (draft_id,),
    )
    db.commit()
    aged = briefs.get_brief(db, draft_id)
    assert aged is not None

    second = briefs.save_brief(db, draft_id, summary="Second.")

    assert second["created_at"] == aged["created_at"]
    assert second["updated_at"] != aged["updated_at"]
    assert first["summary"] == "First."


def test_fields_are_stripped_on_save(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)

    saved = briefs.save_brief(db, draft_id, assignment_type="  essay  ", summary=" x ")

    assert saved["assignment_type"] == "essay"
    assert saved["summary"] == "x"


def test_save_refuses_a_missing_artifact_and_a_non_draft(
    db: sqlite3.Connection, class_id: int
) -> None:
    with pytest.raises(NotFoundError):
        briefs.save_brief(db, 999_999, summary="x")

    # A non-draft artifact, made by re-kinding a draft: creating a real solution set here
    # would drag in its source-document requirements, which are not what is under test.
    other = _draft(db, class_id)
    db.execute("update artifacts set kind = ? where id = ?", (artifacts.KIND_QUIZ, other))
    db.commit()
    with pytest.raises(NotFoundError):
        briefs.save_brief(db, other, summary="x")


def test_save_refuses_a_source_document_that_does_not_exist(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)

    with pytest.raises(NotFoundError):
        briefs.save_brief(db, draft_id, summary="x", source_document_id=999_999)


def test_save_refuses_a_status_outside_the_lifecycle(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)

    with pytest.raises(ValueError, match="status"):
        briefs.save_brief(db, draft_id, summary="x", status="settled")


def test_confirm_without_a_brief_is_not_found(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)

    with pytest.raises(NotFoundError):
        briefs.confirm_brief(db, draft_id)


def test_delete_drops_the_brief_and_tolerates_absence(
    db: sqlite3.Connection, class_id: int
) -> None:
    draft_id = _draft(db, class_id)
    briefs.save_brief(db, draft_id, summary="x")

    briefs.delete_brief(db, draft_id)
    briefs.delete_brief(db, draft_id)  # Deleting a guess that is not there is not an error.

    assert briefs.get_brief(db, draft_id) is None


def test_deleting_the_draft_cascades_to_its_brief(db: sqlite3.Connection, class_id: int) -> None:
    draft_id = _draft(db, class_id)
    briefs.save_brief(db, draft_id, summary="x")

    artifacts.delete_artifact(db, draft_id)

    row = db.execute(
        "select artifact_id from draft_briefs where artifact_id = ?", (draft_id,)
    ).fetchone()
    assert row is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # Pages are how assignments are actually quoted, and the reason a five-page ask
        # used to produce three paragraphs: nothing turned this into a number.
        ("5 pages", 2000),
        ("1 page", 400),
        ("about 3 pages", 1200),
        # Words are taken at face value, commas and all.
        ("1,500 words", 1500),
        ("750 words", 750),
        # A range is written to its middle. Writing to the bottom of a range is exactly
        # the failure mode this whole budget exists to stop.
        ("5-7 pages", 2400),
        ("1500 to 2000 words", 1750),
        # Double spacing halves what fits on a page.
        ("5 pages, double-spaced", 1250),
        ("4 pages double spaced", 1000),
        # A bare small number is a page count nobody typed the unit for; a bare large
        # one is a word count. Reading "5" as five words would cap the document at
        # nothing at all.
        ("5", 2000),
        ("2000", 2000),
        # Nothing to read.
        ("", None),
        (None, None),
        ("as long as it needs to be", None),
    ],
)
def test_length_targets_become_word_counts(target: str | None, expected: int | None) -> None:
    assert briefs.length_target_words(target) == expected


def test_a_free_text_instruction_needs_its_unit_spelled_out() -> None:
    """The pass instruction is arbitrary prose, and a stray number is not a length.

    "tighten section 2" states no length; reading its 2 as a page count would silently
    rewrite the whole pass to a 800-word target the student never asked for.
    """
    assert briefs.length_target_words("tighten section 2", require_unit=True) is None
    assert briefs.length_target_words("make it flow better", require_unit=True) is None
    # An explicit unit still comes through, which is the point of reading it at all -
    # including the way people actually ask, which is in words rather than digits.
    assert briefs.length_target_words("write me five pages", require_unit=True) == 2000
    assert briefs.length_target_words("write about 5 pages on this", require_unit=True) == 2000
    assert briefs.length_target_words("expand to 1200 words", require_unit=True) == 1200
    assert briefs.length_target_words("a three page essay", require_unit=True) == 1200
