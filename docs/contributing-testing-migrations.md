# Contributing, Testing, and Migrations

This guide is for code, workflow, and schema changes on the desktop migration branch.

## Before you change anything

- Read [Architecture](architecture.md) and [Code conventions](conventions.md).
- Read [Security and CI gates](security-and-ci-gates.md) before changing workflows or release
  assumptions.
- Treat historical handoff docs as evidence, not as live requirements.

## Core local checks

Backend:

```bash
uv run --extra dev ruff format --check backend scripts
uv run --extra dev ruff check backend scripts
uv run --extra dev pytest
```

Frontend:

```bash
cd frontend
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm audit --prod
pnpm test:e2e
```

Real-backend browser acceptance:

```bash
./scripts/run-acceptance.sh
```

Python security gate:

```bash
uv run --extra security python scripts/python_security_gate.py
```

Desktop and packaging checks:

```bash
python scripts/check_active_references.py
uv run --extra dev python scripts/packaged_python_smoke.py
uv run --extra packaging pyinstaller --clean --noconfirm packaging/lyra_backend.spec
uv run python scripts/frozen_backend_smoke.py dist/lyra-backend/lyra-backend
uv run python packaging/stage_sidecar.py
python scripts/desktop_resource_report.py --root backend=backend --root frontend=frontend
```

## Rust and desktop-shell checks

Run these when the branch contains `src-tauri/Cargo.toml`:

```bash
cargo fmt --manifest-path src-tauri/Cargo.toml --all --check
cargo clippy --manifest-path src-tauri/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml --workspace --all-targets
(cd src-tauri && cargo audit)
```

Do not add Rust/Tauri claims to the live docs unless those checks and the desktop artifact job have
real evidence behind them.

## Migrations

- Add new schema changes as numbered SQL files in `backend/storage/migrations/`.
- Never edit a migration that has already shipped.
- Keep `pragma user_version` aligned only through checked-in migrations.
- Treat the desktop runtime migration and the SQLite schema as separate concerns; packaging does not
  authorize rewriting on-disk data contracts.

## Release evidence helpers

Three scripts support the packaged-desktop release path:

- `scripts/desktop_resource_report.py`
- `scripts/desktop_runtime_report.py`
- `scripts/packaged_soak_harness.py`

Use them to inventory bundle contents and prepare manual soak runs. They are evidence helpers, not a
substitute for the actual packaged-app launch and restart checks.

## Completion standard

Before calling a change done:

- run the smallest relevant local check set;
- keep `CI Gate` green conceptually when editing workflow assumptions;
- update live docs if you changed the current runtime contract;
- keep historical docs clearly labelled when they still mention retired surfaces.
