# Activity history geometry — PLA-573 / PLA-575

Synthetic Chromium preview evidence for the chat activity placement on the activity branch
(`ab5ebf0`). The browser test is
[`activity-geometry.spec.ts`](../../../frontend/e2e/activity-geometry.spec.ts). It holds the
activity response while an existing settled transcript and composer render, waits for fonts and
two animation frames, then releases a failed `read_file` event followed by a successful
`read_file` on another path. The history button first appears in the app header; the failed
attempt stays in history without a student-attention warning. It also checks opening history,
reload, and switching to another session and back.

Both runs used reduced motion. The narrow case applied CSS `zoom: 1.25` to the document root;
this is a large-text geometry probe, not an operating-system or browser zoom setting. Values
below are CSS pixels from `getBoundingClientRect()` and the transcript viewport's `scrollTop`.
The test permits at most 0.5 px of change on automatic history availability.

| Browser viewport | Measurement | Before | After | Delta |
| --- | --- | ---: | ---: | ---: |
| 900 × 650, 100% | Transcript content top | 102 | 102 | 0 |
| 900 × 650, 100% | Composer input top | 531 | 531 | 0 |
| 900 × 650, 100% | Transcript viewport top / bottom | 102 / 512 | 102 / 512 | 0 / 0 |
| 900 × 650, 100% | Transcript scroll offset | 0 | 0 | 0 |
| 390 × 560, CSS 125% | Transcript content top | −373 | −373 | 0 |
| 390 × 560, CSS 125% | Composer input top | 466.5 | 466.5 | 0 |
| 390 × 560, CSS 125% | Transcript viewport top / bottom | 127 / 443 | 127 / 443 | 0 / 0 |
| 390 × 560, CSS 125% | Transcript scroll offset | 400 | 400 | 0 |

Captured frames: [wide before](activity-geometry-before-wide.png) ·
[wide after](activity-geometry-after-wide.png) ·
[narrow before](activity-geometry-before-narrow-125.png) ·
[narrow after](activity-geometry-after-narrow-125.png). The full-stack selector follow-up
separately passed seven affected Chromium acceptance tests with Applied and Rejected results
still checked in the opened history.

The table and frames above are the original pre-#100 baseline; that selector measured an old
ScrollArea and cannot establish current native chat behavior. The current-main correction
asserts exactly one conversation native host and at most one Conversation DOM fallback, then
measures that actual fallback in Chromium. A missing target or unrelated scroll area now fails.

The corrected browser regression starts at `?session=new`, creates the session on Send, emits a
valid first tool frame, streams a long answer in two parts, verifies the reader can scroll away
from real overflow and stays there through completion, refetches the saved rows, and only then
releases durable activity history. It asserts the
transcript origin, composer top, conversation bounds, and scroll offset each stay within 0.5 CSS
px as the header history control becomes available. The test passes at 800 × 600 and 900 × 650
with CSS 125% zoom and reduced motion. Opening history deliberately is checked separately.
The old settled-history/reload/session-switch cases also pass at 900 × 650 and 390 × 560 with
CSS 125% zoom; the latter still has the documented pre-existing bottom-navigation obstruction.

The attention control now uses the existing Radix menu. A 12-action browser probe at 800 × 600
measured the open menu at x=528–784, y≈37–277 and the conversation fallback at x=0–800,
y=342–462: **no overlap in this crowded supported case**. The source risk was real even though
that layout did not reproduce occlusion: a future overlapping menu is recognized by #100's
open-menu observer, which hides the AppKit transcript and displays the DOM fallback. Browser
checks cover keyboard entry, Escape and outside dismissal, trigger focus restoration, exact
action focus, and unchanged conversation scroll on Escape. Native AppKit hide/show and reading
position remain unverified without an isolated Mac account; this browser geometry is not native
acceptance. The normal installed app and profile were not opened or altered.
