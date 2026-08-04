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

**Text recognition is not only for scanned pages.** PyMuPDF's text layer mangles dense mathematics,
producing garbled glyph sequences or dropping symbols. A vision pass may produce better equations
than the text layer does on pages that were never scanned. This is untested and cheap to test, and
if it holds it changes what transcription is for.

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

**Section hierarchy.**

- Primary source is the PDF outline via PyMuPDF `get_toc()`, which most commercial textbooks carry
- Fallback is heading detection from span-level font size and weight, which PyMuPDF also exposes
- Result is a hierarchical `section_path` on each chunk (`5 / 5.2 / 5.2.1`), replacing the current
  flat `section_title`

`section_path` is the change that matters. A homework problem reading "use the diagram from section
5.2.1" becomes a **direct lookup** rather than a semantic search that may or may not surface the
right page. That is the difference between reliable and lucky, and it is what makes the Phase 2
solver able to follow a textbook's own cross-references.

**Figures.**

- Embedded images via `get_images()`, or a rendered page region for figures that are drawn rather
  than embedded
- Caption-to-figure association by matching a caption pattern (`Figure 5.21`) against the nearest
  image block. This is a heuristic and will fail on some layouts, so it needs an honest fallback
  rather than a silently wrong association
- An extracted figure carries provenance back to its source page, so pulling it into a solution
  document cites correctly

Figures are the first pipeline output that is not text. The artifact model in architecture.md holds
mixed content from the start for this reason.

**Untested at scale.** Ingestion time, progress reporting, and retrieval quality at textbook scale
have not been measured. A 900-page book is roughly two orders of magnitude more chunks than a
syllabus, and while `sqlite-vec` brute-force KNN is comfortable there, local embedding throughput
during ingestion is the open question.

### Stage 2b: Text Recognition (Phase 3)

Everything from here to the end of this stage is Phase 3 scope. It runs behind the transcription
interface described at the top of this document, which the bundled vision model also implements.

**Scanned pages and images**
- Render to PNG at 300 DPI with PyMuPDF
- Run Unlimited-OCR (see below)
- Strip detection markers and regroup blocks
- Output clean text with block structure

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
2. Homework: split on problem markers (`1.`, `Problem 1`, `Q1`)
3. Textbook or notes: split on heading markers (`#`, `##`, numbered sections)
4. No structure detected: paragraph grouping with overlap

**Each chunk stores:** `chunk_id`, `document_id`, `class_id`, `content`, `token_count`, and metadata
(document type, page number, section title, `problem_number`, `part_index`).

`problem_number` and `part_index` are populated today and are the substrate the Phase 2 solver
segments against, so problem-level addressing is not new work in that phase. `section_title` becomes
the hierarchical `section_path` in Phase 3.

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

## Automatic Profile Extraction

Ingestion runs an analysis pass that **proposes** structured facts for the Class Profile. It runs as
the `extracting` stage of the ingestion job, after chunking, so a failure here never blocks the
document from becoming searchable.

1. Send document text, or a summary if it exceeds the extraction budget, to the tutor model
2. The model returns structured JSON
3. Facts are stored as individual rows with `confidence`, `confirmed`, and `source_document_id`
4. `confidence: high` facts become active context immediately
5. `confidence: low` facts are stored but **excluded from prompts until the user confirms them**

**Extraction is skipped entirely when the tutor endpoint is non-local and the user has not given the
one-time acknowledgement described in architecture.md.** It is never performed silently against a
remote endpoint.

**Extraction prompt:**
```
You are analyzing a course document. Extract the following structured information.
Only extract facts that are explicitly stated. Do not infer or guess.
Mark any field you are not certain about with confidence "low".
Return JSON with these fields: deadlines[], topics[], professor_info{}, grading{},
prerequisites[], notes[]
```

Extraction is a real cost: it is a full-document pass through the tutor model on every upload, which
on a local model can take minutes for a long PDF. It therefore runs inside the ingestion job with
its own progress stage and can be disabled in Settings.

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
6. **Proposal, not assertion.** Automatically extracted facts are proposals until confirmed.

## Future Extensions

- Structure-aware retrieval resolving an explicit section reference in a problem (Phase 3)
- Long-horizon multi-page OCR once R-SWA (#24975) lands upstream (Phase 6)
- Cross-class retrieval for prerequisite connections (Phase 5)
- Hybrid retrieval: vector search combined with keyword BM25
- Conversational RAG: use history to rewrite the retrieval query
- Citation links from a claim in a response back to the source page
- User-configurable embedding model, gated on the re-index flow above
