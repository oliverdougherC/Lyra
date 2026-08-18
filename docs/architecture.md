# System Architecture

## High-Level Overview

Lyra is a single-user, local-first study application. The frontend runs in the user's browser, the
backend runs on the user's machine, and inference runs against an OpenAI-compatible endpoint. That
endpoint is user-configured today and bundled with the application in Phase 6, at which point Lyra
is local end to end. Web research runs through a loopback Firecrawl stack that `./run` provisions
and supervises. The user opens the app when studying and stops it when done; there is no cloud
account or always-on Lyra daemon.

```
┌──────────────────────────────────────────────┐
│                   Browser                    │
│  ┌────────────────────────────────────────┐  │
│  │   Next.js Frontend (localhost:3000)    │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────┐  │  │
│  │  │ Classes │ │Workspace │ │Settings │  │  │
│  │  └─────────┘ └──────────┘ └─────────┘  │  │
│  └────────────────────────────────────────┘  │
└──────────────────┬───────────────────────────┘
                   │ HTTP/JSON + SSE
┌──────────────────▼───────────────────────────┐
│       FastAPI Backend (127.0.0.1:8000)       │
│  ┌───────────┐ ┌────────────┐ ┌───────────┐  │
│  │    API    │ │    RAG     │ │  Memory   │  │
│  │  Routes   │ │  Pipeline  │ │  Engine   │  │
│  └───────────┘ └────────────┘ └───────────┘  │
│  ┌────────────────────────────────────────┐  │
│  │     SQLite (lyra.db) + sqlite-vec      │  │
│  └────────────────────────────────────────┘  │
└────────┬─────────────────────┬───────────────┘
         │ subprocess          │ OpenAI-compatible HTTP
┌────────▼────────────┐  ┌─────────▼───────────┐
│  llama.cpp (GGUF)   │  │ Tutor LLM endpoint  │
│   local, bundled    │  │  user-configured    │
│ llama-server: embed │  │llama-server / Ollama│
│ llama-server: OCR   │  │ bundled in Phase 6  │
└─────────────────────┘  └─────────────────────┘

FastAPI ── HTTP on loopback ──> Firecrawl API (`127.0.0.1:3002`)
                                └─ Docker-only workers, browser,
                                   Postgres, Redis, and RabbitMQ
```

Two inference concerns, deliberately separated:

- **Infrastructure models** (embeddings today, text recognition from Phase 3) are Lyra's
  responsibility. They are always local, run through llama.cpp on GGUF weights, and are not
  user-configurable.
- **The tutor model** is the user's responsibility for now, reached over an OpenAI-compatible API.
  Phase 6 makes it Lyra's responsibility too.

## Inference Posture

This section is normative.

**The shipped product bundles its own inference engine.** Lyra ships llama.cpp and tutor-model
weights alongside the embedding and text-recognition models it already manages, so that no part of a
conversation leaves the machine. That is the endpoint state of this architecture and the thing that
makes the local-first pillar unconditional rather than conditional on user configuration. It is
Phase 6 in the roadmap, deferred only because runtime management, weight distribution, memory
budgeting, and model selection are a large surface that would otherwise dominate every earlier
phase. The LLM client abstraction exists so it can land without touching the rest of the system.

**Everything below describes the development period before that lands, and is transitional.** The
user-configured endpoint is scaffolding, not a design goal. It should not be built on as though it
were permanent, and the UI it requires is expected to be removed rather than maintained.

During that period:

1. The user configures a tutor endpoint. The **expected configuration is a local endpoint** on
   loopback, such as `llama-server` or Ollama on `127.0.0.1`.
2. **Remote endpoints are a testing affordance, never a shipping mode.** They exist so the product
   can be developed and evaluated before the bundled engine lands. No third-party provider
   integrations, provider presets, or hosted-account flows are built.
3. Because a remote endpoint means document text leaves the machine, the app MUST make this
   visible rather than silent:
   - Settings shows a persistent **endpoint locality indicator**. An endpoint is treated as local
     only if its host resolves to loopback (`127.0.0.1`, `::1`, `localhost`).
   - A non-local endpoint shows a standing warning in Settings and a marker in the workspace header.
   - The **first** ingestion against a non-local endpoint requires an explicit, one-time
     acknowledgement that document text will be sent to that endpoint. Recorded in settings.
   - Automatic profile extraction (which sends document text to the tutor model) is **skipped** when
     the endpoint is non-local and the acknowledgement has not been given. It is never performed
     silently against a remote endpoint.
4. Embeddings and text recognition never leave the machine under any configuration.

**Model baseline.** Development and testing target Qwen3.6 27B as the reference, with Gemma4
covering smaller memory configurations. Both are vision-capable, and a vision-capable tutor model is
assumed from Phase 3 onward. Feature work does not currently accommodate weaker models; per-feature
requirements are Phase 6, once there is enough built to measure honestly.

**On "never phones home."** There is no telemetry, no analytics, and no update check, and there will
not be. That is a different claim from "makes no network requests": inference against a configured
endpoint is a network request, and the web tools in Phase 4 are outbound requests to arbitrary
hosts through a self-hosted Firecrawl instance. Both are user-initiated and user-controlled. The
distinction to hold is that Lyra never reports on the user, which is a stronger and more honest
promise than pretending the socket is never opened.

## Component Breakdown

### 1. Frontend (Next.js)

**Role:** User interface, state management, streaming rendering.

- Client-side navigation across class, chat, solver, study, draft, and settings routes
- Document upload via drag-and-drop, with ingestion progress
- Streaming tutor responses rendered incrementally
- Reads and writes all state through `lib/api.ts`

**Key pages:**
- **Home:** Class list, recent activity
- **Class hub (`/classes/[id]`):** The class as a place. Its conversations, solution sets,
  study tools, drafts, documents, and profile in one tab bar, with every action that belongs to the class: rename,
  archive, delete, rename or delete a chat or a solution set, and move, reindex, or delete files.
  The open tab is a `?tab=` parameter, so any section of a class is a link.
- **Class workspace (`/classes/[id]/chat`):** Conversation and documents, side by side
- **Study workspace (`/classes/[id]/study/[artifactId]`):** Deck review or quiz attempt flow
- **Draft workspace (`/classes/[id]/drafts/[artifactId]`):** Milkdown editor with streamed writing,
  pending-edit review, and revision history
- **Settings:** Tutor endpoint configuration, model selection, theme

### 2. Backend (FastAPI)

**Role:** Business logic, document processing, inference orchestration, persistence.

**Binding:** The backend binds `127.0.0.1` explicitly. It MUST NOT bind `0.0.0.0`. There is no
authentication, so loopback-only binding is the security boundary.

**CORS:** Allowed origin is exactly `http://localhost:3000` (plus `http://127.0.0.1:3000`).
No wildcard origins.

**Host validation:** A trusted-host check runs before every route and rejects any request whose
`Host` header is not a Lyra loopback host (`127.0.0.1`, `localhost`, or `::1`; any port). This is
part of the loopback-only boundary, not a duplicate of CORS: CORS does not close DNS rebinding,
where a page stays same-origin to a name it controls while that name is rebound to `127.0.0.1`. The
browser cannot forge the `Host` value, so refusing an unrecognized one fails the rebinding request
even when `Origin` is absent or looks acceptable.

**Core modules:**

| Module | Responsibility |
|--------|----------------|
| `api/` | Route handlers, request/response models |
| `core/` | Business logic, class management, sessions, ingestion jobs |
| `rag/` | Document ingestion, parsing, chunking, embedding, retrieval |
| `storage/` | SQLite schema and migrations, vector store, secret storage |
| `llm/` | Tutor client abstraction, prompt templates, streaming |

### 3. API Surface

**Health**
- `GET /api/health/live` - Process liveness only; no database or network probe
- `GET /api/health/ready` - Required SQLite access/migration readiness plus separately reported,
  optional Firecrawl availability

Ingestion is slow (embedding a long document takes seconds, and profile extraction on a local tutor
model can take minutes), so document upload is asynchronous and status is polled. Every list
endpoint is class-scoped.

**Two response patterns, chosen by duration, not by feel.** Anything that outlives a request is a
background job with a polled status endpoint; anything the user watches arrive in real time streams
over SSE. Chat streams, because the user is reading it as it lands and the work ends with the turn.
Ingestion is a job, because it can run for minutes and must survive the tab closing.

The Phase 2 solver is **a job, not a stream**, and this is the single most consequential
architectural decision in that phase. A full problem set with verification passes can run for tens
of minutes on local hardware, which is well past what an open connection should be trusted with.
It follows the ingestion pattern: a job row, a stage machine, a polled status endpoint, and
per-problem results written to the artifact as they complete rather than buffered until the end. A
student who closes the laptop mid-solve comes back to finished work.

**Classes**
- `GET /api/classes` - List all classes
- `POST /api/classes` - Create a class workspace
- `GET /api/classes/{class_id}` - Class details
- `PATCH /api/classes/{class_id}` - Rename, change code or semester
- `DELETE /api/classes/{class_id}` - Delete class, its documents, chunks, and sessions

**Documents**
- `POST /api/classes/{class_id}/documents` - Upload a file. Returns `202` with a document record in
  state `pending` and an `ingestion_job_id`. Does not block on parsing.
- `GET /api/classes/{class_id}/documents` - List documents with ingestion state
- `GET /api/documents/{document_id}` - Document detail, including extracted text availability
- `GET /api/documents/{document_id}/status` - Ingestion progress. Poll target.
- `GET /api/documents/{document_id}/pages/{page_number}` - One page rendered to PNG and cached, which
  is what the solver's source pane shows beside a solution
- `GET /api/documents/{document_id}/text` - The extracted text of a source with no pages to draw, so
  a TXT or MD file has the same reading pane as a PDF. Truncated: this is a pane, not a download
- `POST /api/documents/{document_id}/reingest` - Re-run ingestion after a pipeline change
- `POST /api/documents/{document_id}/recognize` - Recognize unread pages in place, with per-page
  progress and retry
- `POST /api/documents/{document_id}/move` - File the document under another class. Returns `202`:
  a move is a re-ingest, because chunks carry a denormalized `class_id`, their vectors live in a
  table partitioned by it, and the facts drawn out of the text belong to whichever class asked for
  them. The file moves on disk, the old class forgets the facts only this document supported, and
  the document arrives `pending` in its new class. Refused with `409` while the document is still
  processing, or while a solution set is built from it
- `DELETE /api/documents/{document_id}` - Delete document, its file, and its chunks

**Ingestion state machine:** `pending -> parsing -> chunking -> embedding -> extracting -> ready`.

Two terminal states besides `ready`:
- `failed` carries a user-facing `error_message` and the stage that failed.
- `unsupported` means the file is retained but is not currently readable. A scanned PDF or image can
  move out of this state through explicit recognition when the configured tutor endpoint supports
  vision. It is deliberately distinct from `failed`: nothing went wrong during ingestion.

`GET .../status` returns the current stage, a page-level progress counter where known, the count of
pages skipped for lack of extractable text, and the error if failed.

Nothing is pushed, so the interface polls: the document list re-asks every 1.5s for as long as
anything in it is non-terminal, derived from the list itself rather than from whichever screen
mounted it, and it keeps polling while the window is in the background. Ingestion does not pause
because the student switched windows, and a run long enough to walk away from is exactly the one
they will.

**On startup**, a document left `pending` is requeued and a document left mid-stage is failed. The
queue is in memory, so a restart loses it either way, but the two cases are not the same: a queued
document was never touched, while one caught mid-stage is the likeliest reason the process stopped
and would otherwise be requeued into the same crash on every restart from then on. Failing both
meant that dropping a folder into a class and restarting the server turned the whole queue into
rows the student had to retry one at a time.

**The `extracting` stage reads a bounded prefix**, `min(context_window * 0.6, 6000)` tokens. Tying
it to the window alone made every upload's cost a function of a number chosen for chat: at a
262144-token window the stage shipped 629,144 characters per document, overran the client's
300-second read timeout, and returned no facts at all - while holding every queued upload behind
it, since the worker takes one document at a time. What extraction looks for is stated near the
front of a syllabus, so reading further mostly pays to be told nothing.

**Chat**
- `POST /api/classes/{class_id}/sessions` - Create a chat session
- `GET /api/classes/{class_id}/sessions` - List sessions
- `GET /api/sessions/{session_id}/messages` - Message history for a session
- `POST /api/sessions/{session_id}/chat` - Send a message, stream the reply over SSE
- `POST /api/sessions/{session_id}/regenerate` - Answer the last question again, over the same SSE
  protocol. It carries no message body: the question is already stored, which is what makes this a
  retry of the answer rather than a repeat of the question. The reply being replaced is deleted only
  once a new one has been written, so a retry that fails upstream costs the user nothing
- `PATCH /api/sessions/{session_id}` - Rename a conversation. A session is named after its first
  message, which is a guess at what it turned out to be about; this corrects the guess, and a
  session that carries a name is never renamed again by a later message
- `DELETE /api/sessions/{session_id}` - Delete a session

Chat is session-scoped, not class-scoped, so conversation history has an unambiguous owner.

**Solutions (Phase 2)**
- `POST /api/classes/{class_id}/solutions` - Create a solution set. Returns `202` and begins
  segmentation. Does not begin solving: that waits for the segmentation to be confirmed
- `GET /api/classes/{class_id}/solutions` - List this class's solution sets
- `GET /api/solutions/{artifact_id}` - The full artifact: parts, provenance, verdicts
- `GET /api/solutions/{artifact_id}/status` - Poll target
- `PATCH /api/solutions/{artifact_id}/segmentation` - Correct the problem list before solving
- `POST /api/solutions/{artifact_id}/start` - Confirm the segmentation and begin solving
- `POST /api/solutions/{artifact_id}/cancel` - Stop the run, keeping completed problems
- `PATCH /api/solutions/{artifact_id}` - Rename the set. Deliberately does not touch `updated_at`:
  the list is ordered by when the work changed, and renaming is not solving
- `DELETE /api/solutions/{artifact_id}` - Delete the artifact and everything it owns
- `POST /api/solutions/{artifact_id}/resegment` - Read the problem list again from the sources,
  discarding what is there. The way back from a segmentation that went wrong past correcting
- `PATCH /api/solutions/{artifact_id}/parts/{part_id}` - Edit one problem's solution by hand
- `POST /api/solutions/{artifact_id}/parts/{part_id}/regenerate` - Re-solve one problem, given what
  the student says is wrong with it
- `GET /api/solutions/{artifact_id}/parts/{part_id}/revisions` and `POST .../restore` - Every earlier
  version of a part, and the way back to one. Editing and re-solving both write revisions, so no
  correction is a one-way door

Full specification, including the state machine and the review gate, in
[solver-phase-2.md](solver-phase-2.md).

**Study tools (Phase 5, completed ahead of Phase 4)**
- `POST /api/classes/{class_id}/decks` and `/quizzes` - Create grounded study artifacts as jobs
- `GET /api/classes/{class_id}/study` - List the class's decks and quizzes
- `GET /api/decks/{artifact_id}` and `/quizzes/{artifact_id}` - Read generated content and status
- Deck card ratings update the spaced-repetition schedule; quiz attempts retain answers and scores

**Drafts (Phase 5 baseline, completed ahead of Phase 4)**
- `POST /api/classes/{class_id}/drafts` and `GET /api/classes/{class_id}/drafts` - Create and list drafts
- `GET/PATCH /api/drafts/{artifact_id}` and `PATCH .../body` - Read, rename, and autosave a draft
- `POST /api/drafts/{artifact_id}/write` - Stream a proposed passage without silently applying it
- `POST /api/drafts/{artifact_id}/suggest` and `GET .../pending` - Produce and review pending edits
- Draft revisions and status polling reuse the artifact substrate and background-job pattern

**Profile**
- `GET /api/classes/{class_id}/profile` - Class profile, including unconfirmed extracted facts
- `PATCH /api/classes/{class_id}/profile` - Correct or delete a field
- `POST /api/classes/{class_id}/profile/confirm` - Confirm or reject low-confidence facts
- `GET /api/profile` / `PATCH /api/profile` - Global user profile

**Settings**
- `GET /api/settings` - Current settings. Never returns the API key, only whether one is set.
- `PUT /api/settings` - Update settings
- `POST /api/settings/test-connection` - Validate the tutor endpoint
- `POST /api/settings/test-tools` - Ask the endpoint whether it can run tool calls, which is what
  decides whether the solver can check its own work
- `GET /api/settings/models` - Fetch models the endpoint advertises

### 4. RAG Pipeline

See [rag-pipeline.md](rag-pipeline.md) for the full specification, including the exact llama.cpp
invocations and their currently-known upstream limitations.

**Summary:**
- Documents are ingested by a background job on upload
- Text-based PDFs are parsed with PyMuPDF
- Fully scanned documents and images can be recognized explicitly, per page, through a local
  vision-capable tutor endpoint
- Text is chunked semantically, with a hard token ceiling per chunk
- Chunks are embedded locally with `nomic-embed-text-v1.5` GGUF, using mandatory task prefixes
- Stored in SQLite with `sqlite-vec`, searched by exact brute-force KNN
- Retrieved per class, budgeted against the tutor model's context window

### 5. Memory Engine (Three-Tier Profiles)

**User Profile (Global)**
- Learning style and explanation preferences
- High-level strengths and weaknesses
- Persists across all classes and semesters

**Class Profile (Per-Class)**
- Syllabus data: deadlines, exam schedule, grading scheme
- Professor information, course prerequisites
- Key concepts and topics covered
- Progress and difficulty areas in this class

**Session Context (Per-Session)**
- Current homework or study topic
- Active conversation history
- Retrieved chunks for the current turn
- Discarded when the session is deleted; durable learnings are promoted to the Class Profile

**Extraction and confirmation.** Document ingestion runs an analysis pass that proposes structured
facts for the Class Profile, and a second per-class pass merges what the first one restated. The
profile describes the class; documents are evidence for it, tracked in `profile_fact_sources`, and a
second document stating something already known adds evidence rather than a duplicate row. See the
construction section of rag-pipeline.md for the identity and consolidation rules.

Extraction is **proposal-only**: every extracted fact carries a `confidence` and a `confirmed` flag.

- `confidence: high` facts are used as context immediately.
- `confidence: low` facts are stored but **not injected into prompts** until the user confirms them,
  unless two documents state them independently, which is corroboration rather than confirmation.

This is a correctness requirement, not a nicety. A misread exam date or grading weight would
otherwise silently poison the context of every conversation in that class with no way to notice or
fix it. Lyra therefore ships a minimal, user-visible profile view with confirm and correct actions.
See Phase 1 in the roadmap.

### 6. Artifacts (Phase 2)

Through Phase 1, Lyra holds four kinds of thing: inputs the user supplied (`documents`), a derived
index (`chunks`), a transcript (`messages`), and claims about a class (`profile_facts`). None of
these is **a thing Lyra produced that the user keeps, edits, and returns to.** Chat is deliberately
append-only; a message can be regenerated but not revised, and nothing is exportable as a
deliverable.

An **artifact** is that missing primitive, introduced in Phase 2 for the homework solver and reused
in Phase 4 by the agent. Specifying it once, generally, is deliberate: the solver's solution set and
the agent's work product are the same shape, and building them separately would produce two
incompatible models.

An artifact:

- Belongs to a class, and references the source documents it was produced from
- Holds **mixed content**: prose, math, and images. Images are required from the start, because
  Phase 3 pulls textbook figures into solutions. Adding them later would mean rewriting the
  rendering, export, and storage paths at once
- Carries **provenance** at the level of its parts, back to the chunks and pages that informed each
  one, so a claim can be traced and a citation can be rendered
- Is **structured into addressable parts** (a solution's problems and steps), because parts are what
  the user clicks, questions, corrects, and regenerates. An artifact revised only as a whole cannot
  support any of those
- Carries a **status and revision history** per part, distinguishing generated, verified,
  user-corrected, and rejected

`profile_facts` is already this pattern in miniature: generated content with a source document, a
confidence, a confirmed flag, and inline correction. It is the model to generalize from rather than
a separate idea, and the propose-and-confirm posture it establishes carries over unchanged. Nothing
Lyra generates is asserted as fact without the user having a way to see and fix it.

The concrete schema, and the reasoning behind each table, is in
[solver-phase-2.md](solver-phase-2.md).

### 7. Tool Calling (Phase 2)

The tool-calling loop is built in Phase 2 because solution verification needs it, not in Phase 4
where the agent lives. It is written in-house against the existing LLM client rather than adopted
from an agent framework: the tool surface is small (a computer algebra system, later web search and
filesystem access), and what frameworks add beyond the loop itself is multi-provider abstraction,
plugin systems, sandboxing, and session state that Lyra already has or explicitly excludes.

Requirements, in the order they matter:

- **Termination is guaranteed.** A call-depth ceiling and a timeout, with honest reporting when
  either is hit. A loop that silently stops producing is worse than one that says it gave up
- **Tool calls are visible in the transcript.** The user can always see what was run and what came
  back. This is both a debugging affordance and the precondition for trusting the agent later
- **Tools are pure until Phase 4.** The Phase 2 tool set only computes. Nothing reads or writes
  outside `data/`

Two consequences worth recording here rather than only in the phase spec. Tool-call support lives in
`llm/client.py`, not behind a provider-specific framework; and not every OpenAI-compatible endpoint
a user configures implements `tools`, so the capability is probed
and a negative result degrades verification honestly instead of failing the solver. See
[solver-phase-2.md](solver-phase-2.md).

**Threat model, specified before any tool touches the filesystem.** Uploaded documents are
untrusted input by design: a student uploads whatever their professor handed them. Once the model
holds tools, document and web content becomes an injection vector, and the instruction boundary
matters. Today the backend is loopback-only and writes only to `data/`, which is a defensible
boundary. Filesystem and execution tools move it. [phase-4-threat-model.md](phase-4-threat-model.md)
defines path containment, proposal-only model effects, confirmation before execution, durable audit,
SSRF controls, and the hostile-input release gate. Those controls are implemented in the Phase 4
agent substrate; scrape remains default-off until the real pinned-Firecrawl redirect drill passes.

### 8. Data Storage

**SQLite database (`lyra.db`):**
- `classes`, `documents`, `chunks`, `chat_sessions`, `messages`, `settings`
- `class_profiles` and `user_profile` store extracted facts as rows, not opaque blobs, so that
  individual facts can carry `confidence`, `confirmed`, and `source_document_id`
- Ingestion stage, progress, and error live on `documents` columns rather than in a separate job
  table. An earlier draft of this document specified an `ingestion_jobs` table; it was never built,
  because a document has exactly one ingestion at a time and the join bought nothing. The Phase 2
  solver follows the same pattern, keeping its job state on `artifacts`
- `artifacts`, `artifact_sources`, `artifact_parts`, `artifact_part_revisions`, and
  `artifact_provenance` from Phase 2. See [solver-phase-2.md](solver-phase-2.md)
- Study tables hold cards, scheduler state, quiz questions, answers, and attempts; decks and quizzes
  remain artifacts rather than a parallel ownership model
- `pending_edits` stores server-authoritative draft suggestions and their derived review hunks;
  drafts themselves are single-part artifacts with the same revision guarantees as solutions
- `class_workspaces`, `workspace_changes`, and `command_requests` hold default-off workspace grants,
  inert model proposals, reviewed file decisions, and bounded command results
- `tool_audit_events` and `confirmation_nonces` separate durable dispatch history from single-use,
  browser-origin-bound authority for host effects
- `writer_sources`, immutable `writer_source_revisions`, and exact excerpts are the shared evidence
  ledger for writer and class-agent research; web-proposed profile facts retain that evidence and
  stay inactive until confirmation

**Vector store (`sqlite-vec`):**
- A `vec0` virtual table holding chunk embeddings, partitioned by `class_id`
- Vector dimensionality is fixed at table creation, so the embedding model identity is recorded and
  a model change requires a rebuild. See rag-pipeline.md.

**File storage (`data/`):**
- `data/uploads/` original files, `data/text/` extracted text, `data/thumbs/` previews
- `data/pages/` page images rendered by PyMuPDF, added in Phase 2 for the solver's source pane and
  reused by Phase 3 for figures and text recognition
- `data/models/` GGUF weights for embeddings, for text recognition from Phase 3, and for the
  tutor model once inference is bundled in Phase 6
- Operations that mutate both SQLite and these files (upload, move, delete, class delete)
  follow one crash-consistency contract: atomic final-file publication, durable
  `storage_intents` recorded with the database mutation, idempotent startup
  reconciliation, and — for live requests — a process-wide lifecycle mutex with
  conditional (compare-and-swap) transitions and identity-guarded publication of derived
  files. See [storage-consistency.md](storage-consistency.md).

**Secret storage.** The tutor API key is stored in the **OS keychain** (macOS Keychain via
`keyring`), not in `lyra.db`. Encrypting a secret inside the database would require keeping the
decryption key next to the database on the same single-user machine, which provides no real
protection and misleads the reader. If a keychain is unavailable, Lyra stores the key in a
`0600`-permission file and states plainly in the UI that it is stored unencrypted. The key is never
returned by any API response and never written to logs.

## Deployment Model

**Local-only, single-user:**
- No authentication, no multi-tenancy, no network exposure beyond loopback
- All user data on the user's machine
- One app-like entry point, `./run`, supervising Next.js on `3000`, FastAPI on
  `127.0.0.1:8000`, and a pinned Firecrawl stack exposed on `127.0.0.1:3002`
- Docker publishes no Firecrawl database, cache, queue, browser, or worker port to the host

**One launch does not mean one process.** Lyra's frontend and backend remain separate because a
Next.js static export cannot render the app's dynamic route segments without knowing their ids at
build time. Firecrawl is also a multi-service system: its API, workers, browser service, Postgres,
Redis, and RabbitMQ have different reliability and persistence concerns. `./run` turns those
details into one lifecycle with prerequisite checks, idempotent provisioning, migrations, health
gates, diagnostics, and browser launch; it does not hide them in one oversized container.

Firecrawl is built from the pinned upstream
[`v2.11.162`](https://github.com/firecrawl/firecrawl/tree/v2.11.162) source rather than following a
mutable `latest` image. The pin makes rebuilds reviewable and repeatable while retaining the official
self-host layout. The [official self-host guide](https://docs.firecrawl.dev/contributing/self-host)
and [upstream repository](https://github.com/firecrawl/firecrawl) remain the source of truth for
that dependency. Updating the pin is an explicit maintenance change followed by the complete live
search, scrape, and redirect-safety acceptance drill.

There are two readiness levels:

- `GET /api/health/live` proves only that the FastAPI process answers.
- `GET /api/health/ready` returns `503` when SQLite is inaccessible or behind the checked-in
  migrations. It reports Firecrawl as a separate, non-required component, so a Firecrawl outage
  does not take local documents, chat, study tools, or drafts down with it.
- The launcher applies the stricter whole-stack rule. In a normal launch, it also requires the
  Docker services and Firecrawl's own readiness probe before it opens the browser. Only the
  explicit `--skip-firecrawl` path accepts degraded web capability.

The Compose boundary is intentionally extensible. Phase 6 can add a loopback OpenAI-compatible
`inference` service backed by llama.cpp, or a hardware-specific vLLM profile, without changing the
browser-to-backend contract. Model weights and user data remain host-owned volumes; the inference
port is not made public merely because the process moves into a container.

The current launcher still requires compatible Node and Python runtimes on the host and cannot
safely install Docker Desktop or the Docker daemon for the user. A signed native wrapper remains a
Phase 6 distribution option; it will replace the launcher as the desktop entry point, not flatten
the service architecture. Operational details and recovery commands live in
[local-deployment.md](local-deployment.md).

## Design Decisions

### Why FastAPI
- Async support for streaming tutor responses
- Pydantic request and response models
- Automatic OpenAPI documentation
- Minimal boilerplate

### Why Next.js
- Mature ecosystem, and shadcn/ui targets React directly
- Retained over a Vite SPA because the component ecosystem and conventions are worth more than the
  one process we would save. The static-export limitation above is why we run its server rather
  than exporting.

### Why SQLite
- Single-user and local, so a database server would be pure overhead
- Zero configuration
- `sqlite-vec` gives vector search with no external service
- Sufficient at document scale (thousands of chunks)

### Why llama.cpp with GGUF for infrastructure models
- One runtime covers embeddings and later OCR, so there is no PyTorch dependency in the product
- GGUF quantizations run across Apple Silicon, CPU, CUDA, Vulkan, and ROCm, which is the widest
  device compatibility available to us
- vLLM and SGLang were rejected: both are effectively Linux plus NVIDIA, and the reference
  Unlimited-OCR SGLang recipe requires a FlashAttention 3 backend. Neither can run on the primary
  development target (Apple Silicon).

### Why not a desktop app yet
- The value is the AI logic, not OS integration
- Keeps the early phases focused; the wrapper is Phase 6
