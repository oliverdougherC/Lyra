# System Architecture

## High-Level Overview

Lyra is a single-user, local-first study application. The frontend runs in the user's browser, the
backend runs on the user's machine, and inference runs against an OpenAI-compatible endpoint. That
endpoint is user-configured today and bundled with the application in Phase 6, at which point Lyra
is local end to end. The user opens the app when studying and closes it when done. No background
services, no persistent daemon, no cloud account.

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
│ mtmd-cli: OCR (P3)  │  │ bundled in Phase 6  │
└─────────────────────┘  └─────────────────────┘
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
4. Embeddings, and text recognition once it lands, never leave the machine under any configuration.

**Model baseline.** Development and testing target Qwen3.6 27B as the reference, with Gemma4
covering smaller memory configurations. Both are vision-capable, and a vision-capable tutor model is
assumed from Phase 3 onward. Feature work does not currently accommodate weaker models; per-feature
requirements are Phase 6, once there is enough built to measure honestly.

**On "never phones home."** There is no telemetry, no analytics, and no update check, and there will
not be. That is a different claim from "makes no network requests": inference against a configured
endpoint is a network request, and the web tools in Phase 4 are outbound requests to arbitrary
hosts through a self-hosted FireCrawl instance. Both are user-initiated and user-controlled. The
distinction to hold is that Lyra never reports on the user, which is a stronger and more honest
promise than pretending the socket is never opened.

## Component Breakdown

### 1. Frontend (Next.js)

**Role:** User interface, state management, streaming rendering.

- Client-side navigation across workspace routes: `/`, `/classes/[id]`, `/settings`
- Document upload via drag-and-drop, with ingestion progress
- Streaming tutor responses rendered incrementally
- Reads and writes all state through `lib/api.ts`

**Key pages:**
- **Home:** Class list, recent activity
- **Class workspace:** Documents, conversation, class profile
- **Settings:** Tutor endpoint configuration, model selection, theme

### 2. Backend (FastAPI)

**Role:** Business logic, document processing, inference orchestration, persistence.

**Binding:** The backend binds `127.0.0.1` explicitly. It MUST NOT bind `0.0.0.0`. There is no
authentication, so loopback-only binding is the security boundary.

**CORS:** Allowed origin is exactly `http://localhost:3000` (plus `http://127.0.0.1:3000`).
No wildcard origins.

**Core modules:**

| Module | Responsibility |
|--------|----------------|
| `api/` | Route handlers, request/response models |
| `core/` | Business logic, class management, sessions, ingestion jobs |
| `rag/` | Document ingestion, parsing, chunking, embedding, retrieval |
| `storage/` | SQLite schema and migrations, vector store, secret storage |
| `llm/` | Tutor client abstraction, prompt templates, streaming |

### 3. API Surface

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
- `POST /api/documents/{document_id}/reingest` - Re-run ingestion, for example once OCR support
  lands for a document previously marked `unsupported`
- `DELETE /api/documents/{document_id}` - Delete document, its file, and its chunks

**Ingestion state machine:** `pending -> parsing -> chunking -> embedding -> extracting -> ready`.

Two terminal states besides `ready`:
- `failed` carries a user-facing `error_message` and the stage that failed.
- `unsupported` means the file is a kind Lyra cannot read yet, in practice a fully scanned PDF
  before Phase 3. The original file is retained so it can be re-ingested later without re-uploading.
  It is deliberately distinct from `failed`: nothing went wrong, the feature is not built yet.

`GET .../status` returns the current stage, a page-level progress counter where known, the count of
pages skipped for lack of extractable text, and the error if failed.

**Chat**
- `POST /api/classes/{class_id}/sessions` - Create a chat session
- `GET /api/classes/{class_id}/sessions` - List sessions
- `GET /api/sessions/{session_id}/messages` - Message history for a session
- `POST /api/sessions/{session_id}/chat` - Send a message, stream the reply over SSE
- `POST /api/sessions/{session_id}/regenerate` - Answer the last question again, over the same SSE
  protocol. It carries no message body: the question is already stored, which is what makes this a
  retry of the answer rather than a repeat of the question. The reply being replaced is deleted only
  once a new one has been written, so a retry that fails upstream costs the user nothing
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
- `DELETE /api/solutions/{artifact_id}` - Delete the artifact and everything it owns
- `PATCH` and `POST .../parts/{part_id}[/regenerate]` - Edit or re-solve one problem

Full specification, including the state machine and the review gate, in
[solver-phase-2.md](solver-phase-2.md).

**Profile**
- `GET /api/classes/{class_id}/profile` - Class profile, including unconfirmed extracted facts
- `PATCH /api/classes/{class_id}/profile` - Correct or delete a field
- `POST /api/classes/{class_id}/profile/confirm` - Confirm or reject low-confidence facts
- `GET /api/profile` / `PATCH /api/profile` - Global user profile

**Settings**
- `GET /api/settings` - Current settings. Never returns the API key, only whether one is set.
- `PUT /api/settings` - Update settings
- `POST /api/settings/test-connection` - Validate the tutor endpoint
- `GET /api/settings/models` - Fetch models the endpoint advertises

### 4. RAG Pipeline

See [rag-pipeline.md](rag-pipeline.md) for the full specification, including the exact llama.cpp
invocations and their currently-known upstream limitations.

**Summary:**
- Documents are ingested by a background job on upload
- Text-based PDFs are parsed with PyMuPDF
- Fully scanned documents terminate as `unsupported` today; text recognition arrives in Phase 3
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
facts for the Class Profile. Extraction is **proposal-only**: every extracted fact carries a
`confidence` and a `confirmed` flag.

- `confidence: high` facts are used as context immediately.
- `confidence: low` facts are stored but **not injected into prompts** until the user confirms them.

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

Two consequences worth recording here rather than only in the phase spec. The LLM client does not
send or parse tool calls today, so this is new code in `llm/client.py` rather than a flag; and not
every OpenAI-compatible endpoint a user configures implements `tools`, so the capability is probed
and a negative result degrades verification honestly instead of failing the solver. See
[solver-phase-2.md](solver-phase-2.md).

**Threat model, to be specified before any tool touches the filesystem.** Uploaded documents are
untrusted input by design: a student uploads whatever their professor handed them. Once the model
holds tools, document and web content becomes an injection vector, and the instruction boundary
matters. Today the backend is loopback-only and writes only to `data/`, which is a defensible
boundary. Filesystem and execution tools move it, and Phase 4 does not begin until path
allowlisting, confirmation before execution, and a written threat model covering a poisoned upload
are in place.

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
- Two processes in both development and production: Next.js on `3000`, FastAPI on `127.0.0.1:8000`

**Why two processes rather than the backend serving the frontend.** A single-process deployment
would require a Next.js static export, and static export cannot render dynamic route segments such
as `/classes/[id]` without knowing every id at build time. Working around that would mean
abandoning route segments or the app router. Running the Next.js server is simpler, keeps the
framework intact, and costs only a second local process. `scripts/dev` and `scripts/start` launch
both and are the supported entry points.

**Known consequence:** this requires both a Node and a Python runtime on the machine. That is
acceptable during development, when the audience already runs a local model server. Collapsing this
into one distributable artifact is the job of the native wrapper in Phase 6, which will supervise
both processes.

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
