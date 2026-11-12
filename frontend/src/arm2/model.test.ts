import { describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import { all } from './api';
import { emptyRecord, errorText, payload, requirementLabel, shortRepresentativeName } from './model';
vi.mock('../api', async importOriginal => ({...await importOriginal<typeof import('../api')>(), api: vi.fn()}));

it.each([
  ['Эдуард', 'Эдуард'], ['Иванов Иван Иванович', 'Иванов И.И.'], ['Сидорова Анна', 'Сидорова А.'],
  ['Анна-Мария де Соуза', 'Анна-Мария де Соуза'], ['Иванов-Петров Иван', 'Иванов-Петров Иван'],
  ['Доктор Иванов', 'Доктор Иванов'], ['Иванов И.И.', 'Иванов И.И.'],
  ['четыре слова без разбора', 'четыре слова без разбора'], [' Иванов Иван ', 'Иванов И.'],
])('representative display %s → %s leaves the source untouched', (source, expected) => {
  const record = {...emptyRecord('REP'), name: source};
  expect(shortRepresentativeName(record.name)).toBe(expected);
  expect(record.name).toBe(source);
  expect(payload('REP', record, record).name).toBe(source);
});

describe('ARM2 write contracts', () => {
  it('preserves false versus null in TYPE and never sends server-only fields', () => {
    const original = {...emptyRecord('TYPE'), id: 'TYPE-1', version: 7, status: 'REVIEW'};
    expect(payload('TYPE', {...original, requires_calibration: false}, original)).toEqual({name: '', expected_version: 7, requires_sts: true, requires_calibration: false, requires_wialon: null});
    expect(requirementLabel(false)).toBe('Не требуется'); expect(requirementLabel(null)).toBe('Не заполнено');
  });
  it.each(['CP', 'ORG'] as const)('%s sends changed fields and explicit clears, preserving leading zeros', kind => {
    const original = {...emptyRecord(kind), id: kind + '-1', version: 3, full_name: 'Полное', bank_name: 'Банк', bik: '000000001'};
    expect(payload(kind, {...original, bik: '000000002', bank_name: null}, original)).toEqual({name: '', expected_version: 3, bik: '000000002', bank_name: null});
    expect(payload(kind, original, original)).toEqual({name: '', expected_version: 3});
    expect(payload(kind, emptyRecord(kind), emptyRecord(kind))).not.toHaveProperty('primary_signer_id');
  });
  it('VEH PUT switches both owner fields explicitly and retains other fields', () => {
    const original = {...emptyRecord('VEH'), id: 'VEH-1', version: 8, owner_cp_id: 'CP-1', model: 'Модель'};
    expect(payload('VEH', {...original, owner_cp_id: null, owner_org_id: 'ORG-1'}, original)).toEqual({name: '', expected_version: 8, plate_number: '', accounting_state: 'Действующая', model: 'Модель', wialon_unit_id: null, type_id: null, owner_cp_id: null, owner_org_id: 'ORG-1'});
  });
  it('REP PUT retains all fields including informational user link', () => {
    const original = {...emptyRecord('REP'), id: 'CPREP-1', version: 2, user_id: 12, phone: '123'};
    const body = payload('REP', {...original, position: 'Директор'}, original);
    expect(body).toMatchObject({user_id: 12, phone: '123', position: 'Директор', state: 'Действующий', expected_version: 2});
    expect(body).toHaveProperty('middle_name', null); expect(body).not.toHaveProperty('parent_id');
  });
  it('selectors load every page before returning a complete dictionary', async () => {
    vi.mocked(api).mockResolvedValueOnce(Array.from({length: 100}, (_, id) => ({id}))).mockResolvedValueOnce([{id: 100}]);
    expect(await all('/arm2/types')).toHaveLength(101);
    expect(api).toHaveBeenLastCalledWith('/arm2/types?limit=100&offset=100', 'GET', undefined, undefined);
  });
  it.each([401, 403, 404, 409, 422, 500])('gives safe actionable messages for HTTP %s', status => {
    const text = errorText(new ApiError(status, 'database secret'));
    expect(text.length).toBeGreaterThan(20);
    if (status !== 422) expect(text).not.toContain('database secret');
  });
});
