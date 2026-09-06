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
- If Lyra is running, quit it gracefully before replacing its bundle and reopen the
  rebuilt app afterward. Preserve the user's application data and settings.
- Verify the rebuilt app and its backend start successfully. If rebuilding or launch
  verification fails, resolve the failure or explicitly report the blocker; do not
  present a source-only fix as complete.

The local review bundle is `src-tauri/target/release/bundle/macos/Lyra.app`.

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
