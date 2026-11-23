"""The OCR decision for each page of a PDF: whether its text layer is usable, and if not, why.

Pages stored before this migration were read by ``pdf-1``, which kept no measurement of the page,
so the rule (``vault.ingest.text_layer``) cannot be applied to them from the database: an empty
page of ``pdf-1`` is ``empty`` (the rule decides that from the text alone), any other page of
``pdf-1`` is ``unjudged`` until its version is extracted again (nothing does that on its own yet;
the OCR stage of Phase 3 has to), and a page of any other extractor
(TXT, Markdown, DOCX) has no value. The CHECK is added with the column, and SQLite tests it
against every existing row while the column is still empty, so it cannot require a value on a PDF
page; the application does (``vault.db.pages``).

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Written as SQL, so the stored name and text of the CHECK are exactly the ones the schema
    # gives (a test compares them with a database made from the schema).
    op.execute(
        "ALTER TABLE pages ADD COLUMN text_layer VARCHAR"
        " CONSTRAINT ck_pages_text_layer_known CHECK ("
        "text_layer IS NULL OR (text_layer IN ('usable', 'empty', 'sparse', 'unreadable',"
        " 'unjudged') AND extractor GLOB 'pdf-[1-9]*' AND extractor NOT GLOB 'pdf-*[^0-9]*'"
        " AND (text_layer = 'empty') = (text = '')"
        " AND (text_layer = 'unjudged') = (extractor = 'pdf-1' AND text <> '')))"
    )
    op.execute(
        "UPDATE pages SET text_layer = CASE WHEN text = '' THEN 'empty' ELSE 'unjudged' END"
        " WHERE extractor = 'pdf-1'"
    )


def downgrade() -> None:
    op.drop_column("pages", "text_layer")
