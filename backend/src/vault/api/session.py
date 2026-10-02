"""The session: ask who is signed in."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from vault.core.security import LocalUser, current_user

router = APIRouter(tags=["session"])


class Me(BaseModel):
    id: str
    name: str


@router.get("/session")
def read_session(user: Annotated[LocalUser, Depends(current_user)]) -> Me:
    """Who is making this request. Needs the token, so it also tells the app whether it has one."""
    return Me(id=user.id, name=user.name)
