# Optimization evidence — September 8, 2026

This pass implements PLA-509–514 from the optimization review against main
`d6ee39f52574da7f8eeb5633ea650aa8e99cc9a5`. Three DSH Local coding agents implemented
the initial changes and revisions; integration added independent durability, cache,
and citation-work regressions. The combined review source also retains the separately
verified Ask composer and the installed beta.1 release metadata.

## Behavior and boundaries

| Work | Result |
| --- | --- |
| PLA-509: observation | Settled activity stops polling. Running turns and commands remain observed. Hidden document, draft, and study observation skips requests; visibility return forces reconciliation even inside the normal cache freshness window. |
| PLA-510: chat updates | Settled transcript elements and handoff lookups are reused during typing and live deltas. Closed reasoning retains transport bytes without publishing each delta; opening and completion flush the current text. Scroll-follow work is deduplicated per frame. |
| PLA-511: long answers | A memoized Markdown subtree preserves full parser context. An adaptive presentation gap reduces repeated parsing as answers grow; first content, reset, completion, and visibility return remain explicit flush boundaries. |
| PLA-512: editor | Citation decorations map through edits; only affected textblocks are rescanned. Mark steps and structural edits retain full-scan equivalence. Comment normalization is shared per resolution, and flash styling uses mapped anchors. |
| PLA-513: saves | Healthy typing retains the 1.5-second debounce. Retryable failures use increasing delays capped at 60 seconds; further edits share the retry deadline. One document-owned engine survives navigation until its text is confirmed. View cleanup cannot replace newer text or create another writer. |
| PLA-514: migration backup | Database hashing and private source copying/hashing use bounded buffers. Consistency, digest validation, size limits, permissions, no-follow checks, fsync, and failure cleanup remain required. |

The local turn registry uses the existing query cache, synchronous state, and normal
garbage collection. No dependency, alternate Markdown parser, editor replacement, or
new backend event service was added.

## Recorded cost observations

These are synthetic fixtures and operation counts, not native frame-rate or battery tests.

| Fixture | Before | After |
| --- | --- | --- |
| 60 seconds, settled chat without workspace | 44 reads | 2 initial reads; no periodic reads |
| 60 seconds, settled chat with workspace | 106 reads | 4 initial reads; no periodic reads |
| Five minutes of immediate retryable save failures | 200 attempts | 9 attempts |
| One edit in a 150-paragraph draft | 150 text nodes / 7,359 characters rescanned | The affected textblock only |
| Integration fixture: one edit among 200 citations | Initial agent implementation reread 400 document ranges | 0 range rereads, 1 text node / 36 characters rescanned |
| One resolution pass with eight comment threads | Repeated document normalization per thread | One whitespace index and at most one canonical index |
| Migration fixture: two 16 MiB owned files and SQLite snapshot | 553,661,095 bytes peak Python allocation | 2,116,105 bytes peak Python allocation |

The migration baseline's bounded `read(max_bytes + 1)` could itself reserve a large
buffer before returning the smaller source. `tracemalloc` measures Python allocations;
these figures are not total process RSS or ordinary idle memory.

The chat work report uses both short and 60-row histories. Typing previously rendered
3,360 settled rows and performed 6,720 handoff lookups in its typing window; the final
boundary performs zero settled-row iterations or lookups in that window. Forty reasoning
deltas in a closed disclosure publish the initial presence once, retaining the remaining
text until opening or completion.

The local-agent final renderer measurements reduced the 40,002-character fixture from
1,061 reparses to 186–197 across recorded runs. One recorded run processed 1,601,068
normalized characters rather than 20,053,708 and visited 847,024 reveal nodes rather
than 3,364,846. The adaptive gap depends on measured parse duration, so exact counts
vary with host load. Final content is included in the fixture. The invariant is that
held updates do not invoke the parser or traverse the reveal DOM, and completion does
not lose the held text. Wall-time measurements made amid concurrent host activity are
not presented as an achieved native latency improvement.

## Regression coverage

The production-hook tests cover idle and active observation, pending decisions, dismissal
expiry, errors, visibility, and owner-scoped turn release. Integration tests add the
production five-second freshness window and collection of an idle turn registry.

The save tests cover version conflicts, lost acknowledgements, edits/reverts during a
write, retry deadlines, failed flushes, StrictMode, late completion, rapid remount, and
session retirement. A pending document retains one engine and its current version;
an uninitialized or older view cannot flush a stale local buffer over that state.

Citation tests compare the production plugin with its full-scan oracle for edits,
mark boundaries, paste, composition-shaped transactions, split/join, undo/redo, and
replacement. The many-citation regression also guards against hidden full-document
range work. Backup tests use synthetic files and fault injection, including refused
symlinks without descriptor leaks.

Integration verification passed: 1,335 frontend tests across 120 files; 3,854 backend
tests with one skip; frontend lint/typecheck; Python Ruff checks and formatting; and
documentation link/active-reference scans. All 13 existing Chromium stream playback
checks passed, including containment, ordered reveal, stop/retry, hidden completion,
selection, and scroll anchoring. Baseline suites before implementation passed 1,224
frontend tests and 3,840 backend tests with one skip.

The signed review-bundle receipt is recorded with the task handoff. Packaging follows
[local deployment](local-deployment.md), including the rebuilt Python sidecar and the
signed frozen-backend smoke check. The normal installed app is not replaced by that
review-build workflow.

## Remaining measurement limits

PLA-337 native frame pacing and controlled whole-process before/after measurement were
not executed. The available normal account is not the isolated native acceptance
environment required by [AGENTS.md](../AGENTS.md). A browser playback run and a signed
backend smoke check do not establish packaged WKWebView FPS, compositor cadence, energy
use, or battery runtime. No 120 Hz result is claimed.

Full-document parsing remains necessary to preserve arbitrary Markdown context. Long
single textblocks still need a complete local citation scan, and positional decoration
mapping still has a cost. Save-session recovery retains pending text across view changes
within the current webview; server acknowledgement remains the durability boundary.
