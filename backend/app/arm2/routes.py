from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
)
from app.arm2.organization_models import Organization
from app.arm2.organization_store import create_organization, update_organization
from app.arm2.representative_models import (
    CounterpartyRepresentative,
    OrganizationRepresentative,
)
from app.arm2.representative_store import (
    create_representative,
    update_representative,
)
from app.arm2.schemas import (
    PartyCreate,
    PartyResponse,
    PartyUpdate,
    RepresentativeCreate,
    RepresentativeResponse,
    RepresentativeUpdate,
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
Arm2CounterpartyViewer = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_CP_VIEW)),
]
Arm2CounterpartyEditor = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_CP_EDIT)),
]
Arm2OrganizationViewer = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_ORG_VIEW)),
]
Arm2OrganizationEditor = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_ORG_EDIT)),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

_EXPECTED_UNIQUE_CONSTRAINTS = {
    "uq_arm2_counterparties_inn",
    "uq_arm2_organizations_inn",
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


def _party_response(record: Counterparty | Organization) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in (
            "id",
            "name",
            "full_name",
            "legal_address",
            "postal_address",
            "legal_form",
            "short_name",
            "inn",
            "kpp",
            "ogrn",
            "phone",
            "email",
            "bank_name",
            "bik",
            "settlement_account",
            "correspondent_account",
            "primary_signer_id",
            "status",
            "version",
        )
    }


def _representative_response(
    record: CounterpartyRepresentative | OrganizationRepresentative,
) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in (
            "id",
            "parent_id",
            "name",
            "position",
            "authority_basis",
            "phone",
            "email",
            "state",
            "user_id",
            "last_name",
            "first_name",
            "middle_name",
            "version",
        )
    }


def _get_or_404(db: Db, model, object_id: str, message: str):
    record = db.get(model, object_id)
    if record is None:
        raise HTTPException(404, message)
    return record


def _representative_or_404(db: Db, model, parent_id: str, representative_id: str):
    record = db.get(model, representative_id)
    if record is None or record.parent_id != parent_id:
        raise HTTPException(404, "ARM2 representative not found")
    return record


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def _save(db: Db, operation: Callable[[], Any]):
    try:
        record = operation()
        db.commit()
        return record
    except ObjectAuthorizationError as error:
        db.rollback()
        raise HTTPException(403, "ARM2 write permission denied") from error
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


@router.get("/counterparties", response_model=list[PartyResponse])
def list_counterparties(
    db: Db,
    _: Arm2CounterpartyViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    records = db.scalars(
        select(Counterparty).order_by(Counterparty.id).limit(limit).offset(offset)
    )
    return [_party_response(record) for record in records]


@router.get("/counterparties/{cp_id}", response_model=PartyResponse)
def get_counterparty(cp_id: str, db: Db, _: Arm2CounterpartyViewer):
    return _party_response(
        _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    )


@router.post("/counterparties", status_code=201, response_model=PartyResponse)
def post_counterparty(body: PartyCreate, db: Db, user: Arm2CounterpartyEditor):
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_counterparty(
            db, **values, principal=user, context=_context(user)
        ),
    )
    return _party_response(record)


@router.put("/counterparties/{cp_id}", response_model=PartyResponse)
def put_counterparty(
    cp_id: str,
    body: PartyUpdate,
    db: Db,
    user: Arm2CounterpartyEditor,
):
    values = body.model_dump(
        exclude={"expected_version", "name"}, exclude_unset=True
    )
    record = _save(
        db,
        lambda: update_counterparty(
            db,
            cp_id,
            body.expected_version,
            body.name,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _party_response(record)


@router.get("/organizations", response_model=list[PartyResponse])
def list_organizations(
    db: Db,
    _: Arm2OrganizationViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    records = db.scalars(
        select(Organization).order_by(Organization.id).limit(limit).offset(offset)
    )
    return [_party_response(record) for record in records]


@router.get("/organizations/{org_id}", response_model=PartyResponse)
def get_organization(org_id: str, db: Db, _: Arm2OrganizationViewer):
    return _party_response(
        _get_or_404(db, Organization, org_id, "ARM2 organization not found")
    )


@router.post("/organizations", status_code=201, response_model=PartyResponse)
def post_organization(body: PartyCreate, db: Db, user: Arm2OrganizationEditor):
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_organization(
            db, **values, principal=user, context=_context(user)
        ),
    )
    return _party_response(record)


@router.put("/organizations/{org_id}", response_model=PartyResponse)
def put_organization(
    org_id: str,
    body: PartyUpdate,
    db: Db,
    user: Arm2OrganizationEditor,
):
    values = body.model_dump(
        exclude={"expected_version", "name"}, exclude_unset=True
    )
    record = _save(
        db,
        lambda: update_organization(
            db,
            org_id,
            body.expected_version,
            body.name,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _party_response(record)


@router.get(
    "/counterparties/{cp_id}/representatives",
    response_model=list[RepresentativeResponse],
)
def list_counterparty_representatives(
    cp_id: str,
    db: Db,
    _: Arm2CounterpartyViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    records = db.scalars(
        select(CounterpartyRepresentative)
        .where(CounterpartyRepresentative.parent_id == cp_id)
        .order_by(CounterpartyRepresentative.id)
        .limit(limit)
        .offset(offset)
    )
    return [_representative_response(record) for record in records]


@router.get(
    "/counterparties/{cp_id}/representatives/{rep_id}",
    response_model=RepresentativeResponse,
)
def get_counterparty_representative(
    cp_id: str,
    rep_id: str,
    db: Db,
    _: Arm2CounterpartyViewer,
):
    _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    return _representative_response(
        _representative_or_404(db, CounterpartyRepresentative, cp_id, rep_id)
    )


@router.post(
    "/counterparties/{cp_id}/representatives",
    status_code=201,
    response_model=RepresentativeResponse,
)
def post_counterparty_representative(
    cp_id: str,
    body: RepresentativeCreate,
    db: Db,
    user: Arm2CounterpartyEditor,
):
    _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_representative(
            db,
            "CP",
            cp_id,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _representative_response(record)


@router.put(
    "/counterparties/{cp_id}/representatives/{rep_id}",
    response_model=RepresentativeResponse,
)
def put_counterparty_representative(
    cp_id: str,
    rep_id: str,
    body: RepresentativeUpdate,
    db: Db,
    user: Arm2CounterpartyEditor,
):
    _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    _representative_or_404(db, CounterpartyRepresentative, cp_id, rep_id)
    values = body.model_dump(exclude={"expected_version", "name"})
    record = _save(
        db,
        lambda: update_representative(
            db,
            "CP",
            rep_id,
            body.expected_version,
            cp_id,
            body.name,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _representative_response(record)


@router.get(
    "/organizations/{org_id}/representatives",
    response_model=list[RepresentativeResponse],
)
def list_organization_representatives(
    org_id: str,
    db: Db,
    _: Arm2OrganizationViewer,
    limit: Limit = 100,
    offset: Offset = 0,
):
    _get_or_404(db, Organization, org_id, "ARM2 organization not found")
    records = db.scalars(
        select(OrganizationRepresentative)
        .where(OrganizationRepresentative.parent_id == org_id)
        .order_by(OrganizationRepresentative.id)
        .limit(limit)
        .offset(offset)
    )
    return [_representative_response(record) for record in records]


@router.get(
    "/organizations/{org_id}/representatives/{rep_id}",
    response_model=RepresentativeResponse,
)
def get_organization_representative(
    org_id: str,
    rep_id: str,
    db: Db,
    _: Arm2OrganizationViewer,
):
    _get_or_404(db, Organization, org_id, "ARM2 organization not found")
    return _representative_response(
        _representative_or_404(db, OrganizationRepresentative, org_id, rep_id)
    )


@router.post(
    "/organizations/{org_id}/representatives",
    status_code=201,
    response_model=RepresentativeResponse,
)
def post_organization_representative(
    org_id: str,
    body: RepresentativeCreate,
    db: Db,
    user: Arm2OrganizationEditor,
):
    _get_or_404(db, Organization, org_id, "ARM2 organization not found")
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_representative(
            db,
            "ORG",
            org_id,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _representative_response(record)


@router.put(
    "/organizations/{org_id}/representatives/{rep_id}",
    response_model=RepresentativeResponse,
)
def put_organization_representative(
    org_id: str,
    rep_id: str,
    body: RepresentativeUpdate,
    db: Db,
    user: Arm2OrganizationEditor,
):
    _get_or_404(db, Organization, org_id, "ARM2 organization not found")
    _representative_or_404(db, OrganizationRepresentative, org_id, rep_id)
    values = body.model_dump(exclude={"expected_version", "name"})
    record = _save(
        db,
        lambda: update_representative(
            db,
            "ORG",
            rep_id,
            body.expected_version,
            org_id,
            body.name,
            **values,
            principal=user,
            context=_context(user),
        ),
    )
    return _representative_response(record)
