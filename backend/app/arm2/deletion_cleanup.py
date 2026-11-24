"""Run committed cleanup intents through the existing protected B3 storage provider."""

import re

from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from app.arm2 import document_file_storage as files
from app.arm2.approval_models import ApprovalAttempt
from app.arm2.document_file_models import DocumentFile, FileAcceptance
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireUpload


def still_referenced(db, path):
    if db.scalar(select(DocumentFile.id).where(DocumentFile.storage_ref == path)):
        return True
    # Only live unpublished plans can still turn into current FILEs.
    for model, column, conditions in (
        (FileAcceptance, FileAcceptance.plan, (FileAcceptance.state != "accepted",)),
        (ApprovalAttempt, ApprovalAttempt.plan, (ApprovalAttempt.result.is_(None),)),
        (QuestionnaireUpload, QuestionnaireUpload.prepared, (QuestionnaireDraft.status == "OPEN",)),
    ):
        query = select(model).where(cast(column, String).contains(path, autoescape=True), *conditions)
        if model is QuestionnaireUpload:
            query = query.join(QuestionnaireDraft, QuestionnaireDraft.id == model.draft_id)
        if db.scalars(query.limit(1)).first():
            return True
    return False


def clean_job(database, job, report):
    item = job.payload
    try:
        if token := item.get("token"):
            report("LOCAL", 0, 1)
            files.private_path(token).unlink(missing_ok=True)
            return None
        root = files.document_root() + "/"
        target = item["storage_ref"]
        if not target.startswith(root) or not re.fullmatch(r"FILE-[0-9]{6,}", item["id"]):
            return {"code": "ROOT", "message": "Оригинал вне подтверждённого корня. Требуется сверка хранилища"}
        parts = target[len(root):].split("/")
        if len(parts) < 3 or any(part in {"", ".", ".."} or "\\" in part for part in parts) or parts[-2] != item["id"]:
            return {"code": "PATH", "message": "Путь оригинала не прошёл проверку. Требуется сверка хранилища"}
        with Session(database) as db:
            if still_referenced(db, target):
                return {"code": "REFERENCED", "message": "На оригинал ещё ссылается другая запись или операция"}
        report("STORAGE", 0, 1)
        provider = files.storage()
        metadata = getattr(provider, "document_metadata", provider.metadata)
        item_metadata = metadata(target)
        if item_metadata is None:
            return None  # A committed earlier attempt may have moved the exact original to trash.
        if (item_metadata.get("type"), item_metadata.get("sha256"), item_metadata.get("size")) != (
            "file", item["sha256"], item["size"]
        ):
            return {"code": "IDENTITY", "message": "SHA или размер оригинала изменились. Требуется сверка хранилища"}
        # Only this verified, non-reusable FILE path is sent to the provider's reversible trash operation.
        provider.trash_exchange_file(target, item["sha256"])
        if metadata(target) is not None:
            raise files.StorageUnavailable("Оригинал всё ещё в хранилище")
        return None
    except Exception:
        attempts = (job.failure or {}).get("attempts", 0) + 1
        return {"code": "STORAGE", "message": "Очистка пока недоступна. Система повторит попытку",
                "attempts": attempts, "retry_after": min(2 ** min(attempts + 1, 9), 300)}
