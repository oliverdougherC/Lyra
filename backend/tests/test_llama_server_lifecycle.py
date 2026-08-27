"""Contract tests for the health-aware server lifecycle, ownership, and adoption.

Every scenario here runs without a real `llama-server` binary, real models, or real
network. What is worth defending is the lifecycle logic itself: that liveness and health
are distinct, that ownership is verified before every signal, that PID reuse and
external servers cannot be confused with Lyra's own children, and that every Lyra-owned
server — including one adopted after a backend restart — is reclaimed on shutdown.
"""

import io
import os
import time

import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import llama_server
from backend.llm.llama_server import (
    _load_ownership,
    _process_start_token,
    _record_server,
    _remove_server_record,
    _token_matches_pid,
)
from backend.llm.rerank_server import RerankServer

_FAKE_TOKEN = "proc:0"  # noqa: S105

# ------------------------------------------------------------------------------- helpers


def _install_weights() -> None:
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.rerank_model_path.write_bytes(b"GGUF not really")


class _AliveProcess:
    """A spawned child that stays alive: poll() returns None."""

    pid = 99999

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = io.BytesIO(stderr)

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass

    def terminate(self) -> None:
        pass


class _DeadProcess:
    """A spawned child that exited immediately."""

    pid = 99998

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = io.BytesIO(stderr)

    def poll(self) -> int:
        return 1


def _make_server(monkeypatch: pytest.MonkeyPatch) -> RerankServer:
    """A fresh server that believes nothing is listening yet."""
    instance = RerankServer()
    monkeypatch.setattr(instance, "_healthy", lambda: False)
    return instance


def _set_ownership_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Point ownership file at the test's temp directory."""
    runtime_dir = tmp_path / "lyra_runtime"  # type: ignore[operator]
    runtime_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    monkeypatch.setattr(llama_server, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(llama_server, "_OWNERSHIP_FILE", runtime_dir / "server_ownership.json")


# ------------------------------------------------------------------ process identity


class TestProcessIdentity:
    """The birth token is the mechanism that prevents PID-reuse kills."""

    def test_own_process_has_a_token(self) -> None:
        token = _process_start_token(os.getpid())
        assert token is not None
        assert isinstance(token, str)

    def test_dead_pid_has_no_token(self) -> None:
        assert _process_start_token(2**30) is None

    def test_negative_pid_has_no_token(self) -> None:
        assert _process_start_token(-1) is None

    def test_token_matches_own_process(self) -> None:
        pid = os.getpid()
        token = _process_start_token(pid)
        assert _token_matches_pid(pid, token)

    def test_token_does_not_match_wrong_pid(self) -> None:
        token = _process_start_token(os.getpid())
        assert not _token_matches_pid(2**30, token)

    def test_none_token_never_matches(self) -> None:
        assert not _token_matches_pid(os.getpid(), None)

    def test_wrong_token_does_not_match(self) -> None:
        assert not _token_matches_pid(os.getpid(), "proc:0")


# ------------------------------------------------------------------ ownership file


class TestOwnershipFile:
    """Durable ownership records survive backend restarts."""

    def test_record_and_read_roundtrip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge-reranker.gguf")
        from backend.llm.llama_server import _read_server_record

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid
        assert record["port"] == 8083
        assert record["model"] == "bge-reranker.gguf"
        assert isinstance(record["start_token"], str)

    def test_remove_cleans_the_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        _record_server("reranking", os.getpid(), 8083, "bge-reranker.gguf")
        _remove_server_record("reranking")
        from backend.llm.llama_server import _read_server_record

        assert _read_server_record("reranking") is None

    def test_absent_file_loads_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        assert _load_ownership() == {}

    def test_corrupt_file_loads_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        llama_server._OWNERSHIP_FILE.write_text("not json{{{")
        assert _load_ownership() == {}

    def test_multiple_services_coexist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        pid = os.getpid()
        _record_server("embedding", pid, 8081, "nomic.gguf")
        _record_server("reranking", pid, 8083, "bge.gguf")
        data = _load_ownership()
        assert "embedding" in data
        assert "reranking" in data

    def test_remove_one_preserves_others(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        pid = os.getpid()
        _record_server("embedding", pid, 8081, "nomic.gguf")
        _record_server("reranking", pid, 8083, "bge.gguf")
        _remove_server_record("reranking")
        data = _load_ownership()
        assert "embedding" in data
        assert "reranking" not in data


# ------------------------------------------------------------------ lifecycle


class TestHealthAwareness:
    """Liveness and health are separate states."""

    def test_alive_and_healthy_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        process = _AliveProcess()
        instance._process = process
        instance._last_health_ok = 0.0
        monkeypatch.setattr(instance, "_healthy", lambda: True)

        instance.ensure_running()
        assert instance._process is process

    def test_alive_but_unhealthy_is_terminated_and_restarted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        old_process = _AliveProcess()
        instance._process = old_process
        instance._last_health_ok = 0.0

        terminated: list[object] = []
        monkeypatch.setattr(llama_server, "_terminate", lambda p: terminated.append(p))

        health_calls = [False, False]

        def _next_health() -> bool:
            return health_calls.pop(0) if health_calls else True

        monkeypatch.setattr(instance, "_healthy", _next_health)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(
            llama_server.subprocess,
            "Popen",
            lambda argv, **kw: _AliveProcess(),
        )
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert old_process in terminated
        assert instance._process is not old_process

    def test_recently_healthy_skips_recheck(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        instance._process = _AliveProcess()
        instance._last_health_ok = time.monotonic()

        health_called = []
        original_healthy = instance._healthy
        monkeypatch.setattr(
            instance,
            "_healthy",
            lambda: health_called.append(True) or original_healthy(),
        )

        instance.ensure_running()
        assert not health_called

    def test_unhealthy_restarts_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        instance._last_health_ok = 0.0
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(llama_server, "_terminate", lambda p: None)

        for i in range(llama_server._MAX_UNHEALTHY_RESTARTS):
            instance._process = _AliveProcess()
            if i < llama_server._MAX_UNHEALTHY_RESTARTS - 1:
                with pytest.raises(ConfigurationError):
                    instance.ensure_running()
                    break
            else:
                with pytest.raises(ConfigurationError, match="failed health checks"):
                    instance.ensure_running()

        assert instance._failure_message is not None

    def test_healthy_check_resets_unhealthy_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        instance._unhealthy_restarts = 2
        instance._process = _AliveProcess()
        instance._last_health_ok = 0.0
        monkeypatch.setattr(instance, "_healthy", lambda: True)

        instance.ensure_running()
        assert instance._unhealthy_restarts == 0


# ------------------------------------------------------------------ adoption


class TestAdoption:
    """Adoption of Lyra-owned servers after a backend restart."""

    def test_adoption_with_ownership_record_sets_adopted_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        pid = os.getpid()
        _record_server("reranking", pid, instance.port, settings.rerank_model_path.name)

        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        assert instance._adopted_pid == pid
        assert instance._adopted_start_token is not None

    def test_adopted_server_is_reclaimed_on_stop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        pid = os.getpid()
        token = _process_start_token(pid)
        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token

        signals_sent: list[tuple[int, int]] = []

        def mock_killpg(pgid: int, sig: int) -> None:
            signals_sent.append((pgid, sig))

        monkeypatch.setattr(os, "killpg", mock_killpg)

        instance.stop()

        assert len(signals_sent) >= 1
        assert instance._adopted_pid is None

    def test_adopted_unhealthy_server_is_terminated_and_restarted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        _install_weights()
        instance = RerankServer()
        pid = os.getpid()
        token = _process_start_token(pid)
        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        terminated_pids: list[int] = []
        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: terminated_pids.append(p),
        )
        # First health check: unhealthy (for the adopted process).
        # Second health check: nothing on port (before start).
        health_calls = iter([False, False])
        monkeypatch.setattr(instance, "_healthy", lambda: next(health_calls, True))
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(
            llama_server.subprocess,
            "Popen",
            lambda argv, **kw: _AliveProcess(),
        )
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert pid in terminated_pids
        assert instance._adopted_pid is None
        assert instance._process is not None


# ------------------------------------------------------------------ external servers


class TestExternalServer:
    """An external server with the right model is used but never claimed for shutdown."""

    def test_external_compatible_is_used_without_adoption(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/anywhere/{settings.rerank_model_path.name}",
        )
        monkeypatch.setattr(
            instance,
            "_start_locked",
            lambda: pytest.fail("spawned over an external server"),
        )

        instance.ensure_running()

        assert instance._adopted_pid is None
        assert instance._process is None

    def test_stop_does_not_signal_external_server(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/anywhere/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        signals: list[object] = []
        monkeypatch.setattr(llama_server, "_terminate_pid", lambda *a: signals.append(a))
        monkeypatch.setattr(llama_server, "_terminate", lambda p: signals.append(p))

        instance.stop()
        assert not signals


# ------------------------------------------------------------------ wrong model / unrelated


class TestWrongModelAndUnrelated:
    """Wrong model and unidentifiable port owners are refused."""

    def test_wrong_model_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: "/models/nomic-embed-text-v1.5.Q8_0.gguf",
        )

        with pytest.raises(ConfigurationError) as caught:
            instance.ensure_running()

        assert "nomic-embed-text" in caught.value.message
        assert settings.rerank_model_path.name in caught.value.message

    def test_unidentifiable_port_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(instance, "_served_model", lambda: None)

        with pytest.raises(ConfigurationError) as caught:
            instance.ensure_running()

        assert str(instance.port) in caught.value.message


# ------------------------------------------------------------------ PID reuse / stale records


class TestPidReuseAndStaleRecords:
    """Stale ownership records are cleaned, never acted on."""

    def test_stale_record_with_dead_pid_is_cleaned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        # Write a record for a PID that does not exist.
        from backend.llm.llama_server import _save_ownership

        llama_server._OWNERSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:0",
                    "pgid": 2**30,
                    "port": instance.port,
                    "model": settings.rerank_model_path.name,
                    "started_at": time.time(),
                }
            }
        )

        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/m/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        # The stale record is cleaned, and the server is used as external.
        from backend.llm.llama_server import _read_server_record

        assert _read_server_record("reranking") is None
        assert instance._adopted_pid is None

    def test_reused_pid_with_wrong_token_is_not_adopted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        pid = os.getpid()
        real_token = _process_start_token(pid)
        # Write a record with a deliberately wrong token (simulates PID reuse).
        from backend.llm.llama_server import _save_ownership

        llama_server._OWNERSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": real_token + "_shifted",
                    "pgid": os.getpgid(pid),
                    "port": instance.port,
                    "model": settings.rerank_model_path.name,
                    "started_at": time.time(),
                }
            }
        )

        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/m/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        assert instance._adopted_pid is None

    def test_record_with_wrong_port_is_not_adopted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        instance = RerankServer()
        pid = os.getpid()
        _record_server("reranking", pid, instance.port + 100, settings.rerank_model_path.name)

        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/m/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        assert instance._adopted_pid is None

    def test_stop_never_signals_a_dead_adopted_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = RerankServer()
        instance._adopted_pid = 2**30
        instance._adopted_pgid = 2**30
        instance._adopted_start_token = _FAKE_TOKEN

        signals: list[object] = []
        monkeypatch.setattr(os, "killpg", lambda g, s: signals.append((g, s)))
        monkeypatch.setattr(os, "kill", lambda p, s: signals.append((p, s)))
        monkeypatch.setattr(llama_server, "_remove_server_record", lambda s: None)

        instance.stop()
        assert not signals


# ------------------------------------------------------------------ concurrent starts


class TestConcurrentStarts:
    """The port race is handled gracefully: the loser's exit is the winner's success."""

    def test_losing_the_start_race_to_the_right_server_is_a_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        _install_weights()
        instance = RerankServer()
        answers = iter([False])
        monkeypatch.setattr(instance, "_healthy", lambda: next(answers, True))
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: settings.rerank_model_path.name,
        )
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **_: _DeadProcess())

        instance.ensure_running()


# ------------------------------------------------------------------ failed start cooldown


class TestFailedStartCooldown:
    """A broken start is remembered, not retried on every request."""

    def test_a_failed_start_is_remembered_rather_than_respawned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        server = _make_server(monkeypatch)
        monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
        spawns: list[list[str]] = []

        def dying(argv: list[str], **_: object) -> _DeadProcess:
            spawns.append(argv)
            return _DeadProcess(b"boom\n")

        monkeypatch.setattr(llama_server.subprocess, "Popen", dying)

        with pytest.raises(ConfigurationError):
            server.ensure_running()
        with pytest.raises(ConfigurationError):
            server.ensure_running()

        assert len(spawns) == 1

    def test_cooldown_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_weights()
        server = _make_server(monkeypatch)
        monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
        spawns: list[list[str]] = []

        def dying(argv: list[str], **_: object) -> _DeadProcess:
            spawns.append(argv)
            return _DeadProcess(b"boom\n")

        monkeypatch.setattr(llama_server.subprocess, "Popen", dying)

        with pytest.raises(ConfigurationError):
            server.ensure_running()
        server._failed_at -= llama_server._START_FAILURE_COOLDOWN_SECONDS + 1
        with pytest.raises(ConfigurationError):
            server.ensure_running()

        assert len(spawns) == 2

    def test_a_failed_start_quotes_the_child_s_last_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        server = _make_server(monkeypatch)
        monkeypatch.setattr(server, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(
            llama_server.subprocess,
            "Popen",
            lambda argv, **_: _DeadProcess(b"invalid magic\n"),
        )

        with pytest.raises(ConfigurationError) as caught:
            server.ensure_running()

        assert "invalid magic" in caught.value.message


# ------------------------------------------------------------------ ownership recording


class TestOwnershipRecording:
    """Spawning a server records ownership for later adoption."""

    def test_spawn_records_ownership(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: f"test:{pid}")

        spawned_process = _AliveProcess()
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: spawned_process)

        instance.ensure_running()

        from backend.llm.llama_server import _read_server_record

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == spawned_process.pid
        assert record["port"] == instance.port
        assert record["model"] == settings.rerank_model_path.name
        assert record["start_token"] == f"test:{spawned_process.pid}"

    def test_stop_cleans_ownership_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        _set_ownership_dir(monkeypatch, tmp_path)
        _record_server("reranking", os.getpid(), 8083, "bge.gguf")
        instance = RerankServer()

        instance.stop()

        from backend.llm.llama_server import _read_server_record

        assert _read_server_record("reranking") is None


# ------------------------------------------------------------------ shutdown after adoption


class TestShutdownAfterAdoption:
    """Backend lifespan shutdown reclaims every Lyra-owned server, including adopted ones."""

    async def test_lifespan_shutdown_reclaims_adopted_servers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend import main as main_module

        stopped: list[str] = []
        monkeypatch.setattr(
            main_module.embedding_server, "stop", lambda: stopped.append("embedding")
        )
        monkeypatch.setattr(main_module.ocr_server, "stop", lambda: stopped.append("ocr"))
        monkeypatch.setattr(main_module.rerank_server, "stop", lambda: stopped.append("rerank"))
        monkeypatch.setattr(main_module, "start_worker", lambda: None)
        monkeypatch.setattr(main_module.solver, "start_worker", lambda: None)

        async with main_module.lifespan(None):  # type: ignore[arg-type]
            pass

        assert sorted(stopped) == ["embedding", "ocr", "rerank"]


# ------------------------------------------------------------------ stale ownership cleanup


class TestStaleOwnershipCleanup:
    """Stale records are cleaned on every ensure_running that encounters them."""

    def test_stale_record_cleaned_when_port_healthy_with_right_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """A dead PID's record is cleaned, and the live server on the port is used
        as external (since the record doesn't match the live process)."""
        _set_ownership_dir(monkeypatch, tmp_path)
        from backend.llm.llama_server import _save_ownership

        llama_server._OWNERSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:99999",
                    "pgid": 2**30,
                    "port": settings.llama_port + 2,
                    "model": settings.rerank_model_path.name,
                    "started_at": 0,
                }
            }
        )
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: settings.rerank_model_path.name,
        )

        instance.ensure_running()

        from backend.llm.llama_server import _read_server_record

        assert _read_server_record("reranking") is None
        assert instance._adopted_pid is None

    def test_adopted_process_cleared_when_pid_dies_between_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an adopted process dies between ensure_running calls, clear the adoption
        and either re-adopt or start fresh."""
        _install_weights()
        instance = RerankServer()
        instance._adopted_pid = 2**30
        instance._adopted_pgid = 2**30
        instance._adopted_start_token = _FAKE_TOKEN
        instance._last_health_ok = 0.0

        # Nothing on the port → fall through to start.
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert instance._adopted_pid is None
        assert instance._process is not None


# ------------------------------------------------------------------ full crash-and-adopt cycle


class TestCrashAndAdoptCycle:
    """Simulate a backend crash followed by a fresh backend finding the orphan."""

    def test_spawn_crash_adopt_stop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """1) Backend A spawns a server and records ownership.
        2) Backend A crashes (simulate by creating a new LlamaServer instance).
        3) Backend B finds a healthy server on the port with a matching record.
        4) Backend B adopts it.
        5) Backend B stops it cleanly.
        """
        _set_ownership_dir(monkeypatch, tmp_path)

        # Step 1: Backend A spawns and records.
        pid = os.getpid()
        token = _process_start_token(pid)
        port = settings.llama_port + 2
        _record_server("reranking", pid, port, settings.rerank_model_path.name)

        # Step 2: Backend A crashes (fresh instance = no _process).

        # Step 3-4: Backend B finds and adopts.
        instance_b = RerankServer()
        monkeypatch.setattr(instance_b, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance_b,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )

        instance_b.ensure_running()

        assert instance_b._adopted_pid == pid
        assert instance_b._adopted_start_token == token

        # Step 5: Stop reclaims it.
        signals: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda g, s: signals.append((g, s)))

        instance_b.stop()

        assert len(signals) >= 1
        assert instance_b._adopted_pid is None
        from backend.llm.llama_server import _read_server_record

        assert _read_server_record("reranking") is None


# ------------------------------------------------------------------ failure injection


class TestFailureInjection:
    """What happens if Lyra dies or throws after every meaningful lifecycle transition."""

    def test_crash_after_spawn_before_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """If the backend crashes after spawning but before recording ownership,
        the next backend cannot adopt (no record) and starts fresh. The orphan process
        continues running, and the new spawn will either win the port race or the
        orphan will be detected as an external server with the right model."""
        _set_ownership_dir(monkeypatch, tmp_path)
        from backend.llm.llama_server import _read_server_record

        # No ownership record exists.
        assert _read_server_record("reranking") is None

        # Backend B finds a healthy server (the orphan).
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        # Used as external compatible — safe, no false ownership claim.
        assert instance._adopted_pid is None

    def test_crash_after_record_before_health(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """If the backend crashes after recording ownership but before the health
        check passed, the record exists but the server may or may not be healthy.
        A healthy server is adopted normally. An unhealthy/dead one's record is
        cleaned as stale."""
        _set_ownership_dir(monkeypatch, tmp_path)

        # Simulate: record exists for a dead PID.
        from backend.llm.llama_server import _save_ownership

        llama_server._OWNERSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:0",
                    "pgid": 2**30,
                    "port": settings.llama_port + 2,
                    "model": settings.rerank_model_path.name,
                    "started_at": time.time(),
                }
            }
        )

        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert instance._process is not None

    def test_stop_is_safe_when_nothing_was_ever_started(self) -> None:
        instance = RerankServer()
        instance.stop()
