# RAG Pipeline Specification

## Overview

The RAG (Retrieval-Augmented Generation) pipeline is the core differentiator of Lyra. It transforms raw course materials into structured, retrievable knowledge that the LLM can use contextually during tutoring sessions.

The pipeline has two distinct roles:
1. **OCR Engine** (Unlimited OCR) - Converts images and scanned PDFs into structured text
2. **Tutor Model** (user's LLM) - Consumes retrieved context to generate tutoring responses

These are separate models with separate responsibilities. The OCR engine is infrastructure. The tutor model is the product.

## Pipeline Stages

```
Upload -> Parse -> Chunk -> Embed -> Store -> Retrieve -> Generate
```

### Stage 1: Upload

User drops a file into the workspace. Accepted formats:
- PDF (text-based or scanned)
- Images (PNG, JPG, WebP)
- Plain text (TXT, MD)
- Office documents (DOCX, PPTX) - future phase

**Input:** Raw file bytes + metadata (filename, upload timestamp, class ID)

**Output:** File stored in `data/uploads/` with assigned document ID

### Stage 2: Parse

Determine the file type and extract raw text.

**Text-based PDFs:**
- Use `pymupdf` (PyMuPDF) for direct text extraction
- Preserve page numbers and section structure
- Output: Structured text with page boundaries

**Scanned PDFs / Images:**
- Render to bitmap at 300 DPI
- Send to Unlimited OCR model
- Parse OCR output, strip detection tokens (`<|det|>...<|/det|>`)
- Post-process: group lines into blocks, separate with double newlines
- Output: Clean text with block structure

**Plain text / Markdown:**
- Read directly
- Output: Text as-is

**OCR Configuration:**
- Model: `baidu/Unlimited-OCR` (3B params, ~6.7GB BF16, quantized variants available)
- Serving: vLLM or SGLang (user's choice)
- API: OpenAI-compatible endpoint (separate from tutor model)
- Single image mode: `gundam` config (base_size=1024, image_size=640, crop_mode=True)
- Multi-page mode: `base` config (image_size=1024)
- Post-processing regex: `r'<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)'`

**Output:** Raw text with structural markers (pages, sections)

### Stage 3: Chunk

Split raw text into semantic units. This is not uniform chunking - it respects document structure.

**Chunking strategy by document type:**

| Document Type | Chunk Boundary | Max Size | Overlap |
|---------------|----------------|----------|---------|
| Homework | Individual problem | Full problem text | None |
| Textbook | Section/subsection | 2000 tokens | 100 tokens |
| Lecture notes | Topic heading | 1500 tokens | 75 tokens |
| Syllabus | Logical section | 1000 tokens | 50 tokens |
| Generic | Paragraph | 1500 tokens | 100 tokens |

**Chunk detection logic:**
1. Attempt to detect document type from filename patterns and content heuristics
2. If homework: split on problem markers ("1.", "Problem 1", "Q1", etc.)
3. If textbook/notes: split on heading markers (#, ##, or numbered sections)
4. If no structure detected: fall back to paragraph-based splitting with overlap

**Each chunk stores:**
- `chunk_id`: Unique identifier
- `document_id`: Parent document
- `class_id`: Owning class
- `content`: The text
- `metadata`: Document type, page number, section title, problem number (if applicable)
- `token_count`: Approximate token count

**Output:** Array of structured chunks

### Stage 4: Embed

Convert each chunk into a vector embedding for similarity search.

**Model:** `nomic-embed-text` (137M params, runs on CPU, fast)
- Embedding dimension: 768
- Max input: 8192 tokens
- Local-first, no external API calls

**Alternative models (future):**
- `all-MiniLM-L6-v2` (smaller, faster, slightly less accurate)
- User-configurable if they want a different embedding model

**Output:** Float array (768 dimensions) per chunk

### Stage 5: Store

Persist chunks and embeddings for retrieval.

**Storage: SQLite + sqlite-vec extension**
- Single database file, no external dependencies
- `chunks` table: id, document_id, class_id, content, metadata (JSON), token_count
- `embeddings` table: chunk_id, embedding (vector), indexed for ANN search
- Scoped by class_id - retrieval always filters to the current class

**Indexing:**
- ANN index on embeddings for fast similarity search
- Metadata index for filtered retrieval (e.g., "only homework chunks")

### Stage 6: Retrieve

Given a user query, find the most relevant chunks from the class's document store.

**Retrieval process:**
1. Embed the user's query with the same embedding model
2. ANN search against the class's vector store
3. Return top-K results (K=8 by default)
4. Re-rank by recency (newer documents slightly weighted higher)
5. Filter to stay within context window budget

**Context window budget:**
- Reserve 40% of the LLM's context for system prompt + conversation history
- Allocate 60% for retrieved documents
- Trim retrieved chunks from bottom up until within budget
- Log a warning if retrieval is heavily truncated

**Output:** Ranked list of chunks with content and metadata

### Stage 7: Generate

Construct the prompt and stream the response from the tutor model.

**Prompt structure:**
```
[System prompt with class profile + user profile + mode setting]

[Retrieved context - labeled by source document]

[Conversation history]

[User's current message]
```

**Streaming:**
- Use SSE (Server-Sent Events) for token streaming to the frontend
- Frontend renders tokens as they arrive
- Markdown rendering happens incrementally

**Output:** Streaming response rendered in the chat interface

## Automatic Profile Extraction

When a document is uploaded, before showing the user anything, the system performs an internal analysis pass:

1. Send the document text (or a summary if it's large) to the tutor model
2. System prompt instructs the model to extract structured facts:
   - Deadlines and dates
   - Course topics and prerequisites
   - Professor preferences and grading scheme
   - Key concepts and methods
3. Extracted facts are merged into the Class Profile
4. The user never sees this step - it happens silently during ingestion
5. If the model is uncertain about something, it marks it as `confidence: low` for later user review

**Prompt for extraction:**
```
You are analyzing a course document. Extract the following structured information.
Only extract facts that are explicitly stated. Do not infer or guess.
Return JSON with these fields: deadlines[], topics[], professor_info{}, grading{}, prerequisites[], notes[]
```

## Design Principles

1. **Local-first** - OCR, embedding, and storage all run on the user's machine. Only the tutor model may be remote.
2. **Compartimentalized** - Each pipeline stage is a discrete module. Swapping OCR models, embedding models, or storage backends should not require rewriting other stages.
3. **Semantic chunking** - Never split a homework problem in half. Never split a code block in half. Chunks respect document structure.
4. **Lightweight context** - The pipeline is designed to fit retrieved context into small LLM context windows (8K-32K tokens). Retrieval is tight and targeted.
5. **Class-scoped** - All retrieval is scoped to the current class. No cross-class leakage unless explicitly requested.

## Future Extensions

- Cross-class retrieval for prerequisite connections (Phase 3)
- User-configurable chunking strategies per document type
- Hybrid retrieval: combine vector search with keyword BM25
- Conversational RAG: use conversation history to refine retrieval queries
- Citation links: click a claim in the AI's response to see the source document
