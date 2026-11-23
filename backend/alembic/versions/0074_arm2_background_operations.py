"""Persist background operation progress and outcomes."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "arm2_background_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.String(40), nullable=False),
        sa.Column("author_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("progress", JSONB, nullable=False),
        sa.Column("failure", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_arm2_operation_state"),
    )
    op.create_index("uq_arm2_active_operation", "arm2_background_operations", ["kind", "resource_id"],
                    unique=True, postgresql_where=sa.text("state IN ('QUEUED','RUNNING')"))
    op.create_index("ix_arm2_operation_journal", "arm2_background_operations", ["kind", "author_id", "created_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM arm2_background_operations)")):
        raise RuntimeError("0074 downgrade refused: ARM2 background operation journal contains records")
    op.drop_table("arm2_background_operations")
