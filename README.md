# Lyra

Local-first AI study companion for students. Upload your course materials, get intelligent help that understands your specific class context.

## Core Pillars

1. **Local-first AI** - Designed to run entirely on your machine. No cloud required. Promotes ethical AI use without data center dependency. Remote API support exists but is secondary.
2. **Polished by default** - Every pixel, animation, and interaction is intentional. The UI should feel like a Fortune 500 product, not a weekend project. Earthy, warm, educational aesthetic.
3. **Contextual understanding** - The AI knows your class: syllabus, deadlines, professor expectations, previous homework, your strengths and weaknesses. It builds this understanding automatically from your documents.
4. **Quality over quantity** - One feature, deeply polished, before the next. No bloated feature lists with half-baked implementations.

## Architecture Overview

- **Frontend:** Next.js (React + TypeScript), Tailwind CSS v4, shadcn/ui, Framer Motion
- **Backend:** Python, FastAPI
- **Data:** SQLite (single-user, local-only)
- **RAG Pipeline:** Unlimited OCR for document parsing, local embedding model, SQLite-based vector store
- **LLM:** User-configurable via API endpoint (local llama.cpp, Ollama, or remote)

## Quick Start

```bash
cd /Users/ofhd/Developer/Lyra
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Project Structure

```
Lyra/
  backend/          # Python FastAPI backend
    api/            # API routes
    core/           # Core business logic
    rag/            # OCR + RAG pipeline
    storage/        # SQLite models + vector store
  frontend/         # Next.js frontend
    src/
      app/          # Pages and layouts
      components/   # UI components
      lib/          # Utilities and API client
  docs/             # Project documentation
  scripts/          # Build and utility scripts
```

## Documentation

See `docs/` for detailed specifications:

- [Architecture](docs/architecture.md)
- [Design System](docs/design-system.md)
- [RAG Pipeline](docs/rag-pipeline.md)
- [Feature Roadmap](docs/feature-roadmap.md)
- [Code Conventions](docs/conventions.md)
