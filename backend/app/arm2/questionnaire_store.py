"""B5 transport/draft orchestration, private upload ledger and ONE atomic domain SAVE."""
import hashlib
import io
import secrets
import zipfile
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, text

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
from app.arm2.questionnaire_excel import build_workbook
from app.arm2.questionnaire_models import (
    QuestionnaireDraft,
    QuestionnaireExport,
    QuestionnaireReceipt,
    QuestionnaireUpload,
)
from app.arm2.questionnaire_parse import (
    EXPORT_VERSION,
    MAX_COMPRESSED,
    MAX_ENTRIES,
    MAX_EXPANDED,
    fingerprint,
    parse,
)
from app.arm2.representative_models import CounterpartyRepresentative
from app.arm2.representative_store import create_representative
from app.arm2.vehicle_models import Vehicle
from app.arm2.vehicle_store import create_vehicle, set_operational_owner
from app.arm2.vehicle_type_models import VehicleType
from app.models import LoginSession, User
from app.permissions import Permission, has_permission

OLE_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def attachment_format(path):
    """Classify the outer document; a legacy OLE original may contain ZIP streams."""
    try:
        media, extension = files.content_type(path)
        return media, extension, None
    except ValueError as failure:
        unrecognized = str(failure).startswith('Содержимое файла не соответствует')
        with path.open('rb') as source:
            header = source.read(8)
        if unrecognized and header == OLE_SIGNATURE:
            return 'application/octet-stream', 'bin', None
        if zipfile.is_zipfile(path):
            return None, None, 'Вложенный ZIP нельзя принять как документ; приложите отдельный файл'
        if header and unrecognized:
            return 'application/octet-stream', 'bin', None
        return None, None, str(failure)


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
    # Repair only the known false ZIP classification in an existing OPEN draft.
    # Preserve file bytes/SHA, decisions, ownership and all accepted FILE history.
    for stage in db.scalars(select(QuestionnaireUpload).where(QuestionnaireUpload.draft_id == draft.id,
            QuestionnaireUpload.state == 'STAGED')):
        if stage.error and stage.error.startswith('Вложенный ZIP'):
            try:
                path = files.verified_stage(stage)
                with path.open('rb') as source:
                    ole = source.read(8) == OLE_SIGNATURE
                if ole:
                    media, extension, error = attachment_format(path)
                    if error is None:
                        stage.media_type, stage.extension, stage.error = media, extension, None
            except files.StorageUnavailable:
                pass  # Keep the existing error if the original cannot be verified.
    comparison = build(db, user, draft)
    approval_reset = False
    if reset_on_change and draft.preview and review_hash(draft.preview, draft.choices) != review_hash(comparison, draft.choices):
        # Never reuse an old owner/field/file approval against freshly changed server data.
        choices = dict(draft.choices)
        old = draft.preview
        if old['cp'] != comparison['cp']:
            choices.update(cp_fields=[], cp_clears=[], confirm_identity=False)
        for group in ('representatives', 'vehicles', 'documents'):
            before = {p['key']: p for p in old[group]}
            changed = {p['key'] for p in comparison[group] if p != before.get(p['key'])}
            reset = {'fields': [], 'signer': False} if group == 'representatives' else \
                    {'fields': [], 'include': False, 'confirm_owner': False} if group == 'vehicles' else {'accept': False}
            choices[group] = {k: {**v, **(reset if k in changed else {})} for k, v in choices.get(group, {}).items()}
        draft.choices = choices
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
        return {k: v for k, v in plan.items() if k not in ('candidates', 'attachment_options', 'hint', 'diagnostic_hint', 'legacy_hint', 'source', 'volume_notice')}
    cp = item(comparison['cp'])
    cp['fields'] = {k: f for k, f in cp['fields'].items() if f['selected']}
    reps = [item(r) for r in comparison['representatives'] if r['target']]
    docs = [item(d) for d in comparison['documents'] if d.get('selected')]
    parent_keys = {d.get('parent_key') for d in docs}
    rows = [item(r) for r in comparison['vehicles'] if choices.get('vehicles', {}).get(r['key'], {}).get('include') or r['key'] in parent_keys]
    return fingerprint({'cp': cp, 'representatives': reps, 'vehicles': rows, 'documents': docs})


def upload(db, user, content, filename):
    authorized(db, user, 'arm2_cp_view')
    parsed, attachments = parse(content, filename)
    digest = hashlib.sha256(content).hexdigest()
    # Serialize only the same author/content pair, including simultaneous uploads.
    lock = int.from_bytes(hashlib.sha256(f'B5:{user.id}:{digest}'.encode()).digest()[:8], 'big', signed=True)
    db.execute(text('SELECT pg_advisory_xact_lock(:lock)'), {'lock': lock})
    active = db.scalar(select(QuestionnaireDraft).where(QuestionnaireDraft.author_id == user.id,
        QuestionnaireDraft.source_sha256 == digest, QuestionnaireDraft.status == 'OPEN').with_for_update())
    if active is not None:
        return view(db, user, active)
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
                media, extension, error = attachment_format(path)
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
    if set(choices) - {'cp_target', 'cp_fields', 'cp_clears', 'cp_values', 'confirm_identity', 'representatives', 'vehicles', 'documents'}:
        raise ValueError('Неизвестное решение анкеты')
    if set(choices.get('cp_fields', [])) - set(CP_FIELDS):
        raise ValueError('Неизвестный реквизит CP')
    if set(choices.get('cp_clears', [])) - (set(CP_FIELDS) - {'name', 'type'}):
        raise ValueError('Неизвестная либо обязательная очистка CP')
    if set(choices.get('cp_clears', [])) & set(choices.get('cp_fields', [])):
        raise ValueError('Выберите одно действие set/clear/keep')
    if set(choices.get('cp_values', {})) - {'type', 'legal_form'}:
        raise ValueError('Неизвестное исправление типа/ОПФ')
    for rep in choices.get('representatives', {}).values():
        if (set(rep) - {'target', 'fields', 'signer', 'sources'} or set(rep.get('fields', [])) - set(REP_FIELDS)
                or set(rep.get('sources', {})) - {'name', 'position', 'authority_basis'}
                or set(rep.get('sources', {}).values()) - {'passport', 'list'}):
            raise ValueError('Решение представителя содержит запрещённые реквизиты')
    for row in choices.get('vehicles', {}).values():
        if set(row) - {'fields', 'type_id', 'without_type', 'confirm_owner', 'include'} or set(row.get('fields', [])) - set(VEH_FIELDS):
            raise ValueError('Неизвестный реквизит VEH')
    for doc in choices.get('documents', {}).values():
        if set(doc) - {'upload_id', 'parent_key', 'kind_id', 'document_id', 'replace_file_id', 'append', 'accept'}:
            raise ValueError('Неизвестное решение документа')


def propose_new(db, user, draft, previous):
    """Seed the private decision draft once, never SQL objects or existing values.

    Explicit exclusions survive reopen/edits. Resolving a new row's TYPE is an
    explicit decision; only that TYPE field is refreshed, never another row.
    """
    choices = dict(draft.choices)
    comparison = build(db, user, draft)
    new_cp = choices.get('cp_target') == 'new'
    first_cp = new_cp and previous.get('cp_target') != 'new' and not comparison['cp']['error']
    if first_cp and not choices.get('cp_fields'):
        choices['cp_fields'] = [k for k, f in comparison['cp']['fields'].items() if not f['error'] and f['proposed'] not in (None, '')]
    reps = dict(choices.get('representatives', {}))
    vehicles = dict(choices.get('vehicles', {}))
    if choices.get('cp_target') and not comparison['cp']['error']:
        for r in comparison['representatives']:
            # Never infer an existing representative's identity from a name.
            same_name = any(c['name'] == r['fields'].get('name', {}).get('proposed') for c in r['candidates'])
            if r['key'] not in reps and not r['id'] and not r['source_id'] and not r['error'] and not same_name:
                fields = [k for k, f in r['fields'].items() if not f['error']]
                if 'name' in fields:
                    reps[r['key']] = {'target': 'new', 'fields': fields, 'signer': r['signer_hint']}
            elif (reps.get(r['key'], {}).get('target') == 'new'
                  and previous.get('representatives', {}).get(r['key'], {}).get('target') != 'new' and not r['error']):
                value = reps[r['key']]
                if not value.get('fields'):
                    reps[r['key']] = {**value, 'fields': [k for k, f in r['fields'].items() if not f['error']]}
        for row in comparison['vehicles']:
            if row['key'] not in vehicles and not row['id']:
                valid = not row['errors'] and not any(f['error'] for f in row['fields'].values())
                vehicles[row['key']] = {'include': valid, 'fields': [k for k, f in row['fields'].items() if not f['error']] if valid else [],
                                        'confirm_owner': False}
            elif not row['id'] and row['key'] in vehicles:
                old = previous.get('vehicles', {}).get(row['key'], {})
                value = vehicles[row['key']]
                if (old.get('type_id'), old.get('without_type')) != (value.get('type_id'), value.get('without_type')):
                    fields = [f for f in value.get('fields', []) if f != 'type_id']
                    if 'type_id' in row['fields'] and not row['fields']['type_id']['error']:
                        fields.append('type_id')
                    vehicles[row['key']] = {**value, 'fields': fields}
            # Adding a NEW machine includes its declared selected CP relationship.
            # Only existing machines expose independent owner reassignment.
            if not row['id'] and vehicles.get(row['key'], {}).get('include'):
                value = vehicles[row['key']]
                vehicles[row['key']] = {**value, 'fields': list(dict.fromkeys([*value.get('fields', []), 'owner']))}
    choices.update(representatives=reps, vehicles=vehicles)
    draft.choices = choices
    comparison = build(db, user, draft)
    documents = dict(choices.get('documents', {}))
    for doc in comparison['documents']:
        if doc['key'] not in documents and doc.get('upload_id') and not doc.get('error') and not doc.get('equal') and not doc.get('document_id'):
            parent_new = (first_cp if doc['parent_key'] == 'CP' else
                          any(r['key'] == doc['parent_key'] and not r['id'] and vehicles.get(r['key'], {}).get('include') for r in comparison['vehicles']))
            if parent_new:
                documents[doc['key']] = {'kind_id': doc['kind_id'], 'accept': True}
    draft.choices = {**choices, 'documents': documents}


def edit(db, user, identifier, revision, choices):
    with attempt_lease(db, 'B5:' + identifier):
        draft = owned(db, user, identifier)
        if draft.revision != revision:
            raise ObjectVersionConflictError('Черновик изменён в другом окне; откройте его заново')
        if draft.preview and review_hash(build(db, user, draft), draft.choices) != review_hash(draft.preview, draft.choices):
            raise ObjectVersionConflictError('Живые карточки изменились; откройте сверку заново перед новым решением')
        validate_choices(choices)
        original = previous = draft.choices
        overrides = choices.get('cp_values', {})
        changed_values = {f for f in set(previous.get('cp_values', {})) | set(overrides)
                          if previous.get('cp_values', {}).get(f) != overrides.get(f)}
        choices = {**choices, 'cp_fields': [f for f in choices.get('cp_fields', []) if f not in changed_values]}
        def identity_selection(value):
            return (set(value.get('cp_fields', [])) & {'type', 'legal_form'}, value.get('cp_values', {}),
                    'legal_form' in value.get('cp_clears', []))
        if identity_selection(previous) != identity_selection(choices):
            choices['cp_clears'] = [f for f in choices.get('cp_clears', [])
                                    if f not in previous.get('cp_clears', []) or f == 'legal_form']
            if previous.get('confirm_identity'):
                choices['confirm_identity'] = False
        if draft.choices.get('cp_target') and choices.get('cp_target') != draft.choices.get('cp_target'):
            choices = {**choices, 'cp_fields': [], 'cp_clears': [], 'confirm_identity': False,
                'representatives': {k: {**v, 'target': None, 'fields': [], 'signer': False} for k, v in choices.get('representatives', {}).items()},
                'vehicles': {k: {**v, 'fields': [f for f in v.get('fields', []) if f != 'owner'], 'confirm_owner': False} for k, v in choices.get('vehicles', {}).items()},
                'documents': {k: {**v, 'accept': False} for k, v in choices.get('documents', {}).items()}}
        # A different signer projection is a different proposed value: an earlier
        # approval must not silently carry over when its source is changed.
        representatives = {}
        for key, decision in choices.get('representatives', {}).items():
            previous = draft.choices.get('representatives', {}).get(key, {}).get('sources', {})
            sources = decision.get('sources', {})
            changed = {field for field in set(previous) | set(sources) if previous.get(field) != sources.get(field)}
            representatives[key] = {**decision, 'fields': [field for field in decision.get('fields', []) if field not in changed]}
        choices = {**choices, 'representatives': representatives}
        draft.choices = choices
        propose_new(db, user, draft, original)
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
    if len({d.get('upload_id') for d in docs}) != len(docs):
        raise ValueError('Один загруженный файл выбран для нескольких документов; уточните привязку')
    if not (cp_fields or cp['target'] == 'new' or reps or rows or docs):
        return cp_fields, reps, rows, docs
    if cp['error']:
        raise ValueError(cp['error'])
    if cp['target'] == 'new' and not cp_fields.get('name'):
        raise ValueError('Для нового CP явно примите рабочее имя')
    if cp['target'] == 'new' and not cp_fields.get('type'):
        raise ValueError('Для нового CP явно примите тип')
    for field, value in cp['fields'].items():
        if value['selected'] and value['error']:
            raise ValueError(f'{field}: {value["error"]}')
    if cp.get('identity_conflict') and not choices.get('confirm_identity'):
        raise ObjectVersionConflictError('Отдельно подтвердите конфликт личности, изменение типа или ОПФ')
    for rep in reps:
        if rep['error']:
            raise ValueError(rep['error'])
        if any(f['selected'] and f['error'] for f in rep['fields'].values()):
            raise ValueError('Выбранный реквизит представителя некорректен')
        if rep['target'] == 'new' and not rep['fields'].get('name', {}).get('selected'):
            raise ValueError('Для нового представителя явно примите имя')
    if len([r for r in reps if r['signer']]) > 1:
        raise ValueError('Выберите только одного основного подписанта')
    existing_rep_ids = [r['id'] for r in reps if r['target'] != 'new']
    if len(existing_rep_ids) != len(set(existing_rep_ids)):
        raise ValueError('Один REP выбран для нескольких строк; устраните повтор')
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
    permitted = set(values) | {'version'}
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
                    own = row['fields']['owner']
                    target = own['proposed'] if own['selected'] else None
                    if target == ['CP', '$CP']:
                        target = ['CP', cp.id]
                    record = create_vehicle(db, values.pop('name', None) or row['plate_number'], row['plate_number'],
                        owner_cp_id=target[1] if target and target[0] == 'CP' else None,
                        owner_org_id=target[1] if target and target[0] == 'ORG' else None,
                        principal=user, context=context, **values)
                identities[row['key']] = record.id
                own = row['fields']['owner']
                if row['id'] and own['selected'] and not own['equal']:
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
            def receipt_link(event):
                if event.object_type == 'CPREP':
                    return f'/arm2/cp/{cp.id}/representatives/{event.object_id}'
                if event.object_type == 'DOCUMENT':
                    document = db.get(Document, event.object_id)
                    return f'/arm2/{document.parent_type.lower()}/{document.parent_id}/documents/{document.id}'
                return f'/arm2/{event.object_type.lower()}/{event.object_id}'
            for entry in outcomes:
                identifier = entry.get('id') or entry.get('document_id')
                model = {'CP': Counterparty, 'CPREP': CounterpartyRepresentative, 'VEH': Vehicle, 'DOCUMENT': Document}[entry['kind']]
                record = db.get(model, identifier)
                if record:
                    entry['name'] = record.name + (f' · {record.plate_number}' if entry['kind'] == 'VEH' else '')
                event = next((e for e in event_rows if e.object_type == entry['kind'] and e.object_id == identifier), None)
                if event:
                    entry['url'] = receipt_link(event)
            result = {'command_id': command_id, 'draft_id': draft.id, 'cp_id': cp.id if cp else None,
                'filled_date': draft.filled_date.isoformat() if draft.filled_date else None,
                'uploaded_at': draft.uploaded_at.astimezone(UTC).isoformat(), 'accepted_at': accepted.isoformat(),
                'objects': outcomes, 'event_ids': events, 'no_op': not events, 'correlation_id': command_id,
                'changes': [{'kind': e.object_type, 'id': e.object_id, 'operation': e.operation,
                             'fields': e.changes, 'url': receipt_link(e)} for e in event_rows]}
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
    representatives = list(db.scalars(select(CounterpartyRepresentative).where(
        CounterpartyRepresentative.parent_id == cp.id).order_by(CounterpartyRepresentative.id)))
    if len(representatives) > 1000:
        raise ValueError('The questionnaire supports up to 1000 representatives; no records were omitted')
    linked_users = {rep.user_id: db.get(User, rep.user_id) for rep in representatives if rep.user_id is not None}
    type_view = has_permission(user, Permission.ARM2_TYPE_VIEW)
    types = {t.id: t for t in db.scalars(select(VehicleType).order_by(VehicleType.id))} if type_view else {}
    kinds = {item['document_id']: db.get(DocumentKind, db.get(Document, item['document_id']).kind_ref).name for item in mapping}
    last_filled = db.scalar(select(QuestionnaireReceipt.result['filled_date'].astext).where(
        QuestionnaireReceipt.result['cp_id'].astext == cp.id).order_by(QuestionnaireReceipt.accepted_at.desc()).limit(1))
    manifest = {'schema': 'ARM2_QUESTIONNAIRE_1', 'transport_version': EXPORT_VERSION,
        'cp_id': cp.id, 'primary_signer_id': cp.primary_signer_id, 'files': mapping,
        'representatives': [{'index': i, 'id': rep.id, 'parent_id': cp.id, 'version': rep.version}
                            for i, rep in enumerate(representatives)],
        'incomplete': {'author_id': user.id, 'reason': incomplete_reason, 'missing': missing} if missing else None}
    data = build_workbook(cp, representatives, linked_users, vehicles, owned_vehicles, types, mapping, kinds, last_filled, manifest)
    media, suffix = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'
    with zipfile.ZipFile(io.BytesIO(data)) as workbook_archive:
        workbook_expanded = sum(entry.file_size for entry in workbook_archive.infolist())
    if workbook_expanded > MAX_EXPANDED:
        raise ValueError('Анкета превышает предел распакованного XLSX; строки каталога не пропущены')
    if with_files:
        if len(originals) + 1 > MAX_ENTRIES or sum(len(v) for v in originals.values()) + workbook_expanded > MAX_EXPANDED:
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
