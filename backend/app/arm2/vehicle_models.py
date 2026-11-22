from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Vehicle(Base):
    """ARM 2.0 vehicle foundation; no legacy Transport MAX table is modified."""

    __tablename__ = "arm2_vehicles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plate_number: Mapped[str] = mapped_column(String, nullable=False)
    type_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("arm2_vehicle_types.id", ondelete="RESTRICT")
    )
    accounting_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'Действующая'")
    )
    model: Mapped[str | None] = mapped_column(String)
    wialon_unit_id: Mapped[str | None] = mapped_column(String)
    owner_cp_id: Mapped[str | None] = mapped_column(String, ForeignKey("arm2_counterparties.id", ondelete="RESTRICT"))
    owner_org_id: Mapped[str | None] = mapped_column(String, ForeignKey("arm2_organizations.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    calibration_m3: Mapped[float | None] = mapped_column(Float)
    calibration_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calibration_confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    calibration_document_id: Mapped[str | None] = mapped_column(ForeignKey("arm2_documents.id", ondelete="RESTRICT", use_alter=True, name="fk_arm2_calibration_document"))
    calibration_revision_id: Mapped[str | None] = mapped_column(ForeignKey("arm2_document_revisions.id", ondelete="RESTRICT", use_alter=True, name="fk_arm2_calibration_revision"))
    calibration_plate_number: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        ForeignKeyConstraint(["calibration_revision_id","calibration_document_id"],["arm2_document_revisions.id","arm2_document_revisions.document_id"], use_alter=True, name="fk_arm2_calibration_revision_document", ondelete="RESTRICT"),
        CheckConstraint("(calibration_m3 IS NULL AND calibration_confirmed_at IS NULL AND calibration_confirmed_by IS NULL AND calibration_document_id IS NULL AND calibration_revision_id IS NULL AND calibration_plate_number IS NULL) OR (calibration_m3 IS NOT NULL AND calibration_m3>0 AND calibration_m3<'Infinity'::float8 AND calibration_confirmed_at IS NOT NULL AND calibration_confirmed_by IS NOT NULL AND calibration_document_id IS NOT NULL AND calibration_revision_id IS NOT NULL AND calibration_plate_number IS NOT NULL)", name="ck_arm2_vehicle_calibration"),
        CheckConstraint("id ~ '^VEH-[0-9]{6,}$'", name="ck_arm2_vehicles_id"),
        CheckConstraint("name = btrim(name) AND name <> ''", name="ck_arm2_vehicles_name"),
        CheckConstraint(
            "(plate_number ~ '^[АВЕКМНОРСТУХ][0-9]{3}[АВЕКМНОРСТУХ]{2}[0-9]{2,3}$') "
            "OR (plate_number ~ '^[0-9]{4}[АВЕКМНОРСТУХ]{2}[0-9]{2}$')",
            name="ck_arm2_vehicles_plate_format",
        ),
        CheckConstraint(
            "accounting_state IN ('Действующая', 'Архивная')",
            name="ck_arm2_vehicles_accounting_state",
        ),
        UniqueConstraint("plate_number", name="uq_arm2_vehicles_plate_number"),
        CheckConstraint("NOT (owner_cp_id IS NOT NULL AND owner_org_id IS NOT NULL)", name="ck_arm2_vehicles_one_owner"),
    )
