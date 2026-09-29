# Daily-study correction review receipt — September 29, 2026

## R9 competing-write correction — current review handoff

The owning #96 head is `2767ef8daedc0f4e775ec0b21ccca096073dce65`.
It reaches #97 at `7b95edb7281aa724531e9140b1b56c9ba517f8a9` and
combined #98 code at `17d038e3cb4179ef51e748648bf2fd493daee028`, with
the review-stack ancestry intact. The retained signed production-identity
review bundle embeds the #98 code SHA; the receipt commit that follows changes
documentation only. All three PRs remain unmerged for independent review.

The [storage contract](../../chat-draft-persistence.md) uses a distinct key
for each draft revision. Cleanup names an immutable revision key, so a
competing newer write cannot occupy the key being removed. Each saved revision
also records its surviving predecessor keys before it reaches storage. If
cleanup is refused or interrupted, later acceptance acknowledges those exact
predecessors before the submitted revision; a different active writer's branch
is not in that ancestry. Acknowledgement markers are per revision, so two
windows cannot overwrite one another's acknowledgement. Legacy mutable writer
slots are read and suppressed but never removed by new code.

| Check | Evidence on this correction |
| --- | --- |
| Original ordering | The new two-module regression failed on the reviewed mutable-key implementation: writer A completed a durable newer write in the removal hook after B's final comparison, then B deleted that key; A and a relaunched reader lost the text. The same exact hook passes with revision keys and restores A's text. |
| Related interleavings | Adoption cleanup with an active competing edit; two acknowledgements in one scope; an interrupted/denied predecessor cleanup followed by another edit and accepted send; a full-store cleanup recovery; and two windows racing for the last of 128 unsent slots pass. The two-page Chromium test preserves distinct visible window drafts through reload without a false saved-copy warning. |
| Bounds and refusal | 240 fresh-session accepted-send cycles leave one unsent revision and no obsolete v2 markers. Mixed 64 retired/64 genuinely unsent legacy records admit the next prompt without deleting a legacy mutable slot; 128 genuine unsent records refuse another durable write. v2 records are admitted to 128 live slots; a refused deletion can temporarily leave an extra physical copy, and further writes refuse the occupied cap until cleanup recovers. Pre-existing v1 slots form a fixed migration population. Denied marker writes remain visible as failed settlement rather than a false durable claim. |
| Combined checks | 1,426 frontend unit tests, lint, typecheck, formatting, documentation links, and active-reference scan passed. The Chromium run had 81 passes, three platform skips, and one source-picker stability timeout; that exact case passed on rerun. The retained review app was rebuilt with the frozen Python sidecar, signed with the established Apple Development identity (78 code objects), bundle-verified, and passed authenticated frozen-backend smoke. A copied app passed the private installer rehearsal without opening or replacing the normal app. Exact-head GitHub CI is checked separately before handoff. |

The exact-head [#96 CI](https://github.com/oliverdougherC/Lyra/actions/runs/36606787394)
and [#97 CI](https://github.com/oliverdougherC/Lyra/actions/runs/36606850822)
each passed all 11 jobs. The [#98 code-head CI](https://github.com/oliverdougherC/Lyra/actions/runs/36610148493)
is the combined gate; its final disposition is checked before handoff.

A temporary synthetic store benchmark at 127 existing unsent records measured
100 replacement writes at 0.39 ms median, 0.62 ms p95, and 1.22 ms maximum
in the jsdom test runtime; it is a write-cost check, not a native frame result.
The separate signed R9 diagnostic used code `17d038e`, a unique compiled ID
and persistent WebKit store, disposable backend profile, null Keyring, and a
keyless loopback tutor. Its frontend asset bytes and frozen backend executable
matched the retained production-identity review build; only diagnostic native
identity, profile, WebKit store, and event-timing overlay differed. In its Class
Ask composer, 71 synthetic keydowns reached
the next callback in 12/22 ms median/p95; 48 later chat follow-up keydowns were
9/37 ms. One synthetic send completed with an empty, warning-free composer;
a real Quit/relaunch kept it retired, and another real Quit/relaunch restored
the later unsent follow-up. The fixture logged one completion request. These
are isolated-variant observations with automation and overlay overhead, not
presented frames, physical input-to-paint, or production-default WebKit/Keychain
acceptance. The R8 [chat/source/idle report](native-final-r8.md) remains the
broader native evidence, and PLA-570's 120 Hz requirement remains open.
`/Applications/Lyra.app` was neither launched nor replaced.

## Historical R8 review pass and final-code native diagnostic

The owning
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

Final combined local verification: 1,420 frontend unit tests and 81 Chromium
browser checks passed (three WebKit-specific skips); affected router/store,
frontend typecheck/lint/format, docs checks, and frozen-backend smoke passed.
The source-page follow-up also passed its native diagnostic. Exact final-head
CI is tracked on #98. The retained production-identity review bundle at
`src-tauri/target/release/bundle/macos/Lyra.app` is signed and smoke checked
separately from native diagnostic acceptance.

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
