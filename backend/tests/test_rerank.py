"""Contract tests for reranking and its optional runtime.

Nothing here loads a 640 MB cross-encoder. What is worth defending is the behaviour
around it: that its absence is an ordinary configuration rather than a fault, that a
malformed reply is discarded whole rather than half-believed, and that a reply is put
back into the caller's order rather than read off in the order the server chose.
"""

import contextlib
import io

import httpx
import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import llama_server
from backend.llm.rerank_server import RerankServer
from backend.rag import rerank as rerank_module
from backend.rag.rerank import RerankStatus, rerank


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

    @contextlib.contextmanager
    def lease(self):  # type: ignore[no-untyped-def]
        """Match the production residency boundary without starting a real helper."""
        self.ensure_running()
        yield


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
            pid = 99999

            def poll(self) -> None:
                return None

        return Process()

    # The spawn now happens in the shared lifecycle, so it is patched there.
    monkeypatch.setattr(llama_server.subprocess, "Popen", record)
    monkeypatch.setattr(llama_server, "_record_server", lambda *_a, **_k: None)
    server.ensure_running()

    assert "--reranking" in spawned[0]


def _install_weights() -> None:
    """Put a believable file where the weights go. Its contents are never read here."""
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.rerank_model_path.write_bytes(b"GGUF not really")


class _DeadProcess:
    """A spawned child that lost the port or crashed on load: exits at once.

    Carries its stderr as a real stream so the shared lifecycle's drain thread reads it
    the way it reads a real pipe.
    """

    pid = 99998

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = io.BytesIO(stderr)

    def poll(self) -> int:
        return 1


def test_adoption_refuses_a_server_holding_the_wrong_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health answers 200 for any llama-server, so on its own it would adopt a stale
    text-only server as this one - which would then rank with the wrong model and look
    perfectly healthy doing it. The identity check is what refuses that."""
    instance = RerankServer()
    monkeypatch.setattr(instance, "_healthy", lambda: True)
    monkeypatch.setattr(
        instance, "_served_model", lambda: "/models/nomic-embed-text-v1.5.Q8_0.gguf"
    )

    with pytest.raises(ConfigurationError) as caught:
        instance.ensure_running()

    # Both models by name, so whoever reads it knows what is squatting and what was
    # wanted, rather than a bare "port in use".
    message = caught.value.message
    assert "nomic-embed-text-v1.5.Q8_0.gguf" in message
    assert settings.rerank_model_path.name in message


def test_adoption_refuses_a_port_that_will_not_say_what_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every llama-server identifies its model; something that answers /health but not
    /props or /v1/models is not a llama-server and must not be adopted as one."""
    instance = RerankServer()
    monkeypatch.setattr(instance, "_healthy", lambda: True)
    monkeypatch.setattr(instance, "_served_model", lambda: None)

    with pytest.raises(ConfigurationError) as caught:
        instance.ensure_running()

    assert str(instance.port) in caught.value.message


def test_adoption_accepts_a_server_holding_the_right_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart case adoption exists for: the subprocess outlived the backend, and
    starting over it would fail the bind and tell the student to download a model they
    already have."""
    instance = RerankServer()
    monkeypatch.setattr(instance, "_healthy", lambda: True)
    monkeypatch.setattr(
        instance, "_served_model", lambda: f"/anywhere/{settings.rerank_model_path.name}"
    )
    monkeypatch.setattr(
        instance,
        "_start_locked",
        lambda: pytest.fail("A second server was spawned over an adoptable one"),
    )

    instance.ensure_running()


def test_a_failed_start_quotes_the_child_s_last_words(
    server: RerankServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stderr used to go to DEVNULL, so "failed to start" carried zero information about
    a corrupt GGUF or a rejected flag. The tail of the child's stderr is the diagnosis,
    and it belongs in the error."""
    _install_weights()
    monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
    monkeypatch.setattr(
        llama_server.subprocess,
        "Popen",
        lambda argv, **_: _DeadProcess(b"llama_model_load: error loading model\ninvalid magic\n"),
    )
    monkeypatch.setattr(llama_server, "_record_server", lambda *_a, **_k: None)

    with pytest.raises(ConfigurationError) as caught:
        server.ensure_running()

    assert "invalid magic" in caught.value.message


def test_a_failed_start_is_remembered_rather_than_respawned(
    server: RerankServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ensure_running` is called on every retrieval, so without a cooldown a corrupt
    model file costs a spawn-load-crash cycle per question, forever."""
    _install_weights()
    monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
    spawns: list[list[str]] = []

    def dying(argv: list[str], **_: object) -> _DeadProcess:
        spawns.append(argv)
        return _DeadProcess(b"boom\n")

    monkeypatch.setattr(llama_server.subprocess, "Popen", dying)
    monkeypatch.setattr(llama_server, "_record_server", lambda *_a, **_k: None)

    with pytest.raises(ConfigurationError) as first:
        server.ensure_running()
    with pytest.raises(ConfigurationError) as second:
        server.ensure_running()

    assert len(spawns) == 1, "the second failure should be remembered, not re-earned"
    assert second.value.message == first.value.message

    # And the memory expires: rewind the clock past the cooldown and the next call is
    # allowed to try again, because the student may have re-downloaded the weights.
    server._failed_at -= llama_server._START_FAILURE_COOLDOWN_SECONDS + 1
    with pytest.raises(ConfigurationError):
        server.ensure_running()
    assert len(spawns) == 2


def test_losing_the_start_race_to_the_right_server_is_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent starts race for one bind and the loser's child exits. If the winner
    is healthy and holds the right model, that exit is not a failure and must not be
    reported as one."""
    _install_weights()
    instance = RerankServer()
    # Nothing on the port when this start begins; the rival wins the bind in between.
    answers = iter([False])
    monkeypatch.setattr(instance, "_healthy", lambda: next(answers, True))
    monkeypatch.setattr(instance, "_served_model", lambda: settings.rerank_model_path.name)
    monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
    monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **_: _DeadProcess())
    monkeypatch.setattr(llama_server, "_record_server", lambda *_a, **_k: None)

    instance.ensure_running()


async def test_shutdown_stops_every_llama_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The servers are spawned in their own sessions so a restart can adopt them, which
    means only the lifespan ever reclaims them. Stopping one of three left the other two
    holding hundreds of megabytes, forever."""
    from backend import main as main_module

    stopped: list[str] = []
    monkeypatch.setattr(
        main_module.embedding_server, "stop_for_app_quit", lambda: stopped.append("embedding")
    )
    monkeypatch.setattr(main_module.ocr_server, "stop_for_app_quit", lambda: stopped.append("ocr"))
    monkeypatch.setattr(
        main_module.rerank_server, "stop_for_app_quit", lambda: stopped.append("rerank")
    )
    # The workers are real threads and other tests' concern.
    monkeypatch.setattr(main_module, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.solver, "start_worker", lambda: None)

    async with main_module.lifespan(None):  # type: ignore[arg-type]
        pass

    assert sorted(stopped) == ["embedding", "ocr", "rerank"]


async def test_startup_does_not_warm_the_optional_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening Lyra must not load optional model weights before a feature uses them."""
    from backend import main as main_module

    _install_weights()
    monkeypatch.setattr(
        main_module.rerank_server,
        "ensure_running",
        lambda: pytest.fail("startup must not warm the optional reranker"),
    )
    monkeypatch.setattr(main_module, "start_worker", lambda: None)
    monkeypatch.setattr(main_module.solver, "start_worker", lambda: None)

    async with main_module.lifespan(None):  # type: ignore[arg-type]
        pass


def test_no_passages_is_not_a_request(installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieval returning nothing is normal, and asking a model to rank nothing is not."""
    monkeypatch.setattr(rerank_module.httpx, "Client", _answers({"results": []}))
    outcome = rerank("anything", [])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.EMPTY_INPUT


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

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.UPSTREAM_ERROR


def test_a_runtime_that_will_not_start_keeps_the_search_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rerank_module, "rerank_server", _Stub(available=True, starts=False))

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.START_REFUSED


def test_the_weights_being_absent_asks_the_server_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case, and it must not cost a request or a subprocess."""
    monkeypatch.setattr(rerank_module, "rerank_server", _Stub(available=False))

    def fail(*_: object, **__: object) -> None:
        raise AssertionError("reranking was attempted without the weights")

    monkeypatch.setattr(rerank_module.httpx, "Client", fail)

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.WEIGHTS_ABSENT


# ----------------------------------------------------------------- adversarial evidence tests


def test_timeout_is_distinguished_from_other_failures(
    installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout must produce TIMEOUT, not the generic UPSTREAM_ERROR."""

    class TimeoutClient:
        def __enter__(self) -> "TimeoutClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> object:
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(rerank_module.httpx, "Client", lambda timeout: TimeoutClient())

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.TIMEOUT


def test_malformed_response_is_distinguished_from_success(
    installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 response with the wrong shape must be MALFORMED_RESPONSE, not APPLIED."""
    monkeypatch.setattr(rerank_module.httpx, "Client", _answers({"results": [{"bad": "shape"}]}))

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.MALFORMED_RESPONSE


def test_invalid_json_is_distinguished_from_upstream_error(
    installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 response with non-JSON body must be INVALID_JSON, not UPSTREAM_ERROR."""

    class InvalidJsonClient:
        def __enter__(self) -> "InvalidJsonClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"not json at all",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(rerank_module.httpx, "Client", lambda timeout: InvalidJsonClient())

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores is None
    assert outcome.status == RerankStatus.INVALID_JSON


def test_successful_rerank_returns_applied(
    installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed response produces APPLIED with scores."""
    monkeypatch.setattr(
        rerank_module.httpx,
        "Client",
        _answers(
            {
                "results": [
                    {"index": 0, "relevance_score": 1.0},
                    {"index": 1, "relevance_score": -2.0},
                ]
            }
        ),
    )

    outcome = rerank("a question", ["one", "two"])
    assert outcome.scores == [1.0, -2.0]
    assert outcome.status == RerankStatus.APPLIED
