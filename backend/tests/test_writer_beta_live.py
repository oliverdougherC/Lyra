"""HTTP-started live-draft regressions; scripted inference is not model acceptance."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import live_drafts, writer_pipeline, writer_plans, writer_runs
from backend.core.app_settings import TutorAccess, TutorConfig


@pytest.fixture
def live_run(db, class_id, monkeypatch, request):
    jobs = []
    monkeypatch.setattr(writer_pipeline, "enqueue", jobs.append)
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 32768), None, True
        ),
    )
    app = FastAPI()
    app.include_router(routes_drafts.router)
    with TestClient(app) as client:
        draft = client.post(f"/api/classes/{class_id}/drafts", json={"title": "Bus memo"}).json()
        artifact_id = draft["id"]
        body = "I missed the last bus again. Keep this student sentence exactly."
        if getattr(request, "param", None) == "long":
            body += " Meaningful student argument." * 20000
        current = client.get(f"/api/drafts/{artifact_id}").json()
        assert (
            client.patch(
                f"/api/drafts/{artifact_id}/body",
                json={"content": body, "expected_version": current["body_version"]},
            ).status_code
            == 200
        )
        assert client.post(f"/api/drafts/{artifact_id}/pass", json={}).status_code == 202
        job = jobs.pop()
        assert job.run_id == client.get(f"/api/drafts/{artifact_id}/status").json()["run_id"]
        writer_plans.create_plan(
            db,
            artifact_id,
            thesis="Keep evening transit available.",
            sections=[{"section_ref": "1", "title": "Evidence", "word_budget": 100}],
        )
        live = live_drafts.get_live_suggestion_for_run(db, job.run_id)
        for index in range(2):
            live_drafts.model_update_block(
                db,
                live["id"],
                f"1:p{index + 1}",
                section_ref="1",
                heading="Evidence",
                paragraph_ordinal=index + 1,
                status="complete",
                target_words=50,
                content=f"Distinct passage {index + 1}. " + "The survey measured ridership. " * 15,
            )
        writer_runs.checkpoint(db, job.run_id, stage="transitions")
        yield client, job, live["id"], body


@pytest.mark.parametrize(
    "failure", ["malformed_transition", "malformed_assessment", "empty_revision", "foreign_block"]
)
def test_invalid_live_review_cannot_finalize(live_run, db, monkeypatch, failure):
    client, job, suggestion_id, body = live_run

    def complete(config, messages, schema=None, **kwargs):
        if schema is writer_pipeline.prompts.TRANSITION_REVIEW_SCHEMA:
            return (
                "not json"
                if failure == "malformed_transition"
                else json.dumps(
                    {"needs_change": False, "rationale": "Clear", "revised_next_paragraph": ""}
                )
            )
        if schema is writer_pipeline.prompts.OVERALL_ASSESSMENT_SCHEMA:
            if failure == "malformed_assessment":
                return "not json"
            return json.dumps(
                {
                    "summary": "Revise",
                    "issues": [
                        {
                            "block_key": "unrelated:p1" if failure == "foreign_block" else "1:p1",
                            "problem": "Overclaim",
                            "revision_instruction": "Limit claim to measured riders.",
                        }
                    ],
                }
            )
        return ""

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(job)
    status = client.get(f"/api/drafts/{job.artifact_id}/status").json()
    assert status["run_status"] == "failed"
    assert client.get(f"/api/drafts/{job.artifact_id}").json()["body"] == body
    assert client.get(f"/api/drafts/{job.artifact_id}/pending").json() is None
    assert all(
        block["content"] for block in live_drafts.get_live_suggestion(db, suggestion_id)["blocks"]
    )


def test_revision_call_contains_the_passage_it_is_revising(live_run, db, monkeypatch):
    _, job, suggestion_id, _ = live_run
    original = live_drafts.get_live_suggestion(db, suggestion_id)["blocks"][0]["content"]
    revision_prompts = []

    def complete(config, messages, schema=None, **kwargs):
        if schema is writer_pipeline.prompts.TRANSITION_REVIEW_SCHEMA:
            return '{"needs_change":false,"rationale":"clear","revised_next_paragraph":""}'
        if schema is writer_pipeline.prompts.OVERALL_ASSESSMENT_SCHEMA:
            return json.dumps(
                {
                    "summary": "Narrow claim",
                    "issues": [
                        {
                            "block_key": "1:p1",
                            "problem": "Overclaim",
                            "revision_instruction": "Limit claim to measured riders.",
                        }
                    ],
                }
            )
        revision_prompts.append(messages)
        return original

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(job)
    assert revision_prompts
    assert original in "\n".join(message["content"] for message in revision_prompts[0])


def test_restart_after_finalization_does_not_repeat_review(live_run, db, monkeypatch):
    client, job, suggestion_id, body = live_run
    writer_runs.checkpoint(db, job.run_id, stage="finalizing")

    def forbidden(*args, **kwargs):
        pytest.fail("A finalizing run already settled model review before restart")

    monkeypatch.setattr(writer_pipeline, "_complete", forbidden)
    writer_pipeline.run_pass(job)
    assert client.get(f"/api/drafts/{job.artifact_id}/status").json()["run_status"] == "completed"
    assert client.get(f"/api/drafts/{job.artifact_id}").json()["body"] == body
    assert live_drafts.get_live_suggestion(db, suggestion_id)["status"] == "ready"


def test_restart_between_review_chunks_keeps_settled_revision(live_run, db, monkeypatch):
    client, job, suggestion_id, body = live_run
    writer_runs.checkpoint(db, job.run_id, stage="reviewing")
    monkeypatch.setattr(writer_pipeline, "LIVE_REVIEW_CHUNK_BLOCKS", 1)
    assessments = []
    revisions = []
    interrupted = False
    original = live_drafts.get_live_suggestion(db, suggestion_id)["blocks"][0]["content"]
    revised = original.replace("Distinct passage 1.", "Precisely corrected passage 1.").strip()

    def complete(config, messages, schema=None, **kwargs):
        nonlocal interrupted
        rendered = str(messages)
        if schema is writer_pipeline.prompts.OVERALL_ASSESSMENT_SCHEMA:
            key = "1:p1" if "[1:p1]" in rendered else "1:p2"
            assessments.append(key)
            if key == "1:p2" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("Synthetic process death after prior chunk commit")
            return json.dumps(
                {
                    "summary": "Review",
                    "issues": []
                    if key == "1:p2"
                    else [
                        {
                            "block_key": key,
                            "problem": "Clarify",
                            "revision_instruction": "Narrow the claim.",
                        }
                    ],
                }
            )
        revisions.append(rendered)
        return revised

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    with pytest.raises(KeyboardInterrupt):
        writer_pipeline.run_pass(job)
    assert live_drafts.get_live_suggestion(db, suggestion_id)["blocks"][0]["content"] == revised
    writer_runs.queue_for_restart(db, job.run_id, "Synthetic restart")
    writer_pipeline.run_pass(job)
    assert assessments == ["1:p1", "1:p2", "1:p2"]
    assert len(revisions) == 1
    assert client.get(f"/api/drafts/{job.artifact_id}/status").json()["run_status"] == "completed"
    assert client.get(f"/api/drafts/{job.artifact_id}").json()["body"] == body
    assert live_drafts.get_live_suggestion(db, suggestion_id)["blocks"][0]["content"] == revised


def test_malformed_transition_object_is_rejected(live_run, db, monkeypatch):
    client, job, sid, body = live_run
    malformed = {"not_prose": "meaningless structured payload " * 30}

    def complete(config, messages, schema=None, **kwargs):
        if schema is writer_pipeline.prompts.TRANSITION_REVIEW_SCHEMA:
            return json.dumps(
                {"needs_change": True, "rationale": "test", "revised_next_paragraph": malformed}
            )
        return '{"summary":"clear","issues":[]}'

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(job)
    assert client.get(f"/api/drafts/{job.artifact_id}/status").json()["run_status"] == "failed"
    assert live_drafts.get_live_suggestion(db, sid)["blocks"][1]["content"] != str(malformed)


def test_cancellation_after_inference_preserves_saved_block(live_run, db, monkeypatch):
    client, job, sid, body = live_run
    original = live_drafts.get_live_suggestion(db, sid)["blocks"][1]["content"]
    replacement = "late callback text " * 30

    def complete(config, messages, schema=None, **kwargs):
        assert client.post(f"/api/drafts/{job.artifact_id}/cancel").status_code == 200
        return json.dumps(
            {"needs_change": True, "rationale": "test", "revised_next_paragraph": replacement}
        )

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(job)
    assert client.get(f"/api/drafts/{job.artifact_id}/status").json()["run_status"] == "cancelled"
    assert live_drafts.get_live_suggestion(db, sid)["blocks"][1]["content"] == original


@pytest.mark.parametrize("operation", ["status", "finalize"])
def test_publication_reads_concurrent_changes_under_writer_lock(live_run, db, operation):
    _, _, suggestion_id, _ = live_run
    before = live_drafts.get_live_suggestion(db, suggestion_id)
    block = before["blocks"][0]
    student_edit = "My new sentence must appear in the published suggestion."

    class BeforeLock:
        def __init__(self):
            self.injected = False

        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, sql, *args):
            if sql == "begin immediate" and not self.injected:
                self.injected = True
                live_drafts.patch_block(
                    db, block["id"], expected_revision=block["revision"], content=student_edit
                )
                if operation == "status":
                    live_drafts.update_live_suggestion(db, suggestion_id, detail="Newer progress")
            return db.execute(sql, *args)

    if operation == "status":
        result = live_drafts.update_live_suggestion(BeforeLock(), suggestion_id, status="running")
        assert result["version"] == before["version"] + 2
    else:
        result = live_drafts.finalize_to_pending_edit(BeforeLock(), suggestion_id)
        assert student_edit in result["proposed_content"]


def test_schema_calls_reserve_output_for_the_requested_artifact(monkeypatch):
    from backend.core.app_settings import TutorConfig

    seen = []

    async def complete(*args, **kwargs):
        seen.append((kwargs["max_tokens"], kwargs["enable_thinking"]))
        return '{"summary":"Evidence checked","issues":[]}'

    monkeypatch.setattr(writer_pipeline.client, "complete", complete)
    writer_pipeline._complete(
        TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 262144),
        [{"role": "user", "content": "Return the requested structured assessment."}],
        schema=writer_pipeline.prompts.OVERALL_ASSESSMENT_SCHEMA,
    )
    assert seen == [(4096, False)]


def test_research_schema_uses_the_same_id_type_as_saved_evidence(db, class_id):
    from backend.core import source_ledger

    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Synthetic evidence",
        url="https://synthetic.invalid/id-contract",
        snapshot="Measured trips, not attendance.",
    )
    assert isinstance(source["id"], int)
    assert (
        writer_pipeline.prompts.RESEARCH_NOTES_SCHEMA.schema["properties"]["source_ids"]["items"][
            "type"
        ]
        == "integer"
    )


def test_existing_plan_cannot_hide_assignment_or_student_voice_from_live_review(
    live_run, db, monkeypatch
):
    from backend.core import briefs

    _, job, _, body = live_run
    restriction = "Do not invent costs, staffing claims, or personal observations."
    briefs.save_brief(db, job.artifact_id, summary=restriction)
    seen = []

    def complete(config, messages, schema=None, **kwargs):
        seen.append("\n".join(message["content"] for message in messages))
        if schema is writer_pipeline.prompts.TRANSITION_REVIEW_SCHEMA:
            return '{"needs_change":false,"rationale":"clear","revised_next_paragraph":""}'
        return '{"summary":"clear","issues":[]}'

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(job)
    assert seen
    assert all(restriction in prompt and body in prompt for prompt in seen)


def test_targeted_prose_reserves_output_for_writing(monkeypatch):
    from backend.core.app_settings import TutorConfig

    seen = []

    async def complete(*args, **kwargs):
        seen.append((kwargs["max_tokens"], kwargs["enable_thinking"]))
        return "A narrowly corrected passage."

    monkeypatch.setattr(writer_pipeline.client, "complete", complete)
    writer_pipeline._complete(
        TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 8192),
        [{"role": "user", "content": "Correct this passage only."}],
        target_words=200,
    )
    assert seen == [(480, False)]


@pytest.mark.parametrize("live_run", ["long"], indirect=True)
def test_existing_plan_cannot_bypass_mandatory_prose_budget(live_run, monkeypatch):
    client, job, _, body = live_run

    def forbidden(*args, **kwargs):
        pytest.fail("The saved plan must not hide oversized mandatory student prose")

    monkeypatch.setattr(writer_pipeline, "_complete", forbidden)
    writer_pipeline.run_pass(job)
    status = client.get(f"/api/drafts/{job.artifact_id}/status").json()
    assert status["run_status"] == "failed"
    assert "context window" in status["error_message"]
    assert client.get(f"/api/drafts/{job.artifact_id}").json()["body"] == body


def test_scoped_writer_evidence_retains_supporting_revision(db, class_id):
    from backend.core import source_ledger

    original = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Synthetic measurements",
        url="https://synthetic.invalid/revisions",
        snapshot="The earlier count was 10.",
        excerpts=["The earlier count was 10."],
    )
    source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Synthetic measurements",
        url="https://synthetic.invalid/revisions",
        snapshot="The corrected count is 20.",
    )
    entries = writer_pipeline._ledger_entries(db, class_id, {"source_ids": [original["id"]]}, "1")
    excerpt = entries[0]["excerpts"][0]
    assert excerpt["id"] == original["excerpts"][0]["id"]
    assert excerpt["source_revision_id"] == original["current_revision_id"]
    assert excerpt["supporting_revision"] == 1


@pytest.mark.parametrize("marker", ["known", "unknown"])
def test_live_model_citations_are_resolvable_before_publication(live_run, db, class_id, marker):
    from backend.core import source_ledger

    client, job, sid, body = live_run
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Synthetic observations",
        url="https://synthetic.invalid/citation",
        snapshot="The survey measured ridership.",
        excerpts=["The survey measured ridership."],
    )
    plan = writer_plans.get_active_plan(db, job.artifact_id)
    writer_plans.update_plan_section(db, plan["id"], "1", source_ids=[source["id"]])
    block = live_drafts.get_live_suggestion(db, sid)["blocks"][0]
    reference = source["id"] if marker == "known" else 999999
    live_drafts.model_update_block(
        db,
        sid,
        block["stable_key"],
        content=block["content"] + f" [@{reference}]",
        status="complete",
    )
    writer_runs.checkpoint(db, job.run_id, stage="finalizing")
    writer_pipeline.run_pass(job)
    status = client.get(f"/api/drafts/{job.artifact_id}/status").json()
    if marker == "known":
        assert status["run_status"] == "completed"
        proposal = client.get(f"/api/drafts/{job.artifact_id}/pending").json()["proposed_content"]
        assert f"[@lyra:{reference}]" in proposal
        assert f"[@{reference}]" not in proposal
    else:
        assert status["run_status"] == "failed"
        assert client.get(f"/api/drafts/{job.artifact_id}/pending").json() is None
    assert client.get(f"/api/drafts/{job.artifact_id}").json()["body"] == body


@pytest.mark.parametrize("cutoff", [False, True])
def test_resumed_paragraph_does_not_append_an_echo_of_saved_prose(
    live_run, db, monkeypatch, cutoff
):
    import time
    from dataclasses import replace

    from backend.core.app_settings import TutorConfig

    _, job, sid, _ = live_run
    block = live_drafts.get_live_suggestion(db, sid)["blocks"][0]
    original = block["content"]

    async def stream(*args, **kwargs):
        text = original[: len(original) // 2] if cutoff else original
        for offset in range(0, len(text), 32):
            yield writer_pipeline.client.StreamDelta("answer", text[offset : offset + 32])
        if cutoff:
            raise writer_pipeline.client.StreamCompletionError("unknown")

    monkeypatch.setattr(writer_pipeline.client, "stream_chat", stream)

    def operation():
        return writer_pipeline._stream_live_paragraph(
            db,
            replace(job, _deadline=time.monotonic() + 2),
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 8192),
            sid,
            block,
            [{"role": "user", "content": "Continue only if needed."}],
        )

    if cutoff:
        with pytest.raises(writer_pipeline.client.StreamCompletionError):
            operation()
    else:
        operation()
    assert live_drafts.get_live_suggestion(db, sid)["blocks"][0]["content"] == original
