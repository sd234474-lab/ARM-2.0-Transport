"""Add the ARM 2.0 vehicle ID sequence."""

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence("arm2_vehicle_id_seq", start=1)))


def downgrade():
    op.execute(sa.schema.DropSequence(sa.Sequence("arm2_vehicle_id_seq")))
