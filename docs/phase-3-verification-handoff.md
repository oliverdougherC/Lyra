# Phase 3 Verification Handoff

You are the last set of hands on Phase 3. The phase is code-complete: a critical review pass
found ~60 defects across the pipeline, and a close-out pass fixed everything it could and
recorded what it could not. Your job is the part that unit tests cannot give: run the real
measurements, drive the real app, and confirm the fixes behave under real conditions. Nothing
here asks you to build a feature.

Read [phase-3-handoff.md](phase-3-handoff.md) first — especially "The close-out pass" and
"Traps" — and [rag-pipeline.md](rag-pipeline.md) for the pipeline itself. This document does not
repeat them.

## State you are inheriting

- Branch `main`, clean tree. The last 11 commits are Phase 3: five feature commits
  (`ad9e1e6..0f111ff`) and six close-out commits (`4c2f4a7..e85370d`).
- Suites at handoff: **794 backend** (`.venv/bin/python -m pytest backend/tests -q`),
  **360 frontend** (`cd frontend && npx vitest run`), ruff check and format clean.
- The development tutor endpoint was answering `model name=Qwen3.6-27b failed to load` to every
  request when this was written. `POST /api/settings/test-vision` tells you whether it is back.
  Several tasks below are gated on it; do the ungated ones regardless.
- Eval workspaces already on disk: `data/eval-ingest` (the 608-page Kuttler textbook),
  `data/eval-recognize` (the scanned Fourier handout, already transcribed), `data/eval-class`
  (the whole 36-document course). The harness works in its own workspace and never touches the
  student's data — but see the trap note below.

## Environment facts that will bite you if unknown

- `settings.db_path` now **derives** from `data_dir`, so `LYRA_DATA_DIR` alone moves everything,
  database included. `LYRA_DB_PATH` still wins when set. The handoff's first trap describes the
  old behavior; you have the new one.
- The eval harness overrides the remote-content acknowledgement and prints a notice when it
  does. If the configured endpoint is remote, page images leave the machine — that is your
  choice to make, not the harness's.
- PyMuPDF fixture trap: `insert_image` needs `keep_proportion=False` or your fixture silently
  shrinks below the figure size floor.
- The frontend test setup installs a throwing `fetch`. A test that needs the network is a test
  that needs a stub at `api`.

## Tasks, in priority order

### 1. Re-run the retrieval measurements (ungated — needs only the embedding server)

The close-out changed retrieval-relevant code: every path now filters to `ready` documents,
document-scoped search is real, section references resolve case-insensitively, and — the one
that matters most — **boundary pages are now credited to the section whose heading they
announce** (`rag/structure.py::section_for_page`). That last fix only takes effect when a
document is re-ingested, because section metadata is stamped onto chunk rows at chunk time.

Run the documented command pairs from the handoff's "Re-running every measurement" section
(they now carry `--k 64` explicitly; the default also matches). Concretely:

- `data/eval-ingest`: re-ingest the textbook `--fresh`, then `retrieve`. The recorded numbers
  to hold or beat: **17/17 at k=8, 16/17 at rank 1**. The boundary fix should help or do
  nothing; if any question regresses, the fix is the first suspect — bisect with the chunk's
  `section_path` for the failing question's expected page.
- `data/eval-class`: re-ingest `--fresh --no-extraction`, `reindex Fourier_Tables --recognize`
  is **gated on the endpoint** — if it is down, skip the reindex and note that the class is
  measured without the handout's transcription, then run both `retrieve` variants (plain and
  `--rerank`) with the ece203 set. Recorded: 9/16 → 12/16 rank-1 with reranking. Small-n
  caveats apply ("What these numbers can and cannot say") — a one-question wobble is noise, a
  three-question drop is a finding.
- Write the new numbers into the handoff tables if they differ, with the date. A negative
  result is recorded, not discarded.

### 2. Drive the server lifecycle for real (ungated)

Unit tests fake the processes; you should not.

- Start the backend (`scripts/start` or however `scripts/dev` does it), wait for startup, then
  `ps aux | grep llama-server`. With reranker weights installed you should see the warm-started
  rerank server. Stop the backend cleanly and confirm **zero** llama-server processes survive —
  this was the worst lifecycle bug of the review; the fix is `main.py`'s lifespan.
- Kill the backend uncleanly (SIGKILL) and restart: the servers it orphaned should be adopted
  on the next start (health + model verification), not duplicated. `ps` again.
- If cheap to stage: put a wrong GGUF on the rerank port's expected path or start a foreign
  llama-server on that port, and confirm retrieval logs the refusal and keeps the embedding
  order rather than adopting it.

### 3. Verify the frontend changes in the browser (ungated)

Method per the handoff's last section: a **copy** of a real class in a scratch data dir
(`LYRA_DATA_DIR` now suffices, but set `LYRA_DB_PATH` too if you want to be certain against an
older checkout), states edited in for the rows a healthy class never has. Check at 1280 and
375, both themes:

- The new ready-with-skip-reason state on `DocumentRow`: a `ready` document whose
  `recognize=1` and `error_message` is set must show the reason and a "Try again" action.
- The failed-with-endpoint-blame state (`ENDPOINT_FAILED_MESSAGE`) reads as an endpoint
  problem, not a document problem.
- Regression: `FigureBlock` images still render at natural size (`self-start` fix from the
  phase proper).

### 4. Exercise the failure paths end to end (ungated, use a dead endpoint on purpose)

Configure a tutor endpoint that connects but cannot answer (or point at a closed port), then:

- Upload a small scanned document with recognition requested: the run must stop after three
  consecutive page failures and the document must blame the **endpoint**, not claim the pages
  are unreadable. Retry after removing the endpoint must surface the no-endpoint message, not
  the stale one.
- Queue a solve on a two-problem sheet: the breaker must abort after three consecutive unit
  failures (be patient once — each unit may take the timeout) and the artifact must land
  `failed`, never `ready` with a full progress bar.
- Mid-recognition, delete the document from the UI: the run must abandon quietly; re-upload
  the same file immediately and confirm the new document ingests clean with no inherited state.

### 5. Gated on the endpoint coming back

- **The outstanding `TRANSCRIBE_PROMPT` measurement** — the one open acceptance item. The
  command and the baseline to beat are in the handoff ("three notations, three of eight pages
  with no table markup, ten headings the chunker can see"). Read the result back with the
  notation counter as the recorded run was.
- The class-scale `reindex Fourier_Tables --recognize` from task 1.

### 6. Polish sweep (last, only if the above is green)

- `git log` sanity: messages honest, no stray files committed.
- Grep for leftover debug prints or TODO markers introduced by the close-out commits.
- Confirm `docs/` internal cross-references still resolve (the close-out edited four docs).

## Rules that outrank anything above

- **Measure through the real code path in process.** No reimplementations, no shortcuts. This
  is the project's standard of evidence and it has caught four faults no test suite did.
- **Record negative results with their numbers** in the doc that owns the topic, so nobody
  spends the day twice.
- **Do not make recognition automatic**, do not sweep `unsupported` documents into a queue,
  and do not weaken the reranker's silent-degradation contract — all three are deliberate
  product posture, documented in the handoff.
- The three deliberately open items (transcribe re-measurement when gated, BM25-before-new-
  embedder, page-selective vision gate) are **next-phase work** except where task 5 opens one.
  Do not start them here.

## Definition of done

Every ungated task above has either a confirming result or a written finding; the handoff's
tables carry re-measured numbers where they changed; anything you could not do says so, with
the gate named. If you find a defect, fix it only if it is small and within the verification's
blast radius — otherwise record it precisely and leave it: this phase has had its surgery.
