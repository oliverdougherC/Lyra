# Feature Roadmap

Phased feature plan for Lyra. Each phase must be polished and stable before the next begins. No
phase is complete until its core flow works without caveats.

**What Lyra is.** A convenience tool built by a student for students. It exists to accelerate the
way people actually work, not the way they are nominally supposed to. That principle decides design
arguments: when a feature could either enforce good pedagogy or remove friction, Lyra removes the
friction and trusts the user. The Guide, Show, and Solve ladder is the shape this takes in the
product, and Solve is a real rung on it rather than an escape hatch bolted to the side.

**Scope principle.** One feature, deeply polished, before the next. A phase closes when its flow
works end to end with no caveats, not when its code exists.

## Phase 0: Foundation Cleanup

The repository previously held a different product, a Jan-derived Tauri writing app. That
scaffolding had to go before new code landed, otherwise it silently misconfigured the new stack.

- [x] Initialize version control and commit a baseline
- [x] Remove stale scaffolding: Tauri Cargo config, the Jan devcontainer, the Tauri macOS release
      workflow, the Yarn install state, and the husky hook that ran a script with no manifest
- [x] Replace `.prettierrc` with the config in conventions.md
- [x] Retarget dependabot at the real project structure
- [x] Add `pyproject.toml` with the Ruff and pytest configuration from conventions.md
- [x] Add `frontend/package.json`, Tailwind v4, and shadcn/ui with the token bridge in `globals.css`

Phase 0 is complete.

## Phase 1: Foundation (MVP)

**Goal:** Upload text documents, get contextual AI help about them, in an interface that already
feels finished.

Phase 1 deliberately excluded OCR. Scanned documents were the single largest source of technical
risk in the pipeline, and they were not required to prove the product. The effort freed by that cut
went into the interface, which is a stated core pillar and the thing a user actually judges. See
[ui-phase-1.md](ui-phase-1.md) for the screen-level specification.

### Scaffolding

- [x] FastAPI backend bound to `127.0.0.1`, CORS restricted to the local frontend origin
- [x] SQLite schema and migrations
- [x] Next.js frontend shell with the Lyra tokens and shadcn bridge in `globals.css`
- [x] `scripts/dev` and `scripts/start` launching both processes

### Settings

- [x] Tutor endpoint configuration, API key stored in the OS keychain
- [x] Test-connection action that validates the endpoint and lists available models
- [x] Model selection populated from the endpoint
- [x] Endpoint locality indicator, non-local warning, and one-time acknowledgement
- [x] Theme control: system, light, dark

### Class workspace

- [x] Create a class (name, code, semester)
- [x] Class list on the home page
- [x] Workspace view with document sidebar and chat area
- [x] Rename a class, and delete a class with everything it owns
- [x] Archive and restore a class, keeping its data intact
- [x] A class hub at `/classes/[id]`: its conversations, solution sets, documents, and profile in one
      place, with every action that belongs to the class. Clicking a class used to open a
      conversation, which made the class the chat and left everything else reachable only from the
      sidebar, where it could be opened but never managed
- [x] Rename or delete a conversation, and rename or delete a solution set
- [x] Move a document to another class, which re-indexes it there and withdraws the facts it alone
      supported from the class it left

### Document upload and ingestion

- [x] Drag-and-drop upload returning `202`
- [x] Supported in this phase: text-based PDF, TXT, MD
- [x] Text extraction with PyMuPDF, preserving page numbers
- [x] Scanned-page detection that produces a clear `unsupported` state rather than a silent empty
      document, offering to keep the file for later
- [x] Background ingestion job with the documented state machine and a polled status endpoint
- [x] Semantic chunking with the 2048-token ceiling
- [x] Local embeddings with mandatory `search_document: ` prefixes
- [x] Vector storage in `sqlite-vec`, with the embedding-model identity recorded
- [x] Delete a document and its chunks
- [x] Recursive folder upload with a batch loader reporting stage and progress

### Contextual chat

- [x] Sessions, message history, and streaming replies over SSE
- [x] Retrieved context injected from the class's documents
- [x] Guide and Show toggle (Socratic versus direct explanation)
- [x] Markdown rendering, incremental during streaming, with math and code blocks
- [x] Indicator when retrieval was heavily trimmed
- [x] Stop generation, and retry a failed turn as a re-answer rather than a re-ask
- [x] Reasoning-model support: thoughts streamed and stored separately from the reply, closed by
      default, with the elapsed duration persisted
- [x] Conversations named from their first message and addressable at `?session={n}`
- [x] Scope retrieval to a single selected document for the next turn

### Automatic profile extraction, proposal-only

- [x] Analysis pass as the `extracting` ingestion stage
- [x] Facts stored with `confidence`, `confirmed`, and source document
- [x] High-confidence facts injected as context; low-confidence facts withheld until confirmed
- [x] Skipped against a non-local endpoint without acknowledgement

### Class profile view

- [x] Facts grouped by kind, each showing its source document
- [x] Confirm or reject low-confidence facts, and correct a wrong value

### Interface and visual quality

Specified screen by screen in [ui-phase-1.md](ui-phase-1.md) and part of the definition of done.

- [x] Every screen implements all four data states: loading, empty, error, and populated
- [x] Skeletons match the real layout; no spinners for page or list loads
- [x] `prefers-reduced-motion` honored in CSS and in Framer Motion variants
- [x] Dark mode complete, including code and math rendering
- [x] No hardcoded colors; every surface uses a token

### Phase 1 closeout

**Phase 1 is complete.** Both suites pass: 163 backend tests and 146 frontend tests. The items
below were the gap between "the code exists" and "the phase is closed", and all of them are
verification that could not be done by reading code.

One known limit, recorded rather than hidden: the end-to-end run covered short documents. How
ingestion and retrieval behave on a 900-page textbook is genuinely unknown, and closing it is
Phase 3 work. No Phase 1 claim depends on it.

- [x] Frontend test suite. Vitest with Testing Library, 146 tests over the modules that carry
      real logic: markdown normalization, the API client and its SSE frame parsing, theme and
      local-storage state, the reasoning trace, the streaming renderer, and the document hooks.
      The vendored shadcn primitives in `components/ui/` are deliberately not covered
- [x] Contrast contracts verified by recomputing every recorded pair from `globals.css` rather
      than by eye. All 13 pairs match the documented ratio in both themes, and each clears its
      floor. The check is scripted, so it can be re-run whenever a token moves
- [x] Breakpoints verified at 1280, 768, and 375. Desktop shows the two-pane workbench; 768
      collapses to Chat and Documents line tabs defaulting to Chat; 375 folds the ancestor
      breadcrumb and moves navigation to the floating bottom shelf. No horizontal body scroll at
      any width
- [x] Keyboard map verified end to end: skip-to-content is the first focusable element and is
      hidden until focused, Cmd+K focuses the composer, Cmd+N opens the class dialog, Cmd+B
      toggles the rail, Escape closes an overlay, and focus is trapped inside a dialog and
      restored to its trigger on close
- [x] `:focus-visible` rings verified on every interactive element in the shell. Two controls, the
      skip link and the header breadcrumb, were falling back to the browser default 1px outline
      instead of the documented 2px ring with 2px offset; both now carry the standard ring
- [x] Dark mode verified for the full workspace, including KaTeX math and document rows. Console
      is clean on load and after interaction: no errors, no warnings
- [x] Recorded end-to-end run of the definition of done below, against the baseline model. The
      full flow works: create a class, upload, watch ingestion finish, ask, and get an answer
      grounded in the uploaded material. Exercised on short documents only. Textbook-scale
      ingestion and retrieval are deliberately untested here and are Phase 3 work, so nothing in
      Phase 1 rests on them

**Definition of done:** A student creates a class, uploads a text-based homework PDF, watches
ingestion finish, asks about a specific problem, and gets an answer that references the uploaded
material. Extracted syllabus facts are visible and correctable. Dropping a scanned PDF produces an
honest, actionable message. Every screen is keyboard-navigable, responsive, correct in both themes,
and free of placeholder styling.

## Phase 2: Homework Solver

**Goal:** Stop being read-only. Upload a homework set and get back a complete, checked, editable set
of solutions that follow the method the course actually teaches.

This is the phase that makes Lyra something other than a chat window pointed at your documents. It
is also where the Solve rung of the Guide/Show/Solve ladder lands: clicking any step of a solution
to ask about it drops into Guide on that step, so the solver and the conversation are one product at
two altitudes rather than two features.

Specified in [solver-phase-2.md](solver-phase-2.md) for the data model, job architecture, and
verification, and in [ui-phase-2.md](ui-phase-2.md) screen by screen. Four decisions the roadmap
left open are settled there: the segmentation review is a blocking gate rather than a correction
after the fact; verification is a separate pass with the tools attached rather than tools available
during solving; reference solutions are designated per solve run rather than by a role on the
document; and the build order puts the shared substrate first. The checklist below is the scope; the
specs are the source of truth for how.

### Shared substrate

Two pieces are built here because the solver is their first consumer, but both are general and
Phase 4 depends on them. They are called out separately so the dependency is visible and so neither
gets designed solely around homework.

- [x] **Artifact model.** Lyra currently has inputs (documents), a derived index (chunks), a
      transcript (messages), and claims about a class (profile facts). It has nothing representing
      a thing Lyra produced that the user keeps, edits, and returns to. An artifact holds mixed
      content (prose, math, and images), carries provenance back to the chunks and pages that
      informed it, has a status, and supports revision at the level of its parts. `profile_facts`
      is this pattern in miniature and is the model to generalize from
- [x] **Tool-calling loop.** Send messages plus tool definitions, execute returned calls, append
      results, repeat to a bounded depth. Built in-house against the existing LLM client rather
      than forked: the tool surface is small, and what agent frameworks provide beyond it is
      multi-provider abstraction, plugin systems, and state management we already have
- [x] Tool calls are visible in the transcript, never silent
- [x] Termination guarantees: call-depth ceiling, timeout, and honest reporting when a loop is cut

### Problem segmentation

- [x] Identify each problem and sub-part in an uploaded homework set. `chunks.problem_number` and
      `chunks.part_index` already exist and are populated by the chunker
- [x] Handle sets that span multiple uploaded files
- [x] Present the segmentation before solving, so a missed or merged problem is correctable

### Solving

- [x] Per-problem solution generation grounded in retrieved course material
- [x] Method alignment: prefer the approach the course teaches over the approach the model prefers,
      retrieved from lecture notes and textbook sections
- [x] Optional professor solutions as few-shot examples for notation, style, and method. Where a
      student has last term's solutions, this is the strongest available signal and the clearest
      advantage over pasting a problem into a general chatbot
- [x] Solutions are structured by step, because steps are what the user clicks, asks about, and
      regenerates
- [x] Per-problem regeneration, including a correction supplied by the user

### Verification

Accuracy is the entire value proposition. A confidently wrong solution is worse than none.

- [x] **Computer algebra tool.** SymPy through the tool loop: symbolic integration and
      differentiation, equation solving, linear algebra, exact arithmetic. This is deterministic
      verification rather than a second opinion, and it is the single highest-value check available
      for the math and engineering work Lyra is best at
- [x] **Unit and dimensional checking.** Cheap, and it catches a large share of physics and
      engineering errors
- [x] Verify a finished solution in a second pass with the tools attached, re-deriving once on a
      refutation. The tool calls the verifier makes **are** the checkable claims, so there is no
      separate claim-extraction format to keep in sync with the tool set
- [x] Grounding surfaced per step, distinguishing steps grounded in retrieved course material from
      steps the model supplied on its own. Provenance rather than a score: there is no confidence
      percentage anywhere, because a number nobody can audit reads as precision that does not exist

Deliberately not used for verification: web lookup of existing answers. Answer sites are paywalled
and hostile to fetching, matching a problem across textbook editions is its own hard problem, and a
wrong answer retrieved from the web is more dangerous than the model's own wrong answer because it
arrives with borrowed authority and the model will defer to it. Web search earns its place looking
up unfamiliar methods, which is Phase 4.

Self-critique is kept because it is nearly free, but it is not the safety net. Models ratify their
own work.

### Solver workspace

- [x] Dedicated route opening on a drop target for one or more homework PDFs
- [x] Side-by-side source document and solution document, with problem-level anchoring in both
      directions. Overlaying solutions on the PDF stays open as a later refinement; anchored
      side-by-side is a fraction of the work for most of the value
- [x] Click any step to ask about it, which opens a Guide-mode exchange scoped to that step
- [x] Mark a solution wrong and have that problem re-solved with the correction as input
- [x] Stage display driven by real backend events. Built in-house rather than adopting an
      off-the-shelf loading component, both for token control and because canned components narrate
      fixed sequences on a timer, which would violate the honesty principle in ui-phase-1.md
- [x] Export to PDF

### Architecture note

The solver is **ingestion-shaped, not chat-shaped**. A full problem set with verification passes can
run for tens of minutes on local hardware, so it must be a background job with a polled status
endpoint that survives the tab closing, following the pattern already proven by document ingestion.
Per-problem results are written as they complete, never buffered to the end.

**Deferred within this phase:** figure extraction into solutions, which needs the structural work in
Phase 3, and web method lookup, which needs the tools in Phase 4. Both make the solver better; the
phase closes without them.

**Definition of done:** A student uploads a problem set, watches it segment and solve, reads
solutions that use the method their course teaches, catches a wrong answer and has that one problem
re-solved, asks a clarifying question about a single step, and exports the result.

Phase 2 is complete, and it has been measured against a whole course rather than one sheet: eight
ECE 203 problem sets with the professor's answer keys, through `scripts/eval_solver.py`.
Segmentation finds the right number of problems on all eight, stably across repeated runs, having
found it on six of eight before that evaluation surfaced what was wrong. Two sets solved end to end
without their keys attached produced twelve of twelve answers that agree with those keys, nineteen
results once sub-parts are counted, verified off 112 computer algebra calls.

[phase-2-handoff.md](phase-2-handoff.md) records the faults that evaluation found, what is still
weak, and how to re-run it. Two things a reader of this file should know: retrieval is class-scoped,
so a student who has uploaded the professor's solutions to the set being solved will see the solver
ground its steps in them and the provenance line names the file, which is visible rather than
hidden; and nothing in those twelve problems was wrong, so the re-derive-on-refutation path has been
exercised by the test suite and not yet by a real mistake.

## Phase 3: Large Documents

**Goal:** Make a textbook as useful as a syllabus, and accept the documents Phase 1 rejects.

These are two problems that are easy to conflate. Retrieval quality over a 900-page book is a
problem that exists **today**, for text-based PDFs, and has nothing to do with scanning. Reading
scanned pages is a separate capability. The structural work is the more valuable of the two and has
no external dependencies, so it comes first.

Specified in [rag-pipeline.md](rag-pipeline.md) stages 2a, 2b, 3, and 6 for the pipeline, and in
[ui-phase-3.md](ui-phase-3.md) screen by screen. The checklist below is the scope; those documents
are the source of truth for how.

[phase-3-handoff.md](phase-3-handoff.md) records what the phase measured, the four items still
open, and the traps that cost real time while building it. Read it before picking any of them up:
two of the four did not exist when this checklist was written, and three of the traps fail
silently.

### What measuring the reference book found, before anything was planned

Phase 2 was specified against a real course rather than its own fixtures, and four faults surfaced
that no test suite could have found. Phase 3 opened the same way, against Kuttler's *Linear Algebra:
A First Course*: 608 pages, a 131-entry outline nested four levels deep. Four findings, and they
decide the order of everything below.

1. **Ingestion performance at textbook scale is a non-issue, and was the phase's stated open
   question.** 0.8 s to parse 608 pages, 25 ms per chunk to embed, so the whole book indexes in
   well under a minute. No batching or parallelism work is justified.
2. **A textbook is chunked as homework today.** `detect_doc_type` has no textbook rule at all, so a
   book of numbered exercises trips the problem-marker heuristic and is cut at every numbered line
   in it: 1312 fragments averaging 162 tokens, none carrying any structural metadata. This is worse
   than the "flat chunking retrieves poorly" this phase was written to fix.
3. **The heading regex cannot be promoted.** Forced over the book it labels 595 of 596 chunks, and
   the labels are things like `3 times the second row to the first row.`, `Sn`, and
   table-of-contents dot leaders. High coverage of wrong values is worse than none.
4. **The text layer loses the mathematics on pages that were never scanned.** Every matrix extracts
   as a column of loose digits with its shape discarded. In a linear algebra textbook that is most
   of the content, and Lyra currently reports those pages as ingested successfully.

Finding 4 is the one that moves an item between tracks. A vision pass over pages that already have a
text layer is a **quality** feature for every document rather than a scanned-document feature, so it
is measured before bulk transcription rather than after it.

### Build order

Sequenced because the tracks have different risk, not because the checklists are ordered.

| Step | What | Why here |
| ---- | ---- | -------- |
| 1 | `scripts/eval_ingest.py`, and a measurement | Nothing below is claimable without it |
| 2 | Structural parsing | No external dependency, and finding 2 makes it the largest single win |
| 3 | Vision against the text layer | Finding 4, and it sizes the whole recognition track |
| 4 | Text recognition for scanned pages | Depends on step 3's interface |
| 5 | The document-list interface | Steps 2 to 4 shipped capabilities with no way to reach them |
| 6 | Figures | Depends on step 2's structure, and unblocks a known Phase 2 fault |

Step 5 was added after step 4 rather than planned. The trigger was concrete: the `unsupported`
popover still told a student that scans would be readable "in a future update" and would "process
automatically then", and both halves had become false, the second in a way that would make someone
wait forever. Recognition, image upload, and the vision readout had all shipped with no affordance
at all. Its screens are the document list; figures land in the solution workspace, so doing this
one first costs no second pass over the same components.

### Measurement

- [x] `scripts/eval_ingest.py`, the sibling of `eval_solver.py`: staged, resumable, with its own
      workspace database, ingest and retrieve and report. The Phase 2 harness is the model, and the
      reason is the same: a result from the real code path is evidence about the product rather
      than about the harness
- [x] A retrieval question set with known section answers, written by hand from the reference book,
      so structure-aware retrieval can be shown to work rather than asserted to. Seventeen
      questions in `scripts/eval_questions/`, both controls verified absent from the book
- [x] Record what `extract_facts` costs on a 608-page document. It is one model call per document
      and the one ingestion stage that does not scale with chunk count
- [x] Measure `k = 8`. It is a Phase 1 constant chosen for chat turns over syllabi, the Phase 2
      handoff already lists it as a known weakness in solving, and textbook-scale retrieval is the
      first thing that can judge it honestly

**What it recorded.** The whole book ingests in 52.8 s: parse 0.85 s, chunk 0.01 s, embed 18.3 s,
extract 20.7 s, consolidate 12.7 s. The profile pass is 63 percent of that, more than the pipeline
it runs after, and it is the only stage whose cost does not scale with chunk count. Nothing about
ingestion at this scale needs performance work; if anything here is ever worth attention it is the
profile pass, and only because a book teaches a class almost nothing that a syllabus does not.

Retrieval, before any Phase 3 work, finds the right section for 14 of 17 questions inside `k = 8`
and 11 of 17 at rank 1. Widening to 32 gains two, both of which are the bare section references
below, so **`k = 8` is not what is limiting this** and there is no case for raising it. The failures
are chunks that do not rank, not neighbours that were not asked for.

Three questions miss, and they are the targets for the structural work rather than a verdict on it:

| Question | Rank | What it says |
| --- | --- | --- |
| `bare-section-reference` | 12 | "What does section 2.2 cover?" has no vocabulary to match on |
| `bare-chapter-reference` | 23 | The same, worse |
| `well-ordering` | never found | Real content, in an appendix, scoring below both controls |

Both bare references score **below the controls**, which are questions about material the book does
not contain at all. A section number is a fact on the page rather than a similarity, so no embedding
improvement reaches them and structure-aware retrieval is the only thing that will.

**Two faults surfaced on the first run**, in the pattern Phase 2 established, and both are fixed. A
chunk ceiling of 2048 measured in estimated tokens against a real limit of 2048 real tokens, which
gave it no headroom and failed the whole document on five chunks out of 1312. And a sibling
expansion that read `problem_number` as unique within a document, so one hit emitted 120 unrelated
chunks at an identical score and the ranking was gone. Neither could have been found by a fixture.

### Structural parsing

- [x] **A textbook detection rule in `detect_doc_type`.** Structural signals, principally a PDF
      outline with real depth over a long document, checked ahead of the homework marker heuristic.
      Highest value per line in the phase, and finding 2 above is why
- [x] Chapter and section hierarchy from the PDF outline (`get_toc()`), which most commercial
      textbooks carry
- [x] Hierarchical `section_path` on chunks, replacing the flat `section_title`. This is the change
      that turns "the diagram in section 5.2.1" into a direct lookup instead of a semantic search,
      which is the difference between reliable and lucky. Built from outline titles, with section
      numbers recovered from page text where they exist: the reference book's outline carries no
      numbers, so a numeric path would be null on exactly the books this exists for
- [x] Structure-aware retrieval that can resolve an explicit section reference in a problem, placed
      ahead of the KNN rather than competing with it, and falling through silently when it resolves
      to nothing
- [x] Documents ingested before this lands keep a null `section_path` and are offered a re-index
      rather than having one run for them. A path is derived from the source file's outline, so
      there is nothing in the database to backfill from. The `Reindex` action the document row
      already carried is that affordance, and the outline disclosure beside it is how a student
      finds out it is worth using
- [ ] Font and weight heading detection as the fallback for a document with no outline. Deferred
      rather than dropped: nothing measured needs it, because a document with no outline is a
      syllabus or a sheet where the existing regex is doing an easier job well enough. It earns its
      place when a book without an outline turns up

**What it changed, on the same seventeen questions.** Retrieval goes from 14 of 17 inside `k = 8` to
**17 of 17**, and from 11 to 16 at rank 1. The three that failed all pass:

| Question | Before | After |
| --- | --- | --- |
| `bare-section-reference` | 12 | 1 |
| `bare-chapter-reference` | 23 | 1 |
| `well-ordering` | never found | 1 |

The two bare references are the lookup doing the work rather than the embedding: they land on pages
111 to 113 and 194 to 196 at cosine similarities of 0.48 and 0.53, well below the controls, which is
exactly the case similarity search cannot reach. `well-ordering` is the chunking, not the lookup,
since its question names no section.

The book now chunks as 596 sections rather than 1312 fragments, mean 358 tokens rather than 161, and
carries 105 distinct real section titles where the regex produced dot leaders and `Sn`.

### Figures

- [x] Extract embedded images with PyMuPDF, cropped out of the composed page. **Not rendered page
      regions**, and that is measured: `cluster_drawings()` turns 2522 vector paths into 112
      clusters on the reference lecture deck and every cluster is the whole page, because each page
      has a full-bleed background rectangle. Three document kinds, three page sizes, same result.
      Shipping it would file one junk figure per page of every deck a student owns
- [x] Caption-to-figure association, with an honest fallback when there is no caption. The fallback
      is the common path rather than the exception: the corpus holds five captions across
      sixty-nine figures. All five are found; the other sixty-four are named for where they were
      found and given no owner
- [x] Pull a referenced figure into a solution document, with provenance back to its source page.
      **Only when the reference is exact**: the problem names the figure, or the problem is alone on
      its page. See below for what that does not cover
- [x] Figures survive export. Kept whole with their caption and capped in height, so a diagram
      cannot claim a page and push the working it belongs to onto the next

**What the figure acceptance case found, and then delivered.** The three block diagrams on
`homework_3` extract correctly, crop cleanly at 220 dpi, serve, render, and print. Attaching them to
the right three problems took two attempts. The first implementation attached every figure on a page
to every problem on it and produced twenty-one attachments of which twelve were wrong: four
Fourier-series problems each received three diagrams belonging to other questions. Every *geometric*
rule tried is wrong on one of the two common layouts, because on that sheet the list markers sit
below their diagrams.

The exact rule - pairing an alternating run of figures and markers - needed each problem to have a
distinct page position, which `locate` now gives it. Stated as it actually has to be stated: every
figure must have a problem marker immediately beside it, all of them on the same side, no two
wanting the same marker, and the page must carry at least two figures. The "equal in number"
condition this document and the handoff both assumed does *not* hold on the acceptance page, which
has three diagrams and seven questions; adjacency plus a consistent direction is what works, and
the four questions with no diagram left over are what the page means.

Measured through the real solver against the real endpoint: **three attachments, none wrong.**
Showing a student a diagram that answers a different question is still worse than showing none, so
a page that does not read as a list gets nothing.
- [x] Distinguish two problems that carry the same label on one page, in `rag/locate.py`.
      `find_labels` searches a page for all of its labels at once, in document order, each taking
      the first occurrence after the last one placed. `homework_3` goes from nine distinct positions
      for twelve problems to twelve. This also fixes the source pane's highlight band, which pointed
      at the wrong marker for any sheet whose sections both number from one

### Text recognition

- [x] Transcription interface: page in, text out, feeding the chunker. Both implementations below
      sit behind it, so the model choice is swappable and does not need settling in advance
- [x] **Image content parts in `llm/client.py`.** The client sends and parses text only. This is new
      code rather than a flag, exactly as tool calling was in Phase 2
- [x] **A vision capability probe**, mirroring `probe_tool_support`, with honest degradation in the
      interface when the configured endpoint cannot see. Inference is bundled in Phase 6, so until
      then a vision-capable tutor model is something the student happens to have rather than
      something Lyra ships. The backend half is done; the settings readout lands with the UI pass
- [x] Skip transcription against a non-local endpoint without acknowledgement, the rule
      `extract_facts` already follows. A page image of the student's document is what gets sent
- [x] Measure vision against the text layer on pages that already have one, on the reference book's
      matrix-heavy pages. If it wins, transcription is a quality feature for every document
- [x] Render for recognition at 300 DPI, cached separately from the 144 DPI the source pane reads.
      One cache entry serving both would silently degrade whichever asked second
- [x] Route scanned pages through the same interface, and time a real sample to extrapolate to
      textbook scale. If bulk transcription through the general model is too slow on modest
      hardware, the specialist earns its download

**What the transcription measurement found.** Six pages read through Qwen3.6 27B against their own
text layer. On the five mathematical pages every collapsed matrix is recovered: 36, 25, 35 and 20
lone-number lines become zero, re-emitted as `\begin{bmatrix}` with the entries in the right order,
including fractions the text layer had rendered ambiguous. Checked by hand, not by counting. The
control page of prose comes back at 1217 characters against 1218 and reproduces the book's own typo,
`contrinuity`, which is the clearest evidence available that this transcribes rather than
paraphrases.

So finding 4 holds and transcription is a quality feature for every document, not a
scanned-document feature. What it is not is free: **13.4 seconds a page**. A scanned homework sheet
is under a minute, a lab handout a few minutes, and a 608-page textbook is 2.3 hours. That is the
case for the specialist path stated as a measurement, and it is why transcription has to be opt-in
per document rather than something ingestion decides for the student.
- [x] Per-page rows carrying page number, state, and error, so `pages_done` becomes a count rather
      than a number written once at the end. Per-page progress and per-page retry are both
      impossible without this
- [x] Mixed documents handled per page. Only the pages without text are sent, so the pages that
      already read perfectly well never cost model time
- [x] Re-ingest documents previously marked `unsupported`. `POST /api/documents/{id}/recognize`
      serves this and the per-page retry, because they are one operation: attempt every page not
      currently carrying text
- [x] Accept PNG and JPG uploads. **Not WebP**, which was specified here and turns out not to be
      decodable by this PyMuPDF build, so accepting it would mean a second image dependency for a
      format a scan is unlikely to be in. Checked rather than assumed, and the specs now say so
- [x] Pin a llama.cpp build and record the commit. Now **b10287**, which is exactly commit
      `b06aa77`, the merge of the `max_tiles` fix. The fetcher asks an installed binary what it is
      rather than trusting the directory name, and removes the build it replaces: every consumer
      takes the first `llama-server` it finds, so `llama-b10235` sorted ahead of `llama-b10287` and
      downloading the new one changed nothing until that was fixed
- [x] Model download and management for the OCR weights, with progress and disk-space checks.
      Opt-in behind `fetch_models.py --ocr`, because it is 2.8 GB for a path that is optional. The
      disk check runs before anything is written: a download that fills the disk leaves a partial
      file and a filesystem error rather than a refusal anyone can act on
- [x] Resolve the serving spike documented in rag-pipeline.md and record the outcome there.
      `scripts/ocr_spike.py` reads one page both ways and compares
- [ ] Unlimited-OCR through llama.cpp, page-batched, as the specialist path. **Built and not
      enabled**, on the strength of the measurement below

**What the OCR spike found.** Two answers, and the second one settles the item.

`llama-server` does serve this model with its chat template applied on the pinned build, so the
upstream tokenization hazard is gone and the one-shot-per-page fallback is not needed. `--special`
turns out to be mandatory and its absence is silent: llama-server suppresses special tokens by
default and this model carries its layout in them, so without the flag a table arrives with its
cells fused, 1943 characters against 2457 on the same page. With it, the server and the CLI agree
to 0.9768 similarity, which meets the acceptance criterion.

Then the specialist loses the comparison it exists to win. Over all eight pages of the scanned
reference document: **18.5 seconds a page against the general path's 13.8**, with repetition loops
on **five of eight** pages, the worst repeating one line 217 times. It is slower and it garbles most
pages. R-SWA is still a draft upstream, so the decoder runs full multi-head attention, and
llama.cpp has no `no_repeat_ngram_size` for the loops - both point at the same merge. The
integration is kept, tested, and disconnected, and the measurement is recorded so the next attempt
starts from a number rather than from the assumption that a specialist must be faster.
- [x] Chunk a dense reference table as something other than one chunk a page. Two real faults in
      the paragraph strategy, both fixed: a block bigger than the target was never divided at all
      and survived to the 1024 ceiling, where it was cut through the middle of a table row; and two
      substantial blocks were packed into one chunk, so one embedding stood for two subjects. The
      unit-step question goes from rank 3 to **rank 1**. The rest of the item did not survive
      measurement - see below

**What the recognition acceptance run found.** `Fourier_Tables.pdf`, eight scanned pages with no
text layer at all, is `ready` and searchable in 110.8 seconds: **13.8 seconds a page**, against the
13.4 the reference book's text-layer pages measured in step 3. Two unrelated documents at different
densities landing within four percent of each other says the rate belongs to the model rather than
to the page, which is what makes the 2.3-hour figure for a 608-page book worth quoting. The
transforms are right where they are checkable by hand, the running heads carry page numbers 774
through 780 in sequence, and a graphic comes back as `[figure]` as the prompt asks.

**And what closing the chunking item found out about it.** Two of the causes this item names are
measured and are not causes. Sweeping the generic chunk target from 750 down to 200 moves rank-1
hits between 2 and 5 of 11 with no trend, and makes the top-4 rate *worse* below 300, so the shipped
target is unchanged. Stripping the repeated running head - detected by normalising away the page
number and requiring the line on half the pages - changes rank-1 and mean rank by nothing at all: a
constant prefix shifts every chunk alike, so it barely moves their order. Neither is implemented and
both are recorded in rag-pipeline.md, because the next reader will have the same two ideas.

What is left is not a chunking fault. The questions that still rank badly are cross-representation
confusions - the discrete-time properties table losing to the continuous-time one for a question
that names discrete time - because the two pages are near-identical mathematics and a 137M embedding
model cannot tell them apart. The lever is the embedding model or a reranker, and that is a Phase 5
question rather than a Phase 3 one.

**Three of those were then taken, and this is what they cost and bought.**

- [x] **Measure retrieval against a class rather than a document.** Every retrieval number quoted
      above was measured in a workspace holding *one* document, and `retrieve` is class-scoped, so
      those runs asked a question of a haystack with nothing in it to compete. Re-measured over the
      real 36-document course: the textbook set is unchanged (nothing in a signals class competes
      with a linear algebra book), and a set written about the course itself scores **9/16 first,
      14/16 in the served eight**, with only 54 of 128 served chunks coming from the right document.
      `eval_ingest.py` now takes `expect_document` per question and scores a hit in the wrong
      document as a miss; `scripts/eval_questions/ece203-class.json` is the set
- [x] **A cross-encoder reranker over a wider over-fetch.** Taken before a bigger embedding model
      because it needs no re-embedding and no `embedding_model` identity change. Fetch 64, rerank,
      serve 8: **12/16 first and 15/16 in the served eight, at 1.6 seconds a question**. Optional,
      640 MB, and every failure falls back to the embedding order. It is *not* uniformly better -
      on the textbook, where nothing competes, it costs one first place and changes nothing at
      `k = 4`
- [x] **Pin the transcription's notation.** Reading the acceptance run's own output back with a
      notation counter says what the by-eye reading missed: the eight pages of one document wrote
      their tables three different ways and three of them marked up no table at all, three headings
      came back bold instead of marked up, and one was split across two lines mid-phrase. That is
      why the appendix chunked with none of `C.1` … `C.9` named - **a retrieval failure created at
      transcription time.** `TRANSCRIBE_PROMPT` now pins one heading form and one table form, and
      `SECTION_HEADING` learned the appendix-lettered form it never matched. The re-run that would
      measure it is blocked: the tutor endpoint has been answering
      `model name=Qwen3.6-27b failed to load` since the change was written

Two questions the reranker does **not** answer, and they are the same shape: a student asking how a
homework problem is *worked* gets the problem statement and eleven near-identical problems from
another week, and the answer key never appears in the top 128. The first stage never surfaces it, so
no amount of reordering reaches it. That is the case a better embedding model would have to earn its
re-index on, and it is now a specific, reproducible case rather than a hunch.

- [ ] **Lexical retrieval before a new embedding model, measured on the answer-key case.** A
      problem set restating its questions verbatim in the answer key is the textbook case for
      lexical matching: the words are identical and the embedder is what cannot tell the documents
      apart. So the cheaper lever comes first — BM25 beside the vectors, with a document-type boost
      toward keys and solutions, which is the hybrid retrieval already on rag-pipeline.md's
      future-extensions list. A new embedding model costs a full re-index and an
      `embedding_model` identity change, and it should not be reached for until the lexical path
      has been measured against this case and found wanting. Fully specified — FTS5 schema,
      rank fusion, the boost, and the acceptance numbers — as Workstream 1 of
      [integration-handoff.md](integration-handoff.md)

- [ ] **A text layer can be junk without being empty, and nothing notices.** Found while building
      the class-scale workspace, and it is a robustness hole rather than a ranking one. `laplace.pdf`
      is a photographed page emailed to the student: it ingests `ready` with one chunk reading
      `3/12/26, 2:14 PM IMG_8887.jpg https://mail.google.com/...` and none of the mathematics. Two
      lecture decks extract their equations as scattered loose characters and contribute 35 chunks
      of noise. Phase 1's rule is "zero characters means offer recognition", which these pass. The
      fix is a legibility check at parse time that can offer recognition to a document that has text
      and cannot be read
- [ ] **The full page-selective vision quality gate, of which the junk-page check above is the
      first step.** Finding 4's pages are the case a legibility check does not reach: a
      matrix-heavy page whose text layer extracts cleanly and is still lossy. The full gate decides
      per page whether what extracted is what the page says, and routes the pages that fail through
      the vision pass that already exists — which is what would make transcription the quality
      feature for every document that the measurement said it is, rather than only a way out of
      `unsupported`. It remains open, and it is the architectural successor to the junk-page gate
      rather than a separate idea

**Note on ordering.** OCR was previously the gating item of its own phase, on the strength of an
unmerged upstream dependency and multi-GB weights. It is no longer gating: it becomes a measured
optimization behind an interface rather than a prerequisite. The research in rag-pipeline.md stands
and is still the plan for the specialist path.

**Definition of done:** A student uploads a 600-page textbook, watches it ingest in a time the
screen states honestly, and asks a question whose answer lives in one section. The answer cites that
section by its path rather than by a page number alone, and a problem that says "use the result from
section 5.2" resolves it directly. A scanned document that Phase 1 refused is re-read in place
without being uploaded again, with per-page progress while it runs and a retry on the one page that
failed. A figure the solution refers to appears in the solution document with provenance back to its
page, and survives export. Everything the phase cannot do, it says.

## Phase 4: Agent

**Goal:** Let Lyra act outside its own database.

The tool-calling loop is already built in Phase 2, because verification needs it. This phase is
therefore additional tools, the security posture that has to come with them, and a surface to use
them from. That makes it considerably smaller than it looks.

- [ ] Web search and fetch through a locally hosted FireCrawl instance
- [ ] Look up an unfamiliar method a textbook specifies, and offer it as a profile fact through the
      existing propose-and-confirm flow rather than writing it silently
- [ ] Read and reason about code for a class or lab
- [ ] Write and edit code, with changes shown before they are applied. The review mechanics —
      pending edits, derived hunks, per-hunk accept and reject, staleness rebasing — are built
      by the Phase 5 draft workspace ([integration-handoff.md](integration-handoff.md),
      Workstream 3) against database-only content; this item reuses them across the filesystem
      boundary once the security posture below exists
- [ ] **Security posture, specified before any tool touches the filesystem.** Uploaded documents are
      untrusted input by design, and once the model holds tools, document content becomes an
      injection vector. Today the backend is loopback-only and writes only to `data/`, which is a
      defensible boundary; filesystem and execution tools move it. Required: path allowlisting, no
      execution without explicit confirmation, every tool call visible in the transcript, and a
      documented threat model covering a poisoned upload

**Note on the local-first pillar.** Web search means outbound requests to arbitrary hosts. That is
not telemetry and a self-hosted FireCrawl keeps it under the user's control, but the pillar's
wording has to distinguish "never reports on you" from "never touches the network", or it oversells.

## Phase 5: Knowledge Building

**Goal:** Help students retain what they learn, not just finish the work in front of them — and
give them a place to produce their own writing, not only consume Lyra's.

Two external projects were evaluated for this phase in August 2026 — NitroAI, an AGPL study app
whose flashcard and quiz designs are worth having, and kuhn, the owner's own AI writing tool
whose suggestion-review machinery is worth having. What survives that evaluation, and exactly how
it lands on the artifact substrate, is specified decision-complete in
[integration-handoff.md](integration-handoff.md); what was deliberately left behind, and why, is
recorded there too. The three specified items are ready to build now and do not wait for Phase 4:
none of them touches the filesystem, so none of them crosses the security boundary that phase has
to specify first.

Specified and ready to build (integration-handoff.md is the source of truth for how):

- [ ] Flashcard generation with spaced repetition. Decks as artifacts, cards grounded through
      retrieval with provenance, a pure-function scheduler, review sessions with honest
      progress buckets. Workstream 2 of the handoff
- [ ] Quiz mode with score tracking and weakness identification. Same substrate; per-type
      validation the model is not trusted to follow; weakness surfaced per topic from real
      attempt data rather than a score nobody can audit. Workstream 2 of the handoff
- [ ] **Draft workspace: AI-assisted writing.** A Milkdown editor over a new `draft` artifact
      kind; inline streamed passages that stay outside the document until accepted; whole-draft
      revisions reviewed and applied hunk by hunk through a server-authoritative pending-edit
      flow that lands every accept in revision history. Grounded in the class's own material.
      This is the "changes shown before they are applied" pattern Phase 4 also needs, proven
      first on database-only content. Workstream 3 of the handoff

Not yet specified:

- [ ] Practice problem generation from uploaded material, with configurable difficulty and topic
      focus, attempts, and feedback. Distinct from quiz mode on purpose: quizzes are closed-form
      and auto-graded; practice problems are open-ended, attempted in the workspace, with
      feedback through Guide-mode chat
- [ ] Study guide generation from a test date and topic list. The draft workspace is its natural
      home: a study guide is a generated draft the student then owns and edits
- [ ] Full class profile editor with a deadline calendar view
- [ ] User profile refinement across classes
- [ ] Cross-class connections, referencing concepts from prerequisite courses
- [ ] Email drafting helper using the class profile for professor contact and tone

## Phase 6: Distribution

**Goal:** Make the local-first pillar unconditional and ship one application.

- [ ] **Bundled inference engine.** Ship llama.cpp and weights for the tutor model, not just
      embeddings and OCR, with model download, storage, selection, and memory-aware defaults. Until
      this lands, the tutor endpoint is user-supplied and the endpoint locality machinery in the UI
      is live scaffolding. Once it lands, that machinery becomes unnecessary and the privacy
      guarantee stops being conditional. This is the item that makes pillar 1 true
- [ ] Per-feature model requirements, so a user on modest hardware learns which features their
      model can carry before being disappointed by one
- [ ] Long-horizon multi-page OCR, adopting one-shot multi-page parsing once R-SWA lands upstream in
      llama.cpp, recovering cross-page context for tables and problems spanning page breaks
- [ ] Bulk document import: Canvas or syllabus export ingestion, organized by type
- [ ] Native wrapper (Tauri or Electron) supervising both processes so the user launches one
      application, with deadline notifications and signed distribution

## Model Baseline

Development and testing target **Qwen3.6 27B**, a capable vision-language model, as the reference.
Gemma4 covers the smaller memory configurations. The rule of thumb: if something does not work on
the reference model, it will not work on a smaller one.

A vision-capable tutor model is assumed from Phase 3 onward. Feature work does not currently
accommodate weaker models; per-feature requirements and the accompanying disclaimer are Phase 6,
once there is enough built to measure honestly.

## Not On The Roadmap (Explicitly Excluded)

- Multi-user, accounts, or cloud sync
- Hosted third-party model provider integrations
- Telemetry, analytics, or update checks
- Social features or sharing
- Mobile app, though the web UI stays responsive
- Plugin system
- Voice interaction
- Video lecture processing
