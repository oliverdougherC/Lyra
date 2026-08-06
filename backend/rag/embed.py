"""Embedding against the local llama.cpp server.

This is the only module that applies the nomic task prefixes. Callers pass raw text, and
the prefix is never stored in `chunks.content`.

It is also where the model's own context limit is enforced, in real tokens rather than
estimated ones. `rag/chunk.py` holds every chunk to `MAX_CHUNK_TOKENS`, but that ceiling is
measured with `estimate_tokens`, which runs four characters to a token, and real text does
not: measured over a linear algebra textbook the median is 3.4 characters per token and the
worst chunk is 1.6. An estimate cannot be the last word in front of a hard wall, so the
count that matters is asked of the tokenizer that will actually be used.
"""

import logging

import httpx

from backend.core.errors import UpstreamError
from backend.llm.embed_server import embedding_server

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "nomic-embed-text-v1.5.Q8_0"
EMBEDDING_DIM = 768

# nomic-embed-text-v1.5 requires an asymmetric task prefix. Omitting one, or using the
# same prefix on both sides, does not raise: it just returns worse neighbours, so the
# regression is invisible until retrieval quality is measured. `test_embed.py` exists to
# make that failure loud.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# What one input may be. This GGUF declares `nomic-bert.context_length = 2048`, which
# llama.cpp reports as `n_ctx_train` and clamps every request to no matter what `-c` says.
# The model card's 8192 is reachable only through rope scaling this GGUF does not carry,
# and an over-long input is refused with `exceed_context_size_error` rather than truncated.
EMBEDDING_CONTEXT_TOKENS = 2048

# Only the per-input limit is real. llama-server splits a request across its own batches
# internally, so sixteen inputs totalling 30,144 tokens are served without complaint
# against `-b 8192`. Measured rather than assumed, because the obvious reading of `-b` is
# that it caps the request, and designing a token budget around that would have been
# machinery guarding nothing.
BATCH_SIZE = 16

# Embedding a full batch on CPU is slow, and the server is on loopback, so a long timeout
# costs nothing and a short one turns a working machine into a spurious upstream failure.
_REQUEST_TIMEOUT_SECONDS = 300.0

_BAD_RESPONSE_MESSAGE = "The local embedding server returned an unexpected response."
_UNREACHABLE_MESSAGE = "The local embedding server did not respond."
_REFUSED_MESSAGE = "The local embedding server refused this text."
_UNSPLITTABLE_MESSAGE = (
    "This document contains an unbroken run of text longer than the local embedding "
    "model can read, and there is no word boundary to split it at."
)


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
    return _embed_all(texts, DOCUMENT_PREFIX)


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
    return _embed_all([text], QUERY_PREFIX)[0]


def _embed_all(texts: list[str], prefix: str) -> list[list[float]]:
    """Embed every text under one task prefix, splitting anything the model cannot take.

    A text above the model's context is cut into pieces that fit and its pieces' vectors
    are pooled back into one, so the caller still gets exactly one vector per input. The
    alternative was what happened before this existed: the server refused the input, the
    HTTP error was reported as an unreachable server, and one oversized chunk failed the
    whole document.
    """
    embedding_server.ensure_running()

    groups = [_split_to_fit(prefix, text) for text in texts]
    pieces = [prefix + piece for group in groups for piece in group]

    vectors: list[list[float]] = []
    for start in range(0, len(pieces), BATCH_SIZE):
        batch = pieces[start : start + BATCH_SIZE]
        vectors.extend(_parse_vectors(_request(batch), len(batch)))

    return _regroup(groups, vectors)


def _split_to_fit(prefix: str, text: str) -> list[str]:
    """Cut `text` into pieces that each fit the model's context, prefix included.

    Halved at whitespace rather than trimmed, because the whole point is that no part of a
    chunk is dropped. The recursion is logarithmic in the overrun, and a text with no
    whitespace to cut at is returned whole so the server refuses it loudly rather than
    this looping.
    """
    if _fits(prefix + text):
        return [text]

    left, right = _halve(text)
    if not left or not right:
        # An unbroken run longer than the model's context, so there is nowhere to cut.
        # Said plainly here rather than left to the server, whose refusal reaches the
        # student as an unreachable endpoint.
        raise UpstreamError(_UNSPLITTABLE_MESSAGE)
    return _split_to_fit(prefix, left) + _split_to_fit(prefix, right)


def _halve(text: str) -> tuple[str, str]:
    """Split `text` at whichever whitespace sits nearest its midpoint."""
    middle = len(text) // 2
    candidates = [
        position
        for position in (text.rfind(" ", 0, middle), text.find(" ", middle))
        if position > 0
    ]
    if not candidates:
        return text, ""
    cut = min(candidates, key=lambda position: abs(position - middle))
    return text[:cut].strip(), text[cut:].strip()


def _fits(text: str) -> bool:
    """Whether one already-prefixed input is inside the model's context."""
    # A token is at least one character, so a short enough string cannot need asking about.
    # This is what keeps queries and small chunks off the tokenizer entirely.
    if len(text) <= EMBEDDING_CONTEXT_TOKENS:
        return True
    return _token_count(text) <= EMBEDDING_CONTEXT_TOKENS


def _regroup(groups: list[list[str]], vectors: list[list[float]]) -> list[list[float]]:
    """One vector per original text, pooling the pieces of anything that was split."""
    result: list[list[float]] = []
    position = 0
    for group in groups:
        taken = vectors[position : position + len(group)]
        position += len(group)
        result.append(taken[0] if len(taken) == 1 else _pool(taken))
    return result


def _pool(vectors: list[list[float]]) -> list[float]:
    """Combine a split text's piece vectors into one, the way the model pools tokens.

    Mean then re-normalize. The server is run with `--pooling mean`, so this is the same
    operation one level up, and re-normalizing is required rather than tidy: retrieval
    compares with cosine distance on the strength of every stored vector being unit length,
    and the mean of unit vectors is not one.
    """
    width = len(vectors[0])
    pooled = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(width)]
    norm = sum(value * value for value in pooled) ** 0.5
    return pooled if norm == 0 else [value / norm for value in pooled]


def _client() -> httpx.Client:
    """Build the HTTP client for one embedding request. A seam for tests."""
    return httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)


def _mapped_error(exc: httpx.HTTPError) -> UpstreamError:
    """Tell a server that answered with an error from one that did not answer.

    Worth the two messages. A 400 naming an input's token count was reported as an
    unreachable endpoint, which is a true statement about neither the server nor the
    document and sends whoever reads it to the wrong problem entirely.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # The body carries the real reason and is for the log, not for the student.
        logger.warning(
            "Embedding server returned %s: %s", exc.response.status_code, exc.response.text[:300]
        )
        return UpstreamError(_REFUSED_MESSAGE)
    return UpstreamError(_UNREACHABLE_MESSAGE)


def _token_count(text: str) -> int:
    """How many tokens the embedding model will actually make of `text`.

    Asked of the server rather than estimated, because this is the number a hard limit is
    compared against. It costs a loopback round trip, measured at 0.34 ms, which is under
    two percent of what embedding the same text costs.
    """
    try:
        with _client() as client:
            response = client.post(
                f"{embedding_server.base_url}/tokenize",
                json={"content": text},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise _mapped_error(exc) from exc
    except ValueError as exc:
        raise UpstreamError(_BAD_RESPONSE_MESSAGE) from exc

    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, list):
        raise UpstreamError(_BAD_RESPONSE_MESSAGE)
    return len(tokens)


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
        raise _mapped_error(exc) from exc
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
