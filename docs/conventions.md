# Code conventions

## Keep changes understandable

Use English, UTF-8, and LF line endings. Follow the local naming and file layout. Prefer existing
utilities and small changes over new layers or dependencies. Keep private data, credentials, model
weights, generated app bundles, and local runtime state out of Git.

Python formatting and lint rules are defined in [pyproject.toml](../pyproject.toml). Use type hints,
`snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Backend errors
use domain exceptions from `backend/core/errors.py`; user-facing failures must not expose internal
tracebacks, credentials, or private paths.

Frontend formatting, scripts, and dependency versions live in
[frontend/package.json](../frontend/package.json), [Prettier configuration](../.prettierrc),
and [TypeScript configuration](../frontend/tsconfig.json). Use `PascalCase` component exports,
`kebab-case` filenames, and typed API contracts. Route server state through TanStack Query and
`frontend/src/lib/api.ts`; desktop-native calls belong at the runtime boundary.

## Follow the boundaries

- `backend/api/` validates requests and exposes the public API.
- `backend/core/` owns workflows and durable behavior; `backend/rag/` owns document processing
  and retrieval; `backend/llm/` owns tutor and helper connections.
- `backend/storage/` owns SQLite, migrations, private storage, and credential abstractions.
- `frontend/src/` contains the Vite/React UI, shared components, state hooks, and runtime client.
- `src-tauri/` owns desktop bootstrap, native dialogs, process ownership, backup, and updates.
- `packaging/` and `scripts/` own reproducible packaging and verification.

Runtime environment configuration comes from [backend/config.py](../backend/config.py) through
`LYRA_` variables. User-editable settings are persisted through the settings API; there is no
`config.yaml` setup step. Credential values use Keychain with private fallback storage and are
never returned through the settings API. See [privacy](privacy-and-data-location.md).

## Design and behavior

Use semantic tokens from [the design system](design-system.md), implemented in
`frontend/src/styles/globals.css`. Pair fill and foreground tokens, preserve keyboard access,
respect reduced motion, and make error/retry/save states understandable. Avoid duplicate settings,
state abstractions, or implementations of existing helpers.

Tests should defend observable behavior: data-loss races, state transitions, consent boundaries,
request contracts, failure recovery, and meaningful rendering/interaction outcomes. Mock model and
provider boundaries in ordinary tests. Keep test data isolated from the operator's profile and
Keychain. See [testing and migrations](contributing-testing-migrations.md) for commands and schema rules.

## Git and documentation

Branch from `main` and open focused PRs back to `main`. Follow
[the contribution workflow](../CONTRIBUTING.md#make-a-change) for Conventional Commit subjects with
Lore decision trailers, test evidence, and documentation impact. [CI](../.github/workflows/ci.yml)
is the automated merge evidence; [releasing](releasing.md) owns publication. Do not assume a local
Git hook is installed or substitutes for the required checks.
