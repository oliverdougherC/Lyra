# Feature Roadmap

Phased feature plan for Lyra. Each phase must be polished and stable before the next begins. No
phase is complete until its core flow works without caveats.

## Phase 0: Foundation Cleanup

The repository previously held a different product, a Jan-derived Tauri writing app. That
scaffolding must go before new code lands, otherwise it silently misconfigures the new stack.

- [x] Initialize version control and commit a baseline
- [ ] Remove stale scaffolding: Tauri Cargo config, the Jan devcontainer, the Tauri macOS release
      workflow, the Yarn install state, and the husky hook that runs a script with no manifest
- [ ] Replace `.prettierrc` with the config in conventions.md (`printWidth: 100`,
      `trailingComma: "all"`)
- [ ] Add `pyproject.toml` with the Ruff and pytest configuration from conventions.md
- [ ] Resolve the OCR serving spike in rag-pipeline.md and record the outcome there

The spike gates all OCR work. Do not build the ingestion job before it is settled.

## Phase 1: Foundation (MVP)

**Goal:** Upload documents, get contextual AI help about them. Nothing else.

- [ ] Project scaffolding
  - FastAPI backend bound to `127.0.0.1`, CORS restricted to the local frontend origin
  - Next.js frontend shell, Lyra design tokens plus the shadcn bridge in `globals.css`
  - SQLite schema and migrations
  - Settings panel: tutor endpoint, API key in the OS keychain, model selection
  - Test-connection action that validates the endpoint and lists models
  - Endpoint locality indicator, with the non-local warning and one-time acknowledgement

- [ ] Class workspace
  - Create a class (name, code, semester)
  - Class list on the home page
  - Workspace view with document sidebar and chat area
  - Delete a class and everything it owns

- [ ] Document upload and ingestion
  - Drag-and-drop PDF and image upload returning `202`
  - Background ingestion job with the documented state machine and a polled status endpoint
  - Per-page scanned detection, PyMuPDF extraction for text pages
  - Unlimited-OCR through llama.cpp for scanned pages, page-batched
  - Semantic chunking with the 2048-token ceiling
  - Local embeddings with mandatory `search_document: ` prefixes
  - Vector storage in `sqlite-vec`, with the embedding-model identity recorded
  - Visible ingestion progress and a usable failure state
  - Delete a document and its chunks

- [ ] Contextual chat
  - Sessions, message history, and streaming replies over SSE
  - Retrieved context injected from the class's documents
  - Guide and Show toggle (Socratic versus direct explanation)
  - Markdown rendering, incremental during streaming
  - Indicator when retrieval was heavily trimmed

- [ ] Automatic profile extraction, proposal-only
  - Analysis pass as the `extracting` ingestion stage
  - Facts stored with `confidence`, `confirmed`, and source document
  - High-confidence facts injected as context; low-confidence facts withheld until confirmed
  - Skipped against a non-local endpoint without acknowledgement

- [ ] Minimal class profile view
  - Read-only list of extracted facts grouped by kind, with source document
  - Confirm or reject low-confidence facts, and correct a wrong value
  - This is in Phase 1 on purpose. Extraction runs in V1, so V1 needs a way to see and fix what it
    produced. Without it, one misread deadline silently corrupts every conversation in that class.

**Definition of done:** A student creates a class, uploads a homework PDF, watches ingestion finish,
asks about a specific problem, and gets an answer that references the uploaded material. Extracted
syllabus facts are visible and correctable. Everything except the tutor endpoint runs locally.

## Phase 2: Study Tools

**Goal:** Purposeful study features on top of documents and chat.

- [ ] Homework walkthrough mode
  - Identify each problem in an uploaded homework
  - Step-by-step walkthrough with checking questions between steps
  - Track which problems the student struggled with

- [ ] Practice problem generation
  - Generate new problems from uploaded material
  - Configurable difficulty and topic focus
  - Attempt answers and receive feedback

- [ ] Full class profile editor
  - Editable fields across the whole profile
  - Deadline calendar view

- [ ] Document viewer
  - In-app PDF and image viewer beside the chat
  - Highlight text to ask about it
  - Citation links from a response back to the source page

## Phase 3: Knowledge Building

**Goal:** Help students retain what they learn, not just finish homework.

- [ ] Flashcard generation with spaced repetition
- [ ] Quiz mode with score tracking and weakness identification
- [ ] Study guide generation from a test date and topic list
- [ ] User profile refinement across classes

## Phase 4: Advanced Features

**Goal:** Remove the remaining external dependencies and polish for distribution.

- [ ] **Bundled tutor inference engine**
  - Ship llama.cpp for the tutor model, not just OCR and embeddings
  - Model download, storage, and selection UI; memory-aware defaults
  - This is the change that makes the local-first pillar unconditional. It was deliberately
    excluded from V1 because runtime management, weight distribution, and model selection are
    a large surface that would have dominated the first release. Until it lands, the tutor endpoint
    is user-supplied and expected to be a local server, with remote endpoints treated as a testing
    affordance only. See the Inference Posture section of architecture.md.

- [ ] Long-horizon multi-page OCR
  - Adopt one-shot multi-page parsing once R-SWA lands upstream in llama.cpp
  - Recovers cross-page context for tables and problems spanning page breaks

- [ ] Cross-class connections
  - Reference concepts from prerequisite courses

- [ ] Email drafting helper
  - Uses the class profile for professor contact and tone

- [ ] Bulk document import
  - Canvas or syllabus export ingestion, automatic organization by type

- [ ] Native wrapper
  - Tauri or Electron, supervising both the backend and frontend processes so the user launches one
    application rather than two servers
  - Deadline notifications
  - Signed distribution

## Not On The Roadmap (Explicitly Excluded)

- Multi-user, accounts, or cloud sync
- Hosted third-party model provider integrations
- Telemetry, analytics, or update checks
- Social features or sharing
- Mobile app, though the web UI stays responsive
- Plugin system
- Voice interaction
- Video lecture processing
