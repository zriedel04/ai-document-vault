"""The pages of each document version: extracted text, its headings and how it was read.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pages",
        sa.Column("version_id", sa.String(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("headings", sa.String(), nullable=False),
        sa.Column("extractor", sa.String(), nullable=False),
        sa.Column("encoding", sa.String(), nullable=True),
        sa.CheckConstraint("page_no >= 1", name=op.f("ck_pages_page_no_positive")),
        sa.CheckConstraint(
            "char_count = length(text)", name=op.f("ck_pages_char_count_is_the_length")
        ),
        sa.CheckConstraint(
            "json_valid(headings) AND json_type(headings) = 'array'",
            name=op.f("ck_pages_headings_are_a_list"),
        ),
        sa.CheckConstraint(
            "length(extractor) BETWEEN 3 AND 40 AND extractor NOT GLOB '*[^a-z0-9-]*'"
            " AND extractor GLOB '[a-z]*-[1-9]*' AND extractor GLOB '*[0-9]'"
            " AND extractor NOT GLOB '*--*'",
            name=op.f("ck_pages_extractor_is_a_name_and_version"),
        ),
        sa.CheckConstraint(
            "encoding IS NULL OR (length(encoding) BETWEEN 1 AND 40"
            " AND encoding GLOB '[a-z]*' AND encoding NOT GLOB '*[^a-z0-9_-]*')",
            name=op.f("ck_pages_encoding_is_a_codec_name"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name="fk_pages_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id", "page_no", name="pk_pages"),
    )


def downgrade() -> None:
    op.drop_table("pages")
