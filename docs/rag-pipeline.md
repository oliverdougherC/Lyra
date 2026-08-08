# RAG Pipeline Specification

## Overview

The RAG pipeline transforms raw course materials into retrievable, class-scoped knowledge. It is the
core differentiator of Lyra.

Three models are involved, with strictly separate roles:

| Role | Model | Runtime | Phase | Configurable |
|------|-------|---------|-------|--------------|
| Embedding | `nomic-embed-text-v1.5` (GGUF) | llama.cpp, local | 1 | No |
| Tutor | user's choice, then bundled | OpenAI-compatible endpoint | 1, bundled in 6 | Until Phase 6 |
| Text recognition | the tutor model today, then `baidu/Unlimited-OCR` | endpoint now, llama.cpp later | 3 | No |

Embedding is infrastructure and always runs locally. The tutor model is the product.

Text recognition is the one role whose runtime is *transitional*, and the table says so rather than
claiming the destination. It is meant to be local infrastructure, and it will be once inference is
bundled in Phase 6. Until then there is no bundled vision model to route pages to, so recognition
uses the endpoint the student configured, which is why it is opt-in per document and why it refuses
an unacknowledged remote endpoint before the image is encoded. See the Inference Posture section of
[architecture.md](architecture.md) for the rules governing this period.

**Text recognition is built through the general path.** The specialist runtime specification below
is retained in full because the research behind it is load-bearing and was expensive to establish,
and because the specialist is the same interface with a different runtime rather than a rewrite.

What changed: a vision-capable tutor model is now assumed, and it can transcribe pages itself.
Bulk transcription therefore sits behind an interface with two implementations, and the choice
between them is a measurement rather than a prerequisite:

| Path | Cost | When it wins |
|------|------|--------------|
| Bundled general vision model | No extra download, slower per page, weaker on dense layout and math | Homework and short scans, most of what users upload |
| `Unlimited-OCR` specialist | Extra weights to ship and manage, far faster per page, better on multi-column text, tables, and math | Textbook-scale bulk ingestion, where throughput decides whether the feature is viable |

That plan was: build the interface, wire the general model first, then time a real sample and
extrapolate. It has run. The general path reads a scanned document end to end at **13.8 seconds a
page**, measured in Stage 2b, so the specialist is now justified by a number rather than by a
worry, and the serving spike stayed off the critical path.

**Text recognition is not only for scanned pages, and this is now the strongest argument for it.**
PyMuPDF's text layer mangles dense mathematics. Phase 2 already paid for this once, at the
segmentation gate, where `e^{-2t}u(t-3)` extracts as `e−2tu(t −3)` and the student was being asked
to check a reading of their homework against text their sheet does not contain.

On the reference textbook in Stage 2a it is far worse than garbled: it is lossy. Every matrix
extracts as a column of loose digits with its shape discarded, so the identity matrix and a row swap
both arrive as `1 0 0 1` down four lines, and nothing downstream can tell them apart or tell either
from a list of numbers. In a linear algebra textbook that is most of the content, on pages that were
never scanned and that Lyra currently reports as ingested successfully.

So a vision pass is not a scanned-document feature that might also help elsewhere. On mathematical
material it is a **quality** feature for every document, and the phase treats it as one: measuring
vision against the text layer on pages that already have one comes before bulk transcription of
pages that do not. See the build order in [feature-roadmap.md](feature-roadmap.md).

**Measured, and it holds.** Six pages of the reference book read through Qwen3.6 27B against the
same pages' text layer, timed by `scripts/eval_ingest.py transcribe`. Five were chosen because the
text layer is known to fail on them; page 13 is a page of ordinary prose, included as a control so
a transcription that flattered itself everywhere would be visible.

| Page | Text layer | Transcription | Matrices recovered | Seconds |
|------|-----------|---------------|--------------------|---------|
| 13 (control) | 1 lone-number line | 1 | 0 | 6.1 |
| 90 | 36 | 0 | 8 | 12.3 |
| 111 | 25 | 0 | 6 | 11.2 |
| 158 | 1 | 0 | 2 | 20.2 |
| 194 | 35 | 0 | 9 | 16.8 |
| 245 | 20 | 0 | 9 | 13.7 |

A lone-number line is what a matrix collapses into when the text layer flattens it, so the count is
the failure's own shape. Every one of them is recovered as `\begin{bmatrix}`.

Checked by hand rather than by counting, which is the standard Phase 2 set. On page 90 the text
layer gives `1 0 1 1 −1 1 1 1 −1` down nine lines and the transcription gives the same nine entries
as a 3x3 matrix in the right order. The inverse on the same page is worse in the text layer and the
transcription still recovers it: `0 1 2 1 2 1 −1 0 1 −1 2 −1 2` is thirteen tokens for nine entries,
because each half of a fraction lands on its own line, and the transcription reads it back as a
matrix carrying `\frac{1}{2}` in the right three places. That ambiguity is the point. Nothing
downstream could have recovered it, because `1` then `2` is either two entries or one fraction and
the text layer does not say which.

The control is the strongest evidence that this is transcription rather than generation: 1217
characters against 1218, and it **reproduces the book's own typo**, `contrinuity`. A model
paraphrasing would have corrected it. No page came back shorter than its text layer, and the densest
page ends on the same half-finished sentence the page itself ends on.

**13.4 seconds a page is the number that decides the specialist path.** A scanned homework sheet is
under a minute and a lab handout a few minutes, which is the size students actually scan. A 608-page
textbook is 2.3 hours, which is not a thing to do by default. That is the case for `Unlimited-OCR`
stated as a measurement rather than an assumption, and it is also why transcription is opt-in per
document rather than something ingestion decides on the student's behalf.

## Pipeline Stages

```
Upload -> Parse -> Chunk -> Embed -> Store -> Retrieve -> Generate
```

Stages 2 through 5 run in a background ingestion job. Upload returns immediately.

### Stage 1: Upload

**Currently accepted formats:**
- PDF, text-based or scanned
- Plain text (TXT, MD)
- Images (PNG, JPG)

**Later:** Office documents (DOCX, PPTX).

**WebP is not accepted, and that is a measurement rather than an omission.** It was listed here and
in ui-phase-3.md alongside PNG and JPG. PyMuPDF 1.28.0 refuses to open a real `cwebp` file outright,
so accepting one would mean carrying a second image dependency for a format a student's scan is very
unlikely to be in. The dropzone names what actually works.

An uploaded image needs no parse path of its own. PyMuPDF opens a PNG or a JPG as a one-page
document whose page has no text, which is exactly the shape of a scanned page, so an image arrives
at recognition the same way page 7 of a PDF does.

A file whose extension is accepted but whose content cannot be read as text today is not an
error in the usual sense. It is a document Lyra will support later, so it terminates in the
`unsupported` state described in Stage 2 and the original file is kept.

**Input:** File bytes plus metadata (filename, timestamp, class ID)
**Output:** File in `data/uploads/`, a `documents` row in state `pending`, and an ingestion job

Upload responds `202`. It never blocks on parsing.

### Stage 2: Parse

**Text-based PDFs**
- PyMuPDF direct text extraction
- Preserve page numbers and section structure

**Plain text and Markdown**
- Read directly

**Scanned-page detection.** A page counts as scanned when its extractable text is below a threshold
of 20 non-whitespace characters. Detection shipped in Phase 1 even though recognition did not,
because the alternative is worse than an error: a scanned PDF would otherwise ingest "successfully"
as an empty document, embed nothing, and then quietly fail to answer any question about it.

Outcome by document composition:

| Composition | Result |
|-------------|--------|
| All pages have extractable text | `ready`, fully searchable |
| Some pages scanned | `ready`, with `pages_skipped` recorded and surfaced in the UI |
| All pages scanned | `unsupported`, terminal, file retained |

`unsupported` is not `failed`. It carries a distinct message telling the user the document needs
text recognition and that the file has been kept, and the file being kept is what makes recognition
able to run over it in place rather than asking for the upload again.

**A character count is not enough on its own.** A page photographed with a phone and printed to PDF
from a mail client arrives carrying a text layer — a timestamp, the image's filename, the URL of the
message it was attached to — which clears twenty characters comfortably while containing nothing
about the page. `_is_photographed_page` is the second gate: a page whose text has fewer than fifteen
alphabetic characters once URLs are stripped, and whose images cover nearly the whole page, is
dropped exactly as a blank scan is. Junk that indexes is worse than nothing, because it embeds and
then competes.

Measured on the real course on 6 August 2026: `laplace.pdf` is two Gmail-printed photographs whose
entire text layer is `3/12/26, 2:14 PM / IMG_8887.jpg / https://mail.google.com/... / 1/1`. Before
the gate it ingested `ready` and contributed two chunks of that to the class; after it, the document
lands `unsupported` and can be read by recognition like any other scan. One true positive, no false
ones over the other 35 documents in the class.

**This table is unchanged by recognition, and that is deliberate.** Recognition is opt-in per
document, so a scanned upload still lands `unsupported` and a mixed one still lands `ready` with
pages skipped. What changes is that both now have a way out: `POST /api/documents/{id}/recognize`.
The reasoning is in Stage 2b, and the short version is that transcription is minutes of model time
and, against a configured remote endpoint, page images of the student's own material leaving the
machine. A capability arriving is not consent to use it on everything already on disk.

### Stage 2a: Structural Parsing (Phase 3)

Retrieval quality over a 900-page textbook is a different problem from reading a scanned page, and
it is easy to conflate the two. This one exists **today**, for text-based PDFs, and has no external
dependencies. It is the more valuable of the two and comes first.

Flat semantic chunking over a textbook produces thousands of chunks with no structural awareness.
Retrieving eight of them for "explain convolution" is a much worse experience than the same
retrieval over a syllabus, and no amount of embedding quality fixes it, because the information the
query needs is hierarchical and the index is flat.

#### The reference book, and what it measured

This stage was specified against a real book rather than against an estimate, in the same way
Phase 2 was specified against a real course. The reference is Kuttler, *Linear Algebra: A First
Course* (2017A): 608 pages, 3.3 MB, a PDF outline of 131 entries nested four levels deep. Every
number below was produced by running the shipping code over it.

| Measurement | Result |
| --- | --- |
| Parse, 608 pages | 0.8 s, 596 pages kept, 12 detected as scanned |
| Extracted text | 855,045 characters |
| `detect_doc_type` | `homework` |
| Chunked as detected | 1312 chunks, mean 162 tokens, **0 carrying a `section_title`** |
| Chunked as `textbook` | 596 chunks, 595 carrying a `section_title`, most of them wrong |
| Embedding | 25 ms per chunk, so 15 s to 33 s for the whole book |

Three of those rows are faults. The last one is the good news, and it is the row that reorders the
phase.

#### A textbook is chunked as homework today

`detect_doc_type` has no rule for `textbook` at all. The strategy table below defines one, and
`CHUNK_RULES` implements one, but nothing reaches it: the filename patterns do not include a
textbook case, so classification falls through to the content heuristic, and a book full of
numbered exercises and numbered theorems trips `PROBLEM_MARKER` thousands of times. The reference
book classifies as `homework` and is cut at every numbered line in it.

The result is not "flat chunking that retrieves poorly", which is what the roadmap describes. It is
1312 fragments averaging 162 tokens, carrying no structural metadata whatsoever, with problem
boundaries drawn through the middle of proofs. A textbook detection rule is therefore the first
change in this stage and the highest value per line in the phase.

Detection must not lean on the filename. A student saves a book under whatever the publisher called
it, and the reference book's name says nothing. The signals that do separate a textbook from a
problem set are structural: a PDF outline with real depth, a page count in the hundreds, and a body
whose numbered markers are spread evenly through it rather than concentrated on two pages.

#### Section hierarchy

- Primary source is the PDF outline via PyMuPDF `get_toc()`, which most commercial textbooks carry
- Fallback is the line-level heading regex over the flattened text, the same `SECTION_HEADING` the
  chunker has always used. A detector reading span-level font size and weight — which PyMuPDF
  exposes and nothing here reads — is the deliberately deferred open item, recorded in
  feature-roadmap.md; it earns its place when a book with no outline turns up
- Result is a hierarchical `section_path` on each chunk, replacing the current flat `section_title`

`section_path` is the change that matters. A homework problem reading "use the diagram from section
5.2.1" becomes a **direct lookup** rather than a semantic search that may or may not surface the
right page. That is the difference between reliable and lucky, and it is what makes the Phase 2
solver able to follow a textbook's own cross-references.

**The path is titles, not numbers.** An earlier draft of this document wrote `section_path` as
`5 / 5.2 / 5.2.1` and assumed the outline carries section numbers. The reference book's outline does
not: its entries read `Systems of Equations`, `Systems Of Equations, Algebraic Procedures`,
`Gaussian Elimination`, with the hierarchy carried by nesting depth and the numbers appearing only
in the page text. So the path is built from outline titles, and a section number is recovered where
the page text under that outline entry supplies one. Designing around numbers that are frequently
absent would have produced a field that is null on exactly the books this stage exists for.

**What placement actually does, which is the modest version on purpose.** A chunk is addressed by
the deepest section covering its page, and chunk *boundaries* are unchanged: they still come from
the heading regex and the packing rule. The measured failure was that a book's sections were
unaddressable, not that its chunks were cut in the wrong places, and rewriting the boundaries at the
same time would have made the measurement unreadable. A page holding the end of one section and the
start of the next is credited to the later one, because that is the section its heading announces.

**The current heading regex cannot be promoted to do this.** `SECTION_HEADING` in `rag/chunk.py`
matches whole lines out of already-flattened text, which is all it can do, and forced over the
reference book it returns a `section_title` on 595 of 596 chunks. That coverage is worthless: among
the titles it finds are `3 times the second row to the first row.`, `Sn`, `I1`, `0 . However, it is
written as V/V0. This is called the Mach number`, and table-of-contents lines complete with their
dot leaders. High coverage of wrong values is worse than no coverage, because a wrong
`section_path` is a lookup that confidently returns the wrong section. The regex stays as the
fallback for documents with no outline, where it is doing a different and easier job, and it is
never the primary source for a book that has one.

#### Figures

Embedded images via `get_images()`, filtered, cropped out of the composed page, and stored in
`document_figures`. Figures are the first pipeline output that is not text; `artifact_parts` has
accepted `kind = 'figure'` with `content_type = 'image'` since Phase 2 with nothing producing one.

Geometry follows the convention `rag/locate.py` and `artifact_provenance.bbox` already established:
fractions of the page box rather than points, because pages render as images at whatever width the
pane has.

**Drawn page regions are not extracted, and that is measured rather than deferred by preference.**
The plan was to clip a region for figures drawn as vector paths instead of embedded as a bitmap,
which PyMuPDF's `cluster_drawings()` exists for. Over the reference course it does not work. On the
112-page lecture deck it reduces 2522 paths to 112 clusters and **every one of those clusters is the
whole page**, because each page carries a full-bleed background rectangle that swallows the rest
into one region. The same is true of the lecture notes and the lab handouts, at three different page
sizes. Shipping it would file one junk figure per page of every deck a student owns. The door is
open; what is closed is a heuristic that was wrong on everything it was tested against.

**The filters come from the corpus.** 69 embedded image placements across the reference course:

| | |
|---|---|
| Smallest real figure | 252 x 21 pt, a block diagram, 1.1% of its page |
| Largest real figure | 53% of its page |
| Scanned pages | 100.2% of the page, every one |
| Decorative images | none at all |

So a figure is anything over 2000 pt² with no side under 12 pt, and under 90% of the page. A
minimum *height* would have been the obvious filter and it discards the acceptance case: the three
diagrams on the reference homework are 21 points tall. The gap between 53% and 100.2% is what lets
the page-coverage cut be a constant rather than a judgment.

**Captions are the rare case, not the primary mechanism.** The specification had caption-to-figure
association as the way figures get named. Across 69 figures the corpus contains **five** captions,
all in one document. All five are found: each starts within a point of the image it names and
overlaps it horizontally, so the rule is a caption pattern plus a 24-point gap below.

A figure with no caption is named for where it was found - `Page 1, figure 2` - and given no owner.
Calling it `Figure 2` would invent a number the document does not use and that its own text may
already mean something else by.

#### Which problem a figure belongs to

This is decided when a solution set is written, not when the figure is extracted, and only by rules
that are exact. A figure is attached to a problem when **the problem's statement names it** ("the
system in Figure 3"), when **the problem is the only one on its page**, or when **the page's
diagrams and problem markers alternate**. Otherwise nothing is attached.

That is deliberately less than the roadmap asked for, and the reason is a measurement. Attaching
every figure on a page to every problem on it was the first implementation, and on the acceptance
homework it produced twenty-one attachments of which **twelve were wrong**: four Fourier-series
problems each received three block diagrams belonging to other questions.

Geometry does not rescue it. On that page the numbered list markers sit *below* their diagrams, so:

| Rule | Result on the acceptance page |
|---|---|
| Nearest preceding marker | Off by one on every figure |
| Nearest marker by distance | Figure 2 wrong, by three thousandths of a page |
| First marker below the figure | Correct - and wrong on the opposite, equally common layout |
| Pair an alternating run of figures and markers | **Exact. Now the third rule** |

The last one is the one that survived, and it took fixing `locate` first. It reads no gap and no
threshold: every figure must have a problem marker immediately beside it, all of them on the same
side, and no two may want the same marker. *Immediately beside* is what makes it structural rather
than a distance guess - the question is whether anything sits between a diagram and a marker, not
how far apart they are. *All on the same side* is what settles the layout: if both readings work
the page has not said which it is and gets neither, which is how the two opposite conventions are
told apart without hard-coding either. A page must carry **at least two figures** for the rule to
apply at all, because one diagram among several questions has a marker on both sides of it - on a
page of questions everything has a marker beside it - and only a repetition is evidence of a
layout. Markers left over once the figures run out are expected and are what the real page looks
like.

On the acceptance homework that gives **three attachments and no wrong ones**: the three block
diagrams at the top of page 1 pair with the first three of its seven questions, and the four below
them get nothing. Verified through the real solver against the real endpoint, not against a
fixture.

What still gets nothing is a crowded uncaptioned page that does not alternate - two diagrams in a
row, or a count that breaks the run. There the figures are extracted, served, rendered, and
printed, and the source pane shows them on the page beside the solution, but Lyra does not claim to
know which question each one answers.

#### Finding a problem on its page

`rag/locate.py` searches a page for **all of its problem labels at once**, in document order, each
taking the first occurrence of its marker that sits after the last label placed. Resolving them one
at a time cannot work and the failure is not exotic: a sheet whose second section numbers from 1
again writes `1.` twice, so both sections' first problem took the first `1.` on the page. On the
acceptance homework that collapsed twelve problems onto nine positions; resolved a page at a time
they are twelve.

Two things depended on it. The solver's source pane drew its highlight band at the wrong marker,
which was a Phase 2 fault present since the band existed. And the alternation rule above needs each
problem to have a position of its own, which is why it could not be built until this was.

A label whose marker appears only above the cursor falls back to the topmost hit and leaves the
cursor where it was, so a page this cannot walk in order is never worse off than the rule it
replaced.

#### Scale, measured

The open question this section used to record was local embedding throughput during ingestion. It
is answered, and it is not a problem.

Embedding the reference book runs at 25 ms per chunk, so the whole 608-page book indexes in 15 s at
596 chunks or 33 s at 1312. Parsing is 0.8 s and chunking is below the resolution of the timer.
Ingestion cost at textbook scale is therefore almost entirely embedding, and embedding is fast
enough that no batching, parallelism, or streaming work is justified by this measurement.

Stated honestly, because it is one measurement and not a benchmark: this was Apple Silicon, against
an embedding server that was already warm, over 64 chunks of one book. What it rules out is an order
of magnitude, not a factor of two. A cold start pays the server's own load time once, which
`embed_server.py` already absorbs on the first call of any ingestion.

Two costs at this scale are still unmeasured and are Step 1 work rather than assumptions:
`extract_facts` runs a model call per document and is the one stage that does not scale with chunk
count, and retrieval quality over 596 chunks has never been compared against retrieval quality over
127. The first is a number to record; the second is the whole point of the stage.

### Stage 2b: Text Recognition (Phase 3)

The general path is built. It runs behind the transcription interface described at the top of this
document, and the `Unlimited-OCR` specialist below is the same interface with a different runtime.

**Scanned pages and images**
- Render to PNG at 300 DPI with PyMuPDF
- Transcribe through the configured vision model, or Unlimited-OCR (see below)
- Splice the transcribed pages back into the parse, in page order
- Chunk, embed, and store exactly as a text-layer page would be

#### What the acceptance run measured

`Fourier_Tables.pdf` is eight pages of a signals textbook appendix, scanned, with no text layer at
all: every page extracts zero characters and Phase 1 correctly refused the whole document. Read
through the general path against Qwen3.6 27B:

| | |
|---|---|
| Pages recognized | 8 of 8, none failed |
| Whole document, upload to `ready` | 110.8 s |
| Per page | 13.8 s mean, 13.9 s median |
| Result | `ready`, 9 chunks, `pages_skipped` 0 |

**13.8 seconds a page, from a completely separate document, against the 13.4 the reference book's
text-layer pages measured in Step 3.** Two different documents at two different densities landing
within four percent of each other is the strongest evidence available that this rate is the model's
and not the page's, which is what makes the extrapolation to a 608-page book worth stating: still
about 2.3 hours, and still the argument for the specialist path.

Read by eye rather than counted, which is the standard this project holds itself to on whether a
reading is *right*. The transforms come back correct: `u(t)` maps to `1/(jw) + pi*delta(w)`,
`t*exp(-at)u(t)` to `1/(a + jw)^2`, the DTFT of `a^n u[n]` to `1/(1 - a e^{-jW})`, Parseval in both
domains. Running heads and page numbers 774 through 780 are transcribed in sequence, the
"(continues on next page)" rule line survives, and a graphic on the opening page comes back as
`[figure]` exactly as the prompt asks. Two-column tables are emitted as tables.

**What it does not fix is ranking inside a dense reference table.** Each page is one chunk, and
every page opens with the same running head, so the embeddings sit close together: asked for the
transform of the unit step, the right page ranks 4th of 8. The document is searchable, which it was
not before, and every page is reachable. But a page of LaTeX table markup is not prose and the
chunker has no heading structure to cut it on. That is a chunking limit rather than a recognition
one, it is recorded in feature-roadmap.md as its own item, and it is not what this stage set out to
fix.

#### The transcription has to commit to a notation

Reading the acceptance run's own output back with the notation counter (`_text_shape` in
`scripts/eval_ingest.py`) says something the by-eye reading missed. The eight pages of one document
wrote their tables **three different ways** — bare pipes with no header rule, a full Markdown table
with `| :--- |`, and a raw LaTeX `tabular` — and **three of the eight marked up no table at all**,
emitting the two columns as consecutive plain lines. Headings varied the same way: three were
returned in bold instead of marked up, and one was split across two lines mid-phrase
(`C.1 Basic Discrete-` / `Time Fourier Series Pairs`).

Each page is individually plausible. Together they are four documents, and nothing downstream can
read them: `chunk.SECTION_HEADING` sees an ATX heading, so a nine-section appendix chunked as eight
anonymous pages with not one of `C.1` … `C.9` recorded as a section title. **The retrieval failure at
the bottom of the previous section was created at transcription time**, not at chunking time.

So `TRANSCRIBE_PROMPT` now pins the notation rather than asking only for a faithful reading:

- every heading on **one** line, starting with `#`, keeping its number — never bold, never split
- every table as a Markdown table, **always** with a header row and a `| --- |` rule, using an empty
  header where the page prints none — never LaTeX, never plain lines

Stated as rules rather than examples, and stated even for the case where the page has no header to
copy: a header row that is sometimes present and sometimes not is a second notation wearing the
first one's clothes.

One thing the prompt cannot fix, fixed beside it: `C.1 Basic Discrete-Time Fourier Series Pairs`
matched none of `SECTION_HEADING`'s four forms even written perfectly on one line, because the
numbered branch required a leading digit. `retrieve.SECTION_REFERENCE` already understood
`section A.2` on the query side, so this was the two ends of one feature disagreeing. The letter must
be followed immediately by a dot and a digit, which keeps a lab's `A. Build the circuit` from reading
as a section of its own.

**The re-run is pending.** The measurement is one command — `eval_ingest.py recognize` prints the
notation count for the document, and `recognition.md` now opens with it — and the tutor endpoint used
for every recognition number in this document has been answering
`model name=Qwen3.6-27b failed to load` since this was written. Until it runs, the prompt change is
defended by unit tests and by the baseline above, and it is not yet defended by a measurement.

#### The general path is not free, and "bundled" is Phase 6

The roadmap phrase for the first implementation is "route scanned pages through the bundled vision
model, since one is present anyway". Nothing is bundled yet. Inference is bundled in Phase 6, and
until then the tutor endpoint is whatever the student configured, which may not accept images at
all. The general path therefore needs three things that do not exist today, and the phase should not
be planned as though it needs none:

- **Image content parts in `llm/client.py`.** The client sends and parses text. Sending a page means
  the OpenAI content-part array shape, which is new code rather than a flag, exactly as tool calling
  was in Phase 2
- **A vision capability probe**, mirroring `probe_tool_support`. An endpoint that cannot see is a
  normal configuration and not an error, and it degrades the same way an endpoint without tools
  degrades verification: honestly, in the interface, with the feature unavailable rather than
  silently wrong
- **The same locality rule the extraction stage already follows.** Transcription sends the page
  image of a student's document to the configured endpoint. Against a non-local endpoint without
  acknowledgement it must skip, for the reason `extract_facts` skips

#### Rendering for recognition is not rendering for reading

`rag/render.py` already rasterizes pages and caches them under `data/pages/{document_id}/`, and its
docstring says it was built for this stage. It renders at 144 DPI, which is chosen for a source pane
on a HiDPI display. Recognition wants 300 DPI per the reference invocation below.

These are two different artifacts and they must not share a cache entry. A page rendered for reading
that silently satisfied a recognition request would degrade transcription with nothing on screen to
say so, and a page rendered at 300 DPI served to the source pane wastes several times the bytes. The
DPI belongs in the cache path.

#### Per-page state, which is where recognition lives

`documents` carried `pages_total`, `pages_done`, and `pages_skipped`, and `pages_done` was written
exactly once, at the end of a run. That was honest while every page of a document shared a single
outcome: either the file parsed or it did not, and ui-phase-1.md deliberately showed a page count
rather than a counter that would never move.

Recognition breaks that. A page costs seconds of model time, can fail on its own, and is worth
retrying on its own, which makes it a row. `document_pages` carries the page number, its state, its
error, and its transcription, and `pages_done` is now counted from it and committed after each page.
A document whose page 7 failed is a document with 39 good pages and one retry.

| Page state | Means |
|---|---|
| `text` | The page had an extractable text layer |
| `scanned` | No text layer, and nothing has been asked to read it |
| `recognized` | Read as an image, with the transcription stored on the row |
| `failed` | Recognition was attempted and could not transcribe it |

Four consequences fall straight out of the table, and they are the reason it is shaped this way.

**A retry is the same operation as a first run.** Both mean "attempt every page not currently
carrying text", which is `scanned` and `failed`, so `Read this document` and `Try those pages` are
one endpoint and a retry never spends model time re-reading a page that worked.

**Re-indexing never re-runs recognition.** The transcription lives on the row, and the bytes a
document id points at do not change, so a re-parse splices the stored text back in. A page with a
text layer is not stored, because re-extracting it costs well under a millisecond and a second copy
is only somewhere for the truth to drift to.

**A blank page is read, not failed.** An empty transcription is the correct reading of an empty
page. It stays out of the chunker, because an empty chunk answers nothing and dilutes every search
that touches it, and it stays out of the retry set, because reading it again returns the same
nothing.

**A run gives up rather than grinding through a dead endpoint.** Every page failing in sequence is
not a document problem, it is the endpoint being down, and discovering that six hundred times costs
the timeout on each one. After three consecutive failures the run stops; the pages it never reached
stay `scanned`, which is the truth, and the retry picks them up.

Recognition is **not** a fifth ingestion state. It runs inside `parsing` with `stage_detail` set to
`recognizing`, which is what tells the interface its page counter may appear. ui-phase-3.md keeps
four steps on screen for the product reason - there is text in the file or there is not, and either
way what Lyra is doing is reading the document - and the schema agrees for a second reason: a
two-hour transcription is then a long `parsing`, and `reconcile_interrupted` already knows what to
do with a document caught mid-parse.

Where all three outcomes of an unreadable document now land:

| Situation | State | Message |
|---|---|---|
| Nobody has asked for it to be read | `unsupported` | The existing scanned-document line |
| Asked, but no endpoint or an unacknowledged remote one | `unsupported` | The reason, which can be acted on |
| Asked, ran, and every page failed | `failed` | Nothing was kept, so nothing to call ready |

#### OCR runtime

Unlimited-OCR is DeepSeek-OCR v1's DeepEncoder with a DeepSeek-V2 MoE decoder whose attention is
replaced by R-SWA (Reference Sliding Window Attention). llama.cpp loads it through MTMD, which
requires **two** GGUF files: the language model and a multimodal projector (`mmproj`).

Weights live in `data/models/`. Pin `Q4_K_M` for the language model and `bf16` for the mmproj.

**Reference invocation** (from the GGUF publisher, used as the baseline for our integration):

```bash
llama-mtmd-cli \
  -m data/models/unlimited-ocr-Q4_K_M.gguf \
  --mmproj data/models/mmproj-unlimited-ocr-bf16.gguf \
  --image page.png -p "document parsing." \
  --chat-template deepseek-ocr --no-jinja \
  --temp 0 --flash-attn off --no-warmup \
  -n 4096 -c 16384 \
  --dry-multiplier 0.8 --dry-base 1.75 --dry-allowed-length 35 \
  --dry-penalty-last-n 128 --dry-sequence-breaker none
```

**Sampler note.** llama.cpp has no `no_repeat_ngram_size`. The reference implementation's
`no_repeat_ngram_size=35` with `ngram_window=128` is approximated by the DRY sampler flags above.
These values are a deliberately weak loop guard. Do not tighten them: aggressive DRY settings garble
the model's HTML table output. Repetition loops on long or dense pages are the expected failure mode
here, so the OCR stage MUST enforce an output token ceiling and mark a page `failed` rather than
hang.

**Prompts.** Single page uses `document parsing.`. The multi-page prompt is
`Multi page parsing.`, which is not used (see the batching decision below).

**Do not treat OCR as a generic OpenAI-compatible call.** The reference vLLM and SGLang recipes pass
server-specific fields that no standard client sends: `images_config.image_mode`,
`custom_logit_processor`, `custom_params.ngram_size`, `custom_params.window_size`, and
`skip_special_tokens: false`. The detection markers must survive the response, so any transport that
strips special tokens breaks post-processing. The `base_size`, `image_size`, and `crop_mode`
arguments in the reference Python API (`gundam` and `base` configs) are arguments to the
`transformers` wrapper and do not exist on the llama.cpp path; there, tiling is driven by the
projector's `preproc_max_tiles` metadata. [INFERENCE, based on the publisher's note about
`preproc_max_tiles`; confirm during the spike below.]

#### Upstream llama.cpp status

Support arrived through a stacked series of pull requests. Only the first is merged.

| Change | PR | State | Consequence if absent |
|--------|----|-------|-----------------------|
| Converter plus full-MHA decoder | [#24969](https://github.com/ggml-org/llama.cpp/pull/24969) | Merged 2026-06-24 | none, this is what makes the model load at all |
| R-SWA in the decoder | [#24975](https://github.com/ggml-org/llama.cpp/pull/24975) | **Draft** | Decoder runs full multi-head attention. The near-constant memory that makes 40-plus-page single-pass parsing viable is lost, so long contexts grow memory and slow down sharply. |
| `max_tiles` fix | [#25614](https://github.com/ggml-org/llama.cpp/pull/25614) | **Merged 2026-08-05** | The projector's `preproc_max_tiles = 32` was ignored in favour of DeepSeek-OCR v1's cap of 9. Now read from GGUF metadata. |

The pin moved for that second row. `scripts/fetch_models.py` now pins **b10287**, which is
exactly commit `b06aa77`, the merge of #25614 - and records the commit beside the tag,
because a tag names a build rather than a state of the source. The fetcher asks an existing
binary what it is with `--version` and replaces it when it is not the pin, and removes the
old extraction rather than leaving it: every consumer takes the first `llama-server` it
finds while walking the directory, so `llama-b10235` sorted ahead of `llama-b10287` and
downloading the new build changed nothing until that was fixed. The embedding server was
re-checked on the new build and produces the same vectors.

**Decision: page-batched OCR, one page per request.** Phase 3 does not use one-shot long-horizon
multi-page parsing, because that feature depends on R-SWA, which is not upstream. Page-at-a-time
processing also gives bounded memory, per-page progress reporting, per-page retry, and page numbers
for later citation. The cost is the loss of cross-page context, so a table or problem spanning a
page break may be split. That is accepted. Revisit when #24975 merges.

Pin the exact llama.cpp build in `scripts/` and record the commit. Do not float on master: this
model's support surface is actively changing.

#### Required spike before the specialist path

One-shot `llama-mtmd-cli` per page reloads several GB of weights per page, which is unacceptable for
a 40-page document. The alternative is a persistent `llama-server` process with `--mmproj` that
Lyra manages and posts one page per request to. That path has a known hazard: with
`--chat-template deepseek-ocr`, llama-server has failed image-marker tokenization for this model
family with `number of bitmaps (1) does not match number of markers (0)`, returning `400`
([issue #21022](https://github.com/ggml-org/llama.cpp/issues/21022), closed as completed). Dropping
the chat template made it run but degraded OCR quality.

**Spike acceptance criteria:** on the pinned build, OCR a known scanned page through persistent
`llama-server` and through one-shot `llama-mtmd-cli`, and confirm byte-identical or
quality-equivalent text with the chat template applied.

#### Spike outcome: llama-server serves it, and the model still loses

Run by `scripts/ocr_spike.py` on b10287 against page 2 of the scanned Fourier tables.

**llama-server serves this model with `--chat-template deepseek-ocr` applied.** No `400`, no
`number of bitmaps (1) does not match number of markers (0)`. The hazard from
[#21022](https://github.com/ggml-org/llama.cpp/issues/21022) is gone on this build, so the
fallback to one-shot `llama-mtmd-cli` per page is not needed.

**`--special` is mandatory, and its absence is silent.** llama-server suppresses special
tokens by default, and this model carries its layout in them. Without the flag the `<|det|>`
markers vanish and, because the table cell tags are special tokens too, a table arrives with
its cells fused: `Time Domain` and `Frequency Domain` come back as `Time DomainFrequency
Domain`. Measured on one page: **1943 characters without it, 2457 with**. It is a
server-only flag; `llama-mtmd-cli` rejects it and prints special tokens anyway. Asking for
them means the caller strips the end-of-sequence token and the detection markers itself,
which `rag/transcribe.py` does.

With the flag on, the two paths agree to **0.9768** similarity. The remainder is
sub-pixel drift in the detection coordinates and a handful of `i` versus `j` and `p` versus
`k` token choices, where neither path is uniformly right. Quality-equivalent, as required.

**And then the specialist loses the comparison that matters.** All eight pages of the same
scanned document, against the general path's numbers from Stage 2b:

| | General (configured vision model) | Specialist (Unlimited-OCR, b10287) |
|---|---|---|
| Seconds a page | **13.8** | **18.5** |
| Pages with a repetition loop | 0 of 8 | **5 of 8** |
| Worst loop | none | one line repeated 217 times |

Page 4 is the clearest failure: 1111 characters of which a single line accounts for 217
repeats. Pages 1, 3, 7 and 8 loop too. The output token ceiling is what ends them, exactly
as this document said it would have to, but a page that ends by hitting a ceiling is a page
that was not read.

So the specialist is **built, downloadable, tested, and not enabled**. `rag/transcribe.py`
carries `transcribe_page_locally` and `llm/ocr_server.py` manages the process, but
`core/recognition.py` does not call them: routing recognition to a path that is 34% slower
and garbles most pages would be shipping worse.

**What would change the answer.** R-SWA (#24975) is still a draft, so the decoder runs full
multi-head attention, which is what this document already predicted would make long contexts
slow down sharply - and a page whose output loops is a long context. The repetition loops are
the other half of the same problem: llama.cpp has no `no_repeat_ngram_size`, the DRY sampler
is a weak substitute, and this document is explicit that tightening it garbles tables
instead. Both point at the same merge. Re-run `scripts/ocr_spike.py` and the eight-page
comparison when it lands.

None of this reaches the student. Transcription through the configured vision model needed
none of it, which is why the phase was sequenced to deliver that first.

#### Post-processing

Detection markers are stripped and lines regrouped into blocks:

```python
DET_RE = re.compile(r'<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)', re.DOTALL)
```

Blocks whose category is `image` are dropped. Lines within a block join with `\n`; blocks separate
with `\n\n`. `re.DOTALL` is required.

**Output:** Raw text with page and block structure

### Stage 3: Chunk

Chunking respects document structure. It never splits a homework problem, a code block, or a table
in half if it can avoid it.

**Strategy by document type:**

| Document type | Chunk boundary | Target size | Overlap |
|---------------|----------------|-------------|---------|
| Homework | Individual problem | Whole problem, capped | None |
| Solutions | Individual problem | Whole problem, capped | None |
| Exam | Individual problem | Whole problem, capped | None |
| Textbook | Section or subsection | 1000 tokens | 100 tokens |
| Lecture notes | Topic heading | 750 tokens | 75 tokens |
| Lab | Topic heading | 750 tokens | 75 tokens |
| Syllabus | Logical section | 500 tokens | 50 tokens |
| Generic | Paragraph group | 750 tokens | 100 tokens |

`solutions` and `exam` share homework's rule because they are the same shape. They are separate
types because extraction reads them completely differently - see Class Profile Construction. An
answer key used to classify as `generic` and be cut on blank lines, so one chunk held the end of one
solution and the start of the next.

**Document types are detected from the filename's *words*, not from substrings of it.** `syllabus`
contains `lab`, `latest` contains `test`, and `keywords` contains `key`, so a substring rule cannot
be extended past the three patterns it started with without misfiling ordinary documents. The
filename is split on punctuation, case changes, and letter/digit runs, and matched against whole
words. Precedence matters as much as the patterns: `solutions` outranks `homework`, so
`homework-4-solutions.pdf` is an answer key rather than an assignment with a due date on it.

**Hard ceiling: 1024 tokens per chunk, enforced for every type after strategy-specific splitting.**
No chunk is ever stored above this. The ceiling exists for two reasons: retrieval budgeting assumes
small targeted chunks, and an oversized chunk is refused at embedding time.

**The ceiling and the limit it respects are measured in different units, which is why it has
headroom.** The ceiling counts with `estimate_tokens` at four characters per token; the limit is
2048 *real* tokens, described in Stage 4. Real text does not run at four characters per token, and
mathematical text is nowhere near it: measured over a 608-page linear algebra textbook the median is
3.4 and the first percentile is 2.1. The two numbers used to both be 2048, so the ceiling had no
headroom in the one direction that matters, and a chunk this pipeline called 2047 tokens arrived at
the server as 2607 and was refused, failing the whole document.

1024 comes from that first percentile, so roughly one chunk in a hundred needs the split Stage 4
performs and the rest go straight through. It is deliberately not set at the observed worst case of
2.1 characters per token, which would put it near 800 and halve chunk sizes again to spare the
embedder a split it handles correctly. Targets moved down with it: a target above the ceiling is not
a larger chunk, it is dead configuration, because the strategy packs towards it and the ceiling then
cuts the result back down. Overlaps are unchanged and are now a larger share of a smaller chunk,
which is the direction that loses less across a seam.

**Oversized homework problems.** A single problem above the ceiling is split in this order, stopping
as soon as it fits: on lettered or numbered sub-parts (`(a)`, `(b)`, `i.`, `ii.`), then on
paragraphs with 100-token overlap. Every resulting chunk keeps the same `problem_number` and gains a
`part_index`, so retrieval can reassemble the full problem when any part matches.

**A page is a hard boundary, so the generic strategy only decides what happens inside one.** Two
rules do, and both were found by the same document - the eight-page scanned appendix of Fourier
tables, where every page came back as one chunk holding a dozen unrelated identities.

- **A block bigger than the target is cut at its own line boundaries.** Without this the paragraph
  strategy has no way to divide a block at all: an oversized one survived every size rule the
  strategy has and reached the ceiling, where it was cut at 1024 tokens and wherever the character
  count landed - through the middle of a table row rather than between two of them. A table
  transcribed one row to a line is exactly that shape, and so is a page of prose an extractor gave
  no blank lines to. No overlap across the seam, the same choice `_hard_split` makes: these are a
  list's items rather than a sentence's clauses, and repeating one duplicates a fact.
- **Two blocks that are each substantial are not packed into one.** Packing is right when the
  blocks are fragments - a heading, a caption, the line that introduces a list - and wrong when both
  are already a chunk's worth, because the result is one embedding standing for two subjects. The
  floor is 100 tokens, from the corpus rather than from taste: the appendix's page of transform
  pairs holds two tables of 241 and 220 tokens that were glued together, and its page of definitions
  holds ten blocks of 9 to 87 tokens that belong together and are still packed. The gap between 87
  and 220 is what lets this be a constant. No overlap across that seam either: it is a division of
  the document, not a place a sentence was cut.

Both apply to the paragraph strategy only. The heading strategies pack towards their target on
purpose, and the reference book's retrieval depends on a chunk holding a section rather than a
paragraph; re-chunking the 608-page book after this change produces the same 596 chunks it did
before, and the same 17-of-17.

**What that bought, and what it did not.** Measured over the scanned appendix with a set of eleven
questions whose answers are checked by eye against the transcription, in
`scripts/eval_questions/fourier-tables.json`:

| | Chunks | Rank 1 | Top 4 | Mean rank |
|---|---|---|---|---|
| Before | 9 | 2 of 11 | 7 of 11 | 3.64 |
| After | 11 | 3 of 11 | 8 of 11 | 3.64 |

The question the handoff named - the transform of the unit step - moves from **third to first**, and
that is the honest size of the win. The aggregate barely moves, and two things that were expected to
move it did not:

- **Chunking finer is not the answer.** The generic target was swept from 750 down to 200. Rank-1
  hits wander between 2 and 5 with no trend and the top-4 rate gets *worse* below 300. The target is
  therefore left where the strategy table puts it: there is no number in that range this document
  prefers, and changing a shipped default on eleven questions over one document would be tuning to
  noise.
- **The running head is not the cause.** The handoff's stated mechanism was that every page opening
  with the same running head pulls their embeddings together. Stripping the line that repeats at the
  top of most pages - detected by normalising away the page number and requiring it on half the
  pages - changes rank-1 not at all and the mean not at all. A constant prefix shifts every chunk
  alike, so it barely moves their order. It is not implemented.

What is left is not a chunking fault. The questions that still rank badly are cross-representation
confusions: the discrete-time properties table ranks below the continuous-time one for a question
that names discrete time, because the two pages are near-identical mathematics and the embedder
cannot tell "discrete" from "continuous" in them. That is a limit of a 137M embedding model on
tables of transform pairs, and the lever for it is the model or a reranker, not the chunker.

**Detection order:**
1. Detect document type from filename patterns and content heuristics
2. Textbook: structural signals, principally a PDF outline with real depth over a long document
3. Homework: split on problem markers (`1.`, `Problem 1`, `Q1`)
4. Textbook or notes: split on heading markers (`#`, `##`, numbered sections)
5. No structure detected: paragraph grouping with overlap

Step 2 is placed above homework deliberately: the two are separated by structure rather than by
markers, and a book of exercises will always out-vote a marker count. Its absence was why the
reference textbook was chunked as homework, into 1312 fragments; with it the same book chunks as
596 sections.

The test is an outline of at least 20 entries nested at least two deep, over at least 50 pages, and
all three are required. Calibrated on one book and then checked against a second corpus: the two
longest documents of a signals course are 112 and 128 pages of lecture notes carrying flat outlines
of 10 and 4 entries, and the depth and entry-count requirements are what keep them out. A filename
the student chose still beats the test, so `homework-4.pdf` is homework whatever its structure.

**Each chunk stores:** `chunk_id`, `document_id`, `class_id`, `content`, `token_count`, and metadata
(document type, page number, section title, `problem_number`, `part_index`).

`problem_number` and `part_index` are populated today and are the substrate the Phase 2 solver
segments against, so problem-level addressing is not new work in that phase. `section_title` becomes
the hierarchical `section_path` in Phase 3, described in Stage 2a.

**Existing chunks keep their flat `section_title` until their document is re-ingested.** A
`section_path` is derived from a PDF outline, and the outline is in the source file rather than in
the database, so there is nothing to backfill from: a migration can add the column but only a
re-parse can fill it. The column is therefore nullable, retrieval treats a null path as "this
document predates structural parsing" rather than as an error, and the student is offered a
re-index rather than having one run on their behalf. This is the same posture the embedding-model
identity rule takes in Stage 5, and for the same reason.

### Stage 4: Embed

**Model:** `nomic-embed-text-v1.5` GGUF, 137M parameters, 768 dimensions, **2048 max input tokens**.
Served locally by llama.cpp. Using llama.cpp rather than sentence-transformers keeps PyTorch out of
the product entirely and reuses the runtime already required for OCR.

**2048, not the 8192 on the model card.** This GGUF declares `nomic-bert.context_length = 2048`,
which llama.cpp reports as `n_ctx_train` and clamps every request to no matter what `-c` says. The
8192 the model is advertised with is reachable only through rope scaling this GGUF does not carry,
and asking for it anyway produces a `n_ctx_seq (8192) > n_ctx_train (2048)` warning and a server
that still refuses anything over 2048. An over-long input is refused with
`exceed_context_size_error` rather than truncated, so this is a wall rather than a quality slope.

**The limit is enforced in real tokens, in `rag/embed.py`, not left to the chunk ceiling.** No
estimate can be the last word in front of a hard wall. Any input the character count cannot prove
safe is measured against the server's own `/tokenize`, at 0.34 ms a call, and anything still over
the limit is halved at whitespace until its pieces fit. The pieces are embedded and their vectors
mean-pooled and re-normalized back into one, so a caller still gets exactly one vector per input:
`_store_chunks` zips chunks to vectors strictly, and a split that changed the count would corrupt
the index rather than fail. Pooling is the same operation the server already performs with
`--pooling mean`, one level up. Nothing is truncated and nothing is dropped.

A string with no whitespace to cut at is a genuine dead end and says so, rather than reaching the
student as an unreachable server.

**Only the per-input limit is real.** llama-server splits a request across its own batches
internally, so sixteen inputs totalling 30,144 tokens are served without complaint against `-b
8192`. Measured, because the obvious reading of `-b` is that it caps the request, and a token budget
built on that reading would have been machinery guarding nothing.

**Task instruction prefixes are mandatory and asymmetric.** This model requires a prefix telling it
which task is being performed. Omitting them, or using the same prefix on both sides, degrades
retrieval quality with no error and no warning.

- Indexing a chunk: `search_document: ` + chunk text
- Embedding a user query: `search_query: ` + query text

These prefixes are applied in exactly one place, `rag/embed.py`, and never at call sites. The
prefix is not stored as part of the chunk content.

The pooling and normalization flags for the llama.cpp embedding server must be confirmed against
the pinned build during scaffolding; nomic uses mean pooling. [INFERENCE: flag names unverified.]

**Output:** 768-dimension float vector per chunk

### Stage 5: Store

**`sqlite-vec`**, a `vec0` virtual table with `class_id` as a partition key so retrieval is scoped
without a post-filter.

```sql
create virtual table chunk_embeddings using vec0(
  chunk_id integer primary key,
  class_id integer partition key,
  embedding float[768] distance_metric=cosine
);
```

**Search is exact brute-force KNN, not approximate.** `sqlite-vec` provides no ANN index: it has no
HNSW and no IVF, and both of its query paths, `vec0` virtual tables and manual `vec_distance_*` with
`ORDER BY`, scan candidate vectors. Do not design features on an assumption of sublinear search.

This is the right tradeoff at our scale. A class holds thousands of chunks, not millions, and exact
search removes recall tuning entirely. The practical ceiling is roughly the low hundreds of
thousands of chunks per class before latency becomes noticeable; we are two orders of magnitude
below that. If that ever changes, the fix is a different vector store, not a flag.

**Embedding model identity is recorded, and changing it requires a rebuild.** A `vec0` table fixes
its dimensionality at creation, so a model with a different dimension cannot be mixed in, and even a
same-dimension model produces vectors that are not comparable to the old ones. Therefore:

- Every chunk row stores `embedding_model` and `embedding_dim`
- Settings records the active embedding model
- On mismatch at startup, Lyra refuses to serve retrieval and offers an explicit re-index that drops
  and rebuilds the vector table from stored chunk text

Chunk text is retained precisely so a re-index never requires re-running OCR.

### Stage 6: Retrieve

1. Embed the query with the **`search_query: `** prefix
2. Exact KNN over the class partition, `k = 64`
3. BM25 over the same partition's FTS5 index, top 64
4. Reciprocal rank fusion of the two rankings — recency acting through the vector ranking, a
   small bonus toward answer keys — cut to the fused top 64
5. Rerank with the cross-encoder and cut back to 8, where one is installed; without one, serve
   the fused top 8
6. Trim to the retrieval budget
7. Expand any matched homework part back to its sibling parts where the budget allows

**Recency weighting.** Cosine distance is adjusted by a bounded recency bonus so that newer material
wins ties without displacing a clearly better match:

```
score = cosine_similarity + 0.05 * recency_factor
```

where `recency_factor` decays linearly from 1.0 for a document uploaded today to 0.0 at 120 days.
The 0.05 coefficient is deliberately smaller than meaningful similarity gaps; it breaks ties, it does
not reorder strong matches.

**Structural lookup, added in Phase 3.** A query that names a section is not a similarity problem
and must not be answered with one. When a query carries an explicit section reference
(`section 5.2.1`, `Chapter 4`, `§A.2`), the matching `section_path` is resolved directly and those
chunks are placed ahead of the KNN result rather than left to compete with it on cosine distance.
The KNN still runs and still fills the remaining budget, because a section reference tells you where
to look and not what the student needs from it.

What counts as a reference is exactly what `SECTION_REFERENCE` in `rag/retrieve.py` matches:
`section`, `chapter`, `part`, or `§`, followed by a number. `Theorem 2.63` is not one — a numbered
result names a statement rather than a place in the outline, and an outline has no entry for it —
so a query citing a theorem falls to the KNN like any other query. An earlier version of this
document listed it among the resolved forms, which was a claim ahead of the code.

Four rules keep this from becoming a worse retrieval than the one it improves:

- A reference that resolves to nothing falls through to the KNN silently. A student may cite a
  section of a book they never uploaded, or a course may number its weeks, and a hard failure there
  would be a regression on every course that says "week 3"
- A resolved section larger than the budget is trimmed by cosine distance within the section, so the
  part of section 5.2 that answers the question outranks the part that does not. What survives is
  then put back into reading order, because a section quoted out of order is harder to follow than
  one quoted short
- **A lookup may take at most half the retrieval budget.** A reference says where to look and not
  what is wanted from it, so the KNN keeps room to answer the second question. Without the cap a
  forty-chunk chapter fills the context on its own
- Structural chunks are still labelled with their source in the context block, so a step grounded in
  a looked-up section carries the same provenance as one grounded in a retrieved one

A lookup matches the section and everything nested under it, so asking for section 2.2 also reaches
2.2.1. It does not reach 2.20, which is a different section rather than a deeper one.

The trim notice is computed over the similarity ranking alone. A chunk that did not fit beside a
section the student asked for by name was not omitted for lack of room in the sense that flag means,
and counting it would raise the notice on every turn that cites a section.

**`k = 8` is a Phase 1 constant.** It was chosen for chat turns over syllabi, and the Phase 2 handoff
records it as a known weakness in solving. It is now measured rather than adjusted by feel, and it
survives: on a real 36-document course every answer that retrieval finds at all is inside the eight.
Nothing else in this document assumes the number stays at 8.

**Retrieval is measured against a class, and was not before.** Every retrieval number this project
quoted through Phase 3 — 17/17 at `k = 8`, 16/17 at first place — was measured in a workspace holding
**one document**. `retrieve` is class-scoped, so those runs asked a question of a haystack with no
competition in it. A real class has thirty-six documents.

Re-measured over the real course, in a workspace holding all of it (1124 chunks, 36 documents), the
difference is the whole story:

- The **textbook** set is unchanged: 16/17 first, 17/17 at `k = 4`, exactly its single-document
  numbers. It is a linear algebra book in a signals course and nothing competes with it
- A set written **about the course itself** scores 9/16 first and 14/16 in the served eight, and only
  54 of the 128 chunks the product would serve come from the document the answer is in
- The two it never finds are the same shape: a student asking how a homework problem is *worked* gets
  the problem statement and eleven near-identical problems from another week, and the answer key
  never appears in the top 128

A control is also a weaker instrument at class scale, and the harness now says so: "not in this book"
does not mean "not in this class", and the textbook set's Fourier control scores *above* the median
real question once a course's Fourier material is in the same class.

`scripts/eval_ingest.py` grew what this needs: a question set names the document each answer is in
(`expect_document`), a chunk from the right pages of the wrong document is scored as a miss, and the
report names which documents did the crowding.

**Reranking, added after Phase 3.** The KNN is a bi-encoder search: the embedder turns a passage into
768 numbers before it has seen the question, which is what lets it search a whole class and is also
what it cannot do — tell two passages apart on a distinction the question makes and the passage does
not announce. A course is full of those. A problem set and its answer key restate every question
verbatim; a low-pass lab and a high-pass lab are one handout with a word changed; a practice midterm
and its solution share an instructions paragraph character for character.

So where the weights are installed, retrieval fetches 64 neighbours instead of 8 and a cross-encoder
reads the question and each passage *together* before choosing the 8 that are served. Measured on the
real course (`scripts/eval_questions/ece203-class.json`, sixteen questions whose answer is a known
page of a known document):

| | first place | in the served 8 | per question |
|---|---|---|---|
| fetch 8, no reranker | 9/16 | 14/16 | 0.02 s |
| fetch 32, reranked | 12/16 | 14/16 | 0.84 s |
| **fetch 64, reranked** | **12/16** | **15/16** | **1.58 s** |

Thirty-two was not enough because one real answer sat at rank 35 in the embedding order, and a
reranker can only reorder what it is given.

Four things this is not:

- **Not a replacement for the embedder.** A cross-encoder cannot search: scoring a thousand chunks
  means a thousand forward passes. It goes second, over a shortlist small enough to afford
- **Not free, and not free in a place that hides.** 1.6 seconds is added to every turn that retrieves,
  on the largest classes measured up to 3.4. It is small beside the model turn it feeds and it is not
  nothing
- **Not required.** The weights are 640 MB and `rag/rerank.py` returns None on every failure — absent,
  refused to start, timed out, malformed reply — and retrieval keeps the embedding order. A student
  without them gets slightly worse search, not a broken class
- **Not always better.** On the linear algebra textbook, whose topics nothing else in the class
  shares, reranking costs one question its first place and changes nothing at `k = 4`. It earns its
  place on documents that compete, which is what a course is

The over-fetch is cut to `k` before budgeting rather than left to the budget. Everything past `k` is
material the search was not confident about and the reranker did not rescue, and letting the budget
decide would quietly change how many chunks a turn is built from.

`score` is the ranking key and nothing else. Where reranking ran it holds the cross-encoder's logit,
which is unbounded, routinely negative, and comparable only against other scores from the same query.
`similarity` is always the embedder's cosine, which is what the interface shows.

**Hybrid retrieval, added in Phase 5.** The one case the reranker could not reach was an embedding
failure before it was a ranking one: a problem set and its answer key restate every question
verbatim, the embedder cannot tell them apart, and the key sat outside the top 128 neighbours,
beyond any reordering. The words being identical is the textbook case for lexical matching, so
retrieval now runs BM25 over an FTS5 index of the same chunks beside the KNN and fuses the two
rankings by reciprocal rank fusion (`RRF_K = 60`) before any reranking. A chunk near the top of
both lists outranks a chunk at the top of only one, and neither list's score scale matters because
only ranks are read. `doc_type = 'solutions'` chunks receive a bonus of half a top-rank
contribution — the document-type boost toward answer keys the roadmap item named.

Measured on the 36-document course, before against after, with a second answer-key question
(`hw3-cascade-impulse-response-answer`) added so the failure is watched twice:

- The unreachable question, `hw5-two-sided-exponential-answer`, goes from absent in the top 128 to
  **rank 4 reranked** — rank 28 on the fused order alone. The class set improves to **17/17 in the
  served eight** at an unchanged 13/17 first
- The textbook set is untouched: 16/17 first and 17/17 in the served eight, plain and reranked, at
  one document and at class scale. Nothing in a signals course competes with a linear algebra
  book, and lexical votes do not create one
- The bonus is what carries the answer key into the candidate sixty-four: measured with it zeroed,
  the question is absent again and no other rank moves. In the fused order alone the bonus costs
  up to four first places — problem statements ceding the head to their keys — and every one stays
  in the served eight; the reranker restores all four. The bonus stays

**Output:** Ranked chunks with content and metadata

### Stage 7: Generate

**Context budget.** The tutor model's context window is divided into four buckets that sum to 100%.
An explicit generation reserve is mandatory: without it, a full prompt leaves no room for the model
to answer.

| Bucket | Share | 8K window | 32K window |
|--------|-------|-----------|------------|
| Generation reserve (output) | 25% | 2048 | 8192 |
| System prompt, mode, profiles | 15% | 1229 | 4915 |
| Conversation history | 20% | 1638 | 6554 |
| Retrieved context | 40% | 3277 | 13107 |

Rules:
- The generation reserve is subtracted first and is never borrowed from.
- Unused history budget may be lent to retrieved context, never the reverse.
- Retrieved chunks are trimmed lowest-score-first until they fit.
- Conversation history is trimmed oldest-first, always keeping the most recent exchange.
- If the configured window is below 8192 tokens, Settings shows a warning that quality will suffer.
- When retrieval is trimmed by more than half, log it **and** surface a quiet indicator in the UI, so
  the user can tell that the model did not see everything.

**Prompt structure:**
```
[System prompt: mode, user profile, confirmed class profile facts]

[Retrieved context, labeled by source document and page]

[Conversation history]

[User's current message]
```

**Streaming.** SSE over `POST`, consumed with `fetch` and a `ReadableStream` reader. Note that the
browser `EventSource` API cannot be used, because it only issues `GET`.

**Reasoning models.** Lyra assumes the user's model may think before it answers, and works either way.
A thought reaches the client on its own `reasoning` frames, never mixed into the `token` frames that
carry the answer, and is stored in the message's `thinking` column beside the reply rather than
inside it. Two upstream shapes are handled:

- A server running a reasoning parser (llama.cpp, vLLM, Ollama, and the hosted DeepSeek and
  OpenRouter APIs) puts the thought in its own delta field: `reasoning_content`, `reasoning`, or
  `thinking`.
- A server without one leaves raw `<think>...</think>` markers inline in the content stream, so the
  client splits them back out, holding back any partial tail that could still become a tag.

One case is deliberately unhandled: a chat template that pre-fills the opening `<think>` server-side,
leaving only the closing marker on the wire. Text already streamed to the reader cannot be
reclassified, and buffering every answer against the chance a close marker arrives would delay the
first word for every non-thinking model. Servers that do this ship a reasoning parser, which is the
first path above.

A stored thought is **never replayed to the model as history**. It is context for the reader, not for
the next turn.

## Class Profile Construction

A class profile is a description of the **course**, not a pile of per-document extractions. That
distinction is the whole design. Text only ever arrives one document at a time, so observing has to
be per-document; but a class with sixteen uploads has one Fourier series topic, not twelve, and one
grading scheme, not sixteen restatements of the course code.

Construction is therefore two phases: **observe**, once per document inside the ingestion job, and
**consolidate**, once per class after new observations land.

### Phase 1: Observe (per document, the `extracting` stage)

Runs after chunking, so a failure here never blocks the document from becoming searchable.

1. Send document text, truncated to the extraction budget, to the tutor model, together with the
   class's own name, code, and term, and the document type `detect_doc_type` decided
2. The model returns structured JSON, constrained by the schema for that document type where the
   endpoint supports `response_format`, at temperature 0
3. **Each entry's `quote` is checked against the text the model was shown.** Found means `high`;
   missing, too short, or paraphrased means `low`
4. Each item is **merged into the class profile** by the identity rule below, rather than inserted
   as a new row per document
5. The document is recorded in `profile_fact_sources` as evidence for each fact it attested

**The document type decides which fields are asked for.** This is the single most important thing
about the prompt and it is a subtraction, not an addition. A homework sheet's prompt does not contain
the string `professor_info` anywhere; a practice exam's contains neither that nor `deadlines`. Asking
a document for a field it cannot honestly fill is an instruction to go and find one, and a field list
outweighs any amount of prose asking for restraint. `EXTRACTION_PROFILES` in `llm/prompts.py` is the
table, and the schema, the worked example, and the field list are all generated from it, so they
cannot disagree.

| Document type | May be asked for |
|---------------|------------------|
| `syllabus` | topics, notes, deadlines, grading, professor, prerequisites |
| `homework`, `lab` | topics, notes, deadlines |
| `solutions`, `exam` | topics, notes |
| `lecture_notes`, `textbook`, `generic` | topics, notes |

`solutions` and `exam` are the two types that exist for this reason alone. They chunk like homework,
and they are read completely differently: an answer key and a practice midterm are the documents a
student is most likely to be holding a **reused** copy of, printed with another term's dates and
another instructor's name. Their prompts say so outright, and their schemas make it moot.

An unrecognised or missing document type gets the `generic` profile, which is the conservative one.
It used to get the permissive one: `textbook` and `generic` had no guidance at all and were asked for
all six kinds.

**Confidence is not the model's opinion of itself.** It used to be - the prompt asked for `high` or
`low` and the answer was believed - and that was the weakest link in the design, because `high` puts
a fact into every chat prompt with nobody having looked at it, and a small model marks everything
`high`. Every entry now carries a `quote`, the words in the document that state it, and
`confidence_for` decides the marking with a string match after normalising case, whitespace, and the
punctuation a PDF prints differently from how a model retypes it. A quote shorter than 12 characters
is not evidence and is refused. The check runs against the **truncated** text the model was actually
sent, never the whole file, so a sentence from a page nobody showed it cannot promote a guess.

**The extraction budget is `min(context_window * 0.6, 6000)` tokens.** The share alone tied every
upload's cost to a number chosen for chat: at a 262144-token window the stage sent 629,144 characters
per document, overran the client's 300-second read timeout, and so returned no facts at all, while
holding every queued upload behind it, since ingestion takes one document at a time. The cap is the
budget an 8192 window gives, which is what this prompt was written against. What extraction is
looking for is stated near the front of a syllabus rather than spread evenly through a hundred pages.

**Extraction is skipped entirely when the tutor endpoint is non-local and the user has not given the
one-time acknowledgement described in architecture.md.** It is never performed silently against a
remote endpoint. The same gate covers Phase 2, which sends fact labels to the same endpoint.

**What is a class fact.** The six kinds are defined by what a tutor would need to know. The `Is not`
column is now mostly enforced by the field list above rather than stated in prose, which is the
change that matters: what a per-document extractor gets wrong by default is not something it can be
talked out of.

| Kind | Is | Is not |
|------|----|--------|
| `topic` | subject matter, named as a textbook index would name it | the course title, a problem's phrasing, a whole sentence |
| `deadline` | a dated obligation | an assignment that carries no date |
| `grading` | anything that determines the grade | a rubric for one worksheet |
| `professor` | name, contact, office hours | the document's author line |
| `prerequisite` | knowledge or software the course assumes | a step in one lab's setup |
| `note` | a convention that holds across the course: a transform's sign or factor convention, required notation, a method the course requires or forbids | anything about the file: its title, type, assignment number, term, course code, or problem count |

`note` was the bucket everything unclassifiable fell into, which is what made a profile unreadable.
It is now the narrowest kind of the six and the most valuable one: a tutor that knows this course
writes the Fourier transform without the $1/2\pi$ out front will not quietly contradict the lectures.

**Extraction prompt:** see `_EXTRACTION_PROMPT` in `backend/llm/prompts.py`, which is the single copy.

### The identity rule

A fact is identified by `(class_id, kind, subject)`, where the subject is its label when the model
gave a real one and its value otherwise, normalized. Two observations with the same identity are the
same fact; the second one adds evidence rather than a row.

Normalization merges only what differs by **formatting**, never by wording:

- Unicode is NFKD-folded and combining marks dropped, so `naive` and `naïve` agree
- Case is folded, and every non-alphanumeric run collapses to a single space, so
  `Linearity and Time-Invariance` and `Linearity and Time Invariance` agree
- One trailing parenthetical is dropped, because it is a gloss on the subject rather than part of it:
  `Convolution Property (Periodic Convolution)` and `Convolution Property` agree

That line is deliberate. Formatting differences can be merged with certainty, so they are merged in
code where the result is free and deterministic. Wording differences (`Time Shift` against
`Time-Shift Property`) require judgment, so they wait for Phase 2. Nothing in Phase 1 can merge two
things a student would consider distinct.

Where several wordings collapse to one identity, the **shortest** is kept as the display label. Among
variants that already agree modulo formatting, the shortest is the canonical name:
`Fourier Transform` over `Fourier Transform computation (X(jω) and x(t))`.

### Phase 2: Consolidate (per class)

Runs at the end of ingestion, once, over the class's `topic`, `prerequisite`, and `note` facts. It
sends a numbered list of subjects, never document text, and asks for two judgments:

- **`duplicates`**: groups that name the same thing in different words. The losers' evidence moves to
  the winner and the loser rows are deleted.
- **`not_about_the_course`**: entries that describe a file rather than the course. These are
  **demoted to `low` confidence**, not deleted: they stay visible in the sheet, drop out of every
  prompt, and wait for the student to confirm or reject them.

Both judgments are conservative and bounded:

- A fact the user has confirmed, rejected, or corrected is never merged away and never demoted. The
  `edited` column exists for the third case, because correcting a value deliberately does not confirm it.
- Every number in the reply must be one that was sent. Anything else is dropped.
- The pass runs only when at least one fact is unconsolidated, so a re-upload that proposes nothing
  new costs nothing. `consolidated` on each fact is what records that.
- The entry list is capped, and a truncation is logged.

If the pass fails or answers unusably, the profile is still the deterministically merged one, which
is the guarantee that makes the model call optional rather than load-bearing.

### What reaches a prompt

`select_active_facts` is the single filter. A fact is active when it is not rejected **and** one of:

1. the user confirmed it,
2. its quote was found in the document it came from, or
3. **two or more distinct documents attested it.**

All three are forms of evidence, which is the point: none of them is the model vouching for itself.
A fact two documents state independently has been corroborated by the material; a fact whose quote
checks out has been corroborated by the page. The student can still reject any of them, which is
what keeps rule 3 safe.

Ordering is by evidence: within a kind, most-attested first. `_render_facts` then caps each kind, so
a class with ninety topics spends a bounded share of the system-prompt budget on the profile and
spends it on the topics the course actually revolves around. Retrieval carries the detail; the
profile is orientation.

### Cost

Phase 1 is a full-document pass through the tutor model on every upload, which on a local model can
take minutes for a long PDF. Phase 2 is one short pass over labels. Both run inside the ingestion job
with its own progress stage, and both are disabled together by the extraction switch in Settings.

### Pruning

Deleting a document withdraws its evidence. A fact left with no sources at all is deleted unless the
user has confirmed, rejected, or corrected it, so removing an upload removes what it alone claimed.

## Design Principles

1. **Local-first.** Text recognition, embedding, chunking, and storage never leave the machine. Only
   the tutor model may currently be remote, and only under the transitional constraints in
   architecture.md. Once inference is bundled in Phase 6, nothing leaves the machine at all.
2. **Compartmentalized.** Each stage is a discrete module behind a narrow interface. Note the honest
   limit: the specialist OCR path is not a drop-in OpenAI client, so the transcription interface has
   to be narrow enough that a general vision model and Unlimited-OCR both satisfy it. Page in, text
   out, with page-level progress. Anything richer leaks one implementation into the other.
3. **Semantic chunking.** Structure is respected, subject to the 1024-token hard ceiling.
4. **Lightweight context.** Retrieval is tight and targeted, budgeted for 8K to 32K windows.
5. **Class-scoped.** All retrieval is partitioned by class. No cross-class leakage until Phase 5.
6. **Proposal, not assertion.** Automatically extracted facts are proposals. A proposal becomes
   active on the user's confirmation, on a quote that was found in the source document, or on
   corroboration by a second document, and never on anything else. In particular, never on the
   model's own say-so: a claim it cannot point to in the text stays out of every prompt.
7. **A profile describes the course.** Documents are evidence for it, not sections of it. Anything
   true of one file and not of the class does not belong in the profile at all.

## Future Extensions

- Long-horizon multi-page OCR once R-SWA (#24975) lands upstream (Phase 6)
- Cross-class retrieval for prerequisite connections (Phase 5)
- Conversational RAG: use history to rewrite the retrieval query
- Citation links from a claim in a response back to the source page
- User-configurable embedding model, gated on the re-index flow above
