from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, text
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
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
    )
