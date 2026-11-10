"""Single-valued document metadata with its source (``document_fields``, plan section 6).

So far the only values stored are a file's own properties (``source = file_properties``), which
the metadata stage reads from the bytes of one version (``vault.ingest.metadata_stage``). They
belong to that version: ``file_properties_of`` returns only the values read from the version it is
given (the document's current one), and ``replace_file_properties`` replaces every value of the
source at once, so a document never shows one field from old bytes beside another from new ones.

The values are the file's own text and as private as the document: they go to the page that asks
for them and are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Final

from sqlalchemy import Connection, delete, insert, select

from vault.db.schema import MAX_FIELD_CHARS, document_fields

FILE_PROPERTIES: Final = "file_properties"


@dataclass(frozen=True)
class FileProperties:
    """The properties a file states about itself, cleaned (dates in the stage's W3CDTF form)."""

    title: str | None = None
    author: str | None = None
    created: str | None = None
    modified: str | None = None


# A test ties these to the schema's FIELD_NAMES, so neither can gain a field alone.
NAMES: Final = tuple(item.name for item in fields(FileProperties))


def replace_file_properties(
    connection: Connection, document_id: str, version_id: str, values: FileProperties
) -> int:
    """Make ``values`` the document's file properties, read from ``version_id``, in the caller's
    transaction: every earlier value of the source goes, and each field that is not None is
    stored. Returns how many were stored. A value that is not a string of 1 to
    ``MAX_FIELD_CHARS`` characters is a ``ValueError`` (the database refuses it too)."""
    rows = []
    for name in NAMES:
        value = getattr(values, name)
        if value is None:
            continue
        if type(value) is not str or not 1 <= len(value) <= MAX_FIELD_CHARS:
            msg = "a field's value is a string of 1 to 255 characters"
            raise ValueError(msg)
        rows.append(
            {
                "document_id": document_id,
                "field": name,
                "source": FILE_PROPERTIES,
                "version_id": version_id,
                "value": value,
            }
        )
    connection.execute(
        delete(document_fields).where(
            document_fields.c.document_id == document_id,
            document_fields.c.source == FILE_PROPERTIES,
        )
    )
    if rows:
        connection.execute(insert(document_fields), rows)
    return len(rows)


def file_properties_of(connection: Connection, document_id: str, version_id: str) -> FileProperties:
    """The document's file properties that were read from ``version_id``; none from any other."""
    found = connection.execute(
        select(document_fields.c.field, document_fields.c.value).where(
            document_fields.c.document_id == document_id,
            document_fields.c.source == FILE_PROPERTIES,
            document_fields.c.version_id == version_id,
        )
    ).all()
    values = {row.field: row.value for row in found if row.field in NAMES}
    return FileProperties(**values)
