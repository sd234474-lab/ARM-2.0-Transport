"""Temporary operator workspace and minimal durable per-command receipts."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BulkSession(Base):
    __tablename__ = "arm2_bulk_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    source: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSONB)


class BulkReceipt(Base):
    # No cascading link to the temporary workspace: cleanup cannot undo a SAVE.
    __tablename__ = "arm2_bulk_receipts"
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    row_key: Mapped[str] = mapped_column(String(100))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("arm2_vehicles.id", ondelete="RESTRICT"))
    event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("arm2_history_events.event_id", ondelete="RESTRICT"))
    version_after: Mapped[int] = mapped_column(Integer)
    fields: Mapped[list] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BulkCleanup(Base):
    __tablename__ = "arm2_bulk_cleanup"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
