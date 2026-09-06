"""Reject structurally signed code that lost runtime hardening or changed identity."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release.signing_evidence import inspect_details, validate_evidence  # noqa: E402

DETAILS = (
    "CodeDirectory v=20500 size=100 flags=0x10002(adhoc,runtime) hashes=1+3\nSignature=adhoc\n"
)


def receipt():
    return {
        **inspect_details(DETAILS),
        "objects": [
            {
                "path": path,
                "entitlements": {"com.apple.security.cs.disable-library-validation": True}
                if path.endswith("/lyra-backend")
                else {},
                "details": DETAILS,
            }
            for path in (".", "Contents/Resources/resources/lyra-backend/lyra-backend")
        ],
    }


def test_observed_ad_hoc_hardened_signatures_are_accepted():
    validate_evidence(receipt())


@pytest.mark.parametrize(
    "details",
    [
        DETAILS.replace("0x10002", "0x2"),
        DETAILS.replace("Signature=adhoc", "Authority=Developer ID Application: fixture"),
        DETAILS + "Authority=fixture\n",
        "",
    ],
)
def test_native_signature_regressions_are_rejected(details):
    with pytest.raises(ValueError):
        inspect_details(details)


def test_nested_runtime_loss_is_rejected():
    value = receipt()
    value["objects"][1]["details"] = DETAILS.replace("0x10002", "0x2")
    with pytest.raises(ValueError, match="hardened runtime"):
        validate_evidence(value)


def test_declared_flags_must_match_inspected_flags():
    value = receipt()
    value["hardened_runtime"] = False
    with pytest.raises(ValueError, match="differs"):
        validate_evidence(value)


def test_receipt_requires_backend_observation():
    value = receipt()
    value["objects"].pop()
    with pytest.raises(ValueError, match="backend"):
        validate_evidence(value)


def test_shell_cannot_receive_helper_library_exception():
    value = receipt()
    value["objects"][0]["entitlements"] = {"com.apple.security.cs.disable-library-validation": True}
    with pytest.raises(ValueError, match="scoped helper"):
        validate_evidence(value)
