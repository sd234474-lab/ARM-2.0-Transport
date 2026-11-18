"""B5 transport/draft orchestration, private upload ledger and ONE atomic domain SAVE."""
import hashlib
import io
import secrets
import zipfile
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select

from app.arm2 import document_file_storage as files
from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_store import create_counterparty
from app.arm2.document_file_models import DocumentFile, DocumentFileCorrection
from app.arm2.document_file_store import (
    attempt_lease,
    publish_prepared_revision_in_transaction,
    revision_files,
)
from app.arm2.document_kind_models import DocumentKind
from app.arm2.document_models import Document
from app.arm2.history_models import HistoryEvent
from app.arm2.ids import next_id
from app.arm2.object_save import (
    ObjectAuthorizationError,
    ObjectVersionConflictError,
    SaveContext,
    update_object,
)
from app.arm2.questionnaire_diff import CP_FIELDS, REP_FIELDS, VEH_FIELDS, authorized, build
from app.arm2.questionnaire_models import (
    QuestionnaireDraft,
    QuestionnaireExport,
    QuestionnaireReceipt,
    QuestionnaireUpload,
)
from app.arm2.questionnaire_parse import (
    CP_CELLS,
    MANIFEST,
    MAX_COMPRESSED,
    MAX_ENTRIES,
    MAX_EXPANDED,
    PASSPORT,
    VEHICLES,
    fingerprint,
    parse,
    seal,
)
from app.arm2.representative_models import CounterpartyRepresentative
from app.arm2.representative_store import create_representative
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle, set_operational_owner
from app.arm2.vehicle_type_models import VehicleType
from app.models import LoginSession
from app.permissions import Permission, has_permission


def owned(db, user, identifier, *, open_only=True):
    authorized(db, user, 'arm2_cp_view')
    draft = db.scalar(select(QuestionnaireDraft).where(QuestionnaireDraft.id == identifier).with_for_update()
                      .execution_options(populate_existing=True))
    if draft is None or draft.author_id != user.id:
        raise ObjectAuthorizationError('Черновик доступен только его автору')
    if open_only and draft.status != 'OPEN':
        raise ObjectVersionConflictError('Разбор завершён; доступен только сохранённый протокол')
    return draft


def view(db, user, draft, *, reset_on_change=True):
    if draft.status != 'OPEN':
        receipt = db.scalar(select(QuestionnaireReceipt).where(QuestionnaireReceipt.draft_id == draft.id))
        return {'id': draft.id, 'revision': draft.revision, 'status': draft.status,
                'receipt': receipt.result if receipt else None}
    comparison = build(db, user, draft)
    approval_reset = False
    if reset_on_change and draft.preview and review_hash(draft.preview, draft.choices) != review_hash(comparison, draft.choices):
        # Never reuse an old owner/field/file approval against freshly changed server data.
        choices = draft.choices
        draft.choices = {**choices, 'cp_fields': [],
            'representatives': {k: {**v, 'fields': [], 'signer': False, 'target': None} for k, v in choices.get('representatives', {}).items()},
            'vehicles': {k: {**v, 'include': False, 'fields': [], 'confirm_owner': False} for k, v in choices.get('vehicles', {}).items()},
            'documents': {k: {**v, 'accept': False} for k, v in choices.get('documents', {}).items()}}
        draft.revision += 1
        draft.staging = {}
        comparison = build(db, user, draft)
        approval_reset = True
    draft.preview = comparison
    db.commit()  # Private review state only; no business object is touched.
    return {'id': draft.id, 'revision': draft.revision, 'status': draft.status, 'filename': draft.filename,
            'choices': draft.choices, 'approval_reset': approval_reset,
            'preview_id': review_hash(comparison, draft.choices), 'comparison': comparison}


def review_hash(comparison, choices):
    """Only chosen dependencies participate; unrelated broken/unselected rows cannot veto SAVE."""
    def item(plan):
        return {k: v for k, v in plan.items() if k not in ('candidates', 'hint', 'diagnostic_hint', 'legacy_hint', 'source', 'volume_notice')}
    cp = item(comparison['cp'])
    cp['fields'] = {k: f for k, f in cp['fields'].items() if f['selected']}
    reps = [item(r) for r in comparison['representatives'] if r['target']]
    docs = [d for d in comparison['documents'] if d.get('selected')]
    parent_keys = {d.get('parent_key') for d in docs}
    rows = [item(r) for r in comparison['vehicles'] if choices.get('vehicles', {}).get(r['key'], {}).get('include') or r['key'] in parent_keys]
    return fingerprint({'cp': cp, 'representatives': reps, 'vehicles': rows, 'documents': docs})


def upload(db, user, content, filename):
    authorized(db, user, 'arm2_cp_view')
    parsed, attachments = parse(content, filename)
    draft = QuestionnaireDraft(id=str(uuid4()), author_id=user.id, filename=files.original_name(filename),
        source_sha256=hashlib.sha256(content).hexdigest(), filled_date=date.fromisoformat(parsed['filled_date']) if parsed['filled_date'] else None,
        payload=parsed, choices={}, preview={}, staging={}, status='OPEN', revision=1)
    paths = []
    try:
        db.add(draft)
        db.flush()
        for name, data in attachments.items():
            token = secrets.token_hex(32)
            error, media, extension = None, None, None
            path = files.private_path(token)
            paths.append(path)
            # Even unsupported attachments remain visible errors; no nested ZIP is ever extracted.
            if len(data) > files.settings().max_file_bytes:
                error = 'Файл превышает штатный предел B3'
            else:
                path.write_bytes(data)
                try:
                    if data.startswith(b'PK'):
                        raise ValueError('Вложенный ZIP не распаковывается и не является СТС/тарировкой')
                    media, extension = files.content_type(path)
                    if extension not in {'pdf', 'jpg', 'png'}:
                        raise ValueError('Неподдерживаемый формат: только PDF/JPG/JPEG/PNG')
                except ValueError:
                    error = 'Неподдерживаемый формат: только проверенные PDF/JPG/JPEG/PNG'
            db.add(QuestionnaireUpload(id=token, draft_id=draft.id, name=name, size=len(data),
                sha256=hashlib.sha256(data).hexdigest(), media_type=media, extension=extension,
                error=error, state='STAGED', prepared={}))
        db.commit()
    except Exception:
        db.rollback()
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return view(db, user, draft)


def validate_choices(choices):
    if set(choices) - {'cp_target', 'cp_fields', 'representatives', 'vehicles', 'documents'}:
        raise ValueError('Неизвестное решение анкеты')
    if set(choices.get('cp_fields', [])) - set(CP_FIELDS):
        raise ValueError('Неизвестный реквизит CP')
    for rep in choices.get('representatives', {}).values():
        if set(rep) - {'target', 'fields', 'signer'} or set(rep.get('fields', [])) - set(REP_FIELDS):
            raise ValueError('Решение представителя содержит запрещённые реквизиты')
    for row in choices.get('vehicles', {}).values():
        if set(row) - {'fields', 'type_id', 'without_type', 'confirm_owner', 'include'} or set(row.get('fields', [])) - set(VEH_FIELDS):
            raise ValueError('Неизвестный реквизит VEH')
    for doc in choices.get('documents', {}).values():
        if set(doc) - {'parent_key', 'kind_id', 'document_id', 'replace_file_id', 'append', 'accept'}:
            raise ValueError('Неизвестное решение документа')


def edit(db, user, identifier, revision, choices):
    with attempt_lease(db, 'B5:' + identifier):
        draft = owned(db, user, identifier)
        if draft.revision != revision:
            raise ObjectVersionConflictError('Черновик изменён в другом окне; откройте его заново')
        if draft.preview and review_hash(build(db, user, draft), draft.choices) != review_hash(draft.preview, draft.choices):
            raise ObjectVersionConflictError('Живые карточки изменились; откройте сверку заново перед новым решением')
        validate_choices(choices)
        if draft.choices.get('cp_target') and choices.get('cp_target') != draft.choices.get('cp_target'):
            choices = {**choices, 'cp_fields': [],
                'representatives': {k: {**v, 'target': None, 'fields': [], 'signer': False} for k, v in choices.get('representatives', {}).items()},
                'vehicles': {k: {**v, 'fields': [f for f in v.get('fields', []) if f != 'owner'], 'confirm_owner': False} for k, v in choices.get('vehicles', {}).items()},
                'documents': {k: {**v, 'accept': False} for k, v in choices.get('documents', {}).items()}}
        draft.choices = choices
        draft.revision += 1
        draft.staging = {}
        return view(db, user, draft, reset_on_change=False)


def selected(comparison, choices):
    cp = comparison['cp']
    cp_fields = {k: f['proposed'] for k, f in cp['fields'].items() if f['selected'] and not f['equal']}
    reps = [r for r in comparison['representatives'] if r['target'] and
            (r['target'] == 'new' or r['signer'] or any(f['selected'] and not f['equal'] for f in r['fields'].values()))]
    if any(not r['target'] and (r['signer'] or any(f['selected'] and not f['equal'] for f in r['fields'].values())) for r in comparison['representatives']):
        raise ValueError('Для принятия представителя/подписанта явно выберите существующую или новую запись')
    rows = [r for r in comparison['vehicles'] if choices.get('vehicles', {}).get(r['key'], {}).get('include')]
    docs = [d for d in comparison['documents'] if d.get('selected')]
    if not (cp_fields or cp['target'] == 'new' or reps or rows or docs):
        return cp_fields, reps, rows, docs
    if cp['error']:
        raise ValueError(cp['error'])
    if cp['target'] == 'new' and not cp_fields.get('name'):
        raise ValueError('Для нового CP явно примите рабочее имя')
    for field, value in cp['fields'].items():
        if value['selected'] and value['error']:
            raise ValueError(f'{field}: {value["error"]}')
    for rep in reps:
        if rep['error']:
            raise ValueError(rep['error'])
        if any(f['selected'] and f['error'] for f in rep['fields'].values()):
            raise ValueError('Выбранный реквизит представителя некорректен')
        if rep['target'] == 'new' and not rep['fields'].get('name', {}).get('selected'):
            raise ValueError('Для нового представителя явно примите имя')
    for row in rows:
        if row['errors']:
            raise ValueError('; '.join(row['errors']))
        for field, value in row['fields'].items():
            if value['selected'] and value['error']:
                raise ValueError(f'Строка {row["row"]}, {field}: {value["error"]}')
        type_error = row['fields'].get('type_id', {}).get('error')
        if not row['id'] and type_error and not row['without_type']:
            raise ValueError(type_error)
        ownership = row['fields']['owner']
        if ownership['selected'] and not ownership['equal'] and ownership['current'] and not row['confirm_owner']:
            raise ObjectVersionConflictError('Отдельно подтвердите прежнего → нового операционного владельца')
    for doc in docs:
        if doc.get('error'):
            raise ValueError(doc['error'])
        if doc['parent_key'] != 'CP' and not doc.get('parent_id') and not any(r['key'] == doc['parent_key'] for r in rows):
            raise ValueError('Для файла новой VEH необходимо принять соответствующую строку техники')
    return cp_fields, reps, rows, docs


def metadata(row):
    return {k: getattr(row, k) for k in ('id', 'original_upload_name', 'stored_name', 'storage_ref', 'size', 'sha256', 'media_type', 'extension')} | {'existing': True}


def partial_update(db, user, context, kind, identifier, version, values):
    saved = update_object(db, object_type=kind, object_id=identifier, expected_version=version,
                          values=values, partial=True, principal=user, context=context)
    permitted = set(values) | {'version'} | ({'status'} if kind == 'CP' else set())
    if saved.event and set(saved.event.changes) - permitted:
        raise ValueError('Невыбранные реквизиты требуют нормализации; сначала явно исправьте обычную карточку')
    return saved


def prepare_files(db, user, draft, comparison, docs, command_id, signature):
    if draft.staging.get('command_id') == command_id:
        if draft.staging['fingerprint'] != signature:
            raise ObjectVersionConflictError('UUID уже связан с другим набором')
        return draft.staging['groups']
    groups = {}
    for doc in docs:
        authorized(db, user, 'arm2_document_view', 'arm2_document_edit',
                   'arm2_' + doc['parent_type'].lower() + '_view', 'arm2_' + doc['parent_type'].lower() + '_edit')
        key = doc['document_id'] or f"{doc['parent_key']}:{doc['kind_id']}"
        existing_document = db.get(Document, doc['document_id']) if doc['document_id'] else None
        kind = db.get(DocumentKind, doc['kind_id'])
        document_name = existing_document.name if existing_document else doc['role'] if doc['role'] != 'Непривязанный файл' else kind.name
        group = groups.setdefault(key, {'document_id': doc['document_id'], 'parent_type': doc['parent_type'],
            'parent_key': doc['parent_key'], 'parent_id': doc['parent_id'], 'kind_id': doc['kind_id'],
            'document_name': document_name, 'expected_version': doc['version'], 'replacements': [],
            'files': [metadata(r) for r in revision_files(db, doc['document_id'], doc['revision_id'])] if doc['document_id'] else []})
        stage = db.get(QuestionnaireUpload, doc['upload_id'])
        files.verified_stage(stage)
        existing = db.scalar(select(DocumentFile).where(DocumentFile.document_id == doc['document_id'],
                              DocumentFile.sha256 == stage.sha256)) if doc['document_id'] else None
        if existing and db.get(DocumentFileCorrection, existing.id):
            raise ValueError('Исправленный ошибочный файл нельзя вернуть')
        if existing:
            item = metadata(existing)
        else:
            file_id = next_id(db, 'FILE')
            if doc['parent_type'] == 'CP':
                field = comparison['cp']['fields'].get('name', {})
                parent_record = db.get(Counterparty, doc['parent_id']) if doc['parent_id'] else None
                parent_name = field.get('proposed') if field.get('selected') else parent_record.name if parent_record else 'Анкета'
            else:
                row = next((r for r in comparison['vehicles'] if r['key'] == doc['parent_key']), None)
                field = row['fields'].get('name', {}) if row else {}
                parent_record = db.get(Vehicle, doc['parent_id']) if doc['parent_id'] else None
                included = draft.choices.get('vehicles', {}).get(doc['parent_key'], {}).get('include')
                parent_name = field.get('proposed') if included and field.get('selected') else parent_record.name if parent_record else row['plate_number'] if row else 'Анкета'
            name = files.safe_name(kind.file_naming_template, SimpleNamespace(name=document_name),
                SimpleNamespace(name=parent_name), kind, file_id, stage.extension)
            item = {'id': file_id, 'original_upload_name': files.original_name(stage.name.rsplit('/', 1)[-1]),
                'stored_name': name, 'storage_ref': '/'.join([files.document_root(), 'questionnaires', draft.id, command_id, file_id, name]),
                'size': stage.size, 'sha256': stage.sha256, 'media_type': stage.media_type,
                'extension': stage.extension, 'existing': False}
        item['upload_id'] = stage.id
        replace = doc['replace_file_id']
        if replace:
            if replace in group['replacements']:
                raise ValueError('Две выбранные загрузки заменяют один FILE; оставьте одну')
            group['replacements'].append(replace)
            index = next((i for i, old in enumerate(group['files']) if old['id'] == replace), None)
            if index is None:
                raise ObjectVersionConflictError('Заменяемый файл уже изменён')
            group['files'][index] = item
        elif not any(old['sha256'] == item['sha256'] for old in group['files']):
            group['files'].append(item)
        stage.prepared = {**stage.prepared, command_id: [*stage.prepared.get(command_id, []), item]}
    if any(len(g['files']) > files.MAX_FILES or len({f['sha256'] for f in g['files']}) != len(g['files']) for g in groups.values()):
        raise ValueError('Состав документа превышает предел B3 или содержит повторяющиеся оригиналы')
    draft.staging = {'command_id': command_id, 'fingerprint': signature, 'groups': list(groups.values())}
    db.commit()  # Private retry/garbage ledger ONLY, before provider IO and domain SQL.
    return draft.staging['groups']


def apply(db, principal, identifier, command_id, revision, preview_id, session_hash=None):
    signature = fingerprint({'draft': identifier, 'command_id': command_id, 'revision': revision, 'preview_id': preview_id})
    with attempt_lease(db, 'B5:' + identifier):
        draft = owned(db, principal, identifier, open_only=False)
        old = db.get(QuestionnaireReceipt, command_id)
        if old:
            if old.author_id != principal.id or old.draft_id != identifier or old.fingerprint != signature:
                raise ObjectVersionConflictError('UUID уже использован с другим содержимым')
            return old.result
        # Revocation is an ACCESS failure even when it also changes the visible preview.
        decisions = draft.choices
        if decisions.get('cp_fields') or decisions.get('cp_target') == 'new' or any(r.get('target') for r in decisions.get('representatives', {}).values()):
            authorized(db, principal, 'arm2_cp_edit')
        if any(r.get('include') for r in decisions.get('vehicles', {}).values()):
            authorized(db, principal, 'arm2_veh_view', 'arm2_veh_edit')
        if any('type_id' in r.get('fields', []) for r in decisions.get('vehicles', {}).values()):
            authorized(db, principal, 'arm2_type_view')
        if any(d.get('accept') for d in decisions.get('documents', {}).values()):
            authorized(db, principal, 'arm2_document_view', 'arm2_document_edit')
        if draft.status != 'OPEN' or draft.revision != revision:
            raise ObjectVersionConflictError('Разбор изменён или закрыт')
        comparison = build(db, principal, draft)
        if review_hash(comparison, draft.choices) != preview_id:
            raise ObjectVersionConflictError('Карточки изменены; повторно откройте сверку и подтвердите решения')
        cp_fields, reps, rows, docs = selected(comparison, draft.choices)
        groups = prepare_files(db, principal, draft, comparison, docs, command_id, signature)
        for group in groups:
            for item in group['files']:
                if files.verify_remote(type('Metadata', (), item)):
                    continue
                if item['existing']:
                    raise files.StorageUnavailable('Текущий оригинал недоступен; SQL-публикация отменена')
                stage = db.get(QuestionnaireUpload, item['upload_id'])
                path = files.verified_stage(stage)
                db.commit()
                try:
                    files.storage().upload(path, item['storage_ref'], item['sha256'])
                except Exception:
                    raise files.StorageUnavailable('Хранилище не подтвердило оригинал; повторите ту же команду') from None
                if not files.verify_remote(type('Metadata', (), item)):
                    raise files.StorageUnavailable('Хранилище не подтвердило SHA/размер; карточки не изменены')
                stage = db.get(QuestionnaireUpload, item['upload_id'])
                stage.state = 'UPLOADED'
                db.commit()  # Private uploaded ledger, not accepted FILE/DOCUMENT.
        try:
            draft = owned(db, principal, identifier)
            # Follow B3 kind->parent lock order, B2 VEH->CP order. Common SAVE rechecks under these locks.
            for kind_id in sorted({g['kind_id'] for g in groups}):
                db.scalar(select(DocumentKind).where(DocumentKind.id == kind_id).with_for_update().execution_options(populate_existing=True))
            for vid in sorted({r['id'] for r in rows if r['id']} | {g['parent_id'] for g in groups if g['parent_type'] == 'VEH' and g['parent_id']}):
                db.scalar(select(Vehicle).where(Vehicle.id == vid).with_for_update().execution_options(populate_existing=True))
            cp_id = comparison['cp']['id']
            if cp_id:
                db.scalar(select(Counterparty).where(Counterparty.id == cp_id).with_for_update().execution_options(populate_existing=True))
            for rep_id in sorted({r['id'] for r in reps if r['id']}):
                db.scalar(select(CounterpartyRepresentative).where(CounterpartyRepresentative.id == rep_id)
                          .with_for_update().execution_options(populate_existing=True))
            for doc_id in sorted({g['document_id'] for g in groups if g['document_id']}):
                db.scalar(select(Document).where(Document.id == doc_id).with_for_update().execution_options(populate_existing=True))
            fresh = build(db, principal, draft)
            if draft.revision != revision or review_hash(fresh, draft.choices) != preview_id:
                raise ObjectVersionConflictError('Версии или связи изменены во время загрузки файлов; весь набор отменён')
            user = authorized(db, principal, 'arm2_cp_view')
            if session_hash:
                session = db.get(LoginSession, session_hash, populate_existing=True)
                if session is None or session.user_id != user.id or session.expires_at <= datetime.now(UTC):
                    raise ObjectAuthorizationError('Сессия завершена; выбранный набор не принят')
            context = SaveContext('HUMAN', str(user.id), 'arm2-questionnaire', command_id)
            outcomes, identities = [], {}
            cp_plan = fresh['cp']
            if cp_plan['target'] == 'new':
                authorized(db, user, 'arm2_cp_edit')
                if not cp_fields.get('name'):
                    raise ValueError('Для нового CP явно примите рабочее имя')
                cp = create_counterparty(db, principal=user, context=context, **cp_fields)
                outcomes.append({'kind': 'CP', 'id': cp.id, 'version': cp.version})
            else:
                cp = db.get(Counterparty, cp_id) if cp_id else None
                if cp_fields:
                    authorized(db, user, 'arm2_cp_edit')
                    result = partial_update(db, user, context, 'CP', cp_id, cp_plan['version'], cp_fields)
                    cp = result.record
                    if result.changed:
                        outcomes.append({'kind': 'CP', 'id': cp.id, 'version': cp.version})
            if cp:
                identities['CP'] = cp.id
            for rep in reps:
                authorized(db, user, 'arm2_cp_edit')
                values = {k: f['proposed'] for k, f in rep['fields'].items() if f['selected'] and not f['equal']}
                if rep['target'] == 'new':
                    record = create_representative(db, 'CP', cp.id, principal=user, context=context, **values)
                else:
                    current_rep = db.get(CounterpartyRepresentative, rep['id'], populate_existing=True)
                    if current_rep.parent_id != cp.id:
                        raise ObjectAuthorizationError('Представитель принадлежит другому CP')
                    record = partial_update(db, user, context, 'CPREP', rep['id'], rep['version'], values).record
                outcomes.append({'kind': 'CPREP', 'id': record.id, 'version': record.version})
                if rep['signer'] and cp.primary_signer_id != record.id:
                    cp = partial_update(db, user, context, 'CP', cp.id, cp.version, {'primary_signer_id': record.id}).record
            for row in rows:
                authorized(db, user, 'arm2_veh_view', 'arm2_veh_edit')
                values = {k: f['proposed'] for k, f in row['fields'].items() if f['selected'] and not f['equal'] and k != 'owner'}
                if row['id']:
                    record = db.get(Vehicle, row['id'], populate_existing=True)
                    if values:
                        record = partial_update(db, user, context, 'VEH', row['id'], row['version'], values).record
                else:
                    if row['withdrawal']:
                        raise ValueError('Нельзя создать новую VEH из строки «ВЫБЫЛО»')
                    record = create_vehicle(db, values.pop('name', None) or row['plate_number'], row['plate_number'],
                        principal=user, context=context, **values)
                identities[row['key']] = record.id
                own = row['fields']['owner']
                if own['selected'] and not own['equal']:
                    target = own['proposed']
                    if target == ['CP', '$CP']:
                        target = ['CP', cp.id]
                    before = own['current']
                    record = set_operational_owner(db, record.id, record.version,
                        target[0] if target else None, target[1] if target else None,
                        before[0] if before else None, before[1] if before else None, row['confirm_owner'],
                        principal=user, context=context).record
                outcomes.append({'kind': 'VEH', 'id': record.id, 'version': record.version})
            for group in groups:
                parent_id = identities.get(group['parent_key']) or group['parent_id']
                published = publish_prepared_revision_in_transaction(db, {**group, 'parent_id': parent_id}, user, context)
                outcomes.append({'kind': 'DOCUMENT', **published})
            accepted = datetime.now(UTC)
            db.flush()
            event_rows = db.scalars(select(HistoryEvent).where(HistoryEvent.correlation_id == command_id,
                HistoryEvent.source == 'arm2-questionnaire', HistoryEvent.actor_id == str(user.id)).order_by(HistoryEvent.event_id)).all()
            by_identity = {(o['kind'], o.get('id') or o.get('document_id')): o for o in outcomes}
            for event in event_rows:
                entry = by_identity.setdefault((event.object_type, event.object_id), {'kind': event.object_type, 'id': event.object_id})
                entry['version'] = event.version_after
            outcomes = list(by_identity.values())
            events = [event.event_id for event in event_rows]
            result = {'command_id': command_id, 'draft_id': draft.id, 'cp_id': cp.id if cp else None,
                'filled_date': draft.filled_date.isoformat() if draft.filled_date else None,
                'uploaded_at': draft.uploaded_at.astimezone(UTC).isoformat(), 'accepted_at': accepted.isoformat(),
                'objects': outcomes, 'event_ids': events, 'no_op': not events, 'correlation_id': command_id}
            db.add(QuestionnaireReceipt(command_id=command_id, draft_id=draft.id, author_id=user.id,
                fingerprint=signature, result=result))
            draft.status, draft.accepted_at = 'ACCEPTED', accepted
            draft.payload, draft.choices, draft.preview, draft.staging = {}, {}, {}, {}
            for stage in db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft.id)):
                if any(item.get('upload_id') == stage.id for g in groups for item in g['files']):
                    stage.state = 'ACCEPTED'
            db.commit()  # THE ONLY DOMAIN COMMIT: CP/CPREP/VEH/DOCUMENT/FILE/HIST and receipt.
        except Exception:
            db.rollback()  # Every selected domain change rolls back; private upload ledger remains retryable.
            raise
        cleanup_local(db, draft.id)
        return result


def cleanup_local(db, draft_id):
    for stage in db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft_id)):
        try:
            files.private_path(stage.id).unlink(missing_ok=True)
        except PermissionError:
            pass  # In-use preview retains only a private file; next cleanup retries it.


def cancel(db, user, identifier):
    with attempt_lease(db, 'B5:' + identifier):
        draft = owned(db, user, identifier)
        draft.status = 'CANCELLED'
        draft.payload, draft.choices, draft.preview, draft.staging = {}, {}, {}, {}
        db.commit()
        cleanup_local(db, identifier)
        return {'id': identifier, 'status': 'CANCELLED'}


def cleanup(db, user, identifier):
    """Only ledger-owned unpublished originals may be moved to provider trash; never accepted FILEs."""
    with attempt_lease(db, 'B5:' + identifier):
        draft = owned(db, user, identifier, open_only=False)
        if draft.status == 'OPEN':
            raise ValueError('Сначала завершите или отклоните разбор')
        pending = 0
        for stage in db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == identifier)):
            for entries in stage.prepared.values():
                for item in entries:
                    if item['existing'] or db.scalar(select(DocumentFile.id).where(DocumentFile.storage_ref == item['storage_ref'])):
                        continue
                    prefix = files.document_root() + '/questionnaires/' + identifier + '/'
                    if not item['storage_ref'].startswith(prefix):
                        raise ObjectAuthorizationError('Нельзя очищать файлы вне staging этого пакета')
                    try:
                        provider = files.storage()
                        remote_metadata = getattr(provider, 'document_metadata', provider.metadata)
                        if remote_metadata(item['storage_ref']) is None:
                            continue
                        if not files.verify_remote(type('Metadata', (), item), provider):
                            pending += 1  # Different/unverifiable bytes are never deleted or declared clean.
                            continue
                        provider.trash_exchange_file(item['storage_ref'], item['sha256'])
                        if remote_metadata(item['storage_ref']) is not None:
                            pending += 1
                    except Exception:
                        pending += 1  # Keep provenance; provider recovery may retry safe garbage cleanup.
            if stage.state != 'ACCEPTED' and not pending:
                stage.state = 'DISCARDED'
        db.commit()
        cleanup_local(db, identifier)
        return {'id': identifier, 'pending': pending}


def export(db, user, cp_id, *, with_files=False, incomplete_reason=None):
    user = authorized(db, user, 'arm2_cp_view', 'arm2_veh_view')
    cp = db.get(Counterparty, cp_id, populate_existing=True)
    if cp is None:
        raise ValueError('Контрагент не найден')
    vehicles = list(db.scalars(select(Vehicle).order_by(Vehicle.id)))  # ALL allowed VEH, not a 45/100 row page.
    owned_vehicles = [v for v in vehicles if v.owner_cp_id == cp.id]
    if len(owned_vehicles) > 1000:
        raise ValueError('У CP больше 1000 строк; транспортный предел анкеты превышен')
    originals, missing, mapping = {}, [], []
    parents = {('CP', cp.id), *[('VEH', v.id) for v in owned_vehicles]}
    if with_files:
        authorized(db, user, 'arm2_document_view')
        for ptype, pid in sorted(parents):
            for doc in db.scalars(select(Document).where(Document.parent_type == ptype, Document.parent_id == pid,
                                                         Document.archive_state == 'Неархивный').order_by(Document.id)):
                for file in revision_files(db, doc.id, doc.current_revision_id):
                    name = f'files/{ptype}_{pid}_{doc.id}_{file.id}.{file.extension}'
                    try:
                        if db.get(DocumentFileCorrection, file.id):
                            raise files.StorageUnavailable('Исправленная ошибочная загрузка')
                        path = files.download(file)
                        try:
                            originals[name] = path.read_bytes()
                        finally:
                            path.unlink(missing_ok=True)
                        mapping.append({'parent_type': ptype, 'parent_id': pid, 'document_id': doc.id,
                            'document_version': doc.version, 'revision_id': doc.current_revision_id,
                            'file_id': file.id, 'name': name, 'sha256': file.sha256})
                    except files.StorageUnavailable:
                        missing.append({'parent_type': ptype, 'parent_id': pid, 'document_id': doc.id, 'file_id': file.id,
                                        'name': file.original_upload_name})
    if missing and not (incomplete_reason and incomplete_reason.strip()):
        return {'confirmation_required': True, 'missing': missing,
                'message': 'Исправьте доступность файлов или явно выдайте неполный ZIP с причиной'}
    book = Workbook()
    passport = book.active
    passport.title = PASSPORT
    passport['B3'] = 'ARM_IMPORT_V2'
    passport['B6'] = cp.id
    last_filled = db.scalar(select(QuestionnaireReceipt.result['filled_date'].astext).where(
        QuestionnaireReceipt.result['cp_id'].astext == cp.id).order_by(QuestionnaireReceipt.accepted_at.desc()).limit(1))
    if last_filled:
        passport['B4'] = last_filled
    for row, field in CP_CELLS.items():
        passport[f'A{row}'] = field
        passport[f'B{row}'] = getattr(cp, field) or ''
    signer = None
    if cp.primary_signer_id:
        signer = db.get(CounterpartyRepresentative, cp.primary_signer_id)
        for row, field in ((20, 'name'), (21, 'position'), (22, 'authority_basis')):
            passport[f'B{row}'] = getattr(signer, field) or ''
    sheet = book.create_sheet(VEHICLES)
    headers = ['Выбор VEH-ID', 'Статус строки', 'Тип текущий', 'Госномер текущий', 'Имя текущее', 'Модель текущая',
               'Тип предложенный', 'Госномер предложенный', 'Имя предложенное', 'Модель предложенная',
               'Объём — только сверка', 'Справка', 'Файл справки', 'СТС', 'Файл СТС', 'Диагностика', 'Подсказка',
               'CP-кандидат', 'Расхождение', 'Описание', 'Legacy reference']
    for column, name in enumerate(headers, 2):
        sheet.cell(4, column, name)
    type_view = has_permission(user, Permission.ARM2_TYPE_VIEW)
    for index, vehicle in enumerate(owned_vehicles, 5):
        typ = db.get(VehicleType, vehicle.type_id) if vehicle.type_id and type_view else None
        for column, value in {2: vehicle.id, 3: 'ЗАЯВЛЕНО', 4: typ.id if typ else '', 5: vehicle.plate_number,
                              6: vehicle.name, 7: vehicle.model or '', 19: cp.id}.items():
            sheet.cell(index, column, value)
        for item in mapping:
            if item['parent_id'] != vehicle.id:
                continue
            doc = db.get(Document, item['document_id'])
            kind_name = db.get(DocumentKind, doc.kind_ref).name
            if kind_name in ('СТС', 'Тарировка'):
                sheet.cell(index, 16 if kind_name == 'СТС' else 14, item['name'])
    cp_files = [f for f in mapping if f['parent_type'] == 'CP']
    if len(cp_files) == 1:
        passport['B27'], passport['B28'] = 'Да', cp_files[0]['name']
    catalog = book.create_sheet('_ARM2_VEH')
    catalog.append(['VEH-ID', 'Госномер', 'TYPE-ID', 'Тип', 'Имя', 'Модель', 'Учётное состояние'])
    for vehicle in vehicles:
        typ = db.get(VehicleType, vehicle.type_id) if vehicle.type_id and type_view else None
        catalog.append([vehicle.id, vehicle.plate_number, typ.id if typ else '', typ.name if typ else '',
                        vehicle.name, vehicle.model or '', vehicle.accounting_state])
    catalog.sheet_state = 'veryHidden'
    book.defined_names.add(DefinedName('ARM2_VEH_IDS', attr_text=f"'_ARM2_VEH'!$A$2:$A${max(2,len(vehicles)+1)}"))
    validation = DataValidation(type='list', formula1='ARM2_VEH_IDS')
    sheet.add_data_validation(validation)
    validation.add('B5:B1004')
    cached_formula_values = {}
    for row in range(5, 1005):
        current = owned_vehicles[row-5] if row-5 < len(owned_vehicles) else None
        typ = db.get(VehicleType, current.type_id) if current and current.type_id and type_view else None
        for col, index, cached_value in [('D', 3, typ.id if typ else ''),
                ('E', 2, current.plate_number if current else ''), ('F', 5, current.name if current else ''),
                ('G', 6, current.model or '' if current else '')]:
            coordinate = f'{col}{row}'
            sheet[coordinate] = f'=IFERROR(VLOOKUP($B{row},\'_ARM2_VEH\'!$A$2:$G${max(2,len(vehicles)+1)},{index},FALSE),"")'
            cached_formula_values[coordinate] = cached_value
    book.create_sheet('Инструкция')['A1'] = 'Пустые значения не стирают ARM. Вложения — отдельные PDF/JPG/JPEG/PNG. Изменения принимает оператор.'
    book.create_sheet('Справочники')['A1'] = 'Источник выбора техники — скрытый полный каталог ARM2 с постоянными ID.'
    manifest = seal({'schema': 'ARM2_QUESTIONNAIRE_1', 'cp_id': cp.id, 'files': mapping,
        'representatives': [{'index': 0, 'id': signer.id, 'parent_id': cp.id, 'version': signer.version}] if signer else [],
        'incomplete': {'author_id': user.id, 'reason': incomplete_reason, 'missing': missing} if missing else None})
    service = book.create_sheet(MANIFEST)
    for offset in range(0, len(manifest), 20000):
        service.append([manifest[offset:offset+20000]])
    service.sheet_state = 'veryHidden'
    for ws in book:
        for row in ws:
            for cell in row:
                if isinstance(cell.value, str) and not (ws is sheet and cell.coordinate in cached_formula_values):
                    cell.data_type = 's'
                    cell.number_format = '@'
        ws.column_dimensions['A'].width = 35
        ws.column_dimensions['B'].width = 48
    out = io.BytesIO()
    book.save(out)
    book.close()
    # Keep server-verified caches for current rows without executing input formulae.
    # Excel recalculates the lookup when the recipient chooses a different VEH-ID.
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as generated:
        cached_out = io.BytesIO()
        with zipfile.ZipFile(cached_out, 'w', zipfile.ZIP_DEFLATED) as target:
            for entry in generated.infolist():
                content = generated.read(entry)
                if entry.filename == 'xl/worksheets/sheet2.xml':
                    xml = ET.fromstring(content)
                    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                    for cell in xml.iter(ns + 'c'):
                        if cell.get('r') in cached_formula_values:
                            cell.set('t', 'str')
                            formula = cell.find(ns + 'f')
                            if formula is None:
                                inline = cell.find(ns + 'is')
                                if inline is not None:
                                    cell.remove(inline)
                                formula = ET.SubElement(cell, ns + 'f')
                                formula.text = f'IFERROR(VLOOKUP($B{cell.get("r")[1:]},\'_ARM2_VEH\'!$A$2:$G${max(2,len(vehicles)+1)},' + str({'D':3,'E':2,'F':5,'G':6}[cell.get('r')[0]]) + ',FALSE),"")'
                            value = cell.find(ns + 'v')
                            if value is None:
                                value = ET.SubElement(cell, ns + 'v')
                            value.text = cached_formula_values[cell.get('r')]
                    content = ET.tostring(xml, encoding='utf-8')
                target.writestr(entry.filename, content)
    data, media, suffix = cached_out.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'
    if with_files:
        if len(originals) + 1 > MAX_ENTRIES or sum(len(v) for v in originals.values()) + len(data) > MAX_EXPANDED:
            raise ValueError('Полная выгрузка превышает транспортные пределы ZIP; файлы не пропущены')
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('questionnaire.xlsx', data)
            for name, content in originals.items():
                archive.writestr(name, content)
        data, media, suffix = zipped.getvalue(), 'application/zip', 'zip'
    if len(data) > MAX_COMPRESSED:
        raise ValueError('Выгрузка превышает 32 МиБ; файлы не пропущены')
    db.add(QuestionnaireExport(id=str(uuid4()), author_id=user.id, cp_id=cp.id,
        incomplete_reason=incomplete_reason.strip() if incomplete_reason else None, missing=missing))
    db.commit()
    return {'data': data, 'media_type': media, 'filename': 'questionnaire.' + suffix}
