"""Liveness endpoint."""

from importlib.metadata import version
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health")
def health() -> Health:
    """Report that the server is up. Reads nothing from the vault or the database."""
    return Health(status="ok", version=version("vault"))
