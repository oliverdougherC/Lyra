# macOS Apple Silicon Release Checklist

Use this checklist for a packaged release candidate on a clean Apple Silicon Mac.

This checklist does not imply the release is currently signed, notarized, or soak-complete. It is
the evidence bar that must be met before those claims are made.

## 1. Candidate artifact

- [ ] Obtain the exact candidate `.app` bundle or `.dmg` artifact built from one merged commit.
- [ ] Record the commit SHA, workflow run ID, and artifact name.
- [ ] Run `python scripts/desktop_resource_report.py --root app=/path/to/Lyra.app --output ...`
      and retain the JSON report.

## 2. Clean first launch

- [ ] Confirm the machine is Apple Silicon and running a supported macOS version.
- [ ] Launch the packaged app without any pre-created profile state.
- [ ] Confirm the packaged frontend loads and the packaged Python backend reaches a usable state.
- [ ] Confirm the app shows tutor locality and Exa configuration state honestly.
- [ ] Confirm web research is unavailable by default until an Exa key is configured.

## 3. Core workflows

- [ ] Create a class, upload a document, and confirm ingestion succeeds.
- [ ] Run chat, solution, study, and draft flows through the packaged app boundary.
- [ ] If the tutor endpoint is remote, confirm the remote-acknowledgement path appears before
      document text is sent.
- [ ] If the tutor endpoint is loopback-local, confirm the local-path UI remains accurate.

## 4. Restart and recovery

- [ ] Prepare a soak run with `python scripts/packaged_soak_harness.py prepare ...`.
- [ ] Restart the packaged app while durable work is in flight.
- [ ] Confirm interrupted ingestion, solution, study, draft, or agent work reconciles honestly on
      restart.
- [ ] Confirm the restart path does not duplicate helper processes or silently lose work.

## 5. Degraded dependencies

- [ ] Confirm the app remains usable when Exa is unconfigured or temporarily unavailable.
- [ ] Confirm a remote tutor outage lands in a truthful retryable state.
- [ ] Confirm startup does not issue an unsolicited Exa request.

## 6. Data and privacy

- [ ] Record where the packaged runtime stores its profile, database, logs, and cached resources.
- [ ] Confirm backups and restores still preserve the local data contract.
- [ ] Confirm no document text, API keys, or private absolute paths leak into diagnostics.
- [ ] Confirm deleting a document or class removes only the intended local data.

## 7. CI and release status

- [ ] Confirm the merge commit passed `CI Gate`.
- [ ] Confirm the Python production dependency audit passed for the same commit.
- [ ] Confirm the desktop artifact lane produced the package tested here.
- [ ] Confirm signing and notarization status from actual release evidence; do not infer either from
      a green build alone.
- [ ] Confirm the release-candidate soak is complete before calling the package release-ready.
