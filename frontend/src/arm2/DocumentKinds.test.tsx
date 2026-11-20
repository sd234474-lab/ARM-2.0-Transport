// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import Workspace from './Workspace';
import type { Can } from './Workspace';
import type { DocumentKind } from './api';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const base = '/arm2/document-kinds';
const canEdit: Can = p => ['arm2_doc_kind_view', 'arm2_doc_kind_edit'].includes(p);
let host: HTMLDivElement, root: ReturnType<typeof createRoot>, records: DocumentKind[], failures: ApiError[], eventId: number;
let events: {event_id: number; object_id: string; object_type: 'DOC-KIND'; operation: string; occurred_at: string; actor_kind: string; actor_id: string; source: string; version_before: number | null; version_after: number; changes: Record<string, {before: unknown; after: unknown}>}[];
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  records = Array.from({length: 105}, (_, i) => ({id: `DOC-KIND-${String(i + 1).padStart(6, '0')}`, name: i === 104 ? 'Тарировка за сотой' : `Вид ${i + 1}`, applicable_object_types: ['CP', 'ORG'], max_current_documents: null, version: 1}));
  failures = []; events = []; eventId = 0;
  vi.mocked(api).mockReset();
  vi.mocked(api).mockImplementation(async (path, method = 'GET', body) => {
    const url = new URL(path, 'http://test');
    if (method === 'POST' || method === 'PUT') {
      if (failures.length) throw failures.shift()!;
      const input = body as {name: string; applicable_object_types: DocumentKind['applicable_object_types']; max_current_documents: number | null; expected_version?: number; file_naming_template: DocumentKind['file_naming_template']};
      const current = method === 'PUT' ? records.find(r => r.id === path.split('/').at(-1)) : undefined;
      if (current && input.expected_version !== current.version) throw new ApiError(409, 'ARM2 object version conflict');
      const next: DocumentKind = {id: current?.id || `DOC-KIND-${String(records.length + 1).padStart(6, '0')}`, name: input.name,
        applicable_object_types: ['CP', 'ORG', 'VEH'].filter(p => input.applicable_object_types.includes(p as 'CP')) as DocumentKind['applicable_object_types'], max_current_documents: input.max_current_documents, version: current?.version || 1, file_naming_template: input.file_naming_template};
      const fields = ['name', 'applicable_object_types', 'max_current_documents', 'file_naming_template'] as const;
      const changed = fields.filter(field => JSON.stringify(current?.[field]) !== JSON.stringify(next[field]));
      if (!current || changed.length) {
        if (current) next.version++;
        const changes = Object.fromEntries((current ? [...changed, 'version' as const] : [...fields, 'version' as const]).map(field => [field, {before: current?.[field] ?? null, after: next[field]}]));
        events.unshift({event_id: ++eventId, object_id: next.id, object_type: 'DOC-KIND', operation: current ? 'UPDATED' : 'CREATED', occurred_at: '2026-11-15T10:00:00Z', actor_kind: 'HUMAN', actor_id: '7', source: 'arm2-api', version_before: current?.version ?? null, version_after: next.version, changes});
        records = current ? records.map(r => r.id === next.id ? next : r) : [...records, next];
      }
      return clone(next);
    }
    if (url.pathname === base + '/search') {
      const matches = records.filter(r => r.name.toLocaleLowerCase('ru').includes((url.searchParams.get('q') || '').toLocaleLowerCase('ru')));
      const offset = Number(url.searchParams.get('offset'));
      return clone({items: matches.slice(offset, offset + 100), total: records.length, matched: matches.length, limit: 100, offset});
    }
    if (path.startsWith('/arm2/history/events/')) return clone(events.find(e => e.event_id === Number(path.split('/').at(-1))));
    if (path.startsWith('/arm2/history/DOC-KIND/')) return clone(events.filter(e => e.object_id === url.pathname.split('/').at(-1)));
    const found = records.find(r => r.id === url.pathname.split('/').at(-1));
    if (!found) throw new ApiError(404, 'not found');
    return clone(found);
  });
});
afterEach(async () => {await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks();});
async function mount(path = base, can = canEdit, browser = false) {
  if (browser) window.history.replaceState({idx: 0}, '', path);
  const content = <Routes><Route path="/arm2/*" element={<Workspace can={can}/>}/></Routes>;
  await act(async () => {root.render(browser ? <BrowserRouter>{content}</BrowserRouter> : <MemoryRouter initialEntries={[path]}>{content}<Moves/></MemoryRouter>); await tick();});
}
function Moves() {const navigate = useNavigate(); return <><button onClick={() => navigate(-1)}>Browser back</button><button onClick={() => navigate(1)}>Browser forward</button></>;}
function button(name: string) {const b = [...host.querySelectorAll<HTMLButtonElement>('button')].find(e => e.textContent === name); if (!b) throw Error('Missing button '+name); return b;}
async function click(name: string) {await act(async () => {button(name).click(); await tick();});}
async function follow(name: string) {await act(async () => {const a = [...host.querySelectorAll<HTMLAnchorElement>('a')].find(e => e.textContent === name); if (!a) throw Error('Missing link '+name); a.click(); await tick();});}
function control(label: string) {const labelNode = [...host.querySelectorAll('label')].find(e => e.textContent?.startsWith(label)); const c = labelNode?.querySelector<HTMLInputElement | HTMLSelectElement>('input,select'); if (!c) throw Error('Missing '+label); return c;}
async function change(label: string, value: string) {await act(async () => {const c = control(label); Object.getOwnPropertyDescriptor(c instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(c,value); c.dispatchEvent(new Event(c instanceof HTMLSelectElement ? 'change' : 'input',{bubbles:true})); await tick();});}
async function check(label: string) {await act(async () => {control(label).click(); await tick();});}
const writes = () => vi.mocked(api).mock.calls.filter(([,method]) => method === 'POST' || method === 'PUT');

it('searches beyond 100, creates/updates/reopens persisted fields and reads real-shaped history', async () => {
  await mount(); expect(host.textContent).toContain('Найдено: 105 / Всего: 105');
  await click('Следующая страница'); expect(host.textContent).toContain('Тарировка за сотой');
  await change('Поиск по имени', 'тарировка'); expect(host.textContent).toContain('Найдено: 1 / Всего: 105');
  expect(vi.mocked(api).mock.calls.at(-1)![0]).toContain('offset=0');
  await follow('Тарировка за сотой'); expect(control('Имя типа').value).toBe('Тарировка за сотой');
  await follow('← К типам документов'); expect(control('Поиск').value).toBe('тарировка');
  await follow('+ Новая запись'); expect((control('Контрагенты') as HTMLInputElement).checked).toBe(false);
  expect(control('Максимум').value).toBe(''); expect(host.textContent).not.toContain('История');
  await change('Имя типа', '  Новый вид  '); await check('Собственные организации'); await check('Контрагенты'); await change('Максимум', 'unlimited');
  await click('Сохранить');
  expect(writes()[0][2]).toEqual({name:'Новый вид', applicable_object_types:['CP','ORG'], max_current_documents:null, file_naming_template:'DOCUMENT_NAME'});
  expect(host.textContent).toContain('DOC-KIND-000106'); expect(host.textContent).toContain('Версия: 1');
  await click('Browser back'); await click('Browser forward'); expect(control('Имя типа').value).toBe('Новый вид');
  await check('Контрагенты'); await check('Собственные организации'); await check('Техника'); await change('Максимум','limited'); await change('Положительное','1');
  await click('Сохранить'); expect(writes().at(-1)![2]).toEqual({name:'Новый вид', applicable_object_types:['VEH'], max_current_documents:1, expected_version:1, file_naming_template:'DOCUMENT_NAME'});
  expect(host.textContent).toContain('Сохранено сервером — версия 2');
  await click('Browser back'); await click('Browser forward'); expect((control('Техника') as HTMLInputElement).checked).toBe(true); expect(control('Положительное').value).toBe('1');
  await click('История'); await click('Открыть событие UPDATED'); expect(host.textContent).toContain('Применимость'); expect(host.textContent).toContain('["CP","ORG"]'); expect(host.textContent).toContain('["VEH"]'); expect(host.textContent).toContain('HUMAN 7'); expect(host.textContent).toContain('arm2-api');
  expect(records.at(-1)!.version).toBe(2); expect(events.map(e=>e.operation)).toEqual(['UPDATED','CREATED']);
  await click('Сохранить'); expect(host.textContent).toContain('Изменений нет — версия 2'); expect(events).toHaveLength(2);
});

it.each([{grants: []}, {grants: ['arm2_doc_kind_edit']}])('denies direct URLs without VIEW and makes no DOC requests (%j)', async ({grants}) => {
  await mount(base+'/new', p=>grants.includes(p)); expect(api).not.toHaveBeenCalled(); expect(host.querySelector('form')).toBeNull(); expect(host.textContent).not.toContain('Типы документов');
});
it('VIEW-only exposes card/history but no creation or writes', async () => {
  await mount(base, p=>p==='arm2_doc_kind_view'); expect(host.textContent).not.toContain('+ Новая запись');
  await follow('Вид 1'); expect(control('Имя типа').matches(':disabled')).toBe(true);
  expect([...host.querySelectorAll('button')].some(b=>b.textContent==='Сохранить')).toBe(false);
  await click('История'); expect(vi.mocked(api).mock.calls.some(([p])=>p.startsWith('/arm2/history/DOC-KIND/'))).toBe(true); expect(writes()).toHaveLength(0);
});

it('validates draft and protects cancel without posting; does not invent defaults', async () => {
  await mount(base+'/new'); await click('Сохранить'); expect(host.textContent).toContain('Укажите имя');
  await change('Имя типа','Черновик'); await click('Сохранить'); expect(host.textContent).toContain('Выберите хотя бы один');
  await check('Контрагенты'); await click('Сохранить'); expect(host.textContent).toContain('Выберите «Без ограничения»');
  await change('Максимум','limited');
  for (const value of ['0','-1','1.5','abc']) {await change('Положительное',value); await click('Сохранить'); expect(writes()).toHaveLength(0);}
  const confirm = vi.spyOn(window,'confirm').mockReturnValue(false);
  await follow('Отмена / к типам документов'); expect(control('Имя типа').value).toBe('Черновик'); expect(confirm).toHaveBeenCalled();
  confirm.mockReturnValue(true); await follow('Отмена / к типам документов'); expect(host.querySelector('form')).toBeNull(); expect(writes()).toHaveLength(0);
});

it('keeps 409 draft, comparison, cancelled reload, confirmed reload and successful retry distinct', async () => {
  await mount(base+'/'+records[0].id); await change('Имя типа','Мой черновик');
  records[0] = {...records[0], name:'Изменено сервером', version:2};
  await click('Сохранить'); expect(control('Имя типа').value).toBe('Мой черновик');
  await click('Сравнить с сервером'); expect(host.textContent).toContain('Изменено сервером'); expect(control('Имя типа').value).toBe('Мой черновик');
  const confirm = vi.spyOn(window,'confirm').mockReturnValue(false); const calls = vi.mocked(api).mock.calls.length;
  await click('Перезагрузить карточку'); expect(control('Имя типа').value).toBe('Мой черновик'); expect(vi.mocked(api).mock.calls).toHaveLength(calls);
  confirm.mockReturnValue(true); await click('Перезагрузить карточку'); expect(control('Имя типа').value).toBe('Изменено сервером'); expect(host.querySelector('[aria-label="Версия на сервере"]')).toBeNull();
  await change('Имя типа','Повторно сохранено'); await click('Сохранить'); expect(records[0].version).toBe(3); expect((writes().at(-1)![2] as {expected_version:number}).expected_version).toBe(2);
});

it('preserves 422/duplicate/5xx drafts, retries explicitly, and latches a 403 read-only', async () => {
  await mount(base+'/'+records[0].id); await change('Имя типа','Не терять');
  for(const error of [new ApiError(422,'invalid'), new ApiError(409,'ARM2 object already exists'), new ApiError(500,'failure')]) {
    failures.push(error); await click('Сохранить'); expect(control('Имя типа').value).toBe('Не терять'); expect(host.textContent).not.toContain('Сохранено сервером');
  }
  const before=writes().length; await act(async()=>{await tick();}); expect(writes()).toHaveLength(before);
  await click('Повторить сохранение'); expect(records[0].name).toBe('Не терять'); expect(records[0].version).toBe(2);
  await change('Имя типа','Запрет'); failures.push(new ApiError(403,'forbidden')); await click('Сохранить');
  expect(host.textContent).toContain('Только просмотр'); expect(control('Имя типа').matches(':disabled')).toBe(true);
  const last=writes().length; await act(async()=>{host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})); await tick();}); expect(writes()).toHaveLength(last);
});

it('handles list errors/retry and empty results', async () => {
  vi.mocked(api).mockRejectedValueOnce(new ApiError(500,'unavailable')); await mount(); expect(host.textContent).toContain('Ошибка сервера');
  await click('Повторить'); expect(host.textContent).toContain('Найдено: 105');
  await change('Поиск','нет такого имени'); expect(host.textContent).toContain('типов документов нет'); expect(button('Следующая страница').disabled).toBe(true);
});

it('guards native Browser Back/Forward without losing a refused draft', async () => {
  await mount(base,canEdit,true); await follow('Вид 1'); await change('Имя типа','Остаться');
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
  await act(async()=>{window.history.back(); await new Promise(resolve=>setTimeout(resolve,35));});
  expect(control('Имя типа').value).toBe('Остаться'); expect(window.location.pathname).toContain('DOC-KIND-000001');
  confirm.mockReturnValue(true); await act(async()=>{window.history.back(); await new Promise(resolve=>setTimeout(resolve,35));});
  expect(window.location.pathname).toBe(base);
  await act(async()=>{window.history.forward(); await new Promise(resolve=>setTimeout(resolve,35));});
  expect(control('Имя типа').value).toBe('Вид 1'); expect(writes()).toHaveLength(0);
});

it('arms the draft guard in the input event before the next React render', async () => {
  await mount(base+'/'+records[0].id);
  const confirm = vi.spyOn(window,'confirm').mockReturnValue(false);
  await act(async()=>{
    const input=control('Имя типа');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,'Быстрый черновик');
    input.dispatchEvent(new Event('input',{bubbles:true}));
    host.querySelector<HTMLAnchorElement>('article > a')!.click();
    await tick();
  });
  expect(confirm).toHaveBeenCalled(); expect(control('Имя типа').value).toBe('Быстрый черновик'); expect(writes()).toHaveLength(0);
});
