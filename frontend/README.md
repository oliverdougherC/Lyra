# Lyra frontend

Lyra's React 19 interface is built by Vite and embedded as static assets in the Tauri
desktop application. Production has no Node or frontend HTTP server. Use this directory
for contributor HMR against a source backend.

## Local development

```bash
pnpm install --frozen-lockfile
pnpm dev
```

`VITE_API_BASE` may select a development backend; otherwise the browser fallback is
`http://127.0.0.1:8000`. In `Lyra.app`, the API address and per-launch credential come
from the narrow `desktop_bootstrap` Tauri command and remain in memory.

Useful scripts:

```bash
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
pnpm test:acceptance
```

## Frontend structure

- `src/app/` - route components and application providers
- `src/router/` - typed hash routing and navigation shims
- `src/components/` - reusable UI components
- `src/lib/` - API client, hooks, and shared utilities
- `src/styles/` - global styles and design tokens
- `src/types/` - hand-written TypeScript mirrors of backend schemas

## References

- [Architecture](../docs/architecture.md)
- [Code conventions](../docs/conventions.md)
- [Design system](../docs/design-system.md)

The frontend uses pnpm and the lockfile is committed. Node and pnpm are contributor tools,
not installed-product dependencies. Heavy workspaces, including the writing editor, load lazily.
