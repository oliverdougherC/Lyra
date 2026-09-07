# PLA-486: scroll persistence without consuming navigation quota

This focused correction follows [review 5128341066](https://github.com/oliverdougherC/Lyra/pull/82#pullrequestreview-5128341066)
on PR #82 head `6e6efd55a50f4f0f3018a71c4ea9cd705a5e1519`. It changes router bookkeeping,
not the UX layout or backend contracts.

## Reproduction and repair

Actual Playwright WebKit 26.0 reproduced the reported failure in the application, using synthetic
class/file APIs but the browser's **real, unmodified History operations**. Wrappers counted calls
and rethrew native exceptions; no quota adapter was substituted for browser evidence.

| Scenario | Reviewed baseline | Repaired application |
| --- | --- | --- |
| 650 unchanged, untracked nested scroll events | 650 replaceState attempts; 550 native SecurityErrors; immediate home navigation refused, no push | Zero scroll History writes or exceptions; immediate home push succeeds |
| Sustained main/Files/programmatic scrolling | 12,007 ms, 721 frames, 3,603 events; 3,403 native errors; home navigation refused | 12,004 ms, 713 frames, 3,563 events; zero scroll History writes/errors; immediate home push succeeds |
| Restoration after the repaired sustained run | Navigation blocked before it could be tested | Main/Files Back, Forward and reload restore exact positions and Files filter |
| Quota deliberately consumed by real History calls before navigation | Native quota exception; home navigation blocked | Native push and identity-stamp refusals observed; Classes renders at its hash URL; original document marker survives and document request count remains one |

WebKit's native message was `Attempt to use history.replaceState() more than 100 times per 10 seconds`.
These are automated browser results, not physical/human acceptance or an independently measured
quota guarantee for every macOS/WebKit version.

The router now assigns history-entry identity at navigation boundaries. Only main and Files
viewport scroll events update positions. Unchanged snapshots are deduplicated, positions stay in
a 64-entry memory cache, and session storage checkpoints at most once per second during scrolling.
Navigation/pagehide flush the final pending snapshot, without History calls. Older normal entries
can be read from session storage after eviction from memory. Existing legacy `lyraScroll` snapshots
remain readable.

Normal push/replace/anchor navigation spends one History operation; initial legacy entries may need
one identity/canonicalization stamp. Sustained scrolling spends **zero**, leaving the shared quota
for navigation. Refused optional storage reads/writes cannot abort a route change. If the browser
refuses even a real History navigation, the router falls back to same-document hash navigation,
keeping URL and rendered destination consistent without reloading the app.

Position restoration is best effort when storage is unavailable or the browser refuses entry
identity stamping: an unstamped fallback hash entry cannot guarantee its previous scroll on a later
visit. It still remains navigable. Ordinary Back/Forward/reload, delayed pane mounting, class return
context, filters, Settings anchors and quiz-help behavior keep their existing contracts.

## Verification protocol

```sh
cd frontend
./node_modules/.bin/vitest run tests/router-hooks.test.tsx tests/scroll-positions.test.ts
VITE_API_BASE=http://127.0.0.1:8000 ./node_modules/.bin/vite build
PLAYWRIGHT_ENABLE_WEBKIT=1 PLAYWRIGHT_FRONTEND_PORT=18169 \
  ./node_modules/.bin/playwright test e2e/scroll-quota.spec.ts --project webkit
```

Unit tests separately exercise a quota-model adapter, refused real-navigation calls, optional
storage quota/security exceptions, sustained checkpoints, unchanged snapshots and memory-cache
recovery. Those models supplement, rather than replace, actual WebKit reproduction.

Local before/after JSON metrics, screenshots and exact captured source hashes are retained under
`output/playwright/scroll-quota`. The final source SHA, CI, affected acceptance and signed isolated
app receipts are posted on PLA-486 and PR #82. No physical/human acceptance is closed by this fix.
