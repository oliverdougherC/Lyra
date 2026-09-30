# Native chat scroll follow-up — September 29, 2026

## Scope and isolation

This pass starts from integrated `main` `c52ac698c62f10cfaf70face1aba3305bb8240ae`.
The code change gives the class-chat transcript a platform scrollbar and checks its
scroll-follow state at most once per animation frame. It leaves message rendering,
streaming, the inline writer thread, and the installed `/Applications/Lyra.app`
unchanged. The signed diagnostic used a separate bundle ID, a fixed separate
persistent WebKit store, null Keyring, and a disposable backend profile containing
a 122-message synthetic chat. It did not open the normal study profile.

## What the native measurements establish

The internal display was configured for 120 Hz. A window-only ScreenCaptureKit
probe requested 120 samples per second and compared sampled pixels in the
diagnostic window; it stored frame times and hashes, not screenshots or user
content. In the same WKWebView, a minimal CSS-moving square produced 770
distinct sampled frames over 7.08 seconds (about 109 per second, 8.8 ms median
between changes). Its JavaScript `requestAnimationFrame` control still returned
181 intervals over three seconds, with a 17 ms median. This demonstrates why
JavaScript callback cadence alone cannot certify visible motion or a universal
60 Hz presentation limit.

An automated long-chat wheel step produced a short run of visible changes near
8–9 ms apart in one diagnostic sample. A smooth "Jump to latest" run changed
mostly at 17–19 ms intervals. These were discrete accessibility-driven actions,
not continuous physical trackpad input. The capture does **not** yet establish
sustained 120 Hz chat scrolling, physical input-to-paint latency, or a measured
before/after hitch reduction from the code change. The capture process and its
window selection were validated against the actual large Lyra window; an
earlier selection of a tiny auxiliary window was discarded.

The [WebKit high-refresh request](https://bugs.webkit.org/show_bug.cgi?id=294338)
remains open. Its status is context, not proof that all WKWebView compositor
motion is limited to 60 Hz. Apple's
[ScreenCaptureKit frame interval](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/minimumframeinterval)
is a capture rate request; counting distinct pixels is necessary to distinguish
capture callbacks from changed content.

## Verification boundary

The focused chat regression covers native overflow, keyboard focus, reader-driven
scroll-follow transitions, and one distance check per JS frame. The full frontend
suite passed 1,427 tests across 126 files; typecheck, lint, formatting, frontend
build, documentation links, and active-reference checks passed. The separately
identified diagnostic bundle was signed with the established development identity
and passed strict bundle verification. A physical trackpad sample and
candidate-equivalent production-identity acceptance remain outstanding. PLA-570
must stay open until actual chat presentation and input latency meet its target.

## Segmented AppKit review candidate

The subsequent macOS-only review candidate keeps React's existing message
renderer but places short transcript sections in one AppKit scroll view. A
single full-height child WebKit view stopped painting near the bottom of a
35,768-point synthetic transcript. Four shorter child views inside section
containers painted the final answer and the boundaries between sections.
The isolated test used 128 synthetic message rows, a separate bundle ID and
backend profile, and null Keyring; the normal installation remained running
and untouched. Light and dark themes, leaving and reopening the chat, and
native scrollbar movement were checked in the isolated app.

With the internal display at 120 Hz on AC power, a three-second native
scroll driver advanced at 360 ticks. Window-only ScreenCaptureKit sampling
of the actual rich chat captured 330 changed transcript frames in 2.995
seconds (about 110 per second), with an 8.69 ms median and 12.02 ms 95th
percentile between visible changes; one interval exceeded 16 ms. The capture
itself completed 946 frames over about 8.4 seconds, below 120 per second,
so this evidence demonstrates substantial improvement over half-rate
presentation but does not certify that every 120 Hz display refresh painted
a distinct frame. Physical trackpad feel and exact production-identity
acceptance remain open gates for PLA-570.

## PR #100 correctness correction

The targeted correction is code commit `8db2fe8db5c0430d3b8b64c738e8868d1ceca116`
on `perf/pla570-native-chat`, following reviewed head
`e16cc21cb133916bff1798a248af7dfc8fe3e8f0`. It keeps the segmented
AppKit renderer and narrow main/child capability split. Main-thread operations
now validate their owning mount and snapshot generation before touching the
scroll view; teardown revokes the owner before releasing the view. Native show
and hide requests carry ordered presentation IDs and acknowledge actual AppKit
application. Child actions carry host, conversation, snapshot, and row identity,
and show waits for the requested conversation's sections to be sized and ready.

The main WebView's source picker and other interactive portals temporarily use
the ordinary transcript above a hidden native view. The handoff transfers the
scroll position and reconnects the ordinary viewport's scroll input and content
resize observer. Selection settlement now requires the actual live row and turn
generation, including when the answer spans a section boundary. A failed native
frame or render tears down its mount and leaves the ordinary transcript usable.

Focused regressions were first observed failing against the reviewed implementation.
The corrected code passed 1,440 frontend tests across 129 files, typecheck, lint,
production frontend build, Rust check, 58 Rust tests, Clippy with warnings denied,
and documentation/active-reference checks. The bridge tests use controlled IPC and
DOM behavior; the Rust tests cover owner and request ordering without executing a
native UI race. The signed candidate bundle and frozen-backend smoke result are
recorded in the PR body at the exact final head after packaging.

No production-identity native acceptance was available: it requires a separate
account or device without an incumbent compiled-ID endpoint. Full main-WebView
reload recovery from an orphaned native host also remains unverified; a foreign
host mount is rejected to protect the current owner. The normal installed app and
study profile were not opened or replaced. PLA-570 remains In Progress because
synthetic scrolling and capture still do not establish exact 120 Hz presentation,
physical trackpad feel, or production-identity acceptance.

## PR #100 targeted section, callback, and selection correction

The next bounded implementation commit is
`30bd3810b105c94d513afcfbd4800e3d2996362b`, based on reviewed PR head
`7551212695192f79e0a5726b94630ad4f7eaf339`. It separates each section's
current sizing revision from the transcript publication version. An unchanged
early section can therefore reflow after a later section changes, and the final
readiness acknowledgement advances the host to the current transcript version
even when that acknowledgement comes from an older section. Frame and scroll
requests use their own ordering while teardown still revokes the mount owner.

The child now sends a stable content epoch and revision with live-turn callbacks.
The Rust action boundary and React host accept a delayed reasoning-open event
when the same turn's text has only grown, and accept a terminal reveal drain
after a content-identical publication such as a theme update. A drain for
extended or replaced text remains invalid; replaced content also invalidates
delayed reasoning state. Host, conversation, row, and generation checks remain.
Exhausted section-height retries report native failure so the ordinary DOM
transcript returns. The live-to-static Markdown handoff restores forward and
backward selections across multiple text nodes.

The delayed reasoning and terminal drain regressions, plus backward selection
restoration, failed against the reviewed head before the implementation edit.
The corrected worktree passed 1,449 frontend tests across 130 files; typecheck,
lint, formatting, and a production frontend build; 61 Rust tests, all-target
check, and Clippy with warnings denied; and documentation and active-reference
checks. Focused tests exercise 65-row section ordering, delayed bridge actions,
terminal turn settlement without another token, exhausted height recovery,
and directional live-to-static selection. These are controlled IPC/DOM and
Rust protocol tests, not a candidate-equivalent native UI race execution.

The retained signed bundle and frozen-backend result are recorded in the PR
description at the final source head. The normal installed app and study profile
remain untouched. Candidate-equivalent native UI acceptance still requires an
isolated account or device without an incumbent compiled-ID endpoint, and
PLA-570 remains In Progress pending its separate presentation/input gate.
