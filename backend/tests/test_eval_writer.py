"""Harness invariants, including a real subprocess and real durable HTTP run."""

import argparse
import json

import pytest

from scripts.eval_writer import (
    ROOT,
    DesktopBackend,
    capture,
    configuration,
    deterministic_metrics,
    expand_body,
    isolated_environment,
    redact,
    run_case,
    section_boundary,
    seed,
)
from scripts.writer_eval_provider import FaultProvider


def test_explicit_profile_overrides_inherited_student_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LYRA_DB_PATH", "/student/private.db")
    monkeypatch.setenv("LYRA_SOURCE_DB_PATH", "/student/source.db")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.macOS.Keyring")
    env = isolated_environment(tmp_path)
    assert env["LYRA_DB_PATH"] == str(tmp_path / "data/lyra.db")
    assert "LYRA_SOURCE_DB_PATH" not in env
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def test_expand_long_draft_keeps_original_and_unique_student_observations():
    corpus = json.loads((ROOT / "scripts/eval_corpora/writer_quality.v1.json").read_text())
    case = next(c for c in corpus["cases"] if "body_repeat" in c)
    expanded = expand_body(case)
    assert expanded.startswith(case["body"])
    assert "observation 1," in expanded
    assert "observation 160," in expanded
    assert len(expanded) > len(case["body"]) + 50000


def test_metrics_do_not_confuse_preserved_body_with_reviewed_proposal():
    result = deterministic_metrics(
        {"expected": {"protected_passages": ["Keep this."]}},
        "Keep this.",
        {"draft": {"body": "Keep this."}, "status": {"run_id": 9}, "comments": []},
    )
    assert result["body_unchanged"]
    assert result["real_run_id_present"]
    assert result["unsupported_rewrites"] is None
    assert result["missed_seeded_issues"] is None


def test_config_requires_explicit_remote_consent():
    args = argparse.Namespace(
        config_db=None, endpoint="https://synthetic.invalid/v1", model="fixture", allow_remote=False
    )
    with pytest.raises(ValueError, match="explicit --allow-remote"):
        configuration(args)


@pytest.mark.parametrize(
    "scenario", ["uninterrupted", "restart_inference", "cancel_retry", "restart_persistence"]
)
def test_durable_http_review_has_real_run_id_and_preserves_prose(scenario):
    provider = FaultProvider(delay=0.01)
    case = {
        "id": "http_contract",
        "operation": "review",
        "brief": {"summary": "Check the observation without inventing evidence."},
        "body": "# Observation\n\nI waited by the gate and counted three trips.\n",
        "sources": [],
        "review_payload": {"depth": "quick"},
        "expected": {"protected_passages": ["I waited by the gate"]},
    }
    args = argparse.Namespace(
        source_root=ROOT,
        backend_executable=None,
        dimensions=["student_voice"],
        context_window=32768,
        allow_remote=False,
        api_key_env="UNUSED_EVAL_KEY",
        timeout=15,
        interrupt_after=0.1,
    )
    try:
        result = run_case(
            args,
            case,
            scenario,
            {"endpoint_url": provider.endpoint, "model": "synthetic-writer-v1"},
            provider,
        )
    finally:
        provider.close()
    assert result["status"] == "recorded", result
    assert result["after"]["status"]["run_id"] > 0
    assert result["after"]["status"]["run_status"] in {"completed", "partial"}
    assert result["deterministic"]["body_unchanged"]
    assert result["subjective_scores"]["student_voice"] is None
    assert result["human_review"] is None
    assert len(provider.requests) > 1
    if scenario == "cancel_retry":
        assert result["after_interruption"]["status"]["run_status"] == "cancelled"
        assert (
            result["after"]["status"]["run_id"] != result["after_interruption"]["status"]["run_id"]
        )
    if scenario != "uninterrupted":
        assert result["intervention_observed"]
    assert result["deterministic"]["duplicate_comment_signatures"] == 0
    if scenario == "restart_persistence":
        assert result["observed_boundary"]["kind"] == "new_comment_observed_via_http"
        ids = result["observed_boundary"]["new_comment_ids"]
        assert ids
        assert set(ids) <= {c["id"] for c in result["after"]["comments"]}


def test_redacts_nested_provider_echoes_without_scrubbing_synthetic_sources():
    value = {
        "error": "failed at http://private.internal/v1/chat with fixture-secret",
        "sources": ["https://synthetic.invalid/source", "private.internal"],
    }
    clean = redact(value, ["http://private.internal/v1", "private.internal", "fixture-secret"])
    assert "private.internal" not in json.dumps(clean)
    assert "fixture-secret" not in json.dumps(clean)
    assert clean["sources"][0] == value["sources"][0]


def test_stream_fault_is_not_consumed_by_nonstream_review_probe():
    import http.client
    from urllib.parse import urlsplit

    provider = FaultProvider(delay=0)
    try:
        provider.arm("partial_stream")
        url = urlsplit(provider.endpoint)
        connection = http.client.HTTPConnection(url.hostname, url.port)
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"stream": False}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert provider.fault == "partial_stream"
        assert provider.requests[-1]["fault"] is None
    finally:
        provider.close()


def test_course_source_deletion_uses_http_and_preserves_saved_evidence(tmp_path):
    case = {
        "id": "source_delete_http",
        "operation": "review",
        "brief": {"summary": "Compare only the recorded observations."},
        "body": "# Notes\n\nI counted three trips.\n",
        "sources": [
            {
                "key": "memo",
                "source_type": "course",
                "title": "Synthetic memo",
                "content": "Three trips were recorded. No attendance was measured.",
                "excerpts": [{"text": "No attendance was measured."}],
            }
        ],
        "source_mutations": [{"source_key": "memo", "operation": "delete_document"}],
    }
    backend = DesktopBackend(tmp_path, ROOT)
    try:
        backend.start()
        aid, cid, _body = seed(backend, case)
        result = capture(backend, aid, cid)
        source = result["sources"][0]
        assert source["document_id"] is None
        assert source["source_type"] == "course"
        assert source["excerpts"][0]["excerpt"] == "No attendance was measured."
        assert (
            result["durable_rows"]["writer_sources"][0]["snapshot"] == case["sources"][0]["content"]
        )
    finally:
        backend.stop()


def test_section_boundary_does_not_confuse_a_paragraph_with_a_section():
    status = {"run_id": 7, "problems_done": 1}
    live = {
        "run_id": 7,
        "blocks": [
            {"id": 1, "section_ref": "1", "status": "complete"},
            {"id": 2, "section_ref": "1", "status": "drafting"},
            {"id": 3, "section_ref": "2", "status": "pending"},
        ],
    }
    assert section_boundary(status, live, "pass", {}) is None
    live["blocks"][1]["status"] = "complete"
    boundary = section_boundary(status, live, "pass", {})
    assert boundary["completed_section_ref"] == "1"
    assert boundary["next_section_ref"] == "2"
    assert boundary["completed_block_ids"] == [1, 2]
    assert section_boundary(status, live, "review", {}) is None
    assert section_boundary(status, {**live, "run_id": 6}, "pass", {}) is None
    assert (
        section_boundary(status, None, "pass", {"sections": ["1", "2"]})["kind"]
        == "selected_section_counter"
    )
    assert section_boundary(status, None, "review", {"sections": ["1", "2"]}) is None


@pytest.mark.parametrize(
    "scenario", ["restart_between_sections", "edit_restart", "edit_cancel_retry"]
)
def test_live_http_section_boundary_and_user_edit_survive_restart(scenario):
    corpus = json.loads((ROOT / "scripts/eval_corpora/writer_quality.v1.json").read_text())
    case = next(c for c in corpus["cases"] if c["id"] == "full_live_draft_from_student_notes")
    provider = FaultProvider(delay=0.2)
    args = argparse.Namespace(
        source_root=ROOT,
        backend_executable=None,
        dimensions=["student_voice"],
        context_window=32768,
        allow_remote=False,
        api_key_env="UNUSED_EVAL_KEY",
        timeout=30,
        interrupt_after=0.1,
    )
    try:
        result = run_case(
            args,
            case,
            scenario,
            {"endpoint_url": provider.endpoint, "model": "synthetic-writer-v1"},
            provider,
        )
    finally:
        provider.close()
    assert result["status"] == "recorded", result
    assert result["intervention_observed"], result
    assert result["deterministic"]["body_unchanged"]
    assert result["after"]["status"]["run_id"] >= 1
    if scenario == "restart_between_sections":
        boundary = result["observed_boundary"]
        assert boundary["completed_section_ref"] != boundary["next_section_ref"]
        assert boundary["kind"] == "complete_section_before_next_unfinished_section"
    else:
        assert result["user_edit"]["user_revision"] > 0
        assert result["user_edit"]["edited_content"] != result["user_edit"]["original_content"]
        assert result["deterministic"]["current_live_user_edit_preserved"], result
        assert result["deterministic"]["historical_user_edit_preserved"], result
        if scenario == "edit_cancel_retry":
            assert result["after_interruption"]["status"]["run_status"] == "cancelled"
            predecessor = result["after_interruption"]["status"]["run_id"]
            successor = result["after"]["status"]["run_id"]
            assert successor != predecessor
            assert result["after"]["status"]["run_status"] == "completed", result
            assert result["after"]["live_suggestion"]["run_id"] == successor
            assert any(
                b["user_revision"] > 0 and b["content"] == result["user_edit"]["edited_content"]
                for b in result["after"]["live_suggestion"]["blocks"]
            )
