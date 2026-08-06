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

**Read the four open items and the traps before touching anything.** Two of the four did not exist
when the phase was planned; they were found by building it. Three of the traps are things that
fail silently, and one of them wrote to a database it was not supposed to.

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

Step 5 was not planned. It was added after step 4 because steps 2 through 4 had shipped
capabilities the interface had no way to reach, and the `unsupported` popover was still telling
students that scans would be readable "in a future update" and would "process automatically then".

604 backend tests, 357 frontend.

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

Two independent documents at different densities landing within 4% of each other (13.4 and 13.8) is
what makes the rate the model's rather than the page's, and what makes "2.3 hours for a 608-page
book" worth quoting.

## The four open items

### 1. `rag/locate.py` cannot tell apart two problems with the same label

**Start here.** It is the only one that fixes an existing fault as well as unblocking new work.

`locate.find_label` takes a label's *first* occurrence on a page. A sheet whose second section also
numbers from 1 therefore resolves both sections' "1." to the first section's marker. Visible right
now in `homework_3`: its twelve problems produce only four distinct bounding boxes.

Two consequences. The solver's source pane draws its highlight band at the wrong marker, which is a
Phase 2 fault that has always been there. And it blocks item 2 below.

Reproduce by segmenting `homework_3.pdf` and reading `artifact_provenance.bbox` per problem.

### 2. Figure-to-problem pairing on a crowded page

Figures extract, crop, serve, render, and print. What does not happen is filing an uncaptioned
figure under a problem when several problems share its page. `core/solver.py::_write_figures`
attaches only on an exact rule: the problem names the figure, or the problem is alone on its page.

The reasoning is recorded in rag-pipeline.md under "Which problem a figure belongs to", and the
short version is that every geometric rule tried is wrong on one of the two common layouts. On
`homework_3` the list markers sit *below* their diagrams. Attaching by page was tried first and
produced 21 attachments of which 12 were wrong.

**The rule that would work** is pairing an alternating run of figures and markers, which is a
structural check rather than a distance guess: if a page's figures and problem markers strictly
alternate and are equal in number, the pairing is forced by whichever comes first. It needs each
problem to have a distinct position, which is item 1.

### 3. A dense reference table chunks as one chunk a page

Found by the recognition acceptance run. The eight-page Fourier appendix comes back correct and
searchable, but every page opens with the same running head and carries no headings to cut on, so
each page is one chunk and their embeddings sit close together. Asked for the transform of the unit
step, the right page ranks **4th of 8**.

A chunking fault, unrelated to whether the page was scanned. `rag/chunk.py` is where it lives.

### 4. Font and weight heading detection

Deliberately deferred, not forgotten. Nothing measured needs it: a document with no outline is a
syllabus or a sheet, where the existing regex is doing an easier job well enough. It earns its
place when a book with no outline turns up.

### And one verification gap, which is not on the roadmap

ui-phase-3.md's definition of done requires the new screens be correct at **1280, 768, and 375, in
both themes**. They were verified at 1280 in dark only. `PageFailureNotice`'s caption row, the
outline list's indent-by-depth, and `FigureBlock`'s image sizing are all layout that could break at
375, and none of them has been looked at in light theme. This is short and should be done first,
because it is the only place where something may be visibly broken right now.

## Traps

These cost real time. Three of them fail silently.

**`LYRA_DATA_DIR` does not move the database.** `settings.db_path` is an independent field
defaulting to `data/lyra.db`, not derived from `data_dir`. Setting `LYRA_DATA_DIR` relocates
uploads, pages, text, and models and leaves the database exactly where it was. A verification run
that believed otherwise wrote 596 chunks and 28 profile facts into the real database. Set
**both** `LYRA_DATA_DIR` and `LYRA_DB_PATH`, and check `/api/classes` before trusting it.

**Frontend tests can reach a live backend.** `API_BASE` defaults to `http://127.0.0.1:8000`, and
jsdom will really fetch it. Seeding a React Query cache is not enough, because the query still
refetches on mount and the live answer replaces the seed. The vision tests passed only while
nothing was listening on port 8000. Stub at `api`, not at the cache.

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

## Re-running every measurement

The harness works in its own workspace with its own database and never touches the student's data.

```bash
python scripts/eval_ingest.py --workspace data/eval-ingest ingest \
  /path/to/Kuttler-LinearAlgebra-AFirstCourse-2017A.pdf --fresh
python scripts/eval_ingest.py --workspace data/eval-ingest retrieve
python scripts/eval_ingest.py --workspace data/eval-ingest transcribe
python scripts/eval_ingest.py --workspace data/eval-ingest report
```

Recognition end to end on the scanned handout, timed per page:

```bash
python scripts/eval_ingest.py --workspace data/eval-recognize recognize \
  data/eval/uploads/1/23-Fourier_Tables.pdf --no-extraction
```

The OCR serving spike, which needs `fetch_models.py --ocr` first:

```bash
python scripts/ocr_spike.py
```

Both `recognize` and `transcribe` call the configured tutor endpoint, so they need one that can
see images. `POST /api/settings/test-vision` says whether it can.

## What a reader should know before changing any of it

**Recognition is opt-in per document, and that is load-bearing.** It is minutes of model time and,
against a configured remote endpoint, page images of the student's own material leaving the
machine. No migration sweeps existing `unsupported` documents into a queue. If you make it
automatic, you have changed the product's posture, not just its defaults.

**A transcription is stored on its page row and outlives a re-parse.** Re-indexing must never
re-run recognition. `recognition.sync_pages` is where that is enforced.

**Recognition is not a fifth ingestion state.** It runs inside `parsing` with
`stage_detail = 'recognizing'`. That is what the page counter reads, and it is why
`reconcile_interrupted` already handles a document caught mid-transcription.

**The evaluation harness is the standard of evidence here.** Every number in this document came
from the real code path in process, not from a reimplementation. Phase 2 set that rule and Phase 3
found four faults with it that no test suite would have caught. Keep it.
