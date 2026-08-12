# Privacy and Data Location

Lyra is local-first. It does not use accounts, telemetry, analytics, or update checks.

## What stays on your machine

By default, Lyra keeps its working data under `data/`.

| Data | Default location | Notes |
| --- | --- | --- |
| Uploaded files | `data/uploads/` | Original files you added to a class. |
| Extracted text | `data/text/` | One `.txt` file per document. |
| Rendered pages | `data/pages/` | Cached page images. Safe to delete and regenerate. |
| Model files | `data/models/` | Downloaded weights and local model runtimes. |
| Database | `data/lyra.db` | Workspace state, settings, and progress. |
| Launcher state | `.lyra/` | Ownership and install metadata for `./run`. |
| Logs | `logs/` | Launcher, backend, frontend, and supervisor logs. |

If you set `LYRA_DATA_DIR`, Lyra moves the data tree with it. If you also set `LYRA_DB_PATH`, that
path overrides the database location.

## What can leave the machine

- The tutor endpoint can be remote. Lyra treats any non-loopback endpoint as remote unless you
  explicitly acknowledge it in Settings.
- When the endpoint is remote and acknowledged, document text may be sent to it for chat,
  extraction, solving, drafting, and related features.
- Firecrawl runs on loopback at `127.0.0.1:3002`, but it can fetch public pages on your behalf
  when web research is enabled.
- Lyra does not send document text directly to Firecrawl. It sends web requests only through the
  loopback service started by `./run`.

## Keys and deletion

- The tutor API key is stored in the OS keychain when one is available.
- On machines without a working keyring backend, Lyra falls back to `data/.api_key` with `0600`
  permissions and says so plainly in the UI.
- The key is never returned by an API response or written to logs. This holds by construction, not
  by trusting the tutor endpoint: an endpoint's own error body is classified into a bounded category
  (for example context-window-exceeded or unsupported-response-format) and only that category and the
  HTTP status are logged, so a server that reflected the key or course text back cannot get it into a
  log line or a user-facing error.
- `./run backup --archive /path/to/lyra-backup.tgz` captures the configured `LYRA_DATA_DIR` tree and
  the active SQLite database. That includes `data/.api_key` when Lyra had to fall back to it, but it
  does not include OS-keychain secrets, `.lyra/`, or `logs/`.
- Deleting a document removes its upload, extracted text, and rendered pages.
- Deleting a class removes the uploads and derived text and pages for every document in that class.
- `./run stop` stops Lyra's owned services only. It does not delete user data.
