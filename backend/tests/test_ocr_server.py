"""Guards for the optional specialist OCR runtime.

Nothing here starts a several-gigabyte model. What is worth testing is the behaviour
around it: that its absence is an ordinary state with a message a student can act on,
that it never contends with the embedding server, and that the reference invocation the
GGUF publisher documented is the one actually spawned.
"""

from pathlib import Path

import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import ocr_server as module
from backend.llm.ocr_server import OcrServer


@pytest.fixture
def server() -> OcrServer:
    """A fresh instance, so no test inherits another's cached binary or process."""
    return OcrServer()


def _install_weights() -> None:
    """Put believable files where the weights go. Their contents are never read here."""
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.ocr_model_path.write_bytes(b"GGUF not really")
    settings.ocr_mmproj_path.write_bytes(b"GGUF not really either")


def test_the_weights_being_absent_is_a_state_rather_than_an_error(server: OcrServer) -> None:
    """The ordinary case. They are 2.8 GB and downloaded only when asked for, so every
    path has to cope with them missing."""
    assert server.available is False


def test_starting_without_the_weights_says_what_to_run_and_that_it_is_optional(
    server: OcrServer,
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        server.ensure_running()

    message = caught.value.message
    assert "fetch_models.py --ocr" in message
    # And that going without it is fine, because it is: recognition works through the
    # configured vision model, and a student told only "not installed" would think it broke.
    assert "configured in Settings" in message


def test_it_never_contends_with_the_embedding_server(server: OcrServer) -> None:
    """Two models on two ports. One server swapping models would make every page wait for
    a reload, which is the cost this whole path exists to avoid."""
    assert server.port != settings.llama_port
    assert server.base_url.startswith("http://127.0.0.1:")


def test_a_missing_runtime_is_reported_separately_from_missing_weights(
    server: OcrServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different things to go and do, so they are two different messages."""
    _install_weights()
    monkeypatch.setattr(OcrServer, "_find_binary", lambda self: None)

    with pytest.raises(ConfigurationError) as caught:
        server.ensure_running()

    assert caught.value.message == module.MISSING_BINARY_MESSAGE


def test_the_spawned_command_is_the_publisher_s_reference_invocation(
    server: OcrServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sampler flags are load-bearing and easy to lose in a refactor.

    llama.cpp has no `no_repeat_ngram_size`, so DRY stands in for it, deliberately weakly:
    tightening it garbles the model's table output. The chat template is the other half -
    dropping it is what made this model run at reduced quality in the upstream issue.
    """
    _install_weights()
    spawned: list[list[str]] = []

    class _Fake:
        def poll(self) -> int | None:
            return None

    def fake_popen(argv: list[str], **kwargs: object) -> _Fake:
        spawned.append(argv)
        return _Fake()

    monkeypatch.setattr(OcrServer, "_find_binary", lambda self: Path("llama-server"))
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OcrServer, "_await_health", lambda self, process: None)

    server.ensure_running()

    argv = spawned[0]
    assert argv[argv.index("--mmproj") + 1] == str(settings.ocr_mmproj_path)
    assert argv[argv.index("--chat-template") + 1] == "deepseek-ocr"
    assert "--no-jinja" in argv
    assert argv[argv.index("--temp") + 1] == "0"
    assert argv[argv.index("--dry-multiplier") + 1] == "0.8"
    # Server-only, and the reason the tables survive. llama-server suppresses special
    # tokens by default and this model's layout lives in them.
    assert "--special" in argv
    assert argv[argv.index("--port") + 1] == str(server.port)


def test_an_output_ceiling_exists_because_repetition_loops_are_the_failure_mode(
    server: OcrServer,
) -> None:
    """A dense page can send this model into a loop, and the weak DRY guard is deliberate.
    So the ceiling is what stops it, and a page that hits it fails rather than hangs."""
    assert module.MAX_OUTPUT_TOKENS == 4096
