# System Architecture

## Current contract

Lyra is a single-user desktop study application whose implemented runtime is:

- `Tauri 2` desktop shell
- `Vite + React` frontend
- packaged Python `FastAPI` backend
- `SQLite` for local state
- optional `Exa` web research
- an OpenAI-compatible tutor endpoint that may be loopback-local or remote

The contributor checkout still uses `./run` for focused development. Reviewable unsigned desktop
artifacts build now; Developer ID signing, notarization, and the final physical release soak are not
complete as of August 30, 2026.

## Topology

Development and packaged modes share the same logical boundary:

```text
browser or Tauri webview
  -> Vite/React UI
       -> loopback FastAPI
            -> SQLite + local files
            -> local helper processes for embeddings / OCR / reranking when configured
            -> OpenAI-compatible tutor endpoint (loopback or remote)
            -> Exa HTTPS API only when web research is enabled and used
```

In development, the frontend is served by Vite and the backend binds `127.0.0.1:8000`. In packaged
mode, Tauri bootstraps the UI with the API base and session header; the frontend no longer depends
on a standalone browser server.

## Frontend

The frontend is now a Vite/React application with client-side routing.

- Route state is handled in the client.
- Runtime bootstrap comes from either `VITE_API_BASE`, injected browser bootstrap data, or the
  Tauri `desktop_bootstrap` command.
- The UI talks only to the FastAPI API surface; it does not call tutor providers or Exa directly.

The production browser suites exercise two different boundaries:

- `pnpm test:e2e` checks the built frontend with Playwright smoke coverage.
- `pnpm test:acceptance` starts the real backend, fake tutor fixture, and built frontend together.

## Backend

The backend owns the durable contract:

- loopback binding and Host/Origin protections
- SQLite migrations and storage permissions
- class, document, chat, solution, study, draft, and agent lifecycles
- local helper-process ownership and reclamation
- tutor-endpoint locality and consent checks
- Exa configuration, query guarding, and source persistence

Readiness is intentionally narrow. Lyra reports database readiness and web-research configuration
without probing Exa during launch. Exa is tested only through the explicit settings action or
mocked transport in CI.

## Inference posture

Two inference surfaces exist and are deliberately separate:

- Infrastructure models stay local. Embeddings, optional OCR, and optional reranking are managed by
  Lyra and never leave the machine.
- The tutor endpoint is user-configured and may be loopback-local or remote.

Remote endpoints are supported as a real operating mode, not as a hidden fallback. The system:

- labels endpoint locality in Settings;
- requires acknowledgement before document text goes to a non-loopback endpoint; and
- preserves the same rule for chat, drafting, solving, and agent turns.

## Web research posture

Web research is optional and Exa-backed.

- No Exa request is made on app launch or readiness checks.
- An Exa API key must be configured before web research is available.
- Searches and fetches are bounded, audited, and filtered through the server-side query and URL
  policies before they leave the machine.
- Missing or failed Exa configuration disables web research without making the rest of Lyra
  unhealthy.

## Storage and packaging

The source checkout keeps explicit relative overrides for tests. The packaged app uses
`~/Library/Application Support/Lyra` for durable data/models, `~/Library/Caches/Lyra` for rendered
page caches, `~/Library/Logs/Lyra` for bounded logs, and Keychain for tutor/Exa credentials. Mutable
state is never written into `Lyra.app`. Local helpers hold active leases and are evicted after five
minutes idle. Release evidence helpers include:

- `scripts/desktop_resource_report.py` for deterministic resource inventories
- `scripts/desktop_runtime_report.py` for the owned process tree, memory, CPU, package, and file-count sample
- `scripts/packaged_soak_harness.py` for release-candidate soak preparation and manual evidence

Those helpers support the migration; they do not claim the packaged app is signed, notarized, or
release-ready.
