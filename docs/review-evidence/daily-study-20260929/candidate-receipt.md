# September 29 daily-study review candidate

**Review only.** This candidate was assembled in an isolated worktree from main
`c52ac698c62f10cfaf70face1aba3305bb8240ae` and the four implementation PR heads:

| PR | Issues | Exact head |
| --- | --- | --- |
| [#101](https://github.com/oliverdougherC/Lyra/pull/101) | PLA-572 | `b05c36966ddfc294c516d258547d1193a196bcb7` |
| [#102](https://github.com/oliverdougherC/Lyra/pull/102) | PLA-573, PLA-575 | `b7ce04277f19a72b96d1a3382018d5c851c96a16` |
| [#103](https://github.com/oliverdougherC/Lyra/pull/103) | PLA-576, PLA-577 | `372be28766f8c0a22a2cbd6e88df7dad9aee1249` |
| [#104](https://github.com/oliverdougherC/Lyra/pull/104) | PLA-574, PLA-461 | `bee2accf981632626abf8ca6531175449a8387c1` |

The application-source commit is `2591e06015f375014c40e24e8f7793fc23a73116`
(Git tree `83284de0c81f8f463bdf99439603dc5da1189ab6`). The evidence-only commit
containing this report follows that source commit. None of these PRs was merged.
Draft PR #100 native scrolling was kept separate; its head was
`e855a8277a70ad078e5c886c693fe671c7678a92` at the final inventory check.

## Signed artifact

The retained bundle is
`/Users/ofhd/Developer/Lyra-pla571-review-candidate/src-tauri/target/release/bundle/macos/Lyra.app`.
Its embedded `lyra-release.json` declares version `0.2.0-beta.1`, build `3.0.2`,
source `2591e06015f375014c40e24e8f7793fc23a73116`, bundle identifier
`com.lyra.desktop`, arm64, and schema range 0–50.

The established Apple Development identity used by both the retained candidate and the
currently installed app is `Apple Development: Oliver Dougherty (X2622B554D)`, team
`36R7UFULAK`. The signing helper signed and verified 78 code objects, including the
frozen backend. App identifier `com.lyra.desktop` has CDHash
`92ae078ce5cb42857fda8613b83d18349e59bcef`; backend identifier
`com.lyra.desktop.backend` has CDHash `e82a8887aaa297c77402ff5fe41d14e7539b8729`.
Both have certificate-backed designated requirements without `cdhash`. The bundle
verifier passed arm64 and macOS 14.0 floor checks on 77 native files.

SHA-256 of `Contents/MacOS/lyra-desktop`:
`681e866b0b69e2ef71754a6e4104b3ab7c329b2d2921ce76e690ecacdd4f80b2`.
SHA-256 of the bundled `lyra-backend` executable:
`244984401c031e3b28c6af9d725fca95df6ea8451d8742eeb4d3229686dea696`.
The signed frozen-backend smoke returned authenticated status, ephemeral loopback, and
two successful CAS computations, including the false-equality check.

## Verification and open gates

- Combined backend before the final prompt-budget correction: 3,938 passed, one skipped.
  After that correction, 284 affected backend tests passed; the source-change PR's full
  exact-head CI is the remaining full-backend rerun.
- Combined frontend: 1,447 unit tests passed. After the final backend-only correction,
  111 focused chat/activity/document tests, typecheck, lint, formatting and build passed.
- Before the final name-search correction, combined real-backend Chromium acceptance on
  private ports: **139 passed**, with one
  expected backend failure consumed and zero unexpected failures. The stack shut down its
  own services and removed its disposable data. Combined browser geometry passed both
  wide and narrow cases after the chat change; the activity branch's measured rectangles
  and synthetic frames are in [activity geometry](activity-geometry.md).
- After the final name-search correction for readable pages whose indexing failed, 139
  affected document-access, retrieval and agent-route backend tests passed. Seven
  upload-to-chat and regeneration browser acceptance cases passed on the exact final
  source, with zero backend failures recorded. The signed bundle and frozen-backend
  smoke were rebuilt from this exact source revision.
- Documentation and active-reference scans passed. All four PRs passed all 11 required
  exact-head CI jobs, including full backend/frontend, full-stack acceptance, Rust,
  frozen Python smoke, macOS artifact and aggregate gate.
- The final live production-route tutor evaluation still **fails** concise definition,
  first-step scope and mathematical-correctness checks. Representative failure: the
  exact vector-space question received about 615 words; a formal definition misstated
  the dimension of `C` over `R`; attempt feedback printed an invalid integral bound.
  The explicit full convolution solution was sound in the same run. Full redacted
  terminal/tool evidence is in [adaptive education evidence](../../adaptive-education-evidence-20260929/README.md).

Finder-to-signed-app drops, native WebKit chat geometry, manual-scroll and narrow-window
composer reachability, and a live-provider grounded Textbook citation remain unexecuted.
No isolated production-identity account/device was available. The normal
`/Applications/Lyra.app` was running and was neither quit, replaced, selected nor
relaunched; private installer validation was also not attempted because the guarded
installer refuses a running Lyra process. No release was published. Independent PR
review and correction are required before integration or local delivery.
