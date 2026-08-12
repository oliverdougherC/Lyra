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

## File permissions

Everything Lyra owns is created private to the user who runs it, independent of the process
`umask`, so a permissive umask or a shared parent directory cannot widen it:

| What | Mode | Notes |
| --- | --- | --- |
| Data directories | `0700` | `data/` and every directory Lyra creates beneath it, including per-class upload and per-document page directories. This is the control that matters: a directory another user cannot enter hides every file inside it. |
| Uploads, extracted text, page and figure caches | `0600` | The coursework and everything derived from it. |
| Database and its `-wal` / `-shm` sidecars | `0600` | Re-hardened whenever Lyra opens the database. |
| Fallback API key (`data/.api_key`) | `0600` | Created private from the first byte. |
| Backup archive | `0600` | Created private from the first byte, because it lives outside the data tree and has no owner-only directory to hide behind. |
| Model files (`data/models/`) | `0700` dir | The directory is private; the bundled runtime keeps its executable bit, so it is not forced to `0600`. |

The first launch after upgrading tightens an older data tree that a previous version left broader,
walking it once. **Attached external workspaces are never touched**: they are your own project
trees, and Lyra reads and edits their files without rewriting their permissions.

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
  the active SQLite database. That includes `data/.api_key` when Lyra had to fall back to it, so the
  archive is created `0600` (owner-only) from the first byte. It does not include OS-keychain
  secrets, `.lyra/`, or `logs/`.
- Deleting a document removes its upload, extracted text, and rendered pages.
- Deleting a class removes the uploads and derived text and pages for every document in that class.
- `./run stop` stops Lyra's owned services only. It does not delete user data.
