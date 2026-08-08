# Lyra Frontend

This directory contains the Next.js app that powers Lyra's browser UI.

The full app is normally started from the repository root with `./run`. Use this directory when
you want frontend-only development against a running backend.

## Local development

```bash
pnpm install
pnpm dev
```

The frontend expects Lyra's backend to be available on `http://127.0.0.1:8000`.

Useful scripts:

```bash
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run build
pnpm run format:check
```

## Frontend structure

- `src/app/` - route segments and page shells
- `src/components/` - reusable UI components
- `src/lib/` - API client, hooks, and shared utilities
- `src/styles/` - global styles and design tokens
- `src/types/` - hand-written TypeScript mirrors of backend schemas

## References

- [Architecture](../docs/architecture.md)
- [Code conventions](../docs/conventions.md)
- [Design system](../docs/design-system.md)

The frontend uses pnpm and the lockfile is committed in this directory. The repository launcher
expects Node.js 20.9+ and pnpm to be installed.
