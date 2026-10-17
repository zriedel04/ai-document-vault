"""The chunks of each document version, and the FTS5 keyword index over them.

``chunks_fts`` is an FTS5 table with its own copy of each chunk's text, the document's title and
the section path, tokenized by ``porter unicode61`` (plan section 4): words are split by Unicode
rules, case and diacritics are folded (``cafe`` finds ``café``), and English words are stemmed.
Its rowid is the chunk's id. Triggers keep it in step with ``chunks``: every insert adds the row,
every delete removes it (deletes by a foreign-key cascade too), and an update replaces it. The
title is the document's name as the library shows it, the file name of its first import (earliest,
ties by id), read when the chunk is written.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# The index row of a new chunk, as the insert and update triggers write it. Fixed text only.
_INDEX_NEW_ROW = (
    "INSERT INTO chunks_fts (rowid, text, title, section_path) VALUES ("
    " new.id, new.text,"
    " coalesce((SELECT original_filename FROM import_sources"
    " WHERE document_id = new.document_id ORDER BY imported_at, id LIMIT 1), ''),"
    " new.section_path);"
)


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.String(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.String(), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name=op.f("ck_chunks_chunk_index_not_negative")),
        sa.CheckConstraint(
            "page_start >= 1 AND page_end >= page_start", name=op.f("ck_chunks_pages_in_order")
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= 0 AND (page_end > page_start"
            " OR (char_end > char_start AND length(text) = char_end - char_start))",
            name=op.f("ck_chunks_span_matches_the_text"),
        ),
        sa.CheckConstraint("length(text) > 0", name=op.f("ck_chunks_text_not_empty")),
        sa.CheckConstraint("token_count >= 1", name=op.f("ck_chunks_token_count_positive")),
        sa.CheckConstraint(
            "json_valid(section_path) AND json_type(section_path) = 'array'",
            name=op.f("ck_chunks_section_path_is_a_list"),
        ),
        sa.CheckConstraint(
            "length(chunker_version) BETWEEN 3 AND 40"
            " AND chunker_version NOT GLOB '*[^a-z0-9-]*'"
            " AND chunker_version GLOB '[a-z]*-[1-9]*' AND chunker_version GLOB '*[0-9]'"
            " AND chunker_version NOT GLOB '*--*'",
            name=op.f("ck_chunks_chunker_version_is_a_name_and_version"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_chunks_version_document_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "page_start"],
            ["pages.version_id", "pages.page_no"],
            name="fk_chunks_page_start_pages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "page_end"],
            ["pages.version_id", "pages.page_no"],
            name="fk_chunks_page_end_pages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.UniqueConstraint("version_id", "chunk_index", name="uq_chunks_version_index"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "text, title, section_path, tokenize = 'porter unicode61')"
    )
    insert_trigger = ("CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN", "END")
    op.execute(" ".join((insert_trigger[0], _INDEX_NEW_ROW, insert_trigger[1])))
    op.execute(
        "CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN"
        " DELETE FROM chunks_fts WHERE rowid = old.id; END"
    )
    update_trigger = (
        "CREATE TRIGGER chunks_fts_update AFTER UPDATE ON chunks BEGIN"
        " DELETE FROM chunks_fts WHERE rowid = old.id;",
        "END",
    )
    op.execute(" ".join((update_trigger[0], _INDEX_NEW_ROW, update_trigger[1])))


def downgrade() -> None:
    op.execute("DROP TRIGGER chunks_fts_update")
    op.execute("DROP TRIGGER chunks_fts_delete")
    op.execute("DROP TRIGGER chunks_fts_insert")
    op.execute("DROP TABLE chunks_fts")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
