"""Run committed cleanup intents through the existing protected B3 storage provider."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arm2 import document_file_storage as files
from app.arm2.approval_models import ApprovalAttempt
from app.arm2.document_file_models import DocumentFile, FileAcceptance
from app.arm2.file_read_guard import protected_cleanup
from app.arm2.ids import is_canonical_id
from app.arm2.questionnaire_models import QuestionnaireDraft, QuestionnaireUpload


def still_referenced(db, path):
    if db.scalar(select(DocumentFile.id).where(DocumentFile.storage_ref == path)):
        return True
    # These are the writer-owned locations of unpublished originals.
    if any(item["storage_ref"] == path for attempt in db.scalars(
        select(FileAcceptance).where(FileAcceptance.state != "accepted")
    ) for item in attempt.plan["files"]):
        return True
    for attempt in db.scalars(select(ApprovalAttempt).where(
        ApprovalAttempt.result.is_(None), ApprovalAttempt.action.in_(("letter", "response-stage")),
    )):
        plans = attempt.plan["files"] if attempt.action == "letter" else [attempt.plan["file"]]
        if any(item["storage_ref"] == path for item in plans):
            return True
    for prepared in db.scalars(select(QuestionnaireUpload.prepared).join(QuestionnaireDraft).where(
        QuestionnaireDraft.status == "OPEN",
    )):
        if any(item["storage_ref"] == path for plan in prepared.values() for item in plan):
            return True
    return False


def clean_job(database, job, report):
    item = job.payload
    try:
        with protected_cleanup(database):
            return clean_original(database, item, report)
    except Exception:
        attempts = (job.failure or {}).get("attempts", 0) + 1
        return {"code": "STORAGE", "message": "Очистка пока недоступна. Система повторит попытку",
                "attempts": attempts, "retry_after": min(2 ** min(attempts + 1, 9), 300)}


def clean_original(database, item, report):
    with Session(database) as db:
        if token := item.get("token"):
            report("LOCAL", 0, 1)
            files.private_path(token).unlink(missing_ok=True)
            return None
        root = files.document_root() + "/"
        target = item["storage_ref"]
        if not target.startswith(root) or not is_canonical_id("FILE", item["id"]):
            return {"code": "ROOT", "message": "Оригинал вне подтверждённого корня. Требуется сверка хранилища"}
        parts = target[len(root):].split("/")
        if len(parts) < 3 or any(part in {"", ".", ".."} or "\\" in part for part in parts) or parts[-2] != item["id"]:
            return {"code": "PATH", "message": "Путь оригинала не прошёл проверку. Требуется сверка хранилища"}
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
    provider.trash_exchange_file(target, item["sha256"])
    if metadata(target) is not None:
        raise files.StorageUnavailable("Оригинал всё ещё в хранилище")
    return None
