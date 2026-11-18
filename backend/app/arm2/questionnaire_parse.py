"""Bounded V2/V3 transport reader. Formulae/caches are hints, never commands."""
import base64
import hashlib
import hmac
import io
import json
import re
import stat
import unicodedata
import zipfile
import zlib
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.config import settings

MAX_COMPRESSED = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_ENTRIES = 256
MAX_VEHICLES = 1000
PASSPORT = 'Паспорт контрагента'
VEHICLES = 'Техника'
MANIFEST = '_ARM2_QUESTIONNAIRE'
REPRESENTATIVES = 'Представители'
EXPORT_VERSION = 'ARM_IMPORT_V3'
SUPPORTED_VERSIONS = {'ARM_IMPORT_V2', EXPORT_VERSION}
REP_COLUMNS = {'C': 'name', 'D': 'position', 'E': 'phone', 'F': 'email', 'G': 'authority_basis', 'H': 'state'}
CP_CELLS = {5: 'legal_form', 8: 'last_name', 9: 'first_name', 10: 'middle_name', 11: 'full_name',
            12: 'short_name', 13: 'inn', 14: 'kpp', 15: 'ogrn', 16: 'legal_address', 17: 'postal_address',
            18: 'phone', 19: 'email', 23: 'bank_name', 24: 'bik', 25: 'settlement_account', 26: 'correspondent_account'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def seal(body):
    key = settings().arm2_table_signing_key
    if len(key) < 32:
        raise ValueError('Для защищённой обратной привязки настройте ключ табличного обмена')
    return canonical({'body': body, 'mac': hmac.new(key.encode(), ('B5:' + canonical(body)).encode(), hashlib.sha256).hexdigest()})


def unseal(value):
    try:
        signed = json.loads(value)
        if not hmac.compare_digest(seal(signed['body']), canonical(signed)):
            return None
        return signed['body'] if signed['body'].get('schema') == 'ARM2_QUESTIONNAIRE_1' else None
    except (ValueError, TypeError, KeyError):
        return None


def archive_entries(data, limit=MAX_ENTRIES, expanded_limit=MAX_EXPANDED):
    if len(data) > MAX_COMPRESSED:
        raise ValueError('Транспорт больше 32 МиБ')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        if len(entries) > limit or sum(e.file_size for e in entries) > expanded_limit:
            raise ValueError('Превышен лимит содержимого ZIP')
        names = set()
        for entry in entries:
            # ZipInfo normalizes backslashes on Windows, so validate its raw name too.
            name = unicodedata.normalize('NFC', entry.orig_filename)
            path = PurePosixPath(name)
            normalized = name.rstrip('/').casefold()
            if (not name or len(name) > 255 or name.startswith(('/', '\\')) or '\\' in name or ':' in name
                    or any(p in ('.', '..') for p in name.split('/'))
                    or path.is_absolute() or len(path.parts) > 4 or normalized in names
                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                    or any(ord(c) < 32 for c in name) or entry.file_size > max(1, entry.compress_size) * 1000):
                raise ValueError('Небезопасный путь, ссылка, дубликат или ZIP-бомба')
            names.add(normalized)
        return archive
    except (zipfile.BadZipFile, OSError, NotImplementedError):
        raise ValueError('Повреждённый ZIP/XLSX') from None


def safe_workbook(data, expanded_limit=MAX_EXPANDED):
    stripped, hyperlink_sheets = {}, set()
    with archive_entries(data, 4096, expanded_limit) as archive:
        if 'xl/workbook.xml' not in archive.namelist():
            raise ValueError('Отсутствует книга XLSX')
        for entry in archive.infolist():
            lower = entry.filename.casefold()
            if 'vbaproject' in lower or 'externallink' in lower or '/embeddings/' in lower:
                raise ValueError('Макросы и внешние связи XLSX запрещены')
            if lower.endswith(('.xml', '.rels')):
                content = archive.read(entry)
                normalized_xml = content.replace(b'\x00', b'').lower()
                if (lower.endswith('.rels') or lower == '[content_types].xml') and any(t in normalized_xml for t in (b'macroenabled', b'vbaproject')):
                    raise ValueError('Макросы XLSX запрещены')
                if b'<!doctype' in normalized_xml or b'<!entity' in normalized_xml:
                    raise ValueError('DTD/XXE запрещены')
                if lower.endswith('.rels') and re.search(br'TargetMode\s*=\s*[\"\x27]External', content.replace(b'\x00', b''), re.I):
                    try:
                        relationships = ET.fromstring(content)
                    except ET.ParseError:
                        raise ValueError('Повреждённые связи XLSX') from None
                    for rel in list(relationships):
                        if rel.get('TargetMode', '').lower() != 'external':
                            continue
                        # Filled V2 samples contain ordinary email/web hyperlinks.
                        # Discard them before openpyxl; never follow or persist targets.
                        if (not lower.startswith('xl/worksheets/_rels/') or
                                rel.get('Type') != 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink' or
                                urlsplit(rel.get('Target', '')).scheme.lower() not in {'http', 'https', 'mailto'}):
                            raise ValueError('Внешние связи XLSX запрещены')
                        relationships.remove(rel)
                    stripped[entry.filename] = ET.tostring(relationships, encoding='utf-8')
                    hyperlink_sheets.add(entry.filename.replace('/_rels/', '/').removesuffix('.rels'))
        if stripped:
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as sanitized:
                for entry in archive.infolist():
                    content = stripped.get(entry.filename, archive.read(entry))
                    if entry.filename in hyperlink_sheets:
                        try:
                            sheet = ET.fromstring(content)
                        except ET.ParseError:
                            raise ValueError('Повреждённый лист XLSX') from None
                        for links in list(sheet):
                            if links.tag == '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}hyperlinks':
                                sheet.remove(links)
                        content = ET.tostring(sheet, encoding='utf-8')
                    sanitized.writestr(entry.filename, content)
            data = output.getvalue()
    try:
        return load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False), \
            load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception:
        raise ValueError('Не удалось прочитать XLSX') from None


def filled_date(value):
    if value is None or value == '':
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    raw = str(value).strip().lower()
    months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября',
              'октября', 'ноября', 'декабря']
    match = re.fullmatch(r'(\d{1,2})\s+([а-я]+)\s+(\d{4})\s*г\.?', raw)
    try:
        if match and match[2] in months:
            return date(int(match[3]), months.index(match[2]) + 1, int(match[1])).isoformat()
        for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
    except ValueError:
        pass
    raise ValueError('Дата заполнения не распознана однозначно')


def _parse(data, filename):
    """Return structured proposals and attachment bytes. No application records/files are created."""
    blobs, workbook = {}, data
    if filename.lower().endswith('.zip'):
        with archive_entries(data) as archive:
            books = [e for e in archive.infolist() if e.filename.lower().endswith('.xlsx') and not e.is_dir()]
            if len(books) != 1:
                raise ValueError('ZIP должен содержать ровно одну XLSX анкету')
            workbook = archive.read(books[0])
            blobs = {e.filename: archive.read(e) for e in archive.infolist() if not e.is_dir() and e != books[0]}
    elif not filename.lower().endswith('.xlsx'):
        raise ValueError('Выберите XLSX или ZIP')
    book, cached = safe_workbook(workbook, MAX_EXPANDED - sum(len(data) for data in blobs.values()))
    warnings = []
    try:
        if PASSPORT not in book.sheetnames or VEHICLES not in book.sheetnames:
            raise ValueError('Нет листов «Паспорт контрагента» и «Техника»')
        passport, vehicles = book[PASSPORT], book[VEHICLES]
        if vehicles.max_row > MAX_VEHICLES + 4:
            raise ValueError('В анкете больше 1000 строк техники')
        # Streaming each sheet once avoids quadratic reparsing of 1000 formula rows.
        grids, cached_grids = {}, {}
        sheets = [(passport, 3, 30, 2, 2), (vehicles, 5, max(5, vehicles.max_row), 2, 22)]
        if REPRESENTATIVES in book.sheetnames:
            reps = book[REPRESENTATIVES]
            if reps.max_row > 1004:
                raise ValueError('В анкете больше 1000 представителей')
            sheets.append((reps, 5, max(5, reps.max_row), 2, 12))
        for sheet, start, end, first, last in sheets:
            grids[sheet.title] = {f'{get_column_letter(col)}{row}': (cell.value, cell.data_type == 'f')
                for row, cells in enumerate(sheet.iter_rows(min_row=start, max_row=end, min_col=first, max_col=last), start)
                for col, cell in enumerate(cells, first)}
            cached_grids[sheet.title] = {f'{get_column_letter(col)}{row}': value
                for row, cells in enumerate(cached[sheet.title].iter_rows(min_row=start, max_row=end, min_col=first, max_col=last, values_only=True), start)
                for col, value in enumerate(cells, first)}

        def cell(sheet, coordinate):
            value, formula = grids[sheet.title].get(coordinate, (None, False))
            if formula:
                value = cached_grids[sheet.title].get(coordinate)
            if value is None:
                return ''
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            value = str(value).strip()
            if len(value) > 4096 or '\x00' in value:
                raise ValueError('Недопустимый размер/содержимое ячейки')
            return value

        version = cell(passport, 'B3')
        if version not in SUPPORTED_VERSIONS:
            raise ValueError('Поддерживаются анкеты ARM_IMPORT_V2 и ARM_IMPORT_V3')
        try:
            date_value = passport['B4'].value if passport['B4'].data_type != 'f' else cached[PASSPORT]['B4'].value
            completed = filled_date(date_value)
        except ValueError as error:
            completed = None
            warnings.append(str(error))
        cp = {field: cell(passport, f'B{row}') for row, field in CP_CELLS.items()}
        cp_errors = {}
        for coordinate, field in [('B25', 'settlement_account'), ('B26', 'correspondent_account')]:
            value = passport[coordinate]
            cached_value = cached[PASSPORT][coordinate]
            if (value.data_type == 'n' and value.value is not None) or (
                    value.data_type == 'f' and cached_value.data_type == 'n' and cached_value.value is not None):
                cp_errors[field] = 'Счёт записан числом Excel: точность и нули не гарантированы. Уточните исходную текстовую запись'
        cp['name'] = cp['short_name'] or cp['full_name'] or ' '.join(cp[f] for f in ('last_name', 'first_name', 'middle_name') if cp[f])
        signer = {field: cell(passport, f'B{row}') for row, field in ((20, 'name'), (21, 'position'), (22, 'authority_basis'))}
        result = {'schema': version, 'filled_date': completed, 'cp': cp, 'cp_errors': cp_errors, 'cp_hint': cell(passport, 'B6'),
                  'representatives': [signer] if signer['name'] else [], 'vehicles': [], 'warnings': warnings,
                  'company_file': cell(passport, 'B28'), 'company_declared': cell(passport, 'B27'),
                  'confirmation_hint': cell(passport, 'B29'), 'comment': cell(passport, 'B30'), 'manifest': None}
        if vehicles.max_row > MAX_VEHICLES + 4:
            raise ValueError('В анкете больше 1000 строк техники')
        for row in range(5, min(vehicles.max_row, MAX_VEHICLES + 4) + 1):
            values = {col: cell(vehicles, f'{col}{row}') for col in 'BCDEFGHIJKLMNOPQRSTUV'}
            if not any(values.values()):
                continue
            selector = values['B']
            identifier = re.search(r'\bVEH-\d{6,}\b', selector)
            result['vehicles'].append({'key': f'v:{row}', 'row': row, 'id': identifier[0] if identifier else '',
                'plate_number': values['I'] or values['E'], 'type_hint': values['H'] or values['D'],
                'name': values['J'] or values['F'], 'model': values['K'] or values['G'], 'state_hint': values['C'],
                'status_error': ('Выберите режим строки: ИЗ СПРАВОЧНИКА, НОВАЯ или ВЫБЫЛО'
                    if version == EXPORT_VERSION and values['C'].upper() not in {'ИЗ СПРАВОЧНИКА', 'НОВАЯ', 'ВЫБЫЛО'} else None),
                'volume_proposal': values['L'], 'calibration_declared': values['M'], 'calibration_file': values['N'],
                'sts_declared': values['O'], 'sts_file': values['P'], 'owner_hint': values['S'],
                'diagnostic_hint': ' · '.join(v for v in (values['Q'], values['R'], values['T'], values['U']) if v),
                'legacy_hint': values['V']})
        if MANIFEST in book.sheetnames:
            if book[MANIFEST].max_row > 256 or book[MANIFEST].max_column != 1:
                raise ValueError('Недопустимый размер служебного манифеста')
            chunks = [str(row[0]) for row in book[MANIFEST].iter_rows(values_only=True) if row and row[0]]
            result['manifest'] = unseal(''.join(chunks))
            if not result['manifest']:
                warnings.append('Служебный манифест не подтверждён; обратная привязка требует выбора оператора')
        if version == EXPORT_VERSION:
            if REPRESENTATIVES in book.sheetnames:
                representative_rows(book[REPRESENTATIVES], cell, signer, result)
            else:
                result['representative_meta'] = [{'id': (result.get('manifest') or {}).get('primary_signer_id'),
                    'row': None, 'signer_hint': True, 'conflicts': {}} for _ in result['representatives']]
        return result, blobs
    finally:
        book.close()
        cached.close()


def representative_rows(sheet, cell, passport_signer, result):
    """Two signer projections, one proposal; identity is never inferred from a name."""
    representatives, metadata = [], []
    for row in range(5, sheet.max_row + 1):
        values = {field: cell(sheet, f'{col}{row}') for col, field in REP_COLUMNS.items()}
        identifier = cell(sheet, f'B{row}')
        if not identifier and not any(values.values()):
            continue
        representatives.append(values)
        metadata.append({'id': identifier, 'row': row, 'signer_hint': cell(sheet, f'K{row}').upper() == 'ДА',
                         'comment': cell(sheet, f'L{row}'), 'conflicts': {}})
    manifest = result.get('manifest') or {}
    signer_id = manifest.get('primary_signer_id')
    matches = [i for i, meta in enumerate(metadata) if (meta['id'] == signer_id if signer_id else meta['signer_hint'])]
    if any(passport_signer.values()):
        if len(matches) == 1:
            index = matches[0]
            for field, value in passport_signer.items():
                listed = representatives[index].get(field)
                if value and listed and value != listed:
                    metadata[index]['conflicts'][field] = {'passport': value, 'list': listed}
                elif value and not listed:
                    representatives[index][field] = value
        else:
            # Missing/ambiguous linkage is a separate proposal for explicit matching,
            # not an automatic name-based merge or deletion of another contact.
            representatives.append(passport_signer)
            metadata.append({'id': signer_id or '', 'row': None, 'signer_hint': True, 'conflicts': {}})
            result['warnings'].append('Подписант паспорта не связан с единственной строкой: явно сопоставьте его с представителем')
    result['representatives'], result['representative_meta'] = representatives, metadata


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def parse(data, filename):
    try:
        return _parse(data, filename)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, OverflowError):
        raise ValueError('Повреждённый или неподдерживаемый ZIP/XLSX') from None
