"""FastAPI application factory, middleware, and lifespan.

Binds loopback only. There is no authentication, so the bind address is the security
boundary and must never become `0.0.0.0`.
"""

import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import (
    routes_agent,
    routes_agent_chat,
    routes_chat,
    routes_classes,
    routes_documents,
    routes_drafts,
    routes_health,
    routes_profile,
    routes_settings,
    routes_solutions,
    routes_study,
    routes_writer,
)
from backend.config import settings
from backend.core import (
    agent_attempts,
    agent_store,
    drafting,
    sessions,
    solver,
    storage_intents,
    study,
    tool_audit,
)
from backend.core.errors import LyraError
from backend.core.ingestion import reconcile_interrupted, start_worker
from backend.core.origins import (
    ALLOWED_BROWSER_ORIGINS,
    LOOPBACK_CLIENT_HEADER,
    host_is_allowed,
    mutation_origin_is_acceptable,
)
from backend.llm.embed_server import embedding_server
from backend.llm.ocr_server import ocr_server
from backend.llm.rerank_server import rerank_server
from backend.storage.database import connect, migrate

logger = logging.getLogger("lyra")

_INVALID_HOST = "Request rejected: the Host header is not a recognized Lyra loopback host."
_INVALID_ORIGIN = (
    "Request rejected: state-changing requests require a trusted browser origin"
    " or a non-browser client header."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.ensure_directories()
    conn = connect()
    try:
        migrate(conn)
        # Before the ingestion queue is rebuilt, so a document whose interrupted move is
        # rolled forward here is re-indexed from the path recovery settled on.
        settled_intents, swept_orphans = storage_intents.reconcile_storage(conn)
        if settled_intents:
            logger.warning("Settled %d interrupted storage operation(s)", settled_intents)
        if swept_orphans:
            logger.info("Removed %d orphaned storage file(s) left by a crash", swept_orphans)
        requeued, interrupted = reconcile_interrupted(conn)
        if requeued:
            logger.info("Requeued %d ingestion job(s) that had not started", requeued)
        if interrupted:
            logger.warning("Marked %d interrupted ingestion job(s) as failed", interrupted)
        # An artifact left `awaiting_review` is deliberately untouched by this: it was not
        # working when the process stopped, it was waiting, and it still is.
        stalled = solver.reconcile_interrupted(conn)
        if stalled:
            logger.warning("Marked %d interrupted solve job(s) as failed", stalled)
        # Study jobs persist their intent (migration 035), so queued work requeues and
        # interrupted work restarts cleanly; only work whose intent cannot be reconstructed
        # is failed.
        requeued_study, interrupted_study = study.reconcile_interrupted(conn)
        if requeued_study:
            logger.info("Requeued %d study generation(s) after restart", requeued_study)
        if interrupted_study:
            logger.warning("Marked %d interrupted study generation(s) as failed", interrupted_study)
        # Drafts caught mid-suggestion go back to ready: the draft was never touched.
        requeued_drafts, resumed_drafts = drafting.reconcile_interrupted(conn)
        if requeued_drafts:
            logger.info("Requeued %d durable draft run(s) after restart", requeued_drafts)
        if resumed_drafts:
            logger.info("Returned %d interrupted legacy suggestion run(s) to ready", resumed_drafts)
        abandoned_tools = tool_audit.reconcile_inflight(conn)
        if abandoned_tools:
            logger.warning("Marked %d interrupted agent tool call(s) as abandoned", abandoned_tools)
        # An agent-turn attempt cannot outlive the process that was running it, so one still
        # `running` at startup is one whose process died mid-turn. Settle it as stopped -
        # a truthful, retryable terminal state - so it never reads forever as in flight.
        abandoned_attempts = agent_attempts.reconcile_running(conn)
        if abandoned_attempts:
            logger.warning("Marked %d interrupted agent turn(s) as stopped", abandoned_attempts)
        abandoned_commands = agent_store.reconcile_running_commands(conn)
        if abandoned_commands:
            logger.warning(
                "Marked %d interrupted verification command(s) as abandoned",
                abandoned_commands,
            )
        discarded = sessions.discard_empty_sessions(conn)
        if discarded:
            logger.info("Discarded %d conversation(s) that were never used", discarded)
        # Solution sets written before positions were recorded keep every correction the
        # student made at the review gate, so they are backfilled rather than re-segmented.
        located = solver.backfill_problem_locations(conn)
        if located:
            logger.info("Located %d problem(s) on their source page", located)
    finally:
        conn.close()
    start_worker()
    solver.start_worker()
    study.start_worker()
    drafting.start_worker()
    _warm_start_reranker()
    try:
        yield
    finally:
        # All three, not just the embedder. The servers are spawned in their own
        # sessions so a backend restart can adopt them, which also means nothing but
        # this line ever reclaims them: a forgotten one outlives the app indefinitely,
        # holding hundreds of megabytes of weights. Each stop is a no-op when that
        # server never ran.
        embedding_server.stop()
        ocr_server.stop()
        rerank_server.stop()


def _warm_start_reranker() -> None:
    """Load the reranking model in the background, if it is installed.

    `rag/rerank.py` starts it lazily, but lazily means the first question of the day
    pays the whole model-load stall inside its own retrieval. A daemon thread pays it
    here instead, off the event loop. Best-effort by design: reranking is optional, so
    a failure is logged and the first search falls back to the embedding order exactly
    as it would have anyway.
    """
    if not settings.rerank_installed:
        return

    def warm() -> None:
        try:
            rerank_server.ensure_running()
        except Exception:
            logger.warning(
                "The reranking server did not warm-start; search will retry lazily",
                exc_info=True,
            )

    threading.Thread(target=warm, name="rerank-warm-start", daemon=True).start()


def create_app() -> FastAPI:
    app = FastAPI(title="Lyra", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_BROWSER_ORIGINS),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CORS withholds *response-read* permission from a hostile page, but does not prevent
    # a simple cross-origin request from being *dispatched*. A malicious page can therefore
    # submit form/no-CORS POSTs to loopback Lyra with a trusted Host and an untrusted
    # Origin. This middleware closes that gap by requiring every state-changing request to
    # carry either a trusted browser Origin or a non-browser client header before the body
    # is parsed or any handler runs. Safe methods are exempt.
    #
    # Registered before the Host guard so the Host guard wraps it: Host is checked first
    # (LIFO), then Origin. A rebinding attack is refused on its Host before Origin is even
    # evaluated, and a legitimate-Host + hostile-Origin CSRF is caught here.
    @app.middleware("http")
    async def enforce_mutation_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        verdict = mutation_origin_is_acceptable(
            request.method,
            request.headers.get("origin"),
            LOOPBACK_CLIENT_HEADER in request.headers,
        )
        if verdict is False:
            return JSONResponse(status_code=403, content={"detail": _INVALID_ORIGIN})
        return await call_next(request)

    # Registered after the Origin guard so it wraps it (LIFO): a request whose `Host` is
    # not a Lyra loopback host is refused here before Origin, CORS, routing, or any route
    # body runs. CORS alone does not close DNS rebinding - a page can stay same-origin to
    # a name it controls while that name is rebound to 127.0.0.1 - and this API is
    # unauthenticated with state-changing and, when granted, workspace/command routes, so
    # the Host check is part of the loopback-only boundary rather than an optimization.
    # The refusal does not depend on `Origin`: a missing or acceptable-looking `Origin`
    # cannot rescue a bad Host.
    @app.middleware("http")
    async def enforce_trusted_host(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not host_is_allowed(request.headers.get("host")):
            return JSONResponse(status_code=400, content={"detail": _INVALID_HOST})
        return await call_next(request)

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        content: dict[str, object] = {"detail": exc.message}
        if exc.extra:
            content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal error"})

    app.include_router(routes_classes.router)
    app.include_router(routes_health.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_profile.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_solutions.router)
    app.include_router(routes_study.router)
    app.include_router(routes_drafts.router)
    app.include_router(routes_writer.router)
    app.include_router(routes_agent.router)
    app.include_router(routes_agent_chat.router)

    return app


app = create_app()
