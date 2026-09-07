"""Full live proposal planning must retain instructions and available saved evidence."""

import json

import pytest

from backend.core import artifacts, briefs, source_ledger, writer_pipeline
from backend.core.app_settings import TutorConfig
from backend.llm import prompts
from backend.rag.retrieve import RetrievalResult
from backend.tests.test_writer_pipeline import _draft

BODY = "# My shuttle memo notes\n\nI want the count to answer only the question we measured."
REQUEST = "Create a separate proposal with exactly two sections: Evidence and Recommendation."
BRIEF = "Use the boarding memo and preserve uncertainty; do not assert an attendance effect."
CAVEAT = "Boardings count trips, not unique people. No attendance data were collected."


@pytest.fixture
def planned(db, class_id, monkeypatch):
    artifact_id, part_id = _draft(db, class_id, BODY)
    briefs.save_brief(
        db, artifact_id, summary=BRIEF, length_target="320 words", status=briefs.CONFIRMED
    )
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic boarding memo",
        url="https://synthetic.invalid/memo",
        snapshot=CAVEAT,
    )
    monkeypatch.setattr(
        writer_pipeline,
        "retrieve",
        lambda *a: RetrievalResult(
            chunks=[],
            trimmed=False,
            omitted_document_count=0,
        ),
    )
    captured = {}
    analysis = {"task": "Choose two policy moves", "criteria": ["Separate inference from action"]}

    def complete(config, messages, schema=None, **kwargs):
        captured[schema.name] = messages
        if schema is prompts.PLAN_BRIEF_SCHEMA:
            return json.dumps(analysis)
        if schema is prompts.PLAN_THESIS_SCHEMA:
            return json.dumps(
                {
                    "selected": "Extend conditionally.",
                    "candidates": [],
                    "rationale": "Limited evidence.",
                }
            )
        if schema is prompts.PLAN_ARGUMENT_SCHEMA:
            return '[{"claim":"Measure access before generalizing."}]'
        assert schema is prompts.PLAN_SECTIONS_SCHEMA
        return json.dumps(
            {
                "sections": [
                    {
                        "ref": "1.1",
                        "title": "Evidence",
                        "job": "Explain observation limits",
                        "word_budget": 160,
                    },
                    {
                        "ref": "1.2",
                        "title": "Recommendation",
                        "job": "Recommend conditional extension",
                        "word_budget": 160,
                    },
                ]
            }
        )

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    plan = writer_pipeline._build_live_plan(
        db,
        writer_pipeline.PassJob(artifact_id, instruction=REQUEST),
        artifacts.get_artifact(db, artifact_id),
        TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768),
        class_id,
        part_id,
    )
    return captured, source, part_id, plan


def test_final_section_planning_retains_brief_request_analysis_without_forcing_notes_outline(
    db, planned
):
    captured, _, part_id, plan = planned
    text = "\n".join(m["content"] for m in captured[prompts.PLAN_SECTIONS_SCHEMA.name])
    assert REQUEST in text
    assert BRIEF in text
    assert "Separate inference from action" in text
    assert "My shuttle memo notes" in text
    assert "Preserve every heading" not in text
    assert "reference" in text.lower()
    assert [s["title"] for s in plan["sections"]] == ["Evidence", "Recommendation"]
    assert artifacts.get_part(db, part_id)["content"] == BODY


@pytest.mark.parametrize(
    "schema_name",
    [
        prompts.PLAN_THESIS_SCHEMA.name,
        prompts.PLAN_ARGUMENT_SCHEMA.name,
        prompts.PLAN_SECTIONS_SCHEMA.name,
    ],
)
def test_strategy_stages_receive_saved_snapshot_caveat_and_revision(db, planned, schema_name):
    captured, source, _, _ = planned
    text = "\n".join(m["content"] for m in captured[schema_name])
    assert CAVEAT in text
    assert f'"source_revision_id": {source["current_revision_id"]}' in text
    assert '"revision": 1' in text
    assert '"omitted": false' in text
    assert source_ledger.get_source(db, source["id"])["snapshot"] == CAVEAT
