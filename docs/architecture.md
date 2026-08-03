# System Architecture

## High-Level Overview

Lyra is a single-user, local-first study application. The frontend runs in the user's browser, the
backend runs on the user's machine, and inference runs against an OpenAI-compatible endpoint that
the user controls. The user opens the app when studying and closes it when done. No background
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
│   local, bundled    │  │   user-configured   │
│ llama-server: embed │  │llama-server / Ollama│
│ mtmd-cli: OCR (P2)  │  │remote: testing only │
└─────────────────────┘  └─────────────────────┘
```

Two inference concerns, deliberately separated:

- **Infrastructure models** (embeddings in V1, OCR from Phase 2) are Lyra's responsibility. They are
  always local, run through llama.cpp on GGUF weights, and are not user-configurable.
- **The tutor model** is the user's responsibility. Lyra talks to it over an OpenAI-compatible API.

## Inference Posture

This section is normative. It resolves the tension between "local-first" and "configurable
endpoint" that would otherwise leak user documents off the machine.

**V1 does not bundle an LLM inference engine for the tutor model.** Shipping and managing a
tutor-model runtime (weights download, memory budgeting, model selection, update path) is a large
amount of complexity that would dominate V1. It is deliberately deferred. See the roadmap entry
"Bundled tutor inference engine" in Phase 5. This is the single most important post-V1 change to the
architecture, and the LLM client abstraction exists so that it can be added without touching the
rest of the system.

Consequences for V1:

1. The user configures a tutor endpoint. The **expected and documented configuration is a local
   endpoint** on loopback, such as `llama-server` or Ollama on `127.0.0.1`.
2. **Remote endpoints are a testing affordance, not a supported shipping mode.** They exist so the
   product can be developed and evaluated before the bundled engine lands. They are not presented as
   a recommended path, and no third-party provider integrations, provider presets, or hosted-account
   flows are built in V1.
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
4. Embeddings, and OCR once it lands, never leave the machine under any configuration.

Nothing in Lyra phones home. There is no telemetry, no analytics, and no update check in V1.

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
  before Phase 2. The original file is retained so it can be re-ingested later without re-uploading.
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
- Fully scanned documents terminate as `unsupported` in V1; OCR arrives in Phase 2
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
fix it. V1 therefore ships a minimal, user-visible profile view with confirm and correct actions.
See Phase 1 in the roadmap.

### 6. Data Storage

**SQLite database (`lyra.db`):**
- `classes`, `documents`, `chunks`, `chat_sessions`, `messages`, `settings`
- `class_profiles` and `user_profile` store extracted facts as rows, not opaque blobs, so that
  individual facts can carry `confidence`, `confirmed`, and `source_document_id`
- `ingestion_jobs` tracks stage, progress, and error for each document

**Vector store (`sqlite-vec`):**
- A `vec0` virtual table holding chunk embeddings, partitioned by `class_id`
- Vector dimensionality is fixed at table creation, so the embedding model identity is recorded and
  a model change requires a rebuild. See rag-pipeline.md.

**File storage (`data/`):**
- `data/uploads/` original files, `data/text/` extracted text, `data/thumbs/` previews
- `data/models/` GGUF weights for embeddings, and for OCR from Phase 2

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

**Known consequence:** V1 requires both a Node and a Python runtime on the machine. This is
acceptable for V1, whose audience already runs a local model server. Collapsing this into one
distributable artifact is the job of the native wrapper in Phase 5, which will supervise both
processes.

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

### Why not a desktop app in V1
- The value is the AI logic, not OS integration
- Keeps V1 focused; the wrapper is Phase 5
