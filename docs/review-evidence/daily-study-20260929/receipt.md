# Daily-study correction review receipt — September 29, 2026

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
