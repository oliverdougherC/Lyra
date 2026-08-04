"""Artifact store contracts.

The two that matter most, because they are the ones that would fail silently: a part's
content never diverges from its revision history, and a part never hangs off another
artifact's tree, where `list_parts` would not find it.
"""

import sqlite3
from collections.abc import Callable

import pytest

from backend.core import artifacts
from backend.core.artifacts import ProvenanceEntry, SourceSpec
from backend.core.errors import NotFoundError


def _document(db: sqlite3.Connection, class_id: int, filename: str = "hw4.pdf") -> int:
    """One ready document, which is all an artifact source needs to exist."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
        (class_id, filename),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _chunk(db: sqlite3.Connection, class_id: int, document_id: int) -> int:
    """One chunk, so provenance has something real to point at."""
    cursor = db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) values (?, ?, 'text', 1, 'homework', 'nomic', 768)",
        (document_id, class_id),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _artifact(db: sqlite3.Connection, class_id: int, title: str = "Problem set 4") -> int:
    """An artifact with one problem-set source."""
    document_id = _document(db, class_id)
    created = artifacts.create_artifact(db, class_id, title, [SourceSpec(document_id)])
    return int(created["id"])


def test_create_artifact_attaches_sources_in_order(db: sqlite3.Connection, class_id: int) -> None:
    problems = _document(db, class_id, "hw4.pdf")
    reference = _document(db, class_id, "hw3_solutions.pdf")

    created = artifacts.create_artifact(
        db,
        class_id,
        "  Problem set 4  ",
        [SourceSpec(problems), SourceSpec(reference, artifacts.REFERENCE_SOLUTIONS)],
    )

    assert created["title"] == "Problem set 4"
    assert created["state"] == artifacts.PENDING
    sources = artifacts.list_sources(db, int(created["id"]))
    assert [source["document_id"] for source in sources] == [problems, reference]
    assert [source["ordinal"] for source in sources] == [0, 1]
    only_reference = artifacts.list_sources(db, int(created["id"]), artifacts.REFERENCE_SOLUTIONS)
    assert [source["filename"] for source in only_reference] == ["hw3_solutions.pdf"]


def test_create_artifact_rejects_a_document_from_another_class(
    db: sqlite3.Connection, class_id: int
) -> None:
    other = int(db.execute("insert into classes (name) values ('Physics')").lastrowid or 0)
    db.commit()
    foreign = _document(db, other)

    # Retrieval is partitioned by class. A source from another class would quietly break
    # that partition for every problem in the run.
    with pytest.raises(NotFoundError):
        artifacts.create_artifact(db, class_id, "Problem set 4", [SourceSpec(foreign)])


def test_create_artifact_rejects_an_unknown_document(db: sqlite3.Connection, class_id: int) -> None:
    with pytest.raises(NotFoundError):
        artifacts.create_artifact(db, class_id, "Problem set 4", [SourceSpec(9999)])


def test_create_artifact_rejects_the_same_document_twice(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)

    with pytest.raises(ValueError, match=str(document_id)):
        artifacts.create_artifact(
            db,
            class_id,
            "Problem set 4",
            [SourceSpec(document_id), SourceSpec(document_id, artifacts.REFERENCE_SOLUTIONS)],
        )


def test_create_artifact_requires_something_to_work_on(
    db: sqlite3.Connection, class_id: int
) -> None:
    reference = _document(db, class_id, "hw3_solutions.pdf")

    with pytest.raises(ValueError, match="problem-set"):
        artifacts.create_artifact(
            db,
            class_id,
            "Problem set 4",
            [SourceSpec(reference, artifacts.REFERENCE_SOLUTIONS)],
        )


def test_create_artifact_rejects_a_blank_title(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)

    with pytest.raises(ValueError, match="blank"):
        artifacts.create_artifact(db, class_id, "   ", [SourceSpec(document_id)])


def test_create_artifact_validates_every_source_role(db: sqlite3.Connection, class_id: int) -> None:
    good = _document(db, class_id, "hw4.pdf")
    bad = _document(db, class_id, "notes.pdf")

    # A valid first role must not short-circuit the check on the second.
    with pytest.raises(ValueError, match="lecture_notes"):
        artifacts.create_artifact(
            db, class_id, "Problem set 4", [SourceSpec(good), SourceSpec(bad, "lecture_notes")]
        )


def test_list_artifacts_orders_by_most_recently_changed(
    db: sqlite3.Connection, class_id: int
) -> None:
    first = _artifact(db, class_id, "Problem set 3")
    second = _artifact(db, class_id, "Problem set 4")
    # Written directly because `datetime('now')` has second resolution, and two writes in
    # the same second would make the assertion depend on how fast the test ran.
    db.execute("update artifacts set updated_at = '2026-01-01 00:00:00' where id = ?", (second,))
    db.execute("update artifacts set updated_at = '2026-06-01 00:00:00' where id = ?", (first,))
    db.commit()

    listed = artifacts.list_artifacts(db, class_id)

    assert [row["id"] for row in listed] == [first, second]


def test_delete_artifact_takes_parts_revisions_and_provenance_with_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Find x.")
    artifacts.set_provenance(db, part_id, [ProvenanceEntry(page_number=2)])

    artifacts.delete_artifact(db, artifact_id)

    assert db.execute("select count(*) from artifact_parts").fetchone()[0] == 0
    assert db.execute("select count(*) from artifact_part_revisions").fetchone()[0] == 0
    assert db.execute("select count(*) from artifact_provenance").fetchone()[0] == 0
    assert db.execute("select count(*) from artifact_sources").fetchone()[0] == 0


def test_list_parts_walks_the_tree_in_document_order(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _artifact(db, class_id)
    second = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 1, label="Problem 2")
    first = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, label="Problem 1")
    step_b = artifacts.create_part(
        db, artifact_id, artifacts.STEP, 1, parent_part_id=first, label="Step 2"
    )
    step_a = artifacts.create_part(
        db, artifact_id, artifacts.STEP, 0, parent_part_id=first, label="Step 1"
    )
    # Three levels deep, which is where Phase 3 puts a figure: under the step that
    # references it, not under the problem.
    figure = artifacts.create_part(
        db, artifact_id, artifacts.FIGURE, 0, parent_part_id=step_a, content_type=artifacts.IMAGE
    )

    ordered = [int(part["id"]) for part in artifacts.list_parts(db, artifact_id)]

    assert ordered == [first, step_a, figure, step_b, second]


def test_create_part_rejects_a_parent_from_another_artifact(
    db: sqlite3.Connection, class_id: int
) -> None:
    one = _artifact(db, class_id, "Problem set 3")
    two = _artifact(db, class_id, "Problem set 4")
    foreign_parent = artifacts.create_part(db, one, artifacts.PROBLEM, 0)

    # `list_parts` walks down from an artifact's roots, so this part would exist in the
    # table and be invisible to every reader of either artifact.
    with pytest.raises(ValueError, match="another artifact"):
        artifacts.create_part(db, two, artifacts.STEP, 0, parent_part_id=foreign_parent)


def test_create_part_with_content_records_the_first_revision(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)

    part_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Find x.")

    revisions = artifacts.list_revisions(db, part_id)
    assert [revision["revision"] for revision in revisions] == [1]
    assert revisions[0]["content"] == "Find x."
    assert revisions[0]["origin"] == artifacts.GENERATED


def test_create_part_without_content_records_no_revision(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)

    # Parts are created empty and filled as solving reaches them. An empty part has no
    # content to have a history of.
    part_id = artifacts.create_part(db, artifact_id, artifacts.STEP, 0)

    assert artifacts.list_revisions(db, part_id) == []


def test_set_part_content_keeps_content_and_history_in_step(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.STEP, 0, content="x = 2")

    second = artifacts.set_part_content(
        db, part_id, "x = 3", artifacts.REGENERATED, note="You dropped a sign."
    )
    third = artifacts.set_part_content(db, part_id, "x = 4", artifacts.USER_CORRECTED)

    assert (second, third) == (2, 3)
    part = artifacts.get_part(db, part_id)
    assert part["content"] == "x = 4"
    assert part["origin"] == artifacts.USER_CORRECTED
    revisions = artifacts.list_revisions(db, part_id)
    assert [revision["content"] for revision in revisions] == ["x = 4", "x = 3", "x = 2"]
    assert [revision["origin"] for revision in revisions] == [
        artifacts.USER_CORRECTED,
        artifacts.REGENERATED,
        artifacts.GENERATED,
    ]
    assert revisions[1]["note"] == "You dropped a sign."


def test_set_part_status_clears_a_stale_error(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _artifact(db, class_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0)

    artifacts.set_part_status(db, part_id, artifacts.PART_FAILED, "The model gave up.")
    assert artifacts.get_part(db, part_id)["error_message"] == "The model gave up."

    # A problem that succeeds on a retry must not keep the previous attempt's error
    # sitting beside its new answer.
    artifacts.set_part_status(db, part_id, artifacts.PART_COMPLETE)
    assert artifacts.get_part(db, part_id)["error_message"] is None


def test_a_complete_part_can_be_unchecked(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _artifact(db, class_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Find x.")

    artifacts.set_part_status(db, part_id, artifacts.PART_COMPLETE)

    # The honest reading of a solution produced against an endpoint with no tool support:
    # finished, and not checked. Neither column may stand in for the other.
    part = artifacts.get_part(db, part_id)
    assert part["status"] == artifacts.PART_COMPLETE
    assert part["verdict"] == artifacts.UNCHECKED


def test_set_provenance_replaces_rather_than_appends(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _artifact(db, class_id)
    document_id = _document(db, class_id, "notes.pdf")
    part_id = artifacts.create_part(db, artifact_id, artifacts.STEP, 0, content="x = 2")
    artifacts.set_provenance(db, part_id, [ProvenanceEntry(document_id=document_id, page_number=4)])

    # A regenerated part was informed by what this run retrieved, not by that plus what
    # the last run retrieved.
    artifacts.set_provenance(db, part_id, [ProvenanceEntry(document_id=document_id, page_number=9)])

    provenance = artifacts.list_provenance(db, part_id)
    assert [entry["page_number"] for entry in provenance] == [9]

    artifacts.set_provenance(db, part_id, [])
    assert artifacts.list_provenance(db, part_id) == []


def test_provenance_outlives_the_chunk_and_document_it_points_at(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)
    document_id = _document(db, class_id, "notes.pdf")
    chunk_id = _chunk(db, class_id, document_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.STEP, 0, content="x = 2")
    artifacts.set_provenance(
        db,
        part_id,
        [ProvenanceEntry(chunk_id=chunk_id, document_id=document_id, page_number=4)],
    )

    # A student re-uploading a source must not lose the solution it informed. Losing the
    # citation is the acceptable half of that trade.
    db.execute("delete from documents where id = ?", (document_id,))
    db.commit()

    provenance = artifacts.list_provenance(db, part_id)
    assert len(provenance) == 1
    assert provenance[0]["page_number"] == 4
    assert provenance[0]["chunk_id"] is None
    assert provenance[0]["document_id"] is None
    assert provenance[0]["filename"] is None


def test_delete_parts_clears_the_list_for_a_re_segmentation(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)
    problem = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Find x.")
    artifacts.create_part(db, artifact_id, artifacts.STEP, 0, parent_part_id=problem)

    # Merge and split are not expressible as per-row edits, so the list is replaced whole.
    artifacts.delete_parts(db, artifact_id)

    assert artifacts.list_parts(db, artifact_id) == []
    assert db.execute("select count(*) from artifact_part_revisions").fetchone()[0] == 0


def test_problems_total_is_null_until_segmentation_counts_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)

    # Null and zero are different answers: not counted yet, versus counted and empty.
    assert artifacts.get_artifact(db, artifact_id)["problems_total"] is None

    artifacts.set_problems_total(db, artifact_id, 0)
    assert artifacts.get_artifact(db, artifact_id)["problems_total"] == 0


def test_increment_problems_done_returns_the_running_count(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)

    assert artifacts.increment_problems_done(db, artifact_id) == 1
    assert artifacts.increment_problems_done(db, artifact_id) == 2
    assert artifacts.get_artifact(db, artifact_id)["problems_done"] == 2


def test_writing_a_part_marks_its_artifact_as_changed(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _artifact(db, class_id)
    db.execute(
        "update artifacts set updated_at = '2020-01-01 00:00:00' where id = ?", (artifact_id,)
    )
    db.commit()

    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Find x.")

    # A part landing counts as the artifact changing, which is what puts an in-progress
    # run at the top of the student's list.
    assert artifacts.get_artifact(db, artifact_id)["updated_at"] != "2020-01-01 00:00:00"


@pytest.mark.parametrize(
    ("call", "bad_value"),
    [
        (lambda db, a: artifacts.set_artifact_state(db, a, "thinking"), "thinking"),
        (lambda db, a: artifacts.create_part(db, a, "paragraph", 0), "paragraph"),
        (lambda db, a: artifacts.create_part(db, a, "step", 0, status="done"), "done"),
        (lambda db, a: artifacts.create_part(db, a, "step", 0, origin="borrowed"), "borrowed"),
        (lambda db, a: artifacts.list_sources(db, a, "appendix"), "appendix"),
    ],
)
def test_unknown_values_are_rejected_by_name(
    db: sqlite3.Connection,
    class_id: int,
    call: Callable[[sqlite3.Connection, int], object],
    bad_value: str,
) -> None:
    artifact_id = _artifact(db, class_id)

    # The columns carry the same check constraints. Failing here instead names the value,
    # which an IntegrityError does not.
    with pytest.raises(ValueError, match=bad_value):
        call(db, artifact_id)


def test_unknown_verdict_is_rejected_by_name(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _artifact(db, class_id)
    part_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0)

    with pytest.raises(ValueError, match="probably"):
        artifacts.set_part_verdict(db, part_id, "probably")


def test_missing_rows_raise_not_found(db: sqlite3.Connection) -> None:
    with pytest.raises(NotFoundError):
        artifacts.get_artifact(db, 9999)
    with pytest.raises(NotFoundError):
        artifacts.get_part(db, 9999)
    with pytest.raises(NotFoundError):
        artifacts.delete_artifact(db, 9999)
