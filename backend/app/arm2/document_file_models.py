"""Immutable accepted bundles and private, retryable staging/acceptance records."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentRevision(Base):
    __tablename__ = "arm2_document_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_documents.id", ondelete="RESTRICT"))
    number: Mapped[int] = mapped_column(Integer)
    file_ids: Mapped[list[str]] = mapped_column(JSONB)
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String)
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(file_ids)='array' AND jsonb_array_length(file_ids)<=32",
            name="ck_arm2_revision_manifest",
        ),
        UniqueConstraint("id", "document_id", name="uq_arm2_revision_document"),
        UniqueConstraint("document_id", "number", name="uq_arm2_revision_number"),
        CheckConstraint(
            "id ~ '^DOCUMENT-REV-[0-9]{6,}$' AND number>0 AND source IN ('arm2-api-manual','arm2-api-correct')",
            name="ck_arm2_revision_identity",
        ),
    )


class DocumentFile(Base):
    __tablename__ = "arm2_document_files"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_documents.id", ondelete="RESTRICT"))
    original_upload_name: Mapped[str] = mapped_column(String)
    stored_name: Mapped[str] = mapped_column(String)
    storage_ref: Mapped[str] = mapped_column(String, unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String)
    media_type: Mapped[str] = mapped_column(String)
    extension: Mapped[str] = mapped_column(String)
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_arm2_file_document"),
        UniqueConstraint("document_id", "sha256", name="uq_arm2_document_file_sha"),
        CheckConstraint(
            "id ~ '^FILE-[0-9]{6,}$' AND size>0 AND sha256 ~ '^[0-9a-f]{64}$'", name="ck_arm2_file_identity"
        ),
        CheckConstraint(
            "extension IN ('pdf','png','jpg','gif','webp','docx','xlsx','pptx','bin') AND stored_name<>'' AND original_upload_name<>'' AND storage_ref LIKE 'disk:/%'",
            name="ck_arm2_file_metadata",
        ),
    )


class DocumentRevisionContext(Base):
    """Immutable historical parent snapshot; original manifests are never rewritten."""
    __tablename__ = "arm2_document_revision_contexts"
    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String)
    parent_type: Mapped[str] = mapped_column(String)
    parent_id: Mapped[str] = mapped_column(String)
    _cp: Mapped[str | None] = mapped_column(String, Computed("CASE WHEN parent_type='CP' THEN parent_id END", persisted=True), ForeignKey("arm2_counterparties.id", ondelete="RESTRICT"))
    _org: Mapped[str | None] = mapped_column(String, Computed("CASE WHEN parent_type='ORG' THEN parent_id END", persisted=True), ForeignKey("arm2_organizations.id", ondelete="RESTRICT"))
    _veh: Mapped[str | None] = mapped_column(String, Computed("CASE WHEN parent_type='VEH' THEN parent_id END", persisted=True), ForeignKey("arm2_vehicles.id", ondelete="RESTRICT"))
    __table_args__ = (
        ForeignKeyConstraint(["revision_id", "document_id"], ["arm2_document_revisions.id", "arm2_document_revisions.document_id"], ondelete="RESTRICT"),
        CheckConstraint("(parent_type='CP' AND parent_id ~ '^CP-[0-9]{6,}$') OR (parent_type='ORG' AND parent_id ~ '^ORG-[0-9]{6,}$') OR (parent_type='VEH' AND parent_id ~ '^VEH-[0-9]{6,}$')", name="ck_arm2_revision_context_parent"),
    )


class DocumentFileCorrection(Base):
    __tablename__ = "arm2_document_file_corrections"
    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String)
    reason_code: Mapped[str] = mapped_column(String)
    actor_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    operation: Mapped[str] = mapped_column(String)
    correlation_id: Mapped[str] = mapped_column(String, unique=True)
    history_event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("arm2_history_events.event_id", ondelete="RESTRICT"), unique=True)
    __table_args__ = (
        ForeignKeyConstraint(["file_id", "document_id"], ["arm2_document_files.id", "arm2_document_files.document_id"], ondelete="RESTRICT"),
        CheckConstraint("reason_code='incorrect_upload' AND operation='CORRECT' AND correlation_id ~ '^[0-9a-f-]{36}$'", name="ck_arm2_correction_identity"),
    )


class RevisionFile(Base):
    __tablename__ = "arm2_document_revision_files"
    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(String)
    file_id: Mapped[str] = mapped_column(String)
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "document_id"],
            ["arm2_document_revisions.id", "arm2_document_revisions.document_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_id", "document_id"],
            ["arm2_document_files.id", "arm2_document_files.document_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("revision_id", "file_id", name="uq_arm2_revision_file"),
        CheckConstraint("position>=0 AND position<32", name="ck_arm2_revision_file_position"),
    )


class FileStage(Base):
    __tablename__ = "arm2_document_file_stages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_documents.id", ondelete="RESTRICT"))
    principal_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    session_hash: Mapped[str] = mapped_column(String)
    file_id: Mapped[str] = mapped_column(String, unique=True)
    original_upload_name: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String)
    media_type: Mapped[str | None] = mapped_column(String)
    extension: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "id ~ '^[0-9a-f]{64}$' AND file_id ~ '^FILE-[0-9]{6,}$' AND size>0 AND state IN ('empty','ready','consumed')",
            name="ck_arm2_stage_identity",
        ),
    )


class FileAcceptance(Base):
    __tablename__ = "arm2_document_file_acceptances"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, ForeignKey("arm2_documents.id", ondelete="RESTRICT"))
    principal_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    session_hash: Mapped[str] = mapped_column(String)
    expected_version: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String)
    plan: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String)
    result: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "id ~ '^[0-9a-f-]{36}$' AND expected_version>0 AND state IN ('prepared','uploaded','accepted')",
            name="ck_arm2_acceptance_identity",
        ),
    )
