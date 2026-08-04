"""Contract tests for the solution-set endpoints.

The worker is never started here. `solver.enqueue` is stubbed so creating a solution set
stays a pure write, and the segmentation the gate operates on is written directly. That
keeps every test synchronous and keeps the two concerns apart: the worker's behaviour is
`test_solver.py`, this file is the HTTP surface.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_solutions
from backend.config import settings
from backend.core import artifacts, solver
from backend.core.errors import LyraError
from backend.core.segmentation import SegmentedPart, SegmentedProblem
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record what would have been queued instead of running it."""
    queued: list[int] = []
    monkeypatch.setattr(routes_solutions.solver, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the solutions router."""
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_solutions.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _document(
    db: sqlite3.Connection,
    class_id: int,
    filename: str = "hw4.pdf",
    state: str = "ready",
) -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, '/tmp/x', 'application/pdf', 1, ?)",
        (class_id, filename, state),
    )
    db.commit()
    document_id = int(cursor.lastrowid or 0)
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    Path(settings.text_dir / f"{document_id}.txt").write_text("1. Find x.", encoding="utf-8")
    return document_id


def _at_the_gate(db: sqlite3.Connection, class_id: int, document_id: int, count: int = 2) -> int:
    """An artifact sitting at `awaiting_review` with a proposed problem list."""
    created = artifacts.create_artifact(
        db, class_id, "Problem set 4", [artifacts.SourceSpec(document_id)]
    )
    artifact_id = int(created["id"])
    solver.write_problems(
        db,
        artifact_id,
        [
            SegmentedProblem(
                label=f"Problem {index}",
                number=str(index),
                statement=f"Statement {index}.",
                document_id=document_id,
                page_number=index,
                parts=(SegmentedPart("(a)", "Sketch it."),) if index == 1 else (),
            )
            for index in range(1, count + 1)
        ],
    )
    artifacts.set_problems_total(db, artifact_id, count)
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    return artifact_id


def test_creating_a_solution_set_returns_202_and_queues_it(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list[int]
) -> None:
    document_id = _document(db, class_id)

    response = client.post(
        f"/api/classes/{class_id}/solutions", json={"sources": [{"document_id": document_id}]}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == artifacts.PENDING
    # Named after the file, without its extension, which is what the setup screen prefills.
    assert body["title"] == "hw4"
    assert body["problems_total"] is None
    assert body["sources"][0]["filename"] == "hw4.pdf"
    assert no_worker == [body["id"]]


def test_a_reference_solution_source_is_recorded_with_its_role(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    problems = _document(db, class_id, "hw4.pdf")
    reference = _document(db, class_id, "hw3_solutions.pdf")

    response = client.post(
        f"/api/classes/{class_id}/solutions",
        json={
            "sources": [
                {"document_id": problems, "role": "problem_set"},
                {"document_id": reference, "role": "reference_solutions"},
            ]
        },
    )

    roles = {source["filename"]: source["role"] for source in response.json()["sources"]}
    assert roles == {"hw4.pdf": "problem_set", "hw3_solutions.pdf": "reference_solutions"}


def test_a_document_still_ingesting_is_refused_by_name(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id, "hw4.pdf", state="embedding")

    response = client.post(
        f"/api/classes/{class_id}/solutions", json={"sources": [{"document_id": document_id}]}
    )

    # Refusing here beats accepting the run and failing it a second later, which would
    # look like Lyra broke rather than like the upload is not finished.
    assert response.status_code == 400
    assert "hw4.pdf" in response.json()["detail"]


def test_a_document_from_another_class_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    other = int(db.execute("insert into classes (name) values ('Physics')").lastrowid or 0)
    db.commit()
    foreign = _document(db, other)

    response = client.post(
        f"/api/classes/{class_id}/solutions", json={"sources": [{"document_id": foreign}]}
    )

    assert response.status_code == 404


def test_creating_against_an_unknown_class_is_a_404(client: TestClient) -> None:
    assert client.post("/api/classes/999/solutions", json={"sources": []}).status_code == 422
    assert (
        client.post(
            "/api/classes/999/solutions", json={"sources": [{"document_id": 1}]}
        ).status_code
        == 404
    )


def test_reading_a_solution_set_returns_its_parts_in_document_order(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    body = client.get(f"/api/solutions/{artifact_id}").json()

    assert [part["label"] for part in body["parts"]] == ["Problem 1", "(a)", "Problem 2"]
    assert body["parts"][1]["parent_part_id"] == body["parts"][0]["id"]
    assert body["parts"][0]["provenance"][0]["filename"] == "hw4.pdf"


def test_the_status_endpoint_carries_no_part_content(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    body = client.get(f"/api/solutions/{artifact_id}/status").json()

    # Polled every second or two while a set solves. Shipping every solution body on each
    # poll would make the poll grow with the work.
    assert body["state"] == artifacts.AWAITING_REVIEW
    assert body["problems_total"] == 2
    assert [part["status"] for part in body["parts"]] == ["pending", "pending"]
    assert "content" not in body["parts"][0]


def test_correcting_the_segmentation_replaces_the_list(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    existing = [
        part
        for part in client.get(f"/api/solutions/{artifact_id}").json()["parts"]
        if part["parent_part_id"] is None
    ]

    response = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={
            "problems": [
                {
                    "id": existing[0]["id"],
                    "label": "Problem 1",
                    "statement": "Statement 1 and 2, merged.",
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [part["label"] for part in body["parts"]] == ["Problem 1"]
    assert body["problems_total"] == 1
    # The merged problem kept where it was found: the student edited the wording, not the
    # page it came from.
    assert body["parts"][0]["provenance"][0]["page_number"] == 1


def test_a_problem_the_student_typed_in_carries_no_provenance(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    body = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={"problems": [{"statement": "A problem Lyra missed entirely."}]},
    ).json()

    # New is honest about having no provenance rather than inheriting someone else's.
    assert body["parts"][0]["provenance"] == []
    assert body["parts"][0]["label"] == "Problem 1"


def test_sub_parts_survive_a_correction(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    body = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={
            "problems": [
                {
                    "statement": "Compute the convolution.",
                    "parts": [{"statement": "Sketch it."}, {"label": "(b)", "statement": "Width."}],
                }
            ]
        },
    ).json()

    # An unlabelled sub-part still has a position, and (a), (b) is what the sheet used.
    assert [part["label"] for part in body["parts"]] == ["Problem 1", "(a)", "(b)"]


def test_a_blank_statement_is_rejected(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    response = client.patch(
        f"/api/solutions/{artifact_id}/segmentation", json={"problems": [{"statement": "   "}]}
    )

    assert response.status_code == 422


def test_the_segmentation_cannot_be_rewritten_once_solving_starts(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.SOLVING)

    response = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={"problems": [{"statement": "Too late."}]},
    )

    # Solutions are keyed to the problem list. Rewriting it underneath them would leave
    # answers attached to problems that no longer exist.
    assert response.status_code == 409
    assert response.json()["detail"] == routes_solutions.NOT_AT_GATE_MESSAGE


def test_resegmenting_requeues_from_a_failure(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list[int]
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    artifacts.mark_artifact_failed(db, artifact_id, artifacts.SEGMENTING, "It broke.")

    response = client.post(f"/api/solutions/{artifact_id}/resegment")

    assert response.status_code == 202
    assert response.json()["state"] == artifacts.PENDING
    assert no_worker == [artifact_id]


def test_resegmenting_a_running_set_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.SEGMENTING)

    assert client.post(f"/api/solutions/{artifact_id}/resegment").status_code == 409


def test_cancelling_a_running_set_marks_it_cancelled(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.SEGMENTING)

    response = client.post(f"/api/solutions/{artifact_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == artifacts.CANCELLED


def test_cancelling_at_the_gate_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    response = client.post(f"/api/solutions/{artifact_id}/cancel")

    # Nothing is running to stop. The interface deletes at the gate instead, which is a
    # different act and carries its own confirmation.
    assert response.status_code == 409
    assert response.json()["detail"] == routes_solutions.NOT_RUNNING_MESSAGE


def test_deleting_a_solution_set_takes_its_parts_with_it(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)

    assert client.delete(f"/api/solutions/{artifact_id}").status_code == 204

    assert client.get(f"/api/solutions/{artifact_id}").status_code == 404
    assert db.execute("select count(*) from artifact_parts").fetchone()[0] == 0


def test_listing_is_class_scoped(client: TestClient, db: sqlite3.Connection, class_id: int) -> None:
    other = int(db.execute("insert into classes (name) values ('Physics')").lastrowid or 0)
    db.commit()
    _at_the_gate(db, class_id, _document(db, class_id))
    _at_the_gate(db, other, _document(db, other))

    listed = client.get(f"/api/classes/{class_id}/solutions").json()

    assert len(listed) == 1
    assert listed[0]["class_id"] == class_id


def test_unknown_ids_are_404(client: TestClient) -> None:
    assert client.get("/api/solutions/999").status_code == 404
    assert client.get("/api/solutions/999/status").status_code == 404
    assert client.delete("/api/solutions/999").status_code == 404
    assert client.post("/api/solutions/999/cancel").status_code == 404


def test_only_the_problems_the_student_touched_are_marked_edited(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    existing = [
        part
        for part in client.get(f"/api/solutions/{artifact_id}").json()["parts"]
        if part["parent_part_id"] is None
    ]

    body = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={
            "problems": [
                # Resubmitted exactly as it came back, sub-part included.
                {
                    "id": existing[0]["id"],
                    "label": "Problem 1",
                    "statement": "Statement 1.",
                    "parts": [{"label": "(a)", "statement": "Sketch it."}],
                },
                {"id": existing[1]["id"], "label": "Problem 2", "statement": "Reworded."},
            ]
        },
    ).json()

    roots = [part for part in body["parts"] if part["parent_part_id"] is None]
    # `Edited` means "not verbatim from the sheet". Marking the whole list as corrected
    # because one entry changed would put that badge on problems nobody touched.
    assert [part["origin"] for part in roots] == ["generated", "user_corrected"]


def test_a_reworded_sub_part_marks_its_problem_edited(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _at_the_gate(db, class_id, document_id)
    existing = [
        part
        for part in client.get(f"/api/solutions/{artifact_id}").json()["parts"]
        if part["parent_part_id"] is None
    ]

    body = client.patch(
        f"/api/solutions/{artifact_id}/segmentation",
        json={
            "problems": [
                {
                    "id": existing[0]["id"],
                    "label": "Problem 1",
                    "statement": "Statement 1.",
                    "parts": [{"label": "(a)", "statement": "Sketch it, carefully."}],
                }
            ]
        },
    ).json()

    assert body["parts"][0]["origin"] == "user_corrected"


# --- Starting, correcting, and re-solving ------------------------------------------


@pytest.fixture(autouse=True)
def no_solve_worker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object, object]]:
    """Record solve and regenerate jobs instead of running them."""
    jobs: list[tuple[str, object, object]] = []
    monkeypatch.setattr(
        routes_solutions.solver,
        "enqueue_solve",
        lambda artifact_id: jobs.append(("solve", artifact_id, "")),
    )
    monkeypatch.setattr(
        routes_solutions.solver,
        "enqueue_regenerate",
        lambda artifact_id, part_id, correction="": jobs.append(
            ("regenerate", part_id, correction)
        ),
    )
    return jobs


def _solved(db: sqlite3.Connection, class_id: int, document_id: int) -> tuple[int, int, int]:
    """A finished set: one problem with one step, checked. Returns (artifact, problem, step)."""
    artifact_id = _at_the_gate(db, class_id, document_id, count=1)
    problem_id = int(
        next(
            part for part in artifacts.list_parts(db, artifact_id) if part["parent_part_id"] is None
        )["id"]
    )
    step_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.STEP,
        0,
        label="Set it up",
        content="Apply the definition.",
        parent_part_id=problem_id,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_part_status(db, problem_id, artifacts.PART_COMPLETE)
    artifacts.set_part_verdict(db, problem_id, artifacts.VERIFIED, "The integral matches.")
    artifacts.record_checks(
        db, problem_id, [artifacts.CheckEntry(tool="cas_integrate", ok=True, result='{"ok":true}')]
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    return artifact_id, problem_id, step_id


def test_start_confirms_the_gate_and_queues_the_solve(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    no_solve_worker: list[tuple[str, object, object]],
) -> None:
    artifact_id = _at_the_gate(db, class_id, _document(db, class_id))

    response = client.post(f"/api/solutions/{artifact_id}/start")

    assert response.status_code == 202
    assert response.json()["state"] == artifacts.PENDING
    assert no_solve_worker == [("solve", artifact_id, "")]


def test_start_is_refused_while_a_run_is_already_going(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _at_the_gate(db, class_id, _document(db, class_id))
    artifacts.set_artifact_state(db, artifact_id, artifacts.SOLVING)

    assert client.post(f"/api/solutions/{artifact_id}/start").status_code == 409


def test_start_refuses_a_set_with_nothing_in_it(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    created = artifacts.create_artifact(db, class_id, "Empty", [artifacts.SourceSpec(document_id)])
    artifacts.set_artifact_state(db, int(created["id"]), artifacts.AWAITING_REVIEW)

    response = client.post(f"/api/solutions/{created['id']}/start")

    assert response.status_code == 400
    assert "no problems" in response.json()["detail"].lower()


def test_a_stopped_run_can_be_started_again(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    no_solve_worker: list[tuple[str, object, object]],
) -> None:
    """`Solve the rest` is this endpoint, not a second one with its own resume logic."""
    artifact_id = _at_the_gate(db, class_id, _document(db, class_id))
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)

    assert client.post(f"/api/solutions/{artifact_id}/start").status_code == 202
    assert no_solve_worker == [("solve", artifact_id, "")]


def test_a_solution_carries_its_verdict_detail_and_the_checks_behind_it(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, problem_id, _ = _solved(db, class_id, _document(db, class_id))

    problem = next(
        part
        for part in client.get(f"/api/solutions/{artifact_id}").json()["parts"]
        if part["id"] == problem_id
    )

    assert problem["verdict"] == "verified"
    assert problem["verdict_detail"] == "The integral matches."
    # The audit trail is the reason to believe the badge, so it travels with it.
    assert [check["tool"] for check in problem["checks"]] == ["cas_integrate"]


def test_editing_a_step_stores_a_revision_and_drops_the_stale_check(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, problem_id, step_id = _solved(db, class_id, _document(db, class_id))
    artifacts.set_part_verdict(db, step_id, artifacts.VERIFIED, "Checked.")

    body = client.patch(
        f"/api/solutions/{artifact_id}/parts/{step_id}",
        json={"content": "Apply the definition, then integrate."},
    ).json()

    assert body["content"] == "Apply the definition, then integrate."
    assert body["origin"] == "user_corrected"
    # Whatever checking concluded, it concluded it about text that is no longer there.
    assert body["verdict"] == "unchecked"
    assert [
        entry["content"]
        for entry in client.get(f"/api/solutions/{artifact_id}/parts/{step_id}/revisions").json()
    ] == [
        "Apply the definition, then integrate.",
        "Apply the definition.",
    ]


def test_a_part_belonging_to_another_set_is_not_editable_through_this_one(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    first, _, step_id = _solved(db, class_id, document_id)
    other = _at_the_gate(db, class_id, document_id, count=1)

    response = client.patch(
        f"/api/solutions/{other}/parts/{step_id}", json={"content": "Not mine."}
    )

    assert response.status_code == 404
    assert first != other


def test_regenerate_queues_the_problem_with_the_correction(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    no_solve_worker: list[tuple[str, object, object]],
) -> None:
    artifact_id, problem_id, _ = _solved(db, class_id, _document(db, class_id))

    response = client.post(
        f"/api/solutions/{artifact_id}/parts/{problem_id}/regenerate",
        json={"correction": "You dropped a factor of two."},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "solving"
    assert no_solve_worker == [("regenerate", problem_id, "You dropped a factor of two.")]


def test_only_a_whole_problem_can_be_re_solved(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    # A step re-derived without the ones after it would leave a solution that no longer
    # follows from itself.
    artifact_id, _, step_id = _solved(db, class_id, _document(db, class_id))

    response = client.post(
        f"/api/solutions/{artifact_id}/parts/{step_id}/regenerate", json={"correction": ""}
    )

    assert response.status_code == 400


def test_restoring_an_earlier_version_is_itself_a_revision(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _, step_id = _solved(db, class_id, _document(db, class_id))
    client.patch(f"/api/solutions/{artifact_id}/parts/{step_id}", json={"content": "My version."})

    body = client.post(
        f"/api/solutions/{artifact_id}/parts/{step_id}/restore", json={"revision": 1}
    ).json()

    assert body["content"] == "Apply the definition."
    revisions = client.get(f"/api/solutions/{artifact_id}/parts/{step_id}/revisions").json()
    # Restoring rewinds the content, never the history: what was there in between is still
    # readable, so restoring is itself undoable.
    assert [entry["content"] for entry in revisions] == [
        "Apply the definition.",
        "My version.",
        "Apply the definition.",
    ]
    assert revisions[0]["note"] == "Restored version 1."
