"""Deterministic stop/admission interleavings; all processes and files are synthetic."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.errors import ConfigurationError
from backend.llm import llama_server
from backend.llm.embed_server import EmbeddingServer
from backend.llm.ocr_server import OcrServer
from backend.llm.rerank_server import RerankServer
from backend.tests.test_llama_server_lifecycle import _FAKE_TOKEN, _AliveProcess, _FakeTimer


@pytest.fixture(params=[EmbeddingServer, RerankServer, OcrServer])
def helper(request, monkeypatch, tmp_path):
    instance = request.param()
    monkeypatch.setattr(llama_server, "_RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(llama_server, "_OWNERSHIP_FILE", tmp_path / "ownership.json")
    monkeypatch.setattr(instance, "_timer_factory", _FakeTimer)
    monkeypatch.setattr(instance, "_healthy", lambda: instance._process is not None)
    monkeypatch.setattr(
        instance, "_start_locked", lambda: setattr(instance, "_process", _AliveProcess())
    )
    instance._process = _AliveProcess()
    instance._idle_since = 0
    monkeypatch.setattr(instance, "_monotonic", lambda: 1000)
    return instance


@pytest.mark.parametrize("boundary", ["idle_decision", "termination"])
@pytest.mark.parametrize("ownership", ["tracked", "adopted"])
def test_eviction_serializes_new_lease_through_complete_stop(
    helper, monkeypatch, boundary, ownership
):
    paused = threading.Event()
    resume = threading.Event()
    attempted = threading.Event()
    old = helper._process
    observations = []
    if ownership == "adopted":
        helper._process = None
        helper._adopted_pid = old.pid
        helper._adopted_pgid = old.pid
        helper._adopted_start_token = _FAKE_TOKEN
        monkeypatch.setattr(
            llama_server, "_token_matches_pid", lambda pid, token: old.poll() is None
        )
        monkeypatch.setattr(helper, "_healthy", lambda: old.poll() is None)
        monkeypatch.setattr(helper, "_served_model", lambda: helper._model_path().name)
        llama_server._save_ownership(
            {
                helper._display_name: {
                    "pid": old.pid,
                    "pgid": old.pid,
                    "start_token": _FAKE_TOKEN,
                    "port": helper.port,
                    "model": helper._model_path().name,
                }
            }
        )

    def pause():
        paused.set()
        assert resume.wait(5)

    original_stop = helper._stop_internal

    def stop(**kwargs):
        if boundary == "idle_decision":
            pause()
        return original_stop(**kwargs)

    def terminate(process):
        if boundary == "termination":
            pause()
        observations.append(helper._lease_count)
        process.terminate()

    monkeypatch.setattr(helper, "_stop_internal", stop)
    monkeypatch.setattr(llama_server, "_terminate", terminate)
    monkeypatch.setattr(llama_server, "_terminate_pid", lambda *args: terminate(old) or True)

    def request_lease():
        attempted.set()
        with helper.lease():
            return helper._process

    with ThreadPoolExecutor(max_workers=2) as pool:
        eviction = pool.submit(helper.evict_if_idle)
        assert paused.wait(5)

        # Probe lock ownership from the contender, without timing or sleeps.
        def contender():
            available = helper._lock.acquire(blocking=False)
            if available:
                helper._lock.release()
            attempted.set()
            return available, request_lease()

        lease = pool.submit(contender)
        assert attempted.wait(5)
        resume.set()
        assert eviction.result(timeout=5)
        available, acquired = lease.result(timeout=5)
    assert not available, "stop decision/termination released lifecycle admission"
    assert observations == [0]
    assert acquired is not old
    assert acquired.poll() is None


def test_quit_rejects_every_new_admission(helper, monkeypatch):
    monkeypatch.setattr(llama_server, "_terminate", lambda process: process.terminate())
    helper.stop_for_app_quit()
    with pytest.raises(ConfigurationError, match="shutting down"), helper.lease():
        pytest.fail("lease admitted after quit")
    with pytest.raises(ConfigurationError, match="shutting down"):
        helper.ensure_running()
    with pytest.raises(ConfigurationError, match="shutting down"):
        helper.start()
    assert helper.active_leases == 0
    assert helper._idle_timer is None


def test_failed_stop_preserves_tracked_helper_and_rechecks_health(helper, monkeypatch):
    old = helper._process
    helper._last_health_ok = float("inf")
    monkeypatch.setattr(llama_server, "_terminate", lambda process: None)
    helper.stop()
    assert helper._process is old
    monkeypatch.setattr(helper, "_healthy", lambda: False)
    with pytest.raises(ConfigurationError, match="could not be terminated"), helper.lease():
        pytest.fail("surviving helper reused without health verification")
    assert helper._process is old
    assert helper.active_leases == 0


def test_quit_closes_admission_while_another_transition_holds_lock(helper, monkeypatch):
    closed = threading.Event()
    original_close = helper.close_admission_for_app_quit

    def close():
        original_close()
        closed.set()

    monkeypatch.setattr(helper, "close_admission_for_app_quit", close)
    monkeypatch.setattr(llama_server, "_terminate", lambda process: process.terminate())
    with ThreadPoolExecutor(max_workers=1) as pool:
        with helper._lock:
            quitting = pool.submit(helper.stop_for_app_quit)
            assert closed.wait(5)
            with pytest.raises(ConfigurationError, match="shutting down"), helper.lease():
                pytest.fail("queued request won admission after quit began")
        quitting.result(timeout=5)
    assert helper._process is None


def test_quit_during_provisioning_prevents_subprocess_spawn(helper, monkeypatch, tmp_path):
    # Exercise the real start path: downloads already in flight can finish, but must
    # not spawn a child once quit admission closes.
    monkeypatch.setattr(
        helper, "_start_locked", lambda: llama_server.LlamaServer._start_locked(helper)
    )
    monkeypatch.setattr(helper, "_find_binary", lambda: tmp_path / "llama-server")
    monkeypatch.setattr(helper, "_ensure_weights", helper.close_admission_for_app_quit)
    monkeypatch.setattr(
        llama_server.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawn after quit")
    )
    with pytest.raises(ConfigurationError, match="shutting down"):
        helper.start()


def test_failed_eviction_retains_adopted_identity_and_retries(helper, monkeypatch):
    helper._process = None
    helper._adopted_pid = 99999
    helper._adopted_pgid = 99999
    helper._adopted_start_token = _FAKE_TOKEN
    monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: token == _FAKE_TOKEN)
    monkeypatch.setattr(llama_server, "_terminate_pid", lambda *args: False)
    llama_server._save_ownership(
        {
            helper._display_name: {
                "pid": 99999,
                "pgid": 99999,
                "start_token": _FAKE_TOKEN,
                "port": helper.port,
                "model": helper._model_path().name,
            }
        }
    )
    assert not helper.evict_if_idle()
    assert helper._adopted_pid == 99999
    assert helper._adopted_start_token == _FAKE_TOKEN
    assert helper._idle_timer.started
    assert llama_server._read_server_record(helper._display_name)["start_token"] == _FAKE_TOKEN
    monkeypatch.setattr(helper, "_healthy", lambda: False)
    with pytest.raises(ConfigurationError, match="could not be terminated"), helper.lease():
        pytest.fail("survivor bypassed health verification")


@pytest.mark.parametrize("durable_only", [False, True])
def test_healthy_survivor_is_quarantined_until_death_proof(helper, monkeypatch, durable_only):
    old = helper._process
    monkeypatch.setattr(llama_server, "_terminate", lambda process: None)
    monkeypatch.setattr(llama_server, "_terminate_pid", lambda *args: False)
    monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: old.poll() is None)
    monkeypatch.setattr(helper, "_healthy", lambda: True)
    llama_server._save_ownership(
        {
            helper._display_name: {
                "pid": old.pid,
                "pgid": old.pid,
                "start_token": _FAKE_TOKEN,
                "port": helper.port,
                "model": helper._model_path().name,
            }
        }
    )
    if durable_only:
        helper._process = None
    helper.stop()
    assert helper.status().state == "failed"
    with pytest.raises(ConfigurationError, match="could not be terminated"), helper.lease():
        pytest.fail("responsive survivor may still have a pending termination signal")
    old.terminate()
    monkeypatch.setattr(helper, "_healthy", lambda: False)
    with helper.lease():
        assert helper._process is not old
        assert helper._process.poll() is None


def test_quit_between_command_preparation_and_spawn_rejects_spawn(helper, monkeypatch, tmp_path):
    prepared = threading.Event()
    resume = threading.Event()

    def argv(binary):
        prepared.set()
        assert resume.wait(5)
        return [str(binary)]

    monkeypatch.setattr(helper, "_argv", argv)
    monkeypatch.setattr(
        llama_server.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawn after quit")
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        spawning = pool.submit(helper._spawn_and_await, tmp_path / "llama-server")
        assert prepared.wait(5)
        helper.close_admission_for_app_quit()
        resume.set()
        with pytest.raises(ConfigurationError, match="shutting down"):
            spawning.result(timeout=5)


def test_backend_restart_reclaims_durable_stop_intent_before_adoption(helper, monkeypatch):
    old = helper._process
    monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: old.poll() is None)
    monkeypatch.setattr(llama_server, "_terminate", lambda process: None)
    monkeypatch.setattr(llama_server, "_terminate_pid", lambda *args: False)
    llama_server._save_ownership(
        {
            helper._display_name: {
                "pid": old.pid,
                "pgid": old.pid,
                "start_token": _FAKE_TOKEN,
                "port": helper.port,
                "model": helper._model_path().name,
            }
        }
    )
    helper.stop()
    assert llama_server._read_server_record(helper._display_name)["stopping"] is True
    restarted = type(helper)()
    monkeypatch.setattr(restarted, "_timer_factory", _FakeTimer)
    monkeypatch.setattr(restarted, "_healthy", lambda: True)
    monkeypatch.setattr(restarted, "_served_model", lambda: helper._model_path().name)
    monkeypatch.setattr(
        restarted, "_start_locked", lambda: setattr(restarted, "_process", _AliveProcess())
    )
    with pytest.raises(ConfigurationError, match="could not be terminated"), restarted.lease():
        pytest.fail("backend restart adopted a helper still shutting down")
    assert restarted._adopted_pid is None
    old.terminate()
    monkeypatch.setattr(restarted, "_healthy", lambda: False)
    with restarted.lease():
        assert restarted._process is not old


def test_stop_intent_write_failure_does_not_signal(helper, monkeypatch):
    old = helper._process
    monkeypatch.setattr(llama_server, "_token_matches_pid", lambda pid, token: True)
    llama_server._save_ownership(
        {
            helper._display_name: {
                "pid": old.pid,
                "pgid": old.pid,
                "start_token": _FAKE_TOKEN,
                "port": helper.port,
                "model": helper._model_path().name,
            }
        }
    )

    def failed_write(data):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(llama_server, "_save_ownership", failed_write)
    monkeypatch.setattr(
        llama_server, "_terminate", lambda *args: pytest.fail("signal before durable intent")
    )
    with pytest.raises(ConfigurationError, match="shutdown could not be recorded"):
        helper.stop()
    assert old.poll() is None
    assert helper._stop_pending
