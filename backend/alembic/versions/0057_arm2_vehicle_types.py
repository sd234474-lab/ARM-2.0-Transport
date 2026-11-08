"""Add persistent ARM 2.0 vehicle types."""

import sqlalchemy as sa

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence("arm2_vehicle_type_id_seq", start=1)))
    op.create_table(
        "arm2_vehicle_types",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("requires_sts", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("requires_calibration", sa.Boolean()),
        sa.Column("requires_wialon", sa.Boolean()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("id ~ '^TYPE-[0-9]{6,}$'", name="ck_arm2_vehicle_types_id"),
        sa.CheckConstraint(
            "name = btrim(name) AND name <> ''",
            name="ck_arm2_vehicle_types_name",
        ),
        sa.CheckConstraint(
            "requires_sts IS NULL OR requires_sts = true",
            name="ck_arm2_vehicle_types_requires_sts",
        ),
        sa.CheckConstraint(
            "status IN ('REVIEW', 'CONFIRMED')",
            name="ck_arm2_vehicle_types_status",
        ),
        sa.UniqueConstraint("name", name="uq_arm2_vehicle_types_name"),
    )


def downgrade():
    op.drop_table("arm2_vehicle_types")
    op.execute(sa.schema.DropSequence(sa.Sequence("arm2_vehicle_type_id_seq")))
