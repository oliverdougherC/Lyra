"""HTTP retry keeps edited live prose in the successor, not only historical rows."""

import sqlite3
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_drafts
from backend.core import (
    artifacts,
    briefs,
    live_drafts,
    source_ledger,
    writer_pipeline,
    writer_plans,
    writer_runs,
)
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError

EDIT = "I revised this claim myself. Keep my phrasing and uncertainty."


@pytest.fixture
def failed_live(db, class_id, monkeypatch, request):
    failure_mode = getattr(request, "param", "failed")
    jobs = []
    config = [TutorConfig("http://127.0.0.1:9/v1", None, "synthetic-model", 32768)]
    monkeypatch.setattr(writer_pipeline, "enqueue", jobs.append)
    monkeypatch.setattr(
        writer_pipeline, "resolve_tutor_access", lambda conn: TutorAccess(config[0], None, True)
    )
    app = FastAPI()
    app.include_router(routes_drafts.router)
    with TestClient(app) as client:
        aid = client.post(f"/api/classes/{class_id}/drafts", json={"title": "Shuttle memo"}).json()[
            "id"
        ]
        writer_plans.create_plan(
            db,
            aid,
            thesis="Measure the limited observation.",
            sections=[
                {"section_ref": "1", "title": "Observed", "word_budget": 80},
                {"section_ref": "2", "title": "Next steps", "word_budget": 80},
            ],
        )
        schemas = []

        def complete(config, messages, schema=None, **kwargs):
            schemas.append(schema)
            if schema is writer_pipeline.prompts.RESEARCH_NOTES_SCHEMA:
                return '{"notes":[],"source_ids":[],"gaps":[],"relied_on":[]}'
            if schema is writer_pipeline.prompts.PARAGRAPH_OUTLINE_SCHEMA:
                return '{"paragraphs":[{"key":"p1","purpose":"Keep limits", "target_words":80}]}'
            if schema is writer_pipeline.prompts.TRANSITION_REVIEW_SCHEMA:
                return '{"needs_change":false,"rationale":"Clear","revised_next_paragraph":""}'
            return '{"summary":"Clear","issues":[]}'

        monkeypatch.setattr(writer_pipeline, "_complete", complete)
        failed = False
        streamed = []

        def stream(conn, job, config, suggestion_id, block, messages):
            nonlocal failed
            streamed.append((job.run_id, block["stable_key"]))
            if block["stable_key"] == "2:p1" and not failed:
                first = live_drafts.get_live_suggestion(db, suggestion_id)["blocks"][0]
                assert (
                    client.patch(
                        f"/api/drafts/{aid}/live-suggestion/blocks/{first['id']}",
                        json={
                            "expected_revision": first["revision"],
                            "content": EDIT,
                            **({"status": "drafting"} if failure_mode == "edited_partial" else {}),
                        },
                    ).status_code
                    == 200
                )
                failed = True
                if failure_mode == "cancelled":
                    assert client.post(f"/api/drafts/{aid}/cancel").status_code == 200
                if failure_mode != "finalizing":
                    raise LyraError("Synthetic transient provider failure after student edit")
            return live_drafts.model_update_block(
                conn,
                suggestion_id,
                block["stable_key"],
                content="A bounded synthetic observation. " * 25,
                status="complete",
            )

        monkeypatch.setattr(writer_pipeline, "_stream_live_paragraph", stream)
        if failure_mode == "finalizing":
            finalize = live_drafts.finalize_to_pending_edit
            interrupted = False

            def fail_once(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise LyraError("Synthetic interruption before final persistence")
                return finalize(*args, **kwargs)

            monkeypatch.setattr(live_drafts, "finalize_to_pending_edit", fail_once)
        assert client.post(f"/api/drafts/{aid}/pass", json={}).status_code == 202
        first = jobs[-1]
        writer_pipeline.run_pass(first)
        assert writer_runs.get_run(db, first.run_id)["status"] == (
            writer_runs.CANCELLED if failure_mode == "cancelled" else writer_runs.FAILED
        )
        old = live_drafts.get_live_suggestion_for_run(db, first.run_id)
        assert old["blocks"][0]["content"] == EDIT
        yield client, aid, jobs, old, config, streamed, schemas


@pytest.mark.parametrize(
    "failed_live", ["failed", "cancelled", "finalizing", "edited_partial"], indirect=True
)
def test_same_request_retry_carries_student_prose_into_current_successor(db, failed_live):
    client, aid, jobs, old, _, streamed, schemas = failed_live
    outline_calls = schemas.count(writer_pipeline.prompts.PARAGRAPH_OUTLINE_SCHEMA)
    total_calls = len(schemas)
    assert client.post(f"/api/drafts/{aid}/pass", json={}).status_code == 202
    successor = jobs[-1]
    writer_pipeline.run_pass(successor)
    latest = client.get(f"/api/drafts/{aid}/live-suggestion").json()
    assert latest["run_id"] == successor.run_id != old["run_id"]
    assert latest["blocks"][0]["content"] == EDIT
    assert latest["blocks"][0]["user_revision"] == old["blocks"][0]["user_revision"]
    assert (successor.run_id, "1:p1") not in streamed
    assert schemas.count(writer_pipeline.prompts.PARAGRAPH_OUTLINE_SCHEMA) == outline_calls
    if old["stage"] == "finalizing":
        assert len(schemas) == total_calls
    assert writer_runs.get_run(db, old["run_id"])["status"] == old["status"]
    assert live_drafts.get_live_suggestion(db, old["id"])["blocks"] == old["blocks"]
    assert EDIT in client.get(f"/api/drafts/{aid}/pending").json()["proposed_content"]
    artifact = artifacts.get_artifact(db, aid)
    assert artifact["problems_done"] == artifact["problems_total"] == 2


@pytest.mark.parametrize(
    "changed",
    [
        "instruction",
        "depth",
        "body",
        "brief",
        "plan",
        "model",
        "context",
        "endpoint",
        "source",
        "missing_identity",
    ],
)
def test_changed_inputs_cannot_transplant_old_prose(db, failed_live, changed):
    client, aid, jobs, old, config, streamed, _ = failed_live
    payload = {}
    if changed == "instruction":
        payload["instruction"] = "Write a different memo about costs."
    elif changed == "depth":
        payload["depth"] = "deep"
    elif changed == "body":
        draft = client.get(f"/api/drafts/{aid}").json()
        assert (
            client.patch(
                f"/api/drafts/{aid}/body",
                json={
                    "content": "A new student thesis.",
                    "expected_version": draft["body_version"],
                },
            ).status_code
            == 200
        )
    elif changed == "brief":
        briefs.save_brief(db, aid, summary="Write about another question.")
    elif changed == "plan":
        writer_plans.create_plan(
            db,
            aid,
            thesis="A new argument.",
            sections=[{"section_ref": "1", "title": "Different", "word_budget": 80}],
        )
    elif changed in {"model", "context", "endpoint"}:
        field, value = {
            "model": ("model", "another-model"),
            "context": ("context_window", 65536),
            "endpoint": ("endpoint_url", "http://127.0.0.1:10/v1"),
        }[changed]
        config[0] = replace(config[0], **{field: value})
    elif changed == "source":
        source_ledger.upsert_source(
            db,
            artifacts.get_artifact(db, aid)["class_id"],
            source_type="web",
            url="https://synthetic.invalid/revised",
            title="New evidence",
            snapshot="New observations.",
        )
    else:
        db.execute(
            "update live_draft_blocks set metadata_json = '{}' where suggestion_id = ?",
            (old["id"],),
        )
        db.commit()
    assert client.post(f"/api/drafts/{aid}/pass", json=payload).status_code == 202
    successor = jobs[-1]
    writer_pipeline.run_pass(successor)
    latest = client.get(f"/api/drafts/{aid}/live-suggestion").json()
    assert all(block["content"] != EDIT for block in latest["blocks"])
    assert (successor.run_id, "1:p1") in streamed
    assert "resumed_from_run_id" not in writer_runs.get_run(db, successor.run_id)["checkpoint"]
    assert live_drafts.get_live_suggestion(db, old["id"])["blocks"][0]["content"] == EDIT


def test_cloning_finalized_review_checkpoints_does_not_repeat_model_work(db, failed_live):
    client, aid, jobs, old, config, _, _ = failed_live
    # Simulate the later stable review boundary, retaining model assessment metadata.
    db.execute(
        "update live_draft_blocks set content = ?, status = 'complete' "
        "where suggestion_id = ? and stable_key = '2:p1'",
        ("A retained observation. " * 40, old["id"]),
    )
    db.execute("update live_draft_suggestions set stage = 'finalizing' where id = ?", (old["id"],))
    db.commit()
    writer_runs.checkpoint(
        db,
        old["run_id"],
        stage="finalizing",
        index=2,
        targets=("1:p1", "2:p1"),
        data={"checked": True},
    )
    assert client.post(f"/api/drafts/{aid}/pass", json={}).status_code == 202
    successor = jobs[-1]
    new = live_drafts.get_live_suggestion_for_run(db, successor.run_id)

    def identity():
        return writer_pipeline._live_generation_identity(
            db, successor, config[0], writer_plans.get_active_plan(db, aid), ""
        )

    assert live_drafts.resume_previous_suggestion(db, new["id"], identity)
    saved = writer_runs.get_run(db, successor.run_id)["checkpoint"]
    assert saved["stage"] == "finalizing"
    assert saved["index"] == 2
    assert saved["data"] == {"checked": True}
    assert saved["resumed_from_run_id"] == old["run_id"]


def test_clone_and_checkpoint_roll_back_together_on_storage_error(db, failed_live):
    client, aid, jobs, old, config, _, _ = failed_live
    assert client.post(f"/api/drafts/{aid}/pass", json={}).status_code == 202
    successor = jobs[-1]
    new = live_drafts.get_live_suggestion_for_run(db, successor.run_id)
    before = writer_runs.get_run(db, successor.run_id)
    db.execute(
        "create temp trigger refuse_retry_blocks before insert on live_draft_blocks "
        "begin select raise(abort, 'synthetic storage failure'); end"
    )

    def identity():
        return writer_pipeline._live_generation_identity(
            db, successor, config[0], writer_plans.get_active_plan(db, aid), ""
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic storage failure"):
        live_drafts.resume_previous_suggestion(db, new["id"], identity)
    assert live_drafts.get_live_suggestion(db, new["id"])["blocks"] == []
    assert writer_runs.get_run(db, successor.run_id) == before
    assert live_drafts.get_live_suggestion(db, old["id"])["blocks"] == old["blocks"]
