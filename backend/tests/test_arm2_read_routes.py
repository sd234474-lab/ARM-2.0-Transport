import pytest
from app.arm2.counterparty_models import Counterparty
from app.arm2.history_models import HistoryEvent
from app.arm2.organization_models import Organization
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_type_models import VehicleType
from app.models import User
from sqlalchemy import func, select


@pytest.fixture
def catalog(db):
    db.add_all([Counterparty(id='CP-000001', name='Партнёр', full_name='Полное имя', legal_form='ООО', status='REVIEW'),
                Organization(id='ORG-000001', name='Своя организация', full_name='Полное ORG', legal_form='ИП', status='REVIEW'),
                VehicleType(id='TYPE-000001', name='Самосвал', requires_sts=True, requires_calibration=False,
                            requires_wialon=True, status='CONFIRMED')])
    db.flush()
    db.add_all([Vehicle(id=f'VEH-{i:06}', name=f'Машина {i:03}', plate_number=f'А{i:03}ВС86',
                        type_id='TYPE-000001' if i % 2 else None, owner_cp_id='CP-000001' if i % 3 == 1 else None,
                        owner_org_id='ORG-000001' if i % 3 == 2 else None,
                        accounting_state='Действующая' if i % 2 else 'Архивная') for i in range(1, 106)])
    db.commit()


def test_full_search_pagination_and_legacy_contract(admin, catalog, db):
    assert isinstance(admin.get('/api/arm2/vehicles?limit=100').json(), list)
    assert len(admin.get('/api/arm2/vehicles?limit=100').json()) == 100
    first = admin.get('/api/arm2/vehicles/search').json()
    assert (first['total'], first['matched'], len(first['items'])) == (105, 105, 100)
    second = admin.get('/api/arm2/vehicles/search?offset=100').json()
    assert len(second['items']) == 5
    assert second['items'][0]['id'] == 'VEH-000101'
    for query in ['Машина 105', 'a 105 bc 86', 'А105ВС86']:
        response = admin.get('/api/arm2/vehicles/search', params={'q': query})
        assert response.status_code == 200
        assert [row['id'] for row in response.json()['items']] == ['VEH-000105']
    assert admin.get('/api/arm2/vehicles/search?q=%25').json()['matched'] == 0
    assert admin.get('/api/arm2/vehicles/search?q=missing').json()['items'] == []
    assert db.scalar(select(func.count()).select_from(HistoryEvent)) == 0


def test_filters_owner_type_state_and_sort(admin, catalog):
    for kind, id, field in [('CP', 'CP-000001', 'owner_cp_id'), ('ORG', 'ORG-000001', 'owner_org_id')]:
        result = admin.get('/api/arm2/vehicles/search', params={'owner_kind': kind, 'owner_id': id}).json()
        assert result['matched'] == 35 and all(row[field] == id for row in result['items'])
        assert all(row['owner_name'] for row in result['items'])
    empty = admin.get('/api/arm2/vehicles/search?owner_kind=NONE').json()
    assert empty['matched'] == 35 and all(not row['owner_cp_id'] and not row['owner_org_id'] for row in empty['items'])
    typed = admin.get('/api/arm2/vehicles/search?type_id=TYPE-000001&state=Действующая').json()
    assert typed['matched'] == 53 and all(row['type_name'] == 'Самосвал' for row in typed['items'])
    descending = admin.get('/api/arm2/vehicles/search?sort=plate_number&direction=desc').json()
    assert descending['items'][0]['id'] == 'VEH-000105'


@pytest.mark.parametrize('query', ['owner_id=CP-000001', 'owner_kind=CP&owner_id=ORG-000001',
                                  'owner_kind=NONE&owner_id=CP-000001', 'type_id=wrong', 'state=READY',
                                  'sort=secret', 'limit=101', 'offset=-1', 'direction=wrong'])
def test_invalid_query_is_rejected(admin, catalog, query):
    assert admin.get('/api/arm2/vehicles/search?' + query).status_code == 422


def test_party_read_models_and_type_search(admin, catalog):
    for path, full_name, form in [('counterparties', 'Полное имя', 'ООО'), ('organizations', 'Полное ORG', 'ИП')]:
        result = admin.get('/api/arm2/' + path + '/search', params={'q': full_name, 'legal_form': form, 'status': 'REVIEW'}).json()
        assert result['total'] == result['matched'] == 1
        assert result['items'][0]['full_name'] == full_name and result['items'][0]['legal_form'] == form
        assert admin.get('/api/arm2/' + path + '/search?status=READY').status_code == 422
    assert admin.get('/api/arm2/types/search?q=Самосвал').json()['matched'] == 1


def test_read_permissions_do_not_leak_related_names(client, users, db, catalog):
    operator = db.scalar(select(User).where(User.login == 'operator'))
    operator.permissions = ['arm2_veh_view']
    db.commit()
    response = client.post('/api/auth/login', json={'login': 'operator', 'password': 'test-password-123'})
    assert response.status_code == 200
    result = client.get('/api/arm2/vehicles/search')
    assert result.status_code == 200
    assert all(row['type_name'] is None and row['owner_name'] is None for row in result.json()['items'])
    assert client.get('/api/arm2/counterparties/search').status_code == 403
    assert client.get('/api/arm2/organizations/search').status_code == 403
    assert client.get('/api/arm2/types/search').status_code == 403
    assert client.get('/api/arm2/vehicles/search?owner_kind=CP').status_code == 403
    assert client.get('/api/arm2/vehicles/search?type_id=TYPE-000001').status_code == 403


def test_anonymous_search_requires_login(client):
    for kind in ['vehicles', 'counterparties', 'organizations', 'types']:
        assert client.get('/api/arm2/' + kind + '/search').status_code == 401
