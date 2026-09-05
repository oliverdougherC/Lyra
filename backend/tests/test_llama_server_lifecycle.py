"""Contract tests for the health-aware server lifecycle, ownership, and adoption.

Every scenario here runs without a real `llama-server` binary, real models, or real
network. What is worth defending is the lifecycle logic itself: that liveness and health
are distinct, that ownership is verified before every signal, that PID reuse and
external servers cannot be confused with Lyra's own children, and that every Lyra-owned
server -- including one adopted after a backend restart -- is reclaimed on shutdown.
"""

import io
import os
import signal
import time
from pathlib import Path

import pytest

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import llama_server
from backend.llm.llama_server import (
    _load_ownership,
    _process_start_token,
    _read_server_record,
    _record_server,
    _remove_server_record,
    _save_ownership,
    _token_matches_pid,
)
from backend.llm.rerank_server import RerankServer

_FAKE_TOKEN = "proc:0"  # noqa: S105

# ------------------------------------------------------------------------------- helpers


def _install_weights() -> None:
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.rerank_model_path.write_bytes(b"GGUF not really")


class _AliveProcess:
    """A spawned child that stays alive: poll() returns None until killed."""

    pid = 99999

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = io.BytesIO(stderr)
        self._killed = False

    def poll(self) -> int | None:
        return 1 if self._killed else None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self._killed = True

    def terminate(self) -> None:
        self._killed = True


class _DeadProcess:
    """A spawned child that exited immediately."""

    pid = 99998

    def __init__(self, stderr: bytes = b"") -> None:
        self.stderr = io.BytesIO(stderr)

    def poll(self) -> int:
        return 1


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTimer:
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


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Every test gets its own ownership directory to prevent cross-contamination."""
    _set_ownership_dir(monkeypatch, tmp_path)


def test_packaged_ownership_defaults_to_application_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    data_dir = tmp_path / "Application Support" / "Lyra"  # type: ignore[operator]
    monkeypatch.setattr(settings, "packaged_mode", True)
    monkeypatch.setattr(settings, "data_dir", data_dir)

    assert llama_server._default_runtime_dir() == data_dir / ".runtime"


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

    def test_record_and_read_roundtrip(self) -> None:
        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge-reranker.gguf")
        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid
        assert record["port"] == 8083
        assert record["model"] == "bge-reranker.gguf"
        assert isinstance(record["start_token"], str)

    def test_remove_cleans_the_record(self) -> None:
        _record_server("reranking", os.getpid(), 8083, "bge-reranker.gguf")
        _remove_server_record("reranking")
        assert _read_server_record("reranking") is None

    def test_absent_file_loads_empty(self) -> None:
        assert _load_ownership() == {}

    def test_corrupt_file_raises_configuration_error(self) -> None:
        llama_server._OWNERSHIP_FILE.write_text("not json{{{")
        with pytest.raises(ConfigurationError, match="corrupt"):
            _load_ownership()

    def test_multiple_services_coexist(self) -> None:
        pid = os.getpid()
        _record_server("embedding", pid, 8081, "nomic.gguf")
        _record_server("reranking", pid, 8083, "bge.gguf")
        data = _load_ownership()
        assert "embedding" in data
        assert "reranking" in data

    def test_remove_one_preserves_others(self) -> None:
        pid = os.getpid()
        _record_server("embedding", pid, 8081, "nomic.gguf")
        _record_server("reranking", pid, 8083, "bge.gguf")
        _remove_server_record("reranking")
        data = _load_ownership()
        assert "embedding" in data
        assert "reranking" not in data

    def test_ownership_file_has_restrictive_permissions(self) -> None:
        _record_server("reranking", os.getpid(), 8083, "bge.gguf")
        mode = os.stat(llama_server._OWNERSHIP_FILE).st_mode
        assert mode & 0o777 == 0o600

    def test_record_server_raises_when_token_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: None)
        with pytest.raises(RuntimeError, match="Cannot establish birth identity"):
            _record_server("reranking", 12345, 8083, "bge.gguf")


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

        def mock_terminate(p: object) -> None:
            terminated.append(p)
            p._killed = True  # type: ignore[union-attr]

        monkeypatch.setattr(llama_server, "_terminate", mock_terminate)

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
        monkeypatch.setattr(
            llama_server,
            "_terminate",
            lambda p: setattr(p, "_killed", True),
        )

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


# ------------------------------------------------------------------ leases and idle eviction


class TestLeasesAndIdleEviction:
    def test_lease_blocks_idle_eviction_until_the_last_holder_releases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        process = _AliveProcess()
        clock = _Clock(100.0)
        timers: list[_FakeTimer] = []
        terminated: list[object] = []

        monkeypatch.setattr(instance, "_process", process)
        monkeypatch.setattr(instance, "ensure_running", lambda: None)
        monkeypatch.setattr(instance, "_check_installed", lambda: None)
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_monotonic", clock.monotonic)
        monkeypatch.setattr(
            instance,
            "_timer_factory",
            lambda delay, callback: timers.append(_FakeTimer(delay, callback)) or timers[-1],
        )
        monkeypatch.setattr(
            llama_server,
            "_terminate",
            lambda helper: terminated.append(helper) or setattr(helper, "_killed", True),
        )

        with instance.lease():
            with instance.lease():
                assert instance.active_leases == 2
                assert not timers
            assert instance.active_leases == 1
            assert not timers

        assert instance.active_leases == 0
        assert len(timers) == 1
        assert timers[0].started is True
        assert timers[0].delay == llama_server.DEFAULT_IDLE_TIMEOUT_SECONDS

        timers[0].fire()
        assert not terminated
        assert len(timers) == 2

        clock.advance(llama_server.DEFAULT_IDLE_TIMEOUT_SECONDS + 1)
        timers[-1].fire()

        assert terminated == [process]
        assert instance.status().state == "idle_evicted"

    def test_new_lease_cancels_pending_idle_timer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_weights()
        instance = RerankServer()
        process = _AliveProcess()
        clock = _Clock(10.0)
        timers: list[_FakeTimer] = []

        monkeypatch.setattr(instance, "_process", process)
        monkeypatch.setattr(instance, "ensure_running", lambda: None)
        monkeypatch.setattr(instance, "_check_installed", lambda: None)
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_monotonic", clock.monotonic)
        monkeypatch.setattr(
            instance,
            "_timer_factory",
            lambda delay, callback: timers.append(_FakeTimer(delay, callback)) or timers[-1],
        )

        with instance.lease():
            pass

        first = timers[-1]
        assert first.started is True
        with instance.lease():
            assert first.cancelled is True
            assert instance.active_leases == 1

    def test_lease_counter_recovers_when_start_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        monkeypatch.setattr(
            instance, "ensure_running", lambda: (_ for _ in ()).throw(ConfigurationError("nope"))
        )

        with pytest.raises(ConfigurationError, match="nope"), instance.lease():
            pass

        assert instance.active_leases == 0

    def test_evict_if_idle_never_stops_a_compatible_external_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        clock = _Clock(500.0)
        signals: list[object] = []

        monkeypatch.setattr(instance, "_monotonic", clock.monotonic)
        monkeypatch.setattr(instance, "_check_installed", lambda: None)
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/external/{settings.rerank_model_path.name}",
        )
        monkeypatch.setattr(llama_server, "_terminate", lambda helper: signals.append(helper))
        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda pid, pgid, token, label: signals.append(pid),
        )

        with instance._lock:
            instance._idle_since = clock.monotonic() - llama_server.DEFAULT_IDLE_TIMEOUT_SECONDS - 1

        assert instance.evict_if_idle() is False
        assert not signals
        assert instance.status().state == "ready"

    def test_stop_for_app_quit_cancels_idle_timer_and_stops_owned_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        process = _AliveProcess()
        clock = _Clock(50.0)
        timers: list[_FakeTimer] = []
        terminated: list[object] = []

        monkeypatch.setattr(instance, "_process", process)
        monkeypatch.setattr(instance, "ensure_running", lambda: None)
        monkeypatch.setattr(instance, "_check_installed", lambda: None)
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_monotonic", clock.monotonic)
        monkeypatch.setattr(
            instance,
            "_timer_factory",
            lambda delay, callback: timers.append(_FakeTimer(delay, callback)) or timers[-1],
        )
        monkeypatch.setattr(
            llama_server,
            "_terminate",
            lambda helper: terminated.append(helper) or setattr(helper, "_killed", True),
        )

        with instance.lease():
            pass

        timer = timers[-1]
        instance.stop_for_app_quit()

        assert timer.cancelled is True
        assert terminated == [process]
        assert instance.status().state == "stopped"


# ------------------------------------------------------------------ adoption


class TestAdoption:
    """Adoption of Lyra-owned servers after a backend restart."""

    def test_adoption_with_ownership_record_sets_adopted_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_adopted_server_is_reclaimed_on_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        pid = os.getpid()
        token = _process_start_token(pid)
        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        killed = [False]
        terminated_pids: list[int] = []

        def mock_terminate_pid(p: int, g: object, t: object, label: str) -> None:
            terminated_pids.append(p)
            killed[0] = True

        monkeypatch.setattr(llama_server, "_terminate_pid", mock_terminate_pid)
        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False if killed[0] else _token_matches_pid(p, t),
        )
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_stop_does_not_signal_external_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ------------------------------------------------------------------ status snapshots


class TestHelperStatus:
    def test_status_reports_not_installed_when_weights_are_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)

        status = instance.status()
        assert status.state == "not_installed"
        assert status.lease_count == 0

    def test_status_reports_loading_during_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        monkeypatch.setattr(instance, "_starting", True)

        status = instance.status()

        assert status.state == "loading"
        assert status.owned is True

    def test_status_reports_failed_during_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_failure_message", "start failed")
        monkeypatch.setattr(instance, "_failed_at", time.monotonic())
        monkeypatch.setattr(instance, "_healthy", lambda: False)

        status = instance.status()

        assert status.state == "failed"
        assert status.detail == "start failed"

    def test_status_reports_incompatible_when_the_port_holds_the_wrong_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(instance, "_served_model", lambda: "/models/wrong.gguf")

        status = instance.status()

        assert status.state == "incompatible"
        assert "wrong.gguf" in (status.detail or "")

    def test_status_reports_idle_evicted_after_idle_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_last_stop_reason", "idle_evicted")
        monkeypatch.setattr(instance, "_healthy", lambda: False)

        status = instance.status()

        assert status.state == "idle_evicted"


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

    def test_stale_record_with_dead_pid_is_cleaned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
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

        assert _read_server_record("reranking") is None
        assert instance._adopted_pid is None

    def test_reused_pid_with_wrong_token_is_not_adopted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = RerankServer()
        pid = os.getpid()
        real_token = _process_start_token(pid)
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

    def test_record_with_wrong_port_is_not_adopted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = RerankServer()
        pid = os.getpid()
        _record_server("reranking", pid, instance.port + 100, settings.rerank_model_path.name)

        killed = [False]
        terminated_pids: list[int] = []

        def mock_terminate_pid(p: int, g: object, t: object, label: str) -> None:
            terminated_pids.append(p)
            killed[0] = True

        monkeypatch.setattr(llama_server, "_terminate_pid", mock_terminate_pid)
        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False if killed[0] else _token_matches_pid(p, t),
        )
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

        instance.stop()
        assert not signals


# ------------------------------------------------------------------ concurrent starts


class TestConcurrentStarts:
    """The port race is handled gracefully: the loser's exit is the winner's success."""

    def test_losing_the_start_race_to_the_right_server_is_a_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

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
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)
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
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)
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
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)
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

    def test_spawn_records_ownership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: f"test:{pid}")

        spawned_process = _AliveProcess()
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: spawned_process)

        instance.ensure_running()

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == spawned_process.pid
        assert record["port"] == instance.port
        assert record["model"] == settings.rerank_model_path.name
        assert record["start_token"] == f"test:{spawned_process.pid}"

    def test_ownership_recorded_before_health_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ownership is recorded immediately after Popen, before _await_health.

        Finding 4: a crash between spawn and health-wait must not create an unowned
        orphan -- the record exists from the moment Popen returns.
        """
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: f"test:{pid}")

        record_at_health_time: list[dict | None] = []

        def check_record_then_pass(process: object) -> None:
            record_at_health_time.append(_read_server_record("reranking"))

        monkeypatch.setattr(instance, "_await_health", check_record_then_pass)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())

        instance.ensure_running()

        assert record_at_health_time[0] is not None
        assert record_at_health_time[0]["pid"] == _AliveProcess.pid

    def test_recording_failure_terminates_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding 5: if _record_server raises, the child is terminated rather than
        left as an unrecoverable orphan."""
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")

        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: None)

        terminated: list[object] = []
        monkeypatch.setattr(llama_server, "_terminate", lambda p: terminated.append(p))
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())

        with pytest.raises(ConfigurationError, match="ownership"):
            instance.ensure_running()

        assert len(terminated) == 1
        assert instance._process is None


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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

        assert _read_server_record("reranking") is None
        assert instance._adopted_pid is None

    def test_adopted_process_cleared_when_pid_dies_between_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_weights()
        instance = RerankServer()
        instance._adopted_pid = 2**30
        instance._adopted_pgid = 2**30
        instance._adopted_start_token = _FAKE_TOKEN
        instance._last_health_ok = 0.0

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

    def test_spawn_crash_adopt_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pid = os.getpid()
        token = _process_start_token(pid)
        port = settings.llama_port + 2
        _record_server("reranking", pid, port, settings.rerank_model_path.name)

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

        signals: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda g, s: signals.append((g, s)))
        # Simulate the adopted process dying after SIGTERM.
        monkeypatch.setattr(llama_server, "_token_matches_pid", lambda p, t: False)

        instance_b.stop()

        assert instance_b._adopted_pid is None
        assert _read_server_record("reranking") is None


# ------------------------------------------------------------------ failure injection


class TestFailureInjection:
    """What happens if Lyra dies or throws after every meaningful lifecycle transition."""

    def test_crash_after_spawn_before_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _read_server_record("reranking") is None

        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        assert instance._adopted_pid is None

    def test_crash_after_record_before_health(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


class TestTerminatePidProofOfDeath:
    def test_terminate_pid_escalates_to_sigkill_when_sigterm_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        signals: list[tuple[int, signal.Signals]] = []

        monkeypatch.setattr(llama_server.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(llama_server.time, "sleep", clock.advance)
        monkeypatch.setattr(llama_server, "_SHUTDOWN_GRACE_SECONDS", 1.0)
        monkeypatch.setattr(llama_server, "_POST_KILL_PROOF_SECONDS", 1.0)
        monkeypatch.setattr(llama_server, "_TERMINATION_POLL_SECONDS", 0.25)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

        def token_matches(pid: int, token: str | None) -> bool:
            return clock.now < 1.25

        monkeypatch.setattr(llama_server, "_token_matches_pid", token_matches)

        assert llama_server._terminate_pid(41, 42, "proc:owned", "reranking") is True
        assert signals == [(42, signal.SIGTERM), (42, signal.SIGKILL)]

    def test_terminate_pid_returns_false_when_process_survives_sigkill_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        signals: list[tuple[int, signal.Signals]] = []

        monkeypatch.setattr(llama_server.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(llama_server.time, "sleep", clock.advance)
        monkeypatch.setattr(llama_server, "_SHUTDOWN_GRACE_SECONDS", 1.0)
        monkeypatch.setattr(llama_server, "_POST_KILL_PROOF_SECONDS", 0.5)
        monkeypatch.setattr(llama_server, "_TERMINATION_POLL_SECONDS", 0.25)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: True)

        assert llama_server._terminate_pid(51, 52, "proc:owned", "reranking") is False
        assert signals == [(52, signal.SIGTERM), (52, signal.SIGKILL)]

    def test_terminate_pid_accepts_delayed_death_after_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        signals: list[tuple[int, signal.Signals]] = []

        monkeypatch.setattr(llama_server.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(llama_server.time, "sleep", clock.advance)
        monkeypatch.setattr(llama_server, "_SHUTDOWN_GRACE_SECONDS", 1.0)
        monkeypatch.setattr(llama_server, "_POST_KILL_PROOF_SECONDS", 1.0)
        monkeypatch.setattr(llama_server, "_TERMINATION_POLL_SECONDS", 0.25)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

        def token_matches(pid: int, token: str | None) -> bool:
            return clock.now < 1.5

        monkeypatch.setattr(llama_server, "_token_matches_pid", token_matches)

        assert llama_server._terminate_pid(61, 62, "proc:owned", "reranking") is True
        assert signals == [(62, signal.SIGTERM), (62, signal.SIGKILL)]

    def test_terminate_pid_never_signals_pid_reuse_or_foreign_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: False)
        monkeypatch.setattr(
            os,
            "killpg",
            lambda pgid, sig: pytest.fail("stale ownership must not signal a foreign process"),
        )
        monkeypatch.setattr(
            os,
            "kill",
            lambda pid, sig: pytest.fail("stale ownership must not signal a foreign process"),
        )

        assert llama_server._terminate_pid(71, 72, "proc:stale", "reranking") is True


# Finding 1: stop() durable record ----------------------------------------


class TestStopDurableRecord:
    """Finding 1: stop() must inspect the durable ownership record even when nothing
    was tracked or adopted during this backend lifetime."""

    def test_stop_reclaims_from_durable_record_when_untracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart followed by immediate shutdown (without ensure_running) must
        still terminate the owned helper found in the durable record."""
        pid = os.getpid()
        _process_start_token(pid)
        _record_server("reranking", pid, 8083, "bge.gguf")

        instance = RerankServer()
        assert instance._process is None
        assert instance._adopted_pid is None

        signals: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda g, s: signals.append((g, s)))

        instance.stop()

        assert len(signals) >= 1

    def test_stop_ignores_durable_record_for_dead_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:99999",
                    "pgid": 2**30,
                    "port": 8083,
                    "model": "bge.gguf",
                    "started_at": time.time(),
                }
            }
        )

        instance = RerankServer()
        signals: list[object] = []
        monkeypatch.setattr(os, "killpg", lambda g, s: signals.append((g, s)))
        monkeypatch.setattr(os, "kill", lambda p, s: signals.append((p, s)))

        instance.stop()

        assert not signals
        assert _read_server_record("reranking") is None

    def test_stop_never_signals_stale_record_for_live_foreign_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_ownership(
            {
                "reranking": {
                    "pid": os.getpid(),
                    "start_token": "proc:stale-bogus-token",
                    "pgid": os.getpgid(os.getpid()),
                    "port": 8083,
                    "model": "bge.gguf",
                    "started_at": time.time(),
                }
            }
        )

        instance = RerankServer()
        monkeypatch.setattr(
            os,
            "killpg",
            lambda g, s: pytest.fail("stale foreign ownership must not be signaled"),
        )
        monkeypatch.setattr(
            os,
            "kill",
            lambda p, s: pytest.fail("stale foreign ownership must not be signaled"),
        )

        instance.stop()

        assert _read_server_record("reranking") is None

    def test_stop_never_signals_other_service_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _record_server("embedding", os.getpid(), 8081, "nomic.gguf")

        instance = RerankServer()
        monkeypatch.setattr(
            os,
            "killpg",
            lambda g, s: pytest.fail("stop() must ignore ownership records for other services"),
        )
        monkeypatch.setattr(
            os,
            "kill",
            lambda p, s: pytest.fail("stop() must ignore ownership records for other services"),
        )

        instance.stop()

        assert _read_server_record("embedding") is not None

    def test_stop_never_signals_user_operated_compatible_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )
        monkeypatch.setattr(
            os,
            "killpg",
            lambda g, s: pytest.fail("user-operated compatible servers must not be signaled"),
        )
        monkeypatch.setattr(
            os,
            "kill",
            lambda p, s: pytest.fail("user-operated compatible servers must not be signaled"),
        )

        instance.stop()

    def test_stop_skips_durable_record_when_tracked_process_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a process is directly tracked, stop() handles it via _terminate()
        and does NOT also inspect the durable record for a second kill."""
        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge.gguf")

        instance = RerankServer()
        process = _AliveProcess()
        instance._process = process

        terminated: list[object] = []
        monkeypatch.setattr(llama_server, "_terminate", lambda p: terminated.append(p))

        durable_signals: list[object] = []
        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: durable_signals.append(p),
        )

        instance.stop()

        assert process in terminated
        assert not durable_signals


# Finding 2: ownership survives failed termination -------------------------


class TestOwnershipSurvivesFailedTermination:
    """Finding 2: the durable record is cleared only when the process is confirmed dead."""

    def test_record_preserved_when_process_survives_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the process survives SIGTERM+SIGKILL during stop(), its ownership
        record must be preserved so the next backend can try again."""
        pid = os.getpid()
        token = _process_start_token(pid)
        _record_server("reranking", pid, 8083, "bge.gguf")

        instance = RerankServer()
        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token

        monkeypatch.setattr(os, "killpg", lambda g, s: None)
        monkeypatch.setattr(os, "kill", lambda p, s: None)
        monkeypatch.setattr(llama_server, "_SHUTDOWN_GRACE_SECONDS", 0.01)

        instance.stop()

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid

    def test_record_removed_when_process_confirmed_dead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the PID is dead or reused after stop(), the record is cleaned."""
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:99999",
                    "pgid": 2**30,
                    "port": 8083,
                    "model": "bge.gguf",
                    "started_at": time.time(),
                }
            }
        )

        instance = RerankServer()
        instance.stop()

        assert _read_server_record("reranking") is None

    def test_verify_and_adopt_preserves_record_for_live_mismatched_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 2 corollary: a config-drift survivor that cannot be terminated
        raises ConfigurationError and its record is preserved for stop()."""
        pid = os.getpid()
        instance = RerankServer()
        _record_server("reranking", pid, instance.port + 100, settings.rerank_model_path.name)

        terminated_pids: list[int] = []
        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: terminated_pids.append(p),
        )

        with pytest.raises(ConfigurationError, match="could not be terminated"):
            instance.ensure_running()

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid

    def test_startup_failure_preserves_record_if_process_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If _await_health fails but the process is somehow still alive (wedged),
        the ownership record is preserved rather than removed."""
        _install_weights()
        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(llama_server, "_process_start_token", lambda pid: f"test:{pid}")

        wedged_process = _AliveProcess()
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: wedged_process)
        monkeypatch.setattr(llama_server, "_terminate", lambda p: None)
        monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, tok: True)

        def _health_fails(process: object) -> None:
            raise ConfigurationError("health check timed out")

        monkeypatch.setattr(instance, "_await_health", _health_fails)

        with pytest.raises(ConfigurationError):
            instance.ensure_running()

        record = _read_server_record("reranking")
        assert record is not None


# Finding 3: config drift reconciliation -----------------------------------


class TestConfigDriftReconciliation:
    """Finding 3: a port or model change must not silently orphan an old helper."""

    def test_port_change_terminates_old_owned_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the port changes between backend lifetimes, the old owned process
        on the previous port is terminated before launching on the new port."""
        pid = os.getpid()
        token = _process_start_token(pid)
        old_port = 9999
        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": token,
                    "pgid": os.getpgid(pid),
                    "port": old_port,
                    "model": settings.rerank_model_path.name,
                    "started_at": time.time(),
                }
            }
        )

        _install_weights()
        instance = RerankServer()
        assert instance.port != old_port

        killed = [False]
        terminated_pids: list[int] = []

        def mock_terminate_pid(p: int, g: object, t: object, label: str) -> None:
            terminated_pids.append(p)
            killed[0] = True

        monkeypatch.setattr(llama_server, "_terminate_pid", mock_terminate_pid)
        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False if killed[0] else _token_matches_pid(p, t),
        )
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert pid in terminated_pids

    def test_model_change_terminates_old_owned_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the model changes, the old owned process is terminated."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()
        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": token,
                    "pgid": os.getpgid(pid),
                    "port": instance.port,
                    "model": "old-model.gguf",
                    "started_at": time.time(),
                }
            }
        )

        _install_weights()
        killed = [False]
        terminated_pids: list[int] = []

        def mock_terminate_pid(p: int, g: object, t: object, label: str) -> None:
            terminated_pids.append(p)
            killed[0] = True

        monkeypatch.setattr(llama_server, "_terminate_pid", mock_terminate_pid)
        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False if killed[0] else _token_matches_pid(p, t),
        )
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)

        instance.ensure_running()

        assert pid in terminated_pids

    def test_matching_config_skips_reconciliation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No termination when the durable record matches current config."""
        pid = os.getpid()
        instance = RerankServer()
        _record_server("reranking", pid, instance.port, settings.rerank_model_path.name)

        terminated_pids: list[int] = []
        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: terminated_pids.append(p),
        )
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/m/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()

        assert pid not in terminated_pids

    def test_dead_stale_record_removed_without_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale record for a dead PID with drifted config is just removed."""
        instance = RerankServer()
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:0",
                    "pgid": 2**30,
                    "port": 9999,
                    "model": "old.gguf",
                    "started_at": time.time(),
                }
            }
        )

        signals: list[object] = []
        monkeypatch.setattr(llama_server, "_terminate_pid", lambda *a: signals.append(a))
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)
        _install_weights()

        instance.ensure_running()

        assert not signals
        assert _read_server_record("reranking") is None

    def test_surviving_stale_process_preserves_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the old helper survives termination after a config change, its record
        is preserved and ConfigurationError is raised to block replacement."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()
        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": token,
                    "pgid": os.getpgid(pid),
                    "port": 9999,
                    "model": "old.gguf",
                    "started_at": time.time(),
                }
            }
        )

        monkeypatch.setattr(os, "killpg", lambda g, s: None)
        monkeypatch.setattr(os, "kill", lambda p, s: None)
        monkeypatch.setattr(llama_server, "_SHUTDOWN_GRACE_SECONDS", 0.01)

        with pytest.raises(ConfigurationError, match="could not be terminated"):
            instance.ensure_running()

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid


# ------------------------------------------------------------------ Finding 6: adopted model check


class TestAdoptedModelCheck:
    """Finding 6: ensure_running must check whether an adopted process still serves
    the currently configured model."""

    def test_adopted_process_with_wrong_model_is_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the adopted process serves an obsolete model (config changed since
        adoption), it is terminated and a new server is launched."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()

        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": token,
                    "pgid": os.getpgid(pid),
                    "port": instance.port,
                    "model": "old-model.gguf",
                    "started_at": time.time(),
                }
            }
        )

        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        killed = [False]
        terminated_pids: list[int] = []

        def mock_terminate_pid(p: int, g: object, t: object, label: str) -> None:
            terminated_pids.append(p)
            killed[0] = True

        monkeypatch.setattr(llama_server, "_terminate_pid", mock_terminate_pid)
        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False if killed[0] else _token_matches_pid(p, t),
        )
        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(instance, "_find_binary", lambda: settings.models_dir / "llama-server")
        monkeypatch.setattr(instance, "_await_health", lambda p: None)
        monkeypatch.setattr(llama_server.subprocess, "Popen", lambda argv, **kw: _AliveProcess())
        monkeypatch.setattr(llama_server, "_record_server", lambda *a: None)
        _install_weights()

        instance.ensure_running()

        assert pid in terminated_pids
        assert instance._adopted_pid is None
        assert instance._process is not None

    def test_adopted_process_with_correct_model_is_health_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the adopted process has the right model, the normal health-check
        path runs instead of termination."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()

        _record_server("reranking", pid, instance.port, settings.rerank_model_path.name)

        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        monkeypatch.setattr(instance, "_healthy", lambda: True)

        instance.ensure_running()

        assert instance._adopted_pid == pid
        assert instance._unhealthy_restarts == 0

    def test_adopted_process_with_no_record_skips_model_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no ownership record exists for the adopted process, the model
        check gracefully falls through to the health-check path."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()

        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        monkeypatch.setattr(instance, "_healthy", lambda: True)

        instance.ensure_running()

        assert instance._adopted_pid == pid


# ------------------------------------------------------------------ survival blocks replacement


class TestSurvivalBlocksReplacement:
    """A proved-live process that survives termination blocks its own replacement."""

    def test_surviving_unhealthy_tracked_child_blocks_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a tracked child is unhealthy and survives _terminate, ConfigurationError
        is raised instead of proceeding to _start_locked."""
        instance = RerankServer()
        old_process = _AliveProcess()
        instance._process = old_process
        instance._last_health_ok = 0.0

        monkeypatch.setattr(instance, "_healthy", lambda: False)
        monkeypatch.setattr(llama_server, "_terminate", lambda p: None)

        with pytest.raises(ConfigurationError, match="could not be terminated"):
            instance.ensure_running()

        assert old_process.poll() is None

    def test_surviving_unhealthy_adopted_child_blocks_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an adopted child is unhealthy and survives _terminate_pid,
        ConfigurationError is raised."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()
        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: None,
        )
        monkeypatch.setattr(instance, "_healthy", lambda: False)

        _record_server("reranking", pid, instance.port, settings.rerank_model_path.name)

        with pytest.raises(ConfigurationError, match="could not be terminated"):
            instance.ensure_running()

        assert instance._adopted_pid == pid

    def test_surviving_wrong_model_adopted_child_blocks_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an adopted child with the wrong model survives _terminate_pid,
        ConfigurationError is raised."""
        pid = os.getpid()
        token = _process_start_token(pid)
        instance = RerankServer()

        _save_ownership(
            {
                "reranking": {
                    "pid": pid,
                    "start_token": token,
                    "pgid": os.getpgid(pid),
                    "port": instance.port,
                    "model": "old-model.gguf",
                    "started_at": time.time(),
                }
            }
        )

        instance._adopted_pid = pid
        instance._adopted_pgid = os.getpgid(pid)
        instance._adopted_start_token = token
        instance._last_health_ok = 0.0

        monkeypatch.setattr(
            llama_server,
            "_terminate_pid",
            lambda p, g, t, label: None,
        )

        with pytest.raises(ConfigurationError, match="could not be terminated"):
            instance.ensure_running()

        assert instance._adopted_pid == pid


# ------------------------------------------------------------------ overwrite refusal


class TestOverwriteRefusal:
    """_record_server refuses to overwrite a live record for a different process."""

    def test_record_server_refuses_overwrite_of_live_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge.gguf")

        fake_pid = 2**30 - 1
        monkeypatch.setattr(
            llama_server,
            "_process_start_token",
            lambda p: f"fake:{p}" if p == fake_pid else _process_start_token(p),
        )

        with pytest.raises(RuntimeError, match="different live process"):
            _record_server("reranking", fake_pid, 8083, "bge.gguf")

    def test_record_server_allows_same_process_update(self) -> None:
        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge.gguf")
        _record_server("reranking", pid, 8083, "bge-v2.gguf")

        record = _read_server_record("reranking")
        assert record is not None
        assert record["model"] == "bge-v2.gguf"

    def test_record_server_allows_overwrite_of_dead_process(self) -> None:
        _save_ownership(
            {
                "reranking": {
                    "pid": 2**30,
                    "start_token": "proc:0",
                    "pgid": 2**30,
                    "port": 8083,
                    "model": "bge.gguf",
                    "started_at": time.time(),
                }
            }
        )

        pid = os.getpid()
        _record_server("reranking", pid, 8083, "bge.gguf")

        record = _read_server_record("reranking")
        assert record is not None
        assert record["pid"] == pid


# ------------------------------------------------------------------ ownership file hardening


class TestOwnershipFileHardening:
    """Corrupt or unreadable ownership files fail closed."""

    def test_unreadable_file_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llama_server._OWNERSHIP_FILE.write_text("{}")
        llama_server._OWNERSHIP_FILE.chmod(0o000)
        try:
            with pytest.raises(ConfigurationError, match="cannot be read"):
                _load_ownership()
        finally:
            llama_server._OWNERSHIP_FILE.chmod(0o600)

    def test_non_dict_json_raises_configuration_error(self) -> None:
        llama_server._OWNERSHIP_FILE.write_text("[1, 2, 3]")
        with pytest.raises(ConfigurationError, match="unexpected content"):
            _load_ownership()

    def test_save_ownership_writes_complete_file(self) -> None:
        data = {"reranking": {"pid": 1, "port": 8083}}
        _save_ownership(data)
        loaded = _load_ownership()
        assert loaded == data

    def test_save_ownership_preserves_permissions(self) -> None:
        _save_ownership({"a": 1})
        mode = os.stat(llama_server._OWNERSHIP_FILE).st_mode
        assert mode & 0o777 == 0o600

    def test_save_ownership_atomic_on_write_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the write fails, the original file is not corrupted."""
        _save_ownership({"original": True})

        original_open = os.open

        def failing_open(path: str, flags: int, mode: int = 0) -> int:
            if path.endswith(".tmp"):
                raise OSError("disk full")
            return original_open(path, flags, mode)

        monkeypatch.setattr(os, "open", failing_open)

        with pytest.raises(OSError, match="disk full"):
            _save_ownership({"corrupted": True})

        loaded = _load_ownership()
        assert loaded == {"original": True}


# ----------------------------------------------------------- restart -> adopt -> shutdown


class TestRestartAdoptShutdownRecovery:
    """Full lifecycle: spawn, simulate backend restart, adopt, shut down cleanly."""

    def test_full_restart_adopt_shutdown_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pid = os.getpid()
        port = settings.llama_port + 2

        _record_server("reranking", pid, port, settings.rerank_model_path.name)

        instance = RerankServer()
        monkeypatch.setattr(instance, "_healthy", lambda: True)
        monkeypatch.setattr(
            instance,
            "_served_model",
            lambda: f"/models/{settings.rerank_model_path.name}",
        )

        instance.ensure_running()
        assert instance._adopted_pid == pid

        monkeypatch.setattr(
            llama_server,
            "_token_matches_pid",
            lambda p, t: False,
        )

        instance.stop()

        assert instance._adopted_pid is None
        assert _read_server_record("reranking") is None


# ------------------------------------------------------------------ binary resolution


def _stage_bundle_runtime(tmp_path: object) -> tuple[Path, Path]:
    """The app bundle's resource layout: the frozen backend's directory, with the
    runtime staged next to it, the way the Tauri build lays it out.

    Returns the backend's resource root (where the frozen backend's `resource_root`
    points) and the staged binary.
    """
    backend_dir = tmp_path / "resources" / "lyra-backend"
    backend_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    binary = tmp_path / "resources" / "llama" / "llama-b10287" / "llama-server"
    binary.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    binary.write_bytes(b"not a real binary")
    return backend_dir, binary


def _install_models_runtime() -> Path:
    """A runtime in the user's models directory, where the fetch flow puts it."""
    binary = settings.llama_dir / "llama-b10287" / "llama-server"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"not a real binary either")
    return binary


def test_a_runtime_in_the_models_directory_wins_over_the_bundled_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicitly installed runtime is a choice; the bundle is the fallback."""
    server = _make_server(monkeypatch)
    backend_dir, _ = _stage_bundle_runtime(tmp_path)
    models_binary = _install_models_runtime()
    monkeypatch.setattr(settings, "packaged_mode", True)
    monkeypatch.setattr(settings, "resource_root", backend_dir)

    assert server._find_binary() == models_binary


def test_the_bundled_runtime_is_used_when_the_models_directory_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The packaged clean install: no runtime in the user's models directory, so the
    runtime that ships inside the application is the one that serves."""
    server = _make_server(monkeypatch)
    backend_dir, bundled_binary = _stage_bundle_runtime(tmp_path)
    monkeypatch.setattr(settings, "packaged_mode", True)
    monkeypatch.setattr(settings, "resource_root", backend_dir)

    assert server._find_binary() == bundled_binary


def test_the_bundle_is_not_consulted_outside_packaged_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout resolves the runtime from its models directory only; the bundle
    location is an application fact and says nothing about a development install."""
    server = _make_server(monkeypatch)
    _stage_bundle_runtime(tmp_path)
    monkeypatch.setattr(settings, "resource_root", tmp_path / "resources" / "lyra-backend")

    assert server._find_binary() is None


def test_a_packaged_install_with_no_runtime_anywhere_gets_no_script_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing runtime in the product is a broken installation, so the wording says
    that - never a command to run in a checkout the student does not have."""
    monkeypatch.setattr(settings, "packaged_mode", True)
    server = _make_server(monkeypatch)

    with pytest.raises(ConfigurationError) as caught:
        server.ensure_running()

    assert caught.value.message == llama_server.PACKAGED_MISSING_RUNTIME_MESSAGE
    assert "fetch_models" not in caught.value.message
    assert "python" not in caught.value.message
