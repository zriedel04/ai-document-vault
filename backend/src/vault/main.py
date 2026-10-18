"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI

from vault.api import documents, health, imports, session
from vault.core.config import Settings, load_settings
from vault.core.headers import SecurityHeadersMiddleware
from vault.core.logging import configure_logging, log_event, metric
from vault.core.security import LocalSecurityMiddleware, load_or_create_token, new_token
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.ingest import chunk_stage
from vault.ingest.importer import Importer
from vault.ingest.orphans import sweep_orphans
from vault.jobs.queue import JobQueue
from vault.jobs.worker import WorkerPool
from vault.storage.blobs import BlobStore
from vault.storage.layout import ensure_vault
from vault.web import INDEX, find_file, install_frontend

logger = logging.getLogger(__name__)

# backend/src/vault/main.py -> the repository root, where frontend/dist is built.
# How long shutdown waits for running jobs; one still running is queued again at the next start.
WORKER_STOP_SECONDS = 10.0
DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(settings: Settings | None = None, *, serve_frontend: bool = True) -> FastAPI:
    """Build the application. ``settings`` defaults to :func:`load_settings`.

    With ``vault.path`` set, the vault folder is validated and created, the install's token is read
    (or created), the blob store recovers from a crash (leftover copies and locks are removed, then
    every blob is made read-only again), the database is migrated, blobs that no database row uses
    are moved to ``blobs/orphans/`` and jobs that a killed process left running are queued again,
    all before the first request. The worker pool (``vault.jobs.worker``) starts with the server
    and is stopped when it shuts down. A bad vault folder (inside a Git repository, say) or a
    damaged blob store (the temporary folder is a link) stops the start-up.
    Without a vault (demo mode, tests) there is no database and the token is random for this
    process only, so only the open endpoints work.

    The built frontend is served when ``dist/index.html`` exists (the ``frontend.dist`` setting, or
    ``frontend/dist`` by default). ``serve_frontend=False`` leaves it out, for the schema export.
    """
    settings = settings or load_settings()
    configure_logging(settings.logging.level)

    engine = None
    blobs = None
    importer = None
    workers = None
    if settings.vault.path is not None:
        paths = ensure_vault(settings.vault.path)
        token = load_or_create_token(paths.token_file)
        blobs = BlobStore(paths.blobs)
        removed = blobs.clean_temp()  # first: seal() does not count a blob a crash left linked
        sealed = blobs.seal()
        log_event(logger, "blobs.recovered", removed=metric(removed), sealed=metric(sealed))
        upgrade_to_latest(paths.db)
        engine = create_vault_engine(paths.db)
        # Before any worker runs: jobs a killed process left running are queued again.
        recovery = JobQueue(engine).recover()
        log_event(
            logger,
            "jobs.recovered",
            requeued=metric(recovery.requeued),
            failed=metric(recovery.failed),
        )
        # Before any import can run: blobs a crash left without a row are moved aside, not deleted.
        orphans = sweep_orphans(engine, blobs)
        log_event(
            logger,
            "blobs.orphans",
            moved=metric(orphans.moved),
            failed=metric(orphans.failed),
            strays=metric(orphans.strays),
        )
        importer = Importer(engine, blobs)
        # Only the chunk stage has a handler so far; jobs of the others wait in the queue.
        workers = WorkerPool(
            JobQueue(engine),
            handlers={chunk_stage.CHUNK_STAGE: chunk_stage.handler(engine)},
            limits=settings.workers.limits(),
        )
    else:
        token = new_token()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Recovery above ran before this: the pool must never start while jobs are left running.
        if workers is not None:
            workers.start()
        try:
            yield
        finally:
            if workers is not None:
                await anyio.to_thread.run_sync(workers.stop, WORKER_STOP_SECONDS)
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="AI Document Vault",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token = token
    app.state.engine = engine
    app.state.blobs = blobs
    app.state.importer = importer
    app.state.workers = workers
    app.state.upload_slots = anyio.Semaphore(imports.UPLOAD_SLOTS)
    app.add_middleware(LocalSecurityMiddleware, token=token)
    # Added last, so it is outermost and its headers are on the refusals above as well.
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health.router, prefix="/api")
    app.include_router(session.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    dist = settings.frontend.dist or DEFAULT_FRONTEND_DIST
    serving = serve_frontend and find_file(dist, INDEX) is not None
    if serving:
        install_frontend(app, dist)
    log_event(logger, "app.created", has_vault=engine is not None, frontend=serving)
    return app
