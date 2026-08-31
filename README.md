# Lyra

Lyra is a desktop-first, local-first AI study workspace for one student and their own course
material. `Lyra.app` combines a thin `Tauri 2` shell, a static `Vite/React` frontend, and a frozen
Python backend, with SQLite storage, optional Exa-backed web research, and a tutor endpoint that can
be either loopback-local or remote.

## What it does now

- Create class workspaces and organize course documents by class.
- Chat against uploaded material in a class-scoped workspace.
- Generate solutions, flashcards, quizzes, and draft-writing artifacts.
- Run a bounded general class agent with reviewable file and command proposals.
- Configure an OpenAI-compatible tutor endpoint and optional Exa web research.

## Privacy and network behavior

Lyra does not use accounts, telemetry, analytics, or update checks.

Course files, extracted text, rendered pages, embeddings, and the SQLite database stay on the local
machine. Network traffic happens only when the user deliberately:

- configures a tutor endpoint and sends a request to it; or
- enables web research and uses Exa.

Remote tutor endpoints are first-class and explicitly labeled. Lyra treats non-loopback endpoints as
remote, requires acknowledgement before document text is sent there, and keeps Exa disabled until an
API key is configured.

## Release posture

This branch produces reviewable Apple Silicon `.app` and DMG artifacts. They are not a finished
signed release: Developer ID signing, notarization, the physical clean-8-GB-Mac run, and the final
release-candidate soak remain explicit gates.

## Contributor quick start

Run the development stack from the repository root:

```bash
./run
```

Useful commands:

```bash
./run doctor
./run status
./run logs
./run stop
./scripts/run-acceptance.sh
```

`./run` is only the contributor lifecycle for this checkout. It starts the backend and Vite
frontend so focused changes can be developed quickly; it is not an alternative installed product.
For the real-backend browser suite, use `./scripts/run-acceptance.sh`.

## Project layout

```text
backend/   FastAPI app, storage, retrieval, tutor orchestration, tests
frontend/  Vite/React app, routes, components, browser tests
docs/      Live product docs plus labelled historical records
scripts/   Verification helpers, acceptance entrypoints, packaging evidence tools
packaging/ PyInstaller spec, component inventory, sidecar staging
src-tauri/ Thin native shell, capabilities, CSP, and lifecycle ownership
```

## Documentation

User and release docs

- [Architecture](docs/architecture.md)
- [Local deployment](docs/local-deployment.md)
- [Privacy and data location](docs/privacy-and-data-location.md)
- [Troubleshooting](docs/troubleshooting.md)
- [macOS Apple Silicon release checklist](docs/macos-apple-silicon-release-checklist.md)
- [Feature roadmap](docs/feature-roadmap.md)
- [Security and CI gates](docs/security-and-ci-gates.md)

Contributor docs

- [Code conventions](docs/conventions.md)
- [Contributing, testing, and migrations](docs/contributing-testing-migrations.md)
- [RAG pipeline](docs/rag-pipeline.md)
- [Design system](docs/design-system.md)
- [Desktop migration inventory](docs/desktop-migration-inventory.md)

Historical records

- [Integration handoff](docs/integration-handoff.md)
- [Phase 2 handoff](docs/phase-2-handoff.md)
- [Phase 3 handoff](docs/phase-3-handoff.md)
- [Phase 3 verification handoff](docs/phase-3-verification-handoff.md)
- [Phase 4 handoff](docs/phase-4-handoff.md)
- [Phase 4 writer integration](docs/phase-4-writer-integration.md)
- [Writer overhaul](docs/writer-overhaul.md)
- [Writer roadmap archive](docs/writer-roadmap.md)
