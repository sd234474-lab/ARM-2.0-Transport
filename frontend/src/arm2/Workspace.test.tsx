// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import Workspace from './Workspace';
import type { Can } from './Workspace';
import type { RecordData } from './api';
import { emptyRecord } from './model';

vi.mock('../api', async importOriginal => ({...await importOriginal<typeof import('../api')>(), api: vi.fn()}));
let element: HTMLDivElement, root: ReturnType<typeof createRoot>;
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const type = {...emptyRecord('TYPE'), id: 'TYPE-1', name: 'Самосвал', version: 4, status: 'REVIEW'};
const party = {...emptyRecord('CP'), id: 'CP-1', name: 'Партнёр', version: 2, full_name: 'Полное имя', legal_address: 'Адрес', status: 'CONFIRMED', primary_signer_id: null};
const vehicle = {...emptyRecord('VEH'), id: 'VEH-1', name: 'Машина', plate_number: 'А001АА86', version: 3, type_id: 'TYPE-1', model: 'Модель', owner_cp_id: 'CP-1'};
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api).mockReset();
  element = document.createElement('div'); document.body.append(element); root = createRoot(element);
});
afterEach(async () => {await act(async () => root.unmount()); element.remove(); vi.restoreAllMocks();});
async function mount(path: string, can: Can = () => true) {await act(async () => {root.render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/arm2/*" element={<Workspace can={can}/>}/></Routes></MemoryRouter>); await tick();});}
function button(text: string) {const found = [...element.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent === text); if (!found) throw Error('No button: ' + text); return found;}
async function click(text: string) {await act(async () => {button(text).click(); await tick();});}
async function follow(text: string) {await act(async () => {const a = [...element.querySelectorAll<HTMLAnchorElement>('a')].find(item => item.textContent === text); if (!a) throw Error('No link: ' + text); a.click(); await tick();});}
function control(label: string) {const item = [...element.querySelectorAll('label')].find(item => item.textContent?.startsWith(label)); const input = item?.querySelector<HTMLInputElement | HTMLSelectElement>('input,select'); if (!input) throw Error('No field: ' + label); return input;}
async function change(label: string, value: string) {
  await act(async () => {const input = control(label); Object.getOwnPropertyDescriptor(input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', {bubbles: true})); await tick();});
}
async function submit() {await act(async () => {element.querySelector('form')!.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})); await tick();});}
function defaults(path: string) {
  if (path.includes('?')) return path.startsWith('/arm2/types?') ? [type] : path.startsWith('/arm2/counterparties?') ? [party] : path.startsWith('/arm2/organizations?') ? [{...party, id: 'ORG-1', name: 'Своя организация'}] : [];
  return path.includes('/vehicles/') ? vehicle : path.includes('/types/') ? type : party;
}
it('requests history only by button, renders event differences without related objects', async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith('/arm2/history/events') ? {changes: {requires_calibration: {before: null, after: false}}} : path.startsWith('/arm2/history/') ? [{event_id: 1, occurred_at: '2026-11-11T04:00:00Z', actor_kind: 'HUMAN', actor_id: '1', source: 'arm2-api', version_before: 3, version_after: 4, operation: 'UPDATE'}] : type);
  await mount('/arm2/type/TYPE-1');
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/history/'))).toBe(false);
  await click('История'); expect(api).toHaveBeenCalledWith('/arm2/history/TYPE/TYPE-1?limit=100&offset=0', 'GET', undefined, expect.any(AbortSignal));
  await click('Открыть событие UPDATE'); expect(element.textContent).toContain('Не заполнено'); expect(element.textContent).toContain('Нет');
});
it('creates REVIEW TYPE then completes requirements using false, reopens server version', async () => {
  let saved: RecordData = {...type};
  vi.mocked(api).mockImplementation(async (path, method, body) => {
    if (method === 'POST' || method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; saved.status = saved.requires_calibration != null && saved.requires_wialon != null ? 'CONFIRMED' : 'REVIEW'; return saved;}
    return path.includes('?') ? [saved] : saved;
  });
  await mount('/arm2/type/new'); await change('Рабочее имя', 'Самосвал'); await submit();
  expect(element.textContent).toContain('REVIEW');
  await click('Требования к технике этого типа');
  expect([...control('СТС').querySelectorAll('option')].map(option => option.textContent)).not.toContain('Не требуется');
  await change('Тарировка', 'false'); await change('Wialon', 'true'); await submit();
  expect(saved.requires_calibration).toBe(false); expect(saved.status).toBe('CONFIRMED');
  await follow('← К списку'); await follow('Самосвал'); expect(element.textContent).toContain('CONFIRMED');
  expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2]).toMatchObject({expected_version: 5, requires_calibration: false});
});
it('VEH keeps existing REVIEW TYPE, disables new REVIEW and switches CP to ORG then clears owner', async () => {
  let saved: RecordData = {...vehicle};
  vi.mocked(api).mockImplementation(async (path, method, body) => {
    if (method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; return saved;}
    if (path.startsWith('/arm2/types?')) return [type, {...type, id: 'TYPE-2', name: 'Незавершённый', requires_sts: null}];
    return path.includes('/vehicles/') ? saved : defaults(path);
  });
  await mount('/arm2/veh/VEH-1');
  const options = [...control('Тип техники').querySelectorAll('option')];
  expect(options.find(option => option.value === 'TYPE-1')?.disabled).toBe(false); expect(options.find(option => option.value === 'TYPE-2')?.disabled).toBe(true);
  await change('Вид владельца', 'ORG'); await change('Владелец — собственная организация', 'ORG-1'); await submit();
  expect(saved).toMatchObject({owner_cp_id: null, owner_org_id: 'ORG-1', model: 'Модель', type_id: 'TYPE-1'});
  await change('Рабочее имя', 'Новое имя'); await submit(); expect(saved.owner_org_id).toBe('ORG-1');
  await change('Вид владельца', ''); await submit(); expect(saved).toMatchObject({owner_cp_id: null, owner_org_id: null, version: 6});
  expect(element.textContent).not.toContain('Состояние по последнему ответу сервера');
});
it.each(['cp', 'org'])('%s preserves untouched requisites and string bank accounts, then reopens', async kind => {
  let saved: RecordData = {...party, id: kind === 'cp' ? 'CP-1' : 'ORG-1'};
  vi.mocked(api).mockImplementation(async (path, method, body) => {if (method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; return saved;} return path.includes('?') ? [saved] : saved;});
  await mount(`/arm2/${kind}/${saved.id}`); await click('Банковские счета');
  await change('БИК', '000000001'); await change('Расчётный счёт', '00000000000000000001'); await change('Корреспондентский счёт', '00000000000000000002'); await submit();
  const body = vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2];
  expect(body).toEqual({name: 'Партнёр', expected_version: 2, bik: '000000001', settlement_account: '00000000000000000001', correspondent_account: '00000000000000000002'});
  await follow('← К списку'); await follow('Партнёр'); await click('Реквизиты'); expect(control('Полное наименование').value).toBe('Полное имя');
  await click('Банковские счета'); expect(control('БИК').value).toBe('000000001');
  await click('Документы'); expect(element.textContent).toContain('Функция пока не реализована'); expect(element.textContent).not.toContain('Загрузить документ');
});
it.each(['cp', 'org'])('%s representative edit sends the complete record and retains user link', async kind => {
  const rep = {...emptyRecord('REP'), id: 'REP-1', name: 'Представитель', version: 9, phone: '123', user_id: 8, first_name: 'Иван'};
  vi.mocked(api).mockImplementation(async (path, method, body) => method === 'PUT' ? {...rep, ...body as RecordData, version: 10} : path.includes('/representatives/') ? rep : party);
  await mount(`/arm2/${kind}/PARENT-1/representatives/REP-1`); await change('Должность', 'Директор'); await submit();
  expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2]).toMatchObject({expected_version: 9, user_id: 8, phone: '123', first_name: 'Иван', position: 'Директор', middle_name: null});
});
it('VIEW-only can navigate tabs and representatives but cannot save or read VEH without permission', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('/representatives?') ? [{...emptyRecord('REP'), id: 'REP-1', name: 'Контакт'}] : party);
  await mount('/arm2/cp/CP-1', permission => permission === 'arm2_cp_view');
  expect(control('Рабочее имя').disabled).toBe(true); expect([...element.querySelectorAll('button')].some(item => item.textContent === 'Сохранить')).toBe(false);
  await click('Контакты'); expect(element.textContent).toContain('Контакт'); expect(control('Основной подписант').disabled).toBe(true);
  await click('Техника'); expect(element.textContent).toContain('Нет права просмотра техники');
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/vehicles'))).toBe(false);
});
it.each([409, 422, 500])('HTTP %s retains draft without reporting success or automatically overwriting', async status => {
  vi.mocked(api).mockImplementation(async (path, method) => {if (method === 'PUT') throw new ApiError(status, 'requires_sts must be True when it is specified'); return type;});
  await mount('/arm2/type/TYPE-1'); await change('Рабочее имя', 'Мой черновик'); await submit();
  expect(control('Рабочее имя').value).toBe('Мой черновик'); expect(element.querySelector('[role=alert]')).not.toBeNull(); expect(element.textContent).not.toContain('Сохранено сервером');
  expect(vi.mocked(api).mock.calls.filter(([, method]) => method === 'PUT')).toHaveLength(1);
  if (status === 409) {await click('Сравнить с сервером'); expect(control('Рабочее имя').value).toBe('Мой черновик'); expect(element.textContent).toContain('Версия на сервере');}
});
it('server 403 disables writes after a failed save', async () => {
  vi.mocked(api).mockImplementation(async (path, method) => {if (method === 'PUT') throw new ApiError(403, 'denied'); return type;});
  await mount('/arm2/type/TYPE-1'); await submit(); expect(control('Рабочее имя').disabled).toBe(true); expect(element.textContent).toContain('Только просмотр');
});
it('list pagination fetches next API page and search scope stays explicit', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('offset=100') ? [{...type, id: 'TYPE-101', name: 'На второй странице'}] : Array.from({length: 100}, (_, i) => ({...type, id: String(i)})));
  await mount('/arm2/type'); await click('Следующая страница'); expect(element.textContent).toContain('На второй странице'); expect(element.textContent).toContain('только к текущей странице');
  expect(api).toHaveBeenCalledWith('/arm2/types?limit=100&offset=100', 'GET', undefined, expect.any(AbortSignal));
});

it.each(['cp', 'org'])('%s create → CONFIRMED → create representative → select scoped primary signer', async kind => {
  const parentId = kind === 'cp' ? 'CP-NEW' : 'ORG-NEW';
  const path = kind === 'cp' ? '/arm2/counterparties' : '/arm2/organizations';
  let saved: RecordData = {...emptyRecord(kind === 'cp' ? 'CP' : 'ORG'), id: parentId, version: 1, status: 'REVIEW'};
  const reps: RecordData[] = [];
  vi.mocked(api).mockImplementation(async (url, method, body) => {
    if (url.includes('/representatives')) {
      if (method === 'POST') {const rep = {...emptyRecord('REP'), ...body as RecordData, id: 'REP-NEW', parent_id: parentId, version: 1}; reps.push(rep); return rep;}
      return url.includes('?') ? reps : reps[0];
    }
    if (method === 'POST' || method === 'PUT') {saved = {...saved, ...body as RecordData, version: method === 'POST' ? 1 : saved.version + 1}; saved.status = saved.full_name && saved.legal_address ? 'CONFIRMED' : 'REVIEW'; return saved;}
    return url.includes('?') ? [saved] : saved;
  });
  await mount(`/arm2/${kind}/new`); await change('Рабочее имя', 'Новый партнёр'); await submit(); expect(element.textContent).toContain('REVIEW');
  await change('Юридический адрес', 'Адрес'); await click('Реквизиты'); await change('Полное наименование', 'Полное имя'); await submit(); expect(saved.status).toBe('CONFIRMED');
  await click('Контакты'); await follow('Добавить представителя'); await change('Рабочее имя', 'Иван Петров'); await change('Телефон представителя', '123'); await submit();
  expect(reps[0]).toMatchObject({name: 'Иван Петров', phone: '123', state: 'Действующий', user_id: null});
  await follow('← К родительской карточке'); await click('Контакты');
  expect([...control('Основной подписант').querySelectorAll('option')].map(option => option.value)).toEqual(['', 'REP-NEW']);
  await change('Основной подписант', 'REP-NEW'); await submit();
  expect(api).toHaveBeenCalledWith(`${path}/${parentId}`, 'PUT', {name: 'Новый партнёр', expected_version: 2, primary_signer_id: 'REP-NEW'});
  expect(saved.primary_signer_id).toBe('REP-NEW');
});

it.each([401, 404])('HTTP %s on card GET shows failure and no editable form', async status => {
  vi.mocked(api).mockRejectedValue(new ApiError(status, 'not found'));
  await mount('/arm2/type/MISSING'); expect(element.querySelector('[role=alert]')).not.toBeNull(); expect(element.querySelector('form')).toBeNull();
});

it('history has real pagination and requests event detail only when selected', async () => {
  const event = {event_id: 1, occurred_at: '2026-11-11T04:00:00Z', actor_kind: 'HUMAN', actor_id: '1', source: 'arm2-api', version_before: 3, version_after: 4, operation: 'UPDATE'};
  vi.mocked(api).mockImplementation(async path => path.includes('/history/') ? path.includes('offset=100') ? [] : Array.from({length: 100}, (_, i) => ({...event, event_id: i + 1})) : type);
  await mount('/arm2/type/TYPE-1'); await click('История'); await click('Следующая страница');
  expect(api).toHaveBeenCalledWith('/arm2/history/TYPE/TYPE-1?limit=100&offset=100', 'GET', undefined, expect.any(AbortSignal));
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/history/events/'))).toBe(false);
});

it('late create response does not redirect the operator after leaving the card', async () => {
  let finish!: (record: RecordData) => void;
  const pending = new Promise<RecordData>(resolve => {finish = resolve;});
  vi.mocked(api).mockImplementation(async (path, method) => method === 'POST' ? pending : path.includes('?') ? [] : type);
  await mount('/arm2/type/new'); await change('Рабочее имя', 'Самосвал'); await submit();
  await follow('Контрагенты'); await act(async () => {finish(type); await tick();});
  expect(element.querySelector('h2')?.textContent).toBe('Контрагенты');
  expect(element.querySelector('form')).toBeNull();
});
