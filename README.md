# Lyra

Local-first AI study companion for students. Upload your course materials, get intelligent help that
understands your specific class context.

## Core Pillars

1. **Local-first AI** - Document parsing, OCR, embeddings, and all your data stay on your machine.
   Lyra never phones home: no accounts, no telemetry, no cloud service. The tutor model runs against
   an endpoint you control, and is expected to be a local server. See the Inference Posture section
   of [docs/architecture.md](docs/architecture.md) for exactly what is and is not local in V1.
2. **Polished by default** - Every pixel, animation, and interaction is intentional. The UI should
   feel like a Fortune 500 product, not a weekend project. Earthy, warm, educational aesthetic, and
   accessible by construction rather than by retrofit.
3. **Contextual understanding** - The AI knows your class: syllabus, deadlines, professor
   expectations, previous homework, your strengths and weaknesses. It builds this from your
   documents, and shows you what it inferred so you can correct it.
4. **Quality over quantity** - One feature, deeply polished, before the next. No bloated feature
   lists with half-baked implementations.

## Architecture Overview

- **Frontend:** Next.js (React + TypeScript), Tailwind CSS v4, shadcn/ui, Framer Motion
- **Backend:** Python, FastAPI, bound to loopback only
- **Data:** SQLite, single-user and local, with `sqlite-vec` for vector search
- **OCR:** `baidu/Unlimited-OCR` (GGUF) through llama.cpp
- **Embeddings:** `nomic-embed-text-v1.5` (GGUF) through llama.cpp
- **Tutor LLM:** user-configured OpenAI-compatible endpoint (llama.cpp, Ollama, or remote for testing)

One runtime, llama.cpp on GGUF weights, covers both OCR and embeddings. That keeps PyTorch out of
the product and gives the widest device compatibility: Apple Silicon, CPU, CUDA, Vulkan, and ROCm.

**V1 does not bundle an inference engine for the tutor model.** You point Lyra at a local model
server. Bundling one is the headline Phase 4 item; see
[docs/feature-roadmap.md](docs/feature-roadmap.md).

## Status

Pre-scaffolding. The `docs/` specifications are the source of truth and are complete enough to build
against. Phase 0 in the roadmap covers cleanup and the one open technical spike (OCR serving mode),
which gates ingestion work.

## Quick Start

Not yet runnable; there is no application code. Once Phase 1 scaffolding lands, the flow is:

```bash
# Backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Frontend
cd frontend && yarn install && cd ..

# Both processes
scripts/dev
```

The backend serves `127.0.0.1:8000` and the frontend `localhost:3000`. Lyra runs two local
processes; the native wrapper that collapses them into one launch is Phase 4.

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
- [RAG Pipeline](docs/rag-pipeline.md) - OCR, chunking, embedding, retrieval, context budget
- [Design System](docs/design-system.md) - tokens with contrast contracts, components, motion
- [Feature Roadmap](docs/feature-roadmap.md) - phased plan and explicit exclusions
- [Code Conventions](docs/conventions.md) - style, structure, testing, git
