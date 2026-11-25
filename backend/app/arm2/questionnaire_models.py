"""Private B5 drafts, private B3 staging provenance, immutable command receipts."""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class QuestionnaireDraft(Base):
    __tablename__ = 'arm2_questionnaire_drafts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default='OPEN')
    filename: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64))
    filled_date: Mapped[date | None] = mapped_column(Date)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    choices: Mapped[dict] = mapped_column(JSONB, default=dict)
    preview: Mapped[dict] = mapped_column(JSONB, default=dict)
    staging: Mapped[dict] = mapped_column(JSONB, default=dict)


class QuestionnaireUpload(Base):
    """Uses B3 private_path/verified_stage/provider; this is not a second FILE store."""
    __tablename__ = 'arm2_questionnaire_uploads'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey('arm2_questionnaire_drafts.id', ondelete='RESTRICT'))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str | None] = mapped_column(String)
    extension: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default='STAGED')
    prepared: Mapped[dict] = mapped_column(JSONB, default=dict)


class QuestionnaireReceipt(Base):
    __tablename__ = 'arm2_questionnaire_receipts'
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey('arm2_questionnaire_drafts.id', ondelete='RESTRICT'))
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    fingerprint: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    result: Mapped[dict] = mapped_column(JSONB)


class QuestionnaireExport(Base):
    __tablename__ = 'arm2_questionnaire_exports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    cp_id: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    incomplete_reason: Mapped[str | None] = mapped_column(String(500))
    missing: Mapped[list] = mapped_column(JSONB, default=list)
    __table_args__ = (CheckConstraint("cp_id ~ '^CP-[0-9]{6,}$'", name="ck_arm2_questionnaire_export_cp"),)
