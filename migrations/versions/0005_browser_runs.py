"""Add managed-browser run provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "browser_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "browser_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("investigation_id", sa.String(length=36), nullable=False),
        sa.Column("collector_id", sa.String(length=50), nullable=False),
        sa.Column("session_name", sa.String(length=100), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("page_title", sa.Text(), nullable=True),
        sa.Column("challenge_type", sa.String(length=100), nullable=True),
        sa.Column("browser_version", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("screenshot_reference", sa.Text(), nullable=True),
        sa.Column("screenshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("html_reference", sa.Text(), nullable=True),
        sa.Column("html_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_reference", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_browser_runs_investigation_id", "browser_runs", ["investigation_id"])


def downgrade() -> None:
    if "browser_runs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_browser_runs_investigation_id", table_name="browser_runs")
    op.drop_table("browser_runs")
