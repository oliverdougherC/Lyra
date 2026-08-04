# Lyra

Local-first AI study companion for students. Upload your course materials, get intelligent help that
understands your specific class context.

## Core Pillars

1. **Local-first AI** - Document parsing, OCR, embeddings, and all your data stay on your machine.
   Lyra never reports on you: no accounts, no telemetry, no analytics, no update checks, no cloud
   service. That is distinct from never touching the network. Lyra makes exactly two kinds of
   outbound request, both of which you initiate and control: inference against your tutor endpoint,
   and, once web tools land, searches through your own self-hosted instance. The shipped product
   bundles its own inference engine, at which point nothing about a conversation leaves the machine
   at all. See the Inference Posture section of [docs/architecture.md](docs/architecture.md).
2. **Polished by default** - Every pixel, animation, and interaction is intentional. The UI should
   feel like a Fortune 500 product, not a weekend project. Earthy, warm, educational aesthetic, and
   accessible by construction rather than by retrofit.
3. **Contextual understanding** - The AI knows your class: syllabus, deadlines, professor
   expectations, previous homework, your strengths and weaknesses. It builds this from your
   documents, and shows you what it inferred so you can correct it.
4. **Quality over quantity** - One feature, deeply polished, before the next. No bloated feature
   lists with half-baked implementations.

**Built by a student, for students.** Lyra is a convenience tool. It exists to accelerate the way
people actually work, not the way they are nominally supposed to. When a feature could either
enforce good pedagogy or remove friction, Lyra removes the friction and trusts the user. That is why
the tutor has a Guide, Show, and Solve ladder rather than a single mode, and why Solve is a genuine
rung on it.

## Architecture Overview

- **Frontend:** Next.js (React + TypeScript), Tailwind CSS v4, shadcn/ui, Framer Motion
- **Backend:** Python, FastAPI, bound to loopback only
- **Data:** SQLite, single-user and local, with `sqlite-vec` for vector search
- **Embeddings:** `nomic-embed-text-v1.5` (GGUF) through llama.cpp
- **Tutor LLM:** OpenAI-compatible endpoint. User-configured today, bundled in the shipped product
- **OCR (Phase 3):** `baidu/Unlimited-OCR` (GGUF) through llama.cpp

llama.cpp on GGUF weights is the single runtime for local models: embeddings today, text recognition
from Phase 3, and the tutor model itself once inference is bundled. That keeps PyTorch out of the
product and gives the widest device compatibility, covering Apple Silicon, CPU, CUDA, Vulkan, and
ROCm.

**Current scope.** Text-based PDFs, TXT, and MD. Scanned documents are recognized and reported
honestly rather than silently ingested as empty, and gain text recognition in Phase 3. Cutting OCR
from the MVP removed the pipeline's largest technical risk and redirected that effort into the
interface, which is a core pillar.

**Inference is not bundled yet.** You point Lyra at a model server you run. Bundling it is the
headline Phase 6 item and the thing that makes pillar 1 unconditional; the user-configured endpoint
is a development affordance until then. See [docs/feature-roadmap.md](docs/feature-roadmap.md).

**Model baseline.** Development targets Qwen3.6 27B, with Gemma4 covering smaller memory
configurations. Both are vision-capable, which Phase 3 onward assumes.

## Status

Phase 1, the MVP, is complete. A student can create a class, upload course material, watch it
ingest, and hold a contextual streaming conversation about it, with extracted syllabus facts
visible and correctable. The interface has been verified against its own specification: contrast
contracts recomputed from the tokens, all three breakpoints, the full keyboard map, and both
themes. 163 backend tests and 146 frontend tests pass.

Known limit: this has been exercised on short documents. Textbook-scale ingestion and retrieval are
untested and are Phase 3 work.

Phase 2 is the homework solver, the first feature that produces something rather than only
answering. The `docs/` specifications remain the source of truth.

## Quick Start

```bash
# Backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Frontend
cd frontend && pnpm install && cd ..

# Both processes
scripts/dev
```

The backend serves `127.0.0.1:8000` and the frontend `localhost:3000`. Lyra runs two local
processes; the native wrapper that collapses them into one launch is Phase 6.

You also need a model server running an OpenAI-compatible API, configured in Settings.

## Project Structure

```
Lyra/
  backend/          # Python FastAPI backend
    api/            # Route handlers
    core/           # Business logic, ingestion jobs
    rag/            # OCR, chunking, embedding, retrieval
    storage/        # SQLite, vector store, keychain access
    llm/            # Tutor client, prompts, streaming
    tests/
  frontend/         # Next.js frontend
    src/
      app/          # Pages and layouts
      components/   # UI components
      lib/          # API client and utilities
      styles/       # Tokens and the shadcn bridge
    tests/
  data/             # Uploads, extracted text, GGUF weights (never committed)
  docs/             # Project documentation
  scripts/          # Dev, start, and model download scripts
```

## Documentation

- [Architecture](docs/architecture.md) - components, inference posture, API surface, storage
- [RAG Pipeline](docs/rag-pipeline.md) - parsing, chunking, embedding, retrieval, context budget
- [Design System](docs/design-system.md) - tokens with contrast contracts, components, motion
- [Phase 1 Interface](docs/ui-phase-1.md) - screen-by-screen specification for the MVP
- [Homework Solver](docs/solver-phase-2.md) - artifact model, job architecture, verification
- [Phase 2 Interface](docs/ui-phase-2.md) - screen-by-screen specification for the solver
- [Feature Roadmap](docs/feature-roadmap.md) - phased plan and explicit exclusions
- [Code Conventions](docs/conventions.md) - style, structure, testing, git
