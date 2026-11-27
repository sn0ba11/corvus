"""Add first-class location observations."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "location_observations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "location_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("investigation_id", sa.String(length=36), nullable=False),
        sa.Column("finding_id", sa.String(length=36), nullable=True),
        sa.Column("place_name", sa.String(length=300), nullable=False),
        sa.Column("locality", sa.String(length=200), nullable=True),
        sa.Column("region", sa.String(length=200), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("precision_radius_m", sa.Integer(), nullable=True),
        sa.Column("evidence_type", sa.String(length=40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_platform", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("account_identifier", sa.String(length=320), nullable=True),
        sa.Column("privacy_masked", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("raw_evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], name="fk_location_observations_finding", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_location_observations_investigation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_location_observations_investigation_id",
        "location_observations",
        ["investigation_id"],
    )
    op.create_index(
        "ix_location_observations_finding_id", "location_observations", ["finding_id"]
    )


def downgrade() -> None:
    if "location_observations" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_location_observations_finding_id", table_name="location_observations")
    op.drop_index("ix_location_observations_investigation_id", table_name="location_observations")
    op.drop_table("location_observations")
