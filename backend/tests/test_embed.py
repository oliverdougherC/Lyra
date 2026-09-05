"""Guards for the embedding call, where a regression is silent rather than loud.

A missing or swapped task prefix, or a batch reassembled in the wrong order, still returns
768 plausible floats. Nothing raises; retrieval just gets quietly worse. These tests fake
the HTTP boundary so the real request building, batching, and parsing all run.
"""

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError, UpstreamError
from backend.llm import llama_server, model_provisioning
from backend.llm.embed_server import EmbeddingServer
from backend.rag import embed
from backend.rag.embed import EMBEDDING_DIM, embed_documents, embed_query
from backend.rag.tokens import estimate_tokens


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the durable ownership file at a per-test directory.

    The provisioning tests below run the real spawn path, which records ownership;
    without this they would read and write the developer's real `.lyra` directory, and a
    live checkout backend would be a stranger in the middle of the test.
    """
    runtime_dir = tmp_path / "lyra_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(llama_server, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(llama_server, "_OWNERSHIP_FILE", runtime_dir / "server_ownership.json")


class _AliveProcess:
    """A spawned child that stays alive: poll() returns None until killed."""

    pid = 2**30

    def __init__(self) -> None:
        self._killed = False

    def poll(self) -> int | None:
        return 1 if self._killed else None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self._killed = True

    def terminate(self) -> None:
        self._killed = True


class _FakeTimer:
    """An idle-eviction timer that never fires on its own.

    The real one is a non-daemon `threading.Timer`; if it outlives the test it keeps the
    interpreter alive forever. Tests decide when eviction happens, by hand.
    """

    def __init__(self, delay: float, callback: object) -> None:
        self.delay = delay
        self._callback = callback
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self._callback()  # type: ignore[misc]


def _wire_a_fake_spawn(server: EmbeddingServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn that lands a live fake process, with no real binary, port, or network."""
    monkeypatch.setattr(server, "_healthy", lambda: False)
    monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
    monkeypatch.setattr(server, "_await_health", lambda process: None)
    monkeypatch.setattr(llama_server, "_record_server", lambda *args: None)
    monkeypatch.setattr(server, "_timer_factory", _FakeTimer)
    monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kwargs: _AliveProcess())


def _land_embedding_weights(**kwargs: object) -> str:
    """A successful `hf_hub_download` for the nomic file, in the models directory."""
    path = Path(str(kwargs["local_dir"])) / model_provisioning.EMBEDDING_WEIGHTS.filename
    path.write_bytes(b"GGUF fake")
    return str(path)


class StubEmbeddingApi:
    """Answers like llama.cpp's /v1/embeddings and /tokenize, recording what was sent."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        shuffled: bool = False,
        duplicate_indices: bool = False,
        dim: int = EMBEDDING_DIM,
        chars_per_token: int = 1,
        status: int = 200,
    ) -> None:
        self.batches: list[list[str]] = []
        self.urls: list[str] = []
        self.tokenized: list[str] = []
        self._shuffled = shuffled
        self._duplicate_indices = duplicate_indices
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
        if self._duplicate_indices:
            # A malformed reply that still has the right count: every item claims to be
            # the first input's vector.
            for item in data:
                item["index"] = 0
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
    # Adoption now also asks the occupant which model it loaded; that identity check
    # has its own tests, and here it is assumed to pass so the no-spawn claim is isolated.
    monkeypatch.setattr(EmbeddingServer, "_verify_and_adopt", lambda _self: None)
    monkeypatch.setattr(
        EmbeddingServer,
        "_start_locked",
        lambda _self: pytest.fail("A second server was spawned onto a held port"),
    )

    EmbeddingServer().ensure_running()


def test_a_silent_embedding_port_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(EmbeddingServer, "_healthy", lambda _self: False)
    monkeypatch.setattr(EmbeddingServer, "_start_locked", lambda _self: started.append(True))

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


def test_an_oversized_text_with_only_newlines_still_splits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table transcribed one row to a line has thousands of characters and no space.

    `_halve` used to cut only at the ASCII space despite its docstring saying "whichever
    whitespace", so a space-free but newline-separated text failed the whole document
    with a false "no word boundary" report. Any whitespace is a boundary.
    """
    stub = StubEmbeddingApi(monkeypatch)
    rows = "\n".join(f"|row{number}|value{number}|" for number in range(400))
    assert " " not in rows

    vectors = embed_documents([rows])

    assert len(vectors) == 1
    embedded = [piece for batch in stub.batches for piece in batch]
    assert len(embedded) > 1, "the newline-separated input should have been split"
    assert all(len(piece) <= embed.EMBEDDING_CONTEXT_TOKENS for piece in embedded)


def test_a_reply_that_reuses_an_index_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply must account for exactly every input once, like `rag/rerank.py` demands.

    Count alone is not enough: a reply of the right length whose items repeat an index
    would silently hand one chunk another chunk's vector, and retrieval would serve the
    misassignment forever with no symptom. Discarding the reply loudly is the only safe
    reading of it.
    """
    StubEmbeddingApi(monkeypatch, duplicate_indices=True)

    with pytest.raises(UpstreamError):
        embed_documents(["alpha", "beta"])


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


# ------------------------------------------------------------------ first use (PLA-402)


class TestFirstUseProvisioning:
    """A missing embedding model provisions itself on first use.

    A clean install has the runtime but not the weights. The first request that needs
    embeddings must land the weights in the application-managed models directory and
    continue - not hand the student a command to run, and not need a restart.
    """

    def test_a_missing_model_is_downloaded_on_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = EmbeddingServer()
        assert not settings.embedding_model_path.exists()
        _wire_a_fake_spawn(server, monkeypatch)

        downloads: list[tuple[str, str]] = []

        def fake_download(*, repo_id: str, filename: str, local_dir: object) -> str:
            downloads.append((repo_id, filename))
            return _land_embedding_weights(repo_id=repo_id, filename=filename, local_dir=local_dir)

        monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

        server.ensure_running()

        assert downloads == [
            ("nomic-ai/nomic-embed-text-v1.5-GGUF", "nomic-embed-text-v1.5.Q8_0.gguf")
        ]
        assert settings.embedding_model_path.exists()
        assert server._process is not None

    def test_the_request_that_found_the_model_missing_gets_its_vectors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`lease()` is the shape every real caller takes (`rag/embed.py`), and the whole
        point is that that same request continues after provisioning.

        `embed_documents` speaks to whatever `rag.embed` calls `embedding_server`, so a
        local instance is pointed at through that name - the real embed path, without
        leaving instance-level fakes on the process-wide singleton.
        """
        server = EmbeddingServer()
        monkeypatch.setattr(embed, "embedding_server", server)
        _wire_a_fake_spawn(server, monkeypatch)

        downloads: list[str] = []

        def fake_download(*, repo_id: str, filename: str, local_dir: object) -> str:
            downloads.append(filename)
            return _land_embedding_weights(repo_id=repo_id, filename=filename, local_dir=local_dir)

        monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

        def fake_client() -> httpx.Client:
            def handle(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/tokenize"):
                    return httpx.Response(200, json={"tokens": [0]})
                inputs = json.loads(request.content)["input"]
                data = [
                    {"object": "embedding", "index": i, "embedding": [0.1] * EMBEDDING_DIM}
                    for i in range(len(inputs))
                ]
                return httpx.Response(200, json={"object": "list", "data": data})

            return httpx.Client(transport=httpx.MockTransport(handle), timeout=1.0)

        monkeypatch.setattr(embed, "_client", fake_client)

        try:
            with server.lease():
                vectors = embed_documents(["hello"])

            assert len(vectors) == 1
            assert len(vectors[0]) == EMBEDDING_DIM
            assert downloads == ["nomic-embed-text-v1.5.Q8_0.gguf"]
            assert server.active_leases == 0
        finally:
            # Reclaim the fake child and cancel its idle-eviction timer.
            server.stop()

    def test_concurrent_first_uses_share_one_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = EmbeddingServer()
        _wire_a_fake_spawn(server, monkeypatch)

        downloads: list[str] = []

        def fake_download(*, repo_id: str, filename: str, local_dir: object) -> str:
            downloads.append(filename)
            time.sleep(0.05)
            return _land_embedding_weights(repo_id=repo_id, filename=filename, local_dir=local_dir)

        monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

        errors: list[Exception] = []

        def worker() -> None:
            try:
                server.ensure_running()
            except Exception as exc:  # noqa: BLE001 - the test records the outcome
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert downloads == ["nomic-embed-text-v1.5.Q8_0.gguf"]

    def test_a_failed_download_is_a_simple_retryable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = EmbeddingServer()
        _wire_a_fake_spawn(server, monkeypatch)

        def fake_download(*, repo_id: str, filename: str, local_dir: object) -> str:
            raise httpx.ConnectError("network unreachable")

        monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

        with pytest.raises(ConfigurationError) as caught:
            server.ensure_running()

        message = caught.value.message
        assert "could not be downloaded" in message
        assert "fetch_models" not in message
        assert str(settings.models_dir) not in message
        assert not settings.embedding_model_path.exists()
        # A download failure is not remembered as a spawn failure: the next request
        # retries the download instead of sitting in the start cooldown.
        assert server._failure_message is None

    def test_status_reports_not_installed_before_any_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = EmbeddingServer()
        monkeypatch.setattr(server, "_healthy", lambda: False)

        status = server.status()

        assert status.state == "not_installed"
        assert "fetch_models" not in (status.detail or "")

    def test_status_reports_downloading_while_provisioning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = EmbeddingServer()
        _wire_a_fake_spawn(server, monkeypatch)

        release = threading.Event()
        download_started = threading.Event()

        def fake_download(*, repo_id: str, filename: str, local_dir: object) -> str:
            download_started.set()
            release.wait()
            return _land_embedding_weights(repo_id=repo_id, filename=filename, local_dir=local_dir)

        monkeypatch.setattr(model_provisioning, "hf_hub_download", fake_download)

        downloader = threading.Thread(target=server.ensure_running)
        downloader.start()
        download_started.wait(timeout=5)
        try:
            # The snapshot must not wait for the lock the downloader is holding.
            status = server.status()

            assert status.state == "downloading"
            assert "embedding" in (status.detail or "").lower()
        finally:
            release.set()
            downloader.join(timeout=10)

        assert server.status().state == "ready"
