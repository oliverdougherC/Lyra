"""Deterministic, offline tests for the Python production vulnerability gate.

These never touch the network or the live advisory feed. They prove the policy engine
fails for a disallowed advisory, honours the severity threshold and the expiring
allowlist, and fails closed on a broken scan. A committed pip-audit fixture stands in for
"a disallowed vulnerability is present" so the test does not depend on the public feed
continuing to ship a conveniently vulnerable package (PLA-297).
"""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "python_security_gate.py"
_SPEC = importlib.util.spec_from_file_location("python_security_gate", _MODULE_PATH)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 8, 22)


def _advisory(
    severity: gate.Severity,
    *,
    advisory_id: str = "PYSEC-2099-0001",
    package: str = "acme",
) -> gate.Advisory:
    return gate.Advisory(
        package=package,
        version="1.2.3",
        advisory_id=advisory_id,
        aliases=("CVE-2099-0001", "GHSA-aaaa-bbbb-cccc"),
        fix_versions=("1.2.4",),
        description="synthetic",
        severity=severity,
    )


def _policy(**overrides) -> gate.Policy:
    base = {
        "fail_threshold": gate.Severity.HIGH,
        "fail_on_unknown_severity": True,
        "allowlist": (),
    }
    base.update(overrides)
    return gate.Policy(**base)


# --- The core requirement: a disallowed advisory fails the gate -----------------------


def test_high_severity_advisory_blocks():
    result = gate.evaluate([_advisory(gate.Severity.HIGH)], _policy(), today=TODAY)
    assert not result.passed
    assert len(result.blocking) == 1


def test_critical_severity_advisory_blocks():
    result = gate.evaluate([_advisory(gate.Severity.CRITICAL)], _policy(), today=TODAY)
    assert not result.passed


def test_committed_vulnerable_fixture_fails_gate():
    """The end-to-end offline proof: parse real pip-audit JSON, classify, and fail."""

    payload = json.loads((FIXTURES / "pip_audit_vulnerable.json").read_text())
    validated = gate.validate_audit_payload(json.dumps(payload))
    advisories = gate.parse_pip_audit(validated)
    assert len(advisories) == 1
    # Severity enrichment is offline here, so the advisory is UNKNOWN and must fail closed.
    result = gate.evaluate(advisories, _policy(), today=TODAY)
    assert not result.passed
    assert result.blocking[0].advisory.package == "acme-vulnerable-widget"


# --- Severity threshold: lower severities are informational ---------------------------


def test_low_severity_is_informational():
    result = gate.evaluate([_advisory(gate.Severity.LOW)], _policy(), today=TODAY)
    assert result.passed
    assert len(result.informational) == 1


def test_medium_below_high_threshold_is_informational():
    result = gate.evaluate([_advisory(gate.Severity.MEDIUM)], _policy(), today=TODAY)
    assert result.passed


def test_unknown_severity_fails_closed():
    result = gate.evaluate([_advisory(gate.Severity.UNKNOWN)], _policy(), today=TODAY)
    assert not result.passed


def test_unknown_severity_informational_when_configured_open():
    result = gate.evaluate(
        [_advisory(gate.Severity.UNKNOWN)],
        _policy(fail_on_unknown_severity=False),
        today=TODAY,
    )
    assert result.passed


# --- Allowlist: valid, expired, and mismatched entries --------------------------------


def _entry(**overrides) -> gate.AllowlistEntry:
    base = {
        "advisory": "CVE-2099-0001",
        "package": None,
        "reason": "not reachable",
        "owner": "Oliver Dougherty",
        "expires": date(2099, 1, 1),
    }
    base.update(overrides)
    return gate.AllowlistEntry(**base)


def test_valid_allowlist_entry_accepts_advisory():
    result = gate.evaluate(
        [_advisory(gate.Severity.CRITICAL)],
        _policy(allowlist=(_entry(),)),
        today=TODAY,
    )
    assert result.passed
    assert len(result.accepted) == 1


def test_allowlist_matches_by_alias():
    result = gate.evaluate(
        [_advisory(gate.Severity.HIGH)],
        _policy(allowlist=(_entry(advisory="GHSA-aaaa-bbbb-cccc"),)),
        today=TODAY,
    )
    assert result.passed


def test_expired_allowlist_entry_fails_gate():
    result = gate.evaluate(
        [_advisory(gate.Severity.HIGH)],
        _policy(allowlist=(_entry(expires=date(2026, 1, 1)),)),
        today=TODAY,
    )
    assert not result.passed
    # Both the still-blocking advisory and the expired exception itself are reported.
    assert result.expired_exceptions
    assert result.blocking


def test_expired_entry_fails_even_without_matching_advisory():
    result = gate.evaluate([], _policy(allowlist=(_entry(expires=date(2026, 1, 1)),)), today=TODAY)
    assert not result.passed
    assert result.expired_exceptions


def test_package_scoped_entry_does_not_match_other_package():
    result = gate.evaluate(
        [_advisory(gate.Severity.HIGH, package="acme")],
        _policy(allowlist=(_entry(package="somethingelse"),)),
        today=TODAY,
    )
    assert not result.passed


# --- Fail-closed scan validation ------------------------------------------------------


def test_empty_output_fails_closed():
    with pytest.raises(gate.ScanError):
        gate.validate_audit_payload("")


def test_non_json_output_fails_closed():
    with pytest.raises(gate.ScanError):
        gate.validate_audit_payload("not json at all")


def test_missing_dependencies_key_fails_closed():
    with pytest.raises(gate.ScanError):
        gate.validate_audit_payload(json.dumps({"fixes": []}))


def test_skipped_dependency_fails_closed():
    payload = {"dependencies": [{"name": "foo", "version": "1.0", "skip_reason": "no version"}]}
    with pytest.raises(gate.ScanError):
        gate.validate_audit_payload(json.dumps(payload))


def test_clean_payload_validates():
    payload = {
        "dependencies": [{"name": "fastapi", "version": "0.141.1", "vulns": []}],
        "fixes": [],
    }
    data = gate.validate_audit_payload(json.dumps(payload))
    assert gate.parse_pip_audit(data) == []


# --- CVSS v3 base score + severity mapping (offline severity classification) -----------


@pytest.mark.parametrize(
    "vector,expected",
    [
        # Canonical CVSS v3.1 examples with known base scores.
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", gate.Severity.CRITICAL),  # 9.8
        ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N", gate.Severity.MEDIUM),  # 5.9
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", gate.Severity.MEDIUM),  # 6.1
        ("CVSS:3.0/AV:L/AC:L/PR:L/UI:N/S:U/C:L/I:N/A:N", gate.Severity.LOW),  # 3.3
    ],
)
def test_cvss_v3_score_to_severity(vector, expected):
    score = gate.cvss_v3_base_score(vector)
    assert score is not None
    assert gate.severity_from_score(score) == expected


def test_cvss_v4_vector_is_unparseable():
    # A CVSS v4 vector is not a v3 base vector; it must read as unknown (fail closed).
    v4 = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
    assert gate.cvss_v3_base_score(v4) is None


def test_severity_from_osv_prefers_explicit_string():
    osv = {"database_specific": {"severity": "CRITICAL"}}
    assert gate.severity_from_osv(osv) == gate.Severity.CRITICAL


def test_severity_from_osv_uses_cvss_vector():
    vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    osv = {"severity": [{"type": "CVSS_V3", "score": vector}]}
    assert gate.severity_from_osv(osv) == gate.Severity.CRITICAL


def test_severity_from_osv_unknown_when_absent():
    assert gate.severity_from_osv({}) == gate.Severity.UNKNOWN


# --- Policy loading -------------------------------------------------------------------


def test_load_policy_parses_threshold_and_allowlist(tmp_path):
    policy_file = tmp_path / "policy.toml"
    policy_file.write_text(
        "\n".join(
            [
                "[policy]",
                'fail_threshold = "critical"',
                "fail_on_unknown_severity = false",
                "",
                "[[allowlist]]",
                'advisory = "CVE-2099-0001"',
                'package = "acme"',
                'reason = "not reachable"',
                'owner = "Oliver Dougherty"',
                "expires = 2099-01-01",
            ]
        )
    )
    policy = gate.load_policy(policy_file)
    assert policy.fail_threshold == gate.Severity.CRITICAL
    assert policy.fail_on_unknown_severity is False
    assert len(policy.allowlist) == 1
    assert policy.allowlist[0].expires == date(2099, 1, 1)


def test_load_policy_rejects_incomplete_exception(tmp_path):
    policy_file = tmp_path / "policy.toml"
    policy_file.write_text(
        "\n".join(
            [
                "[policy]",
                'fail_threshold = "high"',
                "",
                "[[allowlist]]",
                'advisory = "CVE-2099-0001"',
                'reason = "missing owner and expiry"',
            ]
        )
    )
    with pytest.raises(gate.ScanError):
        gate.load_policy(policy_file)


def test_repository_policy_file_is_valid():
    """The committed policy must always parse; a broken policy must not ship."""

    policy = gate.load_policy(_MODULE_PATH.parents[1] / "security" / "python-audit-policy.toml")
    assert policy.fail_threshold == gate.Severity.HIGH
    assert policy.fail_on_unknown_severity is True
