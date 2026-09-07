# Lyra project instructions

## Desktop app is the primary product

Users primarily interact with the packaged `Lyra.app`. After making changes, always
rebuild the desktop app before declaring the task complete. A source edit, passing
tests, or a frontend-only build is not sufficient delivery.

- Follow `docs/local-deployment.md` to rebuild the Python sidecar, stage it, build the
  frontend, and bundle the desktop app. Include backend changes in the frozen sidecar.
- Sign the completed local bundle with `scripts/sign_local_app.py` using the same development
  identity across rebuilds, as documented in `docs/local-deployment.md`. Ad-hoc signing resets
  the backend identity and can repeatedly invalidate Keychain “Always Allow” approvals.
- Run the frozen-backend smoke check against the completed, signed app bundle.
- Verify the rebuilt backend with disposable profiles. Native acceptance requires a genuinely
  isolated account/device with no incumbent sharing the compiled-ID single-instance endpoint.
  Backend `LYRA_*` selectors and null/fail Keyring do not isolate WebKit's default store or IPC.
  Do not launch/select/reopen the production identity on the normal account as a test.
- Report unavailable native acceptance separately; continue safe builds and frozen-backend checks.

The canonical normal installation is `/Applications/Lyra.app`. Only use
`./scripts/build_local_app.sh` for explicitly authorized installation; it replaces that app and
consumes the intermediate build bundle. It does not open the app unless `--open` is explicit.
Quit an authorized installation gracefully before replacement and preserve application data.
For integration review, retain `src-tauri/target/release/bundle/macos/Lyra.app` and use the
lower-level build steps in `docs/local-deployment.md`. Validate the installer against a copy at
an explicit private destination without `--open`; do not replace the normal installed app.
Check embedded source revisions before installation, even when version strings match.

## Documentation impact

Before declaring work complete, review whether behavior, setup, configuration, architecture, or contributor workflow changes require documentation updates. Update maintained docs in the same PR, or state why no documentation change is needed. Run `uv run python scripts/check_docs.py` and the active-reference scan. Start task branches from current main; reconcile and verify against current main before merging. Delete merged branches only after checking for unique work and active worktrees.

## Lore commits and release notes

Use a Conventional Commit prefix and explain why the change was made in the intent line.
Add relevant native Git trailers after the narrative body; omit trailers that add no value.

```text
fix: prevent stale saves from replacing newer writing

Describe the trigger, constraints, and approach for future contributors.

Constraint: External constraint shaping the decision
Rejected: Alternative considered | why rejected
Confidence: high
Scope-risk: narrow
Directive: Forward-looking warning when needed
Tested: Specific verification performed
Not-tested: Known verification gaps
```

Use synthetic fixtures and isolated data for tests. Never commit user documents, databases,
model weights, or credentials. Keep independent agents within explicit file ownership and
preserve other contributors' edits. For cleanup, write a bounded plan and establish regression
coverage before editing. No new dependencies unless explicitly requested.
