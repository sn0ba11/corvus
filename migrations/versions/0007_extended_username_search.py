"""Add per-investigation extended username search option."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("investigations")}
    if "extended_username_search" in columns:
        return
    op.add_column(
        "investigations",
        sa.Column("extended_username_search", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("investigations")}
    if "extended_username_search" not in columns:
        return
    op.drop_column("investigations", "extended_username_search")
