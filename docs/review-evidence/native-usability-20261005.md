# Native usability repair — October 5, 2026

This pass follows the user-reported header clipping and unusable chat scrolling in the
installed app at `25d717f`. Browser and mocked-IPC tests from the preceding review had
not exercised the actual native input and painting path.

## Reproduction and corrections

Both reported failures reproduced in a signed diagnostic app using production UI code,
a separate compiled identifier, nonpersistent main/child WebKit stores, and synthetic
conversations. The long conversation contained forty question/answer pairs across three
native sections. Wheel input over an answer left the visible transcript unchanged.
The native transcript also obscured the lower portion of the breadcrumb/header.

Corrections cover:

- Vertical wheel hit testing now routes to the owning AppKit scroll view, preserving
  WebKit targets for clicks, selection, and horizontal scrolling.
- Viewport coordinates originate in the main WebView's safe-area rectangle, including
  zoom. The observed main view had bounds `1400 × 813`, a flipped coordinate system,
  and safe area `(0, 32, 1400, 781)`. DOM header coordinates excluded that native inset.
- AppKit views and their backing layers explicitly clip hosted WebKit content. Section
  height stores measured content rather than a minimum viewport height per section.
- Page Up/Down and Home/End use the existing authenticated child action route. Cmd/Ctrl+B
  reaches the sidebar even when the transcript owns keyboard focus.
- Initial mounting remeasures layout before publication. A page-load epoch invalidates
  obsolete native owners immediately; asynchronous cleanup and fresh mounts reclaim stale
  overlays without blocking the main thread on the native state mutex.
- An acknowledged draft save now updates the detail cache. A native save → navigate away
  → reopen → edit journey had shown stale text as Saved and then a false external conflict.
  The earlier edit was preserved on disk; the stale query cache seeded the wrong editor body.

Coordinate conversion uses [NSView conversion](https://developer.apple.com/documentation/appkit/nsview/convert(_:from:)-7fbb6)
and the main WebView's [page zoom](https://developer.apple.com/documentation/webkit/wkwebview/pagezoom).

## Native interaction evidence

The final diagnostic included a small diagnostic-only native/DOM indicator and reload
button. A readable DOM fallback was not counted as proof of native rendering. Temporary
frame outlines were removed before the final visual checks.

| Journey | Observed result |
| --- | --- |
| Long native transcript, light and dark themes | Header and composer remain visible; native sections active |
| Wheel over answer text | Moves to earlier answers; Jump to latest appears |
| Home, End, Page Up, Page Down | Navigate the owning native transcript |
| Cmd+B while transcript has focus | Sidebar toggles; transcript follows resized layout |
| Source picker open / Escape | DOM handoff and native return remain usable |
| Main document reload | Fresh native owner appears and wheel scrolling still works |
| Stream → scroll → type follow-up → Stop | Stop remains responsive and unsent follow-up remains available |
| Send after Stop | Subsequent deterministic answer completes and remains in history |
| Short chat, fullscreen entry and exit | Content and composer remain in their respective regions |
| Chat → writing → chat → writing | Native overlay is removed on departure and remounts on return |
| Edit → Saved → navigate → reopen → edit again | Latest text stays visible; second edit saves without false conflict |
| Valid synthetic study card | Answer reveal and Good rating complete; due count updates |

The backup helper's minimal card fixture initially lacked scheduler state and a required
topic. Those fixture fields were completed before the successful study journey; the initial
fixture failure is not presented as a production regression.

## Automated coverage and limits

`scripts/native_chat_view_smoke.m` compiles the production Objective-C implementation,
uses actual nonpersistent WKWebViews in a private offscreen application, and checks geometry,
safe areas, zoom, clipping/layer masks, hit-tested wheel routing, horizontal/click preservation,
short-section resizing, and the keyboard scrolling helper. Frontend and Rust tests cover
the event bridge, stale ownership, reload epochs, and save/query ordering. The macOS CI job
runs this native smoke test. Its private event queue does not synthesize desktop-wide input.

The smoke test does not measure screenshot pixels. The actual native app observations above
provide the visual/interaction evidence. They do not certify physical trackpad latency,
sustained 120 Hz delivery, sleep/wake soak, production Keychain interaction, or live-model
semantic reliability. Exact build and final CI results belong to the associated PR receipt.

## Diagnostic isolation incident

Before the disposable selectors were embedded in the diagnostic entry point, a UI-state
request after Quit reopened the diagnostic through LaunchServices without its shell launch
environment. It was stopped immediately. The production backend log scan showed no new
reconciliation-count entries for this incident, but no before/after state snapshot existed;
absence of impact cannot be established from that scan. Subsequent diagnostic builds fixed
all disposable paths before startup, including automatic reopens. The normal installed app
identity was not used for UI testing. Maintained deployment guidance now explicitly covers
this reopen boundary.
