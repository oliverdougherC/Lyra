# System Architecture

## High-Level Overview

Lyra is a single-user, local-first desktop application with a web frontend served by a Python backend. The user opens the app in their browser, interacts with their study workspace, and closes it when done. No background services, no persistent daemon.

```
┌─────────────────────────────────────────────┐
│                   Browser                    │
│  ┌─────────────────────────────────────────┐ │
│  │             Next.js Frontend             │ │
│  │  ┌──────────┐  ┌──────────┐  ┌────────┐ │ │
│  │  │  Classes  │  │ Workspace│  │Settings│ │ │
│  │  │  List     │  │  View    │  │  Panel │ │ │
│  │  └──────────┘  └──────────┘  └────────┘ │ │
│  └─────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────┘
                   │ HTTP/JSON
┌──────────────────▼──────────────────────────┐
│              FastAPI Backend                 │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │  API     │  │  RAG     │  │ Memory    │ │
│  │  Routes  │  │ Pipeline │  │ Engine    │ │
│  └──────────┘  └──────────┘  └───────────┘ │
│  ┌─────────────────────────────────────────┐ │
│  │         SQLite + Vector Store            │ │
│  └─────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────┘
                   │ OpenAI-compatible API
┌──────────────────▼──────────────────────────┐
│            User's LLM Endpoint               │
│     (llama.cpp / Ollama / remote API)        │
└─────────────────────────────────────────────┘
```

## Component Breakdown

### 1. Frontend (Next.js)

**Role:** User interface, state management, real-time interaction.

- Single-page application with client-side navigation
- Workspace-based routing: `/classes/:id`, `/classes/:id/homework/:id`
- Settings panel for LLM configuration
- Document upload via drag-and-drop
- Real-time streaming responses from the backend

**Key pages:**
- **Home:** Class list, recent activity
- **Class workspace:** Documents, homework, conversation, class profile
- **Settings:** LLM endpoint configuration, theme, model selection

### 2. Backend (FastAPI)

**Role:** Business logic, document processing, LLM orchestration, data persistence.

**API surface:**
- `GET /api/classes` - List all classes
- `POST /api/classes` - Create a new class workspace
- `GET /api/classes/:id` - Class details + profile
- `POST /api/classes/:id/documents` - Upload a document
- `GET /api/classes/:id/documents` - List uploaded documents
- `POST /api/classes/:id/chat` - Send a message (streaming)
- `GET /api/settings` - Get current settings
- `PUT /api/settings` - Update settings
- `POST /api/settings/test-connection` - Test LLM endpoint
- `GET /api/settings/models` - Fetch available models

**Core modules:**

| Module | Responsibility |
|--------|----------------|
| `api/` | Route handlers, request/response models |
| `core/` | Business logic, class management, session handling |
| `rag/` | Document ingestion, OCR, embedding, retrieval |
| `storage/` | SQLite models, vector store, profile persistence |
| `llm/` | LLM client abstraction, prompt templates, streaming |

### 3. RAG Pipeline

See [rag-pipeline.md](rag-pipeline.md) for full specification.

**Summary:**
- Documents are ingested on upload
- Scanned/ image-based PDFs go through Unlimited OCR
- Text is chunked semantically (by problem, section, topic)
- Chunks are embedded with a local embedding model
- Stored in SQLite with vector index (sqlite-vec)
- Retrieved contextually during conversation

### 4. Memory Engine (Three-Tier Profiles)

**User Profile (Global)**
- Learning style preferences
- High-level strengths and weaknesses
- Preferred explanation style
- Study habits and patterns
- Persists across all classes and semesters

**Class Profile (Per-Class)**
- Syllabus data: deadlines, exam schedule, grading scheme
- Professor information and contact details
- Course prerequisites
- Key concepts and topics covered
- Your progress and difficulty areas in this class
- Persists for the semester, archived after

**Session Context (Per-Session)**
- Current homework or study topic
- Active conversation history
- Retrieved documents and context
- In-progress problems
- Discarded after session; key learnings bubble up to Class Profile

**Automatic extraction:** When documents are uploaded, the LLM performs an internal analysis pass (not shown to the user) that extracts structured facts and updates the relevant profiles. This happens transparently during ingestion.

### 5. Data Storage

**SQLite database (`lyra.db`):**
- Classes table
- Documents table
- Class profiles (JSON blob per class)
- User profile (single row)
- Chat sessions and messages
- Settings

**Vector store (sqlite-vec extension):**
- Document chunks with embeddings
- Indexed by class ID for scoped retrieval

**File storage (`data/`):**
- Original uploaded files
- OCR-processed text files
- Thumbnails and previews

## Deployment Model

**Local-only, single-user:**
- No authentication
- No multi-tenancy
- All data lives on the user's machine
- The backend serves the frontend on localhost
- User points the LLM configuration to their local or remote endpoint

**Future expansion (not MVP):**
- Bundled llama.cpp for zero-config local LLM
- macOS native app wrapper (Tauri/Electron) for distribution

## Design Decisions

### Why FastAPI over Flask/Django?
- Async support for streaming LLM responses
- Automatic OpenAPI documentation
- Type-safe request/response models with Pydantic
- Lightweight, minimal boilerplate

### Why Next.js over SvelteKit/Vanilla?
- Mature ecosystem for data fetching and state management
- Better component library support (shadcn/ui is React-native)
- Easier to find and integrate UI components from KokonutUI, etc.

### Why SQLite over PostgreSQL?
- Single-user, local-only means no need for a separate database server
- Zero configuration - the database file just exists
- sqlite-vec provides vector search without external dependencies
- Sufficient performance for document-scale RAG (thousands of chunks, not millions)

### Why not a desktop app?
- The value is in the AI logic, not OS integration
- Web UI is immediately accessible and easy to share
- No need for background services or menu bar presence
- Keeps the project lightweight and focused
