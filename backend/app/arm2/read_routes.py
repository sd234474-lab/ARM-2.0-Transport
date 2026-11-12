"""B1 read models. Legacy list/write contracts and the SAVE kernel are unchanged."""
import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, literal, or_, select

from app.arm2.counterparty_models import Counterparty
from app.arm2.organization_models import Organization
from app.arm2.schemas import PartyResponse, VehicleResponse, VehicleTypeResponse
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import _LATIN_TO_CYRILLIC
from app.arm2.vehicle_type_models import VehicleType
from app.auth import CurrentUser, Db, check_permission
from app.permissions import Permission, has_permission

router = APIRouter()
TextQuery = Annotated[str, Query(max_length=200, pattern=r'^[^\x00]*$')]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
Direction = Literal['asc', 'desc']


class VehicleSearchItem(VehicleResponse):
    type_name: str | None
    owner_kind: Literal['CP', 'ORG'] | None
    owner_name: str | None


class VehiclePage(BaseModel):
    items: list[VehicleSearchItem]
    total: int
    matched: int
    limit: int
    offset: int


class PartyPage(BaseModel):
    items: list[PartyResponse]
    total: int
    matched: int
    limit: int
    offset: int


class TypePage(BaseModel):
    items: list[VehicleTypeResponse]
    total: int
    matched: int
    limit: int
    offset: int


def _like(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _text_match(column, value):
    # Native Windows C locale does not case-fold Cyrillic for ILIKE. Fold the
    # Russian alphabet explicitly; ASCII keeps standard ILIKE semantics. No
    # collation/extension/schema changes or rewritten stored names are needed.
    uppercase = 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'
    return func.translate(column, uppercase, uppercase.lower()).ilike(_like(value.lower()), escape='\\')


def _page(db, stmt, model, sort_column, direction, limit, offset):
    matched = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    order = sort_column.asc() if direction == 'asc' else sort_column.desc()
    rows = db.execute(stmt.order_by(order.nulls_last(), model.id.asc()).limit(limit).offset(offset)).mappings().all()
    return {'items': [dict(row) for row in rows], 'total': db.scalar(select(func.count()).select_from(model)),
            'matched': matched, 'limit': limit, 'offset': offset}


@router.get('/vehicles/search', response_model=VehiclePage)
def search_vehicles(
    db: Db, user: CurrentUser, q: TextQuery = '', limit: Limit = 100, offset: Offset = 0,
    type_id: Annotated[str | None, Query(pattern=r'^TYPE-[0-9]{6,}$')] = None,
    owner_kind: Literal['CP', 'ORG', 'NONE'] | None = None,
    owner_id: Annotated[str | None, Query(pattern=r'^(CP|ORG)-[0-9]{6,}$')] = None,
    state: Literal['Действующая', 'Архивная'] | None = None,
    sort: Literal['name', 'plate_number', 'type', 'owner', 'accounting_state'] = 'name',
    direction: Direction = 'asc',
):
    check_permission(user, Permission.ARM2_VEH_VIEW)
    type_view = has_permission(user, Permission.ARM2_TYPE_VIEW)
    cp_view = has_permission(user, Permission.ARM2_CP_VIEW)
    org_view = has_permission(user, Permission.ARM2_ORG_VIEW)
    if type_id or sort == 'type':
        check_permission(user, Permission.ARM2_TYPE_VIEW)
    if owner_id and (owner_kind not in {'CP', 'ORG'} or not owner_id.startswith(owner_kind + '-')):
        raise HTTPException(422, 'owner_id requires matching CP/ORG owner_kind')
    if owner_kind in {'CP', 'ORG'}:
        check_permission(user, Permission.ARM2_CP_VIEW if owner_kind == 'CP' else Permission.ARM2_ORG_VIEW)
        if owner_id and db.get(Counterparty if owner_kind == 'CP' else Organization, owner_id) is None:
            raise HTTPException(404, 'ARM2 owner not found')
    if sort == 'owner' and not (cp_view and org_view):
        raise HTTPException(403, 'Viewing both owner dictionaries is required for owner sorting')
    columns = [getattr(Vehicle, field).label(field) for field in VehicleResponse.model_fields]
    owner_name = case((Vehicle.owner_cp_id.is_not(None), Counterparty.name if cp_view else literal(None)),
                      else_=Organization.name if org_view else literal(None))
    stmt = select(*columns, (VehicleType.name if type_view else literal(None)).label('type_name'),
                  case((Vehicle.owner_cp_id.is_not(None), 'CP'), (Vehicle.owner_org_id.is_not(None), 'ORG'),
                       else_=None).label('owner_kind'), owner_name.label('owner_name')).select_from(Vehicle)
    if type_view:
        stmt = stmt.outerjoin(VehicleType, VehicleType.id == Vehicle.type_id)
    if cp_view:
        stmt = stmt.outerjoin(Counterparty, Counterparty.id == Vehicle.owner_cp_id)
    if org_view:
        stmt = stmt.outerjoin(Organization, Organization.id == Vehicle.owner_org_id)
    if q.strip():
        # Display grouping spaces may be ignored in SEARCH only, never in writes/import.
        plate = re.sub(r'\s+', '', q.strip().upper().translate(_LATIN_TO_CYRILLIC))
        stmt = stmt.where(or_(_text_match(Vehicle.name, q.strip()),
                              Vehicle.plate_number.ilike(_like(plate), escape='\\')))
    if type_id:
        stmt = stmt.where(Vehicle.type_id == type_id)
    if state:
        stmt = stmt.where(Vehicle.accounting_state == state)
    if owner_kind == 'NONE':
        stmt = stmt.where(Vehicle.owner_cp_id.is_(None), Vehicle.owner_org_id.is_(None))
    elif owner_kind:
        column = Vehicle.owner_cp_id if owner_kind == 'CP' else Vehicle.owner_org_id
        stmt = stmt.where(column == owner_id if owner_id else column.is_not(None))
    sorts = {'name': Vehicle.name, 'plate_number': Vehicle.plate_number, 'type': VehicleType.name,
             'owner': owner_name, 'accounting_state': Vehicle.accounting_state}
    return _page(db, stmt, Vehicle, sorts[sort], direction, limit, offset)


def _search_party(db, user, model, permission, q, status, legal_form, sort, direction, limit, offset):
    check_permission(user, permission)
    stmt = select(*[getattr(model, field).label(field) for field in PartyResponse.model_fields])
    if q.strip():
        stmt = stmt.where(or_(*[_text_match(getattr(model, field), q.strip())
                                for field in ['name', 'full_name', 'inn']]))
    if status:
        stmt = stmt.where(model.status == status)
    if legal_form:
        stmt = stmt.where(model.legal_form == legal_form)
    return _page(db, stmt, model, getattr(model, sort), direction, limit, offset)


PartySort = Literal['name', 'full_name', 'legal_form', 'inn', 'status']


@router.get('/counterparties/search', response_model=PartyPage)
def search_counterparties(db: Db, user: CurrentUser, q: TextQuery = '', status: Literal['REVIEW', 'CONFIRMED'] | None = None,
                         legal_form: Annotated[str | None, Query(max_length=500, pattern=r'^[^\x00]*$')] = None, sort: PartySort = 'name',
                         direction: Direction = 'asc', limit: Limit = 100, offset: Offset = 0):
    return _search_party(db, user, Counterparty, Permission.ARM2_CP_VIEW, q, status, legal_form, sort, direction, limit, offset)


@router.get('/organizations/search', response_model=PartyPage)
def search_organizations(db: Db, user: CurrentUser, q: TextQuery = '', status: Literal['REVIEW', 'CONFIRMED'] | None = None,
                        legal_form: Annotated[str | None, Query(max_length=500, pattern=r'^[^\x00]*$')] = None, sort: PartySort = 'name',
                        direction: Direction = 'asc', limit: Limit = 100, offset: Offset = 0):
    return _search_party(db, user, Organization, Permission.ARM2_ORG_VIEW, q, status, legal_form, sort, direction, limit, offset)


@router.get('/types/search', response_model=TypePage)
def search_types(db: Db, user: CurrentUser, q: TextQuery = '', limit: Limit = 100, offset: Offset = 0):
    check_permission(user, Permission.ARM2_TYPE_VIEW)
    stmt = select(*[getattr(VehicleType, field).label(field) for field in VehicleTypeResponse.model_fields])
    if q.strip():
        stmt = stmt.where(_text_match(VehicleType.name, q.strip()))
    return _page(db, stmt, VehicleType, VehicleType.name, 'asc', limit, offset)
