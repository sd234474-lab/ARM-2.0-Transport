from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.arm2.object_save import (
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
)
from app.arm2.schemas import (
    VehicleCreate,
    VehicleResponse,
    VehicleTypeCreate,
    VehicleTypeResponse,
    VehicleTypeUpdate,
    VehicleUpdate,
)
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle, update_vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type
from app.auth import Db, require_permission
from app.models import User
from app.permissions import Permission

router = APIRouter(prefix="/api/arm2", tags=["arm2"])
Arm2TypeViewer = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_TYPE_VIEW)),
]
Arm2TypeEditor = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_TYPE_EDIT)),
]
Arm2VehicleViewer = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_VEH_VIEW)),
]
Arm2VehicleEditor = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_VEH_EDIT)),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

_EXPECTED_UNIQUE_CONSTRAINTS = {
    "uq_arm2_vehicle_types_name",
    "uq_arm2_vehicles_plate_number",
}


def _context(user: User) -> SaveContext:
    return SaveContext("HUMAN", str(user.id), "arm2-api")


def _type_response(record: VehicleType) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in (
            "id",
            "name",
            "requires_sts",
            "requires_calibration",
            "requires_wialon",
            "status",
            "version",
        )
    }


def _vehicle_response(record: Vehicle) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in (
            "id",
            "name",
            "plate_number",
            "type_id",
            "accounting_state",
            "model",
            "wialon_unit_id",
            "owner_cp_id",
            "owner_org_id",
            "version",
        )
    }


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def _save(db: Db, operation: Callable[[], Any]):
    try:
        record = operation()
        db.commit()
        return record
    except ObjectNotFoundError as error:
        db.rollback()
        raise HTTPException(404, "ARM2 object not found") from error
    except ObjectVersionConflictError as error:
        db.rollback()
        raise HTTPException(409, "ARM2 object version conflict") from error
    except IntegrityError as error:
        db.rollback()
        if _constraint_name(error) in _EXPECTED_UNIQUE_CONSTRAINTS:
            raise HTTPException(409, "ARM2 object already exists") from error
        raise HTTPException(500, "Unexpected ARM2 database error") from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error


@router.get("/status")
def arm2_status(_: Arm2TypeViewer):
    return {"module": "arm2", "phase": "bootstrap"}


@router.get("/types", response_model=list[VehicleTypeResponse])
def list_vehicle_types(
    db: Db,
    _: Arm2TypeViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    records = db.scalars(
        select(VehicleType).order_by(VehicleType.id).limit(limit).offset(offset)
    )
    return [_type_response(record) for record in records]


@router.get("/types/{type_id}", response_model=VehicleTypeResponse)
def get_vehicle_type(type_id: str, db: Db, _: Arm2TypeViewer):
    record = db.get(VehicleType, type_id)
    if record is None:
        raise HTTPException(404, "ARM2 vehicle type not found")
    return _type_response(record)


@router.post("/types", status_code=201, response_model=VehicleTypeResponse)
def post_vehicle_type(body: VehicleTypeCreate, db: Db, user: Arm2TypeEditor):
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_vehicle_type(
            db, **values, principal=user, context=_context(user)
        ),
    )
    return _type_response(record)


@router.put("/types/{type_id}", response_model=VehicleTypeResponse)
def put_vehicle_type(
    type_id: str,
    body: VehicleTypeUpdate,
    db: Db,
    user: Arm2TypeEditor,
):
    values = body.model_dump(exclude={"expected_version"})
    record = _save(
        db,
        lambda: update_vehicle_type(
            db,
            type_id,
            body.expected_version,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _type_response(record)


@router.get("/vehicles", response_model=list[VehicleResponse])
def list_vehicles(
    db: Db,
    _: Arm2VehicleViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    records = db.scalars(
        select(Vehicle).order_by(Vehicle.id).limit(limit).offset(offset)
    )
    return [_vehicle_response(record) for record in records]


@router.get("/vehicles/{vehicle_id}", response_model=VehicleResponse)
def get_vehicle(vehicle_id: str, db: Db, _: Arm2VehicleViewer):
    record = db.get(Vehicle, vehicle_id)
    if record is None:
        raise HTTPException(404, "ARM2 vehicle not found")
    return _vehicle_response(record)


@router.post("/vehicles", status_code=201, response_model=VehicleResponse)
def post_vehicle(body: VehicleCreate, db: Db, user: Arm2VehicleEditor):
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_vehicle(
            db, **values, principal=user, context=_context(user)
        ),
    )
    return _vehicle_response(record)


@router.put("/vehicles/{vehicle_id}", response_model=VehicleResponse)
def put_vehicle(
    vehicle_id: str,
    body: VehicleUpdate,
    db: Db,
    user: Arm2VehicleEditor,
):
    values = body.model_dump(
        exclude={"expected_version", "owner_cp_id", "owner_org_id"}
    )
    for field in ("owner_cp_id", "owner_org_id"):
        if field in body.model_fields_set:
            values[field] = getattr(body, field)
    record = _save(
        db,
        lambda: update_vehicle(
            db,
            vehicle_id,
            body.expected_version,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _vehicle_response(record)
