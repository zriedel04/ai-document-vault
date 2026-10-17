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
    ForeignKeyConstraint,
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

# The lifecycle state of a document (ADR 0005). The first four are the states of a document in
# use; ``trashed`` replaces them while it is in the trash, and the one it had is kept in
# ``state_before_trash`` so that a restore brings it back as it was.
LIVE_STATES: Final = ("processing", "searchable", "ready", "failed")
TRASHED: Final = "trashed"
DOCUMENT_STATES: Final = (*LIVE_STATES, TRASHED)
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
# version are being written, inside one transaction. It must name a version of this document: the
# foreign key covers the pair (version, document), so another document's version is refused.
documents = Table(
    "documents",
    metadata,
    Column("id", String, primary_key=True),
    Column("current_version_id", String, nullable=True),
    Column("state", String, nullable=False),
    Column("state_before_trash", String, nullable=True),
    Column("local_only", Boolean, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("trashed_at", DateTime, nullable=True),
    CheckConstraint(ulid_check("id"), name="id_is_a_ulid"),
    CheckConstraint(_one_of("state", DOCUMENT_STATES), name="state_known"),
    # In the trash exactly when the state says so, with the time it went there and the state to
    # restore.
    CheckConstraint(
        f"(state = '{TRASHED}') = (trashed_at IS NOT NULL)"
        f" AND (state = '{TRASHED}') = (state_before_trash IS NOT NULL)"
        f" AND (state_before_trash IS NULL OR {_one_of('state_before_trash', LIVE_STATES)})",
        name="trash_consistent",
    ),
    CheckConstraint("local_only IN (0, 1)", name="local_only_flag"),
    ForeignKeyConstraint(
        ["current_version_id", "id"],
        ["document_versions.id", "document_versions.document_id"],
        name="fk_documents_current_version_document_versions",
        ondelete="RESTRICT",
        use_alter=True,
    ),
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
    # The target of the documents' foreign key to their current version.
    UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
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

# The text of each page of a document version, as an extractor read it (section 6, step 7 of
# section 7). Derived data: rebuilt from the blob whenever the extractor changes, and private like
# the document itself, so it is never logged. ``extractor`` names the extractor and its version
# (``text-1``) and ``encoding`` the character encoding the bytes were decoded with, when the format
# has one. ``headings`` is a JSON array of ``[level, title, start, end]``, the offsets being
# positions in ``text``; they are read with the page and never queried on their own, so they live
# in the row. ``char_count`` is the plan's column, kept equal to the text's length by the database.
# The plan's OCR columns (``ocr_used``, ``ocr_confidence``) and page size (``width``, ``height``)
# arrive with the OCR and PDF extractors that fill them.
pages = Table(
    "pages",
    metadata,
    Column(
        "version_id",
        String,
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("page_no", Integer, primary_key=True),
    Column("text", String, nullable=False),
    Column("char_count", Integer, nullable=False),
    Column("headings", String, nullable=False),
    Column("extractor", String, nullable=False),
    Column("encoding", String, nullable=True),
    CheckConstraint("page_no >= 1", name="page_no_positive"),
    CheckConstraint("char_count = length(text)", name="char_count_is_the_length"),
    CheckConstraint(
        "json_valid(headings) AND json_type(headings) = 'array'", name="headings_are_a_list"
    ),
    # A looser net than vault.db.pages.EXTRACTOR, which is the rule the application applies.
    CheckConstraint(
        "length(extractor) BETWEEN 3 AND 40 AND extractor NOT GLOB '*[^a-z0-9-]*'"
        " AND extractor GLOB '[a-z]*-[1-9]*' AND extractor GLOB '*[0-9]'"
        " AND extractor NOT GLOB '*--*'",
        name="extractor_is_a_name_and_version",
    ),
    CheckConstraint(
        "encoding IS NULL OR (length(encoding) BETWEEN 1 AND 40"
        " AND encoding GLOB '[a-z]*' AND encoding NOT GLOB '*[^a-z0-9_-]*')",
        name="encoding_is_a_codec_name",
    ),
)

# The retrieval chunks of each document version (section 6, step 10 of section 7), written by a
# chunker from the version's pages (vault.ingest.chunking). Derived and private like the pages.
# ``id`` is an integer that is never reused (AUTOINCREMENT): it is the full-text index's rowid,
# the key of the vectors to come, and what a stored citation names, so a deleted chunk's id must
# never come back as another chunk. ``char_start`` is an offset into the text of ``page_start`` and
# ``char_end`` into the text of ``page_end``; ``text`` is that span of the page. ``section_path`` is
# a JSON array of heading titles, outermost first. The pair (version, document) must be a real
# version of that document, and each page must exist, so re-extracting a version's pages deletes
# its chunks (they would point at text that is gone). The keyword index ``chunks_fts`` is an FTS5
# table that the migration creates with triggers that keep it in step with this table (see
# ``FTS_TABLES``); SQLAlchemy does not describe virtual tables or triggers, so it is not here.
chunks = Table(
    "chunks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version_id", String, nullable=False),
    Column("document_id", String, nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("page_start", Integer, nullable=False),
    Column("page_end", Integer, nullable=False),
    Column("section_path", String, nullable=False),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("text", String, nullable=False),
    Column("token_count", Integer, nullable=False),
    Column("chunker_version", String, nullable=False),
    CheckConstraint("chunk_index >= 0", name="chunk_index_not_negative"),
    CheckConstraint("page_start >= 1 AND page_end >= page_start", name="pages_in_order"),
    # Within one page the span is not empty and the text is exactly as long as the span.
    CheckConstraint(
        "char_start >= 0 AND char_end >= 0 AND (page_end > page_start"
        " OR (char_end > char_start AND length(text) = char_end - char_start))",
        name="span_matches_the_text",
    ),
    CheckConstraint("length(text) > 0", name="text_not_empty"),
    CheckConstraint("token_count >= 1", name="token_count_positive"),
    CheckConstraint(
        "json_valid(section_path) AND json_type(section_path) = 'array'",
        name="section_path_is_a_list",
    ),
    # A looser net than the application's rule (vault.db.chunks), as for the pages' extractor.
    CheckConstraint(
        "length(chunker_version) BETWEEN 3 AND 40"
        " AND chunker_version NOT GLOB '*[^a-z0-9-]*'"
        " AND chunker_version GLOB '[a-z]*-[1-9]*' AND chunker_version GLOB '*[0-9]'"
        " AND chunker_version NOT GLOB '*--*'",
        name="chunker_version_is_a_name_and_version",
    ),
    ForeignKeyConstraint(
        ["version_id", "document_id"],
        ["document_versions.id", "document_versions.document_id"],
        name="fk_chunks_version_document_versions",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["version_id", "page_start"],
        ["pages.version_id", "pages.page_no"],
        name="fk_chunks_page_start_pages",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["version_id", "page_end"],
        ["pages.version_id", "pages.page_no"],
        name="fk_chunks_page_end_pages",
        ondelete="CASCADE",
    ),
    UniqueConstraint("version_id", "chunk_index", name="uq_chunks_version_index"),
    Index("ix_chunks_document_id", "document_id"),
    sqlite_autoincrement=True,
)

# The FTS5 table over chunks and the shadow tables FTS5 keeps for it. The migration ``0005`` creates
# them; the migration comparison (``include_name``) leaves exactly these names out, because
# SQLAlchemy cannot describe a virtual table, and a test checks that the database holds exactly
# these and that the triggers keep them in step.
FTS_TABLES: Final = frozenset(
    {
        "chunks_fts",
        *(f"chunks_fts_{part}" for part in ("config", "content", "data", "docsize", "idx")),
    }
)


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Alembic's filter for comparing the database with ``metadata``: everything but FTS5."""
    return not (type_ == "table" and name in FTS_TABLES)
