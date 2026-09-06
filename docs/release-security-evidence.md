# Release credential, process, and log evidence

Review baseline: current fetched `main` in the isolated release worktree. Live Linear
PLA-456, 457, 458, 459, 460, 462, and 463 were read in full on 2026-09-05; each had
no comments. The source findings remained relevant. PLA-458/460 implementation was
handed to the acceptance lane; its manifest disclosure is exposed as the read-only
`local_model_setup` settings field.

| Issue | Repair / owner | Regression evidence | Remaining candidate evidence |
| --- | --- | --- | --- |
| PLA-456 | Credential lane: durable fallback/deletion authority markers survive initial Keychain outage and restart. | Four tutor/Exa replace/delete outage cases failed before, pass after; historical PLA-302 rollback-of-rollback tests preserved. | Real locked/denied macOS Keychain save/recovery/forget cycle. |
| PLA-457 | Credential lane: keep leader unreaped until group cleanup; observe exit with macOS kqueue or POSIX waitid. Absolute execution and drain bounds. | Three failures reproduced before (sleeping descendant, TERM-ignoring descendant, early pipe closure). All command tests pass on this macOS host, including continuous output and actual descendant disappearance. | Packaged confirmed-command flow; sleep/wake. |
| PLA-459 | Credential lane: safe exception formatter emits exception categories and stack locations, omitting exception values/source excerpts and ignoring cached unsafe traceback text. Private rotated files. | Chained exception/private path/token sentinel and cached-exception test failed before; rotated bytes pass after, all files 0600. | Real packaged diagnostics and any future support exporter must use these serialized logs. No separate export surface is certified here. |
| PLA-462 | Credential lane: one bounded Keychain worker, including get/set/delete/rollback. Pending calls cannot start additional operations; async callers do not wait on Keychain or credential locks. | Six blocked post-probe get/set/delete cases across tutor/Exa pass; repeated saves do not add workers, late completion cannot supersede acknowledged fallback. Immutable-slot cold read yields a retryable error without blocking the event loop. | Actual platform denied/locked prompt behavior. |
| PLA-463 | Credential lane: migration 045 selects an immutable credential UUID in the same SQLite commit as endpoint/model. Legacy key bound to migration-time endpoint; endpoint-only changes do not inherit keys. | Three current-HEAD implementation reproductions failed (endpoint-only inheritance, failed DB commit, retained old row). Passing repairs include two barrier-coordinated saves and mocked exact URL/Bearer-header assertions. | Packaged relaunch and update/backup integration with populated credentials. |

Verification command (the existing developer environment was used without changing the
original checkout):

```sh
/Users/ofhd/Developer/Lyra/.venv/bin/pytest \
  backend/tests/test_api_settings.py \
  backend/tests/test_credential_transitions.py \
  backend/tests/test_commands.py \
  backend/tests/test_packaged_log_privacy.py \
  backend/tests/test_private_permissions.py \
  backend/tests/test_desktop_bootstrap.py -q
```

Result: **160 passed**, with existing FastAPI/Swig deprecation warnings. Ruff passes the
owned source/test files. The release owner is responsible for integrated checks, signing,
frozen-backend rebuilding, installed candidate verification, and the final PR/SHA mapping.
These source tests do not certify a distribution signature or a locked-Keychain cycle.

## Credential authority and recovery boundary

Credentials remain write-only in HTTP responses. New tutor saves use service `lyra`,
username `tutor:<UUID>`. Owner-only `data_dir/credentials/<UUID>.json` records store the
bound endpoint and Keychain locator; only fallback records contain the key. Metadata is
atomically published and fsynced before SQLite can select it. A staged slot from a failed
settings commit is never authoritative. Slots are retained so an already captured old
settings row cannot pair its endpoint with a newer key.

Forget publishes a durable generation revocation, removes historical fallback values,
clears the legacy fallback, and attempts bounded removal of the Keychain entries. A locked
OS store may retain an encrypted obsolete entry until cleanup can run; the revoked slot
cannot resolve through any retained settings row. Repeated Forget retries those entries.
A later save receives a fresh identity in the new generation. Backups must preserve the
credential records and generation marker, with the same privacy as the database; a restore
that excludes credentials must explicitly require new authentication.

Migration 045 adds `tutor_credential_id` and `legacy_credential_endpoint`, and advances
`probe_revision` whenever the selected credential changes. The updater lane derives the
supported schema from migration filenames and creates the pre-migration backup. Existing
settings probe invalidation and remote-document consent remain in place.

## Deliberate simplifications

No client-level header rewrite or second endpoint authority was introduced. Existing
`tutor_config`/`tutor_access` snapshot consumers receive their credential through the same
resolver. Process cleanup owns only the new process group created for the confirmed argv;
it is not an OS sandbox claim. Log redaction retains useful stack locations rather than
attempting to recognize arbitrary private provider text inside exception values.
