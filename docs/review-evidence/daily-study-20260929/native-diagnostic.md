# PLA-570 bounded native diagnostic, 2026-09-28 PDT

## Identity and isolation

- Reviewed pre-correction combined source: `67c5fdb678e0d816195ab55d40d75160a25f3b36` (#98 before R5–R7). This is **not** the final corrected candidate.
- Detached diagnostic checkout and signed bundle, compiled identifier `com.lyra.pla570.diagnostic`, Tauri single-instance endpoint keyed to that identifier, `WebviewWindowBuilder::incognito(true)` for a nonpersistent WebKit store, disposable backend data/cache/log/model/database paths under `/private/tmp`, and null Python Keyring.
- Candidate built frontend files and staged frozen backend source bytes were copied from the retained `67c5fdb` build. The diagnostic added one external self-hosted timing script to its built HTML and changed native identity/store/profile selectors. It therefore exercises the candidate application code and real native window/backend, but is not production-byte, persistent-store, Keychain, or final-head equivalence.
- Code-signature verification and frozen-backend smoke passed (`authenticated=true`, two symbolic computations, ephemeral loopback). The exact diagnostic bundle launched and quit gracefully; its native and backend PIDs exited. The `/Applications/Lyra.app` identity was neither launched nor replaced.

## Synthetic actual-UI journey

Using the diagnostic native window, created a synthetic class, opened its real writing editor, entered a short paragraph, pasted 40 synthetic paragraphs, navigated the long editor, and let it save. No user documents, real tutor, or normal profile were used.

An event-driven diagnostic script scheduled animation frames only for 500 ms after input/keydown/pointer/scroll events; it did not force an idle frame loop. In the long-draft paste snapshot:

| JS timing in actual editor | Samples | Median | p95 | Max |
| --- | ---: | ---: | ---: | ---: |
| `input` event to next rAF callback | 171 | 10 ms | 23 ms | 59 ms |
| Active rAF callback interval | 169 | 17 ms | 30 ms | 89 ms |

The additional page-navigation snapshot reached 287 cumulative active rAF intervals (17 ms median, 25 ms p95, 105 ms max). Only two `scroll` events were observed, so this does **not** establish sustained scrolling pacing. WebKit reported no Long Task API support. These values begin at JS event delivery and end at a JS callback; they do not measure hardware input-to-paint or presented frames. The overlay and UI automation add overhead; no uninstrumented before/after comparison was run. The 17 ms median is consistent with the earlier minimal WKWebView callback result, but cannot establish an OS cap or a 120 Hz delivery claim.

## Owned-process samples

The repository's privacy-safe desktop runtime reporter verified the app's three WebKit sibling processes through LaunchServices coalition membership. A settled, visible long-draft sample after more than 60 seconds of no interaction retained five processes (Tauri shell, WebKit GPU/WebContent/Networking, frozen backend), 389.7 MB aggregate RSS, 1.1% single-sample aggregate CPU, and no forbidden idle helper. A post-task single sample was 389.6 MB RSS and 1.8% aggregate CPU. After the diagnostic minimize button was clicked, a sample was 392.5 MB RSS and 0.9% aggregate CPU; window minimization could not be independently verified by the accessibility surface, so this is **not** a certified hidden-state measurement. These are snapshots, not energy or sustained-idle measurements.

## Remaining gate

No production-identity or final-candidate native acceptance, presented-frame trace, physical input-to-paint latency, streaming chat, ingestion contention, long-session soak, or verified hidden-window quiescence was measured. A system-wide Animation Hitches trace was attempted but had excessive collection overhead and could include inherited environment values; its trace and exports were deleted immediately and no result from it is cited. A separate account/device with no incumbent production-ID endpoint remains required for production-native acceptance. PLA-570 remains In Progress.
