# Security and CI Gates

This is the authoritative description of what must be true before `main` advances, and of
the deterministic security signals that back it. It covers two enforced gates:

1. The **required merge gate** on `main` (PLA-294).
2. The **locked Python production-dependency vulnerability gate** (PLA-297), alongside the
   existing frontend production audit.

Future agents: a green `CI Gate` and a green Python vulnerability lane are **mandatory**
before the Fall release-candidate soak. Do not merge around them.

## 1. The required merge gate on `main`

### Effective protection mechanism (observed live state)

`main` is governed by a **repository ruleset**, not classic branch protection. At the time
of this change the audited state was:

- Classic branch protection: **none** (`GET /repos/.../branches/main/protection` → 404).
- Repository ruleset **"Default"** (id `20537110`), `enforcement: active`, targeting
  `~DEFAULT_BRANCH`, with these rules:
  - `deletion` — the branch cannot be deleted.
  - `non_fast_forward` — force-pushes / history rewrites are refused.
  - `pull_request` — changes must land through a PR
    (`required_approving_review_count: 0`, `require_code_owner_review: true`,
    `require_extra_approval_for_unattributed_changes: true`).
  - **No `required_status_checks` rule** — so CI was *not* a hard merge gate. This was the
    gap PLA-294 closes.
- `bypass_actors: []`, `current_user_can_bypass: "never"` — nobody, including the admin,
  can currently bypass the ruleset.

Always re-read the live ruleset before trusting a UI label:

```bash
gh api repos/oliverdougherC/Lyra/rulesets
gh api repos/oliverdougherC/Lyra/rulesets/20537110
```

### What this change adds

A `required_status_checks` rule is added to the "Default" ruleset requiring a **single,
stable check context: `CI Gate`**.

`CI Gate` is an aggregate job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
that `needs` every real lane and fails unless all of them succeeded. Requiring the
aggregate — rather than each individual job name — means the internal jobs can be split or
renamed without silently dropping protection: the required context stays `CI Gate`. This
is the intentional guard against a workflow refactor quietly disabling the merge gate.

The required evidence `CI Gate` transitively enforces:

| Evidence | Lane / step |
| --- | --- |
| Backend formatting | `backend` → `ruff format --check` |
| Backend lint | `backend` → `ruff check` |
| Backend tests | `backend` → `pytest` |
| Frontend formatting | `frontend` → `pnpm format:check` |
| Frontend lint | `frontend` → `pnpm lint` |
| TypeScript typecheck | `frontend` → `pnpm typecheck` |
| Frontend unit tests | `frontend` → `pnpm test` |
| Frontend build | `frontend` → `pnpm build` |
| Frontend production dependency audit | `frontend` → `pnpm audit --prod` |
| Python production vulnerability gate | `python-security` → the gate below |
| Browser / E2E lane | `frontend` → `pnpm test:e2e` (Playwright Chromium smoke) |

**PLA-292 (not in this change):** when the bounded real-backend acceptance job lands, add
it to the `needs:` list of `ci-gate` in `ci.yml`. Because the required context is the
aggregate, no ruleset edit is needed to bring it into the required gate — only the
workflow's `needs` list changes.

### Force-push, deletion, and PR flow

Retained from the existing ruleset and unchanged: `deletion` and `non_fast_forward` block
ordinary force-push, non-fast-forward updates, and branch deletion; the `pull_request`
rule keeps ordinary changes flowing through PRs.

### Administrator emergency bypass

Default posture: **bypass is disabled** (`bypass_actors: []`,
`current_user_can_bypass: "never"`). Even the repository admin merges through a PR whose
`CI Gate` is green. Dependency/security hotfixes use the same PR + CI path; they do not
need a bypass, and routine Dependabot version churn stays suppressed (see §2).

If a genuine recovery emergency ever requires a bypass, the deliberate, auditable
procedure is:

1. In repository settings, temporarily set the "Default" ruleset to **Evaluate** (or add
   the specific admin as a time-boxed bypass actor). This is a visible configuration
   change, not a silent per-merge override.
2. Land the fix through a PR that records a **written reason** in its description.
3. Immediately restore `enforcement: active` with no bypass actors.
4. Open a follow-up that re-runs the full `CI Gate` on the resulting `main` and records
   the run, so the emergency change is verified after the fact.

## 2. Locked Python production-dependency vulnerability gate

`pnpm audit --prod` already guards the frontend production graph. This gate gives the
Python backend an equivalent, independent, deterministic signal over the **exact pinned
production dependency graph in `uv.lock`** — the packages Lyra actually ships.

It exists because Lyra intentionally suppresses routine Dependabot version-update PRs
(`open-pull-requests-limit: 0`), so a newly published High/Critical advisory against a
package we already ship would otherwise never surface. See
[`.github/dependabot.yaml`](../.github/dependabot.yaml).

### One command, locally and in CI

```bash
uv run --extra security python scripts/python_security_gate.py
```

CI runs the identical command in the `python-security` lane. The scanner (`pip-audit`) is
**pinned in `pyproject.toml` and locked in `uv.lock`** (the `security` extra), so the same
scanner build runs everywhere.

### What it scans

The gate derives the audited set from the lockfile, not from loose top-level constraints:

```
uv export --frozen --no-emit-project --no-hashes --no-editable
```

- `--frozen` refuses to silently re-resolve, so the audit always reflects the committed
  `uv.lock`.
- No `--extra` is passed, so the `dev` and `security` extras are excluded — we never scan
  the test tooling or the scanner itself.
- `--no-emit-project` drops the `lyra` package.

The result is the fully-resolved, pinned production graph across platforms.

### Policy

Configured in [`security/python-audit-policy.toml`](../security/python-audit-policy.toml):

- **Reports every advisory.**
- **Fails on High or Critical** production advisories (`fail_threshold = "high"`). Low and
  Medium are informational.
- **Fails closed on unknown severity** (`fail_on_unknown_severity = true`): an advisory
  whose severity cannot be established is treated as blocking, so an unrated CVE cannot
  slip through. Severity is taken from an explicit GHSA severity when present, otherwise
  computed from the CVSS v3 base vector via OSV; enrichment is best-effort and only
  attempted when advisories exist, so a clean run never depends on it.
- **Never reports an outage as clean.** A missing scanner, unreachable feed, malformed
  output, or a skipped dependency exits with a distinct tooling-error code (`2`) rather
  than success. Exit codes: `0` clean/accepted, `1` policy failure, `2` tooling/feed
  error.

### Temporary exceptions

Add an `[[allowlist]]` entry to the policy file. Each entry **must** name the advisory
(id or a recorded alias), a reason (why Lyra is not exploitable or has no safe upgrade), an
owner, and an `expires` date. An **expired entry fails the gate** so a stale acceptance
cannot rot unnoticed. An optional `package` narrows the match. There are currently no
active exceptions.

### Deterministic proof

`backend/tests/test_python_security_gate.py` proves the gate offline — it never touches the
live feed. It uses a committed pip-audit fixture
(`backend/tests/fixtures/pip_audit_vulnerable.json`) plus direct policy-engine tests to
show that a disallowed advisory fails the gate, that the severity threshold and the
expiring allowlist behave, and that broken scans fail closed. This does not depend on the
public advisory feed continuing to ship a conveniently vulnerable package.

### Recorded evidence

`--json-report <path>` writes machine-readable evidence: scanner version, vulnerability
service, the latest advisory-feed `modified` timestamp seen (when advisories exist),
UTC scan time, commit SHA, packages audited, the policy, and every finding with its status.
CI uploads this as the `python-audit-evidence` artifact. Record the scanner version, the
exact command, the result, and the commit SHA in release evidence.

## 3. GitHub dependency / security-alert settings (tracked separately)

These are **repository settings**, independent of both the version-update config in
`dependabot.yaml` and the required status checks. Do not assume the Dependabot
version-update configuration says anything about whether alerts are active. Observed live
state at the time of this change:

```bash
gh api repos/oliverdougherC/Lyra/vulnerability-alerts   # 404 => alerts disabled
gh api repos/oliverdougherC/Lyra --jq '.security_and_analysis'
```

- Dependabot **vulnerability alerts**: **disabled**.
- Dependabot **security updates**: **disabled**.
- Secret scanning: **enabled**; push protection: **enabled**.

The Python production vulnerability gate above does not depend on GitHub alerts being on —
it is the enforcing signal in CI. Enabling GitHub Dependabot **alerts** (which raise
notifications without opening version-update PR churn) is a reasonable optional hardening
step and is left to the repository owner; it is **not** required for the merge gate and is
recorded here rather than silently assumed.
