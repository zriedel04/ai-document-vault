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
    """Add the catch-all route for the built frontend. Call it after the API routers."""

    @app.api_route("/{request_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend(request_path: str) -> FileResponse:
        if request_path == "api" or request_path.startswith("api/"):
            raise HTTPException(status_code=404)
        file = find_file(dist, request_path) if request_path else None
        if file is not None:
            return FileResponse(file)
        if PurePosixPath(request_path).suffix:  # a missing script or image is a real 404
            raise HTTPException(status_code=404)
        return FileResponse(dist / INDEX)
