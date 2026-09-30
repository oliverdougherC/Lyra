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

This measures history **availability** in a browser with synthetic data. The browser test does
not establish manual scroll-away behavior during streaming, new-draft send timing, or native
WebKit frame pacing. At 390 × 560 with CSS 125% zoom, the existing bottom navigation obscures
the composer in both frames; its position does not change when history appears. The signed Mac
candidate and PR #100 native scrolling acceptance were **not executed** in this evidence pass.
