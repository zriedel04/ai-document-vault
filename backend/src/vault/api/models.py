"""The base of every JSON request body the API reads.

pydantic ignores a key a model does not declare, so a misspelt filter (``"lable"`` for
``"label"``) was dropped and the request ran without it: a 200 for a search the owner did not
ask for. A body model derives from ``RequestBody`` instead of ``BaseModel``, which forbids
unknown keys; the request is then refused with the fixed-text 422 of ``vault.api.errors``, which
says only ``body`` and never the key or its value. A test walks every route's body parameters
and fails on one that is not a ``RequestBody`` or that allows extra keys again.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RequestBody(BaseModel):
    """A JSON request body: every key must be one the model declares."""

    model_config = ConfigDict(extra="forbid")
