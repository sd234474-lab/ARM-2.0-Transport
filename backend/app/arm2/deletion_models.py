"""ARM2 deletion marks and an audit trail independent of current catalog rows."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DeletionMark(Base):
    __tablename__ = "arm2_deletion_marks"

    object_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(36), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeletionBatch(Base):
    __tablename__ = "arm2_deletion_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Identifies the transaction authorized to retire only the captured B3 artifacts.
    execution_tx: Mapped[str | None] = mapped_column(String(24))
    artifacts: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_arm2_deletion_journal", "actor_id", "executed_at"),
        Index("ix_arm2_deletion_execution_tx", "execution_tx"),
    )
