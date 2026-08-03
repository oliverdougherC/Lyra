# Feature Roadmap

Phased feature plan for Lyra. Each phase must be polished and stable before the next begins. No
phase is complete until its core flow works without caveats.

**Scope principle for V1:** Phase 1 deliberately excludes OCR. Scanned documents are the single
largest source of technical risk in the pipeline (an unmerged upstream dependency, an unresolved
serving question, and multi-GB model weights), and they are not required to prove the product. A
student uploading a text-based homework PDF or their lecture notes exercises every other part of the
system. The effort freed by that cut goes into the interface, which is a stated core pillar and the
thing a user actually judges. See [ui-phase-1.md](ui-phase-1.md) for the screen-level specification.

## Phase 0: Foundation Cleanup

The repository previously held a different product, a Jan-derived Tauri writing app. That
scaffolding must go before new code lands, otherwise it silently misconfigures the new stack.

- [x] Initialize version control and commit a baseline
- [x] Remove stale scaffolding: Tauri Cargo config, the Jan devcontainer, the Tauri macOS release
      workflow, the Yarn install state, and the husky hook that ran a script with no manifest
- [x] Replace `.prettierrc` with the config in conventions.md
- [x] Retarget dependabot at the real project structure
- [ ] Add `pyproject.toml` with the Ruff and pytest configuration from conventions.md
- [ ] Add `frontend/package.json`, Tailwind v4, and shadcn/ui with the token bridge in `globals.css`

Nothing in Phase 0 is blocked on external work. The OCR serving spike moved to Phase 2, where it
belongs, and no longer gates the MVP.

## Phase 1: Foundation (MVP)

**Goal:** Upload text documents, get contextual AI help about them, in an interface that already
feels finished. Nothing else.

### Scaffolding
- [ ] FastAPI backend bound to `127.0.0.1`, CORS restricted to the local frontend origin
- [ ] SQLite schema and migrations
- [ ] Next.js frontend shell with the Lyra tokens and shadcn bridge in `globals.css`
- [ ] `scripts/dev` and `scripts/start` launching both processes

### Settings
- [ ] Tutor endpoint configuration, API key stored in the OS keychain
- [ ] Test-connection action that validates the endpoint and lists available models
- [ ] Model selection populated from the endpoint
- [ ] Endpoint locality indicator, non-local warning, and one-time acknowledgement
- [ ] Theme control: system, light, dark

### Class workspace
- [ ] Create a class (name, code, semester)
- [ ] Class list on the home page
- [ ] Workspace view with document sidebar and chat area
- [ ] Rename a class, and delete a class with everything it owns

### Document upload and ingestion
- [ ] Drag-and-drop upload returning `202`
- [ ] Supported in this phase: text-based PDF, TXT, MD
- [ ] Text extraction with PyMuPDF, preserving page numbers
- [ ] Scanned-page detection that produces a clear `unsupported` state rather than a silent empty
      document. This is required in Phase 1 precisely because OCR is not: a student will drop a
      scanned PDF, and the app must say so plainly and offer to keep the file for later.
- [ ] Background ingestion job with the documented state machine and a polled status endpoint
- [ ] Semantic chunking with the 2048-token ceiling
- [ ] Local embeddings with mandatory `search_document: ` prefixes
- [ ] Vector storage in `sqlite-vec`, with the embedding-model identity recorded
- [ ] Delete a document and its chunks

### Contextual chat
- [ ] Sessions, message history, and streaming replies over SSE
- [ ] Retrieved context injected from the class's documents
- [ ] Guide and Show toggle (Socratic versus direct explanation)
- [ ] Markdown rendering, incremental during streaming, with math and code blocks
- [ ] Indicator when retrieval was heavily trimmed
- [ ] Stop generation, and retry a failed turn

### Automatic profile extraction, proposal-only
- [ ] Analysis pass as the `extracting` ingestion stage
- [ ] Facts stored with `confidence`, `confirmed`, and source document
- [ ] High-confidence facts injected as context; low-confidence facts withheld until confirmed
- [ ] Skipped against a non-local endpoint without acknowledgement

### Class profile view
- [ ] Facts grouped by kind, each showing its source document
- [ ] Confirm or reject low-confidence facts, and correct a wrong value
- [ ] In Phase 1 on purpose: extraction ships in V1, so V1 needs a way to see and fix what it
      produced. Without it, one misread deadline silently corrupts every conversation in that class.

### Interface and visual quality
This is a first-class deliverable, not a finishing pass. It is specified screen by screen in
[ui-phase-1.md](ui-phase-1.md) and is part of the definition of done.

- [ ] Every screen implements all four data states: loading, empty, error, and populated
- [ ] Skeletons match the real layout; no spinners for page or list loads
- [ ] Full keyboard operation, `:focus-visible` rings, focus trapping in overlays, skip-to-content
- [ ] `prefers-reduced-motion` honored in CSS and in Framer Motion variants
- [ ] Responsive across the three documented breakpoints
- [ ] Dark mode complete, including code and math rendering
- [ ] No hardcoded colors; every surface uses a token
- [ ] Contrast contracts verified against the values recorded in design-system.md

**Definition of done:** A student creates a class, uploads a text-based homework PDF, watches
ingestion finish, asks about a specific problem, and gets an answer that references the uploaded
material. Extracted syllabus facts are visible and correctable. Dropping a scanned PDF produces an
honest, actionable message. Every screen is keyboard-navigable, responsive, correct in both themes,
and free of placeholder styling. Everything except the tutor endpoint runs locally.

## Phase 2: Scanned Documents (OCR)

**Goal:** Accept the documents Phase 1 rejects.

- [ ] Resolve the OCR serving spike documented in rag-pipeline.md and record the outcome there.
      This gates the rest of the phase.
- [ ] Pin a llama.cpp build and record the commit
- [ ] Model download and management for the OCR weights, with progress and disk-space checks
- [ ] Unlimited-OCR through llama.cpp, page-batched, for scanned pages and images
- [ ] Accept PNG, JPG, and WebP uploads
- [ ] Per-page OCR progress in the ingestion UI, and per-page retry
- [ ] Re-ingest documents previously marked `unsupported`
- [ ] Mixed documents handled per page, so a scan-and-text hybrid works

Deferred within this phase: long-horizon multi-page parsing, which needs R-SWA upstream. See Phase 5.

## Phase 3: Study Tools

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

## Phase 4: Knowledge Building

**Goal:** Help students retain what they learn, not just finish homework.

- [ ] Flashcard generation with spaced repetition
- [ ] Quiz mode with score tracking and weakness identification
- [ ] Study guide generation from a test date and topic list
- [ ] User profile refinement across classes

## Phase 5: Advanced Features

**Goal:** Remove the remaining external dependencies and polish for distribution.

- [ ] **Bundled tutor inference engine**
  - Ship llama.cpp for the tutor model, not just embeddings and OCR
  - Model download, storage, and selection UI; memory-aware defaults
  - This is the change that makes the local-first pillar unconditional. It was deliberately
    excluded from V1 because runtime management, weight distribution, and model selection are a
    large surface that would have dominated the first release. Until it lands, the tutor endpoint is
    user-supplied and expected to be a local server, with remote endpoints treated as a testing
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
  - Tauri or Electron, supervising both processes so the user launches one application
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
