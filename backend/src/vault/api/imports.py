"""Upload one file into the vault: ``POST /api/imports/upload``.

The body is the file's bytes, nothing else (``application/octet-stream``), so the server can
stream it to disk without a form parser and the browser sends one request per file. What the
importer needs besides the bytes comes in headers, never in the address, so a file name cannot end
up in a URL, a browser history or a log of addresses:

- ``X-Vault-Filename``: the file's name, percent-encoded UTF-8 (JavaScript's
  ``encodeURIComponent``). Required.
- ``X-Vault-Batch``: the import batch, a ULID. Optional; the response names the batch it used, so
  the page sends the first file without one and the rest with the batch it got back.
- ``X-Vault-Last-Modified``: the file's modification time in milliseconds since 1970 (a browser
  ``File.lastModified``). Optional.

The body is written to a staging file in the blob store's temporary folder, which the start-up
clean-up empties after a crash, and handed to the importer (``vault.ingest.importer``), which
validates it, stores it and records it. A file the validator refuses is a normal answer
(``outcome: refused`` with the reason); a malformed request is a 400 whose message is fixed text,
because every value in it is the owner's (a file name says what the document is). Nothing here
logs.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, BinaryIO, Final
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from vault.core.security import LocalUser, current_user
from vault.db import ids
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.ingest.validate import EXTENSIONS, Reason
from vault.storage.blobs import BlobStore

router = APIRouter(tags=["imports"])

MAX_NAME_CHARS: Final = 255  # what Windows and most file systems allow for one name
# 1970 to the end of year 9999: what a browser's lastModified can mean and a datetime can hold.
MAX_MTIME_MS: Final = 253_402_300_799_999
PERCENT_ENCODED: Final = re.compile(r"(?:[^%]|%[0-9A-Fa-f]{2})+")
WRITE_CHUNK: Final = 1024 * 1024  # the most of a body held in memory before it is written
# Uploads that may be staged and imported at once. The rest wait for a slot before their body is
# read (the client's connection holds it meanwhile), so neither their disk calls nor their imports
# take worker threads from the shared pool that also answers the page's synchronous routes
# (health, session), and no backlog of staged files builds up while imports run. Two, as the
# plan's extraction stage.
UPLOAD_SLOTS: Final = 2
EPOCH: Final = datetime(1970, 1, 1)  # naive UTC, as the database stores times

BODY_SCHEMA: Final = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


class UploadResult(BaseModel):
    outcome: Outcome
    batch_id: str
    document_id: str | None = None
    reason: Reason | None = None


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def parse_filename(header: str | None) -> str:
    """The decoded file name, or a 400. A name is one name: no folders, no control characters."""
    if header is None or not header:
        raise _bad_request("X-Vault-Filename is required")
    # unquote() leaves a broken escape (%ZZ, a lone %) as it is, and a header may carry Latin-1
    # bytes: only ASCII with complete escapes is what encodeURIComponent writes.
    if not header.isascii() or not PERCENT_ENCODED.fullmatch(header):
        raise _bad_request("X-Vault-Filename is not percent-encoded UTF-8")
    try:
        name = unquote(header, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad_request("X-Vault-Filename is not percent-encoded UTF-8") from None
    if (
        not name
        or len(name) > MAX_NAME_CHARS
        or name in {".", ".."}
        or any(mark in name for mark in "/\\")
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in name)
    ):
        raise _bad_request("X-Vault-Filename is not a file name")
    return name


def parse_batch(header: str | None) -> str:
    if header is None:
        return ids.new_ulid()
    if not ids.is_ulid(header):
        raise _bad_request("X-Vault-Batch is not a ULID")
    return header


def parse_mtime(header: str | None) -> datetime | None:
    """Milliseconds since 1970 as naive UTC, as the database stores times."""
    if header is None:
        return None
    # The length first: int() refuses a string of more than 4,300 digits with its own error.
    if (
        not header.isascii()
        or not header.isdigit()
        or len(header) > len(str(MAX_MTIME_MS))
        or int(header) > MAX_MTIME_MS
    ):
        raise _bad_request("X-Vault-Last-Modified is not a time in milliseconds")
    # Plain arithmetic from 1970: datetime.fromtimestamp goes through the C library, whose range
    # on Windows ends in the year 3000, so a time the check above accepts could fail there.
    return EPOCH + timedelta(milliseconds=int(header))


def parse_length(header: str | None, max_bytes: int) -> None:
    """Refuse a declared length over the limit before reading anything."""
    if header is None:
        return
    if not header.isascii() or not header.isdigit():
        raise _bad_request("Content-Length is not a number")
    if len(header) > len(str(max_bytes)) or int(header) > max_bytes:
        raise HTTPException(status_code=413, detail="the file is larger than the size limit")


def claimed_extension(name: str) -> str | None:
    """The extension the name claims, if the validator knows it at all."""
    extension = Path(name).suffix.removeprefix(".").lower()
    return extension if extension in EXTENSIONS else None


def _write(out: BinaryIO, data: bytearray) -> None:
    out.write(data)


def _finish(out: BinaryIO) -> None:
    out.flush()
    os.fsync(out.fileno())


async def _receive(request: Request, target: Path, max_bytes: int) -> bool:
    """Write the body to ``target``; False if it grew past ``max_bytes`` (nothing more is kept).

    Every disk call runs in a worker thread, so a large upload or a slow disk never holds up the
    event loop that answers the page's other requests. Besides the part the server handed over,
    at most ``WRITE_CHUNK`` bytes wait in memory, however large that part is: it is cut into
    slices that fill the buffer, the buffer is written as it is (no copy), and the next part of the
    body is read only after the last write finished.
    """
    size = 0
    pending = bytearray()
    out = await run_in_threadpool(target.open, "xb")
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                return False
            rest = memoryview(chunk)
            while rest:
                room = WRITE_CHUNK - len(pending)
                pending += rest[:room]
                rest = rest[room:]
                if len(pending) == WRITE_CHUNK:
                    await run_in_threadpool(_write, out, pending)
                    pending.clear()
        if pending:
            await run_in_threadpool(_write, out, pending)
        await run_in_threadpool(_finish, out)
    finally:
        await run_in_threadpool(out.close)
    return True


@router.post(
    "/imports/upload",
    openapi_extra=BODY_SCHEMA,
    responses={400: {"description": "A malformed request"}, 413: {"description": "Too large"}},
)
async def upload(
    request: Request,
    _user: Annotated[LocalUser, Depends(current_user)],
    x_vault_filename: Annotated[str | None, Header()] = None,
    x_vault_batch: Annotated[str | None, Header()] = None,
    x_vault_last_modified: Annotated[str | None, Header()] = None,
) -> UploadResult:
    """Import one file sent as the request body. The answer says whether it became a new
    document, was already in the vault (a new source for that document), or was refused."""
    importer: Importer | None = request.app.state.importer
    store: BlobStore | None = request.app.state.blobs
    if importer is None or store is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    name = parse_filename(x_vault_filename)
    batch_id = parse_batch(x_vault_batch)
    mtime = parse_mtime(x_vault_last_modified)
    max_bytes = importer.limits.max_bytes
    parse_length(request.headers.get("content-length"), max_bytes)

    extension = claimed_extension(name)
    if extension is None:
        return UploadResult(
            outcome=Outcome.REFUSED, batch_id=batch_id, reason=Reason.UNSUPPORTED_EXTENSION
        )
    async with request.app.state.upload_slots:
        staged = await run_in_threadpool(store.staging_path, extension)
        try:
            if not await _receive(request, staged, max_bytes):
                raise HTTPException(
                    status_code=413, detail="the file is larger than the size limit"
                )
            result = await run_in_threadpool(
                importer.import_file,
                ImportRequest(
                    path=staged,
                    original_filename=name,
                    method="upload",
                    batch_id=batch_id,
                    original_mtime=mtime,
                ),
            )
        finally:
            await run_in_threadpool(staged.unlink, missing_ok=True)
    return UploadResult(
        outcome=result.outcome,
        batch_id=batch_id,
        document_id=result.document_id,
        reason=result.reason,
    )
