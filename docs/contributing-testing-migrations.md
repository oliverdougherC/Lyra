# Contributing, Testing, and Migrations

This guide is for code and schema changes.

## Before you change anything

- Read [Architecture](architecture.md) and [Code conventions](conventions.md).
- If you are changing startup or recovery behavior, also read
  [Local deployment](local-deployment.md).

## Test matrix

Backend logic:

```bash
source .venv/bin/activate
python -m pytest backend/tests
```

Frontend logic:

```bash
cd frontend
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
```

Frontend formatting:

```bash
cd frontend
pnpm run format:check
```

Launcher or readiness logic:

```bash
source .venv/bin/activate
python -m pytest \
  backend/tests/test_lyra_launcher.py \
  backend/tests/test_api_health.py \
  backend/tests/test_database.py
```

Run the smallest command set that covers your change, then add the
feature-specific tests that exercise the code path you touched.

## Migrations

- Add new schema changes as a numbered SQL file in `backend/storage/migrations/`.
- Never edit a migration that has already shipped.
- Keep the filename prefix increasing by one so `migrate()` can apply files in order.
- Treat `pragma user_version` as the migration checkpoint, not a value to hand-edit.

After a schema change, run the database and readiness tests plus the feature
tests that cover the new tables or columns. If the change affects startup,
also run `./run doctor`.

## Release verification

Before you call a change done, verify the behavior at the level the change touched.

- Backend-only change:
  `source .venv/bin/activate && python -m pytest backend/tests`
- Frontend-only change:
  `cd frontend && pnpm run lint`, `cd frontend && pnpm run typecheck`, and the relevant frontend
  tests
- Launcher change:
  `source .venv/bin/activate && python -m pytest backend/tests/test_lyra_launcher.py
  backend/tests/test_api_health.py backend/tests/test_database.py` and `./run doctor`
- Data-flow or migration change:
  the schema tests, the affected feature tests, and one full start and stop cycle with `./run`
