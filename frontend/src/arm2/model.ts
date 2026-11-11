import { ApiError } from '../api';
import type { Kind, RecordData, Scalar } from './api';

export const labels: Record<string, string> = {
  name: 'Рабочее имя', full_name: 'Полное наименование', short_name: 'Краткое наименование', inn: 'ИНН', kpp: 'КПП', ogrn: 'ОГРН / ОГРНИП', legal_form: 'Организационно-правовая форма', legal_address: 'Юридический адрес', postal_address: 'Почтовый адрес', phone: 'Официальный телефон', email: 'Официальный email', bank_name: 'Банк', bik: 'БИК', settlement_account: 'Расчётный счёт', correspondent_account: 'Корреспондентский счёт', primary_signer_id: 'Основной подписант',
  requires_sts: 'СТС', requires_calibration: 'Тарировка', requires_wialon: 'Wialon', plate_number: 'Государственный номер', type_id: 'Тип техники', accounting_state: 'Состояние учёта', model: 'Модель', wialon_unit_id: 'Идентификатор машины Wialon', owner_cp_id: 'Владелец — контрагент', owner_org_id: 'Владелец — собственная организация',
  position: 'Должность', authority_basis: 'Основание полномочий', state: 'Состояние', last_name: 'Фамилия', first_name: 'Имя', middle_name: 'Отчество', user_id: 'Связанная учётная запись', version: 'Версия', status: 'Состояние описания',
};
export const requirements = ['requires_sts', 'requires_calibration', 'requires_wialon'];
export const partyFields = ['name', 'full_name', 'legal_address', 'postal_address', 'legal_form', 'short_name', 'inn', 'kpp', 'ogrn', 'phone', 'email', 'bank_name', 'bik', 'settlement_account', 'correspondent_account'];
export const repFields = ['name', 'position', 'authority_basis', 'phone', 'email', 'state', 'user_id', 'last_name', 'first_name', 'middle_name'];
export const vehicleFields = ['name', 'plate_number', 'type_id', 'accounting_state', 'model', 'wialon_unit_id', 'owner_cp_id', 'owner_org_id'];
export const isParty = (kind: Kind) => kind === 'CP' || kind === 'ORG';
export const requirementLabel = (value: Scalar | undefined) => value === true ? 'Требуется' : value === false ? 'Не требуется' : 'Не заполнено';
export const missingRequirements = (row: RecordData) => requirements.filter(key => row[key] == null).map(key => labels[key]).join(', ');
export function emptyRecord(kind: Kind | 'REP'): RecordData {
  const keys = kind === 'REP' ? repFields : kind === 'TYPE' ? ['name', ...requirements] : kind === 'VEH' ? vehicleFields : partyFields;
  const row: RecordData = { id: '', version: 0, name: '' };
  keys.forEach(key => { if (key !== 'name') row[key] = null; });
  if (kind === 'TYPE') row.requires_sts = true;
  if (kind === 'VEH') { row.plate_number = ''; row.accounting_state = 'Действующая'; }
  if (kind === 'REP') row.state = 'Действующий';
  return row;
}
export function payload(kind: Kind | 'REP', draft: RecordData, original: RecordData): Record<string, Scalar> {
  const editing = !!original.id;
  const keys = kind === 'REP' ? repFields : kind === 'TYPE' ? ['name', ...requirements] : kind === 'VEH' ? vehicleFields : [...partyFields, ...(editing ? ['primary_signer_id'] : [])];
  const body: Record<string, Scalar> = { name: draft.name };
  if (editing) body.expected_version = original.version;
  keys.forEach(key => {
    if ((kind === 'CP' || kind === 'ORG') && editing && key !== 'name' && draft[key] === original[key]) return;
    body[key] = draft[key] ?? null;
  });
  return body;
}
export function errorText(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Не удалось связаться с сервером. Повторите попытку.';
  const messages: Record<number, string> = {401: 'Сессия завершена. Войдите снова.', 403: 'Недостаточно прав. Доступен только разрешённый просмотр.', 404: 'Запись или родитель не найдены.', 409: 'Данные изменены другим оператором либо запись уже существует. Сравните с сервером или перезагрузите карточку. Автоматического перезаписывания нет.', 422: 'Проверьте заполненные поля.'};
  if (error.status >= 500) return 'Ошибка сервера. Данные формы сохранены на экране; повторите попытку.';
  const detail = error.status === 422 ? ` ${validationText(error.detail)}` : '';
  return (messages[error.status] || 'Не удалось выполнить запрос.') + detail;
}

function validationText(detail: unknown): string {
  if (Array.isArray(detail)) return detail.map(item => {
    const field = Array.isArray(item.loc) ? item.loc.at(-1) : '';
    return `${labels[field] || field || 'Поле'}: проверьте формат и заполнение`;
  }).join('; ');
  if (typeof detail !== 'string') return 'Проверьте формат и заполнение реквизитов.';
  const known: Record<string, string> = {
    'requires_sts must be True when it is specified': 'СТС: выберите «Требуется» или «Не заполнено».',
    'Invalid plate_number format': 'Государственный номер: неверный формат.',
    'Only an existing CONFIRMED TYPE can be assigned to VEH': 'Машине можно назначить только существующий тип с заполненным описанием (CONFIRMED).',
    'VEH can have only one operational owner': 'Выберите одного операционного владельца: контрагента или собственную организацию.',
    'Counterparty owner does not exist': 'Выбранный контрагент не найден.',
    'Organization owner does not exist': 'Выбранная собственная организация не найдена.',
    'Vehicle name must not be empty': 'Рабочее имя не должно быть пустым.',
    'Vehicle type name must not be empty': 'Рабочее имя не должно быть пустым.',
    'email has invalid format': 'Email: неверный формат.',
  };
  if (known[detail]) return known[detail];
  const digits = detail.match(/^(\w+) must contain exactly ([\d or]+) digits$/);
  if (digits) return `${labels[digits[1]] || digits[1]}: требуется ${digits[2].replace(' or ', ' или ')} цифр.`;
  const empty = detail.match(/^(\w+) must not be empty$/);
  if (empty) return `${labels[empty[1]] || empty[1]}: заполните поле.`;
  const length = detail.match(/^(\w+) must not exceed (\d+) characters$/);
  if (length) return `${labels[length[1]] || length[1]}: не более ${length[2]} символов.`;
  return 'Проверьте формат реквизитов и допустимость выбранных ссылок. ' + detail;
}
