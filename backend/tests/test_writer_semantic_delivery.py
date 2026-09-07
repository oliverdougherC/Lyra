"""Revision requests retain evidence and scope instead of re-executing drafting jobs."""

import json

import pytest

from backend.core import artifacts, source_ledger, writer_pipeline, writer_plans, writer_runs
from backend.core.app_settings import TutorConfig
from backend.llm import prompts
from backend.tests.test_writer_beta_live import live_run as live_run
from backend.tests.test_writer_targeted_validation import targeted as targeted


def test_revision_receives_full_source_and_a_correction_contract(
    db, class_id, live_run, monkeypatch
):
    _, job, suggestion_id, body = live_run
    s = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        title="Survey",
        url="https://synthetic.invalid/survey",
        snapshot=(
            "Eighteen respondents participated. Fourteen valued later service. "
            "Non-riders were not surveyed."
        ),
    )
    source_ledger.add_relied_on_excerpt(db, s["id"], "Non-riders were not surveyed.")
    plan = writer_plans.get_active_plan(db, job.artifact_id)
    writer_plans.update_plan_section(db, plan["id"], "1", source_ids=[s["id"]])
    plan = writer_plans.get_active_plan(db, job.artifact_id)
    writer_runs.mark_running(db, job.run_id)
    captured = []

    def complete(config, messages, schema=None, **kw):
        if schema is prompts.OVERALL_ASSESSMENT_SCHEMA:
            return json.dumps(
                {
                    "summary": "Check unsupported feedback",
                    "issues": [
                        {
                            "block_key": "1:p1",
                            "problem": "14 absent from notes",
                            "revision_instruction": "Delete the 14 respondents statistic.",
                        }
                    ],
                }
            )
        captured.extend(messages)
        return "Distinct passage 1. " + "The survey measured ridership. " * 15

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline._review_live_chunks(
        db,
        job,
        artifacts.get_artifact(db, job.artifact_id),
        TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768),
        class_id,
        suggestion_id,
        plan,
        "Student stance reference",
    )
    text = "\n".join(m["content"] for m in captured)
    assert "Fourteen valued later service." in text
    assert "source_revision_id" in text
    assert "reviewer feedback is a claim to check" in " ".join(
        captured[0]["content"].lower().split()
    )
    assert "Execute the supplied paragraph job" not in captured[0]["content"]
    assert "Distinct passage 1." in text


@pytest.mark.parametrize(
    "instruction", ["Correct causality only.", "Correct causality; keep under 150 words."]
)
def test_targeted_correction_does_not_expand_to_an_allocated_plan_budget(
    db, targeted, monkeypatch, instruction
):
    _, part_id, source = targeted
    artifact_id = artifacts.get_part(db, part_id)["artifact_id"]
    job = writer_pipeline.PassJob(artifact_id, section_refs=("1.2",), instruction=instruction)
    config = TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768)
    calls = []

    def complete(*args, **kwargs):
        calls.append(args)
        return "## Evidence\n\nI want the counts to answer only what was measured."

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline._run_section(
        db,
        job,
        artifacts.get_artifact(db, artifact_id),
        config,
        int(artifacts.get_artifact(db, artifact_id)["class_id"]),
        part_id,
        "1.2",
        "Evidence",
        target_words=150,
    )
    assert len(calls) == 1


def test_existing_section_prompt_prioritizes_correction_over_craft_expansion():
    messages = prompts.build_section_prompt(
        "Memo",
        "1 Evidence",
        "## Evidence\nMy distinctive sentence.",
        None,
        None,
        "Correct causality only.",
        "Soft total target: 450 words",
        "",
        "",
        target_words=150,
        preserve_existing=True,
    )
    assert "A section that stops short" not in messages[0]["content"]
    assert "develop the material to reach it" not in messages[1]["content"]


def test_targeted_explicit_length_still_requires_completion(db, targeted, monkeypatch):
    _, part_id, _ = targeted
    artifact_id = artifacts.get_part(db, part_id)["artifact_id"]
    job = writer_pipeline.PassJob(
        artifact_id, section_refs=("1.2",), instruction="Expand to 150 words."
    )
    calls = []

    def complete(*args, **kwargs):
        calls.append(args)
        return (
            "## Evidence\n\nA very short correction."
            if len(calls) == 1
            else " More support is needed."
        )

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    _, incomplete = writer_pipeline._run_section(
        db,
        job,
        artifacts.get_artifact(db, artifact_id),
        TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768),
        1,
        part_id,
        "1.2",
        "Evidence",
        target_words=150,
    )
    assert len(calls) > 1
    assert incomplete
