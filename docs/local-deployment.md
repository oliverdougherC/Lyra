# Local Deployment

This document describes the installed desktop runtime and the contributor workflow used to build
and test it.

## Installed application

`Lyra.app` is the product surface: a Tauri 2 window with bundled Vite/React assets and a
PyInstaller onedir FastAPI sidecar. It requires no terminal, source checkout, Node, pnpm, system
Python, or local server platform. The shell selects an ephemeral loopback listener, passes the
socket and per-launch credential over inherited descriptors/stdin, and reclaims the owned sidecar
on quit.

### One local installation

`/Applications/Lyra.app` is the canonical local installation. Quit Lyra, then run:

```bash
./scripts/build_local_app.sh
```

The command builds the current checkout, signs with the same development identity, verifies the
installed frozen backend, and opens `/Applications/Lyra.app`. It consumes the temporary build-output
app after successful installation, so Spotlight has one app to launch. Installation stages and
rollback copies stay in a hidden `.noindex` directory and are removed after success. If verification
fails, the previous installed app is restored. A running app is never overwritten.

Application data, models, logs, and Keychain settings remain outside the bundle and are preserved.
This builds the current checkout; it does not fetch or switch branches. Use current `main` for your
normal installation, and check the source revision before intentionally installing another branch.
Eject mounted installer DMGs when finished; their app copies can otherwise appear in macOS searches.

For agents and contributors delivering local product changes, use this command instead of leaving
an app in a worktree. If you already built and signed a candidate, quit Lyra and install it with
`uv run python scripts/install_local_app.py src-tauri/target/release/bundle/macos/Lyra.app`, then open
`/Applications/Lyra.app`.

### Packaging steps for release and troubleshooting

These lower-level steps leave a review artifact until the installer above consumes it. Build with
the same identity across rebuilds:

```bash
python3 scripts/release_metadata.py --check
uv python install 3.12
uv sync --python 3.12 --extra packaging
uv run --extra packaging pyinstaller --clean --noconfirm packaging/lyra_backend.spec
uv run python scripts/frozen_backend_smoke.py dist/lyra-backend/lyra-backend
uv run python packaging/stage_sidecar.py
(cd frontend && pnpm install --frozen-lockfile)
(cd frontend && pnpm licenses list --prod --json) > frontend-licenses.json
uv run python scripts/collect_distribution_notices.py --frontend-inventory frontend-licenses.json
(cd frontend && pnpm build)
(cd frontend && pnpm tauri:build --bundles app --no-sign --ci)
python3 scripts/release_metadata.py --bundle src-tauri/target/release/bundle/macos/Lyra.app --source "$(git rev-parse HEAD)"
uv run python scripts/sign_local_app.py src-tauri/target/release/bundle/macos/Lyra.app
uv run python scripts/verify_macos_bundle.py src-tauri/target/release/bundle/macos/Lyra.app
uv run python scripts/frozen_backend_smoke.py \
  src-tauri/target/release/bundle/macos/Lyra.app/Contents/Resources/resources/lyra-backend/lyra-backend
./scripts/build_dmg.sh \
  src-tauri/target/release/bundle/macos/Lyra.app \
  "src-tauri/target/release/bundle/dmg/Lyra_$(cat version.txt)_aarch64.dmg"
```

The signing helper selects the sole valid Apple Development identity, or an exact
`LYRA_LOCAL_SIGNING_IDENTITY` name/SHA-1 from `security find-identity -v -p codesigning`.
It fails instead of silently using ad-hoc signing. It signs every native object inside-out and
checks stable certificate-backed requirements for `com.lyra.desktop` and
`com.lyra.desktop.backend`. Keep that identity across local rebuilds to preserve Keychain trust.
Moving from old ad-hoc signatures or development to distribution identity may need one new
Keychain approval; the helper does not read credentials or change Keychain access policies.

Reopen the completed app and verify native launch after signing and the frozen smoke check.
Development signing is local review evidence, **not** Developer ID distribution/notarization.
The protected [release pipeline](releasing.md) owns public artifacts and update delivery.

### Stable Keychain approval

Keychain remembers executable identity. Ad-hoc signing identifies a backend by its code hash,
which changes on rebuild. The signing helper verifies certificate-backed designated requirements
without `cdhash` for the app and backend. Reuse the same development certificate across rebuilds.
Changing from an old ad-hoc build or switching certificates may require a new approval. macOS may
also request permission to use the signing certificate private key; the helper does not modify
application credential access controls.

## Contributor checkout

Install the prerequisites in [CONTRIBUTING](../CONTRIBUTING.md), then start the hot-reloading
stack from the repository root:

```bash
./run --dev
```

Useful commands:

```bash
./run doctor
./run status
./run logs
./run stop
./scripts/run-acceptance.sh
```

`./run` without `--dev` builds and serves the frontend with Vite preview. Both modes use
checkout-owned data. See [CONTRIBUTING](../CONTRIBUTING.md) for prerequisites and the complete
first-run path. Rust is required for desktop-shell builds.

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

- `(cd frontend && pnpm test:e2e)` exercises the built frontend boundary.
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
- `scripts/desktop_runtime_report.py` records the retained Lyra process tree, privacy-safe
  per-process resource sample, and the physical gates still left open for a running package.
- `scripts/packaged_soak_harness.py` prepares and records a manual packaged-app soak.

The checked-in report is a preliminary 24 GiB development-machine sample. It does not replace the
signed, notarized, clean-8-GiB-Mac launch, restart, sleep/wake, memory-pressure, live-provider,
or sustained-soak gates.
