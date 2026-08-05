# Code Conventions

## General

- **Language:** English for all code, comments, documentation, and commit messages.
- **Em dashes:** Never use em dashes in any output. Use hyphens, en dashes, or restructure. This
  applies to documentation and UI strings as well as code.
- **Emojis:** Never use emojis in code, comments, documentation, or UI strings.
- **Line length:** 100 characters maximum, enforced by the formatters below, not by habit.
- **File encoding:** UTF-8, LF line endings.

## Python (Backend)

### Style
- **Formatter and linter:** Ruff. Line length must be configured explicitly, because Ruff defaults
  to 88 and would otherwise silently disagree with the rule above.
- **Type hints:** Required on all function signatures. No `Any` unless unavoidable.
- **Docstrings:** Google-style for public modules and classes. One-liners for private helpers.
- **Imports:** Standard library, third-party, local. Sorted by Ruff.

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "ASYNC", "S", "RET", "SIM"]

[tool.pytest.ini_options]
testpaths = ["backend/tests"]
```

### Naming
- Modules and packages: `snake_case`
- Classes: `PascalCase`
- Functions and methods: `snake_case`
- Constants: `SCREAMING_SNAKE_CASE`
- Private attributes: single leading underscore

### Project Structure
```
backend/
  __init__.py
  main.py               # FastAPI app, middleware, lifespan, 127.0.0.1 bind
  config.py             # pydantic-settings
  api/
    __init__.py
    routes_classes.py
    routes_documents.py   # Upload, status, reingest, move between classes, delete
    routes_chat.py
    routes_profile.py
    routes_settings.py
    routes_solutions.py   # The solver's view of the artifact model (Phase 2)
    schemas.py            # Pydantic request/response models
  core/
    __init__.py
    app_settings.py     # Resolving the tutor config, key included, from the settings row
    classes.py          # Class management
    errors.py           # LyraError and its subclasses, each carrying an HTTP status
    sessions.py         # Chat session management
    profiles.py         # Class-scoped fact store, extraction, evidence, confirmation
    consolidation.py    # The per-class pass that merges wording variants and sets noise aside
    ingestion.py        # Background ingestion job orchestration
    artifacts.py        # Artifact and part CRUD, revisions, provenance (Phase 2)
    segmentation.py     # Problem lists from chunk markers plus a model pass (Phase 2)
    solver.py           # Background solve job orchestration: segment, solve, verify (Phase 2)
    solving.py          # One problem: evidence gathered, prompt built, reply parsed (Phase 2)
    verification.py     # The tool-backed check, and the rules that turn it into a verdict
  tools/                # Pure computation, no prompts, no models, no database (Phase 2)
    __init__.py
    result.py           # ToolResult: the one shape every tool returns
    cas.py              # SymPy behind a bounded subprocess
    _cas_runner.py      # The child process. Executed, never imported
    units.py            # Dimensional analysis via pint
  rag/
    __init__.py
    parse.py            # PyMuPDF extraction, scanned-page detection
    chunk.py            # Semantic chunking, token ceiling
    embed.py            # Embedding, and the ONLY place task prefixes are applied
    retrieve.py         # Brute-force KNN, recency weighting, budgeting
    locate.py           # Where a problem sits on its page, for the source pane (Phase 2)
    render.py           # Source pages rasterized to PNG and cached (Phase 2)
    tokens.py           # The one token estimate every budget is counted in
  storage/
    __init__.py
    database.py         # SQLite connection, migrations
    migrations/         # Numbered, applied in order, never edited once shipped
    secrets.py          # OS keychain access
  llm/
    __init__.py
    client.py           # OpenAI-compatible client, streaming and single completions
    locality.py         # Whether an endpoint is local, which gates sending document text
    embed_server.py     # The embedding model's own llama.cpp process, owned by the app
    prompts.py          # System prompt templates
    replies.py          # Parsing what a model sent back, including its stray prose
    tools.py            # Tool-calling loop and tool registry (Phase 2)
  tests/
    conftest.py
    test_chunk.py
    test_retrieve.py
    test_ingestion.py
    test_api_*.py
```

There is no `storage/models.py` or `storage/vector.py`: table definitions live in the numbered
migrations, which are the only description of the schema, and the vector table is addressed through
plain SQL against `sqlite-vec` rather than through a wrapper. There is no `rag/ocr.py` either - OCR
was cut from Phase 1, and `unsupported` is the state that says so.

### Error Handling
- Use Pydantic validation for inputs. Let FastAPI return 422.
- Internal errors: log with traceback, return a generic 500.
- Never expose stack traces, filesystem paths, or endpoint URLs to the frontend.
- Domain errors raise `LyraError` subclasses carrying a user-facing message, mapped to HTTP status
  by a single exception handler.
- Ingestion failures are recorded on the job with a stage marker, never swallowed.
- The tutor API key must never appear in a log line, error message, or API response.

### Configuration
- Non-secret settings: `pydantic-settings`, overridable by environment variables
- User-editable defaults: `config.yaml` in the project root
- The tutor API key lives in the OS keychain via `keyring`, never in `config.yaml` and never in
  `lyra.db`. See the secret storage section of architecture.md for why.

## TypeScript (Frontend)

### Style
- **Formatter:** Prettier, with `printWidth: 100` so it agrees with the line-length rule
- **Linter:** ESLint with TypeScript and React rules
- **Strict mode:** Always (`"strict": true`)

```json
// .prettierrc
{
  "semi": false,
  "singleQuote": true,
  "quoteProps": "consistent",
  "trailingComma": "all",
  "printWidth": 100,
  "endOfLine": "lf"
}
```

`trailingComma` is `all`, not `es5`: this is a TypeScript project and `es5` omits trailing commas in
function parameter lists, producing noisier diffs.

### Naming
- Components: `PascalCase` export
- Files: `kebab-case`
- Hooks: `useCamelCase`
- Utilities: `camelCase`
- Constants: `UPPER_SNAKE_CASE`

### Project Structure
```
frontend/
  src/
    app/
      layout.tsx
      page.tsx                                   # Home, class list
      classes/[id]/page.tsx                      # Class hub: chats, solutions, documents, profile
      classes/[id]/chat/page.tsx                 # Class workspace: conversation and documents
      classes/[id]/solutions/page.tsx            # The solver's index (Phase 2)
      classes/[id]/solutions/new/page.tsx        # Solve setup: sources and title
      classes/[id]/solutions/[artifactId]/page.tsx  # The solver workspace, one route, four phases
      settings/page.tsx
    components/
      ui/                   # shadcn/ui base components
      layout/               # Shell, sidebar, header, bottom nav, header portals
      chat/
      classes/              # Class list, the class hub and its panels, class dialogs
      documents/            # The list, a row, the upload well, move between classes
      profile/              # Profile view, fact confirmation
      solutions/            # The solver's screens (Phase 2)
      settings/
    lib/
      api.ts                # The only module that talks to the backend
      format.ts             # Shared display formatters
      utils.ts              # cn()
      hooks/                # One file per resource, wrapping TanStack Query
    styles/
      globals.css           # Lyra tokens plus the shadcn bridge
    types/
      index.ts              # Hand-written mirror of the backend's Pydantic schemas
  tests/
    setup.ts
    *.test.{ts,tsx}
```

### State Management
- Server state: TanStack Query
- Client state: `useState` and `useReducer`. No Redux or Zustand unless a concrete need appears.
- Form state: React Hook Form with Zod

### API Communication
- All backend calls go through `lib/api.ts`. Components never call `fetch` directly.
- `fetch` with `AbortController` for cancellable requests
- Streaming uses `fetch` plus a `ReadableStream` reader and manual SSE parsing. `EventSource` cannot
  be used because it only issues `GET`.
- Ingestion progress is polled through `GET /api/documents/{id}/status` with TanStack Query
- Errors: toast for request failures, inline messages for validation

## Design Tokens

Design tokens are defined in [design-system.md](design-system.md) and implemented once in
`frontend/src/styles/globals.css`, where the Lyra tokens are also mapped onto the shadcn/ui token
names. Never hardcode a color in a component file.

Spacing uses Tailwind's built-in 4px scale. Do not define parallel `--space-*` variables.

```tsx
// Wrong: hardcoded color, parallel spacing token
<div className="bg-[#F3F0EB]" style={{ padding: 'var(--space-4)' }} />

// Right: semantic token plus Tailwind spacing
<div className="bg-card p-4" />
```

Text on a colored fill always uses that fill's paired foreground token. Never pick a label color by
eye.

## Testing

Tests defend observable contracts. A test that restates the implementation is worse than no test.

**What is worth testing:**
- Chunking boundaries, the 2048-token ceiling, and oversized-problem splitting
- Retrieval ordering, recency weighting, and context-budget trimming
- Embedding prefix application, since a regression here is silent
- Ingestion state transitions, including failure paths
- Endpoint locality detection, and that no path sends document text to an unacknowledged
  remote endpoint
- Profile fact confidence and the rule that unconfirmed low-confidence facts never enter a prompt
- API request and response contracts
- That no verdict overstates itself: `unchecked` and `uncheckable` never render as a pass,
  and `verified` is never reached without a tool call actually having run

**What is not:**
- That a Pydantic model has the fields it was declared with
- Framework plumbing, getters, or constants
- Snapshot tests of markup

**Rules:**
- Backend: `pytest`. Frontend: Vitest with Testing Library.
- Deterministic and isolated. No test depends on another's ordering or leftover state.
- Never call a live LLM, OCR, or embedding model in a test. Fake the client at its interface.
- Tests use a temporary SQLite file, never the developer's `lyra.db`.
- A bug fix adds the test that would have caught it.

## Git

The repository is under version control from the first commit. Nothing about the project is tracked
only on one machine.

### Branch Strategy
- `main` - Known-good code
- `dev` - Active development
- `feature/<short-name>` - Branched off `dev`

### Commit Messages
- Format: `<type>: <short description>`
- Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `style`, `test`
- Examples:
  - `feat: add document upload endpoint`
  - `fix: apply search_query prefix to retrieval embeddings`
  - `docs: record OCR serving spike outcome`

### Conventions
- Small, atomic commits. One logical change each.
- No `fix typo` or `update` commits. Be specific.
- Squash feature branches before merging to `dev`.
- Never commit GGUF weights, `lyra.db`, `data/`, or anything from `.myenv/`.

### Tooling
- The pre-commit hook runs the Python and TypeScript formatters and linters on staged files only.
  It must not run the test suite; a slow hook gets bypassed and then stops being a hook.
- CI runs formatters, linters, and both test suites. There is no release workflow until there is
  something to release.
