# Local Deployment

This document describes the installed desktop runtime and the contributor workflow used to build
and test it.

## Installed application

`Lyra.app` is the product surface: a Tauri 2 window with bundled Vite/React assets and a
PyInstaller onedir FastAPI sidecar. It requires no terminal, source checkout, Node, pnpm, system
Python, or local server platform. The shell selects an ephemeral loopback listener, passes the
socket and per-launch credential over inherited descriptors/stdin, and reclaims the owned sidecar
on quit.

Build an unsigned review artifact from a contributor checkout with:

```bash
uv sync --extra packaging
uv run --extra packaging pyinstaller --clean --noconfirm packaging/lyra_backend.spec
uv run python scripts/frozen_backend_smoke.py dist/lyra-backend/lyra-backend
uv run python packaging/stage_sidecar.py
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
pnpm --dir frontend tauri:build --bundles app --no-sign --ci
./scripts/build_dmg.sh \
  src-tauri/target/release/bundle/macos/Lyra.app \
  src-tauri/target/release/bundle/dmg/Lyra_0.1.0_aarch64.dmg
```

This is unsigned/ad-hoc review evidence. Developer ID signing, notarization, and the final clean
8 GB Mac gate remain release activities.

## Contributor checkout

Start the local development stack from the repository root:

```bash
./run
```

Useful commands:

```bash
./run doctor
./run status
./run logs
./run stop
./scripts/run-acceptance.sh
```

Current host prerequisites for the checkout path:

- Python `3.12+`
- Node.js `20.9+`
- `pnpm`
- enough disk for local data, build output, and model files

Rust is required for desktop-shell builds, not for focused frontend/backend contributor tests.

## Topology

Contributor mode:

```text
browser
  -> Vite frontend on localhost:3000
       -> FastAPI on 127.0.0.1:8000
            -> SQLite + local files
            -> optional local helper processes
            -> configured tutor endpoint
            -> Exa only when web research is enabled and used
```

Installed runtime:

```text
Tauri webview
  -> bundled frontend assets
       -> packaged Python sidecar on loopback
            -> SQLite + local files under the app profile
            -> lazy local helpers with active leases and five-minute idle eviction
            -> configured tutor endpoint
            -> Exa only when web research is enabled and used
```

No active shipping contract depends on the older bundled scrape stack.

## Health and acceptance

`GET /api/health/live` checks only whether FastAPI can answer a request.

`GET /api/health/ready` checks:

- SQLite accessibility and migration currency
- whether the local web-research toggle is enabled

It does not open the Exa key, probe Exa, or claim remote tutor connectivity. Those are explicit
Settings/workflow actions, not startup side effects.

For browser-level verification:

- `pnpm test:e2e` exercises the built frontend boundary.
- `./scripts/run-acceptance.sh` runs the real-backend acceptance suite from a clean checkout.

## Data paths

The packaged app keeps mutable state outside `Lyra.app`:

- durable data, the database, models, and helper ownership:
  `~/Library/Application Support/Lyra`
- regenerable page and frontend caches: `~/Library/Caches/Lyra`
- bounded backend logs: `~/Library/Logs/Lyra`
- tutor and Exa credentials: macOS Keychain when available, with the existing private fallback

The contributor checkout continues to use `data/`, `.lyra/`, and `logs/` so source tests do not
touch an installed profile. `LYRA_DATA_DIR`, `LYRA_CACHE_DIR`, `LYRA_LOGS_DIR`, and
`LYRA_MODELS_DIR` are explicit test/recovery overrides, not the installed-app defaults.

## Release-evidence helpers

The desktop migration includes three evidence helpers:

- `scripts/desktop_resource_report.py` inventories a bundle or source component root without
  emitting absolute paths.
- `scripts/desktop_runtime_report.py` records the owned process tree and a privacy-safe resource
  sample for a running package.
- `scripts/packaged_soak_harness.py` prepares and records a manual packaged-app soak.

The checked-in report is local 24 GB development-machine evidence. It does not replace the signed,
notarized, clean-8-GB-Mac launch, restart, outage, and sustained-soak gates.
