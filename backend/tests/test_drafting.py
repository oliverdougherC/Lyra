"""Contract tests for the drafting worker: dispatch by type, and honest reconciliation.

The pass itself is test_writer_pipeline.py; what lives here is the queue's contract
(only registered job types ride it) and what a restart tells the student about a run
it interrupted.
"""

import sqlite3
from dataclasses import dataclass

import pytest

from backend.core import (
    artifacts,
    drafting,
    review_pipeline,
    solver,
    study,
    writer_pipeline,
    writer_plans,
    writer_runs,
)
from backend.core.app_settings import TutorAccess


def _draft(db: sqlite3.Connection, class_id: int, content: str = "x") -> tuple[int, int]:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    part_id = artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, int(created["id"]), artifacts.READY)
    return int(created["id"]), part_id


def test_enqueue_refuses_a_job_nobody_registered() -> None:
    @dataclass(frozen=True)
    class Stray:
        artifact_id: int

    with pytest.raises(ValueError, match="Stray"):
        drafting.enqueue(Stray(artifact_id=1))


def test_the_pipelines_job_is_registered_by_import() -> None:
    # `enqueue` would raise for an unregistered type, so not raising is the assertion;
    # the queue is drained by a worker no test starts, so the job just sits there.
    drafting.enqueue(writer_pipeline.PassJob(artifact_id=999_999))
    assert drafting._RUNNERS[writer_pipeline.PassJob] is writer_pipeline.run_pass


def test_reconcile_returns_interrupted_runs_to_ready(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Structuring the document")
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, 'notes.pdf', '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    deck = artifacts.create_artifact(
        db,
        class_id,
        "Deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    artifacts.set_artifact_state(db, int(deck["id"]), artifacts.GENERATING)

    requeued, resumed = drafting.reconcile_interrupted(db)

    assert requeued == 0
    assert resumed == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == drafting.INTERRUPTED_DETAIL
    # The study reconcile owns decks; this one leaves them alone.
    assert artifacts.get_artifact(db, int(deck["id"]))["state"] == artifacts.GENERATING


def test_reconcile_reports_how_far_a_sectioned_pass_got(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Drafting 2 Methods")
    artifacts.set_problems_total(db, artifact_id, 5)
    artifacts.set_problems_done(db, artifact_id, 2)

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 0
    assert recovered == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert "2 of 5 steps finished" in str(artifact["stage_detail"])


def test_study_and_solver_reconciles_leave_drafts_alone(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(solver, "enqueue", lambda artifact_id: None)
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Revising")

    study.reconcile_interrupted(db)
    solver.reconcile_interrupted(db)

    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.GENERATING


def test_reconcile_requeues_persisted_writer_runs_and_preserves_cancel_intent(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)
    pass_id, _ = _draft(db, class_id)
    review_id, _ = _draft(db, class_id)
    cancelled_id, _ = _draft(db, class_id)

    started_at = "2026-08-07T12:00:00+00:00"
    writer_runs.create_run(
        db,
        pass_id,
        writer_runs.PASS,
        "quick",
        request={"instruction": "tighten", "section_refs": ["1"]},
        started_at=started_at,
    )
    review_run = writer_runs.create_run(
        db,
        review_id,
        writer_runs.REVIEW,
        "standard",
        request={},
        started_at=started_at,
    )
    cancel_run = writer_runs.create_run(
        db,
        cancelled_id,
        writer_runs.PASS,
        "deep",
        request={"instruction": "revise"},
        started_at=started_at,
    )
    db.execute(
        "update writer_runs set status = ? where id = ?",
        (writer_runs.RUNNING, review_run["id"]),
    )
    db.execute(
        "update writer_runs set status = ?, cancel_requested_at = datetime('now') where id = ?",
        (writer_runs.CANCEL_REQUESTED, cancel_run["id"]),
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ? where id = ?",
        (artifacts.PENDING, "Queued", pass_id),
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ? where id = ?",
        (artifacts.GENERATING, "Reviewing prose", review_id),
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ? where id = ?",
        (artifacts.GENERATING, "Drafting 1 Intro", cancelled_id),
    )
    db.commit()

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert recovered == 0
    assert requeued == 3
    assert isinstance(queued[0], writer_pipeline.PassJob)
    assert isinstance(queued[1], review_pipeline.ReviewJob)
    assert isinstance(queued[2], writer_pipeline.PassJob)
    assert queued[2].run_id is not None
    restored = writer_runs.get_run(db, int(queued[2].run_id))
    assert restored["status"] == writer_runs.CANCEL_REQUESTED
    assert restored["warnings"][0]["code"] == writer_runs.RESTART_WARNING


def test_recovered_pass_skips_a_validated_completed_section(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Essay\n\n## Introduction\n\nRecovered prose.\n\n## Results\n\n[TODO: write]\n"
    artifact_id, _ = _draft(db, class_id, content=body)
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.PASS,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="sections",
        index=1,
        targets=("1.1 Introduction", "1.2 Results"),
        data={
            "processed_sections": [
                {
                    "ref": "1.1",
                    "owned_hash": writer_pipeline._section_hash(
                        "## Introduction\n\nRecovered prose.\n\n"
                    ),
                    "direct_landed": True,
                }
            ],
            "changed": True,
            "cut_off": 0,
            "degraded_web_research": 0,
        },
    )
    db.execute(
        "update writer_runs set status = ? where id = ?", (writer_runs.RUNNING, int(run["id"]))
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ?, problems_total = 2, problems_done = 1 "
        "where id = ?",
        (artifacts.GENERATING, "Drafting 1 Introduction", artifact_id),
    )
    db.commit()

    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=writer_pipeline.TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )
    seen: list[str] = []
    captured_owned: dict[str, str] = {}

    def fake_run_section(conn, job, artifact, config, class_id, part_id, number, title, *args):  # noqa: ANN001, ANN003
        owned = args[1]
        direct_landings = args[3]
        seen.append(number)
        owned[number] = "## Results\n\nFreshly resumed prose.\n"
        direct_landings.add(number)
        return True, False

    def fake_revise(*args):  # noqa: ANN001, ANN003
        captured_owned.update(args[7])
        return False

    monkeypatch.setattr(writer_pipeline, "_run_section", fake_run_section)
    monkeypatch.setattr(writer_pipeline, "_revise_stage", fake_revise)

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 1
    assert recovered == 0
    assert len(queued) == 1
    writer_pipeline.run_pass(queued[0])
    assert seen == ["1.2"]
    assert "Recovered prose." in captured_owned["1.1"]
    assert "Freshly resumed prose." in captured_owned["1.2"]
    artifact = artifacts.get_artifact(db, artifact_id)
    assert int(artifact["problems_done"]) <= int(artifact["problems_total"])
    assert writer_runs.get_run(db, int(queued[0].run_id))["status"] == writer_runs.COMPLETED


def test_recovered_done_pass_settles_without_final_stage_calls(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "# Essay\n\n## Introduction\n\nRecovered prose.\n\n## Results\n\nFreshly resumed prose.\n"
    )
    artifact_id, _ = _draft(db, class_id, content=body)
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.PASS,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="done",
        index=2,
        targets=("1.1 Introduction", "1.2 Results"),
        data={
            "processed_sections": [
                {
                    "ref": "1.1",
                    "owned_hash": writer_pipeline._section_hash(
                        "## Introduction\n\nRecovered prose.\n\n"
                    ),
                    "direct_landed": True,
                },
                {
                    "ref": "1.2",
                    "owned_hash": writer_pipeline._section_hash(
                        "## Results\n\nFreshly resumed prose.\n"
                    ),
                    "direct_landed": True,
                },
            ],
            "changed": True,
            "cut_off": 0,
            "degraded_web_research": 0,
        },
    )
    db.execute(
        "update writer_runs set status = ? where id = ?", (writer_runs.RUNNING, int(run["id"]))
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ?, problems_total = 2, problems_done = 2 "
        "where id = ?",
        (artifacts.GENERATING, "Revising", artifact_id),
    )
    db.commit()

    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)
    monkeypatch.setattr(
        writer_pipeline,
        "_revise_stage",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not rerun revise")),
    )
    monkeypatch.setattr(
        writer_pipeline,
        "_weave_stage",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not rerun weave")),
    )
    monkeypatch.setattr(
        writer_pipeline,
        "_prepare_research_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not redo research preparation")
        ),
    )
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=writer_pipeline.TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 1
    assert recovered == 0
    writer_pipeline.run_pass(queued[0])
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert int(artifact["problems_done"]) <= int(artifact["problems_total"])
    assert writer_runs.get_run(db, int(queued[0].run_id))["status"] == writer_runs.COMPLETED


def test_recovered_partial_parallel_planned_pass_resumes_serially_from_checkpoint(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Essay\n\n## First\n\nFirst prose.\n\n## Second\n\n[TODO: second]\n"
    artifact_id, _ = _draft(db, class_id, content=body)
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="A thesis",
        argument_map={"claims": []},
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "First",
                "job": "First job",
                "claim": "First claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
            {
                "section_ref": "1.2",
                "ordinal": 1,
                "title": "Second",
                "job": "Second job",
                "claim": "Second claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
        ],
    )
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.PASS,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="sections",
        index=1,
        targets=("1.1 First", "1.2 Second"),
        data={
            "processed_sections": [
                {
                    "ref": "1.1",
                    "owned_hash": "",
                    "direct_landed": False,
                }
            ],
            "changed": True,
            "cut_off": 0,
            "degraded_web_research": 0,
        },
    )
    db.execute(
        "update writer_runs set status = ? where id = ?", (writer_runs.RUNNING, int(run["id"]))
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ?, problems_total = 2, problems_done = 1 "
        "where id = ?",
        (artifacts.GENERATING, "Drafting 1.1 First", artifact_id),
    )
    db.commit()

    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)
    monkeypatch.setattr(
        writer_pipeline,
        "_parallel_initial_sections",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovered partial pass should not re-enter parallel lane")
        ),
    )
    monkeypatch.setattr(
        writer_pipeline,
        "_prepare_research_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("post-research resume should not redo research preparation")
        ),
    )
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args: False)
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=writer_pipeline.TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )
    seen: list[str] = []

    def fake_run_section(conn, job, artifact, config, class_id, part_id, number, title, *args):  # noqa: ANN001, ANN003
        seen.append(number)
        return True, False

    monkeypatch.setattr(writer_pipeline, "_run_section", fake_run_section)

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 1
    assert recovered == 0
    writer_pipeline.run_pass(queued[0])
    assert seen == ["1.2"]
