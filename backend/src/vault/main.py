"""Application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from vault.api import health
from vault.core.config import Settings, load_settings
from vault.core.logging import configure_logging, log_event

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. ``settings`` defaults to :func:`load_settings`."""
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
    log_event(logger, "app.created")
    return app
