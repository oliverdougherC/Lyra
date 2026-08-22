"""Contract tests for suggestion-mode pending edits.

Every test drives the module against a real draft body part in a temporary database: the
draft is the artifact, the pending edit is the row, and accepts write through
`set_part_content` so revision history is the undo story.
"""

import sqlite3

import pytest

from backend.core import artifacts, suggestions
from backend.core.errors import ConflictError, StaleContentError

BASE = (
    "# Notes on the delta function\n"
    "\n"
    "The delta function is even.\n"
    "Its sifting property picks out x(0).\n"
    "\n"
    "## Scaling\n"
    "\n"
    "Scaling the argument scales the area.\n"
    "This paragraph says more about that.\n"
    "And one more line for good measure.\n"
    "\n"
    "## Convolution\n"
    "\n"
    "Convolution is an integral.\n"
    "It commutes, which matters later.\n"
)

# The proposal changes one line near the top and one near the bottom, so the diff is
# two hunks with context two.
PROPOSED = BASE.replace(
    "The delta function is even.\n", "The delta function is even, delta(t) = delta(-t).\n"
).replace("Convolution is an integral.\n", "Convolution is an integral over all time.\n")


def _draft(db: sqlite3.Connection, class_id: int, content: str = BASE) -> int:
    """A draft artifact with one body part holding `content`. Returns the part id."""
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    return artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )


def _part_content(db: sqlite3.Connection, part_id: int) -> str:
    return str(artifacts.get_part(db, part_id)["content"])


def test_compute_hunks_groups_changes_with_two_lines_of_context() -> None:
    hunks = suggestions.compute_hunks(BASE, PROPOSED)

    assert len(hunks) == 2
    assert all(hunk.hash for hunk in hunks)
    first = hunks[0]
    assert any(line.startswith("-The delta function is even.") for line in first.lines)
    assert any(line.startswith("+The delta function is even, delta(t)") for line in first.lines)
    # Display lines carry no line endings; the raw ones do, for application.
    assert all("\n" not in line for line in first.to_dict()["lines"])


def test_accepting_every_hunk_one_at_a_time_equals_accept_all(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten the wording")
    edit = suggestions.pending_for_part(db, part_id)
    assert edit is not None

    while True:
        current = suggestions.pending_for_part(db, part_id)
        if current is None:
            break
        hunk = current["hunks"][0]
        result = suggestions.accept(
            db, current["id"], hunk={"index": hunk["index"], "hash": hunk["hash"]}
        )
        if result["remaining"] == 0:
            break

    assert _part_content(db, part_id) == PROPOSED
    assert suggestions.pending_for_part(db, part_id) is None


def test_rejecting_one_hunk_leaves_the_rest_intact(db: sqlite3.Connection, class_id: int) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = suggestions.pending_for_part(db, part_id)
    target = edit["hunks"][1]

    result = suggestions.reject(
        db, edit["id"], hunk={"index": target["index"], "hash": target["hash"]}
    )

    assert result["remaining"] == 1
    # The document is never written by a reject.
    assert _part_content(db, part_id) == BASE
    # The surviving proposal holds only the first change.
    suggestions.accept(db, edit["id"])
    expected = BASE.replace(
        "The delta function is even.\n", "The delta function is even, delta(t) = delta(-t).\n"
    )
    assert _part_content(db, part_id) == expected


def test_a_hunk_hash_race_is_a_409(db: sqlite3.Connection, class_id: int) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, None)
    edit = suggestions.pending_for_part(db, part_id)
    stale_echo = {"index": edit["hunks"][0]["index"], "hash": edit["hunks"][0]["hash"]}

    suggestions.accept(db, edit["id"], hunk=stale_echo)
    with pytest.raises(ConflictError, match="changed since it was fetched"):
        suggestions.accept(db, edit["id"], hunk=stale_echo)


def test_a_user_edit_marks_the_edit_stale_and_blocks_plain_accept(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    edit = suggestions.propose(db, part_id, PROPOSED, "Tighten")
    assert edit is not None

    # The user edits exactly the line the first hunk rewrites, so the diff cannot rebase.
    edited = BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)

    stale = suggestions.pending_for_part(db, part_id)
    assert stale["stale"] is True
    assert "base_content" in stale
    with pytest.raises(ConflictError, match="stale"):
        suggestions.accept(db, edit.id)
    with pytest.raises(ConflictError, match="stale"):
        suggestions.reject(db, edit.id, hunk={"index": 0, "hash": "anything"})


def test_force_accept_on_a_stale_edit_replaces_the_document(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    edit = suggestions.propose(db, part_id, PROPOSED, "Tighten")
    assert edit is not None
    edited = BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)

    result = suggestions.accept(db, edit.id, force=True)

    assert result["remaining"] == 0
    assert _part_content(db, part_id) == PROPOSED


def test_an_edit_elsewhere_rebases_the_proposal(db: sqlite3.Connection, class_id: int) -> None:
    """The user edits a region the suggestion does not touch - beyond both hunks' two
    lines of context - and the base becomes the current document with the proposal
    following it, still fresh."""
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")

    edited = BASE.replace("This paragraph says more about that.\n", "This says more.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)

    refreshed = suggestions.pending_for_part(db, part_id)
    assert refreshed["stale"] is False
    assert refreshed["proposed_content"] == PROPOSED.replace(
        "This paragraph says more about that.\n", "This says more.\n"
    )

    suggestions.accept(db, refreshed["id"])
    assert _part_content(db, part_id) == refreshed["proposed_content"]


def test_making_the_proposed_change_by_hand_resolves_the_edit(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")

    artifacts.set_part_content(db, part_id, PROPOSED, origin=artifacts.USER_CORRECTED)

    assert suggestions.pending_for_part(db, part_id) is None


def test_a_later_proposal_coalesces_onto_the_first_base(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Sequential passes stay coherent: the second proposal replaces the proposed
    content, and the base the review anchors to is still the document at first proposal."""
    part_id = _draft(db, class_id)
    first = suggestions.propose(db, part_id, PROPOSED, "First pass")
    second_pass = PROPOSED.replace("## Scaling", "## Time scaling")
    second = suggestions.propose(db, part_id, second_pass, "Second pass")

    assert first is not None and second is not None
    assert second.id == first.id
    assert second.base_content == BASE
    assert second.proposed_content == second_pass
    assert second.note == "Second pass"


def test_an_empty_diff_proposes_nothing(db: sqlite3.Connection, class_id: int) -> None:
    part_id = _draft(db, class_id)

    assert suggestions.propose(db, part_id, BASE, "Nothing to do") is None
    assert suggestions.pending_for_part(db, part_id) is None


def test_accept_with_a_matching_expected_version_lands_and_moves_the_version(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = suggestions.pending_for_part(db, part_id)
    version = artifacts.part_content_version(db, part_id)

    result = suggestions.accept(db, edit["id"], expected_version=version)

    assert result["remaining"] == 0
    assert _part_content(db, part_id) == PROPOSED
    assert artifacts.part_content_version(db, part_id) == version + 1


def test_accept_racing_a_body_change_is_refused_without_mutation(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten")
    edit = suggestions.pending_for_part(db, part_id)
    reviewed_version = artifacts.part_content_version(db, part_id)
    # A second tab moves the body past the version the student reviewed the suggestion at.
    elsewhere = BASE.replace("It commutes, which matters later.\n", "It commutes; note this.\n")
    artifacts.set_part_content(db, part_id, elsewhere, origin=artifacts.USER_CORRECTED)
    moved_version = artifacts.part_content_version(db, part_id)
    assert moved_version != reviewed_version

    with pytest.raises(StaleContentError) as excinfo:
        suggestions.accept(db, edit["id"], expected_version=reviewed_version)

    assert excinfo.value.current_version == moved_version
    # Refused without touching the body or the pending edit: nothing was overwritten.
    assert _part_content(db, part_id) == elsewhere
    assert artifacts.part_content_version(db, part_id) == moved_version
    assert suggestions.pending_for_part(db, part_id) is not None


def test_force_accept_racing_a_body_change_still_conflicts(
    db: sqlite3.Connection, class_id: int
) -> None:
    # A force is relative to the version the student saw: a change that landed after they
    # looked conflicts rather than being silently overwritten.
    part_id = _draft(db, class_id)
    edit = suggestions.propose(db, part_id, PROPOSED, "Tighten")
    assert edit is not None
    reviewed_version = artifacts.part_content_version(db, part_id)
    edited = BASE.replace("The delta function is even.\n", "The delta function is symmetric.\n")
    artifacts.set_part_content(db, part_id, edited, origin=artifacts.USER_CORRECTED)
    moved_version = artifacts.part_content_version(db, part_id)

    with pytest.raises(StaleContentError):
        suggestions.accept(db, edit.id, force=True, expected_version=reviewed_version)

    assert _part_content(db, part_id) == edited
    assert artifacts.part_content_version(db, part_id) == moved_version


def test_every_accept_writes_a_revision_with_the_instruction(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _draft(db, class_id)
    suggestions.propose(db, part_id, PROPOSED, "Tighten the wording")

    suggestions.accept(db, suggestions.pending_for_part(db, part_id)["id"])

    revisions = artifacts.list_revisions(db, part_id)
    assert revisions[0]["origin"] == artifacts.GENERATED
    assert revisions[0]["note"] == "Accepted suggestion: Tighten the wording"
    assert revisions[0]["content"] == PROPOSED
