"""Contract tests for reranking and its optional runtime.

Nothing here loads a 640 MB cross-encoder. What is worth defending is the behaviour
around it: that its absence is an ordinary configuration rather than a fault, that a
malformed reply is discarded whole rather than half-believed, and that a reply is put
back into the caller's order rather than read off in the order the server chose.
"""

import httpx
import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import rerank_server as server_module
from backend.llm.rerank_server import RerankServer
from backend.rag import rerank as rerank_module
from backend.rag.rerank import rerank


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> RerankServer:
    """A fresh instance that believes nothing is listening yet.

    `ensure_running` adopts whatever already answers on the port, deliberately, so on a
    machine where a real rerank server happens to be up these tests would take that branch
    and never reach the one they are about. The port is a fact about the machine; the start
    path is what is under test.
    """
    instance = RerankServer()
    monkeypatch.setattr(instance, "_healthy", lambda: False)
    return instance


class _Stub:
    """A rerank server that is whatever the test needs it to be.

    Stood in for the shared instance rather than patched onto it, because `available` is a
    read-only property that reads the disk, which is the thing being avoided here.
    """

    def __init__(self, *, available: bool, starts: bool = True) -> None:
        self.available = available
        self.base_url = "http://127.0.0.1:8083"
        self._starts = starts

    def ensure_running(self) -> None:
        if not self._starts:
            raise ConfigurationError("nope")


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tell `rag/rerank.py` the model is here, without putting 640 MB anywhere."""
    monkeypatch.setattr(rerank_module, "rerank_server", _Stub(available=True))


def _answers(payload: object, status: int = 200) -> object:
    """A `httpx.Client` stand-in that returns one canned `/v1/rerank` response."""

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    return lambda timeout: Client()


def test_the_weights_being_absent_is_a_state_rather_than_an_error(server: RerankServer) -> None:
    """The ordinary case on a machine that has not downloaded them."""
    assert server.available is False


def test_starting_without_the_weights_says_what_to_run_and_that_it_is_optional(
    server: RerankServer,
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        server.ensure_running()

    message = caught.value.message
    assert "fetch_models.py" in message
    # And that search still works without it, because it does. A student told only
    # "not installed" would think their class had broken.
    assert "embedding similarity" in message


def test_it_contends_with_neither_the_embedding_server_nor_the_ocr_server(
    server: RerankServer,
) -> None:
    """Three models on three ports. One server swapping models would make every turn wait
    for a reload."""
    from backend.llm.ocr_server import OCR_PORT_OFFSET

    assert server.port != settings.llama_port
    assert server.port != settings.llama_port + OCR_PORT_OFFSET


def test_reranking_mode_is_asked_for_explicitly(
    server: RerankServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--reranking` the model loads perfectly well and `/v1/rerank` answers 501,
    which is a failure that looks like a working server."""
    spawned: list[list[str]] = []
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.rerank_model_path.write_bytes(b"GGUF not really")
    monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
    monkeypatch.setattr(server, "_await_health", lambda process: None)

    def record(argv: list[str], **_: object) -> object:
        spawned.append(argv)

        class Process:
            def poll(self) -> None:
                return None

        return Process()

    monkeypatch.setattr(server_module.subprocess, "Popen", record)
    server.ensure_running()

    assert "--reranking" in spawned[0]


def test_no_passages_is_not_a_request(installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval returning nothing is normal, and asking a model to rank nothing is not."""
    monkeypatch.setattr(rerank_module.httpx, "Client", _answers({"results": []}))
    assert rerank("anything", []) is None


def test_scores_come_back_in_the_order_the_passages_went_out() -> None:
    """The server answers sorted by score and identifies each result by its index into the
    request, so the reply has to be put back rather than read off."""
    payload = {
        "results": [
            {"index": 2, "relevance_score": 4.0},
            {"index": 0, "relevance_score": -1.0},
            {"index": 1, "relevance_score": -9.0},
        ]
    }

    assert rerank_module._scores(payload, 3) == [-1.0, -9.0, 4.0]


def test_a_reply_that_skips_a_passage_is_discarded_whole() -> None:
    """A partial ranking silently demotes whatever went missing to last place, which is a
    wrong answer that looks like a working one."""
    payload = {"results": [{"index": 0, "relevance_score": 1.0}]}

    assert rerank_module._scores(payload, 3) is None


def test_a_reply_naming_the_same_passage_twice_is_discarded_whole() -> None:
    """It has the right length and cannot be trusted, which is the case a length check
    alone would let through."""
    payload = {
        "results": [
            {"index": 0, "relevance_score": 1.0},
            {"index": 0, "relevance_score": 2.0},
        ]
    }

    assert rerank_module._scores(payload, 2) is None


def test_an_index_outside_the_request_is_discarded_whole() -> None:
    payload = {"results": [{"index": 7, "relevance_score": 1.0}]}

    assert rerank_module._scores(payload, 1) is None


def test_a_server_failure_keeps_the_search_order(
    installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reranking improves an ordering that is already usable, so failing it must cost the
    improvement and nothing else."""
    monkeypatch.setattr(rerank_module.httpx, "Client", _answers({"error": "no"}, status=500))

    assert rerank("a question", ["one", "two"]) is None


def test_a_runtime_that_will_not_start_keeps_the_search_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rerank_module, "rerank_server", _Stub(available=True, starts=False))

    assert rerank("a question", ["one", "two"]) is None


def test_the_weights_being_absent_asks_the_server_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case, and it must not cost a request or a subprocess."""
    monkeypatch.setattr(rerank_module, "rerank_server", _Stub(available=False))

    def fail(*_: object, **__: object) -> None:
        raise AssertionError("reranking was attempted without the weights")

    monkeypatch.setattr(rerank_module.httpx, "Client", fail)

    assert rerank("a question", ["one", "two"]) is None
