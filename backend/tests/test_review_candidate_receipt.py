"""Receipts distinguish exact PR candidates from the other lanes' synthetic merge."""

import json

import pytest

from scripts.review_candidate_receipt import receipt, sha256, source_identity


def test_pr_candidate_records_head_base_and_separate_merge():
    value = source_identity("a" * 40, "b" * 40, "a" * 40, "c" * 40)
    assert value["checkout_sha"] == value["pull_request_head_sha"] == "a" * 40
    assert value["workflow_event_sha"] == "b" * 40
    assert value["pull_request_base_sha"] == "c" * 40
    assert value["checkout_kind"] == "pull-request-head"


def test_incorrect_pr_checkout_is_rejected():
    with pytest.raises(ValueError, match="exact candidate"):
        source_identity("b" * 40, "b" * 40, "a" * 40, "c" * 40)


def test_main_receipt_identifies_the_branch_commit():
    assert source_identity("a" * 40, "a" * 40, None)["checkout_kind"] == "branch-commit"


def test_receipt_hashes_the_actual_dmg_and_never_claims_distribution(tmp_path):
    dmg = tmp_path / "Lyra_0.2.0-beta.0_aarch64_UNSIGNED_REVIEW.dmg"
    dmg.write_bytes(b"review installer fixture")
    (tmp_path / "lyra-release.json").write_text(
        json.dumps({"source": "a" * 40, "version": "0.2.0-beta.0"})
    )
    value = receipt(
        tmp_path,
        dmg,
        source_identity("a" * 40, "b" * 40, "a" * 40),
        "0.2.0-beta.0",
        "https://github.com/oliverdougherC/Lyra/actions/runs/123",
    )
    assert value["installer"]["sha256"] == sha256(dmg)
    assert value["status"] == "UNSIGNED_REVIEW_ONLY"
    assert not value["distribution_ready"] and not value["notarized"]
    assert not value["developer_id_signed"] and not value["updater_artifact"]
    for line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert sha256(tmp_path / name) == digest
    assert "authentication may be required" in value["access"]


def test_receipt_rejects_contract_from_a_different_checkout(tmp_path):
    dmg = tmp_path / "candidate.dmg"
    dmg.write_bytes(b"fixture")
    (tmp_path / "lyra-release.json").write_text(
        json.dumps({"source": "b" * 40, "version": "0.2.0-beta.0"})
    )
    with pytest.raises(ValueError, match="built checkout"):
        receipt(
            tmp_path,
            dmg,
            source_identity("a" * 40, "a" * 40, None),
            "0.2.0-beta.0",
            "https://github.com/oliverdougherC/Lyra/actions/runs/123",
        )
