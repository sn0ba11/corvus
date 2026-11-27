"""Add bounded username-pivot execution metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    investigation_columns = _columns("investigations")
    if "auto_follow_username_pivots" not in investigation_columns:
        with op.batch_alter_table("investigations") as batch:
            batch.add_column(
                sa.Column(
                    "auto_follow_username_pivots",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    run_columns = _columns("tool_runs")
    additions = [
        ("query_value", sa.Column("query_value", sa.String(length=320), nullable=True)),
        ("query_type", sa.Column("query_type", sa.String(length=20), nullable=True)),
        (
            "pivot_depth",
            sa.Column("pivot_depth", sa.Integer(), nullable=False, server_default="0"),
        ),
        (
            "pivot_source_finding_id",
            sa.Column("pivot_source_finding_id", sa.String(length=36), nullable=True),
        ),
        (
            "pivot_automatic",
            sa.Column(
                "pivot_automatic",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        ),
    ]
    missing = [column for name, column in additions if name not in run_columns]
    if missing:
        with op.batch_alter_table("tool_runs") as batch:
            for column in missing:
                batch.add_column(column)


def downgrade() -> None:
    run_columns = _columns("tool_runs")
    removable = [
        name
        for name in (
            "pivot_automatic",
            "pivot_source_finding_id",
            "pivot_depth",
            "query_type",
            "query_value",
        )
        if name in run_columns
    ]
    if removable:
        with op.batch_alter_table("tool_runs") as batch:
            for name in removable:
                batch.drop_column(name)

    investigation_columns = _columns("investigations")
    if "auto_follow_username_pivots" in investigation_columns:
        with op.batch_alter_table("investigations") as batch:
            batch.drop_column("auto_follow_username_pivots")
