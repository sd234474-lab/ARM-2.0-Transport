"""Bounded subject commands over the shared ARM2 access, errors and B3 file transport."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictFloat, StrictInt
from sqlalchemy import select

from app.arm2 import approval_store as store
from app.arm2 import document_file_storage as files
from app.arm2.approval_models import ApprovalRequest
from app.arm2.document_file_models import DocumentFile
from app.arm2.document_file_routes import protected_response, translated
from app.arm2.file_read_guard import protected_read
from app.arm2.http_limits import bounded_route
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError
from app.arm2.schemas import RawReference
from app.arm2.vehicle_models import Vehicle
from app.auth import CurrentUser, Db, check_permission
from app.permissions import Permission
from app.schemas import StrictModel

router = APIRouter(route_class=bounded_route(store.MAX_RESPONSE_BYTES, "Ответ превышает 2 МиБ"))
PAGE_SIZE = 100  # Same contract as the shared ARM2 Pagination component.


class Confirmation(StrictModel):
    command_id: UUID
    vehicle_version: StrictInt = Field(gt=0)
    document_id: RawReference
    document_version: StrictInt = Field(gt=0)
    revision_id: RawReference
    plate_number: str
    volume_m3: StrictFloat = Field(gt=0, allow_inf_nan=False)


class Selection(StrictModel):
    vehicle_id: RawReference
    vehicle_version: StrictInt = Field(gt=0)


class Letter(StrictModel):
    command_id: UUID
    contract_number_1: str = Field(min_length=1, max_length=160)
    contract_number_2: str = Field(min_length=1, max_length=160)
    vehicles: list[Selection] = Field(min_length=1, max_length=100)


class Decision(StrictModel):
    vehicle_id: RawReference
    expected_version: StrictInt = Field(gt=0)
    state: Literal["APPROVED", "REJECTED"]


class Response(StrictModel):
    command_id: UUID
    expected_version: StrictInt = Field(gt=0)
    stage_id: UUID
    decisions: list[Decision] = Field(min_length=1, max_length=100)


@router.get("/veh/{vehicle_id}/calibration")
def calibration(vehicle_id: str, db: Db, user: CurrentUser):
    return translated(db, lambda: store.readiness(db, user, vehicle_id))


@router.post("/veh/{vehicle_id}/calibration/confirm")
def confirm(vehicle_id: str, body: Confirmation, request: Request, db: Db, user: CurrentUser):
    return translated(
        db,
        lambda: store.confirm_calibration(
            db, user, request.state.login_session.token_hash, vehicle_id, body.model_dump(mode="json")
        ),
    )


@router.get("/approvals/candidates")
def candidates(db: Db, user: CurrentUser, offset: int = 0):
    def operation():
        check_permission(user, Permission.ARM2_VEH_VIEW)
        check_permission(user, Permission.ARM2_DOCUMENT_VIEW)
        if offset < 0:
            raise ValueError("Недопустимое смещение")
        # Same server readiness as the card and letter command, never client-side find(first).
        result = []
        for vehicle_id in db.scalars(select(Vehicle.id).order_by(Vehicle.id).limit(PAGE_SIZE).offset(offset)):
            try:
                result.append(store.readiness(db, user, vehicle_id))
            except ObjectAuthorizationError:
                continue
        return result

    return translated(db, operation)


@router.get("/approvals")
def requests(db: Db, user: CurrentUser, offset: int = 0):
    def operation():
        check_permission(user, Permission.ARM2_VEH_VIEW)
        check_permission(user, Permission.ARM2_DOCUMENT_VIEW)
        if offset < 0:
            raise ValueError("Недопустимое смещение")
        result = []
        for row in db.scalars(
            select(ApprovalRequest)
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
            .limit(PAGE_SIZE)
            .offset(offset)
        ):
            try:
                result.append(store.describe(db, user, row.id))
            except ObjectAuthorizationError:
                continue
        return result

    return translated(db, operation)


@router.post("/approvals/letter", status_code=201, dependencies=[Depends(protected_read, scope="request")])
def letter(body: Letter, request: Request, db: Db, user: CurrentUser):
    return translated(
        db,
        lambda: store.issue_letter(
            db, user, request.state.login_session.token_hash, body.model_dump(mode="json")
        ),
    )


@router.get("/approvals/{request_id}")
def detail(request_id: str, db: Db, user: CurrentUser):
    return translated(db, lambda: store.describe(db, user, request_id))


@router.put("/approvals/{request_id}/response-stages/{stage_id}", dependencies=[Depends(protected_read, scope="request")])
async def stage(request_id: str, stage_id: UUID, request: Request, db: Db, user: CurrentUser, name: str):
    data = await request.body()
    return translated(
        db,
        lambda: store.stage_response(
            db, user, request.state.login_session.token_hash, request_id, str(stage_id), name, data
        ),
    )


@router.post("/approvals/{request_id}/response", dependencies=[Depends(protected_read, scope="request")])
def response(request_id: str, body: Response, request: Request, db: Db, user: CurrentUser):
    return translated(
        db,
        lambda: store.apply_response(
            db, user, request.state.login_session.token_hash, request_id, body.model_dump(mode="json")
        ),
    )


@router.get("/approvals/{request_id}/response-stages/{stage_id}/{action}",
            dependencies=[Depends(protected_read, scope="request")])
def staged_file(
    request_id: str,
    stage_id: UUID,
    action: Literal["preview", "download"],
    request: Request,
    db: Db,
    user: CurrentUser,
):
    def operation():
        row = store.response_stage(
            db, user, request.state.login_session.token_hash, request_id, str(stage_id)
        )
        plan = row.plan["file"]
        from types import SimpleNamespace

        source = SimpleNamespace(id=plan["token"], size=plan["size"], sha256=plan["sha256"])
        return protected_response(
            files.staged_preview(source),
            plan["stored_name"],
            plan["media_type"],
            action == "preview",
            temporary=True,
        )

    return translated(db, operation)


@router.get("/approvals/{request_id}/files/{file_id}/{action}",
            dependencies=[Depends(protected_read, scope="request")])
def original(
    request_id: str, file_id: str, action: Literal["preview", "download"], db: Db, user: CurrentUser
):
    def operation():
        row = store.access(db, user, request_id)
        if file_id not in [row.letter_file_id, row.package_file_id, *row.response_file_ids]:
            raise ObjectNotFoundError("Файл не принадлежит заявке")
        file = db.get(DocumentFile, file_id)
        if file is None or file.process_id != request_id:
            raise ObjectNotFoundError("Файл не найден")
        return protected_response(
            files.download(file), file.stored_name, file.media_type, action == "preview", temporary=True
        )

    return translated(db, operation)
