# PR 73 original-file recovery acceptance fix

CI run `34006192578`, head `56830`, failed in `source-original-recovery.spec.ts` test starting at line 134. The failing wait was actually the **extracted-text response** in `showDoubleFailure` at line 84; execution had not reached the original-file 404 check.

The downloaded trace establishes the event-order bug:

- `/api/documents/16/text` returned HTTP 200 with `{"filename":"recovery-2.pdf","text":"","truncated":false}`.
- Its request started at monotonic `124100.997` ms and finished after `38.162` ms, at `124139.159` ms.
- The helper armed `waitForResponse` at `124249.107` ms, about 110 ms after completion, then clicked **Read extracted text** at `124250.602` ms.
- `SourcePane` can render `SourceText` before document MIME metadata resolves. The legitimate early text result is cached, so switching to extracted text does not require a second HTTP response.

The test now arms the same response wait before navigation can trigger the request. It retains the HTTP 200/empty-text checks, original-file 404 and download-byte checks, privacy assertion, and source/page/problem context assertions. No application code, timeout, retry, or failure-ledger changes were made.

Verification: both targeted real-stack Chromium acceptance tests passed (4.2 seconds) on isolated ports 3415/8415/19415; zero unconsumed backend failures, owned processes terminated and temporary profile removed. Prettier and `git diff --check` passed. The original local tests also passed before the edit, consistent with the CI trace's event-order race; that local run is not represented as a deterministic pre-fix failure.

Command: `ACCEPTANCE_FRONTEND_PORT=3415 ACCEPTANCE_BACKEND_PORT=8415 ACCEPTANCE_TUTOR_PORT=19415 corepack pnpm exec playwright test --config playwright.acceptance.config.ts e2e/acceptance/source-original-recovery.spec.ts` from `frontend/`.
