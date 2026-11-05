"""initial schema: runs and articles

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("triggered_by", sa.String(length=32), server_default="manual", nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed')", name="ck_runs_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("post_date", sa.Date(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("generated_title", sa.Text(), nullable=True),
        sa.Column("generated_body", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="selected", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("kbnews_post_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('world', 'india', 'maharashtra')", name="ck_articles_category"
        ),
        sa.CheckConstraint(
            "status IN ('selected', 'generated', 'posted', 'skipped', 'failed')",
            name="ck_articles_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_url"),
    )
    op.create_index("ix_articles_run_id", "articles", ["run_id"], unique=False)
    op.create_index(
        "uq_articles_posted_per_category_day",
        "articles",
        ["category", "post_date"],
        unique=True,
        postgresql_where=sa.text("status = 'posted'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_articles_posted_per_category_day",
        table_name="articles",
        postgresql_where=sa.text("status = 'posted'"),
    )
    op.drop_index("ix_articles_run_id", table_name="articles")
    op.drop_table("articles")
    op.drop_table("runs")
