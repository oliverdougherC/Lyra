# Native/workspace release repairs

Scope: PLA-471 and PLA-473, with packaged backup/restore added for the installed-app gate.

Extraction plan for backup reuse: first run existing launcher backup/restore regressions; move only the stdlib archive validation/copy/extraction helpers to a backend module; preserve the launcher's public helper names and archive format; compose packaged operations around the shared helpers; verify round-trip, populated-profile restoration and interrupted rename recovery. No external commands or added dependencies.

Workspace: synthetic ancestor-swap read and listing regressions failed before the patch on macOS (outside sentinel bytes / outside file metadata); 34 focused workspace tests pass after descriptor-rooted reads, listings, mutation snapshots/parents, and private bounded search snapshots. Search snapshots have a 32 MiB / 10,000-entry cap and share the search deadline; oversize scopes request a narrower subfolder.

Native: blocking bootstrap/retry/import and quit cleanup now use controlled blocking workers with serialized lifecycle ownership. Import publication (60 seconds) and helper reclamation (10 seconds) have absolute deadlines, bounded 8 KiB stdout/stderr capture, process-group cleanup and reaping. 40 native tests passed including noisy/hung helpers, inherited-pipe descendants, existing readiness/retry/import identity and updater tests. Actual signed-app responsive painting and installed soak evidence remains an integration gate; source tests do not establish it.

## Final source verification (September 2026)

- Workspace checks: 35 tests passed. The baseline search was also replayed with a swap-and-restore barrier and returned the synthetic outside sentinel; the patched search returns only descriptor-read snapshot content. Inherited `.gitignore` policy is retained. No live user files were used.
- Native checks: `cargo test --manifest-path src-tauri/Cargo.toml --lib` passed 44 tests, including the integrated updater modules; `cargo clippy --manifest-path src-tauri/Cargo.toml --lib --tests -- -D warnings` passed. The `desktop_print` command uses Tauri's supported macOS webview print API for the native Print / Save as PDF path.
- Packaged backup, launcher, bootstrap, and import checks: 161 tests passed, including all 105 existing source-launcher tests and 14 packaged backup tests. Ruff passed on the changed Python files.
- Backup archive helpers now live in `backend/desktop_backup_archive.py`; the source launcher preserves its public helpers and archive format. SQLite's backup API captures commits landing between checkpoint and writer-lock acquisition, covered by an injected second-connection commit.
- The frozen entry supports `--desktop-backup-create <selected path>` and `--desktop-backup-restore <selected archive>`. Native dialogs select the paths; restore requires explicit native confirmation and replaces a populated profile while retaining its predecessor. Native operations stop and restart the backend under the lifecycle owner and use a five-minute helper deadline.
- Restore verifies archive members, database integrity and schema, stages privately, fsyncs the payload/journal, and reconciles interrupted publication or rollback before opening the live database. Tests cover power loss before/after old-profile rename, after stage publication, and after rollback's final rename. A newer live schema plus a pending journal remains unchanged and blocked.
- Restore keeps the **current** connection settings, endpoint/credential reference pairing, authority/tombstone files, credential generation and slots. Archived credentials cannot reverse Forget. An old schema unable to represent current credential references requires tutor reauthentication. Keychain is neither exported nor overwritten; archives can contain fallback credential values and must be stored privately. Exa resurrection was reproduced failing-before and passes after this repair. Independent security review replayed Forget, changed endpoint, rollback and WAL cases successfully.

Remaining integration evidence: build/sign the frozen bundle, exercise actual native save/restore/print dialogs and authenticated restart, run the isolated packaged backup roundtrip, and verify responsive window painting during native lifecycle operations. No source test is claimed as signed-app or physical acceptance. Desktop backup currently requires the standard database layout `data_dir/lyra.db`; startup without a restore journal continues to support configured external database paths. Interrupted helpers may leave private partial staging files; they do not publish a partial archive or delete the retained profile.

## Actual frozen backup exercise and cross-profile correction

The actual `dist/lyra-backend/lyra-backend` binary (SHA-256 `f8e4cb3c7baf49d832b2a0db5833b7c42546577845e5c80b43a41a87c279317d`) passed a disposable-profile exercise with `PATH=/usr/bin:/bin`, no `PYTHONPATH`, null Keychain, isolated model/cache directories and an outside-checkout working directory. The driver initialized the real schema through authenticated frozen startup, seeded a class/document/saved draft/deck/source/settings, created a private archive, refused an existing archive, restored into a populated profile, verified original content hashes and retained-current copies, preserved the current endpoint/model, relaunched the frozen backend, and rejected an incompatible-schema archive without changing committed database/source bytes. Schema inspection created an empty WAL and a 32 KiB shared-memory sidecar; this is reported separately from durable-data mutation.

A second-profile extension then reproduced a genuine defect: absolute document paths still pointed to the original profile after successful restore. Backup staging now reuses the existing desktop-import path rewriter with an optional stage directory (normal import behavior unchanged). Its regression and all backup/import/launcher tests pass (144). The expanded driver also requires a successful authenticated original-file download with matching SHA-256 after cross-profile frozen relaunch. That expanded pass must be collected against the refrozen/signed candidate; the earlier binary's successful same-profile result is not represented as this final pass.

Rerun command: `/Users/ofhd/Developer/Lyra/.venv/bin/python .omx/release/verify_packaged_backup.py <absolute frozen binary> --output .omx/release/final-frozen-backup-acceptance.json`. The driver itself uses only the Python standard library; child operations use only the selected frozen binary and the restricted environment. Canonical finding/evidence recorded on PLA-327 comment `357dc74c-fd81-494d-aee2-c1c36cd83bd9`.

## Expanded frozen pass after runtime-specific repair

The refrozen sidecar SHA-256 `3d6a01dc7318c269983286388ca2dd7778286499e9fd4c2dd4f2ed66edf6df73` passed the expanded driver in 3.61 seconds; result is `.omx/release/frozen-backup-acceptance-expanded.json`. All ten checks now pass, including original-document download through the authenticated frozen HTTP endpoint after restoration into a second profile, with the expected source SHA-256. A committed source-WAL essay update survives the archive roundtrip, and the archive contains no staged database WAL/SHM files.

The intermediate binary exposed a second genuine runtime defect: `with sqlite3.connect(...)` does not close the connection. Garbage collection in the frozen runtime closed a staged database after tar traversal listed its WAL/SHM files, producing FileNotFoundError as those transient files disappeared. Backup snapshot readers/writers now explicitly close before archiving; the regression holds strong connection references so garbage collection cannot hide a missing close. New backup readers/settings writers were fixed similarly. Existing updater migration/schema backup connections were inspected and already close explicitly before hashing. Backup/import/launcher checks now pass 145 tests. Helper failures emit only exception type and at most four code basenames/line numbers; no exception values or private path prefixes.

For native UI acceptance the driver also supports `--prepare-only`, `--keep-profile`, and `--profile-root <new/empty outside-checkout directory>`. `.omx/release/native-ui-profile.json` records a retained synthetic profile, complete isolated launch environment, class 9001, and visible saved draft `/classes/9001/drafts/9001`; it has no endpoint or credentials configured and uses the null Keychain backend. The final signed-bundle binary must still run the same expanded driver; this result does not claim native-dialog or signed-app acceptance.


## Actual native UI follow-up

The development-signed candidate launched with a disposable populated profile and no real keys.
Settings displayed version0.2.0-beta.0/build3.0.1 and Not checked; an explicit update check returned
the honest unavailable-feed error. Native Save backup created a mode0600 archive and the app
adopted its new authenticated backend session. Essay typing reached Saved.

An unparented restore confirmation did not appear on macOS27. Attaching it to the main window
fixed the actual native flow: the picker and confirmation appeared, restoration completed, and
the app reopened. The restored essay matched the archive while newer edits remained in the
retained prior profile. No real student profile or Keychain was modified by these tests.

Native Print/PDF is **not certified**: the menu automation did not reliably activate Print.
The native command and save-before-print regressions exist, but a physical menu/print-preview
check remains required. This evidence does not establish clean-machine notarization, installed
N-to-N+1, physical accessibility,8GB memory or sustained-soak acceptance.
