# Lyra

Lyra is a local-first AI study companion for students. It runs on your machine, keeps class
materials organized by class, and gives you help with documents, chat, solutions, study tools,
and writing.

## What it does now

- Create a class workspace.
- Upload PDFs, text files, Markdown, and images the app can process.
- Chat with your class material in a class-scoped workspace.
- Generate and review solutions.
- Build study decks and quizzes from processed documents.
- Draft and review writing in a dedicated workspace.
- Configure an OpenAI-compatible tutor endpoint and optional web research.

## Privacy and network behavior

Lyra does not use accounts, telemetry, analytics, or update checks.

Its backend binds to loopback, and local embeddings, OCR, and document processing stay on your
machine. Content only leaves the machine when you deliberately use a non-local tutor endpoint or
web research. If the tutor endpoint is remote, Lyra shows that fact in Settings and asks before
sending document text. If you skip Firecrawl, web research is unavailable.

Lyra still needs a tutor endpoint in Settings. A bundled local model is not shipped yet.

## Release posture

Lyra currently runs from a source checkout. macOS on Apple Silicon is the first supported release
target; other platforms may work, but they are not yet part of the release gate. Expect the data
format and setup process to keep evolving until the first packaged release.

## Quick start

Start the full app from the repository root:

```bash
./run
```

Useful commands:

```bash
./run doctor         # Check prerequisites and local health
./run status         # Show owned services and health
./run logs           # Show recent logs
./run stop           # Stop only services owned by this checkout
./run --skip-firecrawl
./run --dev
```

Run `./run doctor` first if you are unsure whether your machine is ready. The launcher checks
Python 3.12+, Node.js 20.9+, pnpm, Docker, disk space, and the required ports before it starts.
Use `./run --skip-firecrawl` when you want a degraded launch without web research.

## Project layout

```text
backend/   FastAPI app, document pipeline, retrieval, settings, storage
frontend/  Next.js app, UI components, hooks, and styles
docs/      Public docs, roadmaps, and historical handoff records
scripts/   Launcher, setup, and evaluation helpers
```

## Documentation

User docs

- [Local deployment](docs/local-deployment.md) - setup, launch, recovery, and degraded mode
- [Troubleshooting](docs/troubleshooting.md) - common failures a student can fix locally
- [Privacy and data location](docs/privacy-and-data-location.md) - what stays local and what can
  leave the machine
- [macOS Apple Silicon release checklist](docs/macos-apple-silicon-release-checklist.md) - release
  gate for clean install, recovery, and data preservation
- [Feature roadmap](docs/feature-roadmap.md) - current priorities and completed surfaces

Contributor docs

- [Architecture](docs/architecture.md) - components, data flow, and API boundaries
- [Code conventions](docs/conventions.md) - style, structure, and testing rules
- [Contributing, testing, and migrations](docs/contributing-testing-migrations.md) - change
  workflow, verification, and schema updates
- [RAG pipeline](docs/rag-pipeline.md) - parsing, chunking, embedding, and retrieval
- [Design system](docs/design-system.md) - tokens, components, and motion rules

Historical records

- [Integration handoff](docs/integration-handoff.md)
- [Phase 2 handoff](docs/phase-2-handoff.md)
- [Phase 3 handoff](docs/phase-3-handoff.md)
- [Phase 3 verification handoff](docs/phase-3-verification-handoff.md)
- [Phase 4 handoff](docs/phase-4-handoff.md)
- [Phase 4 writer integration](docs/phase-4-writer-integration.md)
- [Writer overhaul](docs/writer-overhaul.md)
- [Writer roadmap archive](docs/writer-roadmap.md)

## For contributors

If you are changing the app, start with [docs/architecture.md](docs/architecture.md) and
[docs/conventions.md](docs/conventions.md). If you are changing the launcher or local startup
behavior, read [docs/local-deployment.md](docs/local-deployment.md) first.
