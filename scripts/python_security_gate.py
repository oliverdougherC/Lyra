"""Vulnerability gate over the exact locked *production* Python dependency graph.

Lyra pins its runtime dependencies in ``uv.lock`` and intentionally suppresses routine
Dependabot version-update pull requests during the prototype/stabilization period
(``open-pull-requests-limit: 0`` in ``.github/dependabot.yaml``). That keeps version churn
out of the way of product work, but it also means a newly published High/Critical advisory
against a package we already ship would never surface as an update PR. This gate closes
that hole: it is an independent, deterministic security signal that fails the build when a
disallowed advisory affects the production graph.

Design guarantees (see docs/security-and-ci-gates.md for the full policy):

* **Same inputs locally and in CI.** The one command below produces the production
  requirements from ``uv.lock`` and audits them; the CI step runs the identical command.
* **Production graph only.** The audited set is ``uv export`` with no dev/security extras
  and without the project itself, i.e. exactly the packages Lyra ships.
* **Report everything, fail on High/Critical.** Every advisory is printed. The gate fails
  for anything at or above the configured threshold (default ``high``), and for anything
  whose severity cannot be established (fail closed on the unknown), unless an unexpired
  allowlist entry accepts it.
* **A scan is never silently "clean".** A missing scanner, an unreachable advisory feed,
  malformed output, or a skipped dependency exits with a distinct tooling-error code. An
  outage can never be mistaken for "no vulnerabilities".
* **Exceptions expire.** Every allowlist entry names an advisory, package, reason, owner,
  and review date. An expired entry fails the gate so it cannot rot silently.

Run it:

    uv run --extra security python scripts/python_security_gate.py

Exit codes: ``0`` clean/accepted, ``1`` policy failure (blocking advisory or expired
exception), ``2`` tooling/feed error (fail closed).
"""

import argparse
import json
import math
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO_ROOT / "security" / "python-audit-policy.toml"

# Exit codes are part of the contract: CI distinguishes a policy failure from a tooling
# failure, and neither is ever confused with success.
EXIT_OK = 0
EXIT_POLICY_FAILURE = 1
EXIT_TOOLING_ERROR = 2

OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"
OSV_TIMEOUT_SECONDS = 20


class Severity(IntEnum):
    """Ordered so that a numeric comparison expresses the policy threshold.

    ``UNKNOWN`` sorts below the graded levels; it is handled by an explicit
    fail-closed switch rather than by the threshold comparison.
    """

    UNKNOWN = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, text: str) -> "Severity":
        key = (text or "").strip().upper()
        return _SEVERITY_BY_NAME.get(key, cls.UNKNOWN)

    def label(self) -> str:
        return self.name


_SEVERITY_BY_NAME = {s.name: s for s in Severity}


class ScanError(RuntimeError):
    """The scan could not be completed; the result must fail closed, never "clean"."""


@dataclass(frozen=True)
class Advisory:
    """One advisory affecting one pinned production package."""

    package: str
    version: str
    advisory_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]
    description: str
    severity: Severity = Severity.UNKNOWN
    # Latest ``modified`` timestamp reported by the advisory feed, when enrichment ran.
    feed_modified: str | None = None

    def identifiers(self) -> set[str]:
        ids = {self.advisory_id, *self.aliases}
        return {i.strip().upper() for i in ids if i and i.strip()}


@dataclass(frozen=True)
class AllowlistEntry:
    advisory: str
    package: str | None
    reason: str
    owner: str
    expires: date

    def matches(self, advisory: Advisory) -> bool:
        if self.advisory.strip().upper() not in advisory.identifiers():
            return False
        if not self.package:
            return True
        return self.package.strip().lower() == advisory.package.strip().lower()


@dataclass(frozen=True)
class Policy:
    fail_threshold: Severity
    fail_on_unknown_severity: bool
    allowlist: tuple[AllowlistEntry, ...]


@dataclass
class AdvisoryOutcome:
    advisory: Advisory
    status: str  # "blocking" | "informational" | "accepted"
    note: str = ""


@dataclass
class GateResult:
    outcomes: list[AdvisoryOutcome] = field(default_factory=list)
    expired_exceptions: list[AllowlistEntry] = field(default_factory=list)

    @property
    def blocking(self) -> list[AdvisoryOutcome]:
        return [o for o in self.outcomes if o.status == "blocking"]

    @property
    def accepted(self) -> list[AdvisoryOutcome]:
        return [o for o in self.outcomes if o.status == "accepted"]

    @property
    def informational(self) -> list[AdvisoryOutcome]:
        return [o for o in self.outcomes if o.status == "informational"]

    @property
    def passed(self) -> bool:
        return not self.blocking and not self.expired_exceptions


# --------------------------------------------------------------------------------------
# Policy evaluation (pure; no IO, so it is exhaustively unit-tested offline)
# --------------------------------------------------------------------------------------


def evaluate(advisories: list[Advisory], policy: Policy, *, today: date) -> GateResult:
    """Classify advisories against the policy. Deterministic and side-effect free.

    An advisory is *blocking* when its severity meets the threshold, or when its severity
    is unknown and the policy fails closed on the unknown. A matching, unexpired allowlist
    entry downgrades a blocking advisory to *accepted*. Any expired allowlist entry is a
    failure in its own right, whether or not its advisory is still present.
    """

    result = GateResult()

    # Every expired exception is a hard failure: a stale acceptance must be re-reviewed,
    # not left to silently keep suppressing findings.
    result.expired_exceptions = [e for e in policy.allowlist if e.expires < today]

    for advisory in advisories:
        is_blocking = advisory.severity >= policy.fail_threshold or (
            advisory.severity == Severity.UNKNOWN and policy.fail_on_unknown_severity
        )

        if not is_blocking:
            below = (
                f"{advisory.severity.label()} below the {policy.fail_threshold.label()} threshold"
            )
            result.outcomes.append(AdvisoryOutcome(advisory, "informational", note=below))
            continue

        match = _first_valid_exception(advisory, policy, today)
        if match is not None:
            result.outcomes.append(
                AdvisoryOutcome(
                    advisory,
                    "accepted",
                    note=f"allowlisted by {match.owner} until {match.expires.isoformat()}",
                )
            )
        else:
            result.outcomes.append(
                AdvisoryOutcome(
                    advisory,
                    "blocking",
                    note=f"severity {advisory.severity.label()}",
                )
            )

    return result


def _first_valid_exception(
    advisory: Advisory, policy: Policy, today: date
) -> AllowlistEntry | None:
    for entry in policy.allowlist:
        if entry.matches(advisory) and entry.expires >= today:
            return entry
    return None


# --------------------------------------------------------------------------------------
# CVSS v3.x base score (pure; deterministic; unit-tested offline)
# --------------------------------------------------------------------------------------

_CVSS_METRICS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_CVSS_PR = {
    "U": {"N": 0.85, "L": 0.62, "H": 0.27},
    "C": {"N": 0.85, "L": 0.68, "H": 0.5},
}


def cvss_v3_base_score(vector: str) -> float | None:
    """Compute a CVSS v3.0/3.1 base score from its vector string.

    Returns ``None`` for anything that is not a parseable CVSS v3 base vector (for example
    a CVSS v4 vector), so the caller can treat it as unknown severity and fail closed.
    """

    parts = vector.strip().split("/")
    if not parts or not parts[0].upper().startswith("CVSS:3"):
        return None
    metrics = {}
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        metrics[key.upper()] = value.upper()

    required = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
    if any(m not in metrics for m in required):
        return None

    scope = metrics["S"]  # "U" (unchanged) or "C" (changed)
    if scope not in _CVSS_PR:
        return None

    try:
        av = _CVSS_METRICS["AV"][metrics["AV"]]
        ac = _CVSS_METRICS["AC"][metrics["AC"]]
        ui = _CVSS_METRICS["UI"][metrics["UI"]]
        pr = _CVSS_PR[scope][metrics["PR"]]
        conf = _CVSS_METRICS["C"][metrics["C"]]
        integ = _CVSS_METRICS["I"][metrics["I"]]
        avail = _CVSS_METRICS["A"][metrics["A"]]
    except KeyError:
        return None

    iss = 1 - ((1 - conf) * (1 - integ) * (1 - avail))
    if scope == "U":  # noqa: SIM108 - the two impact formulas are clearer spelled out
        impact = 6.42 * iss
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
    exploitability = 8.22 * av * ac * pr * ui

    if impact <= 0:
        return 0.0
    raw = impact + exploitability
    if scope == "C":
        raw *= 1.08
    return _cvss_roundup(min(raw, 10.0))


def _cvss_roundup(value: float) -> float:
    """Round up to one decimal place, per the CVSS v3.1 specification."""

    return math.ceil(value * 10 - 1e-9) / 10.0


def severity_from_score(score: float) -> Severity:
    if score >= 9.0:
        return Severity.CRITICAL
    if score >= 7.0:
        return Severity.HIGH
    if score >= 4.0:
        return Severity.MEDIUM
    if score > 0.0:
        return Severity.LOW
    return Severity.UNKNOWN


def severity_from_osv(osv: dict[str, Any]) -> Severity:
    """Best-effort severity from an OSV vulnerability record.

    Prefers an explicit database severity string (as GHSA advisories carry), then falls
    back to computing the score from a CVSS v3 vector. Anything else is UNKNOWN, which the
    policy treats as fail-closed.
    """

    explicit = str(osv.get("database_specific", {}).get("severity", "") or "")
    graded = Severity.parse(explicit)
    if graded is not Severity.UNKNOWN:
        return graded

    best = Severity.UNKNOWN
    for entry in osv.get("severity", []) or []:
        vector = str(entry.get("score", "") or "")
        score = cvss_v3_base_score(vector)
        if score is not None:
            best = max(best, severity_from_score(score))
    return best


# --------------------------------------------------------------------------------------
# IO: policy loading, running pip-audit, OSV enrichment
# --------------------------------------------------------------------------------------


def load_policy(path: Path) -> Policy:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    section = raw.get("policy", {})
    threshold = Severity.parse(section.get("fail_threshold", "high"))
    if threshold is Severity.UNKNOWN:
        raise ScanError(
            f"policy fail_threshold '{section.get('fail_threshold')}' is not a valid severity"
        )
    fail_on_unknown = bool(section.get("fail_on_unknown_severity", True))

    allowlist = []
    for item in raw.get("allowlist", []) or []:
        for required in ("advisory", "reason", "owner", "expires"):
            if not item.get(required):
                raise ScanError(f"allowlist entry is missing required field '{required}': {item}")
        expires = item["expires"]
        if not isinstance(expires, date):
            # tomllib returns a date for a bare TOML date; a quoted string lands here.
            expires = date.fromisoformat(str(expires))
        allowlist.append(
            AllowlistEntry(
                advisory=str(item["advisory"]),
                package=(str(item["package"]) if item.get("package") else None),
                reason=str(item["reason"]),
                owner=str(item["owner"]),
                expires=expires,
            )
        )

    return Policy(
        fail_threshold=threshold,
        fail_on_unknown_severity=fail_on_unknown,
        allowlist=tuple(allowlist),
    )


def export_production_requirements(destination: Path) -> None:
    """Write the fully-resolved production graph from ``uv.lock`` to ``destination``.

    ``--no-emit-project`` drops the ``lyra`` package itself; no ``--extra`` is passed, so
    the ``dev`` and ``security`` extras are excluded. The result is exactly the pinned set
    Lyra ships. ``--frozen`` refuses to silently re-resolve, so the audit always reflects
    the committed lockfile.
    """

    cmd = [
        "uv",
        "export",
        "--frozen",
        "--no-emit-project",
        "--no-hashes",
        "--no-editable",
        "--output-file",
        str(destination),
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=300, cwd=REPO_ROOT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"could not run 'uv export': {exc}") from exc
    if proc.returncode != 0:
        raise ScanError(f"'uv export' failed (exit {proc.returncode}): {proc.stderr.strip()}")


def pip_audit_version() -> str:
    cmd = ["pip-audit", "--version"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (proc.stdout or proc.stderr).strip() or "unknown"


def run_pip_audit(requirements: Path) -> dict[str, Any]:
    """Run pip-audit over the requirements file and return its parsed JSON.

    Raises :class:`ScanError` on any condition that means the scan did not complete
    cleanly: the tool is missing, it crashed, it emitted output we cannot parse, or it
    skipped a dependency (so coverage would be incomplete). Every such case fails closed.
    """

    cmd = [
        "pip-audit",
        "--format",
        "json",
        "--progress-spinner",
        "off",
        "--strict",
        "--requirement",
        str(requirements),
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"could not run pip-audit: {exc}") from exc

    context = f"exit {proc.returncode}: {proc.stderr.strip()[:500]}"
    return validate_audit_payload(proc.stdout, context=context)


def validate_audit_payload(stdout: str, *, context: str = "") -> dict[str, Any]:
    """Parse and sanity-check pip-audit JSON, failing closed on anything abnormal.

    Pure so the fail-closed conditions are covered by offline tests: empty output, output
    that is not JSON, output without a ``dependencies`` array, or a dependency the scanner
    had to skip (which would mean incomplete coverage) all raise :class:`ScanError`.
    """

    if not stdout.strip():
        raise ScanError(f"pip-audit produced no JSON output ({context})")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScanError(f"pip-audit output was not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "dependencies" not in data:
        raise ScanError("pip-audit output did not contain a 'dependencies' array")

    skipped = [d.get("name", "?") for d in data["dependencies"] if d.get("skip_reason")]
    if skipped:
        raise ScanError(
            "pip-audit skipped dependencies (incomplete coverage): " + ", ".join(skipped)
        )
    return data


def parse_pip_audit(data: dict[str, Any]) -> list[Advisory]:
    advisories: list[Advisory] = []
    for dep in data.get("dependencies", []):
        name = dep.get("name", "")
        version = dep.get("version", "")
        for vuln in dep.get("vulns", []) or []:
            advisories.append(
                Advisory(
                    package=name,
                    version=version,
                    advisory_id=str(vuln.get("id", "")),
                    aliases=tuple(str(a) for a in vuln.get("aliases", []) or []),
                    fix_versions=tuple(str(f) for f in vuln.get("fix_versions", []) or []),
                    description=str(vuln.get("description", "") or ""),
                )
            )
    return advisories


def _fetch_osv(advisory_id: str) -> dict[str, Any] | None:
    url = OSV_VULN_URL + urllib.parse.quote(advisory_id, safe="")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=OSV_TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def enrich_severities(advisories: list[Advisory]) -> list[Advisory]:
    """Attach a severity to each advisory from OSV, best effort.

    Enrichment is only attempted when advisories exist, so a clean run never touches the
    network beyond pip-audit's own feed. A failed lookup leaves the severity UNKNOWN, which
    the policy treats as fail-closed, so an OSV outage can never downgrade a finding.
    """

    enriched: list[Advisory] = []
    for advisory in advisories:
        severity = Severity.UNKNOWN
        feed_modified: str | None = None
        for candidate in (advisory.advisory_id, *advisory.aliases):
            if not candidate:
                continue
            try:
                osv = _fetch_osv(candidate)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                continue
            if not osv:
                continue
            graded = severity_from_osv(osv)
            if graded > severity:
                severity = graded
            feed_modified = feed_modified or str(osv.get("modified", "") or "") or None
            if severity is not Severity.UNKNOWN:
                break
        enriched.append(
            Advisory(
                package=advisory.package,
                version=advisory.version,
                advisory_id=advisory.advisory_id,
                aliases=advisory.aliases,
                fix_versions=advisory.fix_versions,
                description=advisory.description,
                severity=severity,
                feed_modified=feed_modified,
            )
        )
    return enriched


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def render_report(result: GateResult, evidence: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Locked Python production dependency vulnerability gate")
    lines.append("=" * 54)
    lines.append(f"scanner:            {evidence['scanner']}")
    lines.append(f"vulnerability feed: {evidence['vulnerability_service']}")
    lines.append(f"advisory feed seen: {evidence['advisory_feed_modified_max'] or 'n/a'}")
    lines.append(f"scanned at (UTC):   {evidence['scanned_at']}")
    lines.append(f"commit:             {evidence['commit']}")
    lines.append(f"packages audited:   {evidence['packages_audited']}")
    lines.append(f"policy threshold:   fail at {evidence['fail_threshold']} and above")
    unknown = "fail closed" if evidence["fail_on_unknown"] else "informational"
    lines.append(f"unknown severity:   {unknown}")
    lines.append("")

    total = len(result.outcomes)
    if total == 0:
        lines.append("No known advisories affect the locked production graph.")
    else:
        lines.append(f"{total} advisory finding(s):")
        for outcome in _ordered_outcomes(result):
            adv = outcome.advisory
            marker = {"blocking": "FAIL", "accepted": "ACCEPTED", "informational": "info"}[
                outcome.status
            ]
            fix = ", ".join(adv.fix_versions) if adv.fix_versions else "no fixed version"
            lines.append(
                f"  [{marker}] {adv.package} {adv.version} {adv.advisory_id} "
                f"({adv.severity.label()}) -> {fix}"
            )
            if outcome.note:
                lines.append(f"           {outcome.note}")

    if result.expired_exceptions:
        lines.append("")
        lines.append("Expired allowlist exceptions (must be re-reviewed or removed):")
        for entry in result.expired_exceptions:
            expired_on = entry.expires.isoformat()
            lines.append(f"  [FAIL] {entry.advisory} owned by {entry.owner} expired {expired_on}")

    lines.append("")
    if result.passed:
        lines.append("RESULT: PASS")
    else:
        reasons = []
        if result.blocking:
            reasons.append(f"{len(result.blocking)} blocking advisory(ies)")
        if result.expired_exceptions:
            reasons.append(f"{len(result.expired_exceptions)} expired exception(s)")
        lines.append("RESULT: FAIL — " + "; ".join(reasons))
    return "\n".join(lines)


def _ordered_outcomes(result: GateResult) -> list[AdvisoryOutcome]:
    order = {"blocking": 0, "accepted": 1, "informational": 2}
    return sorted(
        result.outcomes,
        key=lambda o: (order[o.status], -int(o.advisory.severity), o.advisory.package),
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _current_commit() -> str:
    cmd = ["git", "rev-parse", "HEAD"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=30, cwd=REPO_ROOT
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def run_gate(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)

    requirements = args.requirements
    if requirements is None:
        requirements = Path(args.work_dir) / "production-requirements.txt"
        export_production_requirements(requirements)

    audit = run_pip_audit(requirements)
    advisories = parse_pip_audit(audit)
    if advisories and not args.no_enrich:
        advisories = enrich_severities(advisories)

    result = evaluate(advisories, policy, today=datetime.now(UTC).date())

    feed_modified = [a.feed_modified for a in advisories if a.feed_modified]
    evidence = {
        "scanner": pip_audit_version(),
        "vulnerability_service": "pypi (pip-audit default) + osv.dev severity enrichment",
        "advisory_feed_modified_max": max(feed_modified) if feed_modified else None,
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _current_commit(),
        "packages_audited": len(audit.get("dependencies", [])),
        "fail_threshold": policy.fail_threshold.label(),
        "fail_on_unknown": policy.fail_on_unknown_severity,
        "passed": result.passed,
        "blocking": [_outcome_json(o) for o in result.blocking],
        "accepted": [_outcome_json(o) for o in result.accepted],
        "informational": [_outcome_json(o) for o in result.informational],
        "expired_exceptions": [
            {"advisory": e.advisory, "owner": e.owner, "expires": e.expires.isoformat()}
            for e in result.expired_exceptions
        ],
    }

    print(render_report(result, evidence))

    if args.json_report:
        Path(args.json_report).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    return EXIT_OK if result.passed else EXIT_POLICY_FAILURE


def _outcome_json(outcome: AdvisoryOutcome) -> dict[str, Any]:
    adv = outcome.advisory
    return {
        "package": adv.package,
        "version": adv.version,
        "advisory_id": adv.advisory_id,
        "aliases": list(adv.aliases),
        "severity": adv.severity.label(),
        "fix_versions": list(adv.fix_versions),
        "status": outcome.status,
        "note": outcome.note,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY,
        help="Path to the severity/allowlist policy TOML.",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=None,
        help="Audit this requirements file instead of exporting the production graph. "
        "Intended for tests; the default derives the graph from uv.lock.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=REPO_ROOT / ".security-audit",
        help="Directory for the exported requirements file.",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="Also write the machine-readable evidence to this path.",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip OSV severity enrichment (offline). Unclassified advisories fail closed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.requirements is None:
        Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    try:
        return run_gate(args)
    except ScanError as exc:
        print(f"RESULT: ERROR — scan did not complete: {exc}", file=sys.stderr)
        print(
            "The gate fails closed: an unavailable scanner or advisory feed is never "
            "reported as 'no vulnerabilities'.",
            file=sys.stderr,
        )
        return EXIT_TOOLING_ERROR


if __name__ == "__main__":
    sys.exit(main())
