"""FastAPI application factory, middleware, and lifespan.

Binds loopback only. There is no authentication, so the bind address is the security
boundary and must never become `0.0.0.0`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import (
    routes_chat,
    routes_classes,
    routes_documents,
    routes_profile,
    routes_settings,
    routes_solutions,
)
from backend.config import settings
from backend.core import sessions, solver
from backend.core.errors import LyraError
from backend.core.ingestion import reconcile_interrupted, start_worker
from backend.llm.embed_server import embedding_server
from backend.storage.database import connect, migrate

logger = logging.getLogger("lyra")

ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.ensure_directories()
    conn = connect()
    try:
        migrate(conn)
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
    try:
        yield
    finally:
        embedding_server.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Lyra", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal error"})

    app.include_router(routes_classes.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_profile.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_solutions.router)

    return app


app = create_app()
