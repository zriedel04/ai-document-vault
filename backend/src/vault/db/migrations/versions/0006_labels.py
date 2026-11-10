"""Labels and the documents that carry them, with where each assignment came from.

The import path's folders become ``folder`` labels when a file is imported. Sources recorded
before this migration have no labels yet, and the rule that turns folder names into labels is
the application's (``vault.ingest.path_metadata``), which a migration is frozen history and cannot
import. So the migration leaves a marker in ``app_meta`` (``PENDING_KEY``) and the application
labels every existing source once at its next start and removes the marker in the same
transaction.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# The same text as vault.ingest.path_metadata.PENDING_KEY; a test checks that they agree.
PENDING_KEY = "pending.folder_labels"


def upgrade() -> None:
    op.create_table(
        "labels",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_key", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 26 AND substr(id, 1, 1) BETWEEN '0' AND '7'"
            " AND id NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'",
            name=op.f("ck_labels_id_is_a_ulid"),
        ),
        sa.CheckConstraint(
            "kind IN ('collection', 'category', 'course', 'project', 'tag', 'topic', 'person',"
            " 'org', 'folder')",
            name=op.f("ck_labels_kind_known"),
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 255 AND length(name_key) = length(name)",
            name=op.f("ck_labels_name_shape"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_labels_not_its_own_parent")
        ),
        sa.CheckConstraint(
            "color IS NULL OR (length(color) = 7 AND color GLOB '#[0-9a-f][0-9a-f][0-9a-f]"
            "[0-9a-f][0-9a-f][0-9a-f]')",
            name=op.f("ck_labels_color_shape"),
        ),
        sa.CheckConstraint("local_only IN (0, 1)", name=op.f("ck_labels_local_only_flag")),
        sa.ForeignKeyConstraint(["parent_id"], ["labels.id"], name="fk_labels_parent_id_labels"),
        sa.PrimaryKeyConstraint("id", name="pk_labels"),
    )
    op.create_index(
        "uq_labels_top_name",
        "labels",
        ["kind", "name_key"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_labels_child_name",
        "labels",
        ["parent_id", "kind", "name_key"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NOT NULL"),
    )
    op.create_table(
        "document_labels",
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("label_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('user', 'ai', 'import_path', 'rule')",
            name=op.f("ck_document_labels_source_known"),
        ),
        sa.CheckConstraint(
            "status IN ('suggested', 'accepted', 'rejected')",
            name=op.f("ck_document_labels_status_known"),
        ),
        sa.CheckConstraint(
            "source = 'ai' OR (confidence IS NULL AND model IS NULL)",
            name=op.f("ck_document_labels_only_ai_names_a_model"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_document_labels_confidence_range"),
        ),
        sa.CheckConstraint(
            "model IS NULL OR length(model) BETWEEN 1 AND 100",
            name=op.f("ck_document_labels_model_shape"),
        ),
        sa.CheckConstraint(
            "source <> 'import_path' OR status = 'accepted'",
            name=op.f("ck_document_labels_import_path_accepted"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_labels_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["label_id"],
            ["labels.id"],
            name="fk_document_labels_label_id_labels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "label_id", "source", name="pk_document_labels"),
    )
    op.create_index("ix_document_labels_label_id", "document_labels", ["label_id"])
    op.execute(
        sa.text("INSERT INTO app_meta (key, value) VALUES (:key, '1')").bindparams(key=PENDING_KEY)
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM app_meta WHERE key = :key").bindparams(key=PENDING_KEY))
    op.drop_table("document_labels")
    op.drop_table("labels")
