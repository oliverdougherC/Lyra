# Phase 3 Handoff

Phase 3 makes a textbook as useful as a syllabus and accepts the documents Phase 1 rejected. It is
built and it has been measured against a real 608-page book and a real scanned handout rather than
against its own fixtures.

This is not a specification. [rag-pipeline.md](rag-pipeline.md) owns the pipeline and its stages,
and [ui-phase-3.md](ui-phase-3.md) owns the screens. Both were kept current as the phase went, so
where this document disagrees with one of them, they win.

What this holds is the part that does not belong in a specification: what the pipeline did when it
met a real textbook, which assumptions that killed, what is still weak, and the traps that cost
real time and will cost them again.

**Read the traps before touching anything.** Three of them fail silently, and one of them wrote to
a database it was not supposed to.

**Three of the four open items below are now closed, and the verification gap with them.** What
they were, what fixing them cost, and what the third one turned out *not* to be, is under
[What the open items became](#what-the-open-items-became). The one that remains is item 4, which
was deferred on purpose and still is.

**Then the numbers in this document were re-measured against a class, and one of them did not
survive.** Every retrieval figure below was taken in a workspace holding *one document*, while
`retrieve` searches a *class*. See [What class scale did to these numbers](#what-class-scale-did-to-these-numbers).

## What shipped

| Step | What it is | Where it lives |
| ---- | ---------- | -------------- |
| 0 | The specifications | `rag-pipeline.md` stages 1, 2, 2a, 2b, 3, 6; `ui-phase-3.md` |
| 1 | Ingestion and retrieval evaluation | `scripts/eval_ingest.py`, `scripts/eval_questions/` |
| 2 | Structural parsing | `012_section_path.sql`, `rag/structure.py`, `rag/retrieve.py` |
| 3 | Vision over the text layer | `llm/client.py` image parts and probe, `rag/transcribe.py` |
| 4 | Text recognition | `013_document_pages.sql`, `core/recognition.py`, image uploads |
| 5 | The document-list interface | `DocumentRow`, `IngestionProgress`, `DocumentDropzone`, settings |
| 6 | Figures | `014_document_figures.sql`, `rag/figures.py`, `core/figures.py`, `FigureBlock` |
| 7 | OCR specialist | `llm/ocr_server.py`, `scripts/ocr_spike.py`, `fetch_models.py --ocr` |
| 8 | The cross-encoder reranker | `rag/rerank.py`, `llm/rerank_server.py`, `scripts/fetch_models.py` |

Step 5 was not planned. It was added after step 4 because steps 2 through 4 had shipped
capabilities the interface had no way to reach, and the `unsupported` popover was still telling
students that scans would be readable "in a future update" and would "process automatically then".

713 backend tests, 358 frontend when the phase proper closed; 794 and 360 after the close-out pass,
which is where they still stand.

## What it was measured against

Two corpora, and the second one matters as much as the first.

**Kuttler, *Linear Algebra: A First Course*.** 608 pages, a 131-entry outline nested four levels
deep, and mathematics on nearly every page. Everything in stages 2a and 3 was measured against it.

**One term of ECE 203**, the same corpus Phase 2 used, in `data/eval/uploads/1/`. This is where the
scanned handout (`Fourier_Tables.pdf`, eight pages, zero extractable characters) and the figure
acceptance case (`homework_3.pdf`, three block diagrams) live. It is also what kept the phase
honest about scale: 66 embedded images, five captions, and not one decorative image.

The tutor endpoint was Qwen3.6 27B on llama.cpp, the development baseline.

## What measuring found, before anything was built

Four findings, and they reordered the phase.

1. **Ingestion at textbook scale is a non-issue.** 0.8 s to parse 608 pages; the whole book indexes
   in under a minute. The phase's stated open question had no problem behind it.
2. **A textbook was chunked as homework.** `detect_doc_type` had no textbook rule, so a book of
   numbered exercises tripped the problem-marker heuristic and was cut at every numbered line:
   1312 fragments averaging 162 tokens, none carrying structure.
3. **The heading regex could not be promoted.** Forced over the book it labels 595 of 596 chunks
   with things like `3 times the second row to the first row.` and table-of-contents dot leaders.
   High coverage of wrong values is worse than none.
4. **The text layer loses mathematics on pages that were never scanned.** Every matrix extracts as
   a column of loose digits, so an identity matrix and a row swap both arrive as `1 0 0 1`.

Finding 4 moved vision from the scanned-document track to a quality feature for every document,
which is why it was measured before bulk transcription rather than after.

## The numbers

| What | Result |
| ---- | ------ |
| Retrieval over the textbook, before structural parsing | 14/17 at k=8, 11/17 at rank 1 |
| Retrieval after | **17/17 at k=8, 16/17 at rank 1** |
| Is `k = 8` the limiter | No. Widening to 32 gained only the two bare section references |
| Vision against the text layer, 6 pages | Every collapsed matrix recovered; control page 1217 chars against 1218, reproducing the book's own typo `contrinuity` |
| Transcription, reference book | 13.4 s a page |
| Transcription, scanned handout, end to end | 13.8 s a page, 8 of 8 pages, `ready` in 110.8 s |
| Specialist OCR, same eight pages | 18.5 s a page, repetition loops on 5 of 8 |
| Problem positions on the acceptance sheet | 9 distinct of 12 before, **12 of 12** after |
| Figures filed under a problem, same sheet | 0 before, **3 with none wrong** after |
| Retrieval over the scanned appendix, 11 questions | rank 1 on 2 before, 3 after; the unit step **3rd → 1st** |
| The same appendix at chunk targets 750 → 200 | No trend. Rank-1 wanders 2 to 5, top-4 worsens below 300 |
| The same appendix with the running head stripped | No change to rank-1 or mean rank |

Two independent documents at different densities landing within 4% of each other (13.4 and 13.8) is
what makes the rate the model's rather than the page's, and what makes "2.3 hours for a 608-page
book" worth quoting.

## What class scale did to these numbers

Every retrieval row in the table above was measured in a workspace holding **one document**.
`retrieve` is class-scoped: it searches a class, and a class of one document has nothing in it to
compete. The measurement was flattering itself and nobody noticed, including the person who wrote
the table.

Re-measured in `data/eval-class`, which holds the whole course — 36 documents, 1124 chunks:

| What | Result |
| ---- | ------ |
| The textbook set, at class scale | **Unchanged.** 16/17 rank 1, 17/17 at k=8 |
| A set about the course itself, at class scale | **9/16 rank 1, 14/16 at k=8**, 2 never found in 32 |
| Share of the served 8 from the right document | **54 of 128** |
| The same set, fetch 64 and reranked | **12/16 rank 1, 15/16 at k=8**, 1.6 s a question |
| The textbook set, fetch 64 and reranked | 15/17 rank 1, 17/17 at k=8 — one place *worse* |

Every row above was re-measured on 6 August 2026 against the close-out's code, both workspaces
rebuilt with `--fresh`, and **every rank in all four runs came back identical, question by
question** — not merely the same rates. The class is one document smaller in the sense that matters
(`laplace.pdf` now reads as photographed rather than indexing its Gmail header as text, so 1123
chunks rather than 1124), and reranking now costs 2.1 s a question rather than 1.6 on the same
hardware. The share of the served 8 from the right document under reranking, which was not recorded
before, is **57 of 128** against the embedding order's 54.

Three things a reader should take from that.

**The textbook was never the hard case.** It is a linear algebra book in a signals course. Nothing
else in the class talks about Gram-Schmidt, so its 17/17 was never in danger and never told anyone
anything about a real class. The set that hurts is the one written about the course, where a problem
set and its answer key restate every question verbatim.

**A control is a weaker instrument at class scale, and the old ones stopped being controls.** "Not in
this book" is not "not in this class": once the course's Fourier material is in the same class, the
textbook set's `control-fourier` scores *above* the median real question. The harness now records
the class size beside every run for this reason — a rank means nothing without the size of the
haystack.

**One question is beyond reranking, and it names the next real problem.** A student asking how a
homework problem is *worked* gets the problem statement plus eleven near-identical problems from
another week; the answer key is not in the top 128, so no reordering can reach it. That is the case a
different embedding model would have to earn its full re-index on, and it is now a reproducible case
rather than a hunch.

An earlier version of this paragraph said *two*, and the recorded runs beside it already disagreed.
`midterm-problem-1-impulse-response` sits at rank 35 in the embedding order and the cross-encoder
lifts it to **rank 2** — it is not out of reach, it is the single clearest thing reranking buys, and
counting it as unreachable understated the reranker while overstating the case for a new embedder.
The one genuinely unreachable question is `hw5-two-sided-exponential-answer`, which is absent from
128 neighbours in every run, reranked or not. Re-measured 6 August 2026; the recorded JSON from the
original run says the same.

### What these numbers can and cannot say

The question sets are sixteen and eleven to seventeen questions, and that size decides what they
are evidence for. The rerank gain, 9/16 to 12/16 at first place, is three questions; the
chunk-target sweep's "no trend" is a flat line drawn through eleven. Neither has the statistical
power to settle a rate comparison to within a question or two, and a future run that lands one
question higher or lower has not necessarily changed anything. What the sets are strong at is
finding cases: the answer key at rank 35 and the discrete-time table losing to the continuous-time
one are real, reproducible failures, and a rate this coarse still surfaces them reliably. So the
decisions recorded here — `RERANK_FETCH_K = 64` among them — are engineering bets made on the best
evidence available and recorded beside it, not optima these sets could prove. The way to overturn
one is another measurement, not a bigger question set taken on faith.

## What the open items became

### 1. `rag/locate.py` could not tell apart two problems with the same label — **closed**

`find_label` took a label's *first* occurrence on a page, so a sheet whose second section also
numbers from 1 gave both sections' "1." the same marker. On `homework_3` its twelve problems
produced nine distinct boxes.

`locate.find_labels` now searches a page for **all of its labels at once**, in document order, each
taking the first occurrence after the last one placed. Twelve problems, twelve positions, and each
one where the sheet actually writes it. A label that cannot be placed in order falls back to the
topmost hit, so a page this cannot walk is never worse off than before.

The solver resolves a page in one call rather than a problem at a time, which also means one PDF
open per page instead of one per problem. `backfill_problem_locations` does the same, one artifact
at a time — two solution sets over the same sheet must not be walked as one sequence, or the second
runs off the bottom of the page it shares with the first.

### 2. Figure-to-problem pairing on a crowded page — **closed**

The alternating-run rule is in, and it needed item 1 first, exactly as this document predicted.
Stated precisely: every figure must have a problem marker immediately beside it, all on the same
side, and no two may want the same marker; and the page must carry at least two figures, because
one diagram among several questions has a marker on both sides of it and only a repetition is
evidence of a layout. The full reasoning is in rag-pipeline.md.

What this document got wrong is the shape of the rule. "Strictly alternate **and be equal in
number**" does not fire on the acceptance page: it has three diagrams and seven questions. The
diagrams pair with the first three markers and the remaining four questions get nothing, which is
what the page means. Adjacency plus a consistent direction is the rule; equal counts was a
description of a simpler page than the one in the corpus.

Measured through the real solver against the real endpoint, on the real sheet: **three attachments,
none wrong**, against nought before and 21-of-which-12-wrong for the first attempt.

### 3. A dense reference table chunked as one chunk a page — **closed, and smaller than it looked**

Two real faults, both in the paragraph strategy and both fixed: a block bigger than the target was
never divided at all and survived to the ceiling, and two substantial blocks were packed into one
chunk. The eight-page appendix goes from 9 chunks to 11, and the unit-step question from **rank 3
to rank 1**.

The rest of the item did not survive contact. Two things this document implies were the cause are
measured and are not:

- Chunking finer does not help. The generic target was swept 750 → 200 and rank-1 hits wander
  between 2 and 5 with no trend; below 300 the top-4 rate gets worse. The shipped target is
  unchanged.
- The running head is not the cause. Detecting and stripping the line that repeats at the top of
  most pages changes rank-1 not at all and the mean rank not at all. A constant prefix shifts every
  chunk alike. It is not implemented, and the measurement is in rag-pipeline.md so nobody spends a
  day on it twice.

What remains is not a chunking problem. The questions that still rank badly are the discrete-time
tables losing to the continuous-time ones and vice versa — near-identical mathematics that a 137M
embedding model cannot tell apart. The lever is the embedding model or a reranker.

The question set is `scripts/eval_questions/fourier-tables.json`, eleven questions plus a control,
with the answers checked by eye against `data/eval-recognize/recognition.md`.

### 4. Font and weight heading detection — **still open, still deferred**

Deliberately deferred, not forgotten. Nothing measured needs it: a document with no outline is a
syllabus or a sheet, where the existing regex is doing an easier job well enough. It earns its
place when a book with no outline turns up.

### The verification gap — **closed, and it was hiding something**

The new screens are now correct at 1280, 768, and 375 in both themes, driven in the browser against
a copy of a real class. One real defect, and it was in the third of the three things this document
guessed at: **`FigureBlock` was blowing its image up**, not breaking at 375. The figure is a flex
column, so a stretched image takes the column's full width whatever its own is; at 1280 the
acceptance homework's 771px diagrams were rendering at 1215, a blurred picture of something drawn
in hairlines. `self-start` fixes it. `PageFailureNotice`'s caption row and the outline list's
indent-by-depth are both fine at 375, in both themes.

## The close-out pass

Before the phase closed, a review pass went over everything above with instructions to hold
nothing back, and it found a defect pattern the phase had been building without noticing:
**ambiguous failure converted into false success.** A truncated transcription stored as the page's
text. A half-embedded document marked `failed` whose committed chunks kept serving. A solve run
against a dead endpoint grinding through every unit's timeout and landing `ready` over zero
solutions, its progress counter reading "12 of 12". A mixed document whose requested recognition
was silently skipped, landing `ready` as if it had run. A mid-stream server death read back to the
student as a finished answer. An unrelated 400 cached forever as "this endpoint cannot do
constrained decoding". All of these are now loud: retrieval serves only `ready` documents and
failure paths take their chunks with them, the solver has recognition's consecutive-failure
breaker and an honest terminal state, truncation fails the page, skip reasons land on the row, and
the client distinguishes an endpoint that refused from an endpoint that broke.

The same pass closed the structural debts this document had been documenting instead of fixing:
`db_path` now derives from `data_dir` (first trap), the three llama-server modules share one
lifecycle in `llm/llama_server.py` and are all stopped at shutdown rather than orphaned, adoption
of an already-running server verifies what model it holds before trusting it, and boundary pages
are credited to the section whose heading they announce rather than the one that happens to be
nested deeper. A conservative parse-time gate now catches the photographed page whose text layer
is a URL, so it reads as scanned instead of indexing as noise.

Three things the pass deliberately did not do. The `TRANSCRIBE_PROMPT` re-measurement still waits
on a working endpoint — that debt stands exactly as recorded below. Lexical retrieval for the
verbatim answer-key case is recorded in the roadmap as the experiment to run *before* buying a new
embedding model, not attempted here. And the full page-selective vision gate — routing only the
pages whose text layer collapsed through the vision model, instead of the whole document or
nothing — is named in the roadmap as the architectural successor to the junk-page check; it wants
its own measured phase, not a close-out afternoon.

## The verification pass

6 August 2026. Everything the close-out changed was then driven for real — the measurements re-run
in process, the servers started and killed, the screens driven in a browser, the failure paths
walked end to end against endpoints chosen to fail. 794 backend tests and 360 frontend tests still
pass; ruff check and format are clean; the working tree stayed clean and `data/lyra.db` was never
opened, verified by its unchanged mtime and checksum.

**What held.** Every retrieval figure reproduced question for question (above). The close-out's
recognition breaker stops a run at exactly three consecutive page failures out of eight and blames
the endpoint, and a retry once the endpoint is removed replaces that message with the no-endpoint
one rather than leaving the stale blame in place. The solver's terminal state is honest in both of
its shapes: four problems against a dead endpoint trip the breaker at three and leave the fourth
unattempted, two problems fall through to "none of the problems could be solved", and both land
`failed` reading **0 of n**, never `ready` with a full bar. Deleting a document mid-recognition
abandons the run with one log line — `Ingestion of document 97 abandoned: deleted or replaced
mid-run` — and leaves no row, page, chunk, figure, or file behind; the same file re-uploaded
straight after ingests clean. On the screens, the ready-with-skip-reason row shows the backend's
reason and a `Try again`, the endpoint-blamed failure reads as an endpoint problem, and `FigureBlock`
renders its 771 px diagrams at 771 px in the 1215 px column that used to blow them up — all checked
at 1280 and 375, in both themes, with no console errors.

**The boundary fix is visible and cost nothing.** Re-ingesting the textbook moves 13 chunks to the
section their page's heading announces (105 distinct sections becomes 107) — page 110, for one, goes
from `Matrices / Matrix Arithmetic / More on Matrix Inverses` to `Matrices / LU Factorization`. Not
one of the 17 questions moved a rank. It only takes effect on re-ingest, because section metadata is
stamped onto chunk rows at chunk time.

**Two findings, both recorded rather than fixed.**

*An adopted llama-server is never reclaimed.* `LlamaServer.stop()` terminates `self._process`, which
is `None` for a server this process adopted rather than spawned, so the shutdown hook that reclaims
a spawned server is a no-op for an adopted one. Driven: a clean stop leaves **zero** survivors, a
`SIGKILL` orphans the rerank server, the next start adopts it without duplicating — and every clean
shutdown from then on leaves it running, indefinitely, holding its weights. This session opened with
field evidence of exactly that: an embedding server running since the previous day on a llama.cpp
build no longer on disk. Not fixed here because making adoption confer ownership means discovering
the holder of a port cross-platform, which is a design decision and not a verification-sized one.

*A refused reranker writes a report that says it reranked.* Staging a foreign llama-server on the
rerank port works exactly as designed — retrieval logs `Reranking is configured but did not start;
keeping the search order` once per question and returns ranks identical to the embedding order. But
the harness then writes `retrieval-…-reranked.json` with `"reranked": true` over embedding-order
numbers, and the only tells are the log and a per-question time of 0.08 s against the usual 2.1. The
silence is the product's contract and correct; recording an unverified claim is the harness's
problem, and `rag/rerank.py` already knows the answer to record.

## Traps

These cost real time. The first two are now removed at the source rather than only written down
here; their entries stay because the close-out pass that removed them is younger than the habits
this document built, and a reader of an older checkout needs to know which side of the fix they
are on.

**`LYRA_DATA_DIR` used to leave the database behind — fixed.** `settings.db_path` now derives
from `data_dir` unless `LYRA_DB_PATH` points it somewhere explicitly, so setting `LYRA_DATA_DIR`
alone moves everything, database included. The trap that motivated the fix: the two fields were
independent, and a verification run that believed otherwise wrote 596 chunks and 28 profile facts
into the real database. `LYRA_DB_PATH` still wins when set, so a run that sets both behaves
exactly as it always did.

**Frontend tests could reach a live backend — fenced.** `API_BASE` defaults to
`http://127.0.0.1:8000`, and jsdom will really fetch it; the vision tests passed only while
nothing was listening on port 8000. `frontend/tests/setup.ts` now installs a throwing `fetch`, so
a test that forgets to stub at `api` fails loudly instead of rotting silently the day a dev server
is up. Seeding a React Query cache is still not stubbing: the query refetches on mount.

**A llama-server the backend adopted survives every clean shutdown after it.** The shutdown hook in
`main.py` reclaims what the process spawned; `stop()` is a no-op on a server it adopted, because
adoption never gave it a `Popen` to terminate. So one `SIGKILL` — or one evaluation run, since the
harness spawns servers and never stops them — leaves a server that no later clean stop will clear.
Verified 6 August 2026, and the same day turned up an embedding server thirteen hours old running a
llama.cpp build that had already been deleted. If `ps aux | grep llama-server` disagrees with what
you think you started, this is why; `pkill -f llama-server` before a lifecycle measurement.

**A stale llama.cpp shadows a new one.** Everything finds the binary with
`sorted(llama_dir.rglob("llama-server"))` and takes the first hit, so `llama-b10235` sorts ahead of
`llama-b10287` and downloading a new pin changes nothing. `fetch_models.py` now removes the build
it replaces and asks `--version` rather than trusting a directory name. Do not reintroduce a second
extracted build.

**PyMuPDF letterboxes an inserted image.** `page.insert_image(rect, pixmap=...)` preserves the
pixmap's aspect ratio inside the rect, so a test fixture built to be 252x21 silently becomes
31x21 and falls under the figure size floor. Pass `keep_proportion=False` in fixtures.

**`llama-server` suppresses special tokens by default.** For Unlimited-OCR that silently destroys
the output: `<|det|>` markers vanish and table cells fuse. `--special` is required and is
server-only; `llama-mtmd-cli` rejects the flag and prints them anyway.

## What is on disk that need not be

The OCR weights, 2.8 GB, in `data/models/`: `unlimited-ocr-Q4_K_M.gguf` and
`mmproj-unlimited-ocr-bf16.gguf`. Nothing in the product reads them, because the specialist path is
disconnected. Delete both to reclaim the space; `fetch_models.py --ocr` puts them back.

The reranker weights, 640 MB, `bge-reranker-v2-m3-Q8_0.gguf`, are a different case: the product
*does* read them, and deleting them changes retrieval rather than only reclaiming disk. Retrieval
falls back to the embedding order, which still works — see the table above for what it costs.

## Re-running every measurement

The harness works in its own workspace with its own database and never touches the student's data.

Every `retrieve` below states its fetch width outright. The numbers in this document were taken at
`--k 64`, the width the product reranks over (`RERANK_FETCH_K` in `rag/retrieve.py`), and the
harness defaults to that same constant — but a run that is going to be compared against a recorded
table should say its width rather than lean on a default to match it. An earlier version of this
document leaned, while the default was 32, and following it re-measured something narrower than the
table it sat beside.

```bash
python scripts/eval_ingest.py --workspace data/eval-ingest ingest \
  /path/to/Kuttler-LinearAlgebra-AFirstCourse-2017A.pdf --fresh
python scripts/eval_ingest.py --workspace data/eval-ingest retrieve --k 64
python scripts/eval_ingest.py --workspace data/eval-ingest transcribe
python scripts/eval_ingest.py --workspace data/eval-ingest report
```

Recognition end to end on the scanned handout, timed per page:

```bash
python scripts/eval_ingest.py --workspace data/eval-recognize recognize \
  data/eval/uploads/1/23-Fourier_Tables.pdf --no-extraction
```

Retrieval over that handout once it has been read, which is how a chunking change is measured
against it. `reindex` re-chunks and re-embeds a document already in a workspace, exactly as the
app's Reindex action does, so this costs half a second rather than the 110 s the vision model spent
reading it — a transcription lives on its page row and outlives a re-parse, and this is also the
check that it really does:

```bash
python scripts/eval_ingest.py --workspace data/eval-recognize reindex Fourier_Tables --no-extraction
python scripts/eval_ingest.py --workspace data/eval-recognize retrieve \
  --questions scripts/eval_questions/fourier-tables.json --k 64
python scripts/eval_ingest.py --workspace data/eval-recognize report
```

A question set says where its answer is either by naming an outline section, as the textbook set
does, or by giving `expect_pages` outright. The second is not a lesser form of the first: a scan
has no outline to name, and the pages of an eight-page appendix are as checkable by eye as an
outline entry.

**Retrieval at class scale, which is the run that matters and the one that was missing.** Build a
workspace holding the whole course and ask a set that names the document each answer is in:

```bash
python scripts/eval_ingest.py --workspace data/eval-class ingest --fresh --no-extraction \
  /path/to/course/*.pdf
python scripts/eval_ingest.py --workspace data/eval-class reindex Fourier_Tables --recognize
python scripts/eval_ingest.py --workspace data/eval-class retrieve \
  --questions scripts/eval_questions/ece203-class.json --k 64
python scripts/eval_ingest.py --workspace data/eval-class retrieve \
  --questions scripts/eval_questions/ece203-class.json --k 64 --rerank
python scripts/eval_ingest.py --workspace data/eval-class report
```

The pair of runs is the measurement, so each writes its own report and neither overwrites the other.
`--rerank` is set explicitly in both directions rather than left to whether the weights happen to be
on the machine: that is a fact about the machine, not about the product.

**`--rerank` asks for reranking; it does not prove reranking happened.** The reranker degrades
silently by design, so a run whose server would not start writes `"reranked": true` over
embedding-order numbers and looks like a measurement. Read the per-question time before believing
the ranks: reranking costs seconds a question (2.1 s over this class on the machine these numbers
were taken on) and the embedding order costs hundredths. A reranked report that came back in 0.08 s
a question reranked nothing, and `Reranking is configured but did not start` will be in the log
above it.

`reindex --recognize` reads a scan that is already sitting in a class, which is what keeps it one
document rather than two — uploading it again through `recognize` would spend the vision model's
minutes a second time and put a duplicate in the class being measured.

Strip the corpus filenames of any `NN-` prefix before ingesting. `detect_doc_type` reads the
filename first, and the workspace's own `uploads/` names carry a document-id prefix.

The OCR serving spike, which needs `fetch_models.py --ocr` first:

```bash
python scripts/ocr_spike.py
```

Both `recognize` and `transcribe` call the configured tutor endpoint, so they need one that can
see images. `POST /api/settings/test-vision` says whether it can.

**One measurement is outstanding for exactly this reason.** `TRANSCRIBE_PROMPT` was changed to pin
one heading form and one table form, after the recorded acceptance transcription was read back with
the notation counter and found to have written its tables three ways across eight pages, with three
more pages marking up no table at all. The re-run that would measure the change has not happened:
the development endpoint has been answering `model name=Qwen3.6-27b failed to load` to every
request, text or image, since the change was written. When it is back:

```bash
python scripts/eval_ingest.py --workspace data/eval-recognize recognize \
  data/eval-class/uploads/1/24-Fourier_Tables.pdf --no-extraction
```

`recognition.md` now opens with the line that answers it. The baseline to beat, from the recorded
run, is: three notations, three of eight pages with no table markup at all, ten headings the chunker
can see, three written in bold instead.

**The gate is narrower than "the endpoint is down", and knowing which narrows what to wait for.**
Checked 6 August 2026: the endpoint is up and serving — `test-connection` answers
`Connected. 9 models available.` What will not load is the *configured model*. `Qwen3.6-27b` and
`Qwen3.6-27B-Heretic` both still answer `failed to load` to a one-token request, so `test-vision`
fails; `Gemma4-12B` is loaded, vision-capable, and passes `test-vision` outright. So a vision model
is available and this measurement still did not run, deliberately: the baseline above was taken on
Qwen, and re-running the new prompt on Gemma would vary the prompt and the model at once and answer
neither question. What it needs is Qwen back, or a fresh two-run baseline on one model — old prompt
and new — which is a different and larger piece of work than the one this line has been waiting for.
The class-scale `reindex Fourier_Tables --recognize` is gated the same way, and the class numbers
above were measured without the handout's transcription, exactly as the recorded run was.

## What a reader should know before changing any of it

**Recognition is opt-in per document, and that is load-bearing.** It is minutes of model time and,
against a configured remote endpoint, page images of the student's own material leaving the
machine. No migration sweeps existing `unsupported` documents into a queue. If you make it
automatic, you have changed the product's posture, not just its defaults.

**A transcription is stored on its page row and outlives a re-parse.** Re-indexing must never
re-run recognition. `recognition.sync_pages` is where that is enforced.

**A retrieval number measured on one document is not a retrieval number.** `retrieve` searches a
class. Any run whose workspace holds a single document has measured a search with nothing to
compete, and this document shipped four such numbers before anyone checked. Every question set now
names the document its answer is in, and the report records how many documents and chunks were in the
class, so a rank always comes with the size of the haystack it was drawn from.

**The reranker is optional and every failure is silent by design.** Absent weights, a server that
will not start, a timeout, a malformed reply — all of them return None from `rag/rerank.py` and
retrieval keeps the embedding order. That is correct: reranking improves an ordering that already
works. It also means a broken reranker looks exactly like no reranker, so if a measurement stops
reproducing, check `rerank_server.available` and the warning log before suspecting the model. The
close-out pass tightened the lifecycle around that silence without changing it: the server is
warmed at startup so the first chat turn does not pay the model load, stopped at shutdown so it no
longer outlives the app, refused when the port is answered by a server holding some other model,
and a failed start is remembered for five minutes instead of being re-paid on every retrieval.

**Recognition is not a fifth ingestion state.** It runs inside `parsing` with
`stage_detail = 'recognizing'`. That is what the page counter reads, and it is why
`reconcile_interrupted` already handles a document caught mid-transcription.

**The evaluation harness is the standard of evidence here.** Every number in this document came
from the real code path in process, not from a reimplementation. Phase 2 set that rule and Phase 3
found four faults with it that no test suite would have caught. Keep it.

**A negative measurement is worth as much as a positive one, and is easier to lose.** Two of the
things closing item 3 was expected to need — a smaller chunk target, and stripping the repeated
running head — were built, measured, found to do nothing, and thrown away. Both are recorded in
rag-pipeline.md with their numbers, because the next reader will have the same two ideas and
should not spend the day finding out again.

**The interface was verified against a copy of a real class, not against the student's own.** A
scratch database and a scratch data directory, with `LYRA_DATA_DIR` *and* `LYRA_DB_PATH` both set —
see the first trap — states edited into the copy for the rows a real class does not happen to have,
and the segmentation that produced the figure attachments run against the real endpoint in that
copy. Nothing was written to `data/lyra.db`. Do it that way again: the states worth checking are
exactly the ones a healthy class never has.
