"""Single-valued document metadata with its source; the first values are a DOCX's own properties.

Documents imported before this migration get their ``metadata`` job at the next start
(``vault.ingest.metadata_stage.queue_pending``), which finds them from the jobs table, so the
migration leaves no marker.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_fields",
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("version_id", sa.String(), nullable=True),
        sa.Column("value", sa.String(), nullable=False),
        sa.CheckConstraint(
            "field IN ('title', 'author', 'created', 'modified')",
            name=op.f("ck_document_fields_field_known"),
        ),
        sa.CheckConstraint(
            "source IN ('file_properties')", name=op.f("ck_document_fields_source_known")
        ),
        sa.CheckConstraint(
            "length(value) BETWEEN 1 AND 255", name=op.f("ck_document_fields_value_shape")
        ),
        sa.CheckConstraint(
            "source <> 'file_properties' OR version_id IS NOT NULL",
            name=op.f("ck_document_fields_file_properties_version"),
        ),
        sa.CheckConstraint(
            "field NOT IN ('created', 'modified') OR (length(value) IN (4, 7, 10, 20)"
            " AND value GLOB '[0-9][0-9][0-9][0-9]*' AND value NOT GLOB '*[^0-9TZ:-]*')",
            name=op.f("ck_document_fields_date_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_fields_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_fields_version_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "field", "source", name="pk_document_fields"),
    )
    op.create_index("ix_document_fields_version_id", "document_fields", ["version_id"])


def downgrade() -> None:
    op.drop_table("document_fields")
