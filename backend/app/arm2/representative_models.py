from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CounterpartyRepresentative(Base):
    __tablename__ = "nsi_counterparty_representatives"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_counterparties.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[str | None] = mapped_column(String(500))
    authority_basis: Mapped[str | None] = mapped_column(String(1000))
    phone: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'Действующий'"))
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    last_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(200))
    middle_name: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __table_args__ = (
        CheckConstraint("id ~ '^CPREP-[0-9]{6,}$'", name="ck_cprep_id"),
        CheckConstraint("state IN ('Действующий','Архивный')", name="ck_cprep_state"),
    )


class OrganizationRepresentative(Base):
    __tablename__ = "nsi_organization_representatives"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_organizations.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[str | None] = mapped_column(String(500))
    authority_basis: Mapped[str | None] = mapped_column(String(1000))
    phone: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'Действующий'"))
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    last_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(200))
    middle_name: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __table_args__ = (
        CheckConstraint("id ~ '^ORGREP-[0-9]{6,}$'", name="ck_orgrep_id"),
        CheckConstraint("state IN ('Действующий','Архивный')", name="ck_orgrep_state"),
    )
