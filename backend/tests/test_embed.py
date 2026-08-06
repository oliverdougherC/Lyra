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
    """Answers like llama.cpp's /v1/embeddings and /tokenize, recording what was sent."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shuffled: bool = False,
        dim: int = EMBEDDING_DIM,
        chars_per_token: int = 1,
        status: int = 200,
    ) -> None:
        self.batches: list[list[str]] = []
        self.urls: list[str] = []
        self.tokenized: list[str] = []
        self._shuffled = shuffled
        self._dim = dim
        # One token per character by default, which is the worst ratio observed on real
        # documents and the one that makes an over-long input deterministic in a test.
        self._chars_per_token = chars_per_token
        self._status = status
        monkeypatch.setattr(EmbeddingServer, "ensure_running", lambda _self: None)
        monkeypatch.setattr(embed, "_client", self._make_client)

    def _make_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), timeout=1.0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tokenize"):
            content = json.loads(request.content)["content"]
            self.tokenized.append(content)
            count = max(1, len(content) // self._chars_per_token)
            return httpx.Response(200, json={"tokens": list(range(count))})

        inputs = json.loads(request.content)["input"]
        self.batches.append(inputs)
        self.urls.append(str(request.url))
        if self._status != 200:
            return httpx.Response(self._status, json={"error": {"message": "refused"}})
        data = [
            {"object": "embedding", "index": position, "embedding": [float(position)] * self._dim}
            for position in range(len(inputs))
        ]
        if self._shuffled:
            data.reverse()
        return httpx.Response(200, json={"object": "list", "data": data})


def _long_text(characters: int) -> str:
    """Text with word boundaries to split at, of a known length."""
    word = "convolution "
    return (word * (characters // len(word) + 1))[:characters].strip()


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


def test_a_healthy_embedding_port_is_not_started_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess outlives the backend that started it, so a restart meets it again.

    It is spawned in its own session and `./run` frees only the backend and frontend
    ports, so a restarted backend routinely finds a healthy embedding server on 8081.
    Spawning a second one cannot bind the port, so it exits, and every upload after a
    restart failed at the embedding stage telling the student to download a model they
    already had.
    """
    monkeypatch.setattr(EmbeddingServer, "_healthy", lambda _self: True)
    monkeypatch.setattr(
        EmbeddingServer,
        "start",
        lambda _self: pytest.fail("A second server was spawned onto a held port"),
    )

    EmbeddingServer().ensure_running()


def test_a_silent_embedding_port_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(EmbeddingServer, "_healthy", lambda _self: False)
    monkeypatch.setattr(EmbeddingServer, "start", lambda _self: started.append(True))

    EmbeddingServer().ensure_running()

    assert started == [True]


def test_a_short_input_is_never_tokenized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token is at least one character, so a short string needs no asking about.

    This is what keeps every ordinary chunk and every query off the tokenizer, so the
    real-token check costs nothing on the documents that never needed it.
    """
    stub = StubEmbeddingApi(monkeypatch)

    embed_documents(["alpha", "beta"])
    embed_query("what is due")

    assert stub.tokenized == []


def test_an_oversized_chunk_is_split_rather_than_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure that a 608-page textbook found.

    `MAX_CHUNK_TOKENS` is measured with `estimate_tokens` at four characters to a token,
    and dense mathematics runs closer to two, so a chunk the chunker called 2047 tokens
    reached the server as 2607 and was refused. One such chunk failed the whole document,
    reported as an unreachable endpoint.
    """
    stub = StubEmbeddingApi(monkeypatch)

    vectors = embed_documents([_long_text(5000)])

    assert len(vectors) == 1
    embedded = [piece for batch in stub.batches for piece in batch]
    assert len(embedded) > 1, "the oversized input should have been split"
    assert all(len(piece) <= embed.EMBEDDING_CONTEXT_TOKENS for piece in embedded)
    assert all(piece.startswith("search_document: ") for piece in embedded), (
        "every piece needs the task prefix, not just the first"
    )


def test_a_split_chunk_returns_one_unit_length_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval compares with cosine distance on the strength of stored vectors being
    unit length, and the mean of unit vectors is not one."""
    StubEmbeddingApi(monkeypatch)

    [vector] = embed_documents([_long_text(5000)])

    assert len(vector) == EMBEDDING_DIM
    assert sum(value * value for value in vector) ** 0.5 == pytest.approx(1.0)


def test_splitting_keeps_one_vector_per_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller zips chunks to vectors strictly, so a split may never change the count."""
    StubEmbeddingApi(monkeypatch)

    vectors = embed_documents(["alpha", _long_text(5000), "beta"])

    assert len(vectors) == 3


def test_text_with_nowhere_to_split_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbroken run over the limit is a dead end, and it says which one.

    Left to the server this arrives as a 400 that the client used to report as
    `did not respond`, which is a true statement about neither the server nor the file.
    """
    StubEmbeddingApi(monkeypatch)

    with pytest.raises(UpstreamError) as caught:
        embed_documents(["x" * 5000])

    assert "unbroken run" in str(caught.value)


def test_a_refusal_is_not_reported_as_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that answered 400 is not a server that was not there."""
    StubEmbeddingApi(monkeypatch, status=400)

    with pytest.raises(UpstreamError) as caught:
        embed_documents(["alpha"])

    assert "did not respond" not in str(caught.value)


def test_estimate_tokens_never_returns_zero() -> None:
    # A zero would let a budget loop admit an unbounded number of short chunks.
    assert estimate_tokens("") == 1
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 8) == 2
