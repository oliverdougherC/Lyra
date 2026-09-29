# Document processing and retrieval

The pipeline turns uploaded course material into searchable, class-scoped context. This maintained
map describes the current implementation. Prior experiments, model research, and phase plans are
preserved in the [historical RAG specification](rag-pipeline-history.md).

## Upload and ingestion

`backend/api/routes_documents.py` accepts PDF, plain text, Markdown, PNG, and JPEG uploads. The
upload returns before background processing completes. `backend/core/ingestion.py` owns the queue
and durable stage transitions: `pending`, `parsing`, `chunking`, `embedding`, then
`ready`, `unsupported`, or `failed`. A document becomes searchable after its index is
validated; optional class-profile extraction runs separately and cannot hold the upload queue.

The accepted file limit is 50 MiB. Before multipart parsing can spool uploaded parts, an ASGI
guard caps the complete request at that limit plus 64 KiB of framing overhead, including
unexpected parts. It rejects excessive declared lengths immediately and counts incoming bytes
when the length is missing or understated. Host, Origin, and packaged-session checks run first.
The existing file-level copy limit, private atomic publication, and database rollback remain
in force; oversized requests receive HTTP 413 and temporary files close on rejected or
disconnected uploads. Unsupported file types are rejected before publication, though multipart
parsing has already occurred within the request limit. The client gives each logical upload a
stable idempotency key. Repeating that operation returns the same document, while a deliberate
new upload receives a new identity. A deleted operation keeps a tombstone so a delayed retry
cannot silently recreate an intentionally removed document.

`backend/rag/parse.py` extracts text and page/section metadata. Scanned or otherwise unreadable
pages are recorded explicitly. The document API reports each page as readable, not attempted,
recognition failed, or genuinely blank, with a bounded reason and whether its text is indexed.
`ready` means a usable published index, not complete page coverage. A partly readable document
can be ready while its unreadable pages need recognition; a wholly unreadable one retains its
original upload in the unsupported state. Recognition is an explicit document action, not an
automatic upload of every scanned page.

After parsing and chunking, a first upload publishes page-native readable text independently
of embedding. If the local embedding helper fails, the document remains `failed` for semantic
search, but its valid text pages remain available to bounded direct page reads and lexical
search. A retry builds vectors without duplicating the published text. Refresh keeps the old
page evidence and old index until the replacement vector index and page text commit together.
Physical page numbers come from the parser; printed page labels in the content are not used as
citations. Older indexed documents recover exact page text from their retained source file on
first page read, without discarding the usable old index.

`backend/core/recognition.py` records per-page progress and sends requested page images through
`backend/rag/transcribe.py` to the configured vision-capable tutor. It resolves endpoint access and
remote consent before rendering/sending pages. An acknowledged remote tutor receives those images.
The local specialist helper (`transcribe_page_locally` and `backend/llm/ocr_server.py`) exists but
is not the selected ingestion path. Do not promise local-only recognition based on its presence.
Truncated transcription is rejected rather than stored as complete page text. A refresh builds
replacement evidence while the previous validated index stays available; failure or restart
keeps that index and marks the refresh separately. Deleting or moving a file still revokes its
old class evidence.

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
can go to an acknowledged remote endpoint. They run after core indexing; a stalled or failed
profile request does not make readable pages unavailable. `backend/core/profiles.py` and
`backend/core/consolidation.py` own evidence, confirmation, and prompt-selection rules.

## Retrieval and generation

`backend/rag/retrieve.py` combines exact class-partitioned vector search with FTS5 lexical ranking
using reciprocal rank fusion. It can rerank candidates with the local cross-encoder, resolve
section references, and apply source/context budgets. When the embedding helper is unavailable,
bounded lexical matching still serves indexed text and reports its narrower basis. Selected
document page/problem references are resolved directly before ranked excerpts. Ready-only
filters prevent unpublished or failed indexes from contributing context. Empty retrieval is a
valid result, not an invented source or an infrastructure error.

The class agent can list, search, and read bounded pages and numbered problems from uploaded
documents independently of its optional local code workspace. Every read checks the live class,
conversation, selected-document scope, and document-text consent; citations retain document and
page provenance. Bounded responses provide a revision-scoped continuation cursor, exact source
character ranges, and a `has_more` flag. A continuation repeats the live scope and consent
checks; replacing, moving, or deleting a source invalidates its cursor. Problem chunks split
at physical page boundaries while retaining their shared problem number. Incomplete short
worksheets are refused before inference when the requested problem page is unreadable and the
configured tutor has no confirmed vision capability. With
confirmed vision and consent, Lyra may attach at most three relevant rendered pages from a
selected document, including a pure scan that has no text index yet; it never sends all pages
of a long book merely because they are scanned.
An explicit page-image read validates the class, selected file and physical page before rendering,
then rechecks the source identity while reading at most 3 MiB of image bytes. Endpoint consent
still gates sending those bytes to the tutor. When a tool discovers a needed image after the
initial request, the next provider round receives the actual encoded page image with document
and page provenance. The same turn allows at most three page images in total, and a context
guard stops before sending an overfull transcript. A confirmed vision-capable endpoint is
required; a settings, consent, source identity, or Stop change blocks later delivery. Once a
private image is opened, public web-research tools refuse dispatch for the rest of that turn.
When a narrow context cannot fit the extra image tool schema alongside an already attached
page, Lyra retains text tools and explains the image limit rather than pretending to have
seen another page. The Files view labels page-native readable text separately from semantic
index readiness, and that text can be selected for class chat while embedding retry is pending.

Chat, solutions, study, drafting, and agent workflows build their own bounded prompts from the
selected scope. They use the configured OpenAI-compatible tutor. Source references remain tied to
stored documents/pages; generation is not proof that a claim is correct. Provider and consent
boundaries are described in [architecture](architecture.md) and [privacy](privacy-and-data-location.md).

## Durability and verification

Text source previews read at most 200,001 decoded characters through the private-file reader,
return the first 200,000, and use the extra character to report truncation. Large extractions
therefore do not need to be read in full just to open a preview. Missing extractions retain the
empty-preview response; corrupt, inaccessible, or unsafe entries (including final-component
symlinks) fail rather than appearing as a successfully empty source. Previewing never truncates
the stored extraction or original document.

Document publication checks that the source still belongs to the active job. Deletion, reingestion,
or interruption must not allow a stale worker to publish over newer state. See
[storage consistency](storage-consistency.md) for filesystem/database reconciliation.

Regression coverage lives beside the relevant seams: `backend/tests/test_ingestion.py`,
`test_recognition.py`, `test_embed.py`, `test_retrieve.py`, and `test_api_documents.py`.
Use [the testing guide](contributing-testing-migrations.md) for commands. Real-model measurements
are dated evidence and must not be represented as current benchmark certification.
