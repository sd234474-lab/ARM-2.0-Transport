"""V4 questionnaire for contractor review and return to ARM2."""
import io
import zipfile
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

from app.arm2.counterparty_rules import official_names
from app.arm2.questionnaire_parse import (
    CP_CELLS,
    EXPORT_VERSION,
    MANIFEST,
    MANIFEST_CHUNK_SIZE,
    MAX_MANIFEST_ROWS,
    PASSPORT,
    REP_COLUMNS,
    REPRESENTATIVES,
    VEHICLE_SELECTOR_HEADER,
    VEHICLES,
    seal,
)
from app.permissions import Permission, has_permission

PASSPORT_LABELS = {
    3: 'Версия формата', 4: 'Дата заполнения контрагентом', 5: 'Организационно-правовая форма',
    6: 'Контрагент — постоянный CP-ID', 7: 'Служебная строка V2 (не используется)',
    8: 'Фамилия контрагента — ИП/ФЛ', 9: 'Имя контрагента — ИП/ФЛ', 10: 'Отчество контрагента — ИП/ФЛ',
    11: 'Полное наименование', 12: 'Краткое наименование', 13: 'ИНН', 14: 'КПП', 15: 'ОГРН / ОГРНИП',
    16: 'Юридический / регистрационный адрес', 17: 'Почтовый адрес', 18: 'Основной телефон',
    19: 'Электронная почта', 20: 'Основной подписант — имя / ФИО', 21: 'Должность подписанта',
    22: 'Основание полномочий подписанта', 23: 'Наименование банка', 24: 'БИК — 9 цифр, текст',
    25: 'Расчётный счёт — 20 цифр, текст', 26: 'Корреспондентский счёт — 20 цифр, текст',
    27: 'Карточка предприятия приложена?', 28: 'Имя файла карточки предприятия',
    29: 'Подтверждение достоверности', 30: 'Комментарий',
}
VEH_HEADERS = ['№', VEHICLE_SELECTOR_HEADER, 'Статус строки', 'Тип техники (из АРМ)', 'Госномер (из АРМ)',
    'Короткое имя (из АРМ)', 'Модель (из АРМ)', 'Тип новой техники / предложение',
    'Госномер новой техники', 'Короткое имя / предложение', 'Модель / предложение',
    'Объём, м³ (только предложение)', 'Справка о тарировке приложена?', 'Имя файла справки',
    'СТС приложено?', 'Имя файла СТС', 'Диагностика номера (подсказка)', 'Контроль комплекта (подсказка)',
    'CP-ID (справочно)', 'Ошибка в старой карточке?', 'Описание расхождения', 'Legacy V3 ID (только справка)']
REP_HEADERS = ['№', 'REP-ID (из АРМ)', 'Имя / ФИО', 'Должность / функция', 'Телефон', 'Электронная почта',
    'Основание полномочий', 'Состояние контакта', 'Текущий доступ к АРМ (справочно)',
    'Пользователь АРМ (справочно)', 'Основной подписант (справочно)', 'Комментарий контрагента']

CONTRACTOR_INSTRUCTIONS = (
    ('Как пользоваться анкетой', 'Сохраните последнюю анкету, полученную от администратора. '
     'При изменениях откройте её, заполните только изменившиеся поля и верните файл администратору. '
     'Укажите дату заполнения в паспорте.'),
    ('Паспорт контрагента', 'Проверьте реквизиты стороны договора. Она может отличаться от собственника в СТС. '
     'При необходимости исправьте адреса, '
     'телефон, почту и банковские реквизиты. Вводите БИК и счета как текст, со всеми нулями. '
     'Для ИП/ФЛ заполните ФИО. Полное и краткое наименования рассчитываются из него.'),
    ('Представители и подписант', 'Один человек — одна строка на листе «Представители». '
     'В строке можно уточнить ФИО, должность, контакты и основание полномочий. '
     'Если меняется основной подписант, укажите это в комментарии. Сведения о доступе приведены для справки.'),
    ('Техника из справочника', 'В столбце B выберите машину по госномеру. В C укажите «ИЗ СПРАВОЧНИКА». '
     'Тип, госномер, короткое имя и модель заполняются в D–G. Эти ячейки предназначены для просмотра.'),
    ('Уточнение техники', 'Если поменялись тип, короткое имя или модель, укажите новое значение в H, J или K '
     'соответственно. Если неверен госномер или есть другое расхождение, отметьте его в T и опишите в U.'),
    ('Новая техника', 'Используйте свободную строку. Оставьте B пустым, в C выберите «НОВАЯ» и заполните H–K. '
     'Тип выбирается по названию. Госномер вводите без пробелов, кириллицей. Примеры: 8500УХ86 и К227СА186.'),
    ('Выбывшая техника', 'Оставьте выбранный госномер в B и поставьте «ВЫБЫЛО» в C. '
     'Это предложение снять связь машины с контрагентом; удаление строки из анкеты эту связь сохраняет.'),
    ('Документы на технику', 'При первой подаче сведений о машине приложите свидетельство о регистрации. '
     'Для самосвала укажите объём в м³ по справке о тарировке и приложите справку. '
     'В M–P отметьте наличие файлов и впишите их имена с расширениями. При изменении документа приложите новый файл.'),
    ('Карточка предприятия', 'Прикрепите карточку предприятия и укажите имя файла в паспорте. '
     'Отметьте подтверждение достоверности сведений перед отправкой.'),
    ('Что отправить', 'Отправьте XLSX, если изменились только сведения. Если есть вложения, отправьте один ZIP '
     'с анкетой XLSX и отдельными документами, например PDF или JPG. Файлы указывайте под теми же именами, что в ZIP.'),
    ('Проверка перед отправкой', 'Проверьте госномера, названия файлов и заполнение изменённых полей. '
     'Жёлтые ячейки предназначены для ввода, голубые содержат справочные данные и формулы. '
     'Изменения вступят в силу после сверки администратором.'),
)


def text(sheet, coordinate, value):
    cell = sheet[coordinate]
    cell.value = '' if value is None else str(value)
    cell.data_type, cell.number_format = 's', '@'  # User content is never an Excel formula.


def validation(sheet, cells, formula):
    rule = DataValidation(type='list', formula1=formula, allow_blank=True, showErrorMessage=True)
    rule.errorTitle, rule.error = 'Выберите значение', 'Используйте значение из списка'
    sheet.add_data_validation(rule)
    rule.add(cells)


def build_workbook(cp, representatives, linked_users, vehicles, owned, types, mapping, kinds, filled_date, manifest):
    book = Workbook()
    book.remove(book.active)
    instructions = book.create_sheet('Инструкция')
    text(instructions, 'A1', 'Анкета контрагента и техники')
    instructions.merge_cells('A1:B1')
    instructions['A1'].font = Font(bold=True, size=14, color='FFFFFF')
    instructions['A1'].fill = PatternFill('solid', fgColor='24527A')
    instructions.row_dimensions[1].height = 25
    instructions.column_dimensions['A'].width = 31
    instructions.column_dimensions['B'].width = 98
    for row, (heading, content) in enumerate(CONTRACTOR_INSTRUCTIONS, 3):
        text(instructions, f'A{row}', heading)
        text(instructions, f'B{row}', content)
        instructions[f'A{row}'].font = Font(bold=True, color='24527A')
        instructions.row_dimensions[row].height = 58
        for cell in instructions[row]:
            cell.alignment = Alignment(wrap_text=True, vertical='center')
            if row % 2:
                cell.fill = PatternFill('solid', fgColor='E7EEF5')
    passport = book.create_sheet(PASSPORT)
    for row, label in PASSPORT_LABELS.items():
        text(passport, f'A{row}', label)
    text(passport, 'A1', 'Паспорт контрагента')
    for coordinate, value in {'B3': EXPORT_VERSION, 'B4': filled_date, 'B6': cp.id}.items():
        text(passport, coordinate, value)
    for row, field in CP_CELLS.items():
        names = official_names({f: getattr(cp, f) for f in ('type', 'legal_form', 'last_name', 'first_name', 'middle_name', 'full_name', 'short_name')})
        text(passport, f'B{row}', names[0] if field == 'full_name' else names[1] if field == 'short_name' else getattr(cp, field))
    text(passport, 'A5', 'Тип контрагента')
    text(passport, 'A7', 'ОПФ: у ФЛ ИП или пусто; у ЮЛ свободный текст')
    validation(passport, 'B5', '"Физическое лицо,Юридическое лицо"')
    # A dependent list offers ИП/blank for FL; UL remains free text without VBA.
    # Excel guidance is deliberately not the trust boundary: the server checks the pair.
    opf = DataValidation(type='list', formula1='IF($B$5="Физическое лицо",ARM2_PERSON_OPF,ARM2_EMPTY_OPF)',
                         allow_blank=True, showErrorMessage=False, showInputMessage=True)
    opf.promptTitle = 'ОПФ'
    opf.prompt = 'ФЛ: ИП или пусто. ЮЛ: введите любую ОПФ. Итоговую пару проверит АРМ.'
    passport.add_data_validation(opf)
    opf.add('B7')
    for coordinate in ['B27', 'B29']:
        validation(passport, coordinate, '"ДА,НЕТ"')
    rep_sheet = book.create_sheet(REPRESENTATIVES)
    text(rep_sheet, 'A1', 'Представители — один человек в одной строке')
    for col, label in enumerate(REP_HEADERS, 1):
        text(rep_sheet, rep_sheet.cell(4, col).coordinate, label)
    cache = {}

    def formula(sheet, coordinate, expression, cached):
        sheet[coordinate] = expression
        cache.setdefault(sheet.title, {})[coordinate] = '' if cached is None else str(cached)
    if cp.type == 'Физическое лицо':
        full, short = official_names({f: getattr(cp, f) for f in ('type', 'legal_form', 'last_name', 'first_name', 'middle_name', 'full_name', 'short_name')})
        formula(passport, 'B11', '=IF(B5="Физическое лицо",IF(B7="ИП","Индивидуальный предприниматель ","")&TRIM(B8&" "&B9&" "&B10),"")', full)
        formula(passport, 'B12', '=IF(B5="Физическое лицо",IF(B7="ИП","ИП ","")&TRIM(B8&" "&IF(B9="","",LEFT(B9,1)&".")&IF(B10="","",LEFT(B10,1)&".")),"")', short)

    for index, rep in enumerate(representatives, 5):
        text(rep_sheet, f'A{index}', index - 4)
        text(rep_sheet, f'B{index}', rep.id)
        for col, field in REP_COLUMNS.items():
            text(rep_sheet, f'{col}{index}', getattr(rep, field))
        user = linked_users.get(rep.user_id)
        access = 'Нет связанной учётной записи' if user is None else (
            ('Есть доступ к АРМ' if any(has_permission(user, p) for p in Permission if p.value.startswith('arm2_'))
             else 'Нет доступа к АРМ; учётная запись активна')
            if user.active and user.deleted_at is None else 'Учётная запись недоступна')
        text(rep_sheet, f'I{index}', access)
        text(rep_sheet, f'J{index}', f'{user.id} · {user.login}' if user else '')
        text(rep_sheet, f'K{index}', 'ДА' if cp.primary_signer_id == rep.id else '')
        if cp.primary_signer_id == rep.id:
            for row, col, field in [(20, 'C', 'name'), (21, 'D', 'position'), (22, 'G', 'authority_basis')]:
                formula(passport, f'B{row}', f"='{REPRESENTATIVES}'!{col}{index}", getattr(rep, field))
    rep_end = min(1004, max(34, len(representatives) + 14))
    validation(rep_sheet, f'H5:H{rep_end}', '"Действующий,Архивный"')
    for row in range(len(representatives) + 5, rep_end + 1):
        text(rep_sheet, f'A{row}', row - 4)
    sheet = book.create_sheet(VEHICLES)
    text(sheet, 'A1', 'Техника контрагента — действующие и предложенные сведения')
    for col, label in enumerate(VEH_HEADERS, 1):
        text(sheet, sheet.cell(4, col).coordinate, label)
    catalog = book.create_sheet('Справочники')
    text(catalog, 'J1', 'ОПФ физического лица')
    text(catalog, 'J2', 'ИП')
    text(catalog, 'J3', '')
    book.defined_names.add(DefinedName('ARM2_PERSON_OPF', attr_text="'Справочники'!$J$2:$J$3"))
    book.defined_names.add(DefinedName('ARM2_EMPTY_OPF', attr_text="'Справочники'!$J$3"))
    for col, label in enumerate(['VEH-ID', 'Госномер', 'TYPE-ID', 'Тип', 'Имя', 'Модель', 'Учётное состояние'], 1):
        text(catalog, catalog.cell(1, col).coordinate, label)
    for row, vehicle in enumerate(sorted(vehicles, key=lambda v: v.plate_number), 2):
        typ = types.get(vehicle.type_id)
        values = [vehicle.id, vehicle.plate_number, typ.id if typ else '', typ.name if typ else '',
                  vehicle.name, vehicle.model, vehicle.accounting_state]
        for col, value in enumerate(values, 1):
            text(catalog, catalog.cell(row, col).coordinate, value)
    catalog.sheet_state = 'veryHidden'
    book.defined_names.add(DefinedName('ARM2_VEH_PLATES', attr_text=f"'Справочники'!$B$2:$B${max(2,len(vehicles)+1)}"))
    type_sheet = book.create_sheet('_ARM2_TYPES')
    confirmed_types = sorted((typ for typ in types.values() if typ.status == 'CONFIRMED'), key=lambda typ: typ.name.casefold())
    for row, typ in enumerate(confirmed_types, 2):
        text(type_sheet, f'A{row}', typ.id)
        text(type_sheet, f'B{row}', typ.name)
    type_sheet.sheet_state = 'veryHidden'
    book.defined_names.add(DefinedName('ARM2_TYPE_NAMES', attr_text=f"'_ARM2_TYPES'!$B$2:$B${max(2,type_sheet.max_row)}"))
    validation(sheet, 'B5:B1004', 'ARM2_VEH_PLATES')
    validation(sheet, 'H5:H1004', 'ARM2_TYPE_NAMES')
    validation(sheet, 'C5:C1004', '"ИЗ СПРАВОЧНИКА,НОВАЯ,ВЫБЫЛО"')
    for col in ('M', 'O'):
        validation(sheet, f'{col}5:{col}1004', '"ДА,НЕТ"')
    validation(sheet, 'T5:T1004', '"НЕТ,ЕСТЬ РАСХОЖДЕНИЕ"')
    for row in range(5, 1005):
        current = owned[row - 5] if row - 5 < len(owned) else None
        typ = types.get(current.type_id) if current else None
        text(sheet, f'A{row}', row - 4)
        if current:
            text(sheet, f'B{row}', current.plate_number)
            text(sheet, f'C{row}', 'ИЗ СПРАВОЧНИКА')
            for item in mapping:
                if item['parent_id'] == current.id and kinds.get(item['document_id']) in ('СТС', 'Тарировка'):
                    is_sts = kinds[item['document_id']] == 'СТС'
                    text(sheet, f'{"P" if is_sts else "N"}{row}', item['name'])
                    text(sheet, f'{"O" if is_sts else "M"}{row}', 'ДА')
        for col, index, cached in [('D', 3, typ.name if typ else ''), ('E', 1, current.plate_number if current else ''),
                ('F', 4, current.name if current else ''), ('G', 5, current.model if current else '')]:
            lookup = f"VLOOKUP($B{row},'Справочники'!$B$2:$G${max(2,len(vehicles)+1)},{index},FALSE)"
            formula(sheet, f'{col}{row}', f'=IF(AND($B{row}<>"",OR(C{row}="ИЗ СПРАВОЧНИКА",C{row}="ВЫБЫЛО")),'
                f'IFERROR(IF({lookup}=0,"",{lookup}),""),"")', cached)
        formula(sheet, f'Q{row}', f'=IF(C{row}="","",IF(IF(C{row}="НОВАЯ",I{row},E{row})="","НЕТ ГОСНОМЕРА","ПРОВЕРЯЕТСЯ В АРМ"))',
                'ПРОВЕРЯЕТСЯ В АРМ' if current else '')
        formula(sheet, f'R{row}', f'=IF(C{row}="","",IF(C{row}="ВЫБЫЛО","СНЯТИЕ СВЯЗИ — РЕШЕНИЕ ОПЕРАТОРА","СВЕРКА ДОКУМЕНТОВ В АРМ"))',
                'СВЕРКА ДОКУМЕНТОВ В АРМ' if current else '')
        formula(sheet, f'S{row}', f'=IF(C{row}="","",\'{PASSPORT}\'!$B$6)', cp.id if current else '')
    cp_files = [f for f in mapping if f['parent_type'] == 'CP']
    if len(cp_files) == 1:
        text(passport, 'B27', 'ДА')
        text(passport, 'B28', cp_files[0]['name'])
    service = book.create_sheet(MANIFEST)
    signed = seal({**manifest, 'vehicle_selector': 'plate',
                   'vehicle_refs': {vehicle.plate_number: vehicle.id for vehicle in vehicles},
                   'type_refs': {typ.name: typ.id for typ in confirmed_types}})
    if len(signed) > MAX_MANIFEST_ROWS * MANIFEST_CHUNK_SIZE:
        raise ValueError('Каталог техники не помещается в защищённую выгрузку; строки не пропущены')
    for offset in range(0, len(signed), MANIFEST_CHUNK_SIZE):
        text(service, f'A{offset//MANIFEST_CHUNK_SIZE+1}', signed[offset:offset+MANIFEST_CHUNK_SIZE])
    service.sheet_state = 'veryHidden'
    for column in ('S', 'V'):
        sheet.column_dimensions[column].hidden = True
    edge = Side(style='thin', color='C3CFD9')
    cell_border = Border(left=edge, right=edge, top=edge, bottom=edge)
    for ws in (passport, rep_sheet, sheet):
        ws.freeze_panes = 'C5' if ws is not passport else 'B5'
        ws.column_dimensions['A'].width = 43 if ws is passport else 6
        ws.column_dimensions['B'].width = 48 if ws is passport else 24
        for col in range(3, ws.max_column + 1):
            ws.column_dimensions[ws.cell(4, col).column_letter].width = 25
        if ws is not passport:
            ws.auto_filter.ref = f'A4:{ws.cell(ws.max_row, ws.max_column).coordinate}'
            ws.row_dimensions[4].height = 34
        for row in ws:
            for cell in row:
                if (ws is passport and cell.row >= 3) or (ws is not passport and cell.row >= 4):
                    cell.border = cell_border
                if ws is not passport and cell.row == 4:
                    cell.font, cell.fill = Font(bold=True, color='FFFFFF'), PatternFill('solid', fgColor='24527A')
                    cell.alignment = Alignment(wrap_text=True, vertical='center')
                elif (cell.data_type == 'f' or (ws is rep_sheet and cell.column in (2, 9, 10, 11))
                      or (ws is sheet and cell.column in (4, 5, 6, 7))):
                    cell.fill = PatternFill('solid', fgColor='E7EEF5')
                else:
                    cell.fill = PatternFill('solid', fgColor='FFF8DC')
                    if cell.value is None:
                        cell.number_format = '@'
    out = io.BytesIO()
    sheet_files = {f'xl/worksheets/sheet{i}.xml': ws.title for i, ws in enumerate(book, 1)}
    book.save(out)
    book.close()
    # OOXML caches are computed only from current server data, never by executing input formulae.
    cached_out = io.BytesIO()
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as source, zipfile.ZipFile(cached_out, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry)
            values = cache.get(sheet_files.get(entry.filename), {})
            if values:
                root = ET.fromstring(content)
                for cell in root.iter(ns + 'c'):
                    if cell.get('r') in values:
                        cell.set('t', 'str')
                        value = cell.find(ns + 'v')
                        if value is None:
                            value = ET.SubElement(cell, ns + 'v')
                        value.text = values[cell.get('r')]
                content = ET.tostring(root, encoding='utf-8')
            target.writestr(entry.filename, content)
    return cached_out.getvalue()
