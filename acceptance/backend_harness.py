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

import threading

import backend.core.ingestion as _ingest_mod
import backend.core.study as _study_mod
import backend.rag.embed as _embed_mod
from backend.llm.llama_server import LlamaServer
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

from backend.main import app  # noqa: E402, F401

# --------------------------------------------------------------------------
# Acceptance-only endpoints -- not part of the production API.
# --------------------------------------------------------------------------

from fastapi import Request
from fastapi.responses import JSONResponse


# --------------------------------------------------------------------------
# FakeHelperServer: LlamaServer subclass that spawns fake-helper.py
# through the production supervisor lifecycle (PLA-301 acceptance testing).
# --------------------------------------------------------------------------

from pathlib import Path

_FAKE_HELPER_PATH = Path(__file__).resolve().parent.parent / "frontend" / "e2e" / "acceptance" / "fake-helper.py"
_ACCEPTANCE_HELPER_PORT = 19_500
_fake_helper_instance: "FakeHelperServer | None" = None


class FakeHelperServer(LlamaServer):
    """Routes fake-helper.py through the production LlamaServer supervisor."""

    def __init__(
        self,
        model_name: str = "acceptance-test-model",
        fail_health: bool = False,
        slow_start: float = 0,
    ) -> None:
        super().__init__(
            display_name="acceptance-helper",
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
        args = [
            "uv", "run", "python", str(_FAKE_HELPER_PATH),
            "--port", str(self.port),
            "--model", self._fake_model_name,
        ]
        if self._fake_fail_health:
            args.append("--fail-health")
        if self._fake_slow_start > 0:
            args.extend(["--slow-start", str(self._fake_slow_start)])
        return args


from backend.llm.llama_server import (
    _process_start_token,
    _read_server_record,
    _remove_server_record,
)


@app.post("/_acceptance/helper/start")
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
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    _fake_helper_instance = helper
    pid = helper._process.pid if helper._process else None
    token = _process_start_token(pid) if pid else None
    return JSONResponse({
        "ok": True,
        "port": helper.port,
        "pid": pid,
        "birth_token": token,
    })


@app.post("/_acceptance/helper/stop")
async def _stop_helper() -> JSONResponse:
    """Stop the fake helper through the production LlamaServer supervisor."""
    global _fake_helper_instance
    if _fake_helper_instance is None:
        return JSONResponse({"ok": True, "was_running": False})
    _fake_helper_instance.stop()
    _fake_helper_instance = None
    return JSONResponse({"ok": True, "was_running": True})


@app.get("/_acceptance/helper/status")
async def _helper_status() -> JSONResponse:
    """Return the supervisor's view of the fake helper."""
    if _fake_helper_instance is None:
        return JSONResponse({"running": False})
    helper = _fake_helper_instance
    pid = helper._process.pid if helper._process else None
    token = _process_start_token(pid) if pid else None
    healthy = helper._healthy()
    record = _read_server_record("acceptance-helper")
    return JSONResponse({
        "running": pid is not None,
        "port": helper.port,
        "pid": pid,
        "birth_token": token,
        "healthy": healthy,
        "ownership_record": record,
    })


@app.post("/_acceptance/helper/cleanup-ownership")
async def _cleanup_helper_ownership() -> JSONResponse:
    """Remove the acceptance-helper ownership record (for test isolation)."""
    _remove_server_record("acceptance-helper")
    return JSONResponse({"ok": True})


@app.post("/_acceptance/writer-inject-effect")
async def _inject_writer_effect(request: Request) -> JSONResponse:
    """Link a fake durable target to a writer attempt for PLA-310 testing."""
    body = await request.json()
    attempt_id = body["attempt_id"]
    target_kind = body.get("target_kind", "brief")
    target_id = body.get("target_id", 99999)
    from backend.storage.database import connect
    from backend.core.writer_attempts import link_target

    conn = connect()
    link_target(conn, attempt_id, target_kind=target_kind, target_id=target_id)
    return JSONResponse({"ok": True, "attempt_id": attempt_id})


@app.get("/_acceptance/writer-attempt-targets/{attempt_id}")
async def _get_writer_targets(attempt_id: int) -> JSONResponse:
    """Read durable targets for a writer attempt."""
    from backend.storage.database import connect
    from backend.core.writer_attempts import targets_for_attempt

    conn = connect()
    targets = targets_for_attempt(conn, attempt_id)
    return JSONResponse({"targets": targets})


@app.get("/_acceptance/writer-latest-attempt/{session_id}")
async def _get_latest_writer_attempt(session_id: int) -> JSONResponse:
    """Return the latest writer attempt ID and state for a session."""
    from backend.storage.database import connect

    conn = connect()
    row = conn.execute(
        "select id, state from writer_turn_attempts "
        "where session_id = ? order by id desc limit 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return JSONResponse({"found": False})
    return JSONResponse({"found": True, "id": row["id"], "state": row["state"]})


@app.post("/_acceptance/source-barrier/enable")
async def _enable_source_barrier() -> JSONResponse:
    """Enable the PLA-291 source-validation barrier."""
    global _source_barrier_event
    _source_barrier_arrived.clear()
    _source_barrier_event = threading.Event()
    return JSONResponse({"ok": True})


@app.get("/_acceptance/source-barrier/arrived")
async def _source_barrier_arrived_check() -> JSONResponse:
    """Check whether a worker has arrived at the source-validation barrier."""
    return JSONResponse({"arrived": _source_barrier_arrived.is_set()})


@app.post("/_acceptance/source-barrier/release")
async def _release_source_barrier() -> JSONResponse:
    """Release any worker held at the source-validation barrier."""
    global _source_barrier_event
    ev = _source_barrier_event
    if ev is not None:
        ev.set()
    _source_barrier_event = None
    _source_barrier_arrived.clear()
    return JSONResponse({"ok": True})
