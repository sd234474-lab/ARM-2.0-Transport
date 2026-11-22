"""Private durable B4 workspaces; one confirmed VEH per transaction."""

import base64
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.arm2.bulk_diff import CLEARABLE, owner_access, preview, state, validate_value
from app.arm2.bulk_models import BulkCleanup, BulkReceipt, BulkSession
from app.arm2.bulk_parse import MAX_BYTES, MAX_CELL, MAX_COLS, MAX_ROWS
from app.arm2.bulk_tables import (
    LABELS,
    export_vehicles,
    fingerprint,
    originals,
    parse_table,
    permissions,
    suggest,
    xlsx,
)
from app.arm2.history_models import HistoryEvent
from app.arm2.http_limits import bounded_route
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
    create_object,
    update_object,
)
from app.arm2.vehicle_models import Vehicle
from app.auth import AdminUser, CurrentUser, Db
from app.models import User

BoundedRoute = bounded_route(MAX_BYTES, "Запрос больше 5 МиБ")

router = APIRouter(prefix="/bulk", route_class=BoundedRoute)
Cell = Annotated[str, Field(max_length=MAX_CELL)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportQuery(Command):
    q: Annotated[str, Field(max_length=200, pattern=r"^[^\x00]*$")] = ""
    type_id: Annotated[str | None, Field(pattern=r"^TYPE-[0-9]{6,}$")] = None
    owner_kind: Literal["CP", "ORG", "NONE"] | None = None
    owner_id: Annotated[str | None, Field(pattern=r"^(CP|ORG)-[0-9]{6,}$")] = None
    state: Literal["Действующая", "Архивная"] | None = None
    sort: Literal["name", "plate_number", "type", "owner", "accounting_state"] = "name"
    direction: Literal["asc", "desc"] = "asc"


class Draft(Command):
    revision: Annotated[StrictInt, Field(gt=0)]
    headers: Annotated[list[Cell], Field(min_length=1, max_length=MAX_COLS)]
    rows: Annotated[list[Annotated[list[Cell], Field(max_length=MAX_COLS)]], Field(max_length=MAX_ROWS)]
    mapping: Annotated[list[str], Field(max_length=MAX_COLS)]
    excluded: list[StrictInt] = []
    clears: dict[str, list[str]] = {}
    decisions: dict[str, dict[str, StrictInt]] = {}
    confirmations: dict[str, dict[str, StrictBool]] = {}

    @model_validator(mode="after")
    def validate_table(self):
        width = len(self.headers)
        if len(self.mapping) != width or any(len(r) != width for r in self.rows):
            raise ValueError("Размеры таблицы и соответствий различаются")
        fields = [f for f in self.mapping if f]
        if any(f not in LABELS or f == "owner" for f in fields) or len(set(fields)) != len(fields):
            raise ValueError("Неизвестный либо повторно назначенный реквизит")
        if any(i < 0 or i >= len(self.rows) for i in self.excluded):
            raise ValueError("Строка исключения не найдена")
        for index, clear in self.clears.items():
            if not index.isdigit() or int(index) >= len(self.rows) or set(clear) - CLEARABLE:
                raise ValueError("Недопустимая команда CLEAR")
        for row in [self.headers, *self.rows]:
            if any("\x00" in cell or cell.lstrip().startswith(("=", "+", "@")) for cell in row):
                raise ValueError("Формулы и NUL запрещены")
        return self


class Apply(Command):
    command_id: UUID
    preview_id: UUID
    row_key: Annotated[str, Field(max_length=100)]
    fields: dict[str, StrictInt]
    confirm_owner: StrictBool = False
    confirm_plate: StrictBool = False


class Finish(Command):
    revision: Annotated[StrictInt, Field(gt=0)]
    cancel: StrictBool = False


class Cleanup(Command):
    confirm_session: UUID


def workspace(db, user, session_id):
    user = permissions(db, user, edit=True)
    record = db.scalar(select(BulkSession).where(BulkSession.id == str(session_id)).with_for_update()
                       .execution_options(populate_existing=True))
    if record is None:
        raise HTTPException(404, "Рабочий набор не найден")
    if record.user_id != user.id:
        raise HTTPException(403, "Продолжать загрузку может только её автор")
    return record


def receipts(db, user, session_id):
    result = []
    records = db.scalars(select(BulkReceipt).where(BulkReceipt.session_id == str(session_id), BulkReceipt.user_id == user.id)
                         .order_by(BulkReceipt.at, BulkReceipt.command_id)).all()
    for record in records:
        vehicle = db.get(Vehicle, record.vehicle_id, populate_existing=True)
        event = db.get(HistoryEvent, record.event_id) if record.event_id else None
        verified = vehicle is not None and vehicle.version >= record.version_after
        if record.event_id:
            verified &= (event is not None and event.correlation_id == record.command_id and event.object_type == "VEH"
                         and event.object_id == record.vehicle_id and event.version_after == record.version_after
                         and event.actor_id == str(user.id) and event.source == "arm2-table")
        result.append({"command_id": record.command_id, "row_key": record.row_key, "vehicle_id": record.vehicle_id,
                       "event_id": record.event_id, "version_after": record.version_after, "fields": record.fields,
                       "at": record.at.isoformat(), "status": "UNKNOWN" if not verified else "SAVED" if event else "SKIP"})
    return result


def checked(db, user, record):
    results = receipts(db, user, record.id)
    plans = preview(db, user, record.payload)
    preview_id = str(uuid4())
    reset = record.payload.get("plans") is not None and fingerprint(record.payload["plans"]) != fingerprint(plans)
    record.payload = {**record.payload, "plans": plans, "preview_id": preview_id,
                      **({"decisions": {}, "confirmations": {}} if reset else {})}
    db.commit()
    return {"id": record.id, "source": record.source, "revision": record.revision,
            **{k: record.payload.get(k) for k in ("headers", "rows", "mapping", "excluded", "clears", "decisions", "confirmations", "warning", "failures")},
            "plans": plans, "preview_id": preview_id, "results": results,
            "blocked": any(r["status"] == "UNKNOWN" for r in results), "approval_reset": reset}


@router.get("/vehicles/export")
def export(db: Db, user: CurrentUser, filters: Annotated[ExportQuery, Query()]):
    return Response(export_vehicles(db, user, filters.model_dump()), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="vehicles.xlsx"'})


@router.post("/sessions", status_code=201)
async def upload(request: Request, db: Db, user: CurrentUser,
                 filename: Annotated[str, Query(max_length=200)] = "paste.tsv",
                 sheet: Annotated[str | None, Query(max_length=100)] = None):
    user = permissions(db, user, edit=True)
    table = await run_in_threadpool(parse_table, await request.body(), filename, sheet)
    if "sheets" in table:
        return table
    original, warning = originals(db, table.pop("manifest"))
    source_sheet = table.pop("source_sheet", None)
    mapping = suggest(table["headers"])
    # A repeated familiar heading is still ambiguous; request a manual mapping.
    mapping = [v if not v or mapping.count(v) == 1 else "" for v in mapping]
    draft = Draft(revision=1, mapping=mapping, **table)
    filename = filename.replace("\\", "/").split("/")[-1]
    source = filename[:164] + " · " + source_sheet[:31] if source_sheet else filename
    record = BulkSession(id=str(uuid4()), user_id=user.id, source=source,
                         revision=1, payload={**draft.model_dump(exclude={"revision"}), "originals": original, "warning": warning})
    db.add(record)
    db.flush()
    return checked(db, user, record)


@router.get("/sessions")
def sessions(db: Db, user: CurrentUser):
    permissions(db, user, edit=True)
    return [{"id": s.id, "source": s.source, "created_at": s.created_at, "revision": s.revision}
            for s in db.scalars(select(BulkSession).where(BulkSession.user_id == user.id).order_by(BulkSession.created_at.desc()))]


@router.get("/sessions/{session_id}")
def reopen(session_id: UUID, db: Db, user: CurrentUser):
    return checked(db, user, workspace(db, user, session_id))


@router.put("/sessions/{session_id}")
def edit(session_id: UUID, body: Draft, db: Db, user: CurrentUser):
    record = workspace(db, user, session_id)
    if record.revision != body.revision:
        raise HTTPException(409, "Черновик изменён в другом окне; откройте актуальный набор")
    changes = body.model_dump(exclude={"revision"})
    data_changed = any(changes[k] != record.payload.get(k, {} if k == "clears" else []) for k in ("headers", "rows", "mapping", "excluded", "clears"))
    record.payload = {**record.payload, **changes, **({"decisions": {}, "confirmations": {}} if data_changed else {})}
    record.revision += 1
    return checked(db, user, record)


@router.post("/sessions/{session_id}/apply")
def apply(session_id: UUID, body: Apply, db: Db, user: CurrentUser):
    record = workspace(db, user, session_id)
    user = permissions(db, user, edit=True)  # Repeat after any workspace row-lock wait.
    command_id = str(body.command_id)
    signature = fingerprint(body.model_dump(mode="json"))
    old = db.get(BulkReceipt, command_id)
    if old:
        if old.session_id != record.id or old.user_id != user.id or old.fingerprint != signature:
            raise HTTPException(409, "Идентификатор команды уже использован с другим содержимым")
        result = next(r for r in receipts(db, user, record.id) if r["command_id"] == command_id)
        if result["status"] == "UNKNOWN":
            raise HTTPException(409, "Результат не определён; повторная запись заблокирована до проверки")
        return result
    if any(r["status"] == "UNKNOWN" for r in receipts(db, user, record.id)):
        raise HTTPException(409, "Сначала проверьте неопределённые результаты")
    if command_id in record.payload.get("failures", {}):
        raise HTTPException(409, "Эта попытка уже отклонена. Повторите сравнение и явно подтвердите новую попытку")
    if record.payload.get("preview_id") != str(body.preview_id):
        raise HTTPException(409, "Сравнение устарело; проверьте таблицу заново")
    plan = next((p for p in record.payload.get("plans", []) if p["key"] == body.row_key), None)
    if plan is None or plan["errors"] or not body.fields:
        raise HTTPException(422, "Выберите проверенные изменения одной VEH")
    try:
        with db.begin_nested():
            vehicle = db.scalar(select(Vehicle).where(Vehicle.id == plan["vehicle_id"]).with_for_update()
                                .execution_options(populate_existing=True)) if plan["vehicle_id"] else None
            user = permissions(db, user, edit=True)
            if plan["vehicle_id"] and (vehicle is None or vehicle.version != plan["version"]):
                raise ObjectVersionConflictError("Версия VEH изменилась; повторите сравнение")
            chosen = {}
            for field, index in body.fields.items():
                options = plan["fields"].get(field, [])
                if index < 0 or index >= len(options) or options[index]["error"]:
                    raise ValueError("Нельзя принять непроверенное значение реквизита")
                chosen[field] = options[index]["value"]
            before = state(vehicle)
            for field, value in chosen.items():
                validate_value(db, user, field, value, before,
                               chosen.get("plate_number") or before["plate_number"],
                               chosen.get("type_id", before["type_id"]))
            if "owner" in chosen:
                owner_access(user, before["owner"], chosen["owner"])
                if before["owner"] and before["owner"] != chosen["owner"] and not body.confirm_owner:
                    raise ValueError("Отдельно подтвердите прежнего → нового операционного владельца")
            if vehicle and "plate_number" in chosen and chosen["plate_number"] != vehicle.plate_number:
                if not body.confirm_plate or not any(record.payload["mapping"][i] == "id" and record.payload["rows"][s-2][i].strip() == vehicle.id
                                                     for s in plan["sources"] for i in range(len(record.payload["mapping"]))):
                    raise ValueError("Смена номера требует однозначного VEH-ID и отдельного подтверждения")
            values = {f: v for f, v in chosen.items() if f != "owner"}
            if "owner" in chosen:
                owner = chosen["owner"]
                values.update(owner_cp_id=owner[1] if owner and owner[0] == "CP" else None,
                              owner_org_id=owner[1] if owner and owner[0] == "ORG" else None)
            context = SaveContext("HUMAN", str(user.id), "arm2-table", command_id)
            if vehicle:
                saved = update_object(db, object_type="VEH", object_id=vehicle.id, expected_version=plan["version"],
                                      values=values, partial=True, principal=user, context=context)
                # Historic non-normalized fields must not change incidentally.
                if any(before[f] != state(saved.record)[f] for f in before if f not in chosen):
                    raise ValueError("Невыбранные реквизиты требуют нормализации; сначала явно исправьте карточку")
            else:
                if "plate_number" not in chosen:
                    raise ValueError("Для CREATE явно примите госномер")
                defaults = dict(name=chosen.get("name") or chosen["plate_number"], plate_number=chosen["plate_number"],
                                type_id=None, model=None, wialon_unit_id=None, owner_cp_id=None, owner_org_id=None,
                                accounting_state="Действующая")
                defaults.update(values)
                saved = create_object(db, object_type="VEH", values=defaults, principal=user, context=context)
            receipt = BulkReceipt(command_id=command_id, session_id=record.id, user_id=user.id, source=record.source,
                                  fingerprint=signature, row_key=body.row_key, vehicle_id=saved.record.id,
                                  event_id=saved.event.event_id if saved.event else None,
                                  version_after=saved.record.version, fields=sorted(chosen))
            db.add(receipt)
            db.flush()
        db.commit()  # VEH + HIST + VERSION + durable receipt: one outer commit.
    except (ValueError, ObjectVersionConflictError, IntegrityError, ObjectAuthorizationError) as error:
        status = 409 if isinstance(error, (ObjectVersionConflictError, IntegrityError)) else 403 if isinstance(error, ObjectAuthorizationError) else 422
        message = "Госномер уже занят; повторите сравнение" if isinstance(error, IntegrityError) else str(error)
        failures = {**record.payload.get("failures", {}), command_id: {"row_key": body.row_key, "status": status, "message": message}}
        record.payload = {**record.payload, "failures": failures}
        db.commit()
        raise HTTPException(status, message) from None
    return next(r for r in receipts(db, user, record.id) if r["command_id"] == command_id)


@router.post("/sessions/{session_id}/finish")
def finish(session_id: UUID, body: Finish, db: Db, user: CurrentUser):
    record = workspace(db, user, session_id)
    if record.revision != body.revision:
        raise HTTPException(409, "Черновик изменён; сначала откройте актуальный набор")
    outcomes = receipts(db, user, record.id)
    if any(r["status"] == "UNKNOWN" for r in outcomes):
        raise HTTPException(409, "Неопределённый результат: завершение заблокировано до проверки")
    if (outcomes or not body.cancel) and record.payload.get("report_revision") != record.revision:
        raise HTTPException(409, "Сначала скачайте отчёт об отказах в Excel; временный набор сохранён")
    plans = preview(db, user, record.payload)
    if (outcomes or not body.cancel) and record.payload.get("report_fingerprint") != fingerprint({"plans": plans, "results": outcomes}):
        raise HTTPException(409, "Отчёт устарел после изменений; скачайте его заново. Набор сохранён")
    report = rejection_report(db, user, record, plans)
    db.delete(record)
    db.commit()
    return {"session_id": str(session_id), "cancelled": body.cancel, "results": outcomes,
            "report": base64.b64encode(report).decode(), "message": "Временный набор удалён. Уже сохранённые VEH и история не откатываются"}


def rejection_report(db, user, record, plans=None):
    plans = preview(db, user, record.payload) if plans is None else plans
    rows = []
    for p in plans:
        for source in p["sources"]:
            reasons = [*p["errors"], *[o["error"] or "Не принято: " + LABELS[f] for f, opts in p["fields"].items() for o in opts if source in o["sources"] and not o.get("no_change")]]
            if reasons:
                rows.append([source, *record.payload["rows"][source-2], "; ".join(dict.fromkeys(reasons))])
    for index in record.payload.get("excluded", []):
        rows.append([index+2, *record.payload["rows"][index], "Исключено оператором"])
    return xlsx(["Строка", *record.payload["headers"], "Результат / причина"], rows)


@router.get("/sessions/{session_id}/report")
def download_report(session_id: UUID, db: Db, user: CurrentUser):
    record = workspace(db, user, session_id)
    plans, outcomes = preview(db, user, record.payload), receipts(db, user, record.id)
    report = rejection_report(db, user, record, plans)
    record.payload = {**record.payload, "report_revision": record.revision,
                      "report_fingerprint": fingerprint({"plans": plans, "results": outcomes})}
    db.commit()
    return Response(report, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="vehicle-results.xlsx"'})


@router.get("/protocols")
def protocols(db: Db, user: CurrentUser):
    permissions(db, user)
    ids = db.execute(select(BulkReceipt.session_id, BulkReceipt.source).where(BulkReceipt.user_id == user.id)
                     .group_by(BulkReceipt.session_id, BulkReceipt.source).limit(100)).all()
    return [{"id": sid, "source": source, "author": user.id, "results": receipts(db, user, sid)} for sid, source in ids]


@router.get("/cleanup")
def abandoned(db: Db, user: AdminUser):
    permissions(db, user)
    return [{"id": s.id, "author": s.user_id, "source": s.source, "created_at": s.created_at}
            for s in db.scalars(select(BulkSession).order_by(BulkSession.created_at))]


@router.post("/cleanup/{session_id}")
def cleanup(session_id: UUID, body: Cleanup, db: Db, user: AdminUser):
    record = db.scalar(select(BulkSession).where(BulkSession.id == str(session_id)).with_for_update())
    actor = permissions(db, user)
    if actor.role != "ADMIN_OPERATOR":
        raise HTTPException(403, "Требуется действующий администратор")
    if str(body.confirm_session) != str(session_id):
        raise HTTPException(422, "Подтвердите ID очищаемого набора")
    if record is None:
        raise HTTPException(404, "Набор не найден")
    # Verify durable subject outcomes even though admin cannot continue the workspace.
    author = db.get(User, record.user_id)
    if any(r["status"] == "UNKNOWN" for r in receipts(db, author, record.id)):
        raise HTTPException(409, "Сначала проверьте неопределённые результаты")
    db.add(BulkCleanup(id=str(uuid4()), session_id=record.id, user_id=user.id))
    db.delete(record)
    db.commit()
    return {"cleaned": str(session_id), "message": "Удалён только временный набор; принятые VEH, HIST и протокол сохранены"}
