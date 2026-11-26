// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import BulkVehicles, { VehicleExport } from './BulkVehicles';
import type { BulkWorkspace } from './BulkVehicles';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
let element: HTMLDivElement, root: ReturnType<typeof createRoot>, stored: BulkWorkspace | undefined;
const sid = '11111111-1111-4111-8111-111111111111';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
function fixture(): BulkWorkspace {
  const option = {value: 'А123ВС86', label: 'А123ВС86', current: null, current_label: 'Пусто', original: null, has_original: false, error: null, conflict: false, clear: false, sources: [2, 3]};
  return {id: sid, source: 'paste.tsv', revision: 1, headers: ['Номер', 'Имя', 'Тип'], rows: [['А123ВС86', 'Имя А', 'Ошибочный TYPE'], ['А123ВС86', 'Имя Б', '']], mapping: ['plate_number', 'name', 'type_id'], excluded: [], clears: {}, decisions: {}, confirmations: {}, warning: 'Сторонняя таблица: исходного снимка нет', preview_id: '22222222-2222-4222-8222-222222222222', results: [], blocked: false, approval_reset: false,
    plans: [{key: 'А123ВС86', sources: [2, 3], action: 'CONFLICT', errors: [], fields: {plate_number: [option], name: [{...option, value: 'Имя А', label: 'Имя А', conflict: true, sources: [2]}, {...option, value: 'Имя Б', label: 'Имя Б', conflict: true, sources: [3]}], type_id: [{...option, value: null, label: 'Ошибочный TYPE', error: 'Тип не найден', sources: [2]}]}}]};
}
function install() {
  vi.mocked(api).mockImplementation(async (path, method = 'GET', body) => {
    if (path === '/arm2/bulk/sessions' && method === 'GET') return stored ? [{id: sid, source: stored.source, revision: stored.revision, created_at: '2026-11-17T08:00:00Z'}] : [];
    if (path === '/arm2/bulk/protocols') return stored?.results.length ? [{id: sid, source: stored.source, author: 7, results: clone(stored.results)}] : [];
    if (path.startsWith('/arm2/bulk/sessions?')) {stored = fixture(); return clone(stored);}
    if (path.endsWith('/finish')) {stored = undefined; return {message: 'Временный набор удалён; принятые VEH сохранены'};}
    if (path.endsWith('/apply')) {
      const command = body as {command_id: string; fields: Record<string, number>};
      stored!.results.push({command_id: command.command_id, row_key: 'А123ВС86', vehicle_id: 'VEH-000001', event_id: 88, version_after: 1, status: 'SAVED', fields: Object.keys(command.fields), at: '2026-11-17T08:00:00Z'});
      stored!.plans = [{key: 'VEH-000001', vehicle_id: 'VEH-000001', sources: [2, 3], action: 'SKIP', errors: [], fields: {}}];
      return clone(stored!.results[0]);
    }
    if (path === `/arm2/bulk/sessions/${sid}`) {
      if (method === 'PUT') {const update = body as Partial<BulkWorkspace>; expect(update.revision).toBe(stored!.revision); stored = {...stored!, ...clone(update), revision: stored!.revision + 1};}
      return clone(stored!);
    }
    throw Error('Unexpected API path ' + path);
  });
}
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  stored = undefined; vi.mocked(api).mockReset(); install();
  element = document.createElement('div'); document.body.append(element); root = createRoot(element);
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2]))));
  URL.createObjectURL = vi.fn().mockReturnValue('blob:test'); URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});
afterEach(async () => {await act(async () => root.unmount()); element.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();});
async function mount(resume = false) {await act(async () => {root.render(<MemoryRouter initialEntries={['/arm2/veh/bulk' + (resume ? '?session=' + sid : '')]}><BulkVehicles/></MemoryRouter>); await tick();});}
function button(text: string) {const b = [...element.querySelectorAll<HTMLButtonElement>('button')].find(e => e.textContent === text); if (!b) throw Error('No button ' + text); return b;}
async function click(text: string) {await act(async () => {button(text).click(); await tick(); await tick();});}
async function change(input: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  await act(async () => {const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(input, value); input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', {bubbles: true})); await tick(); await tick();});
}
async function paste() {await change(element.querySelector('textarea')!, 'Номер\tИмя\nА123ВС86\tИмя'); await click('Открыть вставленную таблицу');}

it('reopens an unchanged import outcome with a link to the authorised deletion event', async () => {
  const receipt = {command_id: 'command', vehicle_id: 'VEH-000013', event_id: 2, version_after: 1,
    status: 'SAVED', fields: [], at: '2026-11-24T04:00:00Z', current_object: {state: 'DELETED', event_id: 3}};
  vi.mocked(api).mockImplementation(async path => {
    if (path === `/arm2/bulk/protocols?session_id=${sid}`) return [{id: sid, source: 'Техника.xlsx', results: [receipt]}];
    if (path === '/arm2/history/events/3') return {changes: {version: {before: 1, after: null}}};
    throw Error(path);
  });
  await act(async () => {
    root.render(<MemoryRouter initialEntries={[`/arm2/veh/bulk?protocol=${sid}&receipt=command`]}>
      <BulkVehicles/>
    </MemoryRouter>);
    await tick();
  });
  expect(element.textContent).toContain('Объект удалён');
  expect(element.textContent).toContain('Сохранено VEH: 1');
  expect(element.querySelector('a[href="/arm2/veh/VEH-000013"]')).toBeNull();
  await click('История удаления');
  expect(vi.mocked(api)).toHaveBeenCalledWith('/arm2/history/events/3', 'GET', undefined, expect.any(AbortSignal));
});

it('loads an editable source without SAVE; accepts valid fields and a chosen duplicate alternative only after confirmation', async () => {
  await mount(); await paste();
  expect(element.textContent).toContain('Тип не найден'); expect(element.textContent).toContain('строки 2, 3');
  expect(vi.mocked(api).mock.calls.some(([p]) => p.endsWith('/apply'))).toBe(false);
  await click('Принять проверенные поля всего набора');
  expect(stored!.decisions['А123ВС86']).toEqual({plate_number: 0});
  const decision = [...element.querySelectorAll('label')].find(e => e.textContent?.startsWith('Решение: Рабочее имя'))!.querySelector('select')!;
  await change(decision, '1');
  await click('Сохранить выбранные изменения');
  const request = vi.mocked(api).mock.calls.find(([p]) => p.endsWith('/apply'))!;
  expect(request[2]).toMatchObject({row_key: 'А123ВС86', fields: {plate_number: 0, name: 1}, confirm_owner: false, confirm_plate: false});
  expect(element.textContent).toContain('Сохранено VEH: 1'); expect(element.textContent).toContain('HIST 88');
});

it('serializes cell edits, manual mapping, exclusions and explicit CLEAR into the durable draft', async () => {
  await mount(); await paste();
  await change(element.querySelector<HTMLInputElement>('[aria-label="Строка 2, столбец 2"]')!, 'Исправлено');
  await change(element.querySelector<HTMLSelectElement>('[aria-label="Укажите реквизит: 3"]')!, '');
  await act(async () => {element.querySelector<HTMLInputElement>('[aria-label="Выделить строку 2"]')!.click(); await tick();});
  await click('CLEAR выделенным строкам');
  expect(stored!.rows[0][1]).toBe('Исправлено'); expect(stored!.mapping[2]).toBe(''); expect(stored!.clears).toEqual({'0': ['type_id']});
  await click('Исключить выделенные строки'); expect(stored!.excluded).toEqual([0]);
  await click('Вернуть исключённые строки'); expect(stored!.excluded).toEqual([]);
  const revisions = vi.mocked(api).mock.calls.filter(([, m]) => m === 'PUT').map(([, , body]) => (body as {revision: number}).revision);
  expect(revisions).toEqual(revisions.map((_, i) => i + 1));
  await click('Проверить таблицу и фактические результаты');
  expect(element.querySelector<HTMLInputElement>('[aria-label="Строка 2, столбец 2"]')!.value).toBe('Исправлено');
  expect(element.textContent).toContain('Черновик сохранён на сервере');
});

it('preserves local edits on failed autosave and lets an operator cancel discarding them', async () => {
  await mount(); await paste();
  const implementation = vi.mocked(api).getMockImplementation()!;
  vi.mocked(api).mockImplementation(async (...args) => {if (args[1] === 'PUT') throw new ApiError(500, 'failure'); return implementation(...args);});
  await change(element.querySelector<HTMLInputElement>('[aria-label="Строка 2, столбец 2"]')!, 'Локальная правка');
  expect(element.textContent).toContain('Есть несохранённые локальные правки'); expect(button('Сохранить выбранные изменения').disabled).toBe(true);
  vi.mocked(window.confirm).mockReturnValue(false); await click('Проверить таблицу и фактические результаты');
  expect(element.querySelector<HTMLInputElement>('[aria-label="Строка 2, столбец 2"]')!.value).toBe('Локальная правка');
  expect(stored!.rows[0][1]).toBe('Имя А');
});

it('recovers a lost SAVE response from server outcomes without retransmitting subject changes', async () => {
  await mount(); await paste(); await click('Принять проверенные поля всего набора');
  const implementation = vi.mocked(api).getMockImplementation()!;
  vi.mocked(api).mockImplementation(async (...args) => {const result = await implementation(...args); if (args[0].endsWith('/apply')) throw new TypeError('lost connection'); return result;});
  await click('Сохранить выбранные изменения');
  expect(vi.mocked(api).mock.calls.filter(([p]) => p.endsWith('/apply'))).toHaveLength(1);
  expect(element.textContent).toContain('Сохранено VEH: 1'); expect(stored!.results).toHaveLength(1);
  await click('Проверить таблицу и фактические результаты');
  expect(element.textContent).toContain('HIST 88'); expect(stored!.results).toHaveLength(1);
});

it('reopens a private server draft and blocks UNKNOWN outcomes including finish', async () => {
  stored = fixture(); stored.blocked = true;
  stored.results = [{command_id: 'x', row_key: 'А123ВС86', vehicle_id: 'VEH-000001', event_id: 88, version_after: 1, status: 'UNKNOWN', fields: ['name'], at: '2026-11-17T08:00:00Z'}];
  await mount(true);
  expect(element.textContent).toContain('Неопределённо: 1'); expect(element.textContent).toContain('Сохранено VEH: 0');
  expect(button('Сохранить выбранные изменения').disabled).toBe(true); expect(button('Скачать отказы в Excel').disabled).toBe(true); expect(button('Завершить и удалить временный набор').disabled).toBe(true);
  expect(element.textContent).not.toContain('Административная очистка');
});

it('downloads the rejection file before deleting the workspace and does not imply rollback after partial SAVE', async () => {
  stored = fixture(); stored.results = [{command_id: 'x', row_key: 'А123ВС86', vehicle_id: 'VEH-000001', event_id: 88, version_after: 1, status: 'SAVED', fields: ['name'], at: '2026-11-17T08:00:00Z'}];
  await mount(true);
  expect(button('Отменить загрузку').disabled).toBe(true);
  await click('Скачать отказы в Excel'); expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/report'), expect.anything());
  expect(button('Отменить загрузку').disabled).toBe(false);
  await click('Отменить загрузку'); expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Уже сохранённые VEH'));
  expect(vi.mocked(api).mock.calls.find(([p]) => p.endsWith('/finish'))?.[2]).toMatchObject({cancel: true});
  expect(element.textContent).toContain('принятые VEH сохранены');
});

it('asks which XLSX sheet to load and then submits precisely that sheet', async () => {
  await mount();
  const implementation = vi.mocked(api).getMockImplementation()!;
  vi.mocked(api).mockImplementation(async (...args) => args[0].includes('/sessions?') && !args[0].includes('sheet=') ? {sheets: ['Первый', 'Техника']} : implementation(...args));
  const file = new File(['synthetic'], 'table.xlsx');
  await act(async () => {const input = element.querySelector<HTMLInputElement>('input[type=file]')!; Object.defineProperty(input, 'files', {value: [file]}); input.dispatchEvent(new Event('change', {bubbles: true})); await tick();});
  const select = [...element.querySelectorAll('label')].find(l => l.textContent?.startsWith('Лист'))!.querySelector('select')!;
  await change(select, 'Техника'); await click('Открыть выбранный лист');
  const requests = vi.mocked(api).mock.calls.filter(([p]) => p.includes('/sessions?'));
  expect(new URLSearchParams(requests.at(-1)![0].split('?')[1]).get('sheet')).toBe('Техника');
  expect(requests.at(-1)![2]).toBe(file);
});

it('exports the complete current filter without the displayed page offset or limit', async () => {
  await act(async () => root.render(<VehicleExport query="q=самосвал&state=Архивная&offset=100&limit=100"/>));
  await click('Выгрузить в Excel');
  const url = String(vi.mocked(fetch).mock.calls[0][0]), params = new URLSearchParams(url.split('?')[1]);
  expect(params.get('q')).toBe('самосвал'); expect(params.get('state')).toBe('Архивная'); expect(params.has('offset')).toBe(false); expect(params.has('limit')).toBe(false);
});
