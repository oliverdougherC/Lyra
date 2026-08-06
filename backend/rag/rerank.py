"""Reranking: read the question and a passage together, and score the pair.

Stage 6b of docs/rag-pipeline.md, between the KNN and the budget. The KNN searches the
whole class and cannot afford to read anything; this reads a few dozen passages properly
and reorders them.

**Never raises.** Reranking is an improvement to an ordering that is already usable, so
every failure - weights absent, server refused to start, request timed out, reply the wrong
shape - returns None and the caller keeps the embedding order. A student whose optional
model did not download gets slightly worse search, not a broken class.
"""

import logging

import httpx

from backend.core.errors import ConfigurationError
from backend.llm.rerank_server import rerank_server

logger = logging.getLogger(__name__)

# Generous because the server is on loopback and the work is bounded: the caller sends one
# request holding the whole over-fetch, and the largest chunks in a real class measure six
# seconds for thirty-two of them. A timeout short enough to bite would only convert a slow
# machine's good ranking into a fast machine's worse one.
_REQUEST_TIMEOUT_SECONDS = 120.0


def rerank(query: str, passages: list[str]) -> list[float] | None:
    """Score every passage against the query with the cross-encoder.

    Args:
        query: The student's question, unprefixed. Unlike embedding, there is no task
            prefix here: a cross-encoder is trained on the bare pair.
        passages: Chunk text, in any order.

    Returns:
        One score per passage, positionally aligned with `passages`, or None when
        reranking is unavailable. Scores are logits, so they are unbounded and routinely
        negative; only their order is meaningful, and they must never be compared against
        a cosine similarity or shown to anyone.
    """
    if not passages:
        return None
    if not rerank_server.available:
        return None

    try:
        rerank_server.ensure_running()
    except ConfigurationError:
        logger.warning("Reranking is configured but did not start; keeping the search order")
        return None

    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{rerank_server.base_url}/v1/rerank",
                json={"query": query, "documents": passages},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("The reranking server failed; keeping the search order", exc_info=True)
        return None

    return _scores(payload, len(passages))


def _scores(payload: object, expected: int) -> list[float] | None:
    """Read the scores back into the caller's order.

    The server answers sorted by score and identifies each result by its index into the
    request, so the reply has to be put back rather than read off. A reply that does not
    account for exactly every passage is discarded whole: a partial ranking silently
    demotes whatever went missing to last place, which is a wrong answer that looks like
    a working one.
    """
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != expected:
        return None

    scores: list[float | None] = [None] * expected
    for result in results:
        if not isinstance(result, dict):
            return None
        index, score = result.get("index"), result.get("relevance_score")
        if not isinstance(index, int) or not isinstance(score, int | float):
            return None
        if not 0 <= index < expected or scores[index] is not None:
            return None
        scores[index] = float(score)

    if any(score is None for score in scores):
        return None
    return [float(score) for score in scores if score is not None]
