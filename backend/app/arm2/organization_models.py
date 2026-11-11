from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Organization(Base):
    __tablename__ = "arm2_organizations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(2000))
    legal_address: Mapped[str | None] = mapped_column(String(2000))
    postal_address: Mapped[str | None] = mapped_column(String(2000))
    legal_form: Mapped[str | None] = mapped_column(String(500))
    short_name: Mapped[str | None] = mapped_column(String(500))
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    phone: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    bank_name: Mapped[str | None] = mapped_column(String(500))
    bik: Mapped[str | None] = mapped_column(String(9))
    settlement_account: Mapped[str | None] = mapped_column(String(20))
    correspondent_account: Mapped[str | None] = mapped_column(String(20))
    primary_signer_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __table_args__ = (
        ForeignKeyConstraint(
            ["primary_signer_id", "id"],
            ["nsi_organization_representatives.id", "nsi_organization_representatives.parent_id"],
            name="fk_arm2_org_primary_signer_parent", ondelete="RESTRICT", use_alter=True,
        ),
        CheckConstraint("id ~ '^ORG-[0-9]{6,}$'", name="ck_arm2_organizations_id"),
        CheckConstraint("name=btrim(name) AND name<>''", name="ck_arm2_organizations_name"),
        CheckConstraint("status IN ('REVIEW','CONFIRMED')", name="ck_arm2_organizations_status"),
        UniqueConstraint("inn", name="uq_arm2_organizations_inn"),
    )
