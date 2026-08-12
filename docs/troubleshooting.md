# Troubleshooting

This guide covers problems a student can usually fix without reading source code.

## First checks

- Run `./run doctor` to check Python, Node.js, pnpm, Docker, disk space, and required ports.
- Run `./run status` to see whether Lyra already owns a healthy stack.
- Run `./run logs` after a failed start or a bad restart.
- Run `./run stop` before retrying when the launcher thinks this checkout still owns stale
  processes.

## If `./run` will not start

- Start Docker Desktop or Docker Engine.
- Free ports `3000`, `8000`, and `3002`, or stop the other Lyra checkout that owns them.
- Install Python 3.12+, Node.js 20.9+, and pnpm.
- Try `./run --no-browser` if browser launch is the only failing step.

## If the app starts but looks wrong

- Use `./run stop`, then start again with `./run`.
- Use `./run --clean` if the frontend cache looks stale.
- Check `./run logs` for the last launcher, backend, or frontend error.

## If web research is unavailable

- Use `./run --skip-firecrawl` to start Lyra in degraded mode.
- Local documents, chat, study tools, and drafts still work.
- Firecrawl-backed search and scrape stay unavailable until the bundled stack is healthy.

## If work was interrupted

- Start Lyra again with `./run`.
- Interrupted ingestion, solve, study, draft, tool, and command work is reconciled during startup.
- If a document or class was deleted while a run was in flight, rerun the action instead of
  waiting for the old job.

## If backup or restore fails

- Run `./run stop` and retry the backup if it reports that the database is still busy. That message
  means another process still has the SQLite file open for writing.
- Choose a new archive path if `./run backup` says the target already exists.
- Restore into a path that does not already exist. Lyra refuses in-place overwrite on purpose.
- If the backup came from a checkout that used `LYRA_DB_PATH` outside `LYRA_DATA_DIR`, pass an
  explicit `--db-path` on restore and create that parent directory first.
- If restore says the archive failed validation or SQLite `quick_check`, discard that restore
  attempt and keep the original backup file. Lyra stages the restore first, so the requested target
  paths stay untouched when validation fails.
- If restore says the external database path could not be finalized, Lyra already rolled back the
  requested targets. Retry with fresh explicit paths after fixing the filesystem problem.

## If you are filing a bug report

- Run `./run diagnostics` to write `logs/diagnostics.json`, a structured snapshot of this
  install: schema currency, tutor and web-research configuration, which optional models are
  present, content counts, and platform.
- The file is safe to attach. It carries no document text, no tutor API key, and no private
  path: the endpoint URL is reduced to whether it is local, the key to present-or-absent, and
  absolute paths to a `<lyra>` or `~` anchor.
- It works even when the app will not start: `./run diagnostics` builds the same snapshot
  offline when the backend is not reachable.

## If the tutor endpoint is remote

- Open Settings and confirm the endpoint is the one you meant to use.
- If the endpoint is not local, turn on the acknowledgement before sending document text.
- If you only need local documents, use a local endpoint or leave the endpoint unset.
