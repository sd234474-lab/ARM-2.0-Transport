"""Synthetic-only visual fixtures saved through existing ARM2 services."""
import json

from arm2_b1_check import LOCAL, config, guard

guard()

from app.arm2.counterparty_store import create_counterparty, update_counterparty  # noqa: E402
from app.arm2.object_save import SaveContext  # noqa: E402
from app.arm2.organization_store import create_organization, update_organization  # noqa: E402
from app.arm2.representative_store import create_representative  # noqa: E402
from app.arm2.vehicle_models import Vehicle  # noqa: E402
from app.arm2.vehicle_store import create_vehicle  # noqa: E402
from app.arm2.vehicle_type_store import create_vehicle_type  # noqa: E402
from app.auth import hasher  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import User  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

with Session(engine(), expire_on_commit=False) as db:
    if db.scalar(select(Vehicle.id).limit(1)):
        raise RuntimeError('Refusing to reseed nonempty B1 visual database')
    admin = db.scalar(select(User).where(User.login == 'b1-admin'))
    assert admin
    viewer = User(login='b1-viewer', name='B1 synthetic viewer', active=True, role='OPERATOR',
                  password_hash=hasher.hash(config()['admin_password']), permissions=['arm2_veh_view'])
    db.add(viewer)
    db.flush()
    context = SaveContext('HUMAN', str(admin.id), 'b1-synthetic-fixtures')
    types = [create_vehicle_type(db, name, requires_sts=True, requires_calibration=calibration,
                                 requires_wialon=wialon, principal=admin, context=context)
             for name, calibration, wialon in [('Самосвал', True, True), ('Погрузчик', False, True), ('Тип без описания', None, None)]]
    cps = [create_counterparty(db, f'Контрагент {i:03}', full_name=f'ООО Синтетический контрагент {i:03}',
                               legal_form='ООО', legal_address='Тестовый адрес', phone='000-00-00',
                               email='test@example.invalid', principal=admin, context=context) for i in range(1, 111)]
    orgs = [create_organization(db, f'Организация {i:03}', full_name=f'Синтетическая организация {i:03}',
                                legal_form='ООО', legal_address='Тестовый адрес', principal=admin, context=context)
            for i in range(1, 106)]
    reps = {}
    for kind, parent, update in [('CP', cps[0], update_counterparty), ('ORG', orgs[0], update_organization)]:
        people = [create_representative(db, kind, parent.id, name, position='Тестовый представитель',
                                       authority_basis='Тестовое основание', phone='000-00-01', email='rep@example.invalid',
                                       state='Архивный' if index == 2 else 'Действующий', user_id=viewer.id,
                                       last_name='СохранённаяФамилия', first_name='СохранённоеИмя', middle_name='СохранённоеОтчество',
                                       principal=admin, context=context)
                  for index, name in enumerate(['Иванов Иван Иванович', 'Эдуард', 'Анна-Мария де Соуза'])]
        update(db, parent.id, parent.version, parent.name, primary_signer_id=people[0].id, principal=admin, context=context)
        reps[kind] = people[0].id
    vehicles = [create_vehicle(db, f'Машина {i:03}' if i != 130 else 'Машина за сотой 130', f'А{i:03}ВС86',
                               type_id=types[0].id if i % 5 else None,
                               accounting_state='Архивная' if i % 10 == 0 else 'Действующая', model='Тестовая модель',
                               owner_cp_id=cps[0].id if i % 3 == 1 else None,
                               owner_org_id=orgs[0].id if i % 3 == 2 else None,
                               principal=admin, context=context) for i in range(1, 131)]
    db.commit()
    evidence = {'cp': cps[0].id, 'org': orgs[0].id, 'vehicle': vehicles[0].id, 'beyond100': vehicles[-1].id,
                'type': types[0].id, 'representatives': reps, 'counts': {'vehicles': 130, 'counterparties': 110, 'organizations': 105}}
    (LOCAL / 'evidence/seed.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
print('Synthetic B1 fixtures saved with ID/version/history; no original user data used')
