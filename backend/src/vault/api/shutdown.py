"""Stopping the server cleanly: ``POST /api/shutdown`` (plan section 4, ``vault.ps1 stop``).

``vault.ps1 stop`` asks the server it started to shut down instead of ending the process, so that
the worker pool is stopped and running jobs get their bounded wait (``WorkerPool.stop``) before
the process exits. The request passes the same checks as every other change (a local ``Host``,
the matching ``Origin`` and the install's token, ``vault.core.security``), and in addition it
must come from a loopback address (``127.0.0.1``, or ``::1`` when the server listens there), so
nothing that reaches the port another way can stop the server. The launcher sends the token only
after ``/api/health`` has answered with the launch id it started this process with.

The answer is sent first and the shutdown begins after it (a background task), so the caller
always learns that the request was taken. Uvicorn then stops accepting connections, gives open
requests ``SHUTDOWN_GRACE_SECONDS`` (``vault.__main__``) and runs the lifespan's shutdown, which
stops the pool. A server that was not started by ``python -m vault`` (tests, the schema export)
has no way to stop itself and answers 503. Only a fixed event is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Final, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from vault.core.logging import log_event
from vault.core.security import LocalUser, current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["server"])

# The addresses a local caller has. The server only ever listens on loopback (config.SERVER_HOSTS),
# so this is a second guard, not the first: it holds even if a proxy or tunnel forwards the port.
LOOPBACK_CLIENTS: Final = frozenset({"127.0.0.1", "::1"})


class Stopping(BaseModel):
    """The request was taken; the server shuts down after this answer."""

    stopping: Literal[True]


@router.post("/shutdown", status_code=202)
def shutdown(
    request: Request,
    background: BackgroundTasks,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> Stopping:
    """Shut the server down cleanly: running jobs get their bounded wait, then the process exits."""
    client = request.client
    if client is None or client.host not in LOOPBACK_CLIENTS:
        raise HTTPException(status_code=403, detail="request refused")
    stop: Callable[[], None] | None = request.app.state.request_shutdown
    if stop is None:
        raise HTTPException(status_code=503, detail="this server cannot stop itself")
    log_event(logger, "server.shutdown_requested")
    background.add_task(stop)
    return Stopping(stopping=True)
