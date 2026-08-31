# Desktop Migration Inventory

Status: implementation inventory created on 2026-08-30 for the desktop-runtime migration. The
first sections preserve the source-checkout baseline; the implementation addendum and final
verification sections record what this branch actually changed and proved.

## Pre-change baseline

- Repository: `oliverdougherC/Lyra`
- Base branch: `main`
- Base SHA after `git fetch --all --prune`: `3b26c46994c38930257a6bf481fc459abcfc1164`
- Open pull requests: none
- Latest `main` CI: successful, GitHub Actions run `33297684261`
- Migration branch: `codex/pla-158-tauri-desktop-migration`
- Baseline machine: Apple Silicon `arm64`, macOS 27.0, 24 GB RAM. This is not the required clean
  8 GB release-reference machine, so physical resource thresholds remain a separate release gate.

At the base SHA, the repository shipped and documented a source-checkout runtime:

- `./run` is the user-facing entry point.
- `scripts/lyra_launcher.py` owns process lifecycle, runtime-state recovery, backup, and restore.
- `frontend/package.json` still defines `next build` and `next start`.
- `.github/workflows/ci.yml` still requires backend, frontend, Python security, and Chromium
  real-stack acceptance through the aggregate `CI Gate`.
- `.github/workflows/webkit-acceptance.yml` still runs the scheduled WebKit acceptance lane.

The base SHA test surface was:

- backend `pytest` with migration, launcher, health, Firecrawl, storage, and security-gate coverage;
- frontend `vitest` and Testing Library coverage;
- Playwright smoke tests against the production frontend server;
- real-stack acceptance using `acceptance/backend_harness.py` and
  `frontend/e2e/acceptance/global-setup.ts`.

### Commands executed before changes

| Check | Result |
| --- | --- |
| `uv run --extra dev ruff format --check backend scripts` | passed; 207 files formatted |
| `uv run --extra dev ruff check backend scripts` | passed |
| `uv run --extra dev pytest` | passed; 2,550 tests, 6 dependency deprecation warnings |
| `uv run --extra security python scripts/python_security_gate.py --json-report ...` | passed; 42 locked production packages audited |
| `pnpm format:check` | passed |
| `pnpm lint` | passed with 3 pre-existing unused-variable warnings in acceptance specs |
| `pnpm typecheck` | passed |
| `pnpm test` | passed; 72 files and 703 tests |
| `pnpm build` | passed with Next.js 16.3.0 |
| `pnpm audit --prod` | passed; no known vulnerabilities |
| `pnpm test:e2e` | 2 passed, 1 failed because the Milkdown editor did not mount within 5 seconds |
| `pnpm test:acceptance` | default run could not start because a pre-existing process owned port 8000 |
| acceptance on ports 18080/13080/18980 | stopped after 8 passed and 4 UI failures; the production frontend had been compiled against fixed backend port 8000, confirming the fixed-port baseline assumption |

The failed browser checks are retained as pre-change evidence rather than presented as migration
regressions. The alternate-port run reclaimed every process group it started and removed its
disposable data directory during teardown.

## Pre-change command matrix

Repository-defined commands in force before the desktop migration:

- Root lifecycle: `./run`, `./run --dev`, `./run doctor`, `./run status`, `./run logs`,
  `./run stop`, `./run backup`, `./run restore`
- Backend checks: `uv sync --extra dev`, `uv run --extra dev ruff format --check backend scripts`,
  `uv run --extra dev ruff check backend scripts`, `uv run --extra dev pytest`
- Frontend checks: `pnpm install --frozen-lockfile`, `pnpm format:check`, `pnpm lint`,
  `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm audit --prod`, `pnpm test:e2e`,
  `pnpm test:acceptance`
- Security gate: `uv sync --extra security`,
  `uv run --extra security python scripts/python_security_gate.py --json-report python-audit-evidence.json`
- Acceptance helper: `./scripts/run-acceptance.sh`
- Firecrawl provisioning helper: `python -m infra.firecrawl start|status|doctor|logs|stop`

## Reconciled assumptions

These assumptions held at the baseline and were revisited during PLA-323 through
PLA-329:

- No packaged desktop runtime is checked in yet.
- No CI lane launches a packaged app bundle or frozen Python sidecar.
- Current acceptance coverage starts backend and frontend processes directly; it does not prove a
  packaged shell, packaged static assets, or packaged sidecar boot.
- Current docs still treat the source checkout and `./run` as the shipped path.
- Launcher runtime state (`STATE_VERSION = 1`) remains separate from SQLite migration versioning.
- Firecrawl and Docker are still active shipping/runtime assumptions in docs, tests, and scripts.

## Cross-cutting inventory retained for review

### Legacy topology to remove

- `./run` and `scripts/lyra_launcher.py` supervise Next.js, FastAPI, and Firecrawl from a source
  checkout.
- Production ports are fixed at frontend `3000`, backend `8000`, and Firecrawl `3002`; helper ports
  are derived from other fixed settings.
- `infra/firecrawl.py`, Compose overrides, environment examples, source pins, and launcher
  readiness/status/log paths require Docker and the Firecrawl service fleet.
- Frontend production and acceptance use `next build` plus `next start`; the UI imports App Router,
  `next/navigation`, `next/link`, and `next/font` APIs.

### Paths and runtime resources

- Mutable defaults are checkout-relative `data/`, `.lyra/`, and `logs/`; model assets currently
  default beneath `data/models/`.
- Helper ownership records, launcher runtime state, and logs are checkout-relative.
- SQLite migrations are resolved from `backend/storage/migrations`; prompts and templates are
  loaded from backend package paths and must be included as immutable packaged resources.
- Native/runtime dependencies that require frozen-build verification include PyMuPDF, sqlite-vec
  and its extension library, keyring/macOS Keychain support, SymPy, Pint, Uvicorn/FastAPI, and their
  transitive dynamic libraries.

### Security, privacy, and durability boundaries to preserve

- Loopback Host and browser Origin/CSRF defenses currently protect an otherwise unauthenticated
  API. Packaged mode adds per-launch authentication while retaining development protections.
- Tutor credentials use the secret abstraction; the migration extends it to independent tutor and
  Exa credentials without persisting keys in normal settings.
- Private-query guarding, source provenance, remote-document acknowledgement, command
  confirmation, process PID-birth ownership, and privacy-safe diagnostics remain required.
- Forward-only SQLite migrations, WAL permissions, storage intents, startup reconciliation,
  durable jobs, backup stage/verify/publish, and non-destructive restore remain authoritative.

### Helper lifecycle to replace

- Embedding and optional OCR start lazily, but an installed reranker is warm-started during backend
  startup.
- Helper ownership already verifies health, model identity, PID birth, and foreign-process refusal;
  the desktop migration adds active leases and idle eviction without weakening those checks.

### Startup network behavior

- Baseline startup probed local Firecrawl and could warm the reranker. The packaged target performs no
  Exa request and starts no llama-server merely because Lyra opened.
- Remote OpenAI-compatible inference stays user-configured and is never contacted until a workflow
  or explicit connection test invokes it.

## New evidence helpers

This lane adds three stdlib-only migration helpers.

### `scripts/desktop_resource_report.py`

Purpose:

- inventory a packaged desktop app root deterministically;
- emit only root-relative paths, never absolute paths;
- redact absolute symlink targets;
- classify executable files, native libraries, Python runtime files, SQLite migrations, frontend
  assets, fonts, images, manifests, and other resources;
- write a JSON report suitable for checked-in or attached soak evidence.

Example:

```bash
python scripts/desktop_resource_report.py \
  --root app=/Applications/Lyra.app \
  --output desktop-resource-report.json
```

### `scripts/packaged_soak_harness.py`

Purpose:

- create a disposable packaged-soak run directory with `profile/`, `artifacts/`, and `logs/`;
- write a versioned `plan.json` for the PLA-147 packaged-desktop soak;
- separate harness-owned preparation from physical execution on a real machine;
- record manual step outcomes and artifact references without claiming the harness executed them.

Example:

```bash
python scripts/packaged_soak_harness.py prepare \
  --app-root /Applications/Lyra.app \
  --run-id pla-147-rc1

python scripts/packaged_soak_harness.py record \
  --plan .desktop-soaks/pla-147-rc1/plan.json \
  --step launch-packaged-app \
  --status completed \
  --note "Launched successfully with disposable profile." \
  --artifact artifacts/launch.png
```

### `scripts/desktop_runtime_report.py`

Purpose:

- capture the package size and owned/correlated process tree for a running `Lyra.app`;
- record RSS, sampled CPU, and open-file counts without command arguments or private paths;
- identify forbidden ordinary-idle services; and
- bind the sample to the source commit used for the package build.

## Focused verification for this lane

The added helpers are covered by focused backend tests:

- `backend/tests/test_desktop_resource_report.py`
- `backend/tests/test_desktop_runtime_report.py`
- `backend/tests/test_packaged_soak_harness.py`

These tests verify privacy-safe output, deterministic relative-path reporting, resource
classification, disposable-run preparation, schema-version checks, and explicit separation between
harness and physical steps.

## 2026-08-30 implementation addendum

- `src-tauri/` now contains the narrow Tauri 2 shell, capability manifest, CSP, single-instance
  handling, inherited-socket sidecar bootstrap, and owned-child shutdown. Rust CI is required.
- `frontend/package.json` has already moved the active frontend scripts to `vite`, `vite build`, and
  `vite preview`.
- `frontend/src/lib/runtime.ts` contains the Tauri bootstrap path (`desktop_bootstrap`) plus
  `VITE_API_BASE` and browser-fallback support, bridging packaged and contributor modes.
- `backend/api/routes_health.py` treats Exa as optional configuration only. Launch/readiness neither
  opens the provider credential nor performs a live Exa request.
- The frozen arm64 PyInstaller onedir backend has been built locally and its authenticated
  inherited-socket smoke has passed. This is functional build evidence, not signing/notarization or
  clean-machine release evidence.
- The active-doc baseline for this migration must not claim completed signing, notarization, an
  8 GB clean-machine release proof, or a finished packaged-app soak before evidence exists.

## Final local verification

Exact package source commit: `0d075921224988e86b9bc229ec931b76e51b2384`.

| Check | Result |
| --- | --- |
| Backend | 2,581 passed, 1 skipped; Ruff passed |
| Frontend | ESLint and TypeScript passed; 705 unit tests and 3 Playwright smoke tests passed |
| Real-stack acceptance | 104 passed with zero unconsumed backend failures |
| Rust shell | format, Clippy with warnings denied, and 3 unit tests passed |
| Dependency audits | Python and frontend had no known vulnerabilities; Rust audit exited zero with 17 allowed upstream warnings |
| Frozen backend | PyInstaller arm64 onedir build and authenticated inherited-socket smoke passed |
| Native artifact | unsigned `Lyra.app` and review DMG built successfully |
| Parent-loss cleanup | terminating the packaged shell caused the frozen sidecar to exit without an orphan |
| Active references | current runtime/workflow scan passed |

Local artifact measurements:

- frozen Python sidecar: approximately 92 MiB on disk;
- `Lyra.app`: 136.9 MiB;
- compressed review DMG: approximately 80 MiB; and
- ordinary-idle sample: 5 Lyra/WebKit processes, no forbidden service processes.

The committed runtime report is from a 24 GiB development Mac and is deliberately labelled as
local evidence. Remaining release gates are Developer ID signing, notarization/Gatekeeper on a
clean Mac, the physical 8 GiB resource run, native backup/restore file selection, a live Exa smoke,
and the full PLA-147 release-candidate soak.
