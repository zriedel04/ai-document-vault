"""Documents, their versions, their import sources and the blobs they point at.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Written out rather than imported from vault.db.schema: a migration must keep creating what it
# created when it was written, whatever the schema module says later.
ULID_CHECK = (
    "length({c}) = 26 AND substr({c}, 1, 1) BETWEEN '0' AND '7'"
    " AND {c} NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'"
)


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("ext", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_blobs_sha256_is_a_hash"),
        ),
        sa.CheckConstraint("size >= 0", name=op.f("ck_blobs_size_not_negative")),
        sa.CheckConstraint(
            "length(ext) BETWEEN 1 AND 10 AND ext NOT GLOB '*[^a-z0-9]*'",
            name=op.f("ck_blobs_ext_shape"),
        ),
        sa.CheckConstraint(
            "length(mime) > 0 AND mime NOT GLOB '*[^a-z0-9./+-]*'", name=op.f("ck_blobs_mime_shape")
        ),
        sa.PrimaryKeyConstraint("sha256", name="pk_blobs"),
    )
    # documents and document_versions point at each other. SQLite checks a foreign key when a row
    # changes, not when the table is created, so both are declared in place.
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("current_version_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("state_before_trash", sa.String(), nullable=True),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("trashed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ULID_CHECK.format(c="id"), name=op.f("ck_documents_id_is_a_ulid")),
        sa.CheckConstraint(
            "state IN ('processing', 'searchable', 'ready', 'failed', 'trashed')",
            name=op.f("ck_documents_state_known"),
        ),
        sa.CheckConstraint(
            "(state = 'trashed') = (trashed_at IS NOT NULL)"
            " AND (state = 'trashed') = (state_before_trash IS NOT NULL)"
            " AND (state_before_trash IS NULL"
            " OR state_before_trash IN ('processing', 'searchable', 'ready', 'failed'))",
            name=op.f("ck_documents_trash_consistent"),
        ),
        sa.CheckConstraint("local_only IN (0, 1)", name=op.f("ck_documents_local_only_flag")),
        # The pair, so the current version must be one of this document's versions.
        sa.ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_documents_current_version_document_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
    )
    op.create_index("ix_documents_state", "documents", ["state"])
    op.create_index("ix_documents_trashed_at", "documents", ["trashed_at"])
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("blob_sha256", sa.String(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            ULID_CHECK.format(c="id"), name=op.f("ck_document_versions_id_is_a_ulid")
        ),
        sa.CheckConstraint(
            "version_no >= 1", name=op.f("ck_document_versions_version_no_positive")
        ),
        sa.CheckConstraint(
            "origin IN ('import', 'user', 'ai_copy')",
            name=op.f("ck_document_versions_origin_known"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_versions_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["blob_sha256"],
            ["blobs.sha256"],
            name="fk_document_versions_blob_sha256_blobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_number"),
        sa.UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
    )
    op.create_index("ix_document_versions_blob_sha256", "document_versions", ["blob_sha256"])
    op.create_table(
        "import_sources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("original_path", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("folder_segments", sa.String(), nullable=False),
        sa.Column("original_mtime", sa.DateTime(), nullable=True),
        sa.Column("import_batch_id", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(ULID_CHECK.format(c="id"), name=op.f("ck_import_sources_id_is_a_ulid")),
        sa.CheckConstraint(
            ULID_CHECK.format(c="import_batch_id"), name=op.f("ck_import_sources_batch_is_a_ulid")
        ),
        sa.CheckConstraint(
            "length(original_filename) > 0", name=op.f("ck_import_sources_filename_not_empty")
        ),
        sa.CheckConstraint(
            "original_path IS NULL OR length(original_path) > 0",
            name=op.f("ck_import_sources_path_not_empty"),
        ),
        sa.CheckConstraint(
            "json_valid(folder_segments) AND json_type(folder_segments) = 'array'",
            name=op.f("ck_import_sources_folders_are_a_list"),
        ),
        sa.CheckConstraint(
            "method IN ('upload', 'folder', 'watch')", name=op.f("ck_import_sources_method_known")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_import_sources_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_sources"),
    )
    op.create_index("ix_import_sources_document_id", "import_sources", ["document_id"])
    op.create_index("ix_import_sources_import_batch_id", "import_sources", ["import_batch_id"])


def downgrade() -> None:
    # A document points at its current version, so that link goes first: dropping a table deletes
    # its rows, and the versions could not be deleted while a document still points at one.
    op.execute("UPDATE documents SET current_version_id = NULL")
    op.drop_table("import_sources")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("blobs")
