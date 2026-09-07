# Serial integration preparation — September 7, 2026

> Historical rehearsal record. Its no-merge authorization boundary and test totals describe
> the preparation run only. See the [current integration ledger](release-evidence.md).

**No repository merge or public promotion is authorized.** The reviewed implementation
recommendations are not Oliver's permission to merge. Main remains
`e96bf4886977c648f9e7905c7807c806b1ae7a80`. This work applies reviewed deltas to a disposable,
single-parent rehearsal branch; it does not change main or the reviewed PR heads.

## Inputs and checkpoints

Read the four anchored GitHub reviews and Linear project update
`c9a5afd5-329f-49d3-8782-ac5464ac9f83` before assembly.

| Order | Input | Rehearsal checkpoint | Boundary |
| --- | --- | --- | --- |
| 1 | #81 `fe8ef2c946c81f60e8b6a8fd28749d8f295dafcd` | `a57ade4` | Tree exactly equals reviewed runtime tree. |
| 2 | #83 `0b7aca82f1922c664af15a07888d44d90be0c0a1` | `f890dfc` | Retains real-process SQLite repair; duplicate cache patch occurs once. |
| 3 | #80 `2ee865c0da52c94c1f5aa056ea64a2e2de513e4e` | `2b98e4122a8abc2047321a5b8d1e3122755c647b` | Both owners' prompt changes preserved. |
| 4 | #82 fixed `83d6e0a0a8307db4685254fb01c1edd9359319e4` | `0e63daf` | Rejected `6e6efd55` is not included unchanged; quota fix independently reviewed. |
| Integration repair | `a9b80ed` | `0d6478f43c9c02b2c5ca59b354b8aa1676c7fe7f` including regression | Refresh cached quiz continuation after mutations. |

The four-way source/test tree at `0d6478f` is
`a3e5b8841bea8c8cdc20700b04ae9ceb64e6559a`. Subsequent documentation-only commits do not
change its production source. Generated build receipts must name the actual source commit and
binary hashes; a local rehearsal is not a post-merge candidate or hosted CI result.

### Explicit reconciliation

- `backend/rag/render.py` and its test file are byte-identical in #81 and #83. They remain once.
- All four changed writer prompt nodes and five learning prompt nodes match their anchored ASTs.
  Writer section-edit/citation-ledger/schema changes coexist with Guide, verification, topics,
  cards and quiz changes. Three writer chat-test changes and two learning budget-test changes
  likewise match both anchors.
- Independent assembly review checked 68 changed production/script files. Unique files match
  their owner, shared render matches both, and the runtime window configuration is retained.
- The only textual conflict was a historical native-evidence paragraph present in #81 but absent
  from #83's partial copy. That specific paragraph was retained; no blanket ours/theirs strategy
  was used. Architecture and historical evidence additions from both owners remain.
- #80 supplies `active_attempt_id` and `answered_count` from real non-abandoned unfinished attempts,
  valid ordered question snapshots and committed answer rows. #82's Continue quiz uses that
  metadata and resumes the existing attempt; generated-question counts are never substituted.

### Newly demonstrated combined defect

Real API/UI acceptance at `0e63daf` finished an attempt successfully. The API returned no active
attempt and zero active answered count, yet SPA navigation back to Practice retained Continue quiz
and the stale count. Existing mutation hooks assumed quiz-list data did not change during attempts.

The separately reviewed `a9b80ed` fix invalidates the existing `['study']` query prefix after
successful start, answer and finish; it preserves attempt admission/state. The integration-only
regression `0d6478f` verifies one answered → same attempt/question 2 → reload/return → two answered
→ question 3 → finish clearing. A second case verifies restart abandons the old identity and resets
active progress. Both actual-stack cases and 42 focused frontend tests pass. The test does not mock
the quiz API or write its database directly.

This repair is **not in the quota-only #82 head**. Include it as explicit reviewed refresh work
once #80/#82 coexist. The full-stack regression depends on #80 metadata, so do not bolt it onto a
standalone pre-#80 branch and then weaken it to pass. Root owns this additional integration patch.

## Verification and remaining gates

The seven real-process POSIX SQLite lock regressions passed **before** consequential combined
acceptance. The retained storage implementation and regression bytes match #83 exactly.

| Gate | Status | Evidence / limitation |
| --- | --- | --- |
| Anchored current-base CI | Pass | #81/#83/#80/#76 each had 11 green checks against e96bf488; #82 quota repair has its own exact-head green run. These are separate results. |
| Three-PR combined checks | Pass | 3,446 backend + 1 skipped; 1,014 frontend; 132 real-stack; 52 Rust; lint/typecheck/clippy/docs. |
| Four-way backend | Pass | 3,446 passed / 1 skipped; same backend tree after frontend-only integration fix. |
| Four-way frontend/browser | Pass | 1,073 unit tests; 93 Chromium/WebKit browser cases, with 3 WebKit-only cases skipped on Chromium. |
| Cross-boundary quiz continuation | Pass | Real API/UI failing-before; repaired two-case run and 42 focused unit tests passed. |
| Four-way full-stack | Pass, local: 134 cases | Exact local and hosted receipts are retained separately. |
| Controlled HTTP writing | Pass, recovery only | One control and two observed interventions; current edits retained, no duplicate comments or excerpt revision violations. Not semantic-quality certification. |
| #76 installer against combined tree | Pass, separate overlay | Original five-file delta applies cleanly; 24 installer/isolation tests pass. It is excluded from the integration branch. |
| PLA-475 advance-main behavior | Blocked | Requires an authorized first merge, observing remaining PRs behind, then refresh and new checks. Strict CI Gate/no bypass remains unchanged. |
| Post-merge immutable candidate | Blocked | Requires explicit Oliver authorization, serial integration and new current-main checks. |
| Real Keychain outage/denial/Forget | Blocked | Disposable OS account or genuinely isolated approved setup absent. Null/fail Keyring is not OS Keychain acceptance. |
| Installed N→N+1/rollback | Blocked | Approved integrated artifact pair/feed and safe native acceptance still required. Signature/schema regressions do not substitute. |
| Production-identity native startup / Print / graceful UI quit | Blocked on this account | Backend selectors do not isolate WebKit or single-instance IPC. No combined native shell was launched. |
| Physical input/accessibility | Not run on combined candidate | Earlier receipts remain dated and separate. |
| Sustained study/writing, sleep/wake | Not run on combined candidate | Requires final immutable build and real quality scenarios. Short recovery tests are not a soak. |
| Physical 8 GB | Waived by owner / not measured | No target hardware; no 8 GB performance claim. |
| Distribution licensing | Owner-confirmed decision | Not independent licensing verification; notices and PyMuPDF obligations retained. |
| Persistent updater authentication | Prior verified protection retained | Hardened ad-hoc policy, minimal entitlements, mounted-DMG identity and persistent updater key remain mandatory. |
| Updater off-device backup | Blocked | Confirmation remains missing. Do not rotate or substitute ephemeral keys. |
| Learning/writer semantics | Open / prior failures retained | Follow-up branches must supply actual case/context/configuration and held-out evidence. Completion counts are not quality passes. |

## Native-host safety

No ordinary CUA app selection or LaunchServices relaunch is used for this rehearsal. An alive-PID
check before a launching selector still has a race. **Do not launch the production-identity native
shell on this account, even through direct Popen with backend overrides.** Source inspection of the
installed SDK found two additional boundaries:

- `create_main_window` configures neither incognito nor a separate data-store identifier. Installed
  Wry 0.55.1 uses `WKWebsiteDataStore::defaultDataStore` in `src/wkwebview/mod.rs`; backend cache/data
  selectors do not isolate WebKit/localStorage.
- `tauri-plugin-single-instance` 2.4.3 derives `/tmp/com_lyra_desktop_si.sock` only from the compiled
  app identifier. Neither UID, HOME/TMPDIR nor backend profile overrides separate this IPC name.
  A second process can notify/focus an incumbent and exit. A disposable OS account must also have
  no incumbent sharing this pathname, or use a fully isolated device/login environment.

Root discovered this before launching the combined shell. Frozen-backend tests remain isolated
through their explicit profile/credential selectors and authenticated inherited-socket boundary.
For future candidate-equivalent native acceptance, use a genuinely isolated environment with no
incumbent IPC owner, retain a held explicit child process and prove every descendant's selectors.
A process exit fails the check; it must not select/reopen another app. A unique compiled-ID and
separate-store variant can test isolation mechanics, but is not the production candidate's bytes.
External SIGTERM observations are process-termination evidence, not Cmd-Q or rendered acceptance.

The earlier writer normal-profile auto-launch incident remains excluded from isolated evidence.
Startup effects were not ruled out; do not relabel it impact-free. Any test-only launcher/unique-ID
native variant must prove every entry/relaunch path and remains a variant, not production-byte
certification.

#76's `build_local_app.sh` and `--open` are normal-profile delivery tools. Keep the existing
installer separate; use it without `--open` for a selected private review destination only after
ownership checks. Installation does not prove native-profile isolation. Do not replace the canonical app or share writable profiles during rehearsal.

## Authorization handoff

1. Oliver explicitly authorizes repository merges. Recheck main, each expected head and required
   checks immediately before each operation; stop for unexpected work instead of overwriting it.
2. Integrate #81 first. Record the resulting main SHA and green main CI, and observe stale-base
   refusal for remaining PRs before refreshing them.
3. Refresh #83 against that main, retain the storage regression, compare the reviewed delta and
   rerun storage/recovery checks and CI. Repeat for #80, explicitly preserving prompt sections.
4. Refresh the reviewed/fixed #82 after #80; include the separately reviewed quiz cache repair
   and real continuation regression. Revalidate navigation/quota, quiz, recovery and current-base CI.
5. Review #76 independently against the actual combined main. Build one immutable post-integration
   candidate and collect new SHA/version/build/hash-bound tests. Rehearsal results are useful
   preparation, not automatic approval of that new artifact.
6. Decide public promotion separately after semantic, human, platform and remaining release gates.
   No Apple Developer account prerequisite is reintroduced under the chosen hardened ad-hoc policy.
