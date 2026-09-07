"""HTTP-started live-draft regressions; scripted inference is not model acceptance."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import live_drafts, writer_pipeline, writer_plans, writer_runs
from backend.core.app_settings import TutorAccess, TutorConfig


@pytest.fixture
def live_run(db, class_id, monkeypatch):
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
