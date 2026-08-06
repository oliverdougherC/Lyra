# RAG Pipeline Specification

## Overview

The RAG pipeline transforms raw course materials into retrievable, class-scoped knowledge. It is the
core differentiator of Lyra.

Three models are involved, with strictly separate roles:

| Role | Model | Runtime | Phase | Configurable |
|------|-------|---------|-------|--------------|
| Embedding | `nomic-embed-text-v1.5` (GGUF) | llama.cpp, local | 1 | No |
| Tutor | user's choice, then bundled | OpenAI-compatible endpoint | 1, bundled in 6 | Until Phase 6 |
| Text recognition | bundled vision model, then `baidu/Unlimited-OCR` | llama.cpp, local | 3 | No |

Embedding and text recognition are infrastructure and always run locally. The tutor model is the
product. See the Inference Posture section of [architecture.md](architecture.md) for the rules
governing the transitional period before inference is bundled.

**Text recognition is Phase 3 and is no longer gating.** Phase 1 and 2 accept text-based PDFs, TXT,
and MD only. The OCR specification below is retained in full because the research behind it is
load-bearing and was expensive to establish.

What changed: a vision-capable tutor model is now assumed, and it can transcribe pages itself.
Bulk transcription therefore sits behind an interface with two implementations, and the choice
between them is a measurement rather than a prerequisite:

| Path | Cost | When it wins |
|------|------|--------------|
| Bundled general vision model | No extra download, slower per page, weaker on dense layout and math | Homework and short scans, most of what users upload |
| `Unlimited-OCR` specialist | Extra weights to ship and manage, far faster per page, better on multi-column text, tables, and math | Textbook-scale bulk ingestion, where throughput decides whether the feature is viable |

Build the interface, wire the general model first, then time a real sample of pages and extrapolate
to textbook scale. If bulk transcription is intolerable on modest hardware, the specialist earns its
download. This removes the serving spike from the critical path without pretending it is settled.

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

## Pipeline Stages

```
Upload -> Parse -> Chunk -> Embed -> Store -> Retrieve -> Generate
```

Stages 2 through 5 run in a background ingestion job. Upload returns immediately.

### Stage 1: Upload

**Currently accepted formats:**
- PDF, text-based
- Plain text (TXT, MD)

**Phase 3 adds:** scanned PDFs and images (PNG, JPG, WebP).
**Later:** Office documents (DOCX, PPTX).

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
text recognition, which is coming, and that the file has been kept so it can be processed later
without re-uploading. Phase 3 re-ingests these documents in place.

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
- Fallback is heading detection from span-level font size and weight, which PyMuPDF also exposes
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

- Embedded images via `get_images()`, or a rendered page region for figures that are drawn rather
  than embedded
- Caption-to-figure association by matching a caption pattern (`Figure 5.21`) against the nearest
  image block. This is a heuristic and will fail on some layouts, so it needs an honest fallback
  rather than a silently wrong association
- An extracted figure carries provenance back to its source page, so pulling it into a solution
  document cites correctly

Figures are the first pipeline output that is not text. The artifact model in architecture.md holds
mixed content from the start for this reason, and `artifact_parts` already accepts `kind = 'figure'`
with `content_type = 'image'`, so this lands without a migration.

Geometry follows the convention `rag/locate.py` and `artifact_provenance.bbox` already established:
fractions of the page box rather than points, because pages render as images at whatever width the
pane has.

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

Everything from here to the end of this stage is Phase 3 scope. It runs behind the transcription
interface described at the top of this document, which the general vision model also implements.

**Scanned pages and images**
- Render to PNG at 300 DPI with PyMuPDF
- Run Unlimited-OCR (see below)
- Strip detection markers and regroup blocks
- Output clean text with block structure

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

#### Per-page state has nowhere to live yet

Per-page progress and per-page retry are both listed as Phase 3 scope, and neither is possible
against the current schema. `documents` carries `pages_total`, `pages_done`, and `pages_skipped`,
and `pages_done` is written exactly once, at the end of a run: ui-phase-1.md documents this and
deliberately shows a page count rather than a counter that would never move.

Recognition changes that. A page is now a unit of work that can succeed, fail, or be retried on its
own, which is a row rather than a column. This stage therefore introduces per-page rows carrying the
page number, its state, and its error, and `pages_done` becomes a count over them rather than a
number written at the end. A document whose page 7 failed is a document with 39 good pages and one
retry, not a failed document.

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
| R-SWA in the decoder | [#24975](https://github.com/ggml-org/llama.cpp/pull/24975) | Open | Decoder runs full multi-head attention. The near-constant memory that makes 40-plus-page single-pass parsing viable is lost, so long contexts grow memory and slow down sharply. |
| `max_tiles` fix | [#25614](https://github.com/ggml-org/llama.cpp/pull/25614) | Open | The projector's `preproc_max_tiles = 32` is ignored and llama.cpp falls back to DeepSeek-OCR v1's cap of 9. Tall or dense pages are split into a coarser tile grid than the reference, reducing accuracy. |

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
quality-equivalent text with the chat template applied. If llama-server cannot serve this model
correctly, fall back to a single long-lived `llama-mtmd-cli` invocation per document batch and accept
the reload cost. Record the outcome in this document.

This spike now gates **only the specialist path**, not the phase. Transcription through the bundled
vision model needs none of it, so Phase 3 can deliver scanned-document support and be measured
before anyone touches a pinned llama.cpp build.

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
| Textbook | Section or subsection | 2000 tokens | 100 tokens |
| Lecture notes | Topic heading | 1500 tokens | 75 tokens |
| Syllabus | Logical section | 1000 tokens | 50 tokens |
| Generic | Paragraph group | 1500 tokens | 100 tokens |

**Hard ceiling: 2048 tokens per chunk, enforced for every type after strategy-specific splitting.**
No chunk is ever stored above this. The ceiling exists for two reasons: retrieval budgeting assumes
small targeted chunks, and an oversized chunk would otherwise be silently truncated at embedding
time.

**Oversized homework problems.** A single problem above the ceiling is split in this order, stopping
as soon as it fits: on lettered or numbered sub-parts (`(a)`, `(b)`, `i.`, `ii.`), then on
paragraphs with 100-token overlap. Every resulting chunk keeps the same `problem_number` and gains a
`part_index`, so retrieval can reassemble the full problem when any part matches.

**Detection order:**
1. Detect document type from filename patterns and content heuristics
2. Textbook: structural signals, principally a PDF outline with real depth over a long document
3. Homework: split on problem markers (`1.`, `Problem 1`, `Q1`)
4. Textbook or notes: split on heading markers (`#`, `##`, numbered sections)
5. No structure detected: paragraph grouping with overlap

Step 2 does not exist yet, and its absence is why the reference textbook in Stage 2a is chunked as
homework. It is placed above homework deliberately: the two are separated by structure rather than
by markers, and a book of exercises will always out-vote a marker count.

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

**Model:** `nomic-embed-text-v1.5` GGUF, 137M parameters, 768 dimensions, 8192 max input tokens.
Served locally by llama.cpp. Using llama.cpp rather than sentence-transformers keeps PyTorch out of
the product entirely and reuses the runtime already required for OCR.

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
2. Exact KNN over the class partition, `k = 8`
3. Apply recency weighting
4. Trim to the retrieval budget
5. Expand any matched homework part back to its sibling parts where the budget allows

**Recency weighting.** Cosine distance is adjusted by a bounded recency bonus so that newer material
wins ties without displacing a clearly better match:

```
score = cosine_similarity + 0.05 * recency_factor
```

where `recency_factor` decays linearly from 1.0 for a document uploaded today to 0.0 at 120 days.
The 0.05 coefficient is deliberately smaller than meaningful similarity gaps; it breaks ties, it does
not reorder strong matches.

**Structural lookup, added in Phase 3.** A query that names a section is not a similarity problem
and must not be answered with one. When a query carries an explicit reference (`section 5.2.1`,
`Chapter 4`, `Theorem 2.63`), the matching `section_path` is resolved directly and those chunks are
placed ahead of the KNN result rather than left to compete with it on cosine distance. The KNN still
runs and still fills the remaining budget, because a section reference tells you where to look and
not what the student needs from it.

Three rules keep this from becoming a worse retrieval than the one it improves:

- A reference that resolves to nothing falls through to the KNN silently. A student may cite a
  section of a book they never uploaded, and a hard failure there would be a regression
- A resolved section larger than the budget is trimmed by KNN score within the section, so the part
  of section 5.2 that answers the question outranks the part that does not
- Structural chunks are still labelled with their source in the context block, so a step grounded in
  a looked-up section carries the same provenance as one grounded in a retrieved one

**`k = 8` is a Phase 1 constant and is unmeasured.** It was chosen for chat turns over syllabi, and
the Phase 2 handoff already records it as a known weakness in solving. Textbook-scale retrieval is
the first thing that can measure it honestly, so Phase 3 measures it rather than adjusting it by
feel. Nothing else in this document assumes the number stays at 8.

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
2. The model returns structured JSON
3. Each item is **merged into the class profile** by the identity rule below, rather than inserted
   as a new row per document
4. The document is recorded in `profile_fact_sources` as evidence for each fact it attested

**The extraction budget is `min(context_window * 0.6, 6000)` tokens.** The share alone tied every
upload's cost to a number chosen for chat: at a 262144-token window the stage sent 629,144 characters
per document, overran the client's 300-second read timeout, and so returned no facts at all, while
holding every queued upload behind it, since ingestion takes one document at a time. The cap is the
budget an 8192 window gives, which is what this prompt was written against. What extraction is
looking for is stated near the front of a syllabus rather than spread evenly through a hundred pages.

**Extraction is skipped entirely when the tutor endpoint is non-local and the user has not given the
one-time acknowledgement described in architecture.md.** It is never performed silently against a
remote endpoint. The same gate covers Phase 2, which sends fact labels to the same endpoint.

**What is a class fact.** The six kinds are defined by what a tutor would need to know, and the
prompt names what to leave out as explicitly as what to collect. The exclusions matter more than the
inclusions, because they are what a per-document extractor gets wrong by default:

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
2. the model marked it `high`, or
3. **two or more distinct documents attested it.**

The third rule is new and follows from evidence being tracked at all. A fact that two documents
state independently has been corroborated, and corroboration is evidence in the same way a model's
own `high` marking is. The student can still reject it, which is what keeps rule 3 safe.

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
3. **Semantic chunking.** Structure is respected, subject to the 2048-token hard ceiling.
4. **Lightweight context.** Retrieval is tight and targeted, budgeted for 8K to 32K windows.
5. **Class-scoped.** All retrieval is partitioned by class. No cross-class leakage until Phase 5.
6. **Proposal, not assertion.** Automatically extracted facts are proposals. A proposal becomes
   active on the user's confirmation, on the model's own `high` marking, or on corroboration by a
   second document, and never on anything else.
7. **A profile describes the course.** Documents are evidence for it, not sections of it. Anything
   true of one file and not of the class does not belong in the profile at all.

## Future Extensions

- Structure-aware retrieval resolving an explicit section reference in a problem (Phase 3)
- Long-horizon multi-page OCR once R-SWA (#24975) lands upstream (Phase 6)
- Cross-class retrieval for prerequisite connections (Phase 5)
- Hybrid retrieval: vector search combined with keyword BM25
- Conversational RAG: use history to rewrite the retrieval query
- Citation links from a claim in a response back to the source page
- User-configurable embedding model, gated on the re-index flow above
