"""Liveness endpoint."""

import os
import re
from importlib.metadata import version
from typing import Final, Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])

# The launcher (vault.ps1) starts the server with a fresh random value in this variable and opens
# the browser, with the vault's token, only once /api/health answers with the same value. Any
# program can answer on the port (another account's, or one that took it while the server was
# still starting), but none of them can read the environment of the process the launcher started,
# so the value proves which process answered. It is not a secret after that: one value per start.
LAUNCH_ENV_VAR: Final = "VAULT_LAUNCH_ID"
# The shape the launcher writes (and the token's); anything else is not echoed.
_LAUNCH_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{32,128}")


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    launch: str | None = None


def launch_id() -> str | None:
    """The value the launcher started this process with, or None if there is none."""
    value = os.environ.get(LAUNCH_ENV_VAR)
    if value is None or not _LAUNCH_SHAPE.fullmatch(value):
        return None
    return value


@router.get("/health")
def health() -> Health:
    """Report that the server is up. Reads nothing from the vault or the database."""
    return Health(status="ok", version=version("vault"), launch=launch_id())
