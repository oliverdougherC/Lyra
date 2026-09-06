"""Release regressions for retained evidence and durable writer state."""

import pytest

from backend.core import artifacts, source_ledger, writer_runs


@pytest.mark.parametrize("operation", ["replace", "reuse", "refresh"])
def test_historical_excerpt_keeps_supporting_revision(db, class_id, operation):
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Measurement",
        url="https://example.org/data",
        snapshot="The measured value was 10.",
        excerpts=["The measured value was 10."],
    )
    original_revision = source["current_revision_id"]
    refreshed = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Measurement",
        url="https://example.org/data",
        snapshot="The corrected value is 20.",
        **({"excerpts": ["The measured value was 10."]} if operation == "refresh" else {}),
    )
    if operation == "replace":
        source_ledger.replace_excerpts(db, source["id"], ["The measured value was 10."])
    if operation == "reuse":
        source_ledger.add_excerpt(db, source["id"], "The measured value was 10.", section_ref="new")
    assert refreshed["current_revision_id"] != original_revision
    for excerpt in source_ledger.get_source(db, source["id"])["excerpts"]:
        assert excerpt["source_revision_id"] == original_revision
        snapshot = db.execute(
            "select snapshot from writer_source_revisions where id = ?",
            (excerpt["source_revision_id"],),
        ).fetchone()[0]
        assert excerpt["excerpt"] in snapshot


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
def test_writer_terminal_states_cannot_be_resurrected(db, class_id, terminal):
    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="2026-09-05")
    db.commit()
    writer_runs._finish(db, run["id"], terminal)
    writer_runs.mark_running(db, run["id"])
    writer_runs.mark_completed(db, run["id"])
    writer_runs.mark_failed(db, run["id"], "late transport error")
    writer_runs.request_cancel(db, artifact["id"])
    assert writer_runs.get_run(db, run["id"])["status"] == terminal


def test_cancellation_wins_over_late_failure(db, class_id):
    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="2026-09-05")
    db.commit()
    writer_runs.request_cancel(db, artifact["id"])
    writer_runs.mark_failed(db, run["id"], "late transport error")
    assert writer_runs.get_run(db, run["id"])["status"] == "cancelled"


@pytest.mark.parametrize(
    "finish,done,outcome",
    [
        ("length", True, "length"),
        (None, False, "unknown"),
        ("stop", False, None),
        ("stop", True, None),
    ],
)
async def test_stream_terminal_contract(finish, done, outcome):
    import json

    import httpx

    from backend.llm import client

    body = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "Useful text"}, "finish_reason": finish}]})
        + "\n"
    )
    if done:
        body += "data: [DONE]\n"
    received = []

    async def consume():
        async for delta in client.stream_chat(
            "http://127.0.0.1/v1",
            None,
            "m",
            [],
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
        ):
            received.append(delta.text)

    if outcome:
        with pytest.raises(client.StreamCompletionError) as caught:
            await consume()
        assert caught.value.outcome == outcome
    else:
        await consume()
    assert "".join(received) == "Useful text"


@pytest.mark.parametrize("mode", ["silent", "forever"])
async def test_wall_clock_deadline_closes_inflight_inference(mode):
    import asyncio
    import time

    from backend.core import writer_pipeline
    from backend.core.errors import LyraError

    closed = []

    async def operation():
        try:
            while True:  # noqa: ASYNC110 - simulate a provider that never completes.
                await asyncio.sleep(0.005 if mode == "forever" else 100)
        finally:
            closed.append(True)

    start = time.monotonic()
    with pytest.raises(LyraError, match="time budget"):
        await writer_pipeline._bounded_inference(operation(), deadline=start + 0.03)
    assert time.monotonic() - start < 0.3
    assert closed == [True]


async def test_cancellation_interrupts_silent_inference(db, class_id):
    import asyncio
    import time

    from backend.core import writer_pipeline
    from backend.core.errors import LyraError

    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="2026-09-05")
    db.commit()
    closed = []

    async def operation():
        try:
            writer_runs.request_cancel(db, artifact["id"])
            await asyncio.sleep(100)
        finally:
            closed.append(True)

    with pytest.raises(LyraError, match="cancelled"):
        await writer_pipeline._bounded_inference(
            operation(),
            deadline=time.monotonic() + 1,
            run=(db, writer_pipeline.PassJob(artifact["id"], run_id=run["id"])),
        )
    assert closed == [True]


def test_complete_preflights_assembled_input_before_upstream(monkeypatch):
    from backend.core import writer_pipeline
    from backend.core.app_settings import TutorConfig
    from backend.core.errors import LyraError

    async def forbidden(*args, **kwargs):
        pytest.fail("Oversized writing must not leave this machine")

    monkeypatch.setattr(writer_pipeline.client, "complete", forbidden)
    with pytest.raises(LyraError, match="context window"):
        writer_pipeline._complete(
            TutorConfig("http://127.0.0.1/v1", None, "m", 2048),
            [{"role": "user", "content": "Student draft " * 5000}],
        )


@pytest.mark.parametrize(
    "pipeline_name,job_name,runner_name",
    [
        ("writer_pipeline", "PassJob", "run_pass"),
        ("review_pipeline", "ReviewJob", "run_review"),
    ],
)
@pytest.mark.parametrize("late_error", [False, True])
def test_pipeline_cancellation_stays_cancelled_and_allows_next_run(
    db,
    class_id,
    monkeypatch,
    pipeline_name,
    job_name,
    runner_name,
    late_error,
):
    from importlib import import_module

    pipeline = import_module("backend.core." + pipeline_name)
    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="2026-09-05")
    db.commit()

    def cancel(conn, job):
        writer_runs.request_cancel(conn, job.artifact_id)
        assert pipeline._cancel_requested(conn, job)
        if late_error:
            raise RuntimeError("late upstream error")

    monkeypatch.setattr(pipeline, "_run", cancel)
    getattr(pipeline, runner_name)(getattr(pipeline, job_name)(artifact["id"], run_id=run["id"]))
    assert writer_runs.get_run(db, run["id"])["status"] == "cancelled"
    assert artifacts.get_artifact(db, artifact["id"])["state"] == "cancelled"
    assert writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="next")["id"]
    db.commit()


@pytest.mark.parametrize("failure", ["length", "short", "unknown"])
def test_live_paragraph_preserves_partial_and_never_completes(db, class_id, monkeypatch, failure):
    import time

    from backend.core import live_drafts, writer_pipeline
    from backend.core.app_settings import TutorConfig
    from backend.core.errors import LyraError

    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="2026-09-05")
    db.commit()
    artifacts.create_part(db, artifact["id"], artifacts.DRAFT_BODY, 1, content="Student writing")
    suggestion = live_drafts.create_live_suggestion(db, artifact["id"], run_id=run["id"])
    block = live_drafts.model_update_block(db, suggestion["id"], "1:p1", target_words=180)

    async def stream(*args, **kwargs):
        yield writer_pipeline.client.StreamDelta("answer", "Useful partial text.")
        if failure != "short":
            raise writer_pipeline.client.StreamCompletionError(failure)

    monkeypatch.setattr(writer_pipeline.client, "stream_chat", stream)
    with pytest.raises(LyraError):
        writer_pipeline._stream_live_paragraph(
            db,
            writer_pipeline.PassJob(
                artifact["id"], run_id=run["id"], _deadline=time.monotonic() + 1
            ),
            TutorConfig("http://127.0.0.1/v1", None, "m", 8192),
            suggestion["id"],
            block,
            [],
        )
    saved = writer_pipeline._live_block_by_key(db, suggestion["id"], "1:p1")
    assert saved["content"] == "Useful partial text."
    assert saved["status"] != "complete"


def test_missing_historical_material_is_rejected_without_deleting_evidence(db, class_id):
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Original evidence",
        excerpts=["Original evidence"],
    )
    db.execute(
        "update writer_source_revisions set snapshot = '' where source_id = ?", (source["id"],)
    )
    db.commit()
    with pytest.raises(ValueError, match="exact passage"):
        source_ledger.replace_excerpts(db, source["id"], ["Original evidence"])
    assert (
        source_ledger.get_source(db, source["id"])["excerpts"][0]["excerpt"] == "Original evidence"
    )


def test_same_content_refresh_keeps_revision(db, class_id):
    kwargs = dict(
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Original evidence",
        excerpts=["Original evidence"],
    )
    before = source_ledger.upsert_source(db, class_id, **kwargs)
    after = source_ledger.upsert_source(db, class_id, **kwargs)
    assert before["current_revision_id"] == after["current_revision_id"]
    assert after["excerpts"][0]["source_revision_id"] == before["current_revision_id"]


def test_refresh_and_excerpt_add_preserve_real_revision_under_concurrency(db, class_id):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.storage.database import connect

    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Original evidence",
        excerpts=["Original evidence"],
    )
    barrier = Barrier(2)

    def operation(refresh):
        conn = connect()
        try:
            barrier.wait(timeout=2)
            if refresh:
                source_ledger.upsert_source(
                    conn,
                    class_id,
                    source_type="web",
                    title="Source",
                    url="https://example.org/source",
                    snapshot="Corrected evidence",
                )
            else:
                source_ledger.add_excerpt(
                    conn, source["id"], "Original evidence", section_ref="new"
                )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(operation, [True, False]))
    assert all(
        e["source_revision_id"] == source["current_revision_id"]
        for e in source_ledger.get_source(db, source["id"])["excerpts"]
    )


def test_preflight_accounts_for_schema_and_exact_input_boundary():
    import json

    from backend.core import writer_pipeline
    from backend.core.app_settings import TutorConfig
    from backend.core.errors import LyraError
    from backend.llm import client
    from backend.llm.turn_budget import input_ceiling
    from backend.rag.tokens import estimate_tokens

    config = TutorConfig("http://127.0.0.1/v1", None, "m", 2048)
    messages = [{"role": "user", "content": ""}]
    limit = input_ceiling(2048, 512)
    while estimate_tokens(json.dumps({"messages": messages}, ensure_ascii=False)) < limit:
        messages[0]["content"] += "a"
    writer_pipeline._preflight_request(config, messages, 512)
    messages[0]["content"] += "aaaa"
    with pytest.raises(LyraError, match="context window"):
        writer_pipeline._preflight_request(config, messages, 512)
    with pytest.raises(LyraError, match="context window"):
        writer_pipeline._preflight_request(
            config,
            [],
            512,
            client.JsonSchema("large", {"description": "x" * 20000}),
        )
    writer_pipeline._preflight_request(
        config, [], 512, client.JsonSchema("small", {"type": "object"})
    )


def test_prompt_and_export_identify_original_supporting_snapshot(db, class_id):
    from backend.core import exporting

    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Original evidence",
        excerpts=["Original evidence"],
        accessed_at="2026-01-01",
    )
    source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Corrected evidence",
        accessed_at="2026-09-01",
    )
    projection = source_ledger.list_sources(db, class_id)
    excerpt = projection[0]["excerpts"][0]
    assert excerpt["supporting_revision"] == 1
    assert excerpt["supporting_accessed_at"] == "2026-01-01"
    rendered = exporting.render_citations(f"Claim [@lyra:{source['id']}]", projection)
    assert "revision 1 (saved 2026-01-01)" in rendered
    assert "Accessed 2026-09-01" not in rendered


def test_live_paragraph_drops_optional_neighbors_before_mandatory_evidence():
    from backend.core import writer_pipeline
    from backend.core.app_settings import TutorConfig

    messages = writer_pipeline._live_paragraph_prompt(
        TutorConfig("http://127.0.0.1/v1", None, "m", 8192),
        "Essay",
        document_map="Mandatory document map",
        section_plan="Mandatory plan",
        paragraph_plan="Mandatory assignment",
        research_block="Useful research",
        ledger_block='Source ledger: [{"id": 5, "source_revision_id": 7}]',
        previous_paragraph="Optional neighbor " * 20000,
        next_paragraph_summary="Next idea",
        target_words=180,
    )
    text = str(messages)
    assert "Optional neighbor" not in text
    for mandatory in (
        "Mandatory document map",
        "Mandatory plan",
        "Mandatory assignment",
        "source_revision_id",
        "Useful research",
        "Next idea",
    ):
        assert mandatory in text


@pytest.mark.parametrize(
    "pipeline_name,job_name,runner_name",
    [
        ("writer_pipeline", "PassJob", "run_pass"),
        ("review_pipeline", "ReviewJob", "run_review"),
    ],
)
def test_cancellation_commit_cannot_overwrite_a_new_run(
    db,
    class_id,
    monkeypatch,
    pipeline_name,
    job_name,
    runner_name,
):
    from importlib import import_module
    from threading import Event, Thread

    from backend.storage.database import connect

    pipeline = import_module("backend.core." + pipeline_name)
    artifact = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    run = writer_runs.create_run(db, artifact["id"], "pass", "quick", started_at="old")
    db.commit()
    released, queued = Event(), Event()
    failures = []

    def queue_successor():
        conn = connect()
        try:
            assert released.wait(3)
            writer_runs.create_run(conn, artifact["id"], "pass", "quick", started_at="new")
            conn.execute(
                "update artifacts set state = 'generating', stage_detail = 'new run', "
                "writer_job_completed_at = null where id = ?",
                (artifact["id"],),
            )
            conn.commit()
        except BaseException as exc:
            failures.append(exc)
        finally:
            conn.close()
            queued.set()

    class PauseAfterCancellationCommit:
        def __init__(self):
            self.conn = connect()
            self.paused = False

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            if (
                not self.paused
                and writer_runs.get_run(self.conn, run["id"])["status"] == "cancelled"
            ):
                self.paused = True
                released.set()
                assert queued.wait(3)

    proxy = PauseAfterCancellationCommit()
    monkeypatch.setattr(pipeline, "connect", lambda: proxy)

    def cancel(conn, job):
        writer_runs.request_cancel(conn, job.artifact_id)
        assert pipeline._cancel_requested(conn, job)

    monkeypatch.setattr(pipeline, "_run", cancel)
    successor = Thread(target=queue_successor)
    successor.start()
    getattr(pipeline, runner_name)(getattr(pipeline, job_name)(artifact["id"], run_id=run["id"]))
    successor.join(timeout=3)
    assert not successor.is_alive()
    assert not failures
    latest = writer_runs.latest_run(db, artifact["id"])
    mirror = artifacts.get_artifact(db, artifact["id"])
    assert latest["id"] != run["id"] and latest["status"] == "queued"
    assert mirror["state"] == "generating" and mirror["stage_detail"] == "new run"
    assert (
        db.execute(
            "select writer_job_completed_at from artifacts where id = ?", (artifact["id"],)
        ).fetchone()[0]
        is None
    )


@pytest.mark.parametrize("available", [True, False])
def test_legacy_wrong_revision_projects_truthfully_without_read_mutation(db, class_id, available):
    from backend.core import exporting

    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Original evidence",
        excerpts=["Original evidence"],
        accessed_at="2026-01-01",
    )
    refreshed = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Source",
        url="https://example.org/source",
        snapshot="Corrected evidence",
        accessed_at="2026-09-01",
    )
    db.execute(
        "update writer_source_excerpts set source_revision_id = ? where source_id = ?",
        (refreshed["current_revision_id"], source["id"]),
    )
    if not available:
        db.execute(
            "update writer_source_revisions set snapshot = '' where id = ?",
            (source["current_revision_id"],),
        )
    db.commit()
    before = db.total_changes
    projection = source_ledger.list_sources(db, class_id)
    excerpt = projection[0]["excerpts"][0]
    assert db.total_changes == before
    assert excerpt["excerpt"] == "Original evidence"
    if available:
        assert excerpt["source_revision_id"] == source["current_revision_id"]
        reused = source_ledger.add_excerpt(db, source["id"], "Original evidence")
        assert reused["source_revision_id"] == source["current_revision_id"]
        stored = db.execute(
            "select source_revision_id from writer_source_excerpts where id = ?", (reused["id"],)
        ).fetchone()[0]
        assert stored == source["current_revision_id"]
    else:
        assert excerpt["source_revision_id"] is None and excerpt["evidence_unavailable"]
        with pytest.raises(ValueError, match="exact passage"):
            source_ledger.add_excerpt(db, source["id"], "Original evidence")
        rendered = exporting.render_citations(f"Claim [@lyra:{source['id']}]", projection)
        assert "supporting snapshots are unavailable" in rendered
