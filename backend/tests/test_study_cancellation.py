"""Deterministic two-connection races at study write boundaries (PLA-472)."""

import sqlite3
import threading
from collections.abc import Callable
from typing import Any

import pytest

from backend.api import routes_study
from backend.core import artifacts, study
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.storage.database import connect


def _job(db: sqlite3.Connection, class_id: int, kind: str) -> study._Job:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'notes.txt', '/tmp/notes', 'text/plain', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()
    artifact = artifacts.create_artifact(
        db,
        class_id,
        "Study",
        [artifacts.SourceSpec(document_id, artifacts.STUDY_SOURCE)],
        kind=kind,
    )
    job = study._Job(int(artifact["id"]), (document_id,), cards_per_topic=1, count=1)
    study.persist_job(db, job, kind)
    return job


def _complete(conn: sqlite3.Connection, job: study._Job, kind: str) -> None:
    if kind == artifacts.KIND_FLASHCARD_DECK:
        study._complete_deck(conn, job, [("Topic", [study._ProposedCard("F", "B", ())])])
    else:
        study._complete_quiz(conn, job, [{"topic": "Topic", "question": "Q"}], list(job.source_ids))


def _after_checkpoint(
    job: study._Job,
    action: Callable[[sqlite3.Connection], Any],
    intervening: Callable[[], Any],
) -> Any:
    """Pause the worker after its read, commit the competing request, then resume."""
    checked = threading.Event()
    released = threading.Event()
    results: list[Any] = []

    def worker() -> None:
        conn = connect()
        try:
            study._raise_if_cancelled(conn, job.artifact_id)
            checked.set()
            assert released.wait(5)
            results.append(action(conn))
        except Exception as exc:
            results.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert checked.wait(5)
        intervening()
    finally:
        released.set()
        thread.join(5)
    assert not thread.is_alive()
    return results[0]


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
@pytest.mark.parametrize(
    "boundary",
    ["reading", "mapping", "topic", "writing", "progress", "increment", "complete", "failure"],
)
def test_cancel_between_checkpoint_and_write_is_terminal(db, class_id, kind, boundary):
    job = _job(db, class_id, kind)
    study._set_stage(db, job.artifact_id, "Reading the material")
    partial = artifacts.create_part(db, job.artifact_id, artifacts.CARD, 1, content="partial")
    before = artifacts.get_artifact(db, job.artifact_id)

    def action(conn):
        if boundary == "complete":
            return _complete(conn, job, kind)
        if boundary == "failure":
            return study._mark_failed(conn, job.artifact_id, LyraError("late failure"))
        if boundary == "progress":
            return study._set_progress(conn, job.artifact_id, 10, 2)
        if boundary == "increment":
            return study._increment_progress(conn, job.artifact_id)
        return study._set_stage(conn, job.artifact_id, boundary)

    result = _after_checkpoint(job, action, lambda: routes_study._cancel_study(before, db))
    assert (
        result is False
        if boundary == "failure"
        else isinstance(result, study._GenerationCancelledError)
    )
    settled = artifacts.get_artifact(db, job.artifact_id)
    assert settled["state"] == artifacts.CANCELLED
    assert settled["problems_done"] == before["problems_done"]
    assert settled["problems_total"] == before["problems_total"]
    assert [p["id"] for p in artifacts.list_parts(db, job.artifact_id)] == [partial]
    # Duplicate cancellation and restart preserve the same settled artifact.
    assert routes_study._cancel_study(before, db)["state"] == artifacts.CANCELLED
    assert study.reconcile_interrupted(db) == (0, 0)


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
def test_completion_wins_over_cancel_from_stale_snapshot(db, class_id, kind):
    job = _job(db, class_id, kind)
    study._set_stage(db, job.artifact_id, "Generating")
    stale = artifacts.get_artifact(db, job.artifact_id)
    other = connect()
    try:
        _complete(other, job, kind)
    finally:
        other.close()
    with pytest.raises(ConflictError, match="ready"):
        routes_study._cancel_study(stale, db)
    assert not study._mark_failed(db, job.artifact_id, LyraError("late"))
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, job.artifact_id)) == 1
    study.run_generation(job)  # a duplicated queue delivery cannot regenerate ready content
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, job.artifact_id)) == 1


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
def test_cancel_queued_start_and_restart(db, class_id, kind, monkeypatch):
    job = _job(db, class_id, kind)
    result = _after_checkpoint(
        job,
        lambda conn: study._set_stage(conn, job.artifact_id, "Reading"),
        lambda: routes_study._cancel_study(artifacts.get_artifact(db, job.artifact_id), db),
    )
    assert isinstance(result, study._GenerationCancelledError)
    monkeypatch.setattr(study, "_resolve_config", lambda *_: pytest.fail("cancelled work started"))
    study.run_generation(job)
    assert study.reconcile_interrupted(db) == (0, 0)


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
def test_deletion_between_checkpoint_and_persistence_leaves_no_rows(db, class_id, kind):
    job = _job(db, class_id, kind)
    study._set_stage(db, job.artifact_id, "Generating")
    result = _after_checkpoint(
        job,
        lambda conn: _complete(conn, job, kind),
        lambda: artifacts.delete_artifact(db, job.artifact_id),
    )
    assert isinstance(result, NotFoundError)
    study.run_generation(job)
    assert not study._mark_failed(db, job.artifact_id, LyraError("late"))
    assert study.reconcile_interrupted(db) == (0, 0)
    assert db.execute("select count(*) from artifact_parts").fetchone()[0] == 0
    assert db.execute("select count(*) from study_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
def test_persistence_failure_rolls_back_every_child_row(db, class_id, kind, monkeypatch):
    job = _job(db, class_id, kind)
    study._set_stage(db, job.artifact_id, "Generating")
    original = artifacts.create_part

    def fail_after_part(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("persistence fault")

    monkeypatch.setattr(artifacts, "create_part", fail_after_part)
    with pytest.raises(RuntimeError, match="persistence fault"):
        _complete(db, job, kind)
    assert study._mark_failed(db, job.artifact_id, LyraError("persistence fault"))
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.FAILED
    for table in (
        "artifact_parts",
        "artifact_part_revisions",
        "artifact_provenance",
        "card_states",
    ):
        assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0  # noqa: S608


def test_cancel_after_recovery_scan_is_not_reset(db, class_id, monkeypatch):
    job = _job(db, class_id, artifacts.KIND_FLASHCARD_DECK)
    study._set_stage(db, job.artifact_id, "Generating")
    original = study._job_from_row

    def cancel_during_reconstruction(row):
        other = connect()
        try:
            routes_study._cancel_study(artifacts.get_artifact(other, job.artifact_id), other)
        finally:
            other.close()
        return original(row)

    monkeypatch.setattr(study, "_job_from_row", cancel_during_reconstruction)
    monkeypatch.setattr(study, "enqueue", lambda *_: pytest.fail("cancelled job was requeued"))
    assert study.reconcile_interrupted(db) == (0, 0)
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.CANCELLED


@pytest.mark.parametrize("kind", study.STUDY_KINDS)
def test_cancel_contending_with_persistence_observes_one_complete_commit(
    db, class_id, kind, monkeypatch
):
    job = _job(db, class_id, kind)
    study._set_stage(db, job.artifact_id, "Generating")
    stale = artifacts.get_artifact(db, job.artifact_id)
    inserted = threading.Event()
    cancel_started = threading.Event()
    release = threading.Event()
    outcomes = []
    original = artifacts.create_part

    def paused_insert(*args, **kwargs):
        part_id = original(*args, **kwargs)
        inserted.set()
        assert release.wait(5)
        return part_id

    def finish():
        conn = connect()
        try:
            _complete(conn, job, kind)
            outcomes.append("ready")
        except Exception as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    def cancel():
        conn = connect()
        conn.set_trace_callback(
            lambda sql: cancel_started.set() if sql.lower() == "begin immediate" else None
        )
        try:
            outcomes.append(routes_study._cancel_study(stale, conn))
        except Exception as exc:
            outcomes.append(exc)
        finally:
            conn.close()

    monkeypatch.setattr(artifacts, "create_part", paused_insert)
    writer = threading.Thread(target=finish)
    canceller = threading.Thread(target=cancel)
    writer.start()
    try:
        assert inserted.wait(5)
        # Even the first part/revision is invisible until the complete artifact commits.
        assert artifacts.list_parts(db, job.artifact_id) == []
        assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.GENERATING
        canceller.start()
        assert cancel_started.wait(5)
    finally:
        release.set()
        writer.join(5)
        if canceller.ident is not None:
            canceller.join(5)
    assert not writer.is_alive() and not canceller.is_alive()
    assert "ready" in outcomes
    assert any(isinstance(value, ConflictError) for value in outcomes)
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, job.artifact_id)) == 1


def test_failure_cleanup_and_state_are_one_commit(db, class_id):
    job = _job(db, class_id, artifacts.KIND_FLASHCARD_DECK)
    study._set_stage(db, job.artifact_id, "Generating")
    artifacts.create_part(db, job.artifact_id, artifacts.CARD, 1, content="partial")
    cleanup_started = threading.Event()
    release = threading.Event()
    outcomes = []

    def fail():
        conn = connect()

        def pause_before_delete(sql):
            if sql.lower().startswith("delete from artifact_parts"):
                cleanup_started.set()
                assert release.wait(5)

        conn.set_trace_callback(pause_before_delete)
        try:
            outcomes.append(study._mark_failed(conn, job.artifact_id, LyraError("failed")))
        finally:
            conn.close()

    worker = threading.Thread(target=fail)
    worker.start()
    try:
        assert cleanup_started.wait(5)
        assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.GENERATING
        assert len(artifacts.list_parts(db, job.artifact_id)) == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [True]
    assert artifacts.get_artifact(db, job.artifact_id)["state"] == artifacts.FAILED
    assert artifacts.list_parts(db, job.artifact_id) == []
