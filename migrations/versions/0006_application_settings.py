"""Add persistent application settings."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "application_settings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "application_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    if "application_settings" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_table("application_settings")
