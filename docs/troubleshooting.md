# Troubleshooting

This guide covers the problems a student or contributor can usually fix without reading source code.

## First checks

- Quit and reopen `Lyra.app` once. The shell will restart only the backend it owns.
- If startup fails, use the fixed recovery screen and inspect `~/Library/Logs/Lyra/backend.log`. It is
  bounded and redacts the home-directory prefix; review any excerpt before sharing it.
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

- In the desktop app, use Settings → Save backup or Restore backup. Finish saving edits first,
  complete the native file dialog, and keep Lyra open while the archive is verified.
- Restore stages and validates the archive before switching profiles and retains a recovery copy.
  The installed-app acceptance limits remain in the [release ledger](release-evidence.md).
- Contributors can also use `./run backup` and `./run restore` for checkout-owned data.
- Choose a new archive path if backup says the target already exists.
- For contributor CLI restore, choose a destination path that does not already exist.
- If restore fails validation, keep the original archive and retry only after fixing the filesystem
  or path issue it reported.

## If you are filing a bug report

- Include the version/build from Settings, macOS version, device model/memory, and reproduction
  steps. Contributors may run `./run diagnostics` for the source stack.
- Diagnostics are designed to redact private information. Review them before sharing; never attach
  raw course documents, databases, credentials, or unredacted screenshots.
- If you are validating packaged-desktop evidence rather than checkout startup, pair that with
  `scripts/desktop_resource_report.py` output or a `scripts/packaged_soak_harness.py` run record.

## Finding setup and recovery tasks

Settings keeps optional Web research and maintenance tasks in labeled disclosures. Open the task
to configure it; an active operation, unresolved error or direct Settings link opens the relevant
section automatically. A remote tutor whose data-flow consent was acknowledged is shown neutrally.
Existing class data prevents non-overwrite import; this restriction appears within Import rather
than as a standing error during ordinary setup.

A failed refresh leaves previously loaded classes, settings and saved work available with Retry.
Retry shows progress and a failed outcome without implying that unsaved or uncertain operations
succeeded. Startup recovery prevents duplicate retries while reconnecting. If an update is ready
but restart is rejected, retain the ready state and use the displayed safe relaunch guidance.
