# Security and CI Gates

This document describes the required merge gate and the evidence it must aggregate on the desktop
migration branch.

## Required merge gate

`main` advances only through the aggregate GitHub check context `CI Gate`.

`CI Gate` is intentionally a single stable status name. Internal jobs may be split or renamed as the
desktop migration lands, but branch protection should continue to require only `CI Gate`.

## Required evidence inside `CI Gate`

The aggregate gate currently requires these lanes:

| Lane | Evidence |
| --- | --- |
| `backend` | `ruff format --check`, `ruff check`, full `pytest` |
| `backend-targeted` | mocked Exa, helper lifecycle, path/auth, readiness, and packaging-helper regression tests |
| `frontend` | `pnpm format:check`, `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm audit --prod`, `pnpm test:e2e` |
| `acceptance` | real-backend Playwright acceptance against the built frontend |
| `python-security` | locked production Python dependency audit from `uv.lock` |
| `packaged-python-smoke` | import/resource smoke for the Python modules and packaged resources the desktop build needs |
| `resource-report` | deterministic component inventory artifact from `scripts/desktop_resource_report.py` |
| `active-reference-absence` | stale active-doc/workflow reference scan |
| `rust-desktop` | `cargo fmt`, `clippy`, `test`, and `audit` for the checked-in Tauri shell |
| `desktop-macos-artifact` | Apple Silicon macOS frozen-sidecar smoke, app/DMG build, resource report, and uploaded artifact evidence |

Two lanes retain a presence guard so older branches can still evaluate the stable aggregate gate:

- `rust-desktop`
- `desktop-macos-artifact`

On this branch `src-tauri/Cargo.toml` exists, so both guards select the real Rust and macOS artifact
work. The first hosted result is still pending until the branch is pushed.

## Python security gate

The production Python dependency audit remains mandatory:

```bash
uv run --extra security python scripts/python_security_gate.py
```

It audits the exact locked production graph from `uv.lock`, fails on known high or critical
advisories, and fails closed on scanner/feed errors. Machine-readable evidence remains part of the
CI artifacts.

## Web research and startup safety

Lyra's readiness contract is intentionally narrower than "everything remote answered":

- readiness reports database state and Exa configuration only;
- startup does not perform a live Exa probe;
- CI uses mocked Exa transports by default, not a live provider account;
- explicit connection checks for Exa remain a user-triggered settings action.

That rule prevents the app or CI from silently contacting a third-party provider just because the
process started.

## macOS Apple Silicon CI note

GitHub's current standard arm64 macOS runner labels include `macos-14`, `macos-15`, and
`macos-latest`, each on Apple Silicon hardware according to GitHub's hosted-runner reference as of
August 30, 2026. The desktop artifact lane should use one of those labels and treat runner availability
as separate from the real clean-machine release soak.

## Release caveats

Green CI is necessary and not sufficient for release.

CI does not yet prove:

- code signing
- notarization
- the full packaged-app release-candidate soak
- sustained resource stability on a real school-use machine

Those remain explicit checklist and evidence items, not implicit claims.
