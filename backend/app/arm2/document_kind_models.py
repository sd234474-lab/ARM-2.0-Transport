from sqlalchemy import BigInteger, CheckConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentKind(Base):
    __tablename__ = "arm2_document_kinds"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    applicable_object_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_current_documents: Mapped[int | None] = mapped_column(BigInteger)
    file_naming_template: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint("file_naming_template IS NULL OR file_naming_template IN ('DOCUMENT_NAME','PARENT_AND_KIND')", name="ck_arm2_kind_file_naming"),
        CheckConstraint("id ~ '^DOC-KIND-[0-9]{6,}$'", name="ck_arm2_document_kinds_id"),
        CheckConstraint(
            "name = btrim(name) AND name <> '' AND name !~ '^[[:space:]]|[[:space:]]$'",
            name="ck_arm2_document_kinds_name",
        ),
        CheckConstraint("version > 0", name="ck_arm2_document_kinds_version"),
        UniqueConstraint("name", name="uq_arm2_document_kinds_name"),
        CheckConstraint(
            "applicable_object_types IN ('[\"CP\"]'::jsonb, '[\"ORG\"]'::jsonb, '[\"VEH\"]'::jsonb, "
            "'[\"CP\",\"ORG\"]'::jsonb, '[\"CP\",\"VEH\"]'::jsonb, '[\"ORG\",\"VEH\"]'::jsonb, "
            "'[\"CP\",\"ORG\",\"VEH\"]'::jsonb)",
            name="ck_arm2_document_kinds_parents",
        ),
        CheckConstraint(
            "max_current_documents IS NULL OR max_current_documents > 0",
            name="ck_arm2_document_kinds_max_current",
        ),
    )
