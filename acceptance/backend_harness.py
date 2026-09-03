"""Acceptance backend harness -- production app with bounded model fixtures.

Replaces the local embedding model call with deterministic vectors and skips
profile extraction/consolidation.  The ingestion worker, SQLite storage, all
background workers, route logic, migrations, middleware, and every other
application component run unmodified.

The fake tutor endpoint (configured via PUT /api/settings after startup)
handles all interactive model calls: chat, study generation, solver, writer,
and agent chat.

Start with:
    LYRA_DATA_DIR=/tmp/acceptance uv run python -m uvicorn \
        acceptance.backend_harness:app --host 127.0.0.1 --port 8000
"""

import asyncio
import atexit
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import backend.core.ingestion as _ingest_mod
import backend.core.study as _study_mod
import backend.rag.embed as _embed_mod
from backend.llm.llama_server import (
    LlamaServer,
    _load_ownership,
    _process_start_token,
    _read_server_record,
    _remove_server_record,
    _save_ownership,
    _stop_tree,
)
from backend.rag.embed import EMBEDDING_DIM

# --------------------------------------------------------------------------
# Worker source-validation barrier (PLA-291 acceptance testing).
#
# When enabled, every call to study._validate_sources pauses at a threading
# barrier before calling the real implementation. This lets acceptance tests
# delete a source document BEFORE _validate_sources runs, exercising the
# exact PLA-291 worker re-validation guard.
# --------------------------------------------------------------------------

_source_barrier_event: threading.Event | None = None
_source_barrier_arrived = threading.Event()
_real_validate_sources = _study_mod._validate_sources


def _barrier_validate_sources(conn, job, class_id):
    global _source_barrier_event
    ev = _source_barrier_event
    if ev is not None:
        _source_barrier_arrived.set()
        ev.wait(timeout=30)
    return _real_validate_sources(conn, job, class_id)


_study_mod._validate_sources = _barrier_validate_sources


def _fake_embed_all(texts: list[str], prefix: str) -> list[list[float]]:
    """Deterministic 768-dim vectors keyed on text content.

    Each vector is unique per input (seeded from its character codes) and
    normalised to unit length.  The dimensionality matches the production
    nomic-embed-text-v1.5 model so sqlite-vec storage works unmodified.
    """
    vectors: list[list[float]] = []
    for text in texts:
        seed = sum(ord(c) for c in text[:200]) % 9973
        raw = [((seed * (i + 1)) % 9973) / 9973 for i in range(EMBEDDING_DIM)]
        norm = sum(v * v for v in raw) ** 0.5
        vectors.append([v / norm for v in raw])
    return vectors


_embed_mod._embed_all = _fake_embed_all
_ingest_mod.extract_facts = lambda conn, document_id, text, doc_type: None
_ingest_mod.consolidate_class = lambda conn, class_id: None

# --------------------------------------------------------------------------
# Backend failure accounting (PLA-292 acceptance gate).
#
# The REAL production FastAPI app is wrapped in a thin ASGI middleware that
# records every unexpected backend failure -- an unhandled request exception
# escaping the app, or an unexpected 5xx response -- with bounded privacy-safe
# metadata (method, route template, status/exception class, sequence number).
# No student content, query strings, path parameters, bodies, or credentials
# are recorded.
#
# This is instrumentation around the real app, not a test double: every route,
# dependency, and middleware below it runs unmodified. The wrapper becomes the
# module-level `app` that uvicorn imports, so every request flows through it.
# The final acceptance gate asserts zero unconsumed failures, so a hidden
# backend 500 can never hide behind passing Playwright assertions. Tests that
# intentionally exercise an expected 5xx consume that specific occurrence (by
# failure id or method+route) rather than globally suppressing a route or class.
# --------------------------------------------------------------------------
from starlette.routing import Match  # noqa: E402

from backend.main import app as _production_app  # noqa: E402


def _privacy_safe_route(scope) -> str:
    """Return the matched route template, never the concrete path.

    Concrete paths may embed student ids or content; the template (e.g.
    `/api/sessions/{session_id}/messages`) identifies the endpoint without it.
    Unroutable requests are reported by method only.
    """

    def candidates(route):
        # FastAPI wraps included routers in a lazy `_IncludedRouter` container that matches
        # its whole subtree but carries no path of its own: descend to the wrapped
        # router's concrete routes so their templates stay visible instead of the
        # container masking every endpoint behind it.
        wrapped = getattr(route, "original_router", None)
        if wrapped is not None:
            return list(getattr(wrapped, "routes", ()))
        return [route]

    try:
        for route in _production_app.routes:
            for candidate in candidates(route):
                match, _child = candidate.matches(scope)
                if match is not Match.FULL:
                    continue
                path = getattr(candidate, "path", None)
                if isinstance(path, str):
                    return path
                break
    except Exception:  # noqa: S110 - accounting must never break a request
        pass
    return "<unroutable>"


class BackendFailureAccounting:
    """Thread-safe ledger of unexpected backend failures for the acceptance lane."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: deque[dict] = deque(maxlen=200)
        self._consumed = 0
        self._total_recorded = 0
        self._seq = 0

    def record(
        self, *, method: str, route: str, kind: str, status: int | None, exc_type: str | None
    ) -> dict:
        with self._lock:
            self._seq += 1
            self._total_recorded += 1
            failure = {
                "id": self._seq,
                "method": method.upper(),
                "route": route,
                "kind": kind,  # 'unhandled_exception' | 'unexpected_5xx'
                "status": status,
                "exc_type": exc_type,
                "at": round(time.time(), 3),
            }
            self._failures.append(failure)
            return failure

    def consume(
        self, *, failure_id: int | None = None, method: str | None = None, route: str | None = None
    ) -> dict:
        """Consume one expected failure. Returns the consumed record or an error."""
        with self._lock:
            for failure in list(self._failures):
                if failure_id is not None and failure["id"] != failure_id:
                    continue
                if method is not None and failure["method"] != method.upper():
                    continue
                if route is not None and failure["route"] != route:
                    continue
                self._failures.remove(failure)
                self._consumed += 1
                return {"ok": True, "consumed": failure}
            return {"ok": False, "error": "no matching unconsumed failure"}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "unconsumed": list(self._failures),
                "unconsumed_count": len(self._failures),
                "consumed": self._consumed,
                "total_recorded": self._total_recorded,
            }


failure_accounting = BackendFailureAccounting()


class _AccountingApp:
    """ASGI wrapper that observes the production app for unexpected failures.

    Delegates every request to `_production_app` (the real FastAPI instance).
    It is a pure ASGI callable -- no middleware registration, no route changes --
    so the production app's own middleware stack and routing are untouched.
    """

    def __init__(self) -> None:
        self._inner = _production_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        status_seen: dict[str, int] = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_seen["status"] = int(message.get("status", 0))
            await send(message)

        try:
            await self._inner(scope, receive, send_wrapper)
        except Exception as exc:
            failure_accounting.record(
                method=method,
                route=_privacy_safe_route(scope),
                kind="unhandled_exception",
                status=status_seen.get("status"),
                exc_type=type(exc).__name__,
            )
            raise
        status = status_seen.get("status")
        if status is not None and 500 <= status < 600:
            failure_accounting.record(
                method=method,
                route=_privacy_safe_route(scope),
                kind="unexpected_5xx",
                status=status,
                exc_type=None,
            )


# The wrapper is what uvicorn imports as `app`; the production app lives inside it.
app = _AccountingApp()


# --------------------------------------------------------------------------
# Acceptance-only endpoints -- not part of the production API.
# --------------------------------------------------------------------------

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


@_production_app.get("/_acceptance/backend-failures")
async def _get_backend_failures() -> JSONResponse:
    """Snapshot of the failure ledger (bounded, privacy-safe)."""
    return JSONResponse(failure_accounting.snapshot())


@_production_app.post("/_acceptance/backend-failures/probe")
async def _probe_backend_failure() -> JSONResponse:
    """Deliberately produce one unhandled 500 for the accounting self-test.

    The accounting layer (which no longer excludes /_acceptance/ routes) records
    this as an unhandled_exception. The self-test then consumes it to prove the
    accounting machinery is live.
    """
    raise RuntimeError("Deliberate acceptance accounting probe")


@_production_app.post("/_acceptance/backend-failures/consume")
async def _consume_backend_failure(request: Request) -> JSONResponse:
    """Consume an expected failure a test intentionally produced.

    Match by `failure_id` (exact), or by method+route (one occurrence). This is
    per-occurrence accounting, never a blanket suppression of a route or class.
    """
    body = await request.json()
    result = failure_accounting.consume(
        failure_id=body.get("failure_id"),
        method=body.get("method"),
        route=body.get("route"),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


# --------------------------------------------------------------------------
# FakeHelperServer: LlamaServer subclass that spawns fake-helper.py
# through the production supervisor lifecycle (PLA-301 acceptance testing).
# --------------------------------------------------------------------------

_FAKE_HELPER_PATH = (
    Path(__file__).resolve().parent.parent / "frontend" / "e2e" / "acceptance" / "fake-helper.py"
)
_ACCEPTANCE_HELPER_PORT = 19_500
_fake_helper_instance: "FakeHelperServer | None" = None


class FakeHelperServer(LlamaServer):
    """Routes fake-helper.py through the production LlamaServer supervisor."""

    def __init__(
        self,
        model_name: str = "acceptance-test-model",
        fail_health: bool = False,
        slow_start: float = 0,
        display_name: str = "acceptance-helper",
    ) -> None:
        super().__init__(
            display_name=display_name,
            port_offset=0,
            health_timeout_seconds=30.0,
            missing_binary_message="fake helper missing",
            start_failed_message="fake helper failed to start",
        )
        self._fake_model_name = model_name
        self._fake_fail_health = fail_health
        self._fake_slow_start = slow_start

    @property
    def port(self) -> int:
        return _ACCEPTANCE_HELPER_PORT

    def _model_path(self) -> Path:
        return Path(self._fake_model_name)

    def _check_installed(self) -> None:
        pass

    def _find_binary(self) -> Path | None:
        return _FAKE_HELPER_PATH

    def _argv(self, binary: Path) -> list[str]:
        # The interpreter directly, not `uv run python`: fake-helper.py is stdlib-only,
        # and the extra wrapper layer makes the recorded/owned PID a `uv` process whose
        # real listener is a grandchild -- an unnecessary difference from the production
        # llama-server shape (owned PID == serving process) that only complicates
        # ownership assertions and reaping.
        args = [
            sys.executable,
            str(_FAKE_HELPER_PATH),
            "--port",
            str(self.port),
            "--model",
            self._fake_model_name,
        ]
        if self._fake_fail_health:
            args.append("--fail-health")
        if self._fake_slow_start > 0:
            args.extend(["--slow-start", str(self._fake_slow_start)])
        return args


def _reclaim_fake_helper_on_exit() -> None:
    """Stop the acceptance helper through production `LlamaServer.stop()` on any exit.

    The backend's lifespan shutdown stops the real embedding/OCR/rerank servers but has
    no knowledge of this harness-only helper instance. Without this, a SIGTERM to the
    backend (normal teardown or a crash) leaves the fake-helper process -- which
    `LlamaServer` deliberately spawns in its own session so it can outlive the parent for
    adoption testing -- running as an orphan and holding its port across runs. Routing
    the cleanup through production `stop()` (not a bare kill) keeps the reclaim path under
    test identical to the real one.
    """
    global _fake_helper_instance
    if _fake_helper_instance is not None:
        with contextlib.suppress(Exception):  # best-effort at interpreter exit
            _fake_helper_instance.stop()
        _fake_helper_instance = None


atexit.register(_reclaim_fake_helper_on_exit)


@_production_app.post("/_acceptance/helper/start")
async def _start_helper(request: Request) -> JSONResponse:
    """Start a fake helper through the production LlamaServer supervisor."""
    global _fake_helper_instance
    body = await request.json()
    model = body.get("model", "acceptance-test-model")
    fail_health = body.get("fail_health", False)
    slow_start = body.get("slow_start", 0)
    if _fake_helper_instance is not None:
        _fake_helper_instance.stop()
        _fake_helper_instance = None
    helper = FakeHelperServer(
        model_name=model,
        fail_health=fail_health,
        slow_start=slow_start,
    )
    try:
        helper.start()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    _fake_helper_instance = helper
    pid = helper._process.pid if helper._process else None
    token = _process_start_token(pid) if pid else None
    return JSONResponse(
        {
            "ok": True,
            "port": helper.port,
            "pid": pid,
            "birth_token": token,
        }
    )


@_production_app.post("/_acceptance/helper/stop")
async def _stop_helper() -> JSONResponse:
    """Stop the fake helper through the production LlamaServer supervisor."""
    global _fake_helper_instance
    if _fake_helper_instance is None:
        return JSONResponse({"ok": True, "was_running": False})
    _fake_helper_instance.stop()
    _fake_helper_instance = None
    return JSONResponse({"ok": True, "was_running": True})


@_production_app.get("/_acceptance/helper/status")
async def _helper_status() -> JSONResponse:
    """Return the supervisor's view of the fake helper."""
    if _fake_helper_instance is None:
        return JSONResponse({"running": False})
    helper = _fake_helper_instance
    pid = helper._process.pid if helper._process else None
    token = _process_start_token(pid) if pid else None
    healthy = helper._healthy()
    record = _read_server_record("acceptance-helper")
    return JSONResponse(
        {
            "running": pid is not None,
            "port": helper.port,
            "pid": pid,
            "birth_token": token,
            "healthy": healthy,
            "ownership_record": record,
        }
    )


@_production_app.post("/_acceptance/helper/cleanup-ownership")
async def _cleanup_helper_ownership() -> JSONResponse:
    """Remove the acceptance-helper ownership record (for test isolation)."""
    _remove_server_record("acceptance-helper")
    return JSONResponse({"ok": True})


@_production_app.post("/_acceptance/helper/cleanup")
async def _cleanup_helper_all() -> JSONResponse:
    """Full per-test isolation reset for the acceptance helper.

    Deterministically frees the helper port (kills any listener, retrying until it is
    verifiably free), stops the tracked harness helper instance, and clears BOTH ownership
    records ("acceptance-helper" and "acc-scenario"). This guarantees every helper test
    starts from a free port with no stale record -- regardless of how a prior spec/test
    left the fixture (a foreign wrong-model helper, an adopted survivor, etc.). Without it,
    one spec's deliberately-left-alive foreign process gets ADOPTED by the next spec's
    supervisor instead of being replaced, corrupting its assertions.
    """
    global _fake_helper_instance

    def _do_cleanup() -> None:
        # 1. Free the port (kill any listener, retry until free).
        deadline = time.monotonic() + 5.0
        while True:
            pids = _listener_pids_sync()
            if not pids:
                break
            for pid in pids:
                with contextlib.suppress(OSError, ValueError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        # 2. Stop the tracked harness helper instance (if any).
        if _fake_helper_instance is not None:
            with contextlib.suppress(Exception):
                _fake_helper_instance.stop()
        # 3. Clear ownership records for both display names used by the helper specs.
        _remove_server_record("acceptance-helper")
        _remove_server_record("acc-scenario")

    await asyncio.to_thread(_do_cleanup)
    return JSONResponse({"ok": True})


@_production_app.get("/_acceptance/turn-state/{session_id}")
async def _turn_state(session_id: int) -> JSONResponse:
    """Report the last user message's attempt state for deterministic test sequencing.

    After a transport-dropped send, the streaming generator is cancelled asynchronously;
    tests use this to wait until the original turn has reached a terminal state before
    resending, so the resend reconciles instead of hitting an ordinary busy 409.
    """
    import backend.core.sessions as _sessions
    import backend.core.tutor_attempts as _attempts
    from backend.storage.database import connect as _connect

    def _read() -> dict:
        conn = _connect()
        try:
            try:
                last_user = _sessions.last_user_message(conn, session_id)
            except Exception:
                return {"has_user": False}
            user_message_id = int(str(last_user["id"]))
            latest = _attempts.latest_attempt_for_message(conn, user_message_id)
            return {
                "has_user": True,
                "user_message_id": user_message_id,
                "state": str(latest["state"]) if latest else None,
            }
        finally:
            conn.close()

    return JSONResponse(await asyncio.to_thread(_read))


@_production_app.get("/_acceptance/writer-attempt-targets/{attempt_id}")
async def _get_writer_targets(attempt_id: int) -> JSONResponse:
    """Read durable targets for a writer attempt."""
    from backend.core.writer_attempts import targets_for_attempt
    from backend.storage.database import connect

    conn = connect()
    targets = targets_for_attempt(conn, attempt_id)
    return JSONResponse({"targets": targets})


@_production_app.get("/_acceptance/writer-latest-attempt/{session_id}")
async def _get_latest_writer_attempt(session_id: int) -> JSONResponse:
    """Return the latest writer attempt ID and state for a session."""
    from backend.storage.database import connect

    conn = connect()
    row = conn.execute(
        "select id, state from writer_turn_attempts where session_id = ? order by id desc limit 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return JSONResponse({"found": False})
    return JSONResponse({"found": True, "id": row["id"], "state": row["state"]})


@_production_app.post("/_acceptance/source-barrier/enable")
async def _enable_source_barrier() -> JSONResponse:
    """Enable the PLA-291 source-validation barrier."""
    global _source_barrier_event
    _source_barrier_arrived.clear()
    _source_barrier_event = threading.Event()
    return JSONResponse({"ok": True})


@_production_app.get("/_acceptance/source-barrier/arrived")
async def _source_barrier_arrived_check() -> JSONResponse:
    """Check whether a worker has arrived at the source-validation barrier."""
    return JSONResponse({"arrived": _source_barrier_arrived.is_set()})


@_production_app.post("/_acceptance/source-barrier/release")
async def _release_source_barrier() -> JSONResponse:
    """Release any worker held at the source-validation barrier."""
    global _source_barrier_event
    ev = _source_barrier_event
    if ev is not None:
        ev.set()
    _source_barrier_event = None
    _source_barrier_arrived.clear()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# Helper-supervision scenario endpoints (PLA-301 acceptance).
#
# These drive a FRESH production LlamaServer through `ensure_running()` so the
# adoption / stale-ownership / external-compatible / wrong-model DECISION is made by
# production code, not the harness. The fake helper may be spawned directly only to
# construct a deliberately FOREIGN process; every ownership/adoption outcome asserted
# in the spec comes from the production supervisor.
# --------------------------------------------------------------------------

from backend.core.errors import ConfigurationError  # noqa: E402

_scn_supervisor: "FakeHelperServer | None" = None


def _fresh_supervisor(display_name: str, model: str) -> FakeHelperServer:
    return FakeHelperServer(model_name=model, display_name=display_name)


def _reap_dropped_child(process: "subprocess.Popen[bytes]") -> None:
    """Fixture-only reaping for a crash-simulated supervisor's child.

    The crash simulation drops the supervisor OBJECT inside this still-running Python
    process, so the helper child keeps this process as its parent. After a genuinely
    dead parent, init would reap the child the moment it exits; without that, the child
    lingers as a ZOMBIE here -- its birth token still resolves, so production stop()
    truthfully reports 'survived termination' and preserves the ownership record even
    though the process is dead. A daemon thread blocking in wait() reproduces init's
    reaping without touching any production ownership/adoption decision.
    """
    with contextlib.suppress(Exception):
        process.wait()


def _orphan_dropped_supervisor(sup: "FakeHelperServer") -> None:
    """Make the crash simulation faithful before dropping a supervisor object."""
    process = sup._process
    if process is not None and process.poll() is None:
        threading.Thread(
            target=_reap_dropped_child,
            args=(process,),
            name="acceptance-dropped-child-reaper",
            daemon=True,
        ).start()


@_production_app.post("/_acceptance/scenario/ensure-running")
async def _scenario_ensure_running(request: Request) -> JSONResponse:
    """Run ensure_running() on a fresh production supervisor and report its decision.

    The previous scenario supervisor (if any) is DROPPED without being stopped unless
    `reset_previous` is true. Dropping it simulates a backend crash without graceful
    shutdown: the supervisor object is gone but its spawned helper -- an independent
    process group with a durable ownership record -- survives, which is exactly the state
    a restarted backend's fresh supervisor must adopt.
    """
    global _scn_supervisor
    body = await request.json()
    display_name = str(body.get("display_name", "acc-scenario"))
    model = str(body.get("model", "acceptance-test-model"))
    reset_previous = bool(body.get("reset_previous", False))
    if _scn_supervisor is not None:
        if reset_previous:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_scn_supervisor.stop)
        else:
            # Drop the reference without stopping -- its child survives (crash sim).
            # Hand the child to a fixture-only reaper first so it cannot linger as a
            # zombie of this process once a later production stop() kills it (a real
            # crashed parent's child would be reaped by init).
            _orphan_dropped_supervisor(_scn_supervisor)
        _scn_supervisor = None
    sup = _fresh_supervisor(display_name, model)
    _scn_supervisor = sup
    try:
        await asyncio.to_thread(sup.ensure_running)
    except ConfigurationError as exc:
        return JSONResponse({"ok": False, "error": str(exc.message)})
    return JSONResponse(
        {
            "ok": True,
            "spawned_pid": sup._process.pid if sup._process else None,
            "adopted_pid": sup._adopted_pid,
            "healthy": sup._healthy(),
        }
    )


@_production_app.post("/_acceptance/scenario/stop")
async def _scenario_stop() -> JSONResponse:
    """Stop the current scenario supervisor through production stop()."""
    global _scn_supervisor
    if _scn_supervisor is None:
        return JSONResponse({"ok": True, "was_running": False})
    await asyncio.to_thread(_scn_supervisor.stop)
    _scn_supervisor = None
    return JSONResponse({"ok": True, "was_running": True})


@_production_app.post("/_acceptance/scenario/write-stale-record")
async def _scenario_write_stale_record(request: Request) -> JSONResponse:
    """Write a synthetic ownership record (dead/reused PID or wrong token).

    Used to construct a deliberately STALE durable record; the production supervisor's
    reconciliation of it is what is under test.
    """
    body = await request.json()
    display_name = str(body.get("display_name", "acc-scenario"))
    data = _load_ownership()
    data[display_name] = {
        "pid": int(body["pid"]),
        "start_token": str(body.get("start_token", "proc:stale-bogus")),
        "pgid": None,
        "port": int(body.get("port", _ACCEPTANCE_HELPER_PORT)),
        "model": str(body.get("model", "acceptance-test-model")),
        "started_at": time.time(),
    }
    _save_ownership(data)
    return JSONResponse({"ok": True})


@_production_app.post("/_acceptance/scenario/kill-port")
async def _scenario_kill_port() -> JSONResponse:
    """Safety net: kill whatever is listening on the helper port (deterministic reset).

    Retries until the port is VERIFIABLY free. A `uv run python` wrapper spawns its real
    listener in a separate session (start_new_session=True), so we must kill the specific
    listening PID(s) -- and a single lsof/kill pass can race a still-dying socket, so we
    loop until no listener remains. This is the deterministic reset that keeps each helper
    test starting from a free port regardless of how a prior test spawned its fixture.
    """

    def _kill_until_free() -> list[int]:
        killed: list[int] = []
        deadline = time.monotonic() + 5.0
        while True:
            pids = _listener_pids_sync()
            if not pids:
                break
            for pid in pids:
                with contextlib.suppress(OSError, ValueError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        return killed

    killed = await asyncio.to_thread(_kill_until_free)
    return JSONResponse({"ok": True, "killed": killed})


@_production_app.get("/_acceptance/scenario/record/{display_name}")
async def _scenario_record(display_name: str) -> JSONResponse:
    """Read the EXACT durable ownership record for a scenario display name.

    The helper/status endpoint reports the record for "acceptance-helper" only; the
    PLA-301 scenario tests own records under "acc-scenario", so their final
    cleaned/reconciled assertions must read that exact record, not a neighbor's.
    """
    return JSONResponse({"ownership_record": _read_server_record(display_name)})


@_production_app.get("/_acceptance/scenario/pid-alive/{pid}")
async def _scenario_pid_alive(pid: int) -> JSONResponse:
    """True if the given PID is currently alive (signal 0)."""
    try:
        os.kill(pid, 0)
        return JSONResponse({"alive": True})
    except OSError:
        return JSONResponse({"alive": False})


@_production_app.get("/_acceptance/scenario/listener-pid")
async def _scenario_listener_pid() -> JSONResponse:
    """Return the PID actually LISTENING on the helper port (the real helper process)."""
    pids = await asyncio.to_thread(_listener_pids_sync)
    return JSONResponse({"pid": pids[0] if pids else None})


# A deliberately FOREIGN helper, spawned by the harness (not by the LlamaServer under
# test) so it is an external process the supervisor must reason about. Spawned as a plain
# `python fake-helper.py` (no uv wrapper) so the tracked PID IS the listener, and tracked
# here for deterministic cleanup.
_scn_foreign: "subprocess.Popen[bytes] | None" = None


@_production_app.post("/_acceptance/scenario/spawn-foreign")
async def _scenario_spawn_foreign(request: Request) -> JSONResponse:
    """Spawn a foreign fake-helper (external process) and wait for it to be healthy."""
    global _scn_foreign
    body = await request.json()
    model = str(body.get("model", "foreign-model"))
    # Ensure a clean port first.
    with contextlib.suppress(Exception):
        if _scn_foreign is not None:
            _stop_tree(_scn_foreign, kill=True)
            _scn_foreign = None
    await asyncio.to_thread(_scenario_kill_port_sync)
    proc = await asyncio.to_thread(_spawn_foreign_sync, model)
    _scn_foreign = proc
    healthy = await asyncio.to_thread(_wait_healthy_sync)
    if not healthy:
        with contextlib.suppress(Exception):
            _stop_tree(proc, kill=True)
        _scn_foreign = None
        return JSONResponse({"ok": False, "error": "foreign helper did not become healthy"})
    return JSONResponse({"ok": True, "pid": proc.pid})


@_production_app.post("/_acceptance/scenario/kill-foreign")
async def _scenario_kill_foreign() -> JSONResponse:
    """Kill the tracked foreign helper (deterministic cleanup)."""
    global _scn_foreign
    if _scn_foreign is None:
        return JSONResponse({"ok": True, "was_running": False})
    await asyncio.to_thread(_stop_tree, _scn_foreign, kill=True)
    _scn_foreign = None
    return JSONResponse({"ok": True, "was_running": True})


def _scenario_kill_port_sync() -> None:
    for pid in _listener_pids_sync():
        _kill_pgid_sync(pid)


def _wait_healthy_sync(timeout_seconds: float = 15.0) -> bool:
    import httpx as _httpx

    deadline = time.monotonic() + timeout_seconds
    with _httpx.Client(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if (
                    client.get(f"http://127.0.0.1:{_ACCEPTANCE_HELPER_PORT}/health").status_code
                    == 200
                ):
                    return True
            except _httpx.HTTPError:
                pass
            time.sleep(0.2)
    return False


def _listener_pids_sync() -> list[int]:
    """PIDs listening on the helper port (empty if none / lsof unavailable)."""
    try:
        out = (
            subprocess.run(  # noqa: S603
                [  # noqa: S607 - lsof is a trusted system binary resolved on PATH
                    "lsof",
                    f"-tiTCP:{_ACCEPTANCE_HELPER_PORT}",
                    "-sTCP:LISTEN",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            .stdout.strip()
            .split()
        )
    except Exception:  # noqa: BLE001 - lsof failure is non-fatal for the safety net
        return []
    pids = []
    for token in out:
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _kill_pgid_sync(pid: int) -> None:
    """Kill a PID's whole process group (falls back to the PID alone)."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _spawn_foreign_sync(model: str) -> "subprocess.Popen[bytes]":
    """Spawn a plain `python fake-helper.py` (no uv wrapper) so the PID IS the listener."""
    return subprocess.Popen(  # noqa: S603, S607 - sys.executable is an absolute path at runtime
        [
            sys.executable,
            str(_FAKE_HELPER_PATH),
            "--port",
            str(_ACCEPTANCE_HELPER_PORT),
            "--model",
            model,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _reclaim_foreign_helper_on_exit() -> None:
    """Kill the tracked foreign helper on any exit so it never orphans its port."""
    global _scn_foreign
    if _scn_foreign is not None:
        with contextlib.suppress(Exception):
            _stop_tree(_scn_foreign, kill=True)
        _scn_foreign = None


atexit.register(_reclaim_foreign_helper_on_exit)
