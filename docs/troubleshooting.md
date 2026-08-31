# Troubleshooting

This guide covers the problems a student or contributor can usually fix without reading source code.

## First checks

- Quit and reopen `Lyra.app` once. The shell will restart only the backend it owns.
- If startup fails, use the fixed recovery screen and keep `~/Library/Logs/Lyra/backend.log` for a
  bug report. It is bounded and redacts the home-directory prefix.
- Confirm `~/Library/Application Support/Lyra` remains present; replacing or deleting the app does
  not delete course data or optional models.

## Contributor checkout diagnostics

`./run doctor`, `./run status`, `./run logs`, and `./run stop` remain contributor tools for the
source test stack. Python, Node, pnpm, and fixed development ports are not installed-app
prerequisites.

## If the app starts but looks wrong

- Reopen `Lyra.app`. If only a source build looks stale, rebuild the Vite assets and `.app`.

## If web research is unavailable

- Open Settings and confirm an Exa API key is configured.
- Confirm `allow_web_research` is enabled for the workspace you are testing.
- Use the explicit Exa connection test in Settings if configuration looks right but searches still
  fail.
- Missing or failed Exa configuration disables web research without making documents, chat, study,
  or drafts unavailable.

## If the tutor endpoint is remote

- Open Settings and confirm the endpoint host is the one you intended.
- If the endpoint is non-local, acknowledge remote document-text sending before using features that
  send course material upstream.
- If you only want local operation, point Lyra at a loopback endpoint instead.

## If acceptance fails locally

- Stop any existing stack with `./run stop`.
- Re-run `./scripts/run-acceptance.sh` from the repository root.
- If the script reports a port collision, clear the conflicting process before retrying.

## If work was interrupted

- Reopen `Lyra.app` (or restart the contributor stack when testing from source).
- Interrupted ingestion, solution, study, draft, and agent work is reconciled on startup.
- If the interrupted action was explicitly cancelled or its source was deleted, rerun the action
  instead of waiting for the old job to finish.

## If backup or restore fails

- For this migration review build, the proven atomic backup/restore engine remains available to
  contributors through `./run backup` and `./run restore`; the native file-selector surface is a
  remaining packaged-flow gate and must not be reported as complete.
- Choose a new archive path if backup says the target already exists.
- Restore into a path that does not already exist.
- If restore fails validation, keep the original archive and retry only after fixing the filesystem
  or path issue it reported.

## If you are filing a bug report

- Export the packaged resource/runtime reports when reproducing a desktop issue; contributors may
  also run `./run diagnostics` for the source stack.
- The diagnostics bundle is redacted: no document text, no API keys, and no raw absolute private
  paths.
- If you are validating packaged-desktop evidence rather than checkout startup, pair that with
  `scripts/desktop_resource_report.py` output or a `scripts/packaged_soak_harness.py` run record.
