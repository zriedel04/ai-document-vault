"""Table definitions (SQLAlchemy Core). The migrations in ``migrations/versions`` create them.

The document tables follow ``docs/architecture.md`` section 6: a **document** is the thing the
owner organises, a **version** is one set of its bytes (a blob), and an **import source** records
every place and time those bytes were imported from, so importing the same file twice adds a
source, not a document. Where the tables differ from the plan's table, the reason is given at the
table.

Values the application writes are checked by the database too (``CheckConstraint``), so a bug or a
hand edit cannot store a state, an id or a hash of the wrong shape. Times are UTC, written by the
application. Ids are ULIDs (``vault.db.ids``).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)

from vault.db.ids import ulid_check

# Named constraints, so a later migration can drop one by name on SQLite.
NAMING = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

metadata = MetaData(naming_convention=NAMING)

# The lifecycle of a document while it is processed (ADR 0005). Being in the trash is not one of
# them: it is ``trashed_at``, so a document restored from the trash comes back as it was.
DOCUMENT_STATES: Final = ("processing", "searchable", "ready", "failed")
VERSION_ORIGINS: Final = ("import", "user", "ai_copy")
IMPORT_METHODS: Final = ("upload", "folder", "watch")
# The processing stages of section 7 (Tier B and C), each a job of its own (ADR 0005).
JOB_STAGES: Final = ("extract", "ocr", "metadata", "chunk", "embed", "enrich")
JOB_STATUSES: Final = ("queued", "running", "done", "failed")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


# Small facts about this vault (key and value, both text), such as when it was created.
app_meta = Table(
    "app_meta",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)

# One row per unique file content in the blob store (ADR 0006). The plan's ``stored_path`` is
# left out: the path follows from the hash and the extension (``BlobStore.path_for``), and a stored
# copy of it could only disagree with them.
blobs = Table(
    "blobs",
    metadata,
    Column("sha256", String, primary_key=True),
    Column("size", Integer, nullable=False),
    # The canonical extension the file is stored under and its media type, both from the
    # validator (vault.ingest.validate), which decides them from the bytes.
    Column("ext", String, nullable=False),
    Column("mime", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("verified_at", DateTime, nullable=True),
    CheckConstraint(
        "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'", name="sha256_is_a_hash"
    ),
    CheckConstraint("size >= 0", name="size_not_negative"),
    CheckConstraint(
        "length(ext) BETWEEN 1 AND 10 AND ext NOT GLOB '*[^a-z0-9]*'", name="ext_shape"
    ),
    CheckConstraint("length(mime) > 0 AND mime NOT GLOB '*[^a-z0-9./+-]*'", name="mime_shape"),
)

# The logical document. ``current_version_id`` is empty only while the document and its first
# version are being written, inside one transaction.
documents = Table(
    "documents",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "current_version_id",
        String,
        ForeignKey("document_versions.id", ondelete="RESTRICT", use_alter=True),
        nullable=True,
    ),
    Column("state", String, nullable=False),
    Column("local_only", Boolean, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("trashed_at", DateTime, nullable=True),
    CheckConstraint(ulid_check("id"), name="id_is_a_ulid"),
    CheckConstraint(_one_of("state", DOCUMENT_STATES), name="state_known"),
    CheckConstraint("local_only IN (0, 1)", name="local_only_flag"),
    Index("ix_documents_state", "state"),
    Index("ix_documents_trashed_at", "trashed_at"),
)

# One set of bytes of a document. The first release has one version per document; editing and
# version history arrive later without changing this table.
document_versions = Table(
    "document_versions",
    metadata,
    Column("id", String, primary_key=True),
    Column("document_id", String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    # A blob is deleted only once no version uses it.
    Column("blob_sha256", String, ForeignKey("blobs.sha256", ondelete="RESTRICT"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("origin", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    CheckConstraint(ulid_check("id"), name="id_is_a_ulid"),
    CheckConstraint("version_no >= 1", name="version_no_positive"),
    CheckConstraint(_one_of("origin", VERSION_ORIGINS), name="origin_known"),
    UniqueConstraint("document_id", "version_no", name="uq_document_versions_number"),
    Index("ix_document_versions_blob_sha256", "blob_sha256"),
)

# Every import of a document: where the file was, what it was called, its folders (kept as a list
# so the owner's hierarchy survives, section 5), when and how. These hold the owner's own folder
# and file names, which is why they live only in vault.db and are never logged.
import_sources = Table(
    "import_sources",
    metadata,
    Column("id", String, primary_key=True),
    Column("document_id", String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    # Empty for a browser upload, which tells the page the file's name but not where it was.
    Column("original_path", String, nullable=True),
    Column("original_filename", String, nullable=False),
    # A JSON array of the folder names above the file, outermost first.
    Column("folder_segments", String, nullable=False),
    Column("original_mtime", DateTime, nullable=True),
    Column("import_batch_id", String, nullable=False),
    Column("method", String, nullable=False),
    Column("imported_at", DateTime, nullable=False),
    CheckConstraint(ulid_check("id"), name="id_is_a_ulid"),
    CheckConstraint(ulid_check("import_batch_id"), name="batch_is_a_ulid"),
    CheckConstraint("length(original_filename) > 0", name="filename_not_empty"),
    CheckConstraint("original_path IS NULL OR length(original_path) > 0", name="path_not_empty"),
    CheckConstraint(
        "json_valid(folder_segments) AND json_type(folder_segments) = 'array'",
        name="folders_are_a_list",
    ),
    CheckConstraint(_one_of("method", IMPORT_METHODS), name="method_known"),
    Index("ix_import_sources_document_id", "document_id"),
    Index("ix_import_sources_import_batch_id", "import_batch_id"),
)

# The persistent job queue (ADR 0005, vault.jobs.queue). A job is one stage for one document.
# ``last_error`` is the name of the exception class that ended the last attempt, never its
# message: a parser's message can quote the content it failed on.
jobs = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("document_id", String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("stage", String, nullable=False),
    Column("status", String, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("last_error", String, nullable=True),
    Column("run_after", DateTime, nullable=False),
    # What the stage works from (a blob hash, plus tool versions later), so a changed input
    # queues the stage again while the same input does not.
    Column("input_hash", String, nullable=False),
    Column("locked_by", String, nullable=True),
    Column("locked_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    CheckConstraint(ulid_check("id"), name="id_is_a_ulid"),
    CheckConstraint(_one_of("stage", JOB_STAGES), name="stage_known"),
    CheckConstraint(_one_of("status", JOB_STATUSES), name="status_known"),
    CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_in_range"),
    CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    CheckConstraint(
        "length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'", name="input_is_a_hash"
    ),
    CheckConstraint(
        "last_error IS NULL OR (length(last_error) BETWEEN 1 AND 100"
        " AND last_error NOT GLOB '*[^A-Za-z0-9_]*')",
        name="error_is_a_class_name",
    ),
    CheckConstraint(f"locked_by IS NULL OR ({ulid_check('locked_by')})", name="worker_is_a_ulid"),
    # A running job has an owner; any other job has none.
    CheckConstraint(
        "(status = 'running') = (locked_by IS NOT NULL AND locked_at IS NOT NULL)",
        name="lock_matches_status",
    ),
    CheckConstraint(
        "(status IN ('done', 'failed')) = (finished_at IS NOT NULL)", name="finished_when_ended"
    ),
    Index("ix_jobs_claim", "status", "stage", "priority", "run_after"),
    Index("ix_jobs_document_id", "document_id"),
    # At most one unfinished job per document, stage and input: enqueueing the same work twice
    # (a second import of the same bytes, a retry pressed twice) does not run it twice.
    Index(
        "uq_jobs_open_work",
        "document_id",
        "stage",
        "input_hash",
        unique=True,
        sqlite_where=text("status IN ('queued', 'running')"),
    ),
)
