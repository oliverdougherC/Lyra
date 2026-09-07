"""Comparison keeps recovery invariants separate from ungraded model quality."""

import hashlib
import json

import pytest

from scripts.compare_writer_evals import compare, main


def _case(scenario="uninterrupted", case_id="memo", **changes):
    value = {
        "case_id": case_id,
        "scenario": scenario,
        "status": "recorded",
        "plan_mode": "corpus_seeded",
        "effective_context_window": 8192,
        "evidence_kind": "deterministic_fault_provider",
        "subjective_scores": {"student_voice": None, "revision_precision": None},
        "after": {"status": {"run_status": "completed"}, "pending": {"proposed_content": "One."}},
        "deterministic": {"missed_seeded_issues": None, "unsupported_rewrites": None},
    }
    value.update(changes)
    return value


def _write(directory, cases, **config):
    directory.mkdir()
    corpus = b'{"version":"synthetic-test"}'
    (directory / "corpus.json").write_bytes(corpus)
    report = {
        "version": "writer-evaluation.v1",
        "corpus_sha256": hashlib.sha256(corpus).hexdigest(),
        "generate_plan": False,
        "locality": "loopback",
        "context_window": 8192,
        "model": "same-model",
        "cases": [],
    }
    report.update(config)
    for case in cases:
        filename = case["case_id"] + "--" + case["scenario"] + ".json"
        (directory / filename).write_text(json.dumps(case))
        report["cases"].append(
            {"case_id": case["case_id"], "scenario": case["scenario"], "file": filename}
        )
    (directory / "report.json").write_text(json.dumps(report))
    return directory


def test_interrupted_run_matches_uninterrupted_without_inventing_grades(tmp_path):
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [_case("restart_inference", harness_timeout=True)])
    result = compare(baseline, candidate)
    pair = result["pairs"][0]
    assert pair["baseline_scenario"] == "uninterrupted"
    assert pair["candidate_scenario"] == "restart_inference"
    assert pair["dimensions"]["student_voice"] == {"baseline": None, "candidate": None}
    assert pair["candidate"]["deterministic"]["unsupported_rewrites"] is None
    assert pair["candidate"]["harness_timeout"] is True
    assert pair["candidate"]["terminal_status"] == "completed"
    assert result["acceptance"] == "not_determined"


@pytest.mark.parametrize(
    "field,value", [("model", "other"), ("context_window", 4096), ("generate_plan", True)]
)
def test_default_refuses_changed_configuration(tmp_path, field, value):
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [_case()], **{field: value})
    with pytest.raises(ValueError, match=field):
        compare(baseline, candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("effective_context_window", 4096),
        ("plan_mode", "generated"),
        ("evidence_kind", "real_provider"),
    ],
)
def test_case_configuration_cannot_hide_behind_equal_report_defaults(tmp_path, field, value):
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [_case(**{field: value})])
    with pytest.raises(ValueError, match=field):
        compare(baseline, candidate)


def test_explicit_model_comparison_never_substitutes_an_interruption_control(tmp_path):
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [_case(), _case("restart_inference")], model="different")
    result = compare(baseline, candidate, model_comparison=True)
    assert result["comparison_kind"] == "model_comparison"
    assert len(result["pairs"]) == 1
    assert result["missing_baseline"] == [["memo", "restart_inference"]]


def test_settled_history_survives_successor_and_duplicates_are_counted(tmp_path):
    settled = [
        {"stable_key": "1:p1", "content": "Preserved.", "status": "complete"},
        {"stable_key": "2:p1", "content": "My edit.", "user_revision": 1},
    ]
    case = _case("cancel_retry", at_interruption={"live_suggestion": {"id": 4, "blocks": settled}})
    case["after"] = {
        "status": {"run_status": "completed"},
        "pending": {"proposed_content": "# Title\n\nOne.\n\nOne.\n\nTwo."},
        "live_suggestion": {"id": 5, "blocks": []},
        "durable_rows": {
            "live_draft_blocks": [
                {**settled[0], "suggestion_id": 4},
                {**settled[1], "suggestion_id": 4, "content": "Overwritten."},
            ]
        },
    }
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [case])
    pair = compare(baseline, candidate)["pairs"][0]
    assert pair["duplicate_paragraph_delta"] == 1
    retained = pair["candidate"]["settled_block_preservation"]
    assert retained["preserved"] == ["1:p1"]
    assert retained["changed"] == ["2:p1"]


def test_missing_cases_and_runnable_json_output(tmp_path):
    baseline = _write(tmp_path / "old", [_case(case_id="old-only")])
    candidate = _write(tmp_path / "new", [_case(case_id="new-only")])
    output = tmp_path / "comparison.json"
    assert (
        main(["--baseline", str(baseline), "--candidate", str(candidate), "--output", str(output)])
        == 0
    )
    report = json.loads(output.read_text())
    assert report["missing_baseline"] == [["new-only", "uninterrupted"]]
    assert report["missing_candidate"] == [["old-only", "uninterrupted"]]
    assert report["pairs"] == []


def test_modified_corpus_is_refused(tmp_path):
    baseline = _write(tmp_path / "old", [_case()])
    candidate = _write(tmp_path / "new", [_case()])
    (candidate / "corpus.json").write_text("changed corpus")
    with pytest.raises(ValueError, match="Corpus bytes"):
        compare(baseline, candidate)
