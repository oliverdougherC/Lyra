"""Targeted model replacements cannot invent source IDs or carry neighboring sections."""

import copy
import json

import pytest

from backend.core import artifacts, source_ledger, suggestions, writer_pipeline
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.llm import prompts
from backend.rag.retrieve import RetrievalResult
from backend.tests.test_writer_pipeline import _draft

BODY = (
    "# Bus memo\n\n## Introduction\n\nMy opening voice, with my own citation [@99].\n\n"
    "## Evidence\n\nI want the boarding counts to earn their place in this paragraph.\n\n"
    "## Recommendation\n\nKeep my cautious recommendation exactly.\n"
)


@pytest.fixture
def targeted(db, class_id, monkeypatch):
    artifact_id, part_id = _draft(db, class_id, BODY)
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.WEB,
        title="Synthetic memo",
        url="https://synthetic.invalid/memo",
        snapshot="Boardings count trips, not unique people.",
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

    def execute(reply):
        return writer_pipeline._run_section(
            db,
            writer_pipeline.PassJob(artifact_id, section_refs=("1.2",)),
            artifacts.get_artifact(db, artifact_id),
            TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768),
            class_id,
            part_id,
            "1.2",
            "Evidence",
            reply_override=reply,
        )

    return execute, part_id, source


@pytest.mark.parametrize("marker", ["[@4]", "[@lyra:11]"])
def test_unknown_targeted_citation_is_rejected_before_proposal(db, targeted, marker):
    execute, part_id, _ = targeted
    with pytest.raises((ValueError, LyraError), match="source"):
        execute(f"## Evidence\n\nThe count describes trips {marker}.")
    assert artifacts.get_part(db, part_id)["content"] == BODY
    assert suggestions.pending_for_part(db, part_id) is None


@pytest.mark.parametrize(
    "extra",
    [
        "## Recommendation\n\nReplace the conclusion.",
        "Recommendation\n--------------\n\nRepeated neighboring conclusion.",
        "## Invented section\n\nUnrequested material.",
        "## Evidence\n\nRepeat the requested section again.",
    ],
)
def test_neighbor_foreign_or_duplicate_heading_is_rejected(db, targeted, extra):
    execute, part_id, _ = targeted
    with pytest.raises(LyraError, match="section|heading"):
        execute("## Evidence\n\nCounts describe trips.\n\n" + extra)
    assert artifacts.get_part(db, part_id)["content"] == BODY
    assert suggestions.pending_for_part(db, part_id) is None


def test_own_heading_and_known_citation_preserve_neighbors_and_user_markup(db, targeted):
    execute, part_id, source = targeted
    execute(f"## Evidence\n\nI count trips, not people [@{source['id']}].")
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    proposed = pending["proposed_content"]
    assert f"[@lyra:{source['id']}]" in proposed
    assert "My opening voice, with my own citation [@99]." in proposed
    assert proposed.count("## Recommendation") == 1
    assert "Keep my cautious recommendation exactly." in proposed
    assert artifacts.get_part(db, part_id)["content"] == BODY


def test_fenced_heading_like_code_is_not_a_foreign_section(db, targeted):
    execute, part_id, _ = targeted
    execute("## Evidence\n\nIllustration:\n```markdown\n## Recommendation\n```\n")
    assert suggestions.pending_for_part(db, part_id) is not None


def test_ledger_disambiguates_source_excerpt_and_revision_identifiers():
    entries = [
        {
            "id": 2,
            "title": "Synthetic memo",
            "excerpts": [
                {
                    "id": 11,
                    "source_revision_id": 7,
                    "supporting_revision": 3,
                    "supporting_accessed_at": "2026-09-06",
                    "excerpt": "Boardings count trips.",
                }
            ],
        }
    ]
    before = copy.deepcopy(entries)
    rendered = prompts.format_ledger_block(entries)
    payload = json.loads(rendered.split("\n", 1)[1])
    assert payload[0]["source_id"] == 2
    assert payload[0]["citation_marker"] == "[@lyra:2]"
    assert payload[0]["excerpts"][0]["excerpt_id"] == 11
    assert "id" not in payload[0]["excerpts"][0]
    assert payload[0]["excerpts"][0]["source_revision_id"] == 7
    assert payload[0]["excerpts"][0]["supporting_revision"] == 3
    assert payload[0]["excerpts"][0]["supporting_accessed_at"] == "2026-09-06"
    assert entries == before


@pytest.mark.parametrize("heading", ["", "## 1.2 Evidence\n\n", "## 2. Evidence\n\n"])
def test_normal_prose_and_own_numbered_heading_are_allowed(db, targeted, heading):
    execute, part_id, source = targeted
    execute(heading + f"I count trips, not people [@{source['id']}].")
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert f"[@lyra:{source['id']}]" in pending["proposed_content"]
    assert "My opening voice, with my own citation [@99]." in pending["proposed_content"]
    assert pending["proposed_content"].count("## Recommendation") == 1
    assert artifacts.get_part(db, part_id)["content"] == BODY
