"""Add reusable search-query templates."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "search_templates" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "search_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("query_template", sa.Text(), nullable=False),
        sa.Column("engines", sa.JSON(), nullable=False),
        sa.Column("query_types", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    if "search_templates" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("search_templates")
