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
    routes_documents.py
    routes_chat.py
    routes_profile.py
    routes_settings.py
    schemas.py          # Pydantic request/response models
  core/
    __init__.py
    classes.py          # Class workspace management
    sessions.py         # Chat session management
    profiles.py         # Profile facts, confidence, confirmation
    ingestion.py        # Background ingestion job orchestration
  rag/
    __init__.py
    parse.py            # PyMuPDF extraction, scanned-page detection
    ocr.py              # Unlimited-OCR via llama.cpp
    chunk.py            # Semantic chunking, token ceiling
    embed.py            # Embedding, and the ONLY place task prefixes are applied
    retrieve.py         # Brute-force KNN, recency weighting, budgeting
  storage/
    __init__.py
    database.py         # SQLite connection, migrations
    models.py           # Table definitions
    vector.py           # sqlite-vec wrapper, re-index
    secrets.py          # OS keychain access
  llm/
    __init__.py
    client.py           # OpenAI-compatible client, endpoint locality check
    prompts.py          # System prompt templates
    stream.py           # SSE streaming
  tests/
    conftest.py
    test_chunk.py
    test_retrieve.py
    test_ingestion.py
    test_api_*.py
```

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
      page.tsx              # Home, class list
      classes/[id]/page.tsx # Class workspace
      settings/page.tsx
    components/
      ui/                   # shadcn/ui base components
      layout/               # Sidebar, header, navigation
      chat/
      classes/
      documents/
      profile/              # Profile view, fact confirmation
      settings/
    lib/
      api.ts                # The only module that talks to the backend
      utils.ts              # cn(), formatters
      hooks/
    styles/
      globals.css           # Lyra tokens plus the shadcn bridge
    types/
      index.ts
  tests/
    setup.ts
    *.test.tsx
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
- Endpoint locality detection and the remote-extraction skip rule
- Profile fact confidence and the rule that unconfirmed low-confidence facts never enter a prompt
- API request and response contracts

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
