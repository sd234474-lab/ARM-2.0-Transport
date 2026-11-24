from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class VehicleType(Base):
    __tablename__ = "arm2_vehicle_types"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    requires_sts: Mapped[bool | None] = mapped_column(Boolean, server_default=text("true"))
    requires_calibration: Mapped[bool | None] = mapped_column(Boolean)
    requires_wialon: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("id ~ '^TYPE-[0-9]{6,}$'", name="ck_arm2_vehicle_types_id"),
        CheckConstraint("name = btrim(name) AND name <> ''", name="ck_arm2_vehicle_types_name"),
        CheckConstraint(
            "requires_sts IS NULL OR requires_sts = true",
            name="ck_arm2_vehicle_types_requires_sts",
        ),
        CheckConstraint(
            "status IN ('REVIEW', 'CONFIRMED')",
            name="ck_arm2_vehicle_types_status",
        ),
        UniqueConstraint("name", name="uq_arm2_vehicle_types_name"),
    )
