"""Serve the built frontend from the same process and origin as the API.

In normal use there is one port: ``/api/...`` is the API and every other GET is the React app.
Real files under the frontend's ``dist`` folder (scripts, styles) are served as they are, and any
other path without a file extension returns ``index.html`` so client-side routes such as
``/documents`` survive a page reload. In development Vite serves the frontend and proxies ``/api``
to this server instead, so this module is not involved.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.routing import Match, Route
from starlette.types import Receive, Scope, Send

INDEX = "index.html"


def find_file(dist: Path, request_path: str) -> Path | None:
    """The file under ``dist`` that ``request_path`` names, or None.

    The resolved path must stay inside ``dist``, which rejects ``..``, absolute paths, drive
    letters and symlinks that point outside the folder.
    """
    root = dist.resolve()
    try:
        candidate = (root / request_path).resolve()
    except (OSError, ValueError):  # for example an embedded NUL character
        return None
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


def install_frontend(app: FastAPI, dist: Path) -> None:
    """Add the routes for the built frontend. Call it after the API routers."""
    # The methods a real route may offer, probed to build the ``Allow`` header of a 405.
    api_methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    class UnknownApi:
        """An API path no route answers is a 404 for every method, never the frontend.

        If a real route matches the path but not the method, the answer is the usual 405 with
        its ``Allow`` header instead.
        """

        # A class with ``__call__`` is mounted as a plain ASGI app, which Starlette routes for
        # every HTTP method. A function endpoint would be limited to GET, and a fixed method list
        # would leave TRACE, CONNECT and custom verbs to Starlette's own 405.
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            # Ask each real route which methods it would answer for this path. Probing with a
            # method works for every kind of route (the included routers have no ``methods``).
            allowed: set[str] = set()
            for route in app.router.routes:
                if getattr(route, "endpoint", None) in (unknown_api, frontend):
                    continue  # the catch-alls are not "real" routes
                for method in api_methods:
                    match, _ = route.matches({**scope, "method": method})
                    if match == Match.FULL:
                        allowed.add(method)
            if allowed:
                raise HTTPException(status_code=405, headers={"Allow": ", ".join(sorted(allowed))})
            raise HTTPException(status_code=404)

    unknown_api = UnknownApi()
    app.router.routes.append(Route("/api", unknown_api, include_in_schema=False))
    app.router.routes.append(Route("/api/{api_path:path}", unknown_api, include_in_schema=False))

    @app.api_route("/{request_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend(request_path: str) -> FileResponse:
        file = find_file(dist, request_path) if request_path else None
        if file is not None:
            return FileResponse(file)
        if PurePosixPath(request_path).suffix:  # a missing script or image is a real 404
            raise HTTPException(status_code=404)
        # The index is checked like any other file, so an index.html that is a symlink to a file
        # outside dist is never served, even if it changed after start-up.
        index = find_file(dist, INDEX)
        if index is None:
            raise HTTPException(status_code=404)
        return FileResponse(index)
