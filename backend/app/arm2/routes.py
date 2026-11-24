from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.arm2.approval_routes import router as approval_router
from app.arm2.background_operations import operation_lifespan
from app.arm2.bulk_routes import router as bulk_router
from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_rules import counterparty_response
from app.arm2.counterparty_store import create_counterparty, update_counterparty
from app.arm2.deletion_cleanup import clean_job
from app.arm2.deletion_routes import router as deletion_router
from app.arm2.deletion_service import CLEANUP, deletion_history_allowed
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_kind_store import create_document_kind, update_document_kind
from app.arm2.document_models import Document
from app.arm2.document_routes import router as document_router
from app.arm2.document_store import parent_access
from app.arm2.history_models import HistoryEvent
from app.arm2.http_errors import CONTROLLED_ERRORS, controlled_error, rollback
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectRegistryError
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectNotFoundError,
    ObjectVersionConflictError,
    SaveContext,
)
from app.arm2.organization_models import Organization
from app.arm2.organization_store import create_organization, update_organization
from app.arm2.questionnaire_jobs import apply_job
from app.arm2.questionnaire_routes import router as questionnaire_router
from app.arm2.questionnaire_store import APPLY_OPERATION
from app.arm2.read_routes import router as read_router
from app.arm2.representative_models import (
    CounterpartyRepresentative,
    OrganizationRepresentative,
)
from app.arm2.representative_store import (
    create_representative,
    update_representative,
)
from app.arm2.schemas import (
    CounterpartyCreate,
    CounterpartyResponse,
    CounterpartyUpdate,
    DocumentKindCreate,
    DocumentKindResponse,
    DocumentKindUpdate,
    HistoryEventDetail,
    HistoryEventSummary,
    HistoryObjectType,
    OperationalOwnerCommand,
    OperationalOwnerResponse,
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
from app.arm2.vehicle_store import create_vehicle, set_operational_owner, update_vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_store import create_vehicle_type, update_vehicle_type
from app.auth import CurrentUser, Db, check_permission, require_permission
from app.models import User
from app.permissions import Permission

router = APIRouter(prefix="/api/arm2", tags=["arm2"],
                   lifespan=operation_lifespan({APPLY_OPERATION: apply_job, CLEANUP: clean_job}))
router.include_router(deletion_router)
router.include_router(read_router)  # Static /search paths precede legacy /{id} routes.
router.include_router(questionnaire_router)
router.include_router(bulk_router)
router.include_router(document_router)
router.include_router(approval_router)
Arm2TypeViewer = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_TYPE_VIEW)),
]
Arm2TypeEditor = Annotated[
    User,
    Depends(require_permission(Permission.ARM2_TYPE_EDIT)),
]
Arm2DocumentKindViewer = Annotated[User, Depends(require_permission(Permission.ARM2_DOC_KIND_VIEW))]
Arm2DocumentKindEditor = Annotated[User, Depends(require_permission(Permission.ARM2_DOC_KIND_EDIT))]
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
    "uq_arm2_document_kinds_name",
    "uq_arm2_counterparties_inn",
    "uq_arm2_organizations_inn",
    "uq_arm2_vehicle_types_name",
    "uq_arm2_vehicles_plate_number",
}
_HISTORY_VIEW_PERMISSIONS = {
    "APPROVAL": Permission.ARM2_VEH_VIEW,
    "APPROVAL-ITEM": Permission.ARM2_VEH_VIEW,
    "DOCUMENT": Permission.ARM2_DOCUMENT_VIEW,
    "DOC-KIND": Permission.ARM2_DOC_KIND_VIEW,
    "TYPE": Permission.ARM2_TYPE_VIEW,
    "VEH": Permission.ARM2_VEH_VIEW,
    "CP": Permission.ARM2_CP_VIEW,
    "CPREP": Permission.ARM2_CP_VIEW,
    "ORG": Permission.ARM2_ORG_VIEW,
    "ORGREP": Permission.ARM2_ORG_VIEW,
}


def _context(user: User) -> SaveContext:
    return SaveContext("HUMAN", str(user.id), "arm2-api")


def _type_response(record: VehicleType) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in (
            "id",
            "name",
            "plate_category",
            "requires_sts",
            "requires_calibration",
            "requires_wialon",
            "status",
            "version",
        )
    }


def _document_kind_response(record: DocumentKind) -> dict[str, Any]:
    return {field: getattr(record, field) for field in (
        "id", "name", "applicable_object_types", "max_current_documents", "version", "file_naming_template",
    )}


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
            "calibration_m3", "calibration_confirmed_at", "calibration_confirmed_by",
            "calibration_document_id", "calibration_revision_id", "calibration_plate_number",
            "owner_cp_id",
            "owner_org_id",
            "version",
        )
    }


def _party_response(record: Counterparty | Organization) -> dict[str, Any]:
    fields = [f for f in PartyResponse.model_fields]
    if isinstance(record, Counterparty):
        fields += ["type", "last_name", "first_name", "middle_name"]
    values = {f: getattr(record, f) for f in fields}
    return counterparty_response(values) if isinstance(record, Counterparty) else values


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


def _history_summary_response(event: HistoryEvent) -> dict[str, Any]:
    return {
        field: getattr(event, field)
        for field in (
            "event_id",
            "object_type",
            "object_id",
            "operation",
            "occurred_at",
            "actor_kind",
            "actor_id",
            "source",
            "version_before",
            "version_after",
        )
    }


def _history_detail_response(event: HistoryEvent) -> dict[str, Any]:
    return {
        **_history_summary_response(event),
        "correlation_id": event.correlation_id,
        "changes": event.changes,
        "related_objects": event.related_objects,
    }


def _validate_history_access(
    db: Db,
    user: User,
    object_type: HistoryObjectType,
    object_id: str,
) -> None:
    check_permission(user, _HISTORY_VIEW_PERMISSIONS[object_type])
    try:
        OBJECT_REGISTRY.require(object_type).validate_id(object_id)
    except ObjectRegistryError as error:
        raise HTTPException(422, "Invalid ARM2 history object id") from error
    try:
        if deletion_history_allowed(db, user, object_type, object_id):
            return
    except ObjectAuthorizationError as error:
        raise HTTPException(403, str(error)) from None
    if object_type in {"APPROVAL", "APPROVAL-ITEM"}:
        from app.arm2.approval_models import ApprovalDecision
        from app.arm2.approval_store import access
        from app.arm2.document_file_routes import translated
        def operation():
            request_id = object_id
            if object_type == "APPROVAL-ITEM":
                decision = db.get(ApprovalDecision, object_id)
                if decision is None:
                    raise ObjectNotFoundError("Решение не найдено")
                request_id = decision.request_id
            access(db, user, request_id)
        translated(db, operation)
        return
    if object_type == "DOCUMENT":
        document = db.get(Document, object_id)
        if document is None:
            raise HTTPException(404, "Документ не найден")
        try:
            parent_access(db, user, document.parent_type, document.parent_id)
        except ObjectAuthorizationError as error:
            raise HTTPException(403, "Нет прав на историю документов этого родителя") from error
        except (ObjectNotFoundError, ValueError) as error:
            raise HTTPException(404, "Родитель документа не найден") from error
        return
    representative_parent = {
        "CPREP": (CounterpartyRepresentative, Counterparty, "CP"),
        "ORGREP": (OrganizationRepresentative, Organization, "ORG"),
    }.get(object_type)
    if representative_parent is None:
        return
    representative_model, parent_model, parent_type = representative_parent
    parent_id = db.scalar(
        select(representative_model.parent_id).where(
            representative_model.id == object_id
        )
    )
    if parent_id is None:
        raise HTTPException(404, "ARM2 representative history not found")
    try:
        OBJECT_REGISTRY.require(parent_type).validate_id(parent_id)
    except ObjectRegistryError as error:
        raise HTTPException(404, "ARM2 representative parent not found") from error
    parent_exists = db.scalar(
        select(parent_model.id).where(parent_model.id == parent_id)
    )
    if parent_exists is None:
        raise HTTPException(404, "ARM2 representative parent not found")


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def _save(db: Db, operation: Callable[[], Any]):
    try:
        record = operation()
        db.commit()
        return record
    except CONTROLLED_ERRORS as error:
        rollback(db)
        raise controlled_error(error, {
            ObjectAuthorizationError: "ARM2 write permission denied",
            ObjectNotFoundError: "ARM2 object not found",
            ObjectVersionConflictError: "ARM2 object version conflict",
        }) from None
    except IntegrityError as error:
        rollback(db)
        if _constraint_name(error) in _EXPECTED_UNIQUE_CONSTRAINTS:
            raise HTTPException(409, "ARM2 object already exists") from None
        raise HTTPException(500, "Unexpected ARM2 database error") from None
    except SQLAlchemyError:
        rollback(db)
        raise HTTPException(500, "Unexpected ARM2 database error") from None


@router.get("/status")
def arm2_status(_: Arm2TypeViewer):
    return {"module": "arm2", "phase": "bootstrap"}


@router.get("/document-kinds", response_model=list[DocumentKindResponse])
def list_document_kinds(db: Db, _: Arm2DocumentKindViewer, limit: Limit = 100, offset: Offset = 0):
    records = db.scalars(select(DocumentKind).order_by(DocumentKind.id).limit(limit).offset(offset))
    return [_document_kind_response(record) for record in records]


@router.get("/document-kinds/{kind_id}", response_model=DocumentKindResponse)
def get_document_kind(kind_id: str, db: Db, _: Arm2DocumentKindViewer):
    return _document_kind_response(_get_or_404(db, DocumentKind, kind_id, "ARM2 document kind not found"))


@router.post("/document-kinds", status_code=201, response_model=DocumentKindResponse)
def post_document_kind(body: DocumentKindCreate, db: Db, user: Arm2DocumentKindEditor):
    record = _save(db, lambda: create_document_kind(db, **body.model_dump(), principal=user, context=_context(user)))
    return _document_kind_response(record)


@router.put("/document-kinds/{kind_id}", response_model=DocumentKindResponse)
def put_document_kind(kind_id: str, body: DocumentKindUpdate, db: Db, user: Arm2DocumentKindEditor):
    values = body.model_dump(exclude={"expected_version"})
    record = _save(db, lambda: update_document_kind(db, kind_id, body.expected_version, **values,
                                                  principal=user, context=_context(user)))
    return _document_kind_response(record)


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
    values = body.model_dump(exclude={"expected_version"}, exclude_unset=True)
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


@router.post("/vehicles/{vehicle_id}/operational-owner", response_model=OperationalOwnerResponse)
def post_operational_owner(vehicle_id: str, body: OperationalOwnerCommand, db: Db, user: CurrentUser):
    saved = _save(db, lambda: set_operational_owner(db, vehicle_id, **body.model_dump(),
                  principal=user, context=SaveContext("HUMAN", str(user.id), "arm2-api-operational-owner")))
    return {**_vehicle_response(saved.record), "no_op": not saved.changed}


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


@router.get("/counterparties", response_model=list[CounterpartyResponse])
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


@router.get("/counterparties/{cp_id}", response_model=CounterpartyResponse)
def get_counterparty(cp_id: str, db: Db, _: Arm2CounterpartyViewer):
    return _party_response(
        _get_or_404(db, Counterparty, cp_id, "ARM2 counterparty not found")
    )


@router.post("/counterparties", status_code=201, response_model=CounterpartyResponse)
def post_counterparty(body: CounterpartyCreate, db: Db, user: Arm2CounterpartyEditor):
    values = body.model_dump()
    record = _save(
        db,
        lambda: create_counterparty(
            db, **values, principal=user, context=_context(user)
        ),
    )
    return _party_response(record)


@router.put("/counterparties/{cp_id}", response_model=CounterpartyResponse)
def put_counterparty(
    cp_id: str,
    body: CounterpartyUpdate,
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


@router.get("/history/events/{event_id}", response_model=HistoryEventDetail)
def get_history_event(event_id: int, db: Db, user: CurrentUser):
    identity = db.execute(
        select(HistoryEvent.object_type, HistoryEvent.object_id).where(
            HistoryEvent.event_id == event_id
        )
    ).one_or_none()
    if identity is None:
        raise HTTPException(404, "ARM2 history event not found")
    _validate_history_access(db, user, identity.object_type, identity.object_id)
    event = db.get(HistoryEvent, event_id)
    if event is None:
        raise HTTPException(404, "ARM2 history event not found")
    return _history_detail_response(event)


@router.get(
    "/history/{object_type}/{object_id}",
    response_model=list[HistoryEventSummary],
)
def list_object_history(
    object_type: HistoryObjectType,
    object_id: str,
    db: Db,
    user: CurrentUser,
    limit: Limit = 100,
    offset: Offset = 0,
):
    _validate_history_access(db, user, object_type, object_id)
    events = db.scalars(
        select(HistoryEvent)
        .where(
            HistoryEvent.object_type == object_type,
            HistoryEvent.object_id == object_id,
        )
        .order_by(HistoryEvent.occurred_at.desc(), HistoryEvent.event_id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_history_summary_response(event) for event in events]


@router.get("/history/{object_type}/{object_id}/document-moves", response_model=list[HistoryEventDetail])
def list_related_document_moves(object_type: Literal["CP", "ORG", "VEH"], object_id: str,
                                db: Db, user: CurrentUser, limit: Limit = 100, offset: Offset = 0):
    try:
        parent_access(db, user, object_type, object_id)
    except ObjectAuthorizationError:
        raise HTTPException(403, "Нет прав на историю связей документов") from None
    except (ObjectNotFoundError, ValueError):
        raise HTTPException(404, "Родитель не найден") from None
    # Only one real DOCUMENT UPDATED event; never fabricate a CP/ORG/VEH event.
    matches = [HistoryEvent.related_objects.contains([
        {"object_type": object_type, "object_id": object_id, "relation": relation}
    ]) for relation in ("document_parent_from", "document_parent_to")]
    events = db.scalars(select(HistoryEvent).join(Document, Document.id == HistoryEvent.object_id)
        .where(HistoryEvent.object_type == "DOCUMENT", HistoryEvent.operation == "UPDATED",
               HistoryEvent.changes.has_key("parent_id"),  # noqa: W601 - PostgreSQL JSONB
               Document.parent_type == object_type, matches[0] | matches[1])
        .order_by(HistoryEvent.occurred_at.desc(), HistoryEvent.event_id.desc()).limit(limit).offset(offset))
    result = []
    for event in events:
        _validate_history_access(db, user, "DOCUMENT", event.object_id)
        result.append(_history_detail_response(event))
    return result
