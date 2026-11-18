"""Bounded ARM_IMPORT_V2 transport reader. Formulae/caches are hints, never commands."""
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
                    raise ValueError('Внешние связи XLSX запрещены')
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
        for sheet, start, end, first, last in [(passport, 3, 30, 2, 2), (vehicles, 5, max(5, vehicles.max_row), 2, 22)]:
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
        if version != 'ARM_IMPORT_V2':
            raise ValueError('Поддерживается анкета ARM_IMPORT_V2')
        try:
            date_value = passport['B4'].value if passport['B4'].data_type != 'f' else None
            completed = filled_date(date_value)
        except ValueError as error:
            completed = None
            warnings.append(str(error))
        cp = {field: cell(passport, f'B{row}') for row, field in CP_CELLS.items()}
        cp['name'] = cp['short_name'] or cp['full_name'] or ' '.join(cp[f] for f in ('last_name', 'first_name', 'middle_name') if cp[f])
        signer = {field: cell(passport, f'B{row}') for row, field in ((20, 'name'), (21, 'position'), (22, 'authority_basis'))}
        result = {'schema': version, 'filled_date': completed, 'cp': cp, 'cp_hint': cell(passport, 'B6'),
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
        return result, blobs
    finally:
        book.close()
        cached.close()


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def parse(data, filename):
    try:
        return _parse(data, filename)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, OverflowError):
        raise ValueError('Повреждённый или неподдерживаемый ZIP/XLSX') from None
