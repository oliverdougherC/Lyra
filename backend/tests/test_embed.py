"""Guards for the embedding call, where a regression is silent rather than loud.

A missing or swapped task prefix, or a batch reassembled in the wrong order, still returns
768 plausible floats. Nothing raises; retrieval just gets quietly worse. These tests fake
the HTTP boundary so the real request building, batching, and parsing all run.
"""

import json

import httpx
import pytest

from backend.core.errors import UpstreamError
from backend.llm.embed_server import EmbeddingServer
from backend.rag import embed
from backend.rag.embed import EMBEDDING_DIM, embed_documents, embed_query
from backend.rag.tokens import estimate_tokens


class StubEmbeddingApi:
    """Answers like llama.cpp's /v1/embeddings and records what was sent."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shuffled: bool = False,
        dim: int = EMBEDDING_DIM,
    ) -> None:
        self.batches: list[list[str]] = []
        self.urls: list[str] = []
        self._shuffled = shuffled
        self._dim = dim
        monkeypatch.setattr(EmbeddingServer, "ensure_running", lambda _self: None)
        monkeypatch.setattr(embed, "_client", self._make_client)

    def _make_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), timeout=1.0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        self.batches.append(inputs)
        self.urls.append(str(request.url))
        data = [
            {"object": "embedding", "index": position, "embedding": [float(position)] * self._dim}
            for position in range(len(inputs))
        ]
        if self._shuffled:
            data.reverse()
        return httpx.Response(200, json={"object": "list", "data": data})


def test_embed_documents_prefixes_every_input(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubEmbeddingApi(monkeypatch)

    embed_documents(["alpha", "beta"])

    assert stub.batches == [["search_document: alpha", "search_document: beta"]]
    assert stub.urls[0].endswith("/v1/embeddings")


def test_embed_documents_returns_input_order_despite_shuffled_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubEmbeddingApi(monkeypatch, shuffled=True)

    vectors = embed_documents(["alpha", "beta", "gamma"])

    # The stub seeds each vector with its own index, so input order is checkable.
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]
    assert stub.batches == [
        ["search_document: alpha", "search_document: beta", "search_document: gamma"]
    ]


def test_embed_query_sends_one_query_prefixed_input(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubEmbeddingApi(monkeypatch)

    vector = embed_query("what is due")

    assert stub.batches == [["search_query: what is due"]]
    assert len(vector) == EMBEDDING_DIM


def test_embed_documents_batches_at_sixteen(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubEmbeddingApi(monkeypatch)

    vectors = embed_documents([f"chunk {number}" for number in range(40)])

    assert [len(batch) for batch in stub.batches] == [16, 16, 8]
    assert len(vectors) == 40


def test_wrong_dimension_is_an_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    StubEmbeddingApi(monkeypatch, dim=512)

    with pytest.raises(UpstreamError):
        embed_documents(["alpha"])


def test_estimate_tokens_never_returns_zero() -> None:
    # A zero would let a budget loop admit an unbounded number of short chunks.
    assert estimate_tokens("") == 1
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 8) == 2
