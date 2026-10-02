"""Application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from vault.api import health
from vault.core.config import Settings, load_settings
from vault.core.logging import configure_logging, log_event
from vault.web import INDEX, find_file, install_frontend

logger = logging.getLogger(__name__)

# backend/src/vault/main.py -> the repository root, where frontend/dist is built.
DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(settings: Settings | None = None, *, serve_frontend: bool = True) -> FastAPI:
    """Build the application. ``settings`` defaults to :func:`load_settings`.

    The built frontend is served when ``dist/index.html`` exists (the ``frontend.dist`` setting, or
    ``frontend/dist`` by default). ``serve_frontend=False`` leaves it out, for the schema export.
    """
    settings = settings or load_settings()
    configure_logging(settings.logging.level)
    app = FastAPI(
        title="AI Document Vault",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    dist = settings.frontend.dist or DEFAULT_FRONTEND_DIST
    serving = serve_frontend and find_file(dist, INDEX) is not None
    if serving:
        install_frontend(app, dist)
    log_event(logger, "app.created", frontend=serving)
    return app
