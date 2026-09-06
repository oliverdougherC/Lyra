# Privacy and Data Location

Lyra is local-first. It does not use accounts, telemetry, analytics, or automatic update checks.

## Local data today

The packaged app uses platform-owned locations rather than writing into `Lyra.app`:

| Data | Packaged macOS location | Notes |
| --- | --- | --- |
| Durable profile | `~/Library/Application Support/Lyra/` | Uploads, extracted text, database, models, and helper ownership. |
| Rendered pages/cache | `~/Library/Caches/Lyra/` | Regenerable page and frontend cache data. |
| Logs | `~/Library/Logs/Lyra/` | Bounded rotating backend logs with the home prefix redacted. |
| Credentials | macOS Keychain | Tutor and Exa keys when Keychain is available. |

The contributor checkout intentionally keeps its mutable working data under repository-relative
paths:

| Data | Default location | Notes |
| --- | --- | --- |
| Uploaded files | `data/uploads/` | Original files added to a class. |
| Extracted text | `data/text/` | One text file per ingested document. |
| Rendered pages | `data/pages/` | Cached page images. Safe to regenerate. |
| Model files | `data/models/` | Embedding, OCR, rerank, and helper runtime files. |
| Database | `data/lyra.db` | Classes, settings, progress, and durable jobs. |
| Launcher state | `.lyra/` | Checkout-owned runtime metadata for `./run`. |
| Logs | `logs/` | Local launcher, backend, and frontend logs. |

If `LYRA_DATA_DIR` is set, the data tree moves with it. If `LYRA_DB_PATH` is also set, that path
overrides the derived database location.

The packaged-smoke and recovery tools may override those roots with `LYRA_DATA_DIR`,
`LYRA_CACHE_DIR`, `LYRA_LOGS_DIR`, and `LYRA_MODELS_DIR`. Ordinary installed launches do not need
those variables.

## What stays on the machine

These never need to leave the local machine for Lyra to function:

- uploaded course files
- extracted text and rendered pages
- the SQLite database and local caches
- local helper runtimes and model weights
- embeddings, OCR, and reranking inputs/outputs

Lyra continues to harden the data tree as private-to-user storage. The data directory and the
directories it creates beneath it are owner-only, and sensitive files remain owner-readable and
owner-writable only.

## What can leave the machine

Network traffic happens only in explicit user-triggered cases:

- Tutor traffic: when the configured OpenAI-compatible tutor endpoint is used
- Web-research traffic: when Exa is configured, enabled, and used
- First-use document helper model downloads: the required embedding weights (about 146 MB) are fetched from Hugging Face when first processing is requested. This download does not upload documents. Interrupted transfers can be retried; verified cached weights work offline. Optional OCR and reranking weights are not downloaded automatically
- Application updates: an explicit check retrieves GitHub-hosted channel metadata; an explicit download retrieves the signed app archive. No update check occurs on launch.

The tutor endpoint may be loopback-local or remote. Lyra treats non-loopback endpoints as remote,
labels them as such, and requires acknowledgement before document text is sent there.

Exa is optional and disabled until an API key is configured. Lyra does not probe Exa during launch
or readiness checks. When web research is used, only bounded search/fetch requests leave the
machine; raw local paths, credentials, quoted private passages, and other obvious secrets are
screened by the server-side query guard before the request is sent.

## Keys

- The tutor API key is stored in the OS keychain when possible, otherwise in a private fallback
  file under the data directory.
- The Exa API key follows the same secret-storage abstraction and is never stored in ordinary
  settings rows.
- API keys are write-only across the settings API surface and are never returned in responses.
- Readiness and CI do not issue a live Exa request by default.

## Deletion, backup, and restore

- Deleting a document removes its upload, extracted text, and rendered pages.
- Deleting a class removes only that class's derived local data.
- `./run backup --archive ...` captures the current data tree and database.
- `./run restore --archive ... --data-dir ...` restores into a new target instead of overwriting an
  existing path.

Settings also provides native Save backup and Restore backup dialogs. Restore validates into a
private staging folder, retains the current profile as a recovery copy, and recovers interrupted
publication on relaunch. Current credential authority is preserved so an old archive cannot
resurrect a forgotten key or attach a current key to an archived endpoint. Backup archives may
contain private fallback credentials; protect them like the original data. Keychain values are
not exported. Final installed-app evidence is tracked in the release ledger.
