"""Embedding against the local llama.cpp server.

This is the only module that applies the nomic task prefixes. Callers pass raw text, and
the prefix is never stored in `chunks.content`.
"""

import httpx

from backend.core.errors import UpstreamError
from backend.llm.embed_server import embedding_server

EMBEDDING_MODEL = "nomic-embed-text-v1.5.Q8_0"
EMBEDDING_DIM = 768

# nomic-embed-text-v1.5 requires an asymmetric task prefix. Omitting one, or using the
# same prefix on both sides, does not raise: it just returns worse neighbours, so the
# regression is invisible until retrieval quality is measured. `test_embed.py` exists to
# make that failure loud.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

BATCH_SIZE = 16

# Embedding a full batch on CPU is slow, and the server is on loopback, so a long timeout
# costs nothing and a short one turns a working machine into a spurious upstream failure.
_REQUEST_TIMEOUT_SECONDS = 300.0

_BAD_RESPONSE_MESSAGE = "The local embedding server returned an unexpected response."
_UNREACHABLE_MESSAGE = "The local embedding server did not respond."


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed chunk text for indexing.

    Args:
        texts: Raw chunk contents, without any task prefix.

    Returns:
        One vector per input, in input order.

    Raises:
        UpstreamError: The embedding server was unreachable or answered with a payload
            that does not match the request.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        vectors.extend(_embed([DOCUMENT_PREFIX + text for text in batch]))
    return vectors


def embed_query(text: str) -> list[float]:
    """Embed a user question for retrieval.

    Args:
        text: The raw query, without any task prefix.

    Returns:
        A single vector.

    Raises:
        UpstreamError: The embedding server was unreachable or answered with a payload
            that does not match the request.
    """
    return _embed([QUERY_PREFIX + text])[0]


def _client() -> httpx.Client:
    """Build the HTTP client for one embedding request. A seam for tests."""
    return httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)


def _embed(inputs: list[str]) -> list[list[float]]:
    """Send one already-prefixed batch and return its vectors in input order."""
    embedding_server.ensure_running()
    payload = _request(inputs)
    return _parse_vectors(payload, len(inputs))


def _request(inputs: list[str]) -> object:
    """POST one batch to the embedding server and decode the JSON body."""
    try:
        with _client() as client:
            response = client.post(
                f"{embedding_server.base_url}/v1/embeddings",
                json={"input": inputs},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise UpstreamError(_UNREACHABLE_MESSAGE) from exc
    except ValueError as exc:
        raise UpstreamError(_BAD_RESPONSE_MESSAGE) from exc


def _parse_vectors(payload: object, expected_count: int) -> list[list[float]]:
    """Validate an embeddings payload and return its vectors ordered by `index`."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != expected_count:
        raise UpstreamError(_BAD_RESPONSE_MESSAGE)

    indexed: list[tuple[int, list[float]]] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            raise UpstreamError(_BAD_RESPONSE_MESSAGE)
        index = item.get("index", position)
        embedding = item.get("embedding")
        if not isinstance(index, int) or not isinstance(embedding, list):
            raise UpstreamError(_BAD_RESPONSE_MESSAGE)
        if len(embedding) != EMBEDDING_DIM:
            # `chunk_embeddings` fixes the width at 768 when the vec0 table is created, so
            # a mismatch here has to fail now rather than at insert time.
            raise UpstreamError(
                f"The local embedding server returned vectors of length {len(embedding)}, "
                f"but {EMBEDDING_DIM} are required. The wrong model may be loaded."
            )
        indexed.append((index, embedding))

    # The OpenAI embeddings schema does not promise the response preserves input order, so
    # sort by the index each item reports. Vectors arrive L2-normalized from llama.cpp and
    # are stored exactly as returned: cosine distance needs no client-side normalization.
    indexed.sort(key=lambda pair: pair[0])
    return [embedding for _, embedding in indexed]
