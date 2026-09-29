# Daily-study correction review receipt — September 29, 2026

This packet accompanies PR #98 and its owning branches #95–#97. It uses synthetic
data only. The screenshots below were captured at the previously reviewed combined
head `90b2938046bf0ba51223b5acc865efe504dbdc4f`; they illustrate preserved
draft and activity presentation, not native performance or proof of the new repairs.

## Correction coverage

| Review item | Owning PR | Failing-before regression and passing behavior |
| --- | --- | --- |
| R1, PLA-564 | #96 | Full module reload, unchanged restored send, and another reload; Class Ask, tutor, and writer composers; new-session handoff; route departure through the production `AppRoutes`; failed send, newer follow-up, and refused durable retirement. Tests inspect visible text and stored revisions. |
| R2, PLA-564 | #96 | Hundreds of same-window reloads and source-choice writes, legacy selection migration, distinct active windows, acknowledged shared revisions, full-cap refusal, and quota denial. Foreign unsent records remain untouched; a full store reports that the new prompt is not durable. |
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
candidate. No candidate-equivalent native account or device was available, so
PLA-570 remains open. Browser timing, packaging, and this probe do not establish
120 Hz Lyra acceptance.
