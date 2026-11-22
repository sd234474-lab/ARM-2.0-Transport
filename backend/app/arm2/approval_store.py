"""Subject orchestration only: existing SAVE, B3 originals, leases and shared errors."""

import hashlib
import json
import math
from datetime import timedelta

from sqlalchemy import select

from app.arm2 import document_file_storage as files
from app.arm2.approval_models import ApprovalAttempt, ApprovalDecision, ApprovalRequest
from app.arm2.document_file_models import DocumentFile, DocumentFileCorrection
from app.arm2.document_file_store import (
    attempt_lease,
    document_access,
    instant,
    revision_files,
    staging_quota,
)
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.document_store import lock_kind, parent_access
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.models import LoginSession

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def context(user, key):
    return SaveContext("HUMAN", str(user.id), "arm2-api-approval", str(key))


def session_access(db, user, session_hash):
    session = db.get(LoginSession, session_hash, populate_existing=True)
    if session is None or session.user_id != user.id or session.expires_at <= instant():
        raise ObjectAuthorizationError("Сессия завершена. Войдите заново")


class _Adapter:
    def __init__(self, model, prefix):
        self.model, self.prefix = model, prefix

    def normalize(self, values):
        return dict(values)

    def create_record(self, db, values):
        row = self.model(id=next_id(db, self.prefix), version=1, **values)
        db.add(row)
        db.flush()
        return row

    def lock_record(self, db, object_id):
        return db.scalar(
            select(self.model)
            .where(self.model.id == object_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def apply(self, record, values):
        for key, value in values.items():
            setattr(record, key, value)


REQUEST_ADAPTER = _Adapter(ApprovalRequest, "APPROVAL")
DECISION_ADAPTER = _Adapter(ApprovalDecision, "APPROVAL-ITEM")


def _request_relations(db, current, values):
    if current:
        immutable = set(OBJECT_REGISTRY.require("APPROVAL").command_fields) - {
            "response_file_ids",
            "last_response_at",
        }
        if any(getattr(current, key) != values[key] for key in immutable):
            raise ValueError("Снимок выпущенного письма неизменяем; создайте новую заявку")
        if not set(current.response_file_ids).issubset(values["response_file_ids"]):
            raise ValueError("Ранее зарегистрированные ответы нельзя удалять")
        for file_id in values["response_file_ids"]:
            original = db.get(DocumentFile, file_id)
            if (
                original is None
                or original.process_id != current.id
                or original.extension not in {"pdf", "png", "jpg"}
            ):
                raise ValueError("Ответ не принадлежит этой заявке или имеет неподдерживаемый формат")
    result = []
    for row in values["vehicles_snapshot"]:
        if db.get(Vehicle, row["vehicle_id"]) is None:
            raise ObjectNotFoundError("Машина заявки не найдена")
        result.append(ObjectReference("VEH", row["vehicle_id"], "approval_vehicle"))
    return tuple(result)


def _decision_relations(db, current, values):
    if current and any(
        getattr(current, key) != values[key]
        for key in ["request_id", "vehicle_id", "plate_number", "volume_m3"]
    ):
        raise ValueError("Характеристики решения неизменяемы; создайте новую заявку")
    request = db.get(ApprovalRequest, values["request_id"])
    if request is None or not any(
        v["vehicle_id"] == values["vehicle_id"]
        and v["plate_number"] == values["plate_number"]
        and v["volume_m3"] == values["volume_m3"]
        for v in request.vehicles_snapshot
    ):
        raise ValueError("Решение не соответствует характеристикам заявки")
    if values["state"] not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("Недопустимое решение заказчика")
    return (
        ObjectReference("APPROVAL", request.id, "approval_request"),
        ObjectReference("VEH", values["vehicle_id"], "approval_vehicle"),
    )


def register_approval_adapters(registry=OBJECT_REGISTRY):
    registry.attach_adapter("APPROVAL", REQUEST_ADAPTER, reference_rules=(_request_relations,))
    registry.attach_adapter("APPROVAL-ITEM", DECISION_ADAPTER, reference_rules=(_decision_relations,))


def vehicle_access(db, user, vehicle_id, *, edit=False):
    parent_access(db, user, "VEH", vehicle_id, edit=edit)
    return db.get(Vehicle, vehicle_id, populate_existing=True)


def access(db, user, request_id, *, edit=False):
    OBJECT_REGISTRY.require("APPROVAL").validate_id(request_id)
    request = db.get(ApprovalRequest, request_id, populate_existing=True)
    if request is None:
        raise ObjectNotFoundError("Заявка не найдена")
    for row in request.vehicles_snapshot:
        vehicle_access(db, user, row["vehicle_id"], edit=edit)
    return request


def attempt(db, user, key, action, target_id, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old = db.get(ApprovalAttempt, str(key), populate_existing=True)
    if old:
        if old.principal_id != user.id:
            raise ObjectAuthorizationError("Команда принадлежит другому пользователю")
        if (old.action, old.target_id, old.fingerprint) != (action, target_id, digest):
            raise ObjectVersionConflictError("Ключ команды уже связан с другими данными")
    return old, digest


def receipt(db, user, key, action, target, digest, result):
    row = db.get(ApprovalAttempt, str(key))
    if row is None:
        row = ApprovalAttempt(
            id=str(key), action=action, target_id=target, principal_id=user.id, fingerprint=digest, plan={}
        )
        db.add(row)
    row.result = result
    db.commit()
    return result


def calibration_document(db, user, vehicle_id, document_id, *, edit=False, lock=False):
    document = document_access(db, user, "VEH", vehicle_id, document_id, edit=edit)
    if lock:
        lock_kind(db, document.kind_ref)
        document = db.scalar(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document.parent_type != "VEH" or document.parent_id != vehicle_id:
            raise ObjectVersionConflictError("Привязка документа изменилась")
    kind = db.get(DocumentKind, document.kind_ref)
    if kind is None or kind.name.strip().casefold() != "тарировка" or document.archive_state != "Неархивный":
        raise ValueError("Выберите неархивный документ типа «Тарировка» этой машины")
    originals = revision_files(db, document.id, document.current_revision_id)
    if not originals or any(db.get(DocumentFileCorrection, f.id) for f in originals):
        raise ValueError("У тарировки нет актуального комплекта правильных файлов")
    return document, originals


def _confirmed_proof(vehicle, document):
    return bool(
        vehicle.calibration_m3
        and vehicle.calibration_confirmed_at
        and vehicle.calibration_confirmed_by
        and vehicle.calibration_document_id == document.id
        and vehicle.calibration_revision_id == document.current_revision_id
        and vehicle.calibration_plate_number == vehicle.plate_number
    )


def customer_state(db, vehicle):
    if vehicle.calibration_m3 is None:
        return {"state": None, "request_id": None, "decision_id": None}
    rows = list(
        db.scalars(
            select(ApprovalDecision)
            .join(ApprovalRequest, ApprovalRequest.id == ApprovalDecision.request_id)
            .where(
                ApprovalDecision.vehicle_id == vehicle.id,
                ApprovalDecision.plate_number == vehicle.plate_number,
                ApprovalDecision.volume_m3 == vehicle.calibration_m3,
            )
            .order_by(
                ApprovalDecision.decided_at.desc().nullslast(),
                ApprovalRequest.created_at.desc(),
                ApprovalDecision.id.desc(),
            )
        )
    )
    terminal = next((r for r in rows if r.state != "PENDING"), None)
    row = terminal or (rows[0] if rows else None)
    return {
        "state": row.state if row else None,
        "request_id": row.request_id if row else None,
        "decision_id": row.id if row else None,
    }


def readiness(db, user, vehicle_id):
    vehicle = vehicle_access(db, user, vehicle_id)
    kind = db.get(VehicleType, vehicle.type_id, populate_existing=True) if vehicle.type_id else None
    reasons, originals, document = [], [], None
    if kind is None or kind.status != "CONFIRMED":
        reasons.append("Выберите TYPE с полным описанием")
    if vehicle.accounting_state != "Действующая":
        reasons.append("Машина находится в архиве")
    if kind and kind.requires_calibration is not True:
        reasons.append("Этот TYPE не требует тарировки для данного процесса")
    if vehicle.calibration_document_id:
        try:
            document, originals = calibration_document(db, user, vehicle.id, vehicle.calibration_document_id)
            if not _confirmed_proof(vehicle, document):
                reasons.append("Подтвердите текущую редакцию тарировки для актуального госномера и объёма")
            if not all(files.verify_remote(f) for f in originals):
                reasons.append("Оригинал тарировки недоступен или не прошёл проверку целостности")
        except (ValueError, ObjectNotFoundError) as error:
            reasons.append(str(error))
    else:
        reasons.append("Объём по документу тарировки ещё не подтверждён оператором")
    return {
        "vehicle_id": vehicle.id,
        "name": vehicle.name,
        "plate_number": vehicle.plate_number,
        "volume_m3": vehicle.calibration_m3,
        "vehicle_version": vehicle.version,
        "document_id": document.id if document else None,
        "document_version": document.version if document else None,
        "revision_id": document.current_revision_id if document else None,
        "file_ids": [f.id for f in originals],
        "confirmed_at": vehicle.calibration_confirmed_at.isoformat()
        if vehicle.calibration_confirmed_at
        else None,
        "confirmed_by": vehicle.calibration_confirmed_by,
        "calibration_required": kind.requires_calibration if kind else None,
        "letter_eligible": not reasons,
        "reasons": reasons,
        "customer": customer_state(db, vehicle),
        "work_readiness_complete": False,
        "work_readiness_notice": "Это допуск к письму, не полный допуск к работе. Проверки СТС/Wialon и заказчика учитываются отдельно.",
    }


def confirm_calibration(db, user, session_hash, vehicle_id, command):
    session_access(db, user, session_hash)
    vehicle_access(db, user, vehicle_id, edit=True)
    payload = dict(command)
    key = payload.pop("command_id")
    with attempt_lease(db, "approval-" + key):
        old, digest = attempt(db, user, key, "calibration", vehicle_id, payload)
        if old and old.result:
            return old.result
        document, originals = calibration_document(
            db, user, vehicle_id, payload["document_id"], edit=True, lock=True
        )
        from app.arm2.vehicle_store import VEHICLE_ADAPTER

        vehicle = VEHICLE_ADAPTER.lock_record(db, vehicle_id)
        if (
            vehicle.version != payload["vehicle_version"]
            or document.version != payload["document_version"]
            or document.current_revision_id != payload["revision_id"]
            or vehicle.plate_number != payload["plate_number"]
        ):
            raise ObjectVersionConflictError(
                "Машина или актуальная редакция изменились; откройте карточку заново"
            )
        value = payload["volume_m3"]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Введите положительный конечный объём кузова в м³")
        for original in originals:
            path = files.download(original)
            path.unlink(missing_ok=True)
        session_access(db, user, session_hash)
        vehicle_access(db, user, vehicle_id, edit=True)
        unchanged = _confirmed_proof(vehicle, document) and vehicle.calibration_m3 == float(value)
        saved = update_object(
            db,
            object_type="VEH",
            object_id=vehicle.id,
            expected_version=vehicle.version,
            values={},
            partial=True,
            principal=user,
            context=context(user, key),
            server_values={}
            if unchanged
            else {
                "calibration_m3": float(value),
                "calibration_confirmed_at": instant(),
                "calibration_confirmed_by": user.id,
                "calibration_document_id": document.id,
                "calibration_revision_id": document.current_revision_id,
                "calibration_plate_number": vehicle.plate_number,
            },
        )
        return receipt(
            db,
            user,
            key,
            "calibration",
            vehicle.id,
            digest,
            {
                "vehicle_id": vehicle.id,
                "version": saved.record.version,
                "no_op": not saved.changed,
                "volume_m3": saved.record.calibration_m3,
                "event_ids": [saved.event.event_id] if saved.event else [],
            },
        )


def describe(db, user, request_id):
    from app.arm2.document_file_store import public_file

    row = access(db, user, request_id)
    decisions = db.scalars(
        select(ApprovalDecision).where(ApprovalDecision.request_id == row.id).order_by(ApprovalDecision.id)
    ).all()
    file_ids = [row.letter_file_id, row.package_file_id, *row.response_file_ids]
    originals = {
        original.id: original
        for original in db.scalars(
            select(DocumentFile).where(DocumentFile.process_id == row.id, DocumentFile.id.in_(file_ids))
        )
    }
    if set(file_ids) != set(originals):
        raise files.StorageUnavailable("Метаданные оригиналов заявки неполны")
    return {
        **{key: getattr(row, key) for key in OBJECT_REGISTRY.require("APPROVAL").history_fields},
        "id": row.id,
        "files": [public_file(originals[file_id], index) for index, file_id in enumerate(file_ids)],
        "decisions": [
            {
                **{key: getattr(d, key) for key in OBJECT_REGISTRY.require("APPROVAL-ITEM").history_fields},
                "id": d.id,
            }
            for d in decisions
        ],
    }


def selected_snapshots(db, user, selection, *, lock=False):
    from app.arm2.vehicle_store import VEHICLE_ADAPTER

    snapshots, originals = [], []
    if not 1 <= len(selection) <= 100 or len({v["vehicle_id"] for v in selection}) != len(selection):
        raise ValueError("Выберите от 1 до 100 разных машин")
    # Lock kind -> document -> vehicle, matching B3 and confirmation.
    for item in sorted(selection, key=lambda row: row["vehicle_id"]):
        vehicle = vehicle_access(db, user, item["vehicle_id"], edit=True)
        if lock and vehicle.calibration_document_id:
            calibration_document(db, user, vehicle.id, vehicle.calibration_document_id, edit=True, lock=True)
            VEHICLE_ADAPTER.lock_record(db, vehicle.id)
            if vehicle.type_id:
                db.scalar(
                    select(VehicleType)
                    .where(VehicleType.id == vehicle.type_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
        ready = readiness(db, user, vehicle.id)
        if ready["vehicle_version"] != item["vehicle_version"]:
            raise ObjectVersionConflictError("Машина изменена; обновите список кандидатов")
        if not ready["letter_eligible"]:
            raise ValueError(f"{vehicle.name}: " + "; ".join(ready["reasons"]))
        snapshots.append(
            {
                key: ready[key]
                for key in [
                    "vehicle_id",
                    "name",
                    "plate_number",
                    "volume_m3",
                    "vehicle_version",
                    "document_id",
                    "document_version",
                    "revision_id",
                    "file_ids",
                ]
            }
        )
        originals.extend(revision_files(db, ready["document_id"], ready["revision_id"]))
    return snapshots, originals


def publish_files(db, request_id, plans):
    for plan in plans:
        db.add(
            DocumentFile(
                process_id=request_id,
                document_id=None,
                **{key: value for key, value in plan.items() if key != "token"},
            )
        )
    db.flush()


def issue_letter(db, user, session_hash, command):
    from app.arm2.approval_letter import render_letter, render_package

    payload = dict(command)
    key = payload.pop("command_id")
    numbers = [payload["contract_number_1"].strip(), payload["contract_number_2"].strip()]
    if any(not number or len(number) > 160 or any(ord(c) < 32 for c in number) for number in numbers):
        raise ValueError("Укажите оба номера контрактов (до 160 символов)")
    session_access(db, user, session_hash)
    with attempt_lease(db, "approval-" + key):
        old, digest = attempt(db, user, key, "letter", "", payload)
        if old and old.result:
            access(db, user, old.result["request_id"], edit=True)
            return old.result
        snapshots, originals = selected_snapshots(db, user, payload["vehicles"])
        if old is None:
            letter_path, package_path = files.fresh_path(), files.fresh_path()
            plans = []
            try:
                render_letter(letter_path, numbers, snapshots)
                letter = files.prepare_original(
                    letter_path,
                    file_id=next_id(db, "FILE"),
                    namespace=key,
                    name="Согласование техники",
                    original_upload_name="Согласование техники.pdf",
                )
                plans.append(letter)
                render_package(package_path, letter_path, letter["stored_name"], snapshots, originals)
                plans.append(
                    files.prepare_original(
                        package_path,
                        file_id=next_id(db, "FILE"),
                        namespace=key,
                        name="Комплект согласования",
                        original_upload_name="Комплект согласования.zip",
                        generated_bundle=True,
                    )
                )
                staging_quota(db, user.id, sum(p["size"] for p in plans), extra_count=len(plans))
                old = ApprovalAttempt(
                    id=key,
                    action="letter",
                    target_id="",
                    principal_id=user.id,
                    fingerprint=digest,
                    plan={"files": plans, "snapshots": snapshots, "created_at": instant().isoformat()},
                )
                db.add(old)
                db.commit()  # Private retry metadata only; no published request or decisions.
            except Exception:
                for plan in plans:
                    files.private_path(plan["token"]).unlink(missing_ok=True)
                raise
            finally:
                letter_path.unlink(missing_ok=True)
                package_path.unlink(missing_ok=True)
        if old.created_at + timedelta(seconds=files.STAGE_SECONDS) < instant():
            raise ObjectVersionConflictError("Срок команды выпуска истёк; повторно выберите машины")
        plan = old.plan
        if snapshots != plan["snapshots"]:
            raise ObjectVersionConflictError("Характеристики изменились; выпустите новую команду")
        db.commit()
        for original in plan["files"]:
            session_access(db, user, session_hash)
            selected_snapshots(db, user, payload["vehicles"])
            db.commit()
            files.ensure_original(original)
        session_access(db, user, session_hash)
        current, _ = selected_snapshots(db, user, payload["vehicles"], lock=True)
        if current != plan["snapshots"]:
            raise ObjectVersionConflictError("Источник изменился; заявка не создана")
        from datetime import datetime

        request = create_object(
            db,
            object_type="APPROVAL",
            principal=user,
            context=context(user, key),
            values={
                "name": "Согласование техники",
                "contract_number_1": numbers[0],
                "contract_number_2": numbers[1],
                "vehicles_snapshot": current,
                "letter_file_id": plan["files"][0]["id"],
                "package_file_id": plan["files"][1]["id"],
                "response_file_ids": [],
                "last_response_at": None,
                "created_at": datetime.fromisoformat(plan["created_at"]),
                "author_id": user.id,
            },
        )
        publish_files(db, request.record.id, plan["files"])
        events = [request.event.event_id]
        for vehicle in current:
            decision = create_object(
                db,
                object_type="APPROVAL-ITEM",
                principal=user,
                context=context(user, key),
                values={
                    "request_id": request.record.id,
                    "vehicle_id": vehicle["vehicle_id"],
                    "plate_number": vehicle["plate_number"],
                    "volume_m3": vehicle["volume_m3"],
                    "state": "PENDING",
                    "response_file_id": None,
                    "decided_at": None,
                    "decided_by": None,
                },
            )
            events.append(decision.event.event_id)
        return receipt(
            db, user, key, "letter", "", digest, {"request_id": request.record.id, "event_ids": events}
        )


def stage_response(db, user, session_hash, request_id, key, name, data):
    session_access(db, user, session_hash)
    access(db, user, request_id, edit=True)
    name = files.original_name(name)
    if not 0 < len(data) <= MAX_RESPONSE_BYTES:
        raise ValueError("Ответ должен быть не больше 2 МиБ")
    payload = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with attempt_lease(db, "approval-" + key):
        old, digest = attempt(db, user, key, "response-stage", request_id, payload)
        if old:
            response_stage(db, user, session_hash, request_id, key)
            return {
                "stage_id": key,
                "name": name,
                "media_type": old.plan["file"]["media_type"],
                "stored_name": old.plan["file"]["stored_name"],
            }
        staging_quota(db, user.id, len(data))
        local = files.fresh_path()
        try:
            local.write_bytes(data)
            media, extension = files.content_type(local)
            if extension not in {"pdf", "png", "jpg"}:
                raise ValueError("Ответ должен быть PDF, JPG или PNG")
            plan = files.prepare_original(
                local,
                file_id=next_id(db, "FILE"),
                namespace=key,
                name="Ответ заказчика",
                original_upload_name=name,
            )
            db.add(
                ApprovalAttempt(
                    id=key,
                    action="response-stage",
                    target_id=request_id,
                    principal_id=user.id,
                    fingerprint=digest,
                    plan={"file": plan, "session_hash": session_hash},
                )
            )
            db.commit()
        finally:
            local.unlink(missing_ok=True)
        return {"stage_id": key, "name": name, "media_type": media, "stored_name": plan["stored_name"]}


def response_stage(db, user, session_hash, request_id, stage_id):
    access(db, user, request_id, edit=True)
    row = db.get(ApprovalAttempt, stage_id, populate_existing=True)
    if row is None or row.action != "response-stage" or row.target_id != request_id:
        raise ObjectNotFoundError("Ответ не найден")
    if row.principal_id != user.id or row.plan["session_hash"] != session_hash:
        raise ObjectAuthorizationError("Ответ принадлежит другой сессии")
    if row.created_at + timedelta(seconds=files.STAGE_SECONDS) < instant():
        raise ObjectVersionConflictError("Срок временного ответа истёк; приложите его заново")
    return row


def apply_response(db, user, session_hash, request_id, command):
    payload = dict(command)
    key = payload.pop("command_id")
    session_access(db, user, session_hash)
    access(db, user, request_id, edit=True)
    with attempt_lease(db, "approval-" + key):
        old, digest = attempt(db, user, key, "response", request_id, payload)
        if old and old.result:
            return old.result
        staged = response_stage(db, user, session_hash, request_id, payload["stage_id"])
        plan = staged.plan["file"]
        request = access(db, user, request_id, edit=True)
        if request.version != payload["expected_version"]:
            raise ObjectVersionConflictError("Заявка изменена; откройте её заново")
        existing = db.scalar(
            select(DocumentFile).where(
                DocumentFile.process_id == request_id,
                DocumentFile.sha256 == plan["sha256"],
                DocumentFile.id.in_(request.response_file_ids),
            )
        )
        db.commit()
        if existing:
            if not files.verify_remote(existing):
                raise files.StorageUnavailable("Ранее сохранённый ответ недоступен; решения не изменены")
        else:
            files.ensure_original(plan)
        session_access(db, user, session_hash)
        access(db, user, request_id, edit=True)
        request = REQUEST_ADAPTER.lock_record(db, request_id)
        if request.version != payload["expected_version"]:
            raise ObjectVersionConflictError("Заявка изменена; откройте её заново")
        choices = payload["decisions"]
        if not choices or len({d["vehicle_id"] for d in choices}) != len(choices):
            raise ValueError("Укажите явные решения по выбранным машинам")
        # Same bytes within this request are the same evidence, regardless of upload attempt.
        evidence = db.scalar(
            select(DocumentFile).where(
                DocumentFile.process_id == request_id,
                DocumentFile.sha256 == plan["sha256"],
                DocumentFile.id.in_(request.response_file_ids),
            )
        )
        evidence_id = evidence.id if evidence else plan["id"]
        changed, events = [], []
        for choice in sorted(choices, key=lambda d: d["vehicle_id"]):
            row = db.scalar(
                select(ApprovalDecision)
                .where(
                    ApprovalDecision.request_id == request_id,
                    ApprovalDecision.vehicle_id == choice["vehicle_id"],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None or choice["state"] not in {"APPROVED", "REJECTED"}:
                raise ValueError("Решение не соответствует машине заявки")
            if row.version != choice["expected_version"]:
                raise ObjectVersionConflictError("Решение изменено; откройте заявку заново")
            if (row.state, row.response_file_id) != (choice["state"], evidence_id):
                changed.append((row, choice))
        if changed and evidence is None:
            publish_files(db, request_id, [plan])
        for row, choice in changed:
            result = update_object(
                db,
                object_type="APPROVAL-ITEM",
                object_id=row.id,
                expected_version=row.version,
                values={
                    "state": choice["state"],
                    "response_file_id": evidence_id,
                    "decided_at": instant(),
                    "decided_by": user.id,
                },
                partial=True,
                principal=user,
                context=context(user, key),
            )
            events.append(result.event.event_id)
        if changed:
            result = update_object(
                db,
                object_type="APPROVAL",
                object_id=request.id,
                expected_version=request.version,
                values={
                    "last_response_at": instant(),
                    "response_file_ids": list(dict.fromkeys([*request.response_file_ids, evidence_id])),
                },
                partial=True,
                principal=user,
                context=context(user, key),
            )
            if result.event:
                events.append(result.event.event_id)
        staged.result = {"consumed_by": key}
        return receipt(
            db,
            user,
            key,
            "response",
            request_id,
            digest,
            {"request_id": request_id, "version": request.version, "event_ids": events, "no_op": not changed},
        )
