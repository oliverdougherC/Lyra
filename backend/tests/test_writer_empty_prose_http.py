"""Empty or off-target revisions must fail honestly through a real desktop HTTP run.

Only the local provider is scripted. Planning, workers, HTTP routes, storage and
completion checks run in the actual isolated backend subprocess, without patches.
"""

import io
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_writer import ROOT, DesktopBackend, capture, seed, source_digest
from scripts.writer_eval_provider import FaultProvider


class _EmptyRevisionProvider(FaultProvider):
    """Delegate ordinary requests; target only a requested post-assessment revision."""

    def __init__(self, revision_case: str = "empty") -> None:
        super().__init__(delay=0.01)
        self.revision_case = revision_case
        self.before_revision: Callable[[], dict[str, Any]] | None = None
        self.revision_snapshots: list[dict[str, Any]] = []
        self.revision_prompts: list[str] = []
        self.assessments = 0
        self.revision_protocol: str | None = None
        self.revision_response: object = None
        owner = self
        ordinary_handler = self.server.RequestHandlerClass

        class Handler(ordinary_handler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                spec = payload.get("response_format", {}).get("json_schema", {})
                name = spec.get("name")
                prompt = "\n".join(str(m.get("content", "")) for m in payload.get("messages", []))
                if name == "writer_plan_sections":
                    content = json.dumps(
                        {
                            "sections": [
                                {
                                    "ref": str(index),
                                    "title": title,
                                    "job": "State only what the supplied sources support.",
                                    "claim": "The observation does not establish causation.",
                                    "evidence": [],
                                    "source_ids": [1],
                                    "word_budget": 160,
                                }
                                for index, title in enumerate(("Evidence", "Recommendation"), 1)
                            ]
                        }
                    )
                    event = "normal_planning"
                elif name == "writer_overall_assessment":
                    owner.assessments += 1
                    content = json.dumps(
                        {
                            "summary": "The opening paragraph needs a narrower opening sentence.",
                            "issues": [
                                {
                                    "block_key": "1:p1",
                                    "problem": "The opening overstates the observation.",
                                    "revision_instruction": (
                                        "Make the opening sentence more tentative without changing "
                                        "its supporting evidence or the rest of the paragraph."
                                    ),
                                }
                            ],
                        }
                    )
                    event = "actionable_overall_assessment"
                elif owner.assessments and (
                    name == "writer_scoped_revision"
                    or (not spec and "revise only" in prompt.lower())
                ):
                    if owner.before_revision is None:
                        raise RuntimeError("Revision snapshot callback was not configured")
                    owner.revision_snapshots.append(owner.before_revision())
                    owner.revision_prompts.append(prompt)
                    if name == "writer_scoped_revision":
                        paragraph = next(
                            b
                            for b in owner.revision_snapshots[-1]["blocks"]
                            if b["stable_key"] == "1:p1"
                        )
                        if owner.revision_case == "off_target":
                            other = next(
                                b
                                for b in owner.revision_snapshots[-1]["blocks"]
                                if b["stable_key"] != "1:p1" and b["content"]
                            )
                            owner.revision_protocol = "scoped_off_target_span"
                            owner.revision_response = {
                                "edits": [
                                    {
                                        "before": other["content"],
                                        "after": "Use a narrower claim here.",
                                    }
                                ]
                            }
                        else:
                            owner.revision_protocol = "scoped_exact_span_empty_replacement"
                            owner.revision_response = {
                                "edits": [{"before": paragraph["content"], "after": ""}]
                            }

                        content = json.dumps(owner.revision_response)
                    else:
                        if owner.revision_case != "empty":
                            raise RuntimeError(
                                "Off-target coverage requires writer_scoped_revision"
                            )
                        owner.revision_protocol = "plain_empty_prose"
                        owner.revision_response = ""
                        content = ""
                    event = f"{owner.revision_case}_paragraph_revision"
                else:
                    # The ordinary provider still handles probes, research, paragraph
                    # outlines, original prose and transitions over real HTTP/SSE.
                    self.rfile = io.BytesIO(raw)
                    super().do_POST()
                    return
                with owner.lock:
                    owner.requests.append(
                        {
                            "index": len(owner.requests) + 1,
                            "schema": name,
                            "stream": bool(payload.get("stream")),
                            "fault": None,
                            "event": event,
                            "response_format": payload.get("response_format"),
                        }
                    )
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream" if payload.get("stream") else "application/json",
                )
                self.end_headers()
                if payload.get("stream"):
                    response = {
                        "choices": [
                            {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
                        ]
                    }
                    self.wfile.write(
                        ("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode()
                    )
                else:
                    self.wfile.write(
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop",
                                    }
                                ]
                            }
                        ).encode()
                    )

        self.server.RequestHandlerClass = Handler


@pytest.mark.parametrize("revision_case", ["empty", "off_target"])
def test_invalid_requested_paragraph_revision_preserves_saved_prose(
    tmp_path: Path, revision_case: str
):
    corpus = json.loads((ROOT / "scripts/eval_corpora/writer_quality.v1.json").read_text())
    case = dict(next(c for c in corpus["cases"] if c["id"] == "full_live_draft_from_student_notes"))
    case.pop("plan")  # Exercise the actual model-facing planning stages as well.
    source_hash_before = source_digest(ROOT)
    provider = _EmptyRevisionProvider(revision_case)
    backend = DesktopBackend(tmp_path, ROOT, embedding_port=provider.server.server_port)
    try:
        backend.start()
        backend.request(
            "PUT",
            "/api/settings",
            {
                "endpoint_url": provider.endpoint,
                "model": "synthetic-writer-v1",
                "context_window": 32768,
                "api_key": "",
                "remote_ack": False,
                "allow_web_research": False,
                "parallel_requests": False,
            },
        )
        assert backend.request("POST", "/api/settings/test-tools")["ok"]
        aid, cid, original_body = seed(backend, case)
        assert backend.request("GET", f"/api/drafts/{aid}/plan") is None
        provider.before_revision = lambda: backend.request(
            "GET", f"/api/drafts/{aid}/live-suggestion"
        )
        started = backend.request("POST", f"/api/drafts/{aid}/pass", case["pass_payload"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = backend.request("GET", f"/api/drafts/{aid}/status")
            if status["run_status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        after = capture(backend, aid, cid)
        evidence_name = (
            "empty-revision-http-evidence.json"
            if revision_case == "empty"
            else "off-target-revision-http-evidence.json"
        )
        (tmp_path / evidence_name).write_text(
            json.dumps(
                {
                    "revision_case": revision_case,
                    "coverage_scope": (
                        "existing_empty_replacement_guard"
                        if revision_case == "empty"
                        else "new_scoped_protocol_off_target_guard_not_baseline_equivalence"
                    ),
                    "source_root": str(ROOT),
                    "source_sha256_at_start": source_hash_before,
                    "source_sha256_at_end": source_digest(ROOT),
                    "empty_revision_protocol": provider.revision_protocol,
                    "empty_revision_response": provider.revision_response,
                    "start_response": started,
                    "provider_events": provider.requests,
                    "before_empty_revision": provider.revision_snapshots,
                    "after": after,
                },
                indent=2,
            )
            + "\n"
        )
        assert isinstance(after["status"]["run_id"], int)
        assert after["status"]["run_id"] > 0
        assert provider.assessments == 1
        assert len(provider.revision_snapshots) == 1, provider.requests
        events = [r.get("event") for r in provider.requests]
        revision_event = f"{revision_case}_paragraph_revision"
        assert events.index("actionable_overall_assessment") < events.index(revision_event)
        revision_request = next(r for r in provider.requests if r.get("event") == revision_event)
        if provider.revision_protocol == "plain_empty_prose":
            assert revision_request["response_format"] is None
            assert provider.revision_response == ""
        else:
            assert provider.revision_protocol == (
                "scoped_exact_span_empty_replacement"
                if revision_case == "empty"
                else "scoped_off_target_span"
            )
            assert (
                revision_request["response_format"]["json_schema"]["name"]
                == "writer_scoped_revision"
            )
            original_paragraph = next(
                b for b in provider.revision_snapshots[0]["blocks"] if b["stable_key"] == "1:p1"
            )
            if revision_case == "empty":
                assert provider.revision_response == {
                    "edits": [{"before": original_paragraph["content"], "after": ""}]
                }
            else:
                other = next(
                    b
                    for b in provider.revision_snapshots[0]["blocks"]
                    if b["stable_key"] != "1:p1" and b["content"]
                )
                assert provider.revision_response == {
                    "edits": [
                        {
                            "before": other["content"],
                            "after": "Use a narrower claim here.",
                        }
                    ]
                }
                assert other["content"] not in original_paragraph["content"]
        assert source_digest(ROOT) == source_hash_before, "Backend source changed during the test"
        assessment_index = events.index("actionable_overall_assessment")
        assert (
            sum(r["stream"] and r["schema"] is None for r in provider.requests[:assessment_index])
            >= 2
        )
        schemas = {r["schema"] for r in provider.requests}
        assert {
            "writer_plan_brief",
            "writer_plan_thesis",
            "writer_plan_argument",
            "writer_plan_sections",
            "writer_section_research_notes",
            "writer_paragraph_outline",
            "writer_transition_review",
        } <= schemas
        before = provider.revision_snapshots[0]
        original = next(b for b in before["blocks"] if b["stable_key"] == "1:p1")
        current = next(b for b in after["live_suggestion"]["blocks"] if b["id"] == original["id"])
        assert original["status"] == "complete" and original["content"].strip()
        assert original["content"] in provider.revision_prompts[0]
        assert current["content"] == original["content"]
        assert after["draft"]["body"] == original_body
        assert after["status"]["run_status"] == "failed"
        expected_error = "empty revision" if revision_case == "empty" else "one exact passage"
        assert expected_error in after["status"]["error_message"].lower()
        assert after["live_suggestion"]["status"] == "failed"
        assert after["pending"] is None
        if revision_case == "empty":
            assert current["metadata"]["overall_assessment"]["completed"] is False
            assert (
                "no replacement prose"
                in current["metadata"]["overall_assessment"]["revision_skipped"]
            )
        else:
            assert not current["metadata"].get("overall_assessment", {}).get("completed", False)
            assert {b["id"]: b["content"] for b in before["blocks"]} == {
                b["id"]: b["content"] for b in after["live_suggestion"]["blocks"]
            }
        assert after["plan"] is not None
    finally:
        backend.stop()
        provider.close()
