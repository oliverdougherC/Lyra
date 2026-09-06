# Document processing and retrieval

The pipeline turns uploaded course material into searchable, class-scoped context. This maintained
map describes the current implementation. Prior experiments, model research, and phase plans are
preserved in the [historical RAG specification](rag-pipeline-history.md).

## Upload and ingestion

`backend/api/routes_documents.py` accepts PDF, plain text, Markdown, PNG, and JPEG uploads. The
upload returns before background processing completes. `backend/core/ingestion.py` owns the queue
and durable stage transitions: `pending`, `parsing`, `chunking`, `embedding`, `extracting`, then
`ready`, `unsupported`, or `failed`. A document is searchable only when it is `ready`.

`backend/rag/parse.py` extracts text and page/section metadata. Scanned or otherwise unreadable
pages are recorded explicitly. A partly readable document can become ready with skipped pages;
a wholly unreadable one retains its original upload in the unsupported state. Recognition is an
explicit document action, not an automatic upload of every scanned page.

`backend/core/recognition.py` records per-page progress and sends requested page images through
`backend/rag/transcribe.py` to the configured vision-capable tutor. It resolves endpoint access and
remote consent before rendering/sending pages. An acknowledged remote tutor receives those images.
The local specialist helper (`transcribe_page_locally` and `backend/llm/ocr_server.py`) exists but
is not the selected ingestion path. Do not promise local-only recognition based on its presence.
Truncated transcription is rejected rather than stored as complete page text.

## Embeddings and optional extraction

`backend/rag/chunk.py` splits text while retaining source structure; its constants own the actual
token limits. `backend/rag/embed.py` applies document/query prefixes and validates the pinned
768-dimensional nomic embedding output. `backend/llm/model_provisioning.py` owns the required
weight source, revision, integrity check, and first-use acquisition behavior. The packaged helper
runtime is staged during build; required embedding weights download on first requested use.

Embeddings run locally. Optional reranking weights are not automatically downloaded. Replacing an
embedding model requires a reviewed reindex/migration path; a vector's dimension alone does not
establish compatibility with existing data.

When enabled, class fact extraction and consolidation use the configured tutor, so document context
can go to an acknowledged remote endpoint. These are part of the requested ingestion workflow;
turning off fact extraction leaves document indexing available. `backend/core/profiles.py` and
`backend/core/consolidation.py` own evidence, confirmation, and prompt-selection rules.

## Retrieval and generation

`backend/rag/retrieve.py` combines exact class-partitioned vector search with FTS5 lexical ranking
using reciprocal rank fusion. It can rerank candidates with the local cross-encoder, resolve
section references, and apply source/context budgets. Ready-only filters prevent partly indexed
or failed documents from contributing context. Empty retrieval is a valid result, not an invented
source or an infrastructure error.

Chat, solutions, study, drafting, and agent workflows build their own bounded prompts from the
selected scope. They use the configured OpenAI-compatible tutor. Source references remain tied to
stored documents/pages; generation is not proof that a claim is correct. Provider and consent
boundaries are described in [architecture](architecture.md) and [privacy](privacy-and-data-location.md).

## Durability and verification

Document publication checks that the source still belongs to the active job. Deletion, reingestion,
or interruption must not allow a stale worker to publish over newer state. See
[storage consistency](storage-consistency.md) for filesystem/database reconciliation.

Regression coverage lives beside the relevant seams: `backend/tests/test_ingestion.py`,
`test_recognition.py`, `test_embed.py`, `test_retrieve.py`, and `test_api_documents.py`.
Use [the testing guide](contributing-testing-migrations.md) for commands. Real-model measurements
are dated evidence and must not be represented as current benchmark certification.
