"""HTTP-created revision jobs review their current proposal, retaining student intent."""

import json
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import artifacts, suggestions, writer_pipeline
from backend.core.app_settings import TutorConfig


@pytest.fixture
def proposal_job(db, class_id, monkeypatch):
    jobs = []
    monkeypatch.setattr(writer_pipeline, "enqueue", jobs.append)
    app = FastAPI()
    app.include_router(routes_drafts.router)
    original = "# Essay\n\n## Evidence\n\nThe shuttle caused higher attendance.\n"
    corrected = original.replace(
        "The shuttle caused higher attendance.", "Boardings are trips, not attendance."
    )
    with TestClient(app) as client:
        aid = client.post(f"/api/classes/{class_id}/drafts", json={"title": "Essay"}).json()["id"]
        client.patch(f"/api/drafts/{aid}/body", json={"content": original, "expected_version": 0})
        assert (
            client.post(
                f"/api/drafts/{aid}/pass",
                json={
                    "sections": ["1.1"],
                    "instruction": "Remove the attendance claim. Keep my voice.",
                },
            ).status_code
            == 202
        )
        part_id = artifacts.list_parts(db, aid)[0]["id"]
        suggestions.propose(db, part_id, corrected, "initial correction")
        yield replace(jobs[0], _deadline=time.monotonic() + 60), part_id, corrected


def converge(db, job, part_id):
    writer_pipeline._converge_section(
        db,
        job,
        artifacts.get_artifact(db, job.artifact_id),
        TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 8192),
        artifacts.get_artifact(db, job.artifact_id)["class_id"],
        part_id,
        "1.1",
        "Evidence",
        None,
        {},
        {"sections": []},
    )


def test_skeptic_reads_the_proposal_instead_of_rejudging_old_prose(db, proposal_job, monkeypatch):
    job, part_id, corrected = proposal_job
    seen = []

    def complete(config, messages, **kwargs):
        seen.append(str(messages))
        return '{"passes":true,"faults":[],"rewrite_instruction":""}'

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    converge(db, job, part_id)
    assert seen and "Boardings are trips, not attendance." in seen[0]
    assert suggestions.pending_for_part(db, part_id)["proposed_content"] == corrected


def test_followup_revision_keeps_original_instruction_and_run_ownership(
    db, proposal_job, monkeypatch
):
    job, part_id, _ = proposal_job
    monkeypatch.setattr(
        writer_pipeline,
        "_complete",
        lambda *args, **kwargs: json.dumps(
            {
                "passes": False,
                "faults": ["Explain the limitation"],
                "rewrite_instruction": "Explain the limitation.",
            }
        ),
    )
    seen = []

    def revise(conn, followup, *args, **kwargs):
        seen.append(followup)
        return False, False

    monkeypatch.setattr(writer_pipeline, "_run_section", revise)
    from backend.core.errors import LyraError

    with pytest.raises(LyraError, match="review"):
        converge(db, job, part_id)
    assert seen
    assert seen[0].run_id == job.run_id
    assert seen[0]._deadline == job._deadline
    assert job.instruction in seen[0].instruction
    assert "Explain the limitation" in seen[0].instruction


def test_skeptic_knows_the_students_requirements_before_giving_style_advice(
    db, proposal_job, monkeypatch
):
    job, part_id, _ = proposal_job
    seen = []

    def complete(config, messages, **kwargs):
        seen.append(str(messages))
        return '{"passes":true,"faults":[],"rewrite_instruction":""}'

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    converge(db, job, part_id)
    assert job.instruction in seen[0]


def test_uncleared_automatic_review_cannot_claim_a_completed_pass(db, proposal_job, monkeypatch):
    from backend.core.errors import LyraError

    job, part_id, corrected = proposal_job
    monkeypatch.setattr(
        writer_pipeline,
        "_complete",
        lambda *args, **kwargs: json.dumps(
            {
                "passes": False,
                "faults": ["The causal assertion remains unsupported."],
                "rewrite_instruction": "Remove the unsupported inference.",
            }
        ),
    )
    monkeypatch.setattr(writer_pipeline, "_run_section", lambda *args, **kwargs: (False, False))
    with pytest.raises(LyraError, match="review"):
        converge(db, job, part_id)
    assert suggestions.pending_for_part(db, part_id)["proposed_content"] == corrected


def test_http_targeted_cutoff_keeps_proposal_but_never_completes(db, proposal_job, monkeypatch):
    from backend.core import writer_runs
    from backend.core.app_settings import TutorAccess
    from backend.rag.retrieve import RetrievalResult

    job, part_id, _ = proposal_job
    original = artifacts.get_part(db, part_id)["content"]
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda conn: TutorAccess(
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 8192), None, True
        ),
    )
    monkeypatch.setattr(writer_pipeline, "retrieve", lambda *args: RetrievalResult([], False, 0))

    def incomplete(config, messages, target_words=None, truncated=None, **kwargs):
        if truncated is not None:
            truncated.append(True)
            return "## Evidence\n\nUseful but incomplete prose."
        return "[]"

    monkeypatch.setattr(writer_pipeline, "_complete", incomplete)
    writer_pipeline.run_pass(job)
    assert writer_runs.get_run(db, job.run_id)["status"] == "failed"
    assert artifacts.get_part(db, part_id)["content"] == original
    assert suggestions.pending_for_part(db, part_id)
