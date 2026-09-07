"""Compare retained writer evaluations without turning recorded runs into acceptance.

Use --baseline OLD --candidate NEW --output comparison.json.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

CONFIG = ("corpus_sha256", "generate_plan", "locality", "context_window", "model")
PROVENANCE = ("tested_sha", "backend_source_sha256", "runtime", "runtime_sha256", "prompt_sha256")


def load(directory):
    report = json.loads((directory / "report.json").read_text())
    if report.get("version") != "writer-evaluation.v1" or any(k not in report for k in CONFIG):
        raise ValueError("Missing or unsupported evaluation configuration")
    if (
        hashlib.sha256((directory / "corpus.json").read_bytes()).hexdigest()
        != report["corpus_sha256"]
    ):
        raise ValueError("Corpus bytes do not match the evaluation receipt")
    cases = {}
    for entry in report["cases"]:
        path = (directory / entry["file"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Case evidence must be inside its evaluation directory")
        case = json.loads(path.read_text())
        key = (entry["case_id"], entry["scenario"])
        if key in cases or key != (case.get("case_id"), case.get("scenario")):
            raise ValueError("Duplicate or mismatched case evidence")
        cases[key] = case
    return report, cases


def blocks(snapshot):
    return (snapshot.get("live_suggestion") or {}).get("blocks", [])


def preservation(case):
    interrupted = case.get("at_interruption")
    if interrupted is None:
        return None
    settled = [
        b for b in blocks(interrupted) if b.get("status") == "complete" or b.get("user_revision", 0)
    ]
    after = case.get("after", {})
    original_id = (interrupted.get("live_suggestion") or {}).get("id")
    candidates = blocks(after)
    if original_id != (after.get("live_suggestion") or {}).get("id"):
        candidates = [
            b
            for b in after.get("durable_rows", {}).get("live_draft_blocks", [])
            if b.get("suggestion_id") == original_id
        ]
    saved = {b.get("stable_key", b.get("block_key")): b for b in candidates}
    changed, missing, preserved = [], [], []
    for block in settled:
        key = block.get("stable_key", block.get("block_key"))
        if key not in saved:
            missing.append(key)
        elif saved[key].get("content") != block.get("content"):
            changed.append(key)
        else:
            preserved.append(key)
    return {
        "eligible": len(settled),
        "preserved": preserved,
        "changed": changed,
        "missing": missing,
        "note": "Exact text comparison within the original suggestion; changes need review.",
    }


def metrics(case):
    after = case.get("after", {})
    proposed = (after.get("pending") or {}).get("proposed_content")
    if proposed is not None:
        basis, text = "pending_proposal", proposed
    elif after.get("live_suggestion"):
        basis, text = "live_suggestion", "\n\n".join(b.get("content", "") for b in blocks(after))
    else:
        basis, text = "accepted_body", after.get("draft", {}).get("body")
    paragraphs = Counter(
        p.strip() for p in (text or "").split("\n\n") if p.strip() and not p.strip().startswith("#")
    )
    return {
        "record_status": case.get("status"),
        "terminal_status": after.get("status", {}).get("run_status"),
        "harness_timeout": case.get("harness_timeout", False),
        "intervention_observed": case.get("intervention_observed"),
        "code_changed_during_case": case.get("code_changed_during_case"),
        "duplicate_paragraphs": {
            "basis": basis,
            "count": sum(n - 1 for n in paragraphs.values()) if text is not None else None,
        },
        "settled_block_preservation": preservation(case),
        "deterministic": case.get("deterministic"),
        "subjective_scores": case.get("subjective_scores"),
        "independent_agent_review": case.get("independent_agent_review"),
        "human_review": case.get("human_review"),
    }


def compare(baseline, candidate, *, model_comparison=False):
    old_report, old = load(baseline)
    new_report, new = load(candidate)
    for field in CONFIG:
        if field == "model" and model_comparison:
            continue
        if old_report[field] != new_report[field]:
            raise ValueError(f"Incompatible {field}; rerun with matched configuration")
    pairs, used, missing_baseline = [], set(), []
    for key, case in sorted(new.items()):
        baseline_key = key if key in old or model_comparison else (key[0], "uninterrupted")
        if baseline_key not in old:
            missing_baseline.append(list(key))
            continue
        prior = old[baseline_key]
        for field in ("effective_context_window", "plan_mode", "evidence_kind"):
            if prior.get(field) is None or case.get(field) is None or prior[field] != case[field]:
                raise ValueError(f"Incompatible or missing {field} for {key[0]}")
        used.add(baseline_key)
        before, after = metrics(prior), metrics(case)
        dimensions = sorted(
            set(before["subjective_scores"] or {}) | set(after["subjective_scores"] or {})
        )
        left, right = before["duplicate_paragraphs"], after["duplicate_paragraphs"]
        delta = (
            right["count"] - left["count"]
            if left["basis"] == right["basis"]
            and left["count"] is not None
            and right["count"] is not None
            else None
        )
        pairs.append(
            {
                "case_id": key[0],
                "baseline_scenario": baseline_key[1],
                "candidate_scenario": key[1],
                "baseline": before,
                "candidate": after,
                "duplicate_paragraph_delta": delta,
                "dimensions": {
                    d: {
                        "baseline": (before["subjective_scores"] or {}).get(d),
                        "candidate": (after["subjective_scores"] or {}).get(d),
                    }
                    for d in dimensions
                },
            }
        )
    return {
        "version": "writer-comparison.v1",
        "comparison_kind": "model_comparison" if model_comparison else "same_model",
        "acceptance": "not_determined",
        "note": "Recorded runs and deterministic metrics do not establish quality acceptance.",
        "configuration": {
            k: {"baseline": old_report[k], "candidate": new_report[k]} for k in CONFIG
        },
        "evidence_directories": {
            "baseline": str(baseline.resolve()),
            "candidate": str(candidate.resolve()),
        },
        "provenance": {
            k: {"baseline": old_report.get(k), "candidate": new_report.get(k)} for k in PROVENANCE
        },
        "pairs": pairs,
        "missing_baseline": missing_baseline,
        "missing_candidate": [list(k) for k in sorted(old.keys() - used)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-comparison", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = compare(args.baseline, args.candidate, model_comparison=args.model_comparison)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
