"""The usage ledger: one ``ai_requests`` row per language model call (plan section 9).

The plan's link to the conversation message is left out: the conversations tables do not exist
yet, and the migration that adds them adds the column with its foreign key (SQLite allows
``ADD COLUMN ... REFERENCES`` for a column whose default is NULL). The lists in the CHECKs are
frozen copies of ``Task``, ``PROVIDER_NAMES`` and ``ErrorKind``; a test compares them with a
database made from the schema.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _whole(column: str, *, nullable: bool = False) -> str:
    check = f"typeof({column}) = 'integer' AND {column} >= 0"
    return f"{column} IS NULL OR ({check})" if nullable else check


def upgrade() -> None:
    op.create_table(
        "ai_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_kind", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("est_cost_nanousd", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "task IN ('answer', 'answer_deep', 'classify', 'query_rewrite')",
            name=op.f("ck_ai_requests_task_known"),
        ),
        sa.CheckConstraint(
            "provider IN ('anthropic', 'openai', 'fake')",
            name=op.f("ck_ai_requests_provider_known"),
        ),
        sa.CheckConstraint(
            "length(model) BETWEEN 1 AND 128 AND model NOT GLOB '*[^A-Za-z0-9._:/@-]*'",
            name=op.f("ck_ai_requests_model_shape"),
        ),
        sa.CheckConstraint("status IN ('ok', 'error')", name=op.f("ck_ai_requests_status_known")),
        sa.CheckConstraint(
            "error_kind IS NULL OR error_kind IN ('authentication', 'rate_limited',"
            " 'unavailable', 'timeout', 'invalid_request', 'bad_response')",
            name=op.f("ck_ai_requests_error_kind_known"),
        ),
        sa.CheckConstraint(
            "(status = 'ok' AND error_kind IS NULL AND input_tokens IS NOT NULL"
            " AND cached_tokens IS NOT NULL AND output_tokens IS NOT NULL)"
            " OR (status = 'error' AND error_kind IS NOT NULL AND input_tokens IS NULL"
            " AND cached_tokens IS NULL AND output_tokens IS NULL AND est_cost_nanousd IS NULL)",
            name=op.f("ck_ai_requests_outcome_shape"),
        ),
        sa.CheckConstraint(
            _whole("input_tokens", nullable=True), name=op.f("ck_ai_requests_input_tokens_whole")
        ),
        sa.CheckConstraint(
            _whole("cached_tokens", nullable=True),
            name=op.f("ck_ai_requests_cached_tokens_whole"),
        ),
        sa.CheckConstraint(
            "cached_tokens IS NULL OR cached_tokens <= input_tokens",
            name=op.f("ck_ai_requests_cached_part_of_input"),
        ),
        sa.CheckConstraint(
            _whole("output_tokens", nullable=True),
            name=op.f("ck_ai_requests_output_tokens_whole"),
        ),
        sa.CheckConstraint(
            _whole("est_cost_nanousd", nullable=True), name=op.f("ck_ai_requests_cost_whole")
        ),
        sa.CheckConstraint(_whole("latency_ms"), name=op.f("ck_ai_requests_latency_whole")),
        sa.CheckConstraint(_whole("doc_count"), name=op.f("ck_ai_requests_doc_count_whole")),
        sa.PrimaryKeyConstraint("id", name="pk_ai_requests"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_ai_requests_created_at", "ai_requests", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_requests_created_at", table_name="ai_requests")
    op.drop_table("ai_requests")
