"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vault.api import health, session
from vault.core.config import Settings, load_settings
from vault.core.logging import configure_logging, log_event
from vault.core.security import LocalSecurityMiddleware, load_or_create_token, new_token
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.storage.layout import ensure_vault

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. ``settings`` defaults to :func:`load_settings`.

    With ``vault.path`` set, the vault folder is validated and created, the database is migrated
    and the install's token is read (or created) before the first request. A bad vault folder
    (inside a Git repository, say) stops the start-up. Without a vault (demo mode, tests) there is
    no database and the token is random for this process only, so only the open endpoints work.
    """
    settings = settings or load_settings()
    configure_logging(settings.logging.level)

    engine = None
    if settings.vault.path is not None:
        paths = ensure_vault(settings.vault.path)
        token = load_or_create_token(paths.token_file)
        upgrade_to_latest(paths.db)
        engine = create_vault_engine(paths.db)
    else:
        token = new_token()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
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
    app.add_middleware(LocalSecurityMiddleware, token=token)
    app.include_router(health.router, prefix="/api")
    app.include_router(session.router, prefix="/api")
    log_event(logger, "app.created", has_vault=engine is not None)
    return app
