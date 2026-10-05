"""Regression coverage for daily-use request admission and interrupted creation."""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_classes, routes_drafts, routes_settings, routes_solutions
from backend.core import artifacts, solver
from backend.core.errors import ConflictError
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def api(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes_classes.router)
    app.include_router(routes_settings.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("field", ["context_window", "extraction_enabled", "remote_ack"])
def test_required_setting_null_is_rejected_before_any_write(
    api: TestClient, db: sqlite3.Connection, field: str
) -> None:
    before = dict(db.execute("select * from settings where id = 1").fetchone())
    response = api.put("/api/settings", json={field: None, "model": "must-not-save"})
    assert response.status_code == 422
    assert dict(db.execute("select * from settings where id = 1").fetchone()) == before


def test_null_class_archive_flag_is_a_validation_error(
    api: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    response = api.patch(f"/api/classes/{class_id}", json={"archived": None, "name": "Changed"})
    assert response.status_code == 422
    assert (
        db.execute("select name from classes where id = ?", (class_id,)).fetchone()[0] != "Changed"
    )


def _solution(db: sqlite3.Connection, class_id: int) -> int:
    document_id = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'synthetic.pdf', '/unused', 'application/pdf', 1, 'ready')",
        (class_id,),
    ).lastrowid
    db.commit()
    return int(
        artifacts.create_artifact(db, class_id, "Synthetic", [artifacts.SourceSpec(document_id)])[
            "id"
        ]
    )


def test_cancelled_queued_segmentation_does_not_restart(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _solution(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)
    calls: list[int] = []
    monkeypatch.setattr(
        solver, "_document_text", lambda document_id: calls.append(document_id) or ""
    )
    solver.run_segmentation(artifact_id)
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.CANCELLED
    assert calls == []


def test_cancelled_queued_solve_does_not_fail_configuration(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _solution(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)
    solver.run_solve(artifact_id)
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.CANCELLED


def test_resegment_refuses_already_queued_job(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _solution(db, class_id)
    queued: list[int] = []
    monkeypatch.setattr(solver, "enqueue", queued.append)
    with pytest.raises(ConflictError):
        routes_solutions.resegment_solution(artifact_id, db)
    assert queued == []


def test_failed_draft_creation_does_not_leave_a_broken_draft(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_part(*args: object, **kwargs: object) -> int:
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(artifacts, "create_part", fail_part)
    with pytest.raises(RuntimeError, match="injected write failure"):
        routes_drafts.create_draft(class_id, routes_drafts.DraftCreate(title="Synthetic"), db)
    assert db.execute("select count(*) from artifacts").fetchone()[0] == 0
    assert not db.in_transaction


@pytest.mark.parametrize("operation", ["start", "resegment"])
def test_concurrent_solution_requests_admit_only_one_job(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    artifact_id = _solution(db, class_id)
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Synthetic problem")
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    queued: list[int] = []
    monkeypatch.setattr(solver, "enqueue", queued.append)
    monkeypatch.setattr(solver, "enqueue_solve", queued.append)
    first_read = Event()
    second_read = Event()
    require_solution = routes_solutions._require_solution_set

    def pause_after_read(conn: sqlite3.Connection, target_id: int) -> dict[str, object]:
        result = require_solution(conn, target_id)
        if not first_read.is_set():
            first_read.set()
            # Without a reserved write lock, the second caller reads the same state
            # during this pause and both enqueue. With the lock it waits for commit.
            second_read.wait(timeout=0.3)
        else:
            second_read.set()
        return result

    monkeypatch.setattr(routes_solutions, "_require_solution_set", pause_after_read)
    handler = (
        routes_solutions.start_solution
        if operation == "start"
        else routes_solutions.resegment_solution
    )

    def request() -> str:
        conn = connect()
        try:
            handler(artifact_id, conn)
            return "accepted"
        except ConflictError:
            return "conflict"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        assert first_read.wait(timeout=2)
        second = pool.submit(request)
        assert sorted([first.result(timeout=5), second.result(timeout=5)]) == [
            "accepted",
            "conflict",
        ]
    assert queued == [artifact_id]


def test_segmentation_write_failure_preserves_original_parts_and_history(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _solution(db, class_id)
    original_id = artifacts.create_part(
        db, artifact_id, artifacts.PROBLEM, 0, content="Original corrected problem"
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    original_parts = artifacts.list_parts(db, artifact_id)
    original_revisions = artifacts.list_revisions(db, original_id)
    create_part = artifacts.create_part
    created = 0

    def fail_second_part(*args: object, **kwargs: object) -> int:
        nonlocal created
        created += 1
        if created == 2:
            raise RuntimeError("injected replacement failure")
        return create_part(*args, **kwargs)

    monkeypatch.setattr(artifacts, "create_part", fail_second_part)
    payload = routes_solutions.SegmentationUpdate(
        problems=[
            routes_solutions.ProblemUpdate(statement="Replacement one"),
            routes_solutions.ProblemUpdate(statement="Replacement two"),
        ]
    )
    with pytest.raises(RuntimeError, match="injected replacement failure"):
        routes_solutions.update_segmentation(artifact_id, payload, db)
    assert artifacts.list_parts(db, artifact_id) == original_parts
    assert artifacts.list_revisions(db, original_id) == original_revisions
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.AWAITING_REVIEW
    assert not db.in_transaction


@pytest.mark.parametrize("first_kind", [solver.SEGMENT, solver.SOLVE])
def test_cancel_then_different_job_never_dispatches_obsolete_queued_work(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, first_kind: str
) -> None:
    import queue

    jobs = queue.Queue()
    monkeypatch.setattr(solver, "_queue", jobs)
    monkeypatch.setattr(solver, "_queued_jobs", {})
    artifact_id = _solution(db, class_id)
    first_enqueue, next_enqueue = (
        (solver.enqueue, solver.enqueue_solve)
        if first_kind == solver.SEGMENT
        else (solver.enqueue_solve, solver.enqueue)
    )
    next_kind = solver.SOLVE if first_kind == solver.SEGMENT else solver.SEGMENT
    called: list[tuple[str, int]] = []
    monkeypatch.setattr(
        solver, "run_segmentation", lambda value, **kwargs: called.append((solver.SEGMENT, value))
    )
    monkeypatch.setattr(
        solver, "run_solve", lambda value, **kwargs: called.append((solver.SOLVE, value))
    )
    first_enqueue(artifact_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)
    artifacts.set_artifact_state(db, artifact_id, artifacts.PENDING)
    next_enqueue(artifact_id)
    solver._run(jobs.get_nowait())
    assert called == []
    solver._run(jobs.get_nowait())
    assert called == [(next_kind, artifact_id)]
    assert solver._queued_jobs == {}


@pytest.mark.parametrize("state", [artifacts.CANCELLED, artifacts.READY, artifacts.SOLVING])
def test_queued_solve_dispatch_requires_pending_state(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import queue

    jobs = queue.Queue()
    monkeypatch.setattr(solver, "_queue", jobs)
    monkeypatch.setattr(solver, "_queued_jobs", {})
    artifact_id = _solution(db, class_id)
    called: list[int] = []
    monkeypatch.setattr(solver, "run_solve", called.append)
    solver.enqueue_solve(artifact_id)
    artifacts.set_artifact_state(db, artifact_id, state)
    solver._run(jobs.get_nowait())
    assert called == []


def test_start_waits_for_gate_replacement_before_capturing_problem_ids(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    artifact_id = _solution(db, class_id)
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Original problem")
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    first_read = Event()
    second_read = Event()
    require_solution = routes_solutions._require_solution_set
    queued_contents: list[str] = []

    def pause_after_read(conn: sqlite3.Connection, target_id: int) -> dict[str, object]:
        result = require_solution(conn, target_id)
        if not first_read.is_set():
            first_read.set()
            second_read.wait(timeout=0.3)
        else:
            second_read.set()
        return result

    def enqueue(target_id: int) -> None:
        conn = connect()
        try:
            queued_contents.extend(
                str(part["content"]) for part in artifacts.list_parts(conn, target_id)
            )
        finally:
            conn.close()

    monkeypatch.setattr(routes_solutions, "_require_solution_set", pause_after_read)
    monkeypatch.setattr(solver, "enqueue_solve", enqueue)

    def correct() -> None:
        conn = connect()
        try:
            routes_solutions.update_segmentation(
                artifact_id,
                routes_solutions.SegmentationUpdate(
                    problems=[routes_solutions.ProblemUpdate(statement="Corrected problem")]
                ),
                conn,
            )
        finally:
            conn.close()

    def start() -> None:
        conn = connect()
        try:
            routes_solutions.start_solution(artifact_id, conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        correction = pool.submit(correct)
        assert first_read.wait(timeout=2)
        started = pool.submit(start)
        correction.result(timeout=5)
        started.result(timeout=5)
    assert queued_contents == ["Corrected problem"]
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.PENDING


def test_worker_replacement_failure_preserves_existing_problem_tree(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.segmentation import SegmentedProblem

    artifact_id = _solution(db, class_id)
    original_id = artifacts.create_part(
        db, artifact_id, artifacts.PROBLEM, 0, content="Original corrected problem"
    )
    original_parts = artifacts.list_parts(db, artifact_id)
    original_revisions = artifacts.list_revisions(db, original_id)
    monkeypatch.setattr(solver, "_document_text", lambda document_id: "Synthetic source")
    monkeypatch.setattr(
        solver,
        "propose_problems",
        lambda *args: [SegmentedProblem("Problem 1", "1", "Replacement problem", 0)],
    )

    def fail_figures(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected figure write failure")

    monkeypatch.setattr(solver, "_write_figures", fail_figures)
    solver.run_segmentation(artifact_id)
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.FAILED
    assert artifacts.list_parts(db, artifact_id) == original_parts
    assert artifacts.list_revisions(db, original_id) == original_revisions


@pytest.mark.parametrize("first_kind", [solver.SEGMENT, solver.SOLVE])
def test_cancel_restart_between_queue_claim_and_dispatch_preserves_new_job(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, first_kind: str
) -> None:
    import queue

    jobs = queue.Queue()
    monkeypatch.setattr(solver, "_queue", jobs)
    monkeypatch.setattr(solver, "_queued_jobs", {})
    artifact_id = _solution(db, class_id)
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Original problem")
    first_enqueue = solver.enqueue if first_kind == solver.SEGMENT else solver.enqueue_solve
    original_run = solver.run_segmentation if first_kind == solver.SEGMENT else solver.run_solve
    first_name = "run_segmentation" if first_kind == solver.SEGMENT else "run_solve"

    def restart_then_run(target: int, *, _claimed: bool = False) -> None:
        routes_solutions.cancel_solution(target, db)
        if first_kind == solver.SEGMENT:
            routes_solutions.start_solution(target, db)
        else:
            routes_solutions.resegment_solution(target, db)
        original_run(target, _claimed=_claimed)

    monkeypatch.setattr(solver, first_name, restart_then_run)
    first_enqueue(artifact_id)
    solver._run(jobs.get_nowait())
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.PENDING
    assert jobs.qsize() == 1
    assert artifact_id in solver._queued_jobs


def test_rescheduled_solve_finishes_current_problem_without_consuming_new_run(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.app_settings import TutorAccess, TutorConfig
    from backend.core.solving import SolvedProblem

    artifact_id = _solution(db, class_id)
    first_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="First")
    second_id = artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 1, content="Second")
    artifacts.set_artifact_state(db, artifact_id, artifacts.SOLVING)
    monkeypatch.setattr(
        solver,
        "resolve_tutor_access",
        lambda conn: TutorAccess(TutorConfig("http://127.0.0.1/v1", None, None, 8192), None, False),
    )
    calls: list[int] = []

    def generate(conn: sqlite3.Connection, target: int, *args: object) -> tuple:
        calls.append(target)
        artifacts.set_artifact_state(conn, target, artifacts.CANCELLED)
        artifacts.set_artifact_state(conn, target, artifacts.PENDING)
        return SolvedProblem((), "Synthetic answer"), []

    monkeypatch.setattr(solver, "_generate", generate)
    monkeypatch.setattr(solver, "_check", lambda *args: None)
    solver.run_solve(artifact_id, _claimed=True)
    assert calls == [artifact_id]
    assert artifacts.get_part(db, first_id)["status"] == artifacts.PART_COMPLETE
    assert artifacts.get_part(db, second_id)["status"] == artifacts.PART_PENDING
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.PENDING


@pytest.mark.parametrize("operation", ["create", "start", "resegment"])
def test_queue_identity_is_registered_before_pending_transaction_is_published(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    artifact_id = _solution(db, class_id)
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, content="Synthetic problem")
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    observed: list[object] = []

    def enqueue(target: int) -> None:
        assert db.in_transaction
        reader = connect()
        try:
            observed.append(solver._current_state(reader, target))
        finally:
            reader.close()

    monkeypatch.setattr(solver, "enqueue", enqueue)
    monkeypatch.setattr(solver, "enqueue_solve", enqueue)
    if operation == "create":
        document_id = int(db.execute("select id from documents limit 1").fetchone()[0])
        routes_solutions.create_solution(
            class_id,
            routes_solutions.SolutionCreate(
                sources=[routes_solutions.SourceCreate(document_id=document_id, role="problem_set")]
            ),
            db,
        )
        assert observed == [None]
    else:
        handler = (
            routes_solutions.start_solution
            if operation == "start"
            else routes_solutions.resegment_solution
        )
        handler(artifact_id, db)
        assert observed == [artifacts.AWAITING_REVIEW]
    assert not db.in_transaction


@pytest.mark.parametrize("kind", [solver.SEGMENT, solver.SOLVE])
def test_failed_worker_cannot_fail_a_new_run_queued_during_error_publication(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    artifact_id = _solution(db, class_id)
    state = artifacts.SEGMENTING if kind == solver.SEGMENT else artifacts.SOLVING
    artifacts.set_artifact_state(db, artifact_id, state)
    mark_failed = artifacts.mark_artifact_failed

    def fail_work(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected worker failure")

    def restart_before_failure_write(
        conn: sqlite3.Connection, target: int, stage: str, message: str, **kwargs: object
    ) -> None:
        artifacts.set_artifact_state(db, target, artifacts.CANCELLED)
        artifacts.set_artifact_state(db, target, artifacts.PENDING)
        mark_failed(conn, target, stage, message, **kwargs)

    monkeypatch.setattr(solver, "_segment" if kind == solver.SEGMENT else "_solve", fail_work)
    monkeypatch.setattr(artifacts, "mark_artifact_failed", restart_before_failure_write)
    run = solver.run_segmentation if kind == solver.SEGMENT else solver.run_solve
    run(artifact_id, _claimed=True)
    result = artifacts.get_artifact(db, artifact_id)
    assert result["state"] == artifacts.PENDING
    assert result["error_message"] is None
