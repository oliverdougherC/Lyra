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
| Database and its `-wal` / `-shm` sidecars | `0600` | Created or hardened with no-follow descriptors before SQLite opens the database. |
| Fallback API key (`data/.api_key`) | `0600` | Created private from the first byte. |
| Backup archive | `0600` | Created private from the first byte, because it lives outside the data tree and has no owner-only directory to hide behind. |
| Restored data and database | `0700` dirs / `0600` files | Extracted privately and published only after verification; model runtimes retain their owner executable bit. |
| Model files (`data/models/`) | `0700` dir | The directory is private; the bundled runtime keeps its executable bit, so it is not forced to `0600`. |

The first launch after upgrading tightens an older data tree that a previous version left broader,
walking it once. **Attached external workspaces are never touched**: they are your own project
trees, and Lyra reads and edits their files without rewriting their permissions.

### What Lyra treats as its own

Lyra's owned tree is the data directory (`LYRA_DATA_DIR`, default `data/`) and everything it
creates beneath it: `uploads/`, `text/`, `pages/`, `models/`, the SQLite database and its
sidecars, the sentinel that records the one-time upgrade, and the fallback API-key file. The
backup archive is owned too, even though it is written outside the tree. Those are the only paths
Lyra sets modes on. The data directory's own *ancestors* — the folders above it in your home
layout — are yours; Lyra creates them if they are missing but never re-permissions a folder that
already existed, and they may be symlinks.

### Symlinks

Lyra never follows a symlink out of its owned tree to create through it, write through it, change
its permissions, or walk into it. The boundary is enforced, not assumed:

- **The data directory must be a real directory.** If `LYRA_DATA_DIR` is a symlink, Lyra refuses
  to start with an actionable error rather than recursively hardening or walking whatever it points
  at. (Symlinks *above* the data directory, in your home layout, are fine.)
- **No directory or file *inside* the tree may be a symlink to somewhere outside it.** If an older
  install linked, say, `uploads/` or a cache directory out to another disk, Lyra fails closed the
  next time it would create or write beneath that link, instead of silently placing coursework — or
  changing permissions — at the link's target. The one-time upgrade walk likewise never descends a
  symlinked directory and never changes a link or its target.
- **A sensitive file is opened without following a link.** Writing the fallback key, extracted text,
  an upload, a rendered cache entry, or the upgrade sentinel refuses if a symlink sits where the file
  belongs, so it can never truncate or overwrite an unrelated file the link points at. Rendered PNG
  bytes go through that no-follow writer at their deterministic partial names before an atomic rename;
  a cache hit is accepted only when the final entry is a real current-user-owned regular file.
- **An explicit `LYRA_DB_PATH` that is a symlink is refused** rather than opened and chmodded through
  — Lyra will not modify an external file while reporting the configured path as secured. A database
  placed at a real path outside the data tree is still created or hardened to `0600` before SQLite
  opens it, without truncating an existing database. Predictable `-wal` and `-shm` entries are likewise
  required to be real current-user-owned regular files and secured before WAL mode is enabled. Lyra
  creates (and owns) only the immediate directory it makes for an external database. A pre-existing
  external parent may remain `0755`, but a group- or world-writable parent is rejected because another
  user could otherwise replace a prepared sidecar name in the instant before SQLite reopens it.

### Non-POSIX filesystems

These modes are POSIX. On Windows, `chmod` only toggles the read-only bit and there is no atomic
no-follow open; Lyra relies on the per-user location of the data directory and does a best-effort
check instead. On a POSIX filesystem that genuinely cannot carry modes (some network mounts report
"operation not supported"), Lyra tolerates the unsupported chmod for the same reason — the directory
location is the isolation there. Any *other* failure to set a mode on owned state is surfaced rather
than passed over, so Lyra does not report a private layout it did not actually achieve.

## What can leave the machine

- The tutor endpoint can be remote. Lyra treats any non-loopback endpoint as remote unless you
  explicitly acknowledge it in Settings.
- When the endpoint is remote and acknowledged, document text may be sent to it for chat,
  extraction, solving, drafting, and related features.
- The class agent (research, code, and command profiles) is bound by the same rule. An agent turn
  sends the tutor endpoint your conversation history and question, and on later tool rounds it can
  send workspace file contents, fetched-page evidence, and command-planning context. If the
  endpoint is remote and not acknowledged, the turn is refused before anything is stored or sent —
  the consent is re-checked on every turn against the exact endpoint the turn would use.
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
- `./run restore` recreates the data tree with `0700` directories and `0600` sensitive files even
  under a permissive umask. An external restored database is `0600` without changing the mode of its
  pre-existing parent directory. On POSIX, that parent must be owned by the current user and must not
  be group- or world-writable, matching the safety check used when the backend opens the database.
- Deleting a document removes its upload, extracted text, and rendered pages.
- Deleting a class removes the uploads and derived text and pages for every document in that class.
- `./run stop` stops Lyra's owned services only. It does not delete user data.
