# Lyra

Lyra is a desktop-first, local-first AI study workspace for one student and their own course
material. `Lyra.app` combines a thin `Tauri 2` shell, a static `Vite/React` frontend, and a frozen
Python backend, with SQLite storage, optional Exa-backed web research, and a tutor endpoint that can
be either loopback-local or remote.

## Download and install

The first public beta is **not published yet**. Review candidates are not approved tester releases.
The permanent [Download Lyra Beta](https://oliverdougherc.github.io/Lyra/beta/) entrypoint will
become available after the first approved publication. Until then, consult the
[release evidence ledger](docs/release-evidence.md); an Actions artifact or draft release is not a
public download.

For an approved release, download the Apple Silicon DMG, open it, drag Lyra to Applications,
then launch Lyra from Applications. Do not disable Gatekeeper to treat an unnotarized candidate
as an approved release. App replacement preserves the separate student-data folder and Keychain.

### Requirements and first use

- Apple Silicon Mac; Intel Macs are not supported by this beta.
- macOS 14 or newer: the bundled SQLite vector extension sets this floor. The final bundle's
  complete native dependency inventory must confirm it before publication.
- The 8 GB reference-device acceptance gate remains open; the development machine has 24 GB.
  No 8 GB performance certification is claimed.
- An OpenAI-compatible tutor endpoint and any key it requires. Lyra does not bundle a general
  tutor model. Remote services may charge separately; configure the endpoint in Settings and
  acknowledge remote document processing before using it.
- Exa is optional and needs a separate user-owned key for web research.

Create a class, add course documents, then configure/test the tutor in Settings. First-use document
processing downloads required embedding weights (about 146 MB) from Hugging Face; this needs
network access and disk space even when the tutor is local. It does not upload course documents.
Optional OCR/reranking weights are not downloaded automatically. The helper
runtime is bundled. Cached local data remains available offline, while inference and web research
need their configured services. Testers do not need a source checkout, Homebrew, Python, Node,
Docker, or a terminal.

PDF export/print and archive backup/restore without contributor tools remain explicit installed-app
release gates. Do not rely on developer-installed Pandoc/Typst as evidence for a clean tester Mac.

## What it does now

- Create class workspaces and organize course documents by class.
- Chat against uploaded material in a class-scoped workspace.
- Generate solutions, flashcards, quizzes, and draft-writing artifacts.
- Run a bounded general class agent with reviewable file and command proposals.
- Configure an OpenAI-compatible tutor endpoint and optional Exa web research.

## Privacy and network behavior

Lyra does not use accounts, telemetry, analytics, or automatic update checks.

Course files, extracted text, rendered pages, embeddings, and the SQLite database stay on the local
machine. Network traffic happens only when the user deliberately:

- configures a tutor endpoint and sends a request to it; or
- enables web research and uses Exa;
- uses document processing that needs first-use helper model downloads; or
- explicitly checks for or downloads an application update from GitHub.

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
- [Release and update process](docs/releasing.md)
- [Release evidence and outstanding gates](docs/release-evidence.md)
- [Security reporting](SECURITY.md)
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
