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
