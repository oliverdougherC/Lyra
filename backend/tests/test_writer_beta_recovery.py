"""HTTP-started durable reviewer fault invariants, with isolated scripted inference.

These are deterministic recovery regressions, not model-quality acceptance evidence.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import artifacts, comments, review_pipeline, writer_pipeline, writer_runs
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.llm.tools import (
    COMPLETED,
    DEPTH,
    OUTPUT_LIMIT,
    TIMEOUT,
    UPSTREAM_FAILED,
    ToolLoopResult,
)
from backend.storage.database import connect, get_db

BODY = (
    "# Introduction\n\nI expected the bus to be empty. My notebook says otherwise.\n\n"
    "# Evidence\n\nThe survey measured twenty journeys, not the whole city.\n"
)


@pytest.fixture
def review_case(db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch, request):
    queued = []
    monkeypatch.setattr(review_pipeline, "enqueue", queued.append)
    monkeypatch.setattr(writer_pipeline, "enqueue", queued.append)
    monkeypatch.setattr(
        review_pipeline,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 8192), None, True
        ),
    )
    artifact = artifacts.create_artifact(
        db, class_id, "Bus observations", [], kind=artifacts.KIND_DRAFT
    )
    artifact_id = int(artifact["id"])
    part_id = artifacts.create_part(
        db, artifact_id, artifacts.DRAFT_BODY, 1, content=BODY, status=artifacts.PART_COMPLETE
    )
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    app = FastAPI()
    app.include_router(routes_drafts.router)

    def request_db() -> Iterator[sqlite3.Connection]:
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = request_db
    with TestClient(app) as client:
        route = (
            "pass"
            if getattr(request.node, "callspec", None)
            and request.node.callspec.params.get("pipeline_name") == "writer_pipeline"
            else "review"
        )
        assert (
            client.post(f"/api/drafts/{artifact_id}/{route}", json={"depth": "quick"}).status_code
            == 202
        )
        assert queued[0].run_id is not None
        yield client, queued, artifact_id, part_id


@pytest.mark.parametrize("stopped", [DEPTH, OUTPUT_LIMIT, TIMEOUT, UPSTREAM_FAILED])
def test_http_review_incomplete_lens_cannot_be_checkpointed_as_complete(
    db, review_case, monkeypatch, stopped
):
    _, queued, artifact_id, part_id = review_case
    calls = 0

    def inference(config, messages, registry, max_depth):
        nonlocal calls
        calls += 1
        if calls == 1:
            result = registry["add_comment"].handler(
                body="Limit this inference to the observed journeys.",
                severity="major",
                quote="The survey measured twenty journeys, not the whole city.",
            )
            assert result.ok
            return ToolLoopResult(content="Partial review", stopped=stopped)
        return ToolLoopResult(content="Done", stopped=COMPLETED)

    monkeypatch.setattr(review_pipeline, "_review_run", inference)
    review_pipeline.run_review(queued[0])
    run = writer_runs.get_run(db, queued[0].run_id)
    assert run["status"] == writer_runs.FAILED
    assert run["checkpoint"]["stage"] != "done"
    assert len(comments.list_threads(db, part_id, BODY)) == 1
    assert artifacts.get_part(db, part_id)["content"] == BODY
    assert "Review complete" not in str(artifacts.get_artifact(db, artifact_id)["stage_detail"])


def test_http_cancelled_review_callback_cannot_file_into_successor(db, review_case, monkeypatch):
    client, queued, artifact_id, part_id = review_case
    late_results = []

    def inference(config, messages, registry, max_depth):
        cancelled = client.post(f"/api/drafts/{artifact_id}/cancel")
        assert cancelled.status_code == 200
        writer_runs.settle_cancellation(db, queued[0].run_id, "Cancelled")
        assert (
            client.post(f"/api/drafts/{artifact_id}/review", json={"depth": "quick"}).status_code
            == 202
        )
        late_results.append(
            registry["add_comment"].handler(
                body="Stale inference finding",
                severity="major",
                quote="I expected the bus to be empty.",
            )
        )
        return ToolLoopResult(content="Done", stopped=COMPLETED)

    monkeypatch.setattr(review_pipeline, "_review_run", inference)
    review_pipeline.run_review(queued[0])
    assert len(late_results) == 1
    assert not late_results[0].ok
    assert comments.list_threads(db, part_id, BODY) == []
    assert writer_runs.get_run(db, queued[1].run_id)["status"] == writer_runs.QUEUED
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.PENDING


def test_http_restart_before_checkpoint_deduplicates_unanchored_finding(
    db, review_case, monkeypatch
):
    _, queued, artifact_id, part_id = review_case
    job = queued[0]
    calls = 0

    class ProcessInterrupted(BaseException):
        pass

    def inference(config, messages, registry, max_depth):
        nonlocal calls
        calls += 1
        if calls <= 2:
            registry["add_comment"].handler(
                body="Explain how the observation method addresses the assignment.",
                severity="major",
            )
        if calls == 1:
            raise ProcessInterrupted()
        return ToolLoopResult(content="Done", stopped=COMPLETED)

    monkeypatch.setattr(review_pipeline, "_review_run", inference)
    with pytest.raises(ProcessInterrupted):
        review_pipeline.run_review(job)
    assert len(comments.list_threads(db, part_id, BODY)) == 1
    writer_runs.queue_for_restart(db, job.run_id, "Synthetic restart at persistence boundary")
    review_pipeline.run_review(writer_runs.build_job(writer_runs.get_run(db, job.run_id)))
    assert len(comments.list_threads(db, part_id, BODY)) == 1
    assert writer_runs.get_run(db, job.run_id)["status"] == writer_runs.COMPLETED
    assert artifacts.get_part(db, part_id)["content"] == BODY


def test_http_restart_after_closing_summary_does_not_duplicate_the_summary(
    db, review_case, monkeypatch
):
    _, queued, artifact_id, part_id = review_case
    monkeypatch.setattr(
        review_pipeline,
        "_review_run",
        lambda *args: ToolLoopResult(content="Done", stopped=COMPLETED),
    )
    original_close = review_pipeline._close

    class ProcessInterrupted(BaseException):
        pass

    def interrupted_close(*args, **kwargs):
        original_close(*args, **kwargs)
        raise ProcessInterrupted()

    monkeypatch.setattr(review_pipeline, "_close", interrupted_close)
    with pytest.raises(ProcessInterrupted):
        review_pipeline.run_review(queued[0])
    monkeypatch.setattr(review_pipeline, "_close", original_close)
    for run in writer_runs.recoverable_runs(db):
        writer_runs.queue_for_restart(db, int(run["id"]), "Synthetic restart after close")
        review_pipeline.run_review(writer_runs.build_job(run))
    assert db.execute("select count(*) from messages").fetchone()[0] == 1
    assert writer_runs.get_run(db, queued[0].run_id)["status"] == writer_runs.COMPLETED


@pytest.mark.parametrize("pipeline_name", ["review_pipeline", "writer_pipeline"])
def test_http_failure_settlement_never_exposes_free_slot_before_artifact_mirror(
    db, review_case, monkeypatch, pipeline_name
):
    from backend.core import writer_pipeline

    _, queued, artifact_id, _ = review_case
    pipeline = review_pipeline if pipeline_name == "review_pipeline" else writer_pipeline
    job = (
        queued[0]
        if pipeline_name == "review_pipeline"
        else writer_pipeline.PassJob(artifact_id, run_id=queued[0].run_id)
    )
    observed = []

    def before_mirror(sql):
        if sql.lower().startswith("update artifacts set state"):
            observer = connect()
            try:
                observed.append(writer_runs.get_run(observer, job.run_id)["status"])
            finally:
                observer.close()

    db.set_trace_callback(before_mirror)
    try:
        pipeline._settle_failed(db, job, RuntimeError("Synthetic provider failure"))
    finally:
        db.set_trace_callback(None)
    assert observed
    assert all(status in (writer_runs.QUEUED, writer_runs.RUNNING) for status in observed)
    assert writer_runs.get_run(db, job.run_id)["status"] == writer_runs.FAILED
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.FAILED


@pytest.mark.parametrize("pipeline_name", ["review_pipeline", "writer_pipeline"])
@pytest.mark.parametrize("settled_status", [writer_runs.FAILED, writer_runs.COMPLETED])
def test_http_old_failure_callback_preserves_a_successor(
    db, review_case, pipeline_name, settled_status
):
    from backend.core import writer_pipeline

    client, queued, artifact_id, _ = review_case
    old_job = queued[0]
    if settled_status == writer_runs.FAILED:
        writer_runs.mark_failed(db, old_job.run_id, "Original failure")
    else:
        writer_runs.mark_completed(db, old_job.run_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.READY)
    assert (
        client.post(f"/api/drafts/{artifact_id}/review", json={"depth": "quick"}).status_code == 202
    )
    before = artifacts.get_artifact(db, artifact_id)
    pipeline = review_pipeline if pipeline_name == "review_pipeline" else writer_pipeline
    job = (
        old_job
        if pipeline_name == "review_pipeline"
        else writer_pipeline.PassJob(artifact_id, run_id=old_job.run_id)
    )
    pipeline._settle_failed(db, job, RuntimeError("Late failure from old inference"))
    after = artifacts.get_artifact(db, artifact_id)
    assert after == before
    assert writer_runs.get_run(db, queued[1].run_id)["status"] == writer_runs.QUEUED
    assert writer_runs.get_run(db, old_job.run_id)["status"] == settled_status


def test_http_reviewer_refuses_oversized_mandatory_context_before_inference(
    db, review_case, monkeypatch
):
    client, queued, artifact_id, part_id = review_case
    from backend.core import briefs

    briefs.save_brief(db, artifact_id, summary="Mandatory assignment constraint. " * 4000)
    monkeypatch.setattr(
        review_pipeline,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 2048), None, True
        ),
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Oversized reviewer context must not reach inference")

    monkeypatch.setattr(review_pipeline, "run_tool_loop", forbidden)
    review_pipeline.run_review(queued[0])
    status = client.get(f"/api/drafts/{artifact_id}/status").json()
    assert status["run_status"] == "failed"
    assert "context window" in status["error_message"]
    assert artifacts.get_part(db, part_id)["content"] == BODY


def test_http_cancel_stops_silent_reviewer_inference(review_case, monkeypatch):
    import asyncio
    import time

    client, queued, artifact_id, _ = review_case
    cleaned = []

    async def silent(*args, **kwargs):
        try:
            assert client.post(f"/api/drafts/{artifact_id}/cancel").status_code == 200
            await asyncio.sleep(1)
            return ToolLoopResult(content="late", calls=(), stopped=COMPLETED)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(review_pipeline, "run_tool_loop", silent)
    started = time.monotonic()
    review_pipeline.run_review(queued[0])
    assert time.monotonic() - started < 0.8
    assert cleaned == [True]
    assert client.get(f"/api/drafts/{artifact_id}/status").json()["run_status"] == "cancelled"


def test_review_with_no_new_comments_does_not_call_standing_findings_clean(
    db, review_case, monkeypatch
):
    client, queued, artifact_id, part_id = review_case
    comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "A prior finding remains open.",
        severity="major",
        quote="The survey measured twenty journeys, not the whole city.",
    )
    monkeypatch.setattr(
        review_pipeline,
        "_review_run",
        lambda *args, **kwargs: ToolLoopResult(
            content="No new comments", calls=(), stopped=COMPLETED
        ),
    )
    review_pipeline.run_review(queued[0])
    status = client.get(f"/api/drafts/{artifact_id}/status").json()
    assert status["run_status"] == "completed"
    assert "earlier comment" in status["stage_detail"]
    assert "no findings" not in status["stage_detail"]
