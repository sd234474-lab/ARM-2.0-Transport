"""Introduce initial ARM 2.0 VEH storage for the common object pipeline.

This is a bounded foundation: owner/document/calibration/process rules remain open.
No legacy Transport MAX tables or user-facing routes are changed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "arm2_vehicles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("plate_number", sa.String(), nullable=False),
        sa.Column(
            "type_id",
            sa.String(),
            sa.ForeignKey("arm2_vehicle_types.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "accounting_state",
            sa.String(),
            nullable=False,
            server_default=sa.text("'Действующая'"),
        ),
        sa.Column("model", sa.String()),
        sa.Column("wialon_unit_id", sa.String()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("id ~ '^VEH-[0-9]{6,}$'", name="ck_arm2_vehicles_id"),
        sa.CheckConstraint("name = btrim(name) AND name <> ''", name="ck_arm2_vehicles_name"),
        sa.CheckConstraint(
            "(plate_number ~ '^[АВЕКМНОРСТУХ][0-9]{3}[АВЕКМНОРСТУХ]{2}[0-9]{2,3}$') "
            "OR (plate_number ~ '^[0-9]{4}[АВЕКМНОРСТУХ]{2}[0-9]{2}$')",
            name="ck_arm2_vehicles_plate_format",
        ),
        sa.CheckConstraint(
            "accounting_state IN ('Действующая', 'Архивная')",
            name="ck_arm2_vehicles_accounting_state",
        ),
        sa.UniqueConstraint("plate_number", name="uq_arm2_vehicles_plate_number"),
    )


def downgrade():
    op.drop_table("arm2_vehicles")
