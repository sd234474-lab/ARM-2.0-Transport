from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.arm2.document_file_models import DocumentFileCorrection
from app.arm2.document_file_routes import router as file_router
from app.arm2.document_file_store import display_state, revision_files
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.document_store import create_document, operate_document, parent_access, update_document
from app.arm2.object_save import ObjectAuthorizationError, ObjectNotFoundError
from app.arm2.read_routes import _text_match
from app.arm2.schemas import (
    DocumentCreate,
    DocumentKindResponse,
    DocumentListResponse,
    DocumentOperation,
    DocumentResponse,
    DocumentUpdate,
)
from app.arm2.vehicle_models import Vehicle, calibration_matches_document
from app.auth import CurrentUser, Db

router = APIRouter()
router.include_router(file_router)
ParentToken = Literal["cp", "org", "veh"]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def scope(db, user, token, parent_id):
    try:
        parent_access(db, user, token.upper(), parent_id)
    except ObjectAuthorizationError as error:
        raise HTTPException(403, str(error)) from error
    except ObjectNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def response(db, record, *, current_files=None):
    return {
        "id": record.id,
        "parent_type": record.parent_type,
        "parent_id": record.parent_id,
        "kind_ref": record.kind_ref,
        "kind_name": db.scalar(select(DocumentKind.name).where(DocumentKind.id == record.kind_ref)),
        "name": record.name,
        "description": record.description,
        "archive_state": record.archive_state,
        "display_state": display_state(db, record, current_files=current_files),
        "version": record.version,
    }


@router.get("/{parent_type}/{parent_id}/documents/kinds", response_model=list[DocumentKindResponse])
def applicable_kinds(
    parent_type: ParentToken,
    parent_id: str,
    db: Db,
    user: CurrentUser,
    q: Annotated[str, Query(max_length=200, pattern=r"^[^\x00]*$")] = "",
    limit: Limit = 100,
    offset: Offset = 0,
):
    scope(db, user, parent_type, parent_id)
    stmt = select(DocumentKind).where(DocumentKind.applicable_object_types.contains([parent_type.upper()]))
    if q.strip():
        stmt = stmt.where(_text_match(DocumentKind.name, q.strip()))
    return [
        {field: getattr(row, field) for field in DocumentKindResponse.model_fields}
        for row in db.scalars(stmt.order_by(DocumentKind.id).limit(limit).offset(offset))
    ]


@router.get(
    "/{parent_type}/{parent_id}/documents",
    response_model=list[DocumentListResponse],
    response_model_exclude_unset=True,
)
def list_documents(
    parent_type: ParentToken,
    parent_id: str,
    db: Db,
    user: CurrentUser,
    archive: Literal["non_archived", "archived", "all"] = "non_archived",
    limit: Limit = 100,
    offset: Offset = 0,
    include_card_details: bool = False,
):
    scope(db, user, parent_type, parent_id)
    stmt = select(Document).where(
        Document.parent_type == parent_type.upper(), Document.parent_id == parent_id
    )
    if archive == "archived":
        stmt = stmt.where(Document.archive_state == "Архивный")
    elif archive == "non_archived":
        stmt = stmt.where(Document.archive_state == "Неархивный")
    rows = db.scalars(stmt.order_by(Document.id).limit(limit).offset(offset))
    if parent_type != "veh" or not include_card_details:
        return [response(db, row) for row in rows]

    vehicle = db.get(Vehicle, parent_id)
    result = []
    for row in rows:
        originals = revision_files(db, row.id, row.current_revision_id)
        data = response(db, row, current_files=originals)
        preview_files = [
            {"id": file.id, "original_upload_name": file.original_upload_name}
            for file in originals
            if db.get(DocumentFileCorrection, file.id) is None
        ]
        confirmed = (
            vehicle is not None
            and row.archive_state == "Неархивный"
            and data["kind_name"].strip().casefold() == "тарировка"
            and bool(preview_files)
            and len(preview_files) == len(originals)
            and calibration_matches_document(vehicle, row)
        )
        data.update(
            calibration_m3=vehicle.calibration_m3 if confirmed else None,
            current_revision_id=row.current_revision_id,
            preview_files=preview_files,
        )
        result.append(data)
    return result


@router.get("/{parent_type}/{parent_id}/documents/counts", response_model=dict[str, int])
def document_counts(parent_type: ParentToken, parent_id: str, db: Db, user: CurrentUser):
    scope(db, user, parent_type, parent_id)
    total, archived = db.execute(select(
        func.count(), func.count().filter(Document.archive_state == "Архивный"),
    ).where(Document.parent_type == parent_type.upper(), Document.parent_id == parent_id)).one()
    return {"all": total, "non_archived": total - archived, "archived": archived}


@router.get("/{parent_type}/{parent_id}/documents/{document_id}", response_model=DocumentResponse)
def get_document(parent_type: ParentToken, parent_id: str, document_id: str, db: Db, user: CurrentUser):
    scope(db, user, parent_type, parent_id)
    record = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.parent_type == parent_type.upper(),
            Document.parent_id == parent_id,
        )
    )
    if record is None:
        raise HTTPException(404, "Документ этой карточки не найден")
    return response(db, record)


@router.post("/{parent_type}/{parent_id}/documents/{document_id}/operations/{operation}", response_model=DocumentResponse)
def document_operation(parent_type: ParentToken, parent_id: str, document_id: str,
                       operation: Literal["archive", "restore", "retype", "move"],
                       body: DocumentOperation, db: Db, user: CurrentUser):
    from app.arm2.object_save import SaveContext
    from app.arm2.routes import _save

    allowed = {"expected_version"} | ({"kind_ref"} if operation == "retype" else
                                      {"target_type", "target_id"} if operation == "move" else set())
    if body.model_fields_set - allowed:
        raise HTTPException(422, "Поля не относятся к выбранной операции")
    record = _save(db, lambda: operate_document(db, parent_type.upper(), parent_id, document_id,
                   operation=operation, principal=user,
                   context=SaveContext("HUMAN", str(user.id), "arm2-api-document-" + operation),
                   **body.model_dump()))
    return response(db, record)


@router.post("/{parent_type}/{parent_id}/documents", status_code=201, response_model=DocumentResponse)
def post_document(parent_type: ParentToken, parent_id: str, body: DocumentCreate, db: Db, user: CurrentUser):
    from app.arm2.routes import _context, _save

    record = _save(
        db,
        lambda: create_document(
            db, parent_type.upper(), parent_id, **body.model_dump(), principal=user, context=_context(user)
        ),
    )
    return response(db, record)


@router.put("/{parent_type}/{parent_id}/documents/{document_id}", response_model=DocumentResponse)
def put_document(
    parent_type: ParentToken,
    parent_id: str,
    document_id: str,
    body: DocumentUpdate,
    db: Db,
    user: CurrentUser,
):
    from app.arm2.routes import _context, _save

    record = _save(
        db,
        lambda: update_document(
            db,
            parent_type.upper(),
            parent_id,
            document_id,
            **body.model_dump(),
            principal=user,
            context=_context(user),
        ),
    )
    return response(db, record)
