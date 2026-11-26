"""B1 read models. Legacy list/write contracts and the SAVE kernel are unchanged."""
import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func, literal, or_, select, true

from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_rules import PERSON, counterparty_response
from app.arm2.document_kind_models import DocumentKind
from app.arm2.ids import id_pattern
from app.arm2.organization_models import Organization
from app.arm2.schemas import (
    CounterpartyResponse,
    DocumentKindResponse,
    PartyResponse,
    VehicleResponse,
    VehicleTypeResponse,
)
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


class CatalogPage(BaseModel):
    total: int
    matched: int
    limit: int
    offset: int
    facets: dict[str, int]


class VehiclePage(CatalogPage):
    items: list[VehicleSearchItem]
    without_owner: int
    without_wialon: int


class PartyPage(CatalogPage):
    items: list[CounterpartyResponse | PartyResponse]


class TypePage(CatalogPage):
    items: list[VehicleTypeResponse]


class DocumentKindPage(CatalogPage):
    items: list[DocumentKindResponse]


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


def _with_facets(db, stmt, selected, groups):
    """Count each option with the other filters applied, before LIMIT/OFFSET."""
    counts = [func.count().filter(
        condition, *(value for key, value in selected.items() if key != group)
    ).label(option) for group, options in groups.items() for option, condition in options.items()]
    summary = db.execute(stmt.with_only_columns(*counts, maintain_column_froms=True)).mappings().one()
    return stmt.where(*selected.values()), dict(summary)


def _missing(column):
    return or_(column.is_(None), func.trim(column) == '')


@router.get('/vehicles/search', response_model=VehiclePage)
def search_vehicles(
    db: Db, user: CurrentUser, q: TextQuery = '', limit: Limit = 100, offset: Offset = 0,
    type_id: Annotated[str | None, Query(pattern=id_pattern("TYPE"))] = None,
    owner_kind: Literal['CP', 'ORG', 'NONE'] | None = None,
    owner_id: Annotated[str | None, Query(pattern=id_pattern("CP", "ORG"))] = None,
    state: Literal['Действующая', 'Архивная'] | None = None,
    sort: Literal['name', 'plate_number', 'type', 'owner', 'accounting_state'] = 'name',
    direction: Direction = 'asc',
    wialon: Literal['missing'] | None = None,
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
    no_owner = Vehicle.owner_cp_id.is_(None) & Vehicle.owner_org_id.is_(None)
    no_wialon = _missing(Vehicle.wialon_unit_id)
    selected = {}
    if state:
        selected['state'] = Vehicle.accounting_state == state
    if owner_kind == 'NONE':
        selected['owner'] = no_owner
    elif owner_kind:
        column = Vehicle.owner_cp_id if owner_kind == 'CP' else Vehicle.owner_org_id
        selected['owner'] = column == owner_id if owner_id else column.is_not(None)
    if wialon:
        selected['wialon'] = no_wialon
    sorts = {'name': Vehicle.name, 'plate_number': Vehicle.plate_number, 'type': VehicleType.name,
             'owner': owner_name, 'accounting_state': Vehicle.accounting_state}
    stmt, facets = _with_facets(db, stmt, selected, {
        'state': {'all_states': true(), 'active': Vehicle.accounting_state == 'Действующая',
                  'archived': Vehicle.accounting_state == 'Архивная'},
        'owner': {'no_owner': no_owner}, 'wialon': {'no_wialon': no_wialon},
        'legacy': {'without_owner': no_owner, 'without_wialon': no_wialon},
    })
    without_owner, without_wialon = facets.pop('without_owner'), facets.pop('without_wialon')
    return {**_page(db, stmt, Vehicle, sorts[sort], direction, limit, offset),
            'facets': facets, 'without_owner': without_owner, 'without_wialon': without_wialon}


def _search_party(db, user, model, permission, q, legal_form, sort, direction, limit, offset, inn, party_type=None):
    check_permission(user, permission)
    stmt = select(*[getattr(model, field).label(field) for field in PartyResponse.model_fields])
    if model is Counterparty:
        stmt = stmt.add_columns(*[getattr(model, f).label(f) for f in ('type', 'last_name', 'first_name', 'middle_name')])
    full_name = (case((Counterparty.type == PERSON, func.trim(func.concat(
        case((Counterparty.legal_form == 'ИП', 'Индивидуальный предприниматель '), else_=''),
        func.concat_ws(' ', Counterparty.last_name, Counterparty.first_name, Counterparty.middle_name)))),
        else_=Counterparty.full_name) if model is Counterparty else model.full_name)
    if q.strip():
        stmt = stmt.where(or_(_text_match(full_name, q.strip()), *[_text_match(getattr(model, field), q.strip())
                                for field in ['name', 'full_name', 'short_name', 'inn',
                                    *(['last_name', 'first_name', 'middle_name'] if model is Counterparty else [])]]))
    if legal_form:
        stmt = stmt.where(model.legal_form == legal_form)
    no_inn = _missing(model.inn)
    selected = {'inn': no_inn} if inn else {}
    groups = {'inn': {'no_inn': no_inn}}
    if model is Counterparty:
        if party_type:
            selected['type'] = model.type == party_type
        groups['type'] = {'all_types': true(), 'person': model.type == PERSON, 'legal': model.type == 'Юридическое лицо'}
    stmt, facets = _with_facets(db, stmt, selected, groups)
    result = _page(db, stmt, model, full_name if sort == 'full_name' else getattr(model, sort), direction, limit, offset)
    if model is Counterparty:
        result['items'] = [counterparty_response(row) for row in result['items']]
    return {**result, 'facets': facets}


PartySort = Literal['name', 'full_name', 'legal_form', 'inn']


@router.get('/counterparties/search', response_model=PartyPage)
def search_counterparties(db: Db, user: CurrentUser, request: Request, q: TextQuery = '',
                         legal_form: Annotated[str | None, Query(max_length=500, pattern=r'^[^\x00]*$')] = None, sort: PartySort = 'name',
                         direction: Direction = 'asc', limit: Limit = 100, offset: Offset = 0,
                         inn: Literal['missing'] | None = None,
                         party_type: Literal['Физическое лицо', 'Юридическое лицо'] | None = None):
    if 'status' in request.query_params:
        raise HTTPException(422, 'У CP нет фильтра полноты status')
    return _search_party(db, user, Counterparty, Permission.ARM2_CP_VIEW, q, legal_form, sort, direction, limit, offset,
                         inn, party_type)


@router.get('/organizations/search', response_model=PartyPage)
def search_organizations(db: Db, user: CurrentUser, request: Request, q: TextQuery = '',
                        legal_form: Annotated[str | None, Query(max_length=500, pattern=r'^[^\x00]*$')] = None, sort: PartySort = 'name',
                        direction: Direction = 'asc', limit: Limit = 100, offset: Offset = 0,
                        inn: Literal['missing'] | None = None):
    if 'status' in request.query_params:
        raise HTTPException(422, 'У ORG нет фильтра полноты status')
    return _search_party(db, user, Organization, Permission.ARM2_ORG_VIEW, q, legal_form, sort, direction, limit, offset,
                         inn)


@router.get('/types/search', response_model=TypePage)
def search_types(db: Db, user: CurrentUser, q: TextQuery = '', limit: Limit = 100, offset: Offset = 0,
                 status: Literal['REVIEW', 'CONFIRMED'] | None = None):
    check_permission(user, Permission.ARM2_TYPE_VIEW)
    stmt = select(*[getattr(VehicleType, field).label(field) for field in VehicleTypeResponse.model_fields])
    if q.strip():
        stmt = stmt.where(_text_match(VehicleType.name, q.strip()))
    selected = {'status': VehicleType.status == status} if status else {}
    stmt, facets = _with_facets(db, stmt, selected, {'status': {
        'all_statuses': true(), 'review': VehicleType.status == 'REVIEW', 'confirmed': VehicleType.status == 'CONFIRMED',
    }})
    return {**_page(db, stmt, VehicleType, VehicleType.name, 'asc', limit, offset), 'facets': facets}


@router.get('/document-kinds/search', response_model=DocumentKindPage)
def search_document_kinds(db: Db, user: CurrentUser, q: TextQuery = '', limit: Limit = 100, offset: Offset = 0,
                          parent: Literal['CP', 'ORG', 'VEH'] | None = None):
    check_permission(user, Permission.ARM2_DOC_KIND_VIEW)
    stmt = select(*[getattr(DocumentKind, field).label(field) for field in DocumentKindResponse.model_fields])
    if q.strip():
        stmt = stmt.where(_text_match(DocumentKind.name, q.strip()))
    parents = {key: DocumentKind.applicable_object_types.contains([key]) for key in ('CP', 'ORG', 'VEH')}
    selected = {'parent': parents[parent]} if parent else {}
    stmt, facets = _with_facets(db, stmt, selected, {'parent': {'all_parents': true(), **parents}})
    return {**_page(db, stmt, DocumentKind, DocumentKind.id, 'asc', limit, offset), 'facets': facets}
