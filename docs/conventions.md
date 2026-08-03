# Code Conventions

## General

- **Language:** English for all code, comments, documentation, and commit messages.
- **Em dashes:** Never use em dashes (`—`) in any output. Use en dashes (`–`), hyphens (`-`), or restructure.
- **Emojis:** Never use emojis in code, comments, or UI strings.
- **Line length:** 100 characters max. Wrap before truncation.
- **File encoding:** UTF-8, LF line endings.

## Python (Backend)

### Style
- **Formatter:** Ruff (auto-format on save)
- **Type hints:** Required on all function signatures. No `Any` unless unavoidable.
- **Docstrings:** Google-style for public modules and classes. Brief one-liners for private helpers.
- **Imports:** Standard library, third-party, local (separated by blank lines). Sorted with ruff.

### Naming
- Modules and packages: `snake_case`
- Classes: `PascalCase`
- Functions and methods: `snake_case`
- Constants: `SCREAMING_SNAKE_CASE`
- Private attributes: single leading underscore `_internal`

### Project Structure
```
backend/
  __init__.py
  main.py              # FastAPI app creation, middleware, lifespan
  api/                  # Route handlers
    __init__.py
    routes_classes.py
    routes_documents.py
    routes_chat.py
    routes_settings.py
    schemas.py          # Pydantic request/response models
  core/                 # Business logic
    __init__.py
    classes.py          # Class workspace management
    sessions.py         # Chat session management
    profiles.py         # Profile extraction and management
  rag/                  # OCR + RAG pipeline
    __init__.py
    ingest.py           # Document ingestion orchestration
    parse.py            # Text extraction (PDF, text)
    ocr.py              # Unlimited OCR integration
    chunk.py            # Semantic chunking
    embed.py            # Embedding model wrapper
    retrieve.py         # Vector search and retrieval
  storage/              # Data persistence
    __init__.py
    database.py         # SQLite connection, migrations
    models.py           # SQLAlchemy/SQLite models
    vector.py           # sqlite-vec wrapper
  llm/                  # LLM integration
    __init__.py
    client.py           # OpenAI-compatible client abstraction
    prompts.py          # System prompt templates
    stream.py           # Streaming response handling
```

### Error Handling
- Use Pydantic validation for API inputs. Let FastAPI return 422 for invalid requests.
- Internal errors: log with traceback, return generic 500 to the client.
- Never expose stack traces or internal paths to the frontend.
- Database errors: wrap in `LyraError` with a user-friendly message.

### Configuration
- Environment variables via `pydantic-settings`
- Config file: `config.yaml` in the project root (user-editable)
- Sensitive values (API keys) stored in the database, encrypted at rest

## TypeScript (Frontend)

### Style
- **Formatter:** Prettier
- **Linter:** ESLint with TypeScript + React rules
- **Strict mode:** Always enabled (`"strict": true` in tsconfig)

### Naming
- Components: `PascalCase`
- Files: `kebab-case` for file names, `PascalCase` for exported component
- Hooks: `useCamelCase`
- Utilities: `camelCase`
- Constants: `UPPER_SNAKE_CASE`

### Project Structure
```
frontend/
  src/
    app/                    # Next.js app router pages
      layout.tsx
      page.tsx              # Home / class list
      classes/
        [id]/page.tsx       # Class workspace
      settings/page.tsx     # Settings panel
    components/             # Reusable UI components
      ui/                   # shadcn/ui base components
      layout/               # Sidebar, header, navigation
      chat/                 # Chat interface components
      classes/              # Class card, class list
      documents/            # Document upload, list, viewer
      settings/             # Settings form components
    lib/                    # Utilities and API client
      api.ts                # Backend API client
      utils.ts              # cn(), formatters, helpers
      hooks/                # Custom React hooks
    styles/                 # Global styles
      globals.css           # Tailwind directives, CSS variables
    types/                  # Shared TypeScript types
      index.ts
```

### State Management
- Server state: TanStack Query (React Query)
- Client state: React `useState` / `useReducer` (no Redux/Zustand unless necessary)
- Form state: React Hook Form with Zod validation

### API Communication
- All backend calls go through `lib/api.ts`
- Use fetch with AbortController for cancellable requests
- Streaming responses use ReadableStream + SSE parsing
- Error handling: toast notifications for failures, inline errors for form validation

## Git

### Branch Strategy
- `main` - Production-ready code
- `dev` - Active development
- `feature/phase-1-foundation` - Feature branches off `dev`

### Commit Messages
- Format: `<type>: <short description>`
- Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `style`
- Examples:
  - `feat: add document upload endpoint`
  - `fix: handle empty OCR output gracefully`
  - `style: apply design system colors to class cards`

### Conventions
- Small, atomic commits. One logical change per commit.
- No `fix typo` or `update` commits. Be specific.
- Squash feature branch commits before merging to `dev`.

## Design Tokens

All design tokens (colors, spacing, typography) are defined in `docs/design-system.md` and implemented as CSS custom properties in `frontend/src/styles/globals.css`. Never hardcode color values or spacing in component files - always reference the token.

Example:
```css
/* globals.css */
:root {
  --bg-primary: #FAF8F5;
  --accent-primary: #7BA17D;
  --space-4: 16px;
}

/* Component */
.card {
  background-color: var(--bg-secondary);
  padding: var(--space-4);
}
```
