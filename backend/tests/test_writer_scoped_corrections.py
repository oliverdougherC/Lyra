"""Only exact, unambiguous correction spans may alter an existing passage."""

import json

import pytest

from backend.core import writer_pipeline
from backend.core.errors import LyraError


def apply(original, edits):
    return writer_pipeline._apply_scoped_revision(original, json.dumps({"edits": edits}), [1])


def test_only_requested_spans_change_and_unrelated_math_voice_survive():
    original = (
        "I keep my odd phrase and \\(x\\). Trips prove attendance. "
        "Fourteen riders valued it [@lyra:1]."
    )
    result = apply(
        original,
        [{"before": "Trips prove attendance.", "after": "Trips do not measure attendance."}],
    )
    assert result == original.replace("Trips prove attendance.", "Trips do not measure attendance.")
    assert apply(original, []) == original


@pytest.mark.parametrize(
    "edits",
    [
        [{"before": "missing", "after": "new"}],
        [{"before": "", "after": "new"}],
        [{"before": "one", "after": "new"}],
        [{"before": "one two", "after": "x"}, {"before": "two", "after": "y"}],
        [{"before": "two", "after": "claim [@99]"}],
    ],
)
def test_ambiguous_overlapping_or_unsupported_edits_fail(edits):
    with pytest.raises(LyraError):
        apply("one two one", edits)


def test_unreadable_edit_is_not_an_unchanged_success():
    with pytest.raises(LyraError):
        writer_pipeline._apply_scoped_revision("Saved paragraph.", "not json", [1])


def test_changed_passage_is_not_replaced_by_a_stale_quoted_correction(db, class_id, monkeypatch):
    from backend.core import artifacts, suggestions
    from backend.core.app_settings import TutorConfig
    from backend.rag.retrieve import RetrievalResult
    from backend.tests.test_writer_pipeline import _draft

    body = "# Memo\n\n## Evidence\n\nTrips prove attendance. Keep my wording.\n"
    artifact_id, part_id = _draft(db, class_id, body)
    changed = body.replace("Keep my wording.", "Keep the new sentence I just wrote.")
    monkeypatch.setattr(
        writer_pipeline,
        "retrieve",
        lambda *a: RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0),
    )

    def complete(*args, **kwargs):
        artifacts.set_part_content(db, part_id, changed, artifacts.USER_CORRECTED)
        return json.dumps(
            {
                "edits": [
                    {
                        "before": "Trips prove attendance.",
                        "after": "Trips do not measure attendance.",
                    }
                ]
            }
        )

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    with pytest.raises(LyraError, match="changed during correction"):
        writer_pipeline._run_section(
            db,
            writer_pipeline.PassJob(artifact_id, section_refs=("1.1",)),
            artifacts.get_artifact(db, artifact_id),
            TutorConfig("http://127.0.0.1:9/v1", None, "synthetic", 32768),
            class_id,
            part_id,
            "1.1",
            "Evidence",
        )
    assert artifacts.get_part(db, part_id)["content"] == changed
    assert suggestions.pending_for_part(db, part_id) is None
