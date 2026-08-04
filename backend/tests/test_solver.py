"""Segmentation, the review gate, and the transitions around them.

`run_segmentation` is called directly rather than through the queue, so the tests stay
synchronous and deterministic. The tutor model is faked at the seam
`backend.core.segmentation` reaches it through, so nothing here touches the network.

The gate is what most of this file defends. It is the phase's most expensive design
decision and the one a refactor is most likely to quietly undo: an artifact that advances
past `awaiting_review` on its own has thrown away the whole reason the state exists.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import artifacts, segmentation, solver
from backend.core.app_settings import document_text_allowed
from backend.core.artifacts import SourceSpec
from backend.core.segmentation import SegmentedPart, SegmentedProblem

_HOMEWORK = """\
ECE 203 Homework 4
Due Friday.

1. Find the Laplace transform of a unit ramp.

2. Compute the convolution of two rectangular pulses.
   (a) Sketch the result.
   (b) State its width.

3. Show that the system is time invariant.
"""


@pytest.fixture(autouse=True)
def no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to the chunker-only path.

    The model pass is opt-in per test through `fake_model`, so a test that does not ask
    for it cannot accidentally depend on one.
    """
    monkeypatch.setattr(segmentation, "resolve_tutor_config", _raise_configuration_error)


def _raise_configuration_error(conn: sqlite3.Connection) -> object:
    from backend.core.errors import ConfigurationError

    raise ConfigurationError("No tutor endpoint is configured. Add one in Settings.")


def fake_model(monkeypatch: pytest.MonkeyPatch, reply: object) -> None:
    """Answer the segmentation pass with `reply`, or raise it when it is an exception."""
    from backend.core.app_settings import TutorConfig

    monkeypatch.setattr(
        segmentation,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:8080/v1", None, "m", 8192),
    )
    monkeypatch.setattr(segmentation, "document_text_allowed", lambda conn: None)

    async def complete(*args: object, **kwargs: object) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr(segmentation.client, "complete", complete)


def _document(
    db: sqlite3.Connection,
    class_id: int,
    filename: str = "hw4.pdf",
    text: str = _HOMEWORK,
    state: str = "ready",
) -> int:
    """A ready document with its extracted text on disk, which is what segmentation reads."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, '/tmp/x', 'application/pdf', 1, ?)",
        (class_id, filename, state),
    )
    db.commit()
    document_id = int(cursor.lastrowid or 0)
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    Path(settings.text_dir / f"{document_id}.txt").write_text(text, encoding="utf-8")
    return document_id


def _chunk(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    content: str,
    problem_number: str | None,
    part_index: int | None = None,
    page_number: int | None = 1,
) -> int:
    """One chunk as the chunker would have written it for a homework document."""
    cursor = db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "problem_number, part_index, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, ?, ?, ?, ?, 'homework', 'nomic', 768)",
        (
            document_id,
            class_id,
            content,
            len(content) // 4,
            page_number,
            problem_number,
            part_index,
        ),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _artifact(db: sqlite3.Connection, class_id: int, document_id: int) -> int:
    created = artifacts.create_artifact(db, class_id, "Problem set 4", [SourceSpec(document_id)])
    return int(created["id"])


def _problems(db: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    """Top-level problems only, in document order."""
    return [
        part for part in artifacts.list_parts(db, artifact_id) if part["parent_part_id"] is None
    ]


def test_chunked_problems_reassembles_a_split_problem(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "Header, due Friday.", None)
    _chunk(db, class_id, document_id, "1. First half.", "1", part_index=0)
    _chunk(db, class_id, document_id, "Second half.", "1", part_index=1, page_number=2)
    _chunk(db, class_id, document_id, "2. Another problem.", "2", page_number=2)

    found = segmentation.chunked_problems(db, document_id)

    # Chunks with no problem number are course headers and belong to no problem.
    assert [problem.number for problem in found] == ["1", "2"]
    assert found[0].statement == "1. First half.\n\nSecond half."
    assert found[0].page_number == 1
    assert len(found[0].chunk_ids) == 2


def test_numbering_that_restarts_under_a_heading_is_not_one_problem(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Twelve problems on a real sheet came back as five, and the model could not fix it.

    Sheets restart their numbering under each section heading, and collecting chunks into
    a dictionary keyed by number folded every group into one row carrying three unrelated
    statements. Because the chunker is the spine, a model pass that read the sheet
    correctly was then reconciled back down to the same five.
    """
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "System of LTI systems", None)
    _chunk(db, class_id, document_id, "1. Cascade them.", "1")
    _chunk(db, class_id, document_id, "2. Add them.", "2")
    _chunk(db, class_id, document_id, "1. Find the Fourier series.", "1", page_number=2)
    _chunk(db, class_id, document_id, "2. Find its power.", "2", page_number=2)

    found = segmentation.chunked_problems(db, document_id)

    assert [problem.statement for problem in found] == [
        "1. Cascade them.",
        "2. Add them.",
        "1. Find the Fourier series.",
        "2. Find its power.",
    ]


def test_equal_numbers_are_matched_in_document_order() -> None:
    """A sheet with two problem 1s has two, and each keeps its own model label.

    A lookup keyed by number silently kept only the last entry for a repeated number, so
    the second problem 1 took the first one's label and sub-parts.
    """
    from_chunks = [
        SegmentedProblem("Problem 1", "1", "1. Cascade them.", 7),
        SegmentedProblem("Problem 1", "1", "1. Find the Fourier series.", 7),
    ]
    from_model = [
        SegmentedProblem("Problem 1 (Systems)", "1", "Cascade.", 7),
        SegmentedProblem("Problem 1 (Fourier series)", "1", "Series.", 7),
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert [problem.label for problem in merged] == [
        "Problem 1 (Systems)",
        "Problem 1 (Fourier series)",
    ]
    assert [problem.statement for problem in merged] == [
        "1. Cascade them.",
        "1. Find the Fourier series.",
    ]


def test_a_second_model_problem_of_the_same_number_is_kept() -> None:
    # The chunker saw one problem 1; the model saw that the sheet has two. The extra one
    # is a problem the markers missed, which is what the model pass is for.
    from_chunks = [SegmentedProblem("Problem 1", "1", "1. Cascade them.", 7)]
    from_model = [
        SegmentedProblem("Problem 1 (Systems)", "1", "Cascade.", 7),
        SegmentedProblem("Problem 1 (Fourier series)", "1", "Series.", 7),
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert [problem.statement for problem in merged] == ["1. Cascade them.", "Series."]


def test_the_model_supplies_labels_and_sub_parts_without_rewriting_statements() -> None:
    from_chunks = [
        SegmentedProblem(
            label="Problem 1",
            number="1",
            statement="1. The document's own words.",
            document_id=7,
            page_number=3,
            chunk_ids=(11,),
        )
    ]
    from_model = [
        SegmentedProblem(
            label="Exercise 3.14",
            number="1",
            statement="A paraphrase the model wrote.",
            document_id=7,
            parts=(SegmentedPart("(a)", "Sketch it."),),
        )
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    # The chunker is the spine: its text is the document's, character for character. What
    # the model adds is the sheet's own wording for the label, and the sub-parts.
    assert merged[0].statement == "1. The document's own words."
    assert merged[0].label == "Exercise 3.14"
    assert merged[0].parts[0].label == "(a)"
    assert merged[0].chunk_ids == (11,)
    assert merged[0].page_number == 3


def test_a_transcription_into_latex_replaces_the_flattened_extraction() -> None:
    """The gate is where a student checks Lyra's reading against their sheet.

    PDF extraction flattens exponents into the line, so the chunker's own text says
    `e−2tu(t −3)` where the sheet says $e^{-2t}u(t-3)$. Keeping it unconditionally meant
    the one screen built for checking mathematics was the one screen printing it wrong.
    """
    from_chunks = [
        SegmentedProblem(
            label="Problem 1",
            number="1",
            statement="Problem 1 (Time Shift)\nStarting pair:\ne−2tu(t) ←→\n1\n2 + jω\n"
            "Find the Fourier Transform of\nx(t) = e−2(t−1)u(t −1)",
            document_id=7,
            chunk_ids=(11,),
        )
    ]
    from_model = [
        SegmentedProblem(
            label="Problem 1 (Time Shift)",
            number="1",
            statement="Starting pair:\n$$e^{-2t}u(t) \\longleftrightarrow \\frac{1}{2 + j\\omega}$$"
            "\n\nFind the Fourier Transform of\n$$x(t) = e^{-2(t-1)}u(t-1)$$",
            document_id=7,
        )
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert "e^{-2t}" in merged[0].statement
    # The citation still points at the chunk the text was found in.
    assert merged[0].chunk_ids == (11,)


def test_a_summary_does_not_replace_the_sheets_own_text() -> None:
    # The rule is not "the model wins". A reading that dropped what the sheet said is a
    # summary, and the student would be confirming a problem their homework does not hold.
    from_chunks = [
        SegmentedProblem(
            label="Problem 1",
            number="1",
            statement="Sketch the magnitude response and mark the corner frequency, "
            "then state the phase at direct current.",
            document_id=7,
        )
    ]
    from_model = [SegmentedProblem("Problem 1", "1", "Sketch the response.", 7)]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert merged[0].statement.startswith("Sketch the magnitude response")


def test_the_label_counts_as_part_of_the_models_reading() -> None:
    # The chunker's text starts with the whole heading, because that is where it cut. A
    # faithful reading puts those words in the label, so comparing against the statement
    # alone read every titled problem as a summary and printed the flattened text.
    from_chunks = [
        SegmentedProblem(
            label="Problem 1",
            number="1",
            statement="Problem 1 (Time Shift + Differentiation)\nStarting pair:\ne−2tu(t) ←→\n1\n"
            "2 + jω\nFind the Fourier Transform of\nx(t) = e−2(t−1)u(t −1)",
            document_id=7,
        )
    ]
    from_model = [
        SegmentedProblem(
            label="Problem 1 (Time Shift + Differentiation)",
            number="1",
            statement="Starting pair:\n$$e^{-2t}u(t) \\longleftrightarrow \\frac{1}{2 + j\\omega}$$"
            "\n\nFind the Fourier Transform of\n$$x(t) = e^{-2(t-1)}u(t-1)$$",
            document_id=7,
        )
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert merged[0].statement.startswith("Starting pair:\n$$e^{-2t}")


def test_sub_parts_count_as_part_of_the_models_reading() -> None:
    # The prompt asks for sub-parts separately, so the model's statement is the lead-in
    # alone. Comparing against it by itself read every problem with parts as a summary.
    from_chunks = [
        SegmentedProblem(
            label="Problem 1",
            number="1",
            statement="Q1.\nCompute X(jw) of the following signals:\n(a)\nx(t) = e−2tu(t)\n"
            "(b)\nx(t) = te−4tu(t)",
            document_id=7,
        )
    ]
    from_model = [
        SegmentedProblem(
            label="Q1",
            number="1",
            statement="Compute $X(j\\omega)$ of the following signals:",
            document_id=7,
            parts=(
                SegmentedPart("(a)", "$x(t) = e^{-2t}u(t)$"),
                SegmentedPart("(b)", "$x(t) = te^{-4t}u(t)$"),
            ),
        )
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert merged[0].statement == "Compute $X(j\\omega)$ of the following signals:"


def test_a_model_reading_by_section_does_not_merge_with_one_by_problem() -> None:
    """The two lists can describe the sheet at different granularities.

    A real set runs 1 to 3 and 1 to 4 under two headings. The markers see seven problems;
    the model saw two, each with the rest as sub-parts. Matching those by number annotated
    problem 2 of the first heading with the second heading, and then appended every
    question the model had folded into a sub-part as its own row, so the gate listed the
    same questions twice.
    """
    from_chunks = [
        SegmentedProblem(f"Problem {n}", str(n), f"{n}. Question {n}.", 7) for n in "123"
    ]
    from_chunks += [
        SegmentedProblem(f"Problem {n}", str(n), f"{n}. Later question.", 7) for n in "12"
    ]
    from_model = [
        SegmentedProblem(
            "System of LTI systems",
            "1",
            "For each system below:",
            7,
            parts=tuple(SegmentedPart(f"({n})", f"Question {n}.") for n in "abc"),
        ),
        SegmentedProblem(
            "Fourier series",
            "2",
            "Find the series for:",
            7,
            parts=(SegmentedPart("(a)", "Later."), SegmentedPart("(b)", "Later.")),
        ),
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert merged == from_chunks


def test_an_ordinary_disagreement_is_still_merged() -> None:
    # The guard above must not fire on the common case: the model finding one problem the
    # markers missed, or missing one they found, is a disagreement about the same reading.
    from_chunks = [SegmentedProblem(f"Problem {n}", str(n), f"{n}. Question.", 7) for n in "123"]
    from_model = [SegmentedProblem("Problem 1 (bonus)", "1", "Question.", 7)]

    merged = segmentation.reconcile(from_chunks, from_model)

    assert [problem.label for problem in merged] == ["Problem 1 (bonus)", "Problem 2", "Problem 3"]


def test_a_problem_the_regex_missed_is_added() -> None:
    from_chunks = [SegmentedProblem("Problem 1", "1", "1. Found.", 7)]
    from_model = [
        SegmentedProblem("Problem 1", "1", "1. Found.", 7),
        SegmentedProblem("Exercise 3.14", "3.14", "Missed by the marker.", 7),
    ]

    merged = segmentation.reconcile(from_chunks, from_model)

    # Catching what a regex cannot is the entire reason the model pass exists.
    assert [problem.number for problem in merged] == ["1", "3.14"]


def test_with_no_chunk_markers_the_model_list_stands_alone() -> None:
    from_model = [SegmentedProblem("Exercise A", "1", "Only the model saw this.", 7)]

    assert segmentation.reconcile([], from_model) == from_model


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        '{"problems": "not a list"}',
        "{}",
        '```json\n{"problems": []}\n```',
    ],
)
def test_an_unreadable_reply_contributes_nothing_rather_than_failing(reply: str) -> None:
    assert segmentation.parse_segmentation(reply, document_id=7) == []


def test_a_fenced_reply_is_read(db: sqlite3.Connection) -> None:
    fenced = (
        '```json\n{"problems": [{"label": "Problem 1", "statement": "Find x.", "page": 2}]}\n```'
    )

    problems = segmentation.parse_segmentation(fenced, document_id=7)

    assert problems[0].label == "Problem 1"
    assert problems[0].page_number == 2


def test_segmentation_lands_at_the_gate_and_stops(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "1. Find the transform.", "1")
    _chunk(db, class_id, document_id, "2. Compute the convolution.", "2")
    artifact_id = _artifact(db, class_id, document_id)

    solver.run_segmentation(artifact_id)

    artifact = artifacts.get_artifact(db, artifact_id)
    # Not `solving`. The whole point of the gate is that nothing expensive starts until a
    # person has looked at the list.
    assert artifact["state"] == artifacts.AWAITING_REVIEW
    assert artifact["problems_total"] == 2
    assert artifact["problems_done"] == 0
    assert [part["label"] for part in _problems(db, artifact_id)] == ["Problem 1", "Problem 2"]


def test_sub_parts_are_nested_problems_not_steps(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "2. Compute the convolution.", "2")
    artifact_id = _artifact(db, class_id, document_id)
    fake_model(
        monkeypatch,
        {
            "problems": [
                {
                    "label": "Problem 2",
                    "number": "2",
                    "statement": "Compute the convolution.",
                    "parts": [
                        {"label": "(a)", "statement": "Sketch the result."},
                        {"label": "(b)", "statement": "State its width."},
                    ],
                }
            ]
        },
    )

    solver.run_segmentation(artifact_id)

    parts = artifacts.list_parts(db, artifact_id)
    children = [part for part in parts if part["parent_part_id"] is not None]
    # A sub-part is something to be solved, which is what a problem is. A step is a line
    # of the solution and does not exist yet.
    assert [part["kind"] for part in children] == [artifacts.PROBLEM, artifacts.PROBLEM]
    assert [part["label"] for part in children] == ["(a)", "(b)"]
    # `problems_total` counts what gets solved, and a problem is solved with its parts.
    assert artifacts.get_artifact(db, artifact_id)["problems_total"] == 1


def test_problem_provenance_points_back_at_its_chunks(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    chunk_id = _chunk(db, class_id, document_id, "1. Find the transform.", "1", page_number=2)
    artifact_id = _artifact(db, class_id, document_id)

    solver.run_segmentation(artifact_id)

    provenance = artifacts.list_provenance(db, int(_problems(db, artifact_id)[0]["id"]))
    assert [entry["chunk_id"] for entry in provenance] == [chunk_id]
    assert provenance[0]["page_number"] == 2
    assert provenance[0]["filename"] == "hw4.pdf"


def test_a_failed_model_pass_falls_back_to_the_chunker(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "1. Find the transform.", "1")
    artifact_id = _artifact(db, class_id, document_id)
    fake_model(monkeypatch, RuntimeError("the endpoint fell over"))

    solver.run_segmentation(artifact_id)

    # A segmentation nobody could improve on is still one the student can correct at the
    # gate. Failing the run instead would cost them the upload.
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.AWAITING_REVIEW
    assert artifact["problems_total"] == 1


def test_a_document_with_no_problems_is_a_real_outcome(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id, text="Just some prose with no numbering.")
    artifact_id = _artifact(db, class_id, document_id)

    solver.run_segmentation(artifact_id)

    # Some documents are prose. That is an empty list at the gate, not an error.
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.AWAITING_REVIEW
    assert artifact["problems_total"] == 0
    assert artifact["error_message"] is None


def test_losing_every_source_fails_with_a_message(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    artifact_id = _artifact(db, class_id, document_id)
    db.execute("delete from documents where id = ?", (document_id,))
    db.commit()

    solver.run_segmentation(artifact_id)

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == solver.NO_SOURCES
    assert artifact["stage_detail"] == artifacts.SEGMENTING


def test_a_deleted_artifact_is_skipped_rather_than_failed(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _artifact(db, class_id, document_id)
    artifacts.delete_artifact(db, artifact_id)

    # Deleting a queued artifact is an ordinary thing to do and must not raise.
    solver.run_segmentation(artifact_id)


def test_a_run_cancelled_mid_pass_does_not_land_at_the_gate(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "1. Find the transform.", "1")
    artifact_id = _artifact(db, class_id, document_id)

    def cancel_while_reading(conn: sqlite3.Connection, *args: object) -> list[SegmentedProblem]:
        # The student pressed Stop while the model was reading. A separate connection,
        # because that is how it really arrives: from a request, not from this thread.
        from backend.storage.database import connect

        other = connect()
        try:
            artifacts.set_artifact_state(other, artifact_id, artifacts.CANCELLED)
        finally:
            other.close()
        return [SegmentedProblem("Problem 1", "1", "Find the transform.", document_id)]

    monkeypatch.setattr(solver, "propose_problems", cancel_while_reading)

    solver.run_segmentation(artifact_id)

    # Landing them at a gate they walked away from is the opposite of what Stop means.
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    assert _problems(db, artifact_id) == []


def test_reconcile_interrupted_leaves_the_gate_alone(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    working = _artifact(db, class_id, document_id)
    waiting = _artifact(db, class_id, document_id)
    artifacts.set_artifact_state(db, working, artifacts.SEGMENTING)
    artifacts.set_artifact_state(db, waiting, artifacts.AWAITING_REVIEW)

    assert solver.reconcile_interrupted(db) == 1

    # `segmenting` was working when the process died. `awaiting_review` was waiting, and a
    # restart does not change what it is waiting for.
    assert artifacts.get_artifact(db, working)["state"] == artifacts.FAILED
    assert artifacts.get_artifact(db, working)["stage_detail"] == artifacts.SEGMENTING
    assert artifacts.get_artifact(db, waiting)["state"] == artifacts.AWAITING_REVIEW


def test_re_segmenting_replaces_the_list_rather_than_adding_to_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "1. Find the transform.", "1")
    artifact_id = _artifact(db, class_id, document_id)
    solver.run_segmentation(artifact_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.PENDING)

    solver.run_segmentation(artifact_id)

    assert len(_problems(db, artifact_id)) == 1


def test_a_multi_file_set_keeps_each_problem_with_its_own_file(
    db: sqlite3.Connection, class_id: int
) -> None:
    first = _document(db, class_id, "hw4a.pdf")
    second = _document(db, class_id, "hw4b.pdf")
    _chunk(db, class_id, first, "1. From the first file.", "1")
    # Both files number their problems from 1. Renumbering would leave the student
    # translating between the sheet and Lyra on every glance.
    _chunk(db, class_id, second, "1. From the second file.", "1")
    created = artifacts.create_artifact(
        db, class_id, "Problem set 4", [SourceSpec(first), SourceSpec(second)]
    )
    artifact_id = int(created["id"])

    solver.run_segmentation(artifact_id)

    problems = _problems(db, artifact_id)
    assert len(problems) == 2
    sources = [
        artifacts.list_provenance(db, int(problem["id"]))[0]["filename"] for problem in problems
    ]
    assert sources == ["hw4a.pdf", "hw4b.pdf"]


def test_document_text_never_reaches_an_unacknowledged_remote_endpoint(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    _chunk(db, class_id, document_id, "1. Find the transform.", "1")
    artifact_id = _artifact(db, class_id, document_id)
    reached = False

    async def complete(*args: object, **kwargs: object) -> str:
        nonlocal reached
        reached = True
        return "{}"

    from backend.core.app_settings import TutorConfig

    monkeypatch.setattr(
        segmentation,
        "resolve_tutor_config",
        lambda conn: TutorConfig("https://api.example.com/v1", None, "m", 8192),
    )
    monkeypatch.setattr(segmentation.client, "complete", complete)
    db.execute(
        "update settings set endpoint_url = 'https://api.example.com/v1', remote_ack = 0 "
        "where id = 1"
    )
    db.commit()

    solver.run_segmentation(artifact_id)

    # Segmentation sends whole documents to the tutor model exactly as profile extraction
    # does, so it is bound by the same rule from the Inference Posture section of
    # architecture.md. The run still lands at the gate, on chunk markers alone.
    assert reached is False
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.AWAITING_REVIEW
    assert artifact["problems_total"] == 1


def test_acknowledging_a_remote_endpoint_lets_the_model_pass_run(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _artifact(db, class_id, document_id)
    db.execute(
        "update settings set endpoint_url = 'https://api.example.com/v1', remote_ack = 1 "
        "where id = 1"
    )
    db.commit()
    fake_model(
        monkeypatch,
        {"problems": [{"label": "Exercise 3.14", "number": "3.14", "statement": "Prove it."}]},
    )
    # The acknowledgement is the whole difference here, so `fake_model`'s blanket allow is
    # undone and the real rule runs against the settings row written above.
    monkeypatch.setattr(segmentation, "document_text_allowed", document_text_allowed)

    solver.run_segmentation(artifact_id)

    assert [part["label"] for part in _problems(db, artifact_id)] == ["Exercise 3.14"]


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        ("4", "Problem 4"),
        ("4.", "Problem 4"),
        ("", "Problem 4"),
        ("Exercise 3.14", "Exercise 3.14"),
        ("Problem 4 (bonus)", "Problem 4 (bonus)"),
    ],
)
def test_a_label_that_is_only_the_number_falls_back(proposed: str, expected: str) -> None:
    from_chunks = [SegmentedProblem("Problem 4", "4", "4. Find x.", 7)]
    from_model = [SegmentedProblem(proposed, "4", "Find x.", 7)]

    merged = segmentation.reconcile(from_chunks, from_model)

    # A card labelled `4` sitting beside its own position index prints the same digit
    # twice, and tells the reader nothing the sheet did not already number.
    assert merged[0].label == expected
