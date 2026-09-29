# Daily-study correction review receipt — September 29, 2026

## Current review pass: R8 and final-code native diagnostic

This section supersedes the historical head and native status below. The owning
R8 commit is `ae6c063` on #96, merged through #97 into #98. The combined code
used for the isolated diagnostic is `7d9e447fdb49a58a1675e056d7980cd3365ed5b6`;
the later receipt-only commit does not change its frontend or frozen backend bytes.
All three PRs remain unmerged for independent review.

| Check | Reviewed behavior | Corrected behavior |
| --- | --- | --- |
| Fresh-session accepted sends | After 128 accepted questions in a single scope, 128 nonempty records and 128 markers remained; the next write returned `durable:false`, and a new session lost that prompt. | A repository test first failed on the reviewed source. After correction, 240 restore → accept unchanged → new-question cycles leave one unsent record and no obsolete markers. The last question restores in another fresh session. |
| Existing full store | Acknowledged nonempty copies consumed all 128 unsent slots. | A seeded store with 64 acknowledged copies in different scopes and 64 genuine unsent records reclaims only the acknowledged copies. The new draft is durable and survives a fresh session; 65 records and zero obsolete markers remain. A full store of 128 genuine unsent records still refuses another durable write. |
| Refusal and concurrent writer | Removal/write/property refusal or an active writer changing a revision could lose data if cleanup were unconditional. | Durable suppression precedes matching-copy removal. Denied deletion keeps the marker and refuses a new durable prompt at capacity until storage recovers; denied marker write reports failure and leaves the old copy. A changed foreign revision remains intact. Storage-property denial retains the in-memory fallback. |

The source-page route also needed one narrow correction found while exercising the
required native paging workload: Files consumes its one-shot navigation anchor
after focusing the document, which previously closed the page preview. A
failing-before route test now keeps a separate preview document ID until the
dialog closes. The signed isolated app visibly opened pages 5 and 8 of a
12-page synthetic PDF. This fix is on #98 and does not alter #96/#97 ownership.

Combined local verification after R8: 1,419 frontend unit tests and 81 Chromium
browser checks passed (three WebKit-specific skips); affected router/store,
frontend typecheck/lint/format, docs checks, and frozen-backend smoke passed.
The source-page follow-up passed its focused tests and native diagnostic; final
combined-head CI is tracked on #98. The retained production-identity review
bundle is signed and smoke checked separately from native diagnostic acceptance.

[Final-code native method, timings, process samples, and limits](native-final-r8.md)
cover actual chat and source journeys. The same WKWebView's minimal control
returned 181 callbacks in each three-second run, with 17 ms median and 18 ms
p95 callback intervals in all three runs. The configured internal display is
120 Hz, yet this callback path did not reach an 8.33 ms cadence. The long-chat
stream showed 17/30 ms median/p95 active callback intervals and 9/20 ms
keydown-to-next-callback timing. These are **JavaScript callbacks**, not
presented frames, physical input-to-paint, or proof of a universal WebKit cap.
No production identity was launched or replaced; `/Applications/Lyra.app`
still embeds #94 source `a8a9c819e43a3305270578021b5eafd5491486f6`.
PLA-570 remains open.

## Historical R5–R7 review receipt

## Third-review correction (R5–R7)

The #96 owning fix is `4577858abdd94a19484be8eda4081df8b013ab03`.
It reaches this combined candidate through an ancestry-preserving #97 merge.
The tests below exercise repository source, including the production class route;
the earlier independent storage fixture was a separate reproduction.

| Finding | Failing before | Passing after |
| --- | --- | --- |
| R5, PLA-564 | A fresh `sessionStorage` lifetime restored a question accepted in the prior lifetime; adopted edits left predecessor text and 129 repeated lifetimes exhausted the record cap. | Unchanged and edited adopted sends retire their exact predecessor; 240 fresh-session adoption/edit cycles leave one record. A second active writer's different unsent revision remains recoverable. Refused writes/removals report failure and retain in-memory recovery. |
| R6, PLA-564 | The production route's returned, still-mounted textarea retained the submitted question after completion. | That textarea clears on settlement without a second navigation; a newer question typed there remains. Tutor and writer stream routes and the nonstream agent route assert visible text and one request. Existing failure, Stop, and new-session handoff regressions remain green. |
| R7, PLA-564 | A protected newer revision shared `false` with failed retirement, causing a saved-copy warning on a healthy send. | Typed `retired`, `superseded`, and `failed` outcomes keep newer text quiet; actual denial still warns. Storage-property access remains inside the memory-fallback boundary. |

The focused owning suite passed 73 tests after correction; the full #96 frontend
suite passed 1,298 tests before the final storage-denial edge cases were added.
Those edge cases and all affected tests pass after the final change. Frontend
lint, typecheck, formatting, documentation checks, and 22 Chromium navigation
and streaming checks passed on #96. Exact combined-head checks are tracked in
the PR after this propagation.

This packet accompanies PR #98 and its owning branches #95–#97. It uses synthetic
data only. The screenshots below were captured at the previously reviewed combined
head `90b2938046bf0ba51223b5acc865efe504dbdc4f`; they illustrate preserved
draft and activity presentation, not native performance or proof of the new repairs.

## Correction coverage

| Review item | Owning PR | Failing-before regression and passing behavior |
| --- | --- | --- |
| R1, PLA-564 | #96 | Full module reload, unchanged restored send, and another reload; Class Ask, tutor, and writer composers; new-session handoff; route departure through the production `AppRoutes`; failed send, newer follow-up, and refused durable retirement. Tests inspect visible text and stored revisions. |
| R2, PLA-564 | #96 | Hundreds of same-window reloads and source-choice writes, legacy selection migration, distinct active windows, acknowledged shared revisions, full-cap refusal, and quota denial. An active window's different unsent revision remains recoverable; a full store reports that the new prompt is not durable. |
| R3, PLA-568 | #97 | Strict HTTP client/loop tests reject the old image-before-tool-reply order. The corrected request puts all tool IDs first for image plus text and two-image batches; refusals, Stop, and overflow cannot send a partial batch. A route test checks actual rendered PDF bytes and physical-page provenance. |
| R4, PLA-565 | #95 | Other-class ingestion that reorders BM25 hits, same-class refresh/deletion, a revision/query race, provider continuation, and transactional migration generation. Continuation now returns all stable results or an explicit expired cursor. A CI follow-up also proves a fresh migrated destination remains importable. |

The owning branches passed 1,285 frontend tests for #96, 223 relevant backend
tests for #97, and 141 document tests plus 108 import/migration/reader tests for
#95's follow-up. After propagation into #98, the affected combined suites passed
118 frontend and 261 backend tests, plus frontend typecheck and formatting. Full
required CI, signed-bundle source verification, and frozen-backend smoke are
reported against the exact final #98 head in the PR.

## Preserved presentation

| Synthetic journey | Before | Reviewed presentation |
| --- | --- | --- |
| Writer chat unsent follow-up | [chat before](before-chat.png) | [chat after](after-chat.png) |
| Class Ask unsent prompt | [Ask before](before-class-ask.png) | [Ask after](after-class-ask.png) |
| Physical-page activity | [activity before](before-activity.png) | [activity after](after-activity.png) |

## Native evidence boundary

The normal installation's embedded source receipt is
`a8a9c819e43a3305270578021b5eafd5491486f6` (#94). It was read without
launching or replacing the installed app. The machine reported macOS 27.0
(26A428), system WebKit 22625, AC power, and an internal display configured at
120 Hz.

An isolated, nonpersistent WKWebView with a minimal moving square ran three
three-second `requestAnimationFrame` samples. Each sample had a 15 ms median
callback interval; the three 95th percentiles were 17 ms. This is only a
same-runtime JavaScript callback probe. It did not measure presented frames,
Lyra navigation, typing, input-to-paint latency, idle energy, or the signed
candidate.

A separately identified, signed diagnostic app then exercised the **pre-correction**
`67c5fdb` frontend in a real native writing editor with a synthetic 40-paragraph
draft. Across 169 active callback intervals, median/p95 were 17/30 ms; 171
input-event-to-next-callback samples were 10/23 ms. A settled visible snapshot
of five owned processes was 389.7 MB RSS and 1.1% sampled CPU. The diagnostic
used a different compiled app identity, nonpersistent WebKit store, disposable
backend paths, and null Keyring; it did not launch the normal installation.
[Method, isolation, and limits](native-diagnostic.md) are retained separately.
These are JavaScript callbacks with instrumentation overhead, not presented frames
or physical input-to-paint, and the final corrected head was not measured. No
candidate-equivalent isolated production account/device was available, so PLA-570
remains In Progress. Neither probe establishes 120 Hz Lyra acceptance.
