"""Acceptance backend harness -- production app with bounded model fixtures.

Replaces the local embedding model call with deterministic vectors and skips
profile extraction/consolidation.  The ingestion worker, SQLite storage, all
background workers, route logic, migrations, middleware, and every other
application component run unmodified.

The fake tutor endpoint (configured via PUT /api/settings after startup)
handles all interactive model calls: chat, study generation, solver, writer,
and agent chat.

Start with:
    LYRA_DATA_DIR=/tmp/acceptance uv run python -m uvicorn \
        acceptance.backend_harness:app --host 127.0.0.1 --port 8000
"""

import backend.core.ingestion as _ingest_mod
import backend.rag.embed as _embed_mod
from backend.rag.embed import EMBEDDING_DIM


def _fake_embed_all(texts: list[str], prefix: str) -> list[list[float]]:
    """Deterministic 768-dim vectors keyed on text content.

    Each vector is unique per input (seeded from its character codes) and
    normalised to unit length.  The dimensionality matches the production
    nomic-embed-text-v1.5 model so sqlite-vec storage works unmodified.
    """
    vectors: list[list[float]] = []
    for text in texts:
        seed = sum(ord(c) for c in text[:200]) % 9973
        raw = [((seed * (i + 1)) % 9973) / 9973 for i in range(EMBEDDING_DIM)]
        norm = sum(v * v for v in raw) ** 0.5
        vectors.append([v / norm for v in raw])
    return vectors


_embed_mod._embed_all = _fake_embed_all
_ingest_mod.extract_facts = lambda conn, document_id, text, doc_type: None
_ingest_mod.consolidate_class = lambda conn, class_id: None

from backend.main import app  # noqa: E402, F401
