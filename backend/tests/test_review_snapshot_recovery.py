"""HTTP reviewer checkpoints certify one writing revision, never changed prose."""

import pytest

from backend.core import artifacts, comments, review_pipeline, writer_runs
from backend.llm.tools import ToolLoopResult
from backend.tests.test_writer_beta_recovery import BODY
from backend.tests.test_writer_beta_recovery import review_case as _review_case

review_case = _review_case

NEW_BODY = BODY.replace("I expected the bus to be empty.", "I counted three riders, carefully.")


def _edit(client, artifact_id):
    draft = client.get(f"/api/drafts/{artifact_id}").json()
    assert (
        client.patch(
            f"/api/drafts/{artifact_id}/body",
            json={"content": NEW_BODY, "expected_version": draft["body_version"]},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("remove_identity", [False, True])
def test_restart_rechecks_changed_prose_with_identical_headings(
    db, review_case, monkeypatch, remove_identity
):
    client, jobs, aid, part_id = review_case
    calls = []

    class ProcessInterrupted(BaseException):
        pass

    def inference(config, messages, registry, depth):
        calls.append(artifacts.get_artifact(db, aid)["stage_detail"])
        if len(calls) == 1:
            registry["add_comment"].handler(body="Earlier saved finding", severity="major")
        if len(calls) == 2:
            raise ProcessInterrupted()
        return ToolLoopResult(content="Reviewed")

    old = comments.add_comment(
        db, part_id, comments.REVIEWER, "Earlier saved finding", severity="major"
    )
    monkeypatch.setattr(review_pipeline, "_review_run", inference)
    with pytest.raises(ProcessInterrupted):
        review_pipeline.run_review(jobs[0])
    if remove_identity:
        checkpoint = writer_runs.get_run(db, jobs[0].run_id)["checkpoint"]
        checkpoint["data"].pop("body_snapshot", None)
        writer_runs.checkpoint(
            db, jobs[0].run_id, stage=checkpoint["stage"], data=checkpoint["data"]
        )
    else:
        _edit(client, aid)
    calls.clear()
    monkeypatch.setattr(
        review_pipeline,
        "_review_run",
        lambda *args: (
            calls.append(artifacts.get_artifact(db, aid)["stage_detail"])
            or ToolLoopResult(content="Reviewed")
        ),
    )
    writer_runs.queue_for_restart(db, jobs[0].run_id, "Synthetic process restart")
    review_pipeline.run_review(jobs[0])
    assert calls[0] == "Reviewing structure"
    run = writer_runs.get_run(db, jobs[0].run_id)
    assert any(w["code"] == writer_runs.CHECKPOINT_MISMATCH_WARNING for w in run["warnings"])
    assert run["status"] == writer_runs.COMPLETED
    assert run["checkpoint"]["data"]["confirmed_comment_ids"] == []
    assert old["id"] in run["checkpoint"]["data"]["previous_snapshot_comment_ids"]
    assert "earlier comment" in artifacts.get_artifact(db, aid)["stage_detail"]
    assert old["id"] in {c["id"] for c in comments.list_threads(db, part_id, NEW_BODY)}
    assert artifacts.get_part(db, part_id)["content"] == (BODY if remove_identity else NEW_BODY)


def test_edit_during_inference_keeps_comments_without_certifying_new_body(
    db, review_case, monkeypatch
):
    client, jobs, aid, part_id = review_case
    comment = comments.add_comment(db, part_id, comments.STUDENT, "Keep this question open.")

    def inference(config, messages, registry, depth):
        registry["add_comment"].handler(body="Partial useful finding.", severity="major")
        _edit(client, aid)
        return ToolLoopResult(content="Reviewed")

    monkeypatch.setattr(review_pipeline, "_review_run", inference)
    review_pipeline.run_review(jobs[0])
    run = writer_runs.get_run(db, jobs[0].run_id)
    assert run["status"] == writer_runs.FAILED
    assert "writing changed" in run["error_message"].lower()
    assert artifacts.get_part(db, part_id)["content"] == NEW_BODY
    roots = comments.list_threads(db, part_id, NEW_BODY)
    assert len(roots) == 2 and comment["id"] in {c["id"] for c in roots}


def test_edit_at_atomic_close_boundary_prevents_success(db, review_case, monkeypatch):
    client, jobs, aid, part_id = review_case
    monkeypatch.setattr(
        review_pipeline, "_review_run", lambda *args: ToolLoopResult(content="Reviewed")
    )
    original = review_pipeline._close

    def changed_before_close(*args, **kwargs):
        _edit(client, aid)
        return original(*args, **kwargs)

    monkeypatch.setattr(review_pipeline, "_close", changed_before_close)
    review_pipeline.run_review(jobs[0])
    assert writer_runs.get_run(db, jobs[0].run_id)["status"] == writer_runs.FAILED
    assert artifacts.get_part(db, part_id)["content"] == NEW_BODY
    assert db.execute("select count(*) from messages").fetchone()[0] == 0
