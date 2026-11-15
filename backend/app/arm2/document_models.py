from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Document(Base):
    __tablename__ = "arm2_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_type: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[str] = mapped_column(String, nullable=False)
    kind_ref: Mapped[str] = mapped_column(
        String, ForeignKey("arm2_document_kinds.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    archive_state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'Неархивный'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # Storage-only generated keys give a polymorphic parent real typed FKs.
    # They cannot be supplied by an operator or appear in the command/response.
    _cp: Mapped[str | None] = mapped_column(
        "_cp",
        String,
        Computed("CASE WHEN parent_type='CP' THEN parent_id END", persisted=True),
        ForeignKey("arm2_counterparties.id", ondelete="RESTRICT"),
    )
    _org: Mapped[str | None] = mapped_column(
        "_org",
        String,
        Computed("CASE WHEN parent_type='ORG' THEN parent_id END", persisted=True),
        ForeignKey("arm2_organizations.id", ondelete="RESTRICT"),
    )
    _veh: Mapped[str | None] = mapped_column(
        "_veh",
        String,
        Computed("CASE WHEN parent_type='VEH' THEN parent_id END", persisted=True),
        ForeignKey("arm2_vehicles.id", ondelete="RESTRICT"),
    )
    __table_args__ = (
        CheckConstraint("id ~ '^DOCUMENT-[0-9]{6,}$'", name="ck_arm2_documents_id"),
        CheckConstraint(
            "(parent_type='CP' AND parent_id ~ '^CP-[0-9]{6,}$') OR (parent_type='ORG' AND parent_id ~ '^ORG-[0-9]{6,}$') OR (parent_type='VEH' AND parent_id ~ '^VEH-[0-9]{6,}$')",
            name="ck_arm2_documents_parent",
        ),
        CheckConstraint(
            "name=btrim(name) AND name<>'' AND name !~ '^[[:space:]]|[[:space:]]$'",
            name="ck_arm2_documents_name",
        ),
        CheckConstraint("archive_state='Неархивный'", name="ck_arm2_documents_archive"),
        CheckConstraint("version>0", name="ck_arm2_documents_version"),
        Index("ix_arm2_documents_parent_kind", "parent_type", "parent_id", "kind_ref", "archive_state"),
    )
