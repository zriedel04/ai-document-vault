"""The session: exchange the install's token for a cookie, and ask who is signed in."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from vault.core.security import (
    COOKIE_NAME,
    LocalUser,
    current_user,
    supplied_token,
    token_matches,
)

router = APIRouter(tags=["session"])


class Me(BaseModel):
    id: str
    name: str


@router.get("/session")
def read_session(user: Annotated[LocalUser, Depends(current_user)]) -> Me:
    """Who is making this request. Needs the token, so it also tells the app whether it has one."""
    return Me(id=user.id, name=user.name)


@router.post("/session", status_code=204)
def start_session(request: Request, response: Response) -> None:
    """Check the token (``Authorization: Bearer ...``) and set the session cookie.

    The middleware leaves this one path open because the caller does not have the cookie yet, so
    the check is made here.
    """
    supplied = supplied_token(request.headers)
    if supplied is None or not token_matches(request.app.state.token, supplied):
        raise HTTPException(status_code=401, detail="request refused")
    response.set_cookie(
        COOKIE_NAME, request.app.state.token, httponly=True, samesite="strict", path="/"
    )
