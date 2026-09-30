# September 29 polish correction: current-main review candidate

**Review only. No PR was approved or merged, and the normal installed app was not changed.**
The application-source commit is ff9848fa1bb8012cd7c41a92f1f03a6c60d5835c, assembled
from main 299d0da7debb22244c484cd7fd779c423866f9c3, which contains merged
native-chat PR #100. This receipt follows that application-source commit. The old candidate
2591e06015f375014c40e24e8f7793fc23a73116 excluded #100 and is not used here.

| Draft PR | Owner | Exact reviewed head |
| --- | --- | --- |
| [#101](https://github.com/oliverdougherC/Lyra/pull/101) | PLA-572 upload | c807d4cd8f73ebb2a7f8f7bf7a668bf752eb6b61 |
| [#102](https://github.com/oliverdougherC/Lyra/pull/102) | PLA-573/575 activity and layout | 04b5a7603b7924705881c1dce071d0001b117620 |
| [#103](https://github.com/oliverdougherC/Lyra/pull/103) | PLA-576/577 names and sorting | bdef03440df9ed4d3cd14d8b3d80c7a3d1bc96aa |
| [#104](https://github.com/oliverdougherC/Lyra/pull/104) | PLA-574/461 education chat | 1a84038a565ed05a4993cc445a40c52efcb21b31 (teaching route source d3eae5b3a1fa5dde3f733a9b4a0f67dbe6691a0d) |

## Checks on the combined application source

- Frontend unit suite on the final combined lockfile: 1,479 passed. TypeScript,
  ESLint, production Vite build, pinned pnpm frozen install and production audit
  passed; the audit found no known vulnerabilities. The only dependency change
  pins the existing transitive fast-uri from 3.1.7 to patched 3.1.8.
- Backend full suite after the page-budget correction: 3,942 passed, one skipped.
  The later lockfile change did not touch Python. Ruff lint and formatting passed.
- Real-backend Chromium acceptance on the final lockfile: 139 passed, zero
  unconsumed backend failures.
  Before the budget correction, the selected later-page and matrix-layout cases
  failed and one agent-chat 503 reached the failure gate. After the correction, the
  focused five upload-to-chat cases and the full 139-case suite passed.
- Current native-chat browser geometry and menu checks: five passed, including draft
  creation, tool activity, streaming, manual scroll-away, completion, delayed
  history, 800x600 and 900x650 at 125 percent text scaling. The measured 12-action
  menu at 800x600 did not overlap the conversation in that layout; its menu role
  participates in the native occlusion observer for overlapping layouts.
- Documentation links, active-reference scan, release metadata check and diff
  whitespace check passed. All four exact-head CI workflows passed all 11 required
  jobs: [#101](https://github.com/oliverdougherC/Lyra/actions/runs/36656723916),
  [#102](https://github.com/oliverdougherC/Lyra/actions/runs/36659021502),
  [#103](https://github.com/oliverdougherC/Lyra/actions/runs/36657987293) and
  [#104](https://github.com/oliverdougherC/Lyra/actions/runs/36665267760).

## Signed candidate

Retained app: /Users/ofhd/Developer/Lyra-pla571-correction-candidate/src-tauri/target/release/bundle/macos/Lyra.app

The embedded release contract names source ff9848fa1bb8012cd7c41a92f1f03a6c60d5835c,
version 0.2.0-beta.1, build 3.0.2, bundle identifier com.lyra.desktop, arm64 and
schema range 0-50. The bundle verifier reports a minimum macOS version of 14.0.
The same Apple Development identity as the
installed app signed and verified 78 code objects: Apple Development: Oliver
Dougherty (X2622B554D), team 36R7UFULAK. The bundle verifier passed 77 native
objects. The completed signed bundle's frozen backend smoke passed authenticated
ephemeral loopback and two CAS computations.

- App CDHash: 3fe5905c06b6e2f32217641757ca17284293f05d
- Backend CDHash: ff0736a0401a900dd6ff14e6e36ae196e212dae1
- App executable SHA-256: 154b389584772e0eb134bc84838434c2a36de0aa40e8d01a6166302d9c5a2fd2
- Frozen backend executable SHA-256: 3fabdcac3fabe76f5dd107e922ed7c5c47296f3a325361e1fdcaa0fe0bc4216c

## Provider and native boundaries

The combined-source [synthetic nicknamed-source run](nickname-grounding-correction.json)
used a disposable class and selected document whose original filename was
signals-handout.txt and nickname was Textbook. The production class-chat planner
received a bounded synthetic page-3 excerpt, and the configured
Qwen3.8-Flash-Next route returned the correct negative Fourier exponent and named
Textbook, physical page 3, in one completed round. This used an injected synthetic
retrieved excerpt, not real upload ingestion or native UI. Its endpoint URL is retained
only as a hash; no key, coursework or user document entered the saved run.
That observation was recorded at combined commit dd783f98cbd0c23c5f52c3b1f0f76681bfbbeb7e;
the later application-source delta is only the frontend fast-uri pin and evidence.
The production planner, backend and prompt used by this case are unchanged.

The [final repeated education answers](../../adaptive-education-evidence-20260929/README.md)
still fail the PLA-461 quality gate. Conversational definitions expanded to 125 and
136 words with unrequested axioms; one scalar follow-up briefly printed the wrong
codomain before correcting it; first-step and attempt replies varied in scope.
The requested full convolution core was correct in both final-route observations,
but auxiliary generalizations were overbroad. Keep PLA-461 and PLA-574 In Progress.

Finder files/folders into the signed app and native AppKit/WebKit geometry, focus,
large-text and source selection were not executed. This machine has only the normal
account and an incumbent /Applications/Lyra.app process sharing the compiled-ID
single-instance endpoint. Backend test selectors cannot isolate WebKit or IPC.
The guarded private installer rehearsal also was not run because the normal app is
running and the installer refuses a running Lyra. No normal study profile was
selected, reopened, replaced or used for destructive testing.

Proposed review/merge order after independent acceptance: #101, #102, #103, then #104
only after its separate semantic gate is resolved. Nickname prompt quoting lives in
#103, so holding #104 does not drop that source-safety fix. Generated release PR #105
is outside this correction pass.
