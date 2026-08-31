# Feature Roadmap

This is Lyra's live roadmap for the desktop migration branch.

Historical phase documents remain useful evidence, but they are not the current queue. The current
queue is about finishing a dependable desktop runtime around the product that already exists.

## Direction

Lyra should be a local-first study workspace for one student and their own course material, with a
desktop runtime that is honest about what stays local, what may go remote, and what is still not
release-ready.

Work is prioritized in this order:

1. Protect student data and privacy.
2. Make failure and degraded behavior explicit.
3. Preserve durable work across restart and outage paths.
4. Prove behavior with CI and release evidence.
5. Only then broaden the product surface.

## Current product surface

- Class workspaces, documents, profile facts, and retrieval
- Class-scoped chat and agent turns
- Solution generation and follow-up
- Flashcard decks and quizzes
- Draft writing, review, and revision history
- Loopback or remote OpenAI-compatible tutor endpoints
- Optional Exa-backed web research

## Now: desktop migration stabilization

### 1. Desktop runtime and packaging

- [x] Move the frontend to Vite/React runtime assumptions.
- [x] Add packaged-runtime evidence helpers for resource inventory and soak preparation.
- [x] Land the `src-tauri/` desktop shell and package wiring on the migration branch.
- [x] Prove packaged Python sidecar startup from a built desktop artifact, not just from the source
  checkout.
- [x] Add a macOS Apple Silicon app and DMG artifact lane to CI.
- [ ] Confirm that artifact lane passes on the pushed branch tip.
- [ ] Complete the real release-candidate soak on one exact merged commit.
- [ ] Finish signing and notarization only after the packaged runtime itself is stable.

### 2. Privacy, safety, and network honesty

- [x] Keep embeddings, OCR, and reranking local.
- [x] Treat remote tutor endpoints as explicit, acknowledged remote operation.
- [x] Keep Exa disabled until configured and avoid probing it during startup.
- [x] Bound and audit web research requests before they leave the machine.
- [x] Preserve the same privacy contract in the packaged runtime.
- [ ] Verify the contract again during the physical clean-machine release soak.

### 3. CI and evidence

- [x] Keep backend formatting, lint, tests, and Python security in `CI Gate`.
- [x] Keep frontend format/lint/typecheck/unit/build/browser/acceptance coverage in `CI Gate`.
- [x] Add active-reference absence checks so live docs and workflows stop drifting back to
  retired runtime language.
- [x] Add packaged-Python smoke and deterministic resource-report evidence lanes.
- [x] Make Rust/Tauri fmt, clippy, test, audit, and the macOS artifact job execute when the checked-in
  shell is present.
- [ ] Confirm the new hosted lanes pass after the branch is pushed.

### 4. Release documentation

- [x] Rewrite live docs around Vite, Exa, remote-or-loopback tutor operation, and the desktop
  migration target.
- [x] Separate live docs from historical records.
- [x] Record local exact-commit packaged launch, process, resource, and failure evidence.
- [ ] Record signed/notarized clean-machine evidence and keep the Apple Silicon checklist current.

## Later

- Bundle supported local tutor model choices so a separate tutor server is optional rather than
  required.
- Add memory-aware model recommendations and stricter per-feature capability checks.
- Expand release validation beyond one candidate soak to cover upgrades and sustained multi-session
  resource stability.
- Finish signed distribution and update-path design after the packaged runtime itself is proven.
