"""Persistent state for ARM2 background operations."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

ACTIVE = ("QUEUED", "RUNNING")


class BackgroundOperation(Base):
    __tablename__ = "arm2_background_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(40))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(12))
    payload: Mapped[dict] = mapped_column(JSONB)
    progress: Mapped[dict] = mapped_column(JSONB)
    failure: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_arm2_operation_state"),
        Index("uq_arm2_active_operation", "kind", "resource_id", unique=True,
              postgresql_where=text("state IN ('QUEUED','RUNNING')")),
        Index("ix_arm2_operation_journal", "kind", "author_id", "created_at"),
    )
