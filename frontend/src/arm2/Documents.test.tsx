// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import Workspace from './Workspace';
import type { Can } from './Workspace';
import type { DocumentRecord } from './api';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const base = '/arm2/cp/CP-000001/documents';
const canEdit: Can = p => ['arm2_cp_view','arm2_cp_edit','arm2_document_view','arm2_document_edit'].includes(p);
let host: HTMLDivElement, root: ReturnType<typeof createRoot>, records: DocumentRecord[], failures: ApiError[];
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  records = []; failures = [];
  vi.mocked(api).mockReset();
  vi.mocked(api).mockImplementation(async (path, method = 'GET', body) => {
    const url = new URL(path, 'http://test');
    if (method === 'POST' || method === 'PUT') {
      if (failures.length) throw failures.shift()!;
      const input = body as {name: string; description: string | null; kind_ref?: string; expected_version?: number};
      const current = method === 'PUT' ? records.find(row => row.id === url.pathname.split('/').at(-1)) : undefined;
      if (current && current.version !== input.expected_version) throw new ApiError(409, 'ARM2 object version conflict');
      const changed = !current || current.name !== input.name || current.description !== input.description;
      const row: DocumentRecord = {id: current?.id || 'DOCUMENT-000001', parent_type: 'CP', parent_id: 'CP-000001', kind_ref: current?.kind_ref || input.kind_ref!, kind_name: 'Существующий тип', name: input.name, description: input.description, archive_state: 'Неархивный', display_state: 'Не загружен', version: current ? current.version + Number(changed) : 1};
      records = current ? records.map(item => item.id === current.id ? row : item) : [...records, row];
      return clone(row);
    }
    if (url.pathname === '/arm2/counterparties/CP-000001') return {id:'CP-000001', name:'Тестовый родитель', version:1, status:'REVIEW'};
    if (url.pathname === base + '/kinds') {
      const kinds = Array.from({length:105}, (_,i)=>({id:`DOC-KIND-${String(i+1).padStart(6,'0')}`,name:i===104?'За сотой':'Тип '+(i+1),applicable_object_types:['CP'],max_current_documents:null,version:1}));
      const matches = kinds.filter(row=>row.name.includes(url.searchParams.get('q') || ''));
      const offset = Number(url.searchParams.get('offset')); return matches.slice(offset,offset+100);
    }
    if (url.pathname === base) return url.searchParams.get('archive') === 'archived' ? [] : clone(records.slice(Number(url.searchParams.get('offset')),Number(url.searchParams.get('offset'))+100));
    if (url.pathname.startsWith('/arm2/history/DOCUMENT/')) return [{event_id:1,object_type:'DOCUMENT',object_id:'DOCUMENT-000001',operation:'UPDATED',occurred_at:'2026-11-15T10:00:00Z',actor_kind:'HUMAN',actor_id:'7',source:'arm2-api',version_before:1,version_after:2}];
    if (url.pathname === '/arm2/history/events/1') return {changes:{name:{before:'Первый',after:'Второй'},version:{before:1,after:2}}};
    const found = records.find(row=>row.id===url.pathname.split('/').at(-1));
    if (found) return clone(found);
    throw new ApiError(404,'not found');
  });
});
afterEach(async () => {await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks();});
function Moves() {const navigate=useNavigate(); return <button onClick={()=>navigate(-1)}>Browser back</button>;}
async function mount(path = '/arm2/cp/CP-000001?tab=documents', can = canEdit) {
  await act(async () => {root.render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/arm2/*" element={<Workspace can={can}/>}/></Routes><Moves/></MemoryRouter>); await tick();});
}
async function click(name:string) {await act(async()=>{const node=[...host.querySelectorAll<HTMLButtonElement>('button')].find(e=>e.textContent===name); if(!node)throw Error('Missing '+name);node.click();await tick();});}
async function follow(name:string) {await act(async()=>{const node=[...host.querySelectorAll<HTMLAnchorElement>('a')].find(e=>e.textContent===name);if(!node)throw Error('Missing '+name);node.click();await tick();});}
function control(label:string) {const node=[...host.querySelectorAll('label')].find(e=>e.textContent?.startsWith(label))?.querySelector<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement>('input,select,textarea');if(!node)throw Error('Missing '+label);return node;}
async function change(label:string,value:string) {await act(async()=>{const node=control(label); const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:node instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value')!.set!.call(node,value);node.dispatchEvent(new Event(node instanceof HTMLSelectElement?'change':'input',{bubbles:true}));await tick();});}
const writes=()=>vi.mocked(api).mock.calls.filter(([,method])=>method==='POST'||method==='PUT');
async function create(description?:string) {await mount(base+'/new');await change('Тип документа','DOC-KIND-000001');await change('Наименование','  Первый  ');if(description!==undefined)await change('Описание',description);await click('Сохранить');}

it('persists a zero-file row, preserves description text, reopens, edits, no-ops and reads history',async()=>{
  await create('  Строка\nвторая  ');
  expect(writes()[0][2]).toEqual({name:'Первый',description:'  Строка\nвторая  ',kind_ref:'DOC-KIND-000001'});
  expect(host.textContent).toContain('DOCUMENT-000001');expect(host.textContent).toContain('Версия: 1');
  expect(host.querySelector('[type=file]')).toBeNull();expect(host.textContent).not.toContain('Действителен');
  await follow('← К документам родителя');
  const table=host.querySelector('section[aria-label="Документы родителя"] table')!;
  expect([...table.querySelectorAll('th')].map(e=>e.textContent)).toEqual(['Тип документа','Наименование документа','Состояние документа']);
  expect(table.textContent).toContain('Не загружен');expect(table.querySelectorAll('tbody tr')).toHaveLength(1);
  await change('Архивность','archived');expect(host.textContent).toContain('Документов по заданным условиям нет');await change('Архивность','all');await follow('Первый');
  await change('Наименование','Второй');await change('Описание','');await click('Сохранить');
  expect(writes().at(-1)![2]).toEqual({name:'Второй',description:'',expected_version:1});expect(records[0].version).toBe(2);
  await click('Сохранить');expect(records[0].version).toBe(2);expect(host.textContent).toContain('Изменений нет — версия 2');
  await click('История');await click('Открыть событие UPDATED');expect(host.textContent).toContain('Первый');expect(host.textContent).toContain('HUMAN 7');expect(host.textContent).toContain('arm2-api');
});
it('never silently changes nullable description on no-op and searches applicable types past page 100',async()=>{
  await mount(base+'/new');expect(control('Тип документа').value).toBe('');expect(host.textContent).not.toContain('Открыть справочник');
  await click('Следующая страница');expect(host.textContent).toContain('За сотой');await change('Тип документа','DOC-KIND-000105');
  await change('Поиск доступного','Тип 1');expect(control('Тип документа').value).toBe('DOC-KIND-000105');
  await change('Наименование','Первый');await click('Сохранить');await click('Сохранить');
  expect(writes()[0][2]).toEqual({name:'Первый',description:null,kind_ref:'DOC-KIND-000105'});expect(records[0].version).toBe(1);
});
it('keeps a stale draft on 409, compares and reloads only after confirmation',async()=>{
  await create();await change('Наименование','Черновик');records[0]={...records[0],name:'Другой оператор',version:2};await click('Сохранить');
  expect(control('Наименование').value).toBe('Черновик');await click('Сравнить с сервером');expect(host.textContent).toContain('Другой оператор');expect(control('Наименование').value).toBe('Черновик');
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await click('Перезагрузить карточку');expect(control('Наименование').value).toBe('Черновик');
  confirm.mockReturnValue(true);await click('Перезагрузить карточку');expect(control('Наименование').value).toBe('Другой оператор');await change('Описание','после');await click('Сохранить');expect(records[0].version).toBe(3);
});
it('protects dirty link/back navigation and retains 422/500 drafts before retry, latches 403 read-only',async()=>{
  await create();await change('Описание','Черновик');const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await follow('Отмена / к документам родителя');expect(control('Описание').value).toBe('Черновик');await click('Browser back');expect(control('Описание').value).toBe('Черновик');expect(confirm).toHaveBeenCalledTimes(2);
  failures.push(new ApiError(422,'Превышено допустимое количество документов'));await click('Сохранить');expect(control('Описание').value).toBe('Черновик');expect(records[0].description).toBeNull();
  failures.push(new ApiError(500,'failure'));await click('Сохранить');expect(control('Описание').value).toBe('Черновик');await click('Повторить сохранение');expect(records[0].description).toBe('Черновик');
  await change('Описание','Отозваны права');failures.push(new ApiError(403,'forbidden'));await click('Сохранить');expect(host.textContent).toContain('Только просмотр');expect(host.querySelector('[type=submit]')).toBeNull();expect(records[0].description).toBe('Черновик');
});
it.each([[],['arm2_document_view'],['arm2_cp_view'],['arm2_doc_kind_view','arm2_doc_kind_edit'],['arm2_cp_edit','arm2_document_edit']].map(grants=>({grants})))('denies document URLs without both typed VIEW grants: $grants',async({grants})=>{
  await mount(base+'/DOCUMENT-000001',p=>grants.includes(p));expect(vi.mocked(api).mock.calls.filter(([path])=>path.includes('/documents'))).toHaveLength(0);expect(host.querySelector('form')).toBeNull();
});
it('requires both typed EDIT grants and validates required fields before any write',async()=>{
  await mount(base+'/new');await click('Сохранить');expect(writes()).toHaveLength(0);await change('Наименование','Имя');await click('Сохранить');expect(writes()).toHaveLength(0);expect(host.textContent).toContain('Выберите существующий тип');
});
