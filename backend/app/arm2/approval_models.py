"""One request for two header numbers; immutable characteristic/evidence snapshots."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ApprovalRequest(Base):
    __tablename__ = "arm2_approval_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    contract_number_1: Mapped[str] = mapped_column(String)
    contract_number_2: Mapped[str] = mapped_column(String)
    vehicles_snapshot: Mapped[list] = mapped_column(JSONB)
    letter_file_id: Mapped[str] = mapped_column(
        ForeignKey(
            "arm2_document_files.id",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_arm2_approval_letter",
        )
    )
    package_file_id: Mapped[str] = mapped_column(
        ForeignKey(
            "arm2_document_files.id",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_arm2_approval_package",
        )
    )
    response_file_ids: Mapped[list] = mapped_column(JSONB)
    last_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        Index("ix_arm2_approval_created", "created_at"),
        ForeignKeyConstraint(
            ["letter_file_id", "id"],
            ["arm2_document_files.id", "arm2_document_files.process_id"],
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_arm2_approval_letter_owner",
        ),
        ForeignKeyConstraint(
            ["package_file_id", "id"],
            ["arm2_document_files.id", "arm2_document_files.process_id"],
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_arm2_approval_package_owner",
        ),
        CheckConstraint(
            "id ~ '^APPROVAL-[0-9]{6,}$' AND version>0 AND btrim(contract_number_1)<>'' AND btrim(contract_number_2)<>''",
            name="ck_arm2_approval_request",
        ),
        CheckConstraint(
            "jsonb_typeof(vehicles_snapshot)='array' AND jsonb_array_length(vehicles_snapshot) BETWEEN 1 AND 100 AND jsonb_typeof(response_file_ids)='array'",
            name="ck_arm2_approval_snapshot",
        ),
    )


class ApprovalDecision(Base):
    __tablename__ = "arm2_approval_decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("arm2_approval_requests.id", ondelete="RESTRICT"))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("arm2_vehicles.id", ondelete="RESTRICT"))
    plate_number: Mapped[str] = mapped_column(String)
    volume_m3: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String)
    response_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("arm2_document_files.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        Index("ix_arm2_approval_characteristics", "vehicle_id", "plate_number", "volume_m3", "decided_at"),
        ForeignKeyConstraint(
            ["response_file_id", "request_id"],
            ["arm2_document_files.id", "arm2_document_files.process_id"],
            name="fk_arm2_approval_response_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("request_id", "vehicle_id", name="uq_arm2_approval_request_vehicle"),
        CheckConstraint(
            "id ~ '^APPROVAL-ITEM-[0-9]{6,}$' AND version>0 AND volume_m3>0 AND volume_m3<'Infinity'::float8",
            name="ck_arm2_approval_decision",
        ),
        CheckConstraint(
            "(state='PENDING' AND response_file_id IS NULL AND decided_at IS NULL AND decided_by IS NULL) OR (state IN ('APPROVED','REJECTED') AND response_file_id IS NOT NULL AND decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name="ck_arm2_approval_evidence",
        ),
    )


class ApprovalAttempt(Base):
    """Technical replay ledger, not a second domain SAVE or a business status."""

    __tablename__ = "arm2_approval_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    action: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    principal_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    fingerprint: Mapped[str] = mapped_column(String)
    plan: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_arm2_approval_attempt_principal", "principal_id", "created_at"),
        CheckConstraint(
            "id ~ '^[0-9a-f-]{36}$' AND action IN ('calibration','letter','response-stage','response') AND fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_arm2_approval_attempt",
        ),
    )
