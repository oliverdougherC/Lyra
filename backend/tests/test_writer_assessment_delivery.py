"""Finite assessment recovery through the real client and HTTP-created writer jobs."""

import json

import httpx
import pytest

from backend.core import artifacts, live_drafts, writer_pipeline, writer_runs
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.llm import client, prompts
from backend.tests.test_writer_beta_live import live_run as live_run


@pytest.fixture
def endpoint(monkeypatch):
    client.reset_json_support()
    responses = []
    requests = []
    real_complete = client.complete

    def handler(request):
        requests.append(json.loads(request.content))
        text, finish = responses.pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": text},
                        "finish_reason": finish,
                    }
                ]
            },
        )

    async def complete(*args, **kwargs):
        return await real_complete(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client, "complete", complete)
    yield responses, requests
    client.reset_json_support()


def assessment(schema, overlong=False):
    if schema is prompts.SKEPTIC_SCHEMA:
        return json.dumps(
            {
                "passes": not overlong,
                "faults": ["x" * 10215] if overlong else [],
                "rewrite_instruction": "Check the supplied evidence." if overlong else "",
            }
        )
    return json.dumps(
        {
            "summary": "The supplied claims are supported.",
            "issues": [
                {
                    "block_key": "1:p1",
                    "problem": "x" * 10215,
                    "revision_instruction": "Check the supplied evidence.",
                }
            ]
            if overlong
            else [],
        }
    )


@pytest.mark.parametrize("schema", [prompts.SKEPTIC_SCHEMA, prompts.OVERALL_ASSESSMENT_SCHEMA])
@pytest.mark.parametrize("failure", ["length", "overlong"])
def test_assessment_recovers_once_without_raising_cap(endpoint, schema, failure):
    responses, requests = endpoint
    good = assessment(schema)
    responses.extend(
        [
            (
                good if failure == "length" else assessment(schema, True),
                "length" if failure == "length" else "stop",
            ),
            (good, "stop"),
        ]
    )
    messages = [{"role": "user", "content": "Original source revision and student wording."}]
    assert (
        writer_pipeline._complete(
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 32768), messages, schema=schema
        )
        == good
    )
    assert len(requests) == 2
    assert all(r["max_tokens"] == 4096 for r in requests)
    assert requests[1]["messages"][:-1] == messages
    assert requests[0]["response_format"] == requests[1]["response_format"]
    assert all(r["chat_template_kwargs"] == {"enable_thinking": False} for r in requests)
    assert messages == [
        {"role": "user", "content": "Original source revision and student wording."}
    ]


@pytest.mark.parametrize("failure", ["length", "overlong"])
def test_http_live_review_exhaustion_keeps_prose_and_run_incomplete(
    live_run, db, endpoint, failure
):
    http, job, suggestion_id, original = live_run
    responses, requests = endpoint
    writer_runs.checkpoint(db, job.run_id, stage="reviewing")
    before = [b["content"] for b in live_drafts.get_live_suggestion(db, suggestion_id)["blocks"]]
    invalid = (
        assessment(prompts.OVERALL_ASSESSMENT_SCHEMA, failure == "overlong"),
        "length" if failure == "length" else "stop",
    )
    responses.extend([invalid, invalid])
    writer_pipeline.run_pass(job)
    assert len(requests) == 2
    assert http.get(f"/api/drafts/{job.artifact_id}/status").json()["run_status"] == "failed"
    assert http.get(f"/api/drafts/{job.artifact_id}").json()["body"] == original
    blocks = live_drafts.get_live_suggestion(db, suggestion_id)["blocks"]
    assert [b["content"] for b in blocks] == before
    assert not any(b["metadata"].get("overall_assessment", {}).get("completed") for b in blocks)


@pytest.mark.parametrize("first", ["normal", "length", "overlong"])
@pytest.mark.parametrize("correct", [False, True])
def test_http_live_normal_assessment_and_empty_correction_preserve_prose(
    live_run, db, endpoint, first, correct
):
    _, job, suggestion_id, original = live_run
    responses, requests = endpoint
    before = [b["content"] for b in live_drafts.get_live_suggestion(db, suggestion_id)["blocks"]]
    if first != "normal":
        responses.append(
            (
                assessment(prompts.OVERALL_ASSESSMENT_SCHEMA, first == "overlong"),
                "length" if first == "length" else "stop",
            )
        )
    responses.extend(
        [
            (
                json.dumps(
                    {
                        "summary": "Verify one claim.",
                        "issues": [
                            {
                                "block_key": "1:p1",
                                "problem": "Verify whether the survey measured riders.",
                                "revision_instruction": (
                                    "Check the claim against the source; keep it if supported."
                                ),
                            }
                        ],
                    }
                ),
                "stop",
            ),
            (
                json.dumps(
                    {
                        "edits": [
                            {
                                "before": "Distinct passage 1.",
                                "after": "Precisely corrected passage 1.",
                            }
                        ]
                        if correct
                        else []
                    }
                ),
                "stop",
            ),
        ]
    )
    writer_pipeline._review_live_chunks(
        db,
        job,
        artifacts.get_artifact(db, job.artifact_id),
        TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 32768),
        artifacts.get_artifact(db, job.artifact_id)["class_id"],
        suggestion_id,
        {"sections": []},
        "Keep the student's wording.",
    )
    blocks = live_drafts.get_live_suggestion(db, suggestion_id)["blocks"]
    expected = [
        before[0].replace("Distinct passage 1.", "Precisely corrected passage 1.")
        if correct
        else before[0],
        *before[1:],
    ]
    assert [b["content"] for b in blocks] == expected
    assert all(b["metadata"]["overall_assessment"]["completed"] for b in blocks)
    assert len(requests) == (2 if first == "normal" else 3)


@pytest.mark.parametrize("schema", [prompts.SKEPTIC_SCHEMA, prompts.OVERALL_ASSESSMENT_SCHEMA])
def test_second_truncated_assessment_is_never_accepted(endpoint, schema):
    responses, requests = endpoint
    responses.extend([(assessment(schema), "length")] * 2)
    with pytest.raises(LyraError, match="review is incomplete"):
        writer_pipeline._complete(
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 32768),
            [{"role": "user", "content": "Student prose and source evidence."}],
            schema=schema,
        )
    assert len(requests) == 2


def test_other_structured_stage_does_not_retry_or_accept_cutoff(endpoint):
    responses, requests = endpoint
    responses.append(("{}", "length"))
    with pytest.raises(LyraError):
        writer_pipeline._complete(
            TutorConfig("http://127.0.0.1:9/v1", None, "fixture", 32768),
            [{"role": "user", "content": "Student prose."}],
            schema=prompts.SCOPED_REVISION_SCHEMA,
        )
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["length", "overlong"])
def test_http_live_recovered_assessment_records_initial_failure(live_run, db, endpoint, failure):
    http, job, suggestion_id, original = live_run
    responses, requests = endpoint
    writer_runs.checkpoint(db, job.run_id, stage="reviewing")
    responses.extend(
        [
            (
                assessment(prompts.OVERALL_ASSESSMENT_SCHEMA, failure == "overlong"),
                "length" if failure == "length" else "stop",
            ),
            (assessment(prompts.OVERALL_ASSESSMENT_SCHEMA), "stop"),
        ]
    )
    writer_pipeline.run_pass(job)
    assert len(requests) == 2
    run = writer_runs.get_run(db, job.run_id)
    assert run["status"] == "completed"
    assert any(
        w["code"]
        == ("assessment_retry_cutoff" if failure == "length" else "assessment_retry_field_limit")
        for w in run["warnings"]
    )
    assert http.get(f"/api/drafts/{job.artifact_id}").json()["body"] == original
