from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class HistoryEvent(Base):
    __tablename__ = "arm2_history_events"

    event_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    object_type: Mapped[str] = mapped_column(String, nullable=False)
    object_id: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String)
    version_before: Mapped[int | None] = mapped_column(Integer)
    version_after: Mapped[int | None] = mapped_column(Integer)
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    related_objects: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("object_type IN ('TYPE', 'VEH')", name="ck_arm2_history_object_type"),
        CheckConstraint(
            "(object_type = 'TYPE' AND object_id ~ '^TYPE-[0-9]{6,}$') OR "
            "(object_type = 'VEH' AND object_id ~ '^VEH-[0-9]{6,}$')",
            name="ck_arm2_history_object_id",
        ),
        CheckConstraint("operation IN ('CREATED', 'UPDATED')", name="ck_arm2_history_operation"),
        CheckConstraint("btrim(actor_kind) <> ''", name="ck_arm2_history_actor_kind"),
        CheckConstraint("btrim(actor_id) <> ''", name="ck_arm2_history_actor_id"),
        CheckConstraint("btrim(source) <> ''", name="ck_arm2_history_source"),
        CheckConstraint(
            "correlation_id IS NULL OR btrim(correlation_id) <> ''",
            name="ck_arm2_history_correlation",
        ),
        CheckConstraint(
            "version_before IS NULL OR version_before > 0",
            name="ck_arm2_history_version_before",
        ),
        CheckConstraint(
            "version_after IS NULL OR version_after > 0",
            name="ck_arm2_history_version_after",
        ),
        CheckConstraint(
            "(operation = 'CREATED' AND version_before IS NULL AND version_after = 1) OR "
            "(operation = 'UPDATED' AND version_before IS NOT NULL "
            "AND version_after IS NOT NULL AND version_after > version_before)",
            name="ck_arm2_history_versions",
        ),
        CheckConstraint(
            "jsonb_typeof(changes) = 'object' AND changes <> '{}'::jsonb",
            name="ck_arm2_history_changes",
        ),
        CheckConstraint(
            "jsonb_typeof(related_objects) = 'array'",
            name="ck_arm2_history_related_objects",
        ),
        Index(
            "ix_arm2_history_object_chronology",
            "object_type",
            "object_id",
            "occurred_at",
            "event_id",
        ),
    )
