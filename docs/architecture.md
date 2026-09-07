# System Architecture

## Current contract

Lyra is a single-user desktop study application whose implemented runtime is:

- `Tauri 2` desktop shell
- `Vite + React` frontend
- packaged Python `FastAPI` backend
- `SQLite` for local state
- optional `Exa` web research
- an OpenAI-compatible tutor endpoint that may be loopback-local or remote

Use `./run --dev` for hot-reloading contributor development. The packaged app is the product.
See [local deployment](local-deployment.md) for development signing and the
[release evidence ledger](release-evidence.md) for current distribution gates.

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
- Browser development explicitly uses `VITE_API_BASE` or injected test bootstrap data. Packaged
  builds fail closed unless the Tauri `desktop_bootstrap` command succeeds.
- Hash routes keep a selected same-page citation in the reserved `lyra-anchor` query parameter.
  Citation jumps therefore preserve the class/chat/draft/solution/study route, query parameters,
  reload state, and browser/WebKit history instead of replacing the route fragment.
- The UI talks only to the FastAPI API surface; it does not call tutor providers or Exa directly.

The production browser suites exercise two different boundaries:

- `pnpm test:e2e` checks the built frontend with Playwright smoke coverage.
- `pnpm test:acceptance` starts the real backend, fake tutor fixture, and built frontend together.

## Desktop trust boundaries

The packaged bootstrap protocol is version `1`. Rust passes one inherited IPv4 loopback listener,
its exact address, the parent PID, `X-Lyra-Session`, and a fresh 64-character lowercase-hex secret
over stdin. Python rejects missing or extra fields and echoes the complete readiness contract;
Rust validates it before exposing only `protocolVersion`, `apiBase`, `sessionHeaderName`, and
`sessionSecret` to the webview. A cached child is reused only after an authenticated `/ready`
request. Retry gracefully stops the old backend, reclaims verified helper ownership, and launches
a new child with a new secret.

The main window allows 540×600 logical-pixel resizing and clamps its initial content size to
the primary display work area, reserving room for window chrome. Browser responsive tests do
not certify native focus, display scaling or usability at that minimum; candidate-specific
native checks remain required.

The main webview denies top-level navigation away from the packaged origin and denies new windows.
All rendered anchors are intercepted centrally. One typed `open_external_url` command revalidates
normalized public HTTP(S) destinations in Rust and uses Tauri's opener plugin; file paths, generic
shell execution, credentials-bearing URLs, unsafe schemes, and local/private/reserved hosts are not
exposed.

Desktop data import is a separate narrow boundary. Tauri's native directory picker writes a
private selection record and returns only an opaque token and label to React. The backend creates a
locked SQLite backup plus hashed file manifest in a resumable stage without changing live data.
Publication occurs only after Tauri stops the backend: the fixed frozen sidecar
`--publish-desktop-import` mode performs backup-first promotion with rollback and startup recovery,
then Tauri relaunches the backend with a fresh bootstrap secret. Non-empty user destinations are
refused and the selected source is never modified by the importer.

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

- Embeddings and optional reranking run locally through managed helpers.
- The tutor endpoint is user-configured and may be loopback-local or remote. The current opt-in
  document recognition path also uses that endpoint, sending page images after locality/consent
  checks. A separate local OCR helper exists, but ingestion currently selects the tutor path.

Remote endpoints are supported as a real operating mode, not as a hidden fallback. The system:

- labels endpoint locality in Settings;
- requires acknowledgement before document text or requested recognition images go to a non-loopback endpoint; and
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
minutes idle. Eviction keeps lifecycle admission locked through termination and ownership cleanup,
so a new request cannot reuse a helper already selected for shutdown. Failed termination retains
ownership and blocks reuse until process death is proved, even if its health endpoint responds.
The ownership record persists shutdown intent before signals, so backend restart also reclaims
that process before allowing a replacement; it cannot adopt a helper still shutting down. Application quit closes admission for
all three helpers before reclaiming them; queued background work cannot restart them afterward.
Release evidence helpers include:

- `scripts/desktop_resource_report.py` for deterministic resource inventories
- `scripts/desktop_runtime_report.py` for the retained Lyra/Tauri/WebKit process inventory, per-process resource sample, and explicit still-open physical gates
- `scripts/packaged_soak_harness.py` for release-candidate soak preparation and manual evidence

Those helpers provide bounded evidence; they do not establish release approval.

## Backup and updates

Native Settings commands coordinate profile backup/restore and explicit update checks. The shell
pauses the backend around profile publication and app replacement, preserves recovery state, and
restarts with a fresh session. Backup validation lives in `backend/desktop_backup_archive.py`;
publication/recovery spans `backend/desktop_backup.py` and `src-tauri/src/backup.rs`. Updates verify
trusted signed artifacts and schema compatibility before replacement; see
[releasing](releasing.md) and `src-tauri/src/updater.rs`. No update check runs automatically at launch.
