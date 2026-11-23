"""Live, permission-aware questionnaire comparison; never writes domain objects."""
import math
import posixpath
import re
import unicodedata

from sqlalchemy import or_, select

from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_rules import LEGAL, PERSON, inapplicable_fields, official_names, transition_clears
from app.arm2.counterparty_store import COUNTERPARTY_ADAPTER, PARTY_FIELDS
from app.arm2.document_file_models import DocumentFile
from app.arm2.document_file_store import revision_files
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.object_save import ObjectAuthorizationError
from app.arm2.organization_models import Organization
from app.arm2.questionnaire_models import QuestionnaireUpload
from app.arm2.representative_models import CounterpartyRepresentative
from app.arm2.representative_store import CPREP_ADAPTER, representative_values
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import VEHICLE_ADAPTER, normalize_plate_number
from app.arm2.vehicle_type_models import VehicleType
from app.arm2.vehicle_type_rules import plate_compatible
from app.models import User
from app.permissions import Permission, has_permission

CP_FIELDS = (*PARTY_FIELDS[:-1], 'type', 'last_name', 'first_name', 'middle_name')
REP_FIELDS = ('name', 'position', 'authority_basis', 'phone', 'email', 'state')
VEH_FIELDS = ('name', 'model', 'type_id', 'accounting_state', 'owner')
VEH_EDIT_FIELDS = ('name', 'model', 'accounting_state')


def authorized(db, principal, *permissions):
    user = db.get(User, principal.id, populate_existing=True)
    if user is None or not user.active or user.deleted_at is not None or any(
            not has_permission(user, Permission(p)) for p in permissions):
        raise ObjectAuthorizationError('Нет действующих прав на выбранные изменения')
    return user


def owner(record):
    return ['CP', record.owner_cp_id] if record and record.owner_cp_id else \
        ['ORG', record.owner_org_id] if record and record.owner_org_id else None


def changes(values, current, selected, errors=None):
    return {field: {'current': getattr(current, field, None) if current else None, 'proposed': value,
                    'selected': field in selected, 'error': (errors or {}).get(field),
                    'equal': current is not None and getattr(current, field, None) == value}
            for field, value in values.items() if value not in (None, '')}


def build(db, principal, draft):
    user = authorized(db, principal, 'arm2_cp_view')
    payload, choices = draft.payload, draft.choices
    cp_edits = choices.get('cp_edits', {})
    cp_values = {**payload.get('cp', {}), **choices.get('cp_values', {}), **cp_edits}
    target = choices.get('cp_target')
    cp = db.get(Counterparty, target, populate_existing=True) if target and target != 'new' else None
    cp_error = None if target == 'new' or cp is not None else 'Выберите существующего или нового контрагента'
    candidates = []
    cp_hint = payload.get('cp_hint')
    if cp_values.get('inn') or cp_values.get('name') or cp_hint:
        candidates = [{'id': r.id, 'name': r.name, 'inn': r.inn} for r in db.scalars(select(Counterparty).where(or_(
            Counterparty.inn == cp_values.get('inn') if cp_values.get('inn') else False,
            Counterparty.name == cp_values.get('name'), Counterparty.id == cp_hint,
            Counterparty.name == cp_hint)).order_by(Counterparty.id).limit(100))]
    if target == 'new' and candidates:
        cp_error = 'Найден совпадающий контрагент; выберите существующую карточку вместо создания дубля'
    cp_errors, normalized = dict(payload.get('cp_errors', {})), {}
    base = {f: getattr(cp, f) if cp else None for f in (*PARTY_FIELDS, 'type', 'last_name', 'first_name', 'middle_name')}
    base['name'] = base['name'] or cp_values.get('name') or 'Анкета'
    proposed_type = cp_values.get('type') or base.get('type')
    normalization_base = {**base, 'type': proposed_type or LEGAL,
                          'legal_form': cp_values.get('legal_form') or base.get('legal_form')}
    if 'legal_form' in choices.get('cp_clears', []):
        normalization_base['legal_form'] = None
    notices = []
    if proposed_type == PERSON:
        names = official_names({**base, **cp_values, 'type': PERSON})
        for field, name in zip(('full_name', 'short_name'), names, strict=True):
            if cp_values.get(field) and cp_values[field] != name:
                notices.append(f'{field}: представление ФЛ вычисляется из ФИО, независимое имя не принимается')
        cp_values = {k: v for k, v in cp_values.items() if k not in ('full_name', 'short_name')}
    if not cp_values.get('type') and target == 'new':
        normalized['type'] = ''
        cp_errors['type'] = cp_errors.get('type') or 'Для нового CP явно выберите тип'
    for field in {*choices.get('cp_values', {}), *cp_edits}:
        cp_errors.pop(field, None)
    for field in CP_FIELDS:
        value = cp_values.get(field)
        if not value:
            continue
        try:
            # An invalid, UNSELECTED type/OPF proposal must not poison an unrelated
            # phone/bank/etc. field. Final SAVE still validates the complete chosen state.
            validation_base = normalization_base
            if field not in ('type', 'legal_form'):
                validation_base = {**base, 'type': base.get('type') or LEGAL}
                if validation_base['type'] == PERSON and validation_base.get('legal_form') not in (None, 'ИП'):
                    validation_base['legal_form'] = None
            normalized[field] = COUNTERPARTY_ADAPTER.normalize({**validation_base, field: value})[field]
            if field in inapplicable_fields(normalization_base) and value != base.get(field):
                cp_errors[field] = 'Реквизит неприменим к предложенному типу контрагента; оставьте без изменения'
            if field == 'inn' and db.scalar(select(Counterparty.id).where(Counterparty.inn == normalized[field],
                                                Counterparty.id != (cp.id if cp else ''))) is not None:
                cp_errors[field] = 'ИНН уже принадлежит другой карточке; выберите её'
        except ValueError as error:
            normalized[field], cp_errors[field] = value, str(error)
    effective = {**base, **{f: normalized[f] for f in choices.get('cp_fields', []) if f in normalized},
                 **{f: None for f in choices.get('cp_clears', [])}}
    candidates_clear = transition_clears(base if cp else None, effective)
    if cp and cp.legal_form:
        candidates_clear['legal_form'] = cp.legal_form
    cp_fields = changes(normalized, cp, choices.get('cp_fields', []), cp_errors)
    if 'type' in cp_errors and 'type' not in cp_fields:
        cp_fields['type'] = {'current': cp.type if cp else None, 'proposed': cp_values.get('type'),
                             'selected': False, 'equal': False, 'error': cp_errors['type']}
    for field in set(candidates_clear) | set(choices.get('cp_clears', [])):
        current = getattr(cp, field, None) if cp else None
        cleared = field in choices.get('cp_clears', [])
        item = cp_fields.setdefault(field, {'current': current, 'proposed': None, 'equal': current is None,
                                           'selected': False, 'error': None})
        item.update(clear_candidate=True, clear_selected=cleared, reason='Явная очистка; пустота анкеты не удаляет значение')
        if cleared:
            item.update(proposed=None, selected=True, equal=current is None, error=None)
    if cp and 'name' in cp_fields and not cp_fields['name']['equal'] and 'name' not in cp_edits:
        cp_fields['name']['error'] = 'Рабочее имя существующего CP сохраняется; изменяйте его вручную в карточке'
    reasons = []
    if cp:
        if cp_hint and cp_hint not in (cp.id, cp.name):
            hinted = db.get(Counterparty, cp_hint) if re.fullmatch(r'CP-\d{6,}', cp_hint) else None
            reasons.append({'field': 'counterparty', 'label': 'Контрагент', 'current': cp.name,
                            'proposed': hinted.name if hinted else 'В анкете указан другой контрагент'})
        if cp_values.get('inn') and cp.inn and cp_values['inn'] != cp.inn:
            reasons.append({'field': 'inn', 'label': 'ИНН', 'current': cp.inn, 'proposed': cp_values['inn']})
        for field, label in (('type', 'Тип контрагента'), ('legal_form', 'Организационно-правовая форма')):
            if effective.get(field) != getattr(cp, field):
                reasons.append({'field': field, 'label': label, 'current': getattr(cp, field), 'proposed': effective.get(field)})
    conflict = bool(reasons)
    cp_plan = {'key': 'CP', 'id': cp.id if cp else None, 'target': target, 'version': cp.version if cp else None,
               'error': cp_error, 'candidates': candidates, 'hint': payload.get('cp_hint'),
               'identity_conflict': conflict, 'identity_conflict_reasons': reasons,
               'name': cp.name if cp else cp_values.get('name'), 'identity_confirmed': bool(choices.get('confirm_identity')),
               'notices': notices, 'fields': cp_fields}
    reps = []
    for index, proposed in enumerate(payload.get('representatives', [])):
        choice = choices.get('representatives', {}).get(str(index), {})
        meta = (payload.get('representative_meta') or [{}] * len(payload.get('representatives', [])))[index]
        rep_id = choice.get('target')
        signed_rep = next((r for r in (payload.get('manifest') or {}).get('representatives', [])
                           if r['index'] == index and cp and r['parent_id'] == cp.id), None)
        hint = meta.get('id') if 'representative_meta' in payload else signed_rep['id'] if signed_rep else None
        lookup_id = rep_id if rep_id and rep_id != 'new' else hint if cp else None
        rep = db.get(CounterpartyRepresentative, lookup_id, populate_existing=True) if lookup_id else None
        error = None
        if hint and cp and (not re.fullmatch(r'CPREP-\d{6,}', hint) or
                db.scalar(select(CounterpartyRepresentative.id).where(CounterpartyRepresentative.id == hint,
                    CounterpartyRepresentative.parent_id == cp.id)) is None):
            error = 'REP-ID не принадлежит выбранному CP или отсутствует; сопоставьте исходные данные'
        if hint and rep_id == 'new':
            error = 'Строка уже содержит REP-ID; выберите существующую запись, не создавайте дубль'
        if hint and sum(m.get('id') == hint for m in payload.get('representative_meta', [])) > 1:
            error = 'Повтор REP-ID в листе представителей'
        if not rep_id and rep and (cp is None or rep.parent_id != cp.id):
            rep = None
        if rep_id and rep_id != 'new' and (rep is None or cp is None or rep.parent_id != cp.id):
            error = 'Представитель не принадлежит выбранному CP'
        rep_values, rep_errors = {}, {}
        base_rep = representative_values(cp.id if cp else '$CP', rep.name if rep else proposed.get('name') or 'Представитель',
            **{f: getattr(rep, f) for f in ('position', 'authority_basis', 'phone', 'email', 'state', 'user_id', 'last_name', 'first_name', 'middle_name')} if rep else {})
        conflicts = meta.get('conflicts', {})
        resolved = {**proposed, **{field: alternatives[choice.get('sources', {}).get(field)]
            for field, alternatives in conflicts.items() if choice.get('sources', {}).get(field) in alternatives}}
        edits = choice.get('edits', {})
        resolved.update(edits)
        for field, value in resolved.items():
            if field not in REP_FIELDS:
                continue
            if not value:
                continue
            try:
                rep_values[field] = CPREP_ADAPTER.normalize({**base_rep, field: value})[field]
            except ValueError as failure:
                rep_values[field], rep_errors[field] = value, str(failure)
        for field in conflicts:
            if field not in edits and choice.get('sources', {}).get(field) not in {'passport', 'list'}:
                rep_errors[field] = 'Паспорт и список различаются: явно выберите источник значения'
        reps.append({'key': str(index), 'id': rep.id if rep else None, 'target': rep_id,
            'version': rep.version if rep else None, 'error': error, 'signer': bool(choice.get('signer')),
            'source_id': hint, 'source_row': meta.get('row'), 'signer_hint': bool(meta.get('signer_hint')),
            'source_comment': meta.get('comment'),
            'conflicts': conflicts, 'sources': choice.get('sources', {}),
            'fields': changes(rep_values, rep, choice.get('fields', []), rep_errors),
            'candidates': [{'id': r.id, 'name': r.name} for r in db.scalars(select(CounterpartyRepresentative).where(
                CounterpartyRepresentative.parent_id == cp.id).order_by(CounterpartyRepresentative.id))] if cp else []})
    rows, seen = [], {}
    type_view = has_permission(user, Permission.ARM2_TYPE_VIEW)
    veh_view = has_permission(user, Permission.ARM2_VEH_VIEW)
    for raw in payload.get('vehicles', []):
        choice = choices.get('vehicles', {}).get(raw['key'], {})
        errors = [raw['status_error']] if raw.get('status_error') else []
        current, plate = None, None
        try:
            if not veh_view:
                raise ValueError('Нет прав просмотра техники')
            plate = normalize_plate_number(re.sub(r'\s+', '', raw['plate_number']))
            by_plate = db.scalar(select(Vehicle).where(Vehicle.plate_number == plate).execution_options(populate_existing=True))
            by_id = db.get(Vehicle, raw['id'], populate_existing=True) if raw['id'] else None
            if by_id and by_id.plate_number != plate:
                raise ValueError('VEH-ID и госномер не совпадают; исправьте исходную строку')
            current = by_id or by_plate
            if current is None and raw.get('state_hint') == 'ВЫБЫЛО':
                raise ValueError('Машина не найдена: снять связь владельца по отметке ВЫБЫЛО невозможно')
            if plate in seen:
                errors.append('Повтор госномера в строках анкеты')
                seen[plate]['errors'].append('Повтор госномера в строках анкеты')
        except ValueError as error:
            errors.append(str(error))
        proposed = {f: raw[f] for f in ('name', 'model') if raw.get(f)}
        typ, type_error = None, None
        hint = raw.get('type_hint')
        type_choice = choice.get('type_id')
        if type_view and (hint or type_choice):
            typ = db.get(VehicleType, type_choice, populate_existing=True) if type_choice else \
                db.scalar(select(VehicleType).where(or_(VehicleType.id == hint, VehicleType.name == hint)))
            if choice.get('without_type'):
                # An incomplete historic link is never removed by a blanket "without TYPE" choice.
                typ = None
            elif typ is None:
                type_error = 'TYPE не распознан: выберите CONFIRMED или явно сохраните без TYPE'
            elif current is not None and current.type_id == typ.id:
                proposed['type_id'] = typ.id
            elif typ.status != 'CONFIRMED' or not plate_compatible(plate or '', typ.plate_category):
                type_error = 'Назначить можно только CONFIRMED TYPE с подходящей категорией номера'
            else:
                proposed['type_id'] = typ.id
        elif hint and not type_view and not choice.get('without_type'):
            type_error = 'Нет прав просмотра TYPE; можно явно оставить без нового назначения'
        if type_error:
            proposed['type_id'] = hint or type_choice
        withdrawal = raw.get('state_hint', '').upper().strip() == 'ВЫБЫЛО'
        actual_owner = owner(current)
        expected_owner = ['CP', cp.id] if cp else ['CP', '$CP'] if target == 'new' else None
        next_owner = (None if actual_owner == expected_owner else actual_owner) if withdrawal else expected_owner
        if current is not None and current.accounting_state == 'Архивная' and not withdrawal:
            proposed['accounting_state'] = 'Действующая'
        field_errors = {'type_id': type_error} if type_error else {}
        for field, value in choice.get('edits', {}).items():
            try:
                base_vehicle = {key: getattr(current, key, None) for key in (
                    'name', 'plate_number', 'type_id', 'accounting_state', 'model',
                    'wialon_unit_id', 'owner_cp_id', 'owner_org_id',
                )}
                base_vehicle['name'] = base_vehicle['name'] or plate or 'Машина'
                base_vehicle['plate_number'] = plate or ''
                base_vehicle['accounting_state'] = base_vehicle['accounting_state'] or 'Действующая'
                proposed[field] = VEHICLE_ADAPTER.normalize({**base_vehicle, field: value})[field]
                if withdrawal:
                    field_errors[field] = 'Для выбывшей машины можно только снять связь с владельцем'
            except ValueError as failure:
                proposed[field], field_errors[field] = value, str(failure)
        fields = changes(proposed, current, choice.get('fields', []), field_errors)
        fields['owner'] = {'current': actual_owner, 'proposed': next_owner, 'selected': 'owner' in choice.get('fields', []),
                           'equal': actual_owner == next_owner, 'error': None if expected_owner else 'Сначала выберите CP'}
        def owner_name(value):
            if not value:
                return 'Не назначен'
            if value == ['CP', '$CP']:
                return cp_values.get('name') or 'Создаваемый контрагент'
            model = Counterparty if value[0] == 'CP' else Organization
            if not has_permission(user, Permission('arm2_' + value[0].lower() + '_view')):
                return 'Другой владелец'
            record = db.get(model, value[1])
            return record.name if record else 'Прежний владелец'
        fields['owner'].update(current_display=owner_name(actual_owner), proposed_display=owner_name(next_owner))
        if 'type_id' in fields:
            old_type = db.get(VehicleType, current.type_id) if current and current.type_id and type_view else None
            fields['type_id'].update(current_display=old_type.name if old_type else 'Не указан',
                                     proposed_display=typ.name if typ else 'Требуется выбор типа или сохранение без типа')
        plan = {'key': raw['key'], 'row': raw['row'], 'id': current.id if current else None,
                'plate_number': plate, 'version': current.version if current else None, 'fields': fields, 'errors': errors,
                'withdrawal': withdrawal, 'confirm_owner': bool(choice.get('confirm_owner')),
                'without_type': bool(choice.get('without_type')), 'source': f'Техника!{raw["row"]}',
                'type_version': typ.version if typ else None,
                'volume_proposal': raw.get('volume_proposal'), 'volume_notice': 'Только сверка по справке; объём и согласование не записываются',
                'legacy_hint': raw.get('legacy_hint'), 'diagnostic_hint': raw.get('diagnostic_hint')}
        rows.append(plan)
        if plate:
            seen[plate] = plan
    attachments = list(db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft.id)
                                 .order_by(QuestionnaireUpload.name, QuestionnaireUpload.id)))
    by_name, by_stem = {}, {}
    def reference(name):
        return unicodedata.normalize('NFC', name.replace('\\', '/').strip()).casefold()
    for attachment in attachments:
        # Non-UTF8 ZIP headers are decoded as CP437 by ZipFile. Keep provenance,
        # but allow an EXACT cell match in the legacy codepages used by senders.
        # Multiple matching physical files still remain ambiguous; no ranking.
        variants = {attachment.name}
        try:
            raw_name = attachment.name.encode('cp437')
        except UnicodeEncodeError:
            raw_name = None
        if raw_name is not None:
            for encoding in ('cp866', 'cp1251'):
                try:
                    variants.add(raw_name.decode(encoding))
                except UnicodeDecodeError:
                    pass
        for name in {reference(n) for v in variants for n in (v, v.rsplit('/', 1)[-1])}:
            by_name.setdefault(name, []).append(attachment)
            stem, extension = posixpath.splitext(name)
            if extension:
                by_stem.setdefault(stem, []).append(attachment)
    declarations = [('company', 'CP', 'Карточка предприятия', payload.get('company_file'))]
    for row in payload.get('vehicles', []):
        declarations.extend([(row['key'] + ':sts', row['key'], 'СТС', row.get('sts_file')),
                             (row['key'] + ':calibration', row['key'], 'Тарировка', row.get('calibration_file'))])
    documents, bound = [], set()
    automatic, automatic_claims = {}, {}
    for key, _, _, name in declarations:
        if not name:
            continue
        ref = reference(name)
        matches = by_name.get(ref, [])
        if not matches:
            # Exact whole cell equals the physical name minus ONE extension.
            # No punctuation stripping, similarity score, parent/kind inference.
            matches = by_stem.get(ref, [])
        automatic[key] = list({a.id: a for a in matches}.values())
        if len(automatic[key]) == 1:
            automatic_claims.setdefault(automatic[key][0].id, []).append(key)
    resolved = {}
    for key, matches in automatic.items():
        override = choices.get('documents', {}).get(key, {}).get('upload_id')
        # A now-unambiguous declared reference also repairs an earlier manual
        # workaround for the missing-extension bug. view()/apply() invalidate
        # the old approval when the physical upload/SHA changes; never auto-save.
        unique = len(matches) == 1 and len(automatic_claims[matches[0].id]) == 1
        resolved[key] = matches if unique or not override else [a for a in attachments if a.id == override]
    claimed = {}
    for key, matches in resolved.items():
        if len(matches) == 1:
            claimed.setdefault(matches[0].id, []).append(key)
    for key, parent_key, role, name in sorted(declarations, key=lambda d: not choices.get('documents', {}).get(d[0], {}).get('accept')):
        if not name:
            continue
        matches = resolved[key]
        if len(matches) == 1:
            attachment = matches[0]
            competing = claimed[attachment.id]
            explicit = [k for k in competing if choices.get('documents', {}).get(k, {}).get('accept')]
            collision = len(competing) > 1 and explicit != [key]
            if attachment.id in bound:
                documents.append({'key': key, 'name': name, 'parent_key': parent_key, 'role': role,
                                  'selected': bool(choices.get('documents', {}).get(key, {}).get('accept')),
                                  'error': 'Файл уже указан для другой строки; выберите отдельное вложение',
                                  'attachment_options': [{'id': a.id, 'name': a.name} for a in attachments]})
                continue
            bound.add(attachment.id)
            plan = document_plan(db, user, draft, attachment, key, parent_key, role, cp, rows)
            if collision:
                plan['error'] = 'Один файл указан несколько раз; явно примите только одну привязку или выберите другой файл'
            documents.append(plan)
        else:
            documents.append({'key': key, 'name': name, 'parent_key': parent_key, 'role': role,
                              'selected': bool(choices.get('documents', {}).get(key, {}).get('accept')),
                              'error': 'Файл отсутствует' if not matches else 'Имя неоднозначно; выберите файл в непривязанных',
                              'attachment_options': [{'id': a.id, 'name': a.name} for a in attachments]})
    for attachment in attachments:
        if attachment.id not in bound:
            signed = next((f for f in (payload.get('manifest') or {}).get('files', []) if f['name'] == attachment.name), None)
            parent_key = None
            if signed and cp and signed['parent_type'] == 'CP' and signed['parent_id'] == cp.id:
                parent_key = 'CP'
            elif signed and signed['parent_type'] == 'VEH':
                parent_key = next((r['key'] for r in rows if r['id'] == signed['parent_id']), None)
            documents.append(document_plan(db, user, draft, attachment, 'unbound:' + attachment.id, parent_key, 'Непривязанный файл', cp, rows))
    kinds = [{'id': k.id, 'name': k.name, 'parents': k.applicable_object_types, 'version': k.version} for k in db.scalars(
        select(DocumentKind).order_by(DocumentKind.id))] if (has_permission(user, Permission.ARM2_DOCUMENT_VIEW)
            and has_permission(user, Permission.ARM2_DOC_KIND_VIEW)) else []
    calibration_ids = {k['id'] for k in kinds if k['name'].strip().casefold() == 'тарировка'}
    for row in rows:
        if row['withdrawal']:
            continue
        attached = [doc for doc in documents if doc.get('kind_id') in calibration_ids and doc.get('upload_id')
                    and doc['parent_key'] in (row['key'], row['id']) and doc.get('parent_type') == 'VEH']
        if attached:
            selected = [doc for doc in attached if doc['selected'] and not doc['error']]
            targets = {(doc.get('document_id'), doc['kind_id']) for doc in selected}
            volume = choices.get('vehicles', {}).get(row['key'], {}).get('volume')
            source = volume if volume is not None else row['volume_proposal']
            numeric, error = None, 'В анкете объём не указан' if not source else None
            if source:
                try:
                    numeric = float(source.strip().replace(',', '.'))
                    if not math.isfinite(numeric) or numeric <= 0:
                        raise ValueError()
                except ValueError:
                    numeric, error = None, 'Укажите положительный объём в м³'
            vehicle = db.get(Vehicle, row['id']) if row['id'] else None
            row['calibration'] = {'value': source or '', 'current': vehicle.calibration_m3 if vehicle else None,
                                  'numeric': numeric, 'error': error, 'selected': volume is not None,
                                  'document_keys': [d['key'] for d in selected] if len(targets) == 1 else []}
    types = [{'id': t.id, 'name': t.name, 'plate_category': t.plate_category} for t in db.scalars(
        select(VehicleType).where(VehicleType.status == 'CONFIRMED').order_by(VehicleType.id))] if type_view else []
    return {'cp': cp_plan, 'representatives': reps, 'vehicles': rows, 'documents': documents,
            'document_kind_notice': (None if kinds else 'Нет доступных видов документов. Администратор должен проверить права и подготовить виды «Карточка предприятия», «СТС», «Тарировка» в справочнике видов документов до импорта.'),
            'document_kinds': kinds, 'types': types, 'warnings': payload.get('warnings', []),
            'filled_date': draft.filled_date.isoformat() if draft.filled_date else None,
            'uploaded_at': draft.uploaded_at.isoformat()}


def document_plan(db, user, draft, attachment, key, parent_key, role, cp, rows):
    choice = draft.choices.get('documents', {}).get(key, {})
    parent_key = (choice.get('parent_key') or parent_key) if key.startswith('unbound:') else parent_key
    row = next((r for r in rows if r['key'] == parent_key), None)
    ptype, pid = ('CP', cp.id if cp else None) if parent_key == 'CP' else ('VEH', row['id'] if row else None)
    if parent_key and parent_key.startswith('VEH-') and has_permission(user, Permission.ARM2_VEH_VIEW):
        target = db.get(Vehicle, parent_key)
        pid = target.id if target else None
    allowed = has_permission(user, Permission.ARM2_DOCUMENT_VIEW) and has_permission(user, Permission('arm2_' + ptype.lower() + '_view'))
    manifest = draft.payload.get('manifest') or {}
    signed = next((f for f in manifest.get('files', []) if f['name'] == attachment.name and
                   f['parent_type'] == ptype and f['parent_id'] == pid), None)
    if signed:
        original = db.get(DocumentFile, signed['file_id'])
        if original is None or original.document_id != signed['document_id']:
            signed = None  # SHA or a signed filename alone cannot establish a different parent/DOCUMENT.
    doc_id = choice.get('document_id') or (signed['document_id'] if signed else None)
    doc = db.get(Document, doc_id, populate_existing=True) if doc_id and doc_id != 'new' and allowed else None
    error = attachment.error
    if not allowed:
        error = 'Нет прав на документы выбранного родителя'
    if not parent_key:
        error = error or 'Выберите допустимого родителя и тип документа'
    if doc_id and doc_id != 'new' and (doc is None or doc.parent_type != ptype or doc.parent_id != pid):
        error = 'DOCUMENT не принадлежит выбранному родителю'
    role_kinds = list(db.scalars(select(DocumentKind).where(DocumentKind.name == role))) if allowed and has_permission(user, Permission.ARM2_DOC_KIND_VIEW) else []
    role_kinds = [k for k in role_kinds if ptype in k.applicable_object_types and k.file_naming_template]
    kind_id = choice.get('kind_id') or (doc.kind_ref if doc else role_kinds[0].id if len(role_kinds) == 1 else None)
    kind = db.get(DocumentKind, kind_id) if kind_id else None
    if doc and kind_id != doc.kind_ref:
        error = error or 'Выбранный DOCUMENT относится к другому виду'
    if kind is None or ptype not in kind.applicable_object_types or not kind.file_naming_template:
        error = error or 'Выберите существующий применимый вид документа с шаблоном имени'
    candidates = [{'id': d.id, 'name': d.name, 'kind_id': d.kind_ref} for d in db.scalars(select(Document).where(
        Document.parent_type == ptype, Document.parent_id == pid, Document.archive_state == 'Неархивный').order_by(Document.id))] if pid and allowed else []
    if not doc and kind and any(c['kind_id'] == kind.id for c in candidates):
        error = error or 'У родителя уже есть документ этого вида; выберите его, не создавайте дубль'
    current = revision_files(db, doc.id, doc.current_revision_id) if doc else []
    same = any(f.sha256 == attachment.sha256 for f in current)
    replace = None if choice.get('append') else choice.get('replace_file_id') or (signed['file_id'] if signed else current[0].id if len(current) == 1 else None)
    if doc and len(current) > 1 and not same and not replace and not choice.get('append'):
        error = error or 'Выберите заменяемый FILE или явно добавьте файл к комплекту'
    if replace and replace not in {f.id for f in current}:
        error = error or 'Заменяемый FILE/редакция уже изменены'
    return {'key': key, 'upload_id': attachment.id, 'name': attachment.name, 'parent_key': parent_key, 'role': role,
            'parent_type': ptype, 'parent_id': pid, 'document_id': doc.id if doc else None,
            'kind_id': kind_id, 'version': doc.version if doc else None,
            'kind_version': kind.version if kind else None,
            'revision_id': doc.current_revision_id if doc else None, 'replace_file_id': replace,
            'append': bool(choice.get('append')), 'selected': bool(choice.get('accept')) and not same,
            'equal': same, 'error': error, 'sha256': attachment.sha256, 'media_type': attachment.media_type,
            'attachment_options': [{'id': a.id, 'name': a.name} for a in db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft.id))],
            'candidates': candidates, 'current_files': [{'id': f.id, 'name': f.original_upload_name, 'sha256': f.sha256} for f in current]}
