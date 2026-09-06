# PR #70 targeted review follow-ups

> Historical record, retained for provenance. Measurements, task lists, screenshots, and setup
> assumptions describe the recorded revision, not the current release. Start with the
> [documentation index](README.md) for maintained guidance.

This completes the PLA-409 and PLA-431 review of
`1960b4bd3d7c67341a2f1dd96398daad01d538c5`, on base
`ae5957720d6ba5b4be387f7f9cfaa23d9546eea2`. The recovered PLA-404 implementation
and its existing regression coverage remain intact. No redesign or dependencies were added.
The PR and Linear records identify the exact published follow-up head and hosted CI run.

## PLA-409: atomic probe publication

`settings.probe_revision` is a non-secret epoch. A database trigger increments it and clears
both capabilities whenever an endpoint or model changes, including writes outside the route.
Each tools/vision probe captures the epoch before resolving its configuration. Publication is
one conditional SQLite UPDATE against that epoch, so a configuration change committed after
the earlier comparison still prevents the old result from being saved.

Credential writes invalidate and commit the epoch before Keychain mutation. The single-process
desktop backend serializes credential writes and probe snapshots with the same lock; a new
snapshot cannot pair that epoch with the old credential. No SQLite transaction spans Keychain
I/O or remote inference, and the snapshot lock is released before remote inference. Secret
values remain in the established credential store. The separate credential architecture
backlog is not incorporated here.

Regression evidence: the reviewed route implementation failed all **six** new tests that pause
immediately after successful configuration comparison, commit an endpoint/model/credential
change on a second connection, then resume publication. Both tools and vision incorrectly
persisted `supported=1`. The repaired implementation passes all six. Six additional A→B→A
cases prove equality alone cannot revive an old measurement. Another test commits through a
second SQLite connection during Keychain mutation, proving the database is not held open in a
transaction across that operation. Existing network-delay and frontend invalidation/serialized
save/input-preservation tests remain.

Changed files: `backend/api/routes_settings.py`, `backend/core/app_settings.py`,
`backend/storage/migrations/044_probe_revision.sql`, `backend/tests/test_api_settings.py`.

## PLA-431: recover the actual original document

When extracted text is unavailable after page failure, **Save original document** fetches the
actual stored original through the existing authenticated transport. The new document endpoint
returns a bounded attachment with `no-store` and `nosniff`, not a filesystem URL or inline active
content. It reuses the owned-tree, descriptor-based no-follow file protections and returns an
honest, path-free 404 for missing or inaccessible originals. The session/origin/host boundary
is preserved; no session credential is placed in a URL.

The packaged app uses the existing Tauri file dialog to choose a destination, then writes the
fetched bytes. Only a sanitized suggested filename and bytes cross IPC; the selected destination
never returns to JavaScript. The command is restricted to the bundled main window's capability.
Cancellation and save errors are distinct from success. Browser mode downloads an object URL
and reports that the download started. Only the helper-created anchor and exact generated blob
URL receive an allowance in the external-navigation guard; arbitrary blob/download markup
remains blocked. Recovery does not navigate away or change the selected
document, page, or problem. Switching sources aborts a pending original fetch before a late
response can open its save dialog.

Regression evidence: **five backend cases failed before** the endpoint existed; exact-byte
recovery and unsafe/missing-file rejection pass afterward, with added authorization and parent
symlink coverage. **Four frontend recovery cases failed before** the action existed; success,
unavailable original, cancellation, source/page preservation and delayed-source switching now
pass. Runtime IPC assertions explicitly use a mock and do not certify the OS dialog. Rust tests
exercise actual filesystem writes, exact bytes, and symlink/directory rejection. Real-stack
acceptance seeds only its isolated database/files, uses actual HTTP without route interception,
compares downloaded bytes, and checks document/page/problem context on success and failure.

Changed files: `backend/api/routes_documents.py`, `backend/storage/private.py`,
`backend/tests/test_api_documents.py`, `frontend/src/components/solutions/source-pane.tsx`,
`frontend/src/lib/runtime.ts`, `frontend/src/lib/external-links.tsx`,
`frontend/tests/external-links.test.tsx`, `frontend/tests/source-pane.test.tsx`,
`frontend/tests/runtime.test.ts`, `frontend/e2e/acceptance/source-original-recovery.spec.ts`,
`src-tauri/src/lib.rs`, `src-tauri/build.rs`, `src-tauri/capabilities/default.json`,
`src-tauri/permissions/autogenerated/save_original_document.toml`.

## Acceptance boundaries

Physical VoiceOver/NVDA, physical CJK input, native zoom, real-model quality, notarization,
clean-machine distribution and the clean 8 GB extended soak remain unverified release gates.
Development signing and deterministic fixtures do not certify those activities. The separate
study-reliability PR still requires Agent 2's semantic integration and combined verification;
this change does not overwrite or certify that branch. Issues remain In Review while unmerged.

## Final local verification

| Check | Result |
| --- | --- |
| Backend full suite, final collected tests | 2,781 passed, 1 existing skip |
| Affected backend settings/migrations/credentials/documents/private-file set | 186 passed |
| Frontend unit/component suite | 879 passed across 88 files |
| Full real-stack deterministic acceptance, sequential helper-port slot | 115 passed; clean failure ledger and zero owned survivors |
| Focused original recovery and failure gate | 4 passed |
| Chromium smoke and viewport | 31 passed |
| WebKit external navigation | 6 passed |
| Rust desktop tests | 27 passed |
| Ruff lint/format, Prettier, ESLint, TypeScript, Rust format/Clippy | Passed |
| Production frontend build; contrast; active references; diff whitespace | Passed; 27 contrast pairs, zero failures |
| Frozen sidecar build, stage and authenticated smoke | Passed |
| Completed development-signed app | 79 code objects verified; authenticated ephemeral-loopback smoke passed |

Actual native verification used the completed signed app at
`src-tauri/target/release/bundle/macos/Lyra.app` and a separate fixture profile. Its real WKWebView
loaded the isolated class and solution from the bundled backend. On document `recovery-2.pdf`,
problem 2/page 2, page rendering and extracted text were unavailable. The actual macOS dialog
opened; Cancel displayed **Save cancelled**. Save wrote an 847-byte PDF identical to the stored
original (SHA-256 `e619b20243786df1c249b342fd58fc980dbcfb949425aa598ba9e34ea6a6f4ee`).
After moving only that test original aside, another recovery attempt showed **The original
document is missing or inaccessible** without opening a save dialog. Returning to page view
retained document 2/page 2 and the selected problem. This is native behavior, separate from the
mocked IPC and browser download assertions. The verified app and backend quit gracefully, and
the previously running app was restored with its original data.

Retained unsuccessful runs are not counted as passing evidence:

- A neighboring PR #71 baseline used an old global-sweep teardown and terminated this run's
  backend: 38 tests passed before connection failures. Agent 2 confirmed the cause and disabled
  that baseline's sweep.
- The first new browser tests had an ambiguous source selector and used the partial Playwright
  header view. Exact combobox/hash-route assertions and the complete header view corrected the
  test setup without weakening checks or increasing timeouts.
- Actual byte-download acceptance then exposed the existing external-navigation interceptor's
  blob restriction. The private app-created download allowance fixed this integration boundary;
  arbitrary blob/download markup is still blocked by regression tests.
- Concurrent acceptance suites also collided on their pre-existing fixed helper port 19500:
  this run had 114 passes and one helper startup failure. Agent 2 independently confirmed the
  collision; run-specific helper-port work remains in PR #71. The final sequential PR #70 run
  passed all 115 tests without product/harness changes or timeout increases for that collision.

Existing editor bundle-size advisories and backend dependency deprecation warnings remain.
Hosted CI is tracked on the exact head in the PR and Linear evidence; local passes are not
represented as hosted results. No merge was performed.

## Failed-overwrite follow-up (reviewed head be6aed7)

Review [issuecomment-5554629488](https://github.com/oliverdougherC/Lyra/pull/70#issuecomment-5554629488)
identified truncation before a fallible write in `write_original_copy`. This follow-up adopts
its explicitly permitted **no-overwrite policy**: existing destinations remain untouched,
with an instruction to choose another filename. Successful replacement is deliberately not
supported; a fresh filename succeeds. The PLA-409 revision barrier, original-byte endpoint,
source-context recovery, and other remediation remain intact.

On Unix, the production helper opens the parent directory once, creates an exclusive random
0600 sibling with `openat(O_CREAT | O_EXCL | O_NOFOLLOW)`, writes all bytes and calls
`sync_all`, then publishes with same-directory `linkat`. This atomic no-replace operation
rejects any entry that appeared after validation, including symlinks and directories. The
existing destination is never opened or truncated. Directory-relative cleanup removes only
the temporary name created by this operation; failed exclusive creation never acquires cleanup
ownership. A retained directory descriptor prevents parent renaming from redirecting cleanup.
Non-Unix saving fails closed until an equally safe native implementation is supplied; the
supported desktop product and native CI run on macOS. Filesystems without hard-link support
return a pre-publication failure without creating the destination.

Errors before publication preserve destination absence, or existing bytes under the early
no-overwrite rejection. After successful publication, cleanup/directory-sync failure reports
**the document was saved but cleanup or durability could not be confirmed**. There is no
rollback claim. Cleanup is attempted on all owned-temporary exit paths, including through the
drop guard; a persistent filesystem cleanup error can leave a temporary sibling. Native
cancellation remains `Ok(false)`. The source pane recognizes only the two exact path-free
native outcome messages, retaining generic reporting for arbitrary errors and the existing
missing-original message. The action stays available to choose another filename.

Changed files: `src-tauri/src/lib.rs`,
`frontend/src/components/solutions/source-pane.tsx`, `frontend/tests/source-pane.test.tsx`,
and this evidence record. The truncating destination-write path is removed; no dependencies,
redesign, or changes to Agent 2's branch are included.

Fresh verification is distinct from the historical signed-app verification above:

- Native Rust: **34 passed** (eight production-helper filesystem tests and 26 existing tests).
  Workspace/all-targets tests, all-features Clippy with warnings denied, and Rust formatting
  passed. The helper cases cover exact binary bytes/private mode,
  pre-write, partial-write (real 4096-byte prefix), flush, and publication fault checkpoints;
  existing and absent destinations; successful save to another filename; cleanup;
  symlink/dangling-symlink/directory/FIFO rejection; racing file/symlink/directory publication;
  parent rename; and post-publication cleanup/durability errors with retained saved bytes.
  Checkpoint failures are injected into the production helper, not a reimplementation; the
  racing-entry cases additionally execute real OS `linkat` failures. Existing destinations
  reject before reaching injected I/O stages because overwriting is explicitly prohibited.
- Mocked frontend: the two native outcome assertions failed against the previous generic-error
  rendering, then passed after the correction. Unknown path-bearing errors remain hidden.
  The runtime/source-pane/external-link suites pass **46 tests**, including native cancellation.
  These assertions do not execute the OS dialog.
- Real-stack original-document recovery acceptance: **2 passed**, real HTTP/download byte
  comparison and source/page/problem preservation, with a clean failure ledger and teardown.
- Backend documents/settings regressions: **65 passed** (existing deprecation warnings only).
- Frontend ESLint, TypeScript and Prettier checks passed. Hosted CI results are recorded
  on the published head in the PR.

Per the explicit instruction for this follow-up, the local packaged app was not rebuilt,
replaced, or launched. Native filesystem verification above executes compiled production Rust;
new macOS dialog/manual signed-bundle verification is not claimed. The required existing CI
workflow retains its own build/test artifact lanes. PLA-431 and PLA-404 reads both returned
HTTP 502 during this follow-up; reconciliation will be attempted again with the final evidence.
Nothing is merged or marked Done; stacked PR #71 still needs its owner's base update and
combined verification after this head is published.
