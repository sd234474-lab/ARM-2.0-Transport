"""Private B4 workspaces and durable SAVE receipts, independent of temp lifetime."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = depends_on = None


def upgrade():
    op.create_table("arm2_bulk_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", JSONB, nullable=False))
    op.create_index("ix_arm2_bulk_sessions_user_id", "arm2_bulk_sessions", ["user_id"])
    op.create_table("arm2_bulk_receipts",
        sa.Column("command_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("row_key", sa.String(100), nullable=False),
        sa.Column("vehicle_id", sa.String(), sa.ForeignKey("arm2_vehicles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("arm2_history_events.event_id", ondelete="RESTRICT")),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_arm2_bulk_receipts_session_id", "arm2_bulk_receipts", ["session_id"])
    op.create_table("arm2_bulk_cleanup",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    db = op.get_bind()
    db.execute(sa.text("LOCK TABLE arm2_bulk_sessions, arm2_bulk_receipts, arm2_bulk_cleanup IN ACCESS EXCLUSIVE MODE"))
    if any(db.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")) for table in
           ("arm2_bulk_sessions", "arm2_bulk_receipts", "arm2_bulk_cleanup")):
        raise RuntimeError("B4 workspaces/protocol/audit exist; downgrade refused without deleting data")
    for table in ("arm2_bulk_cleanup", "arm2_bulk_receipts", "arm2_bulk_sessions"):
        op.drop_table(table)
