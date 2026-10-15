"""The persistent job queue (ADR 0005).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Written out rather than imported (see 0002).
ULID_CHECK = (
    "length({c}) = 26 AND substr({c}, 1, 1) BETWEEN '0' AND '7'"
    " AND {c} NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'"
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ULID_CHECK.format(c="id"), name="ck_jobs_id_is_a_ulid"),
        sa.CheckConstraint(
            "stage IN ('extract', 'ocr', 'metadata', 'chunk', 'embed', 'enrich')",
            name="ck_jobs_stage_known",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="ck_jobs_status_known"
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name="ck_jobs_attempts_in_range"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        sa.CheckConstraint(
            "length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_jobs_input_is_a_hash",
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR (length(last_error) BETWEEN 1 AND 100"
            " AND last_error NOT GLOB '*[^A-Za-z0-9_]*')",
            name="ck_jobs_error_is_a_class_name",
        ),
        sa.CheckConstraint(
            "locked_by IS NULL OR (" + ULID_CHECK.format(c="locked_by") + ")",
            name="ck_jobs_worker_is_a_ulid",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (locked_by IS NOT NULL AND locked_at IS NOT NULL)",
            name="ck_jobs_lock_matches_status",
        ),
        sa.CheckConstraint(
            "(status IN ('done', 'failed')) = (finished_at IS NOT NULL)",
            name="ck_jobs_finished_when_ended",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_jobs_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "stage", "priority", "run_after"])
    op.create_index("ix_jobs_document_id", "jobs", ["document_id"])
    op.create_index(
        "uq_jobs_open_work",
        "jobs",
        ["document_id", "stage", "input_hash"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
