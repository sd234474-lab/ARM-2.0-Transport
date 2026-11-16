// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import Workspace from './Workspace';
import type { Can } from './Workspace';
import type { DocumentRecord, FileBundle, FileStage } from './api';
import { previewBytes } from './api';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
vi.mock('./api', async original => ({...await original<typeof import('./api')>(), previewBytes: vi.fn(async () => new Blob(['test-content'],{type:'image/png'}))}));
const pdfMocks = vi.hoisted(() => ({getDocument:vi.fn(), destroy:vi.fn(async () => {}), render:vi.fn(() => ({promise:Promise.resolve()})), getPage:vi.fn()}));
vi.mock('pdfjs-dist', () => ({GlobalWorkerOptions:{workerSrc:''},getDocument:pdfMocks.getDocument}));
const base = '/arm2/cp/CP-000001/documents';
const canEdit: Can = p => ['arm2_cp_view','arm2_cp_edit','arm2_document_view','arm2_document_edit'].includes(p);
let host: HTMLDivElement, root: ReturnType<typeof createRoot>, records: DocumentRecord[], failures: ApiError[];
let filePage: FileBundle, fileFailures: ApiError[], staged: FileStage;
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  records = []; failures = [];
  Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=> 'blob:http://localhost/qa-preview')});
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:vi.fn()});
  fileFailures=[];
  filePage={document_id:'DOCUMENT-000001',document_version:1,current_revision_id:null,display_state:'Не загружен',revisions:[],max_files:32,max_file_bytes:1048576};
  staged={id:'a'.repeat(64),original_upload_name:'candidate.png',size:12,sha256:null,media_type:null,extension:null,state:'empty'};
  vi.mocked(api).mockReset();
  vi.mocked(api).mockImplementation(async (path, method = 'GET', body) => {
    const url = new URL(path, 'http://test');
    if(url.pathname.endsWith('/files'))return clone(filePage);
    if(url.pathname.endsWith('/stages')&&method==='POST')return clone(staged);
    if(url.pathname.endsWith('/stages/'+staged.id))return clone(staged);
    if(url.pathname.endsWith('/content')){staged={...staged,state:'ready',media_type:'image/png',extension:'png',sha256:'b'.repeat(64)};return clone(staged);}
    if(url.pathname.endsWith('/accept')){
      if(fileFailures.length)throw fileFailures.shift()!;
      const input=body as {expected_version:number};if(input.expected_version!==records[0].version)throw new ApiError(409,'stale');
      records[0]={...records[0],version:records[0].version+1,display_state:'Доступен'};
      filePage={...filePage,current_revision_id:'DOCUMENT-REV-000001',document_version:records[0].version,display_state:'Доступен',revisions:[{id:'DOCUMENT-REV-000001',number:1,document_id:records[0].id,parent_type:'CP',parent_id:'CP-000001',author_id:7,occurred_at:'2026-11-16T10:00:00Z',source:'arm2-api-manual',files:[{id:'FILE-000001',position:0,original_upload_name:'candidate.png',stored_name:'Name-FILE-000001.png',size:12,sha256:'b'.repeat(64),media_type:'image/png',extension:'png'}]}]};
      return {document_id:records[0].id,document_version:records[0].version,current_revision_id:filePage.current_revision_id,no_op:false};
    }
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

async function pickCandidate(file=new File(['test-content'],'candidate.png',{type:'image/png'})) {
  await act(async()=>{const input=host.querySelector<HTMLInputElement>('input[type=file]')!;Object.defineProperty(input,'files',{configurable:true,value:[file]});input.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
}
it('selects locally, stages only on explicit preview, accepts once and refreshes DOCUMENT version without metadata PUT',async()=>{
  await create();await click('+ Добавить файл');await pickCandidate();
  expect(vi.mocked(api).mock.calls.filter(([path])=>path.includes('/stages'))).toHaveLength(0);
  await click('Открыть файл');const frame=host.querySelector('iframe')!;expect(frame.getAttribute('sandbox')).toBe('');expect(frame.src).toBe('blob:http://localhost/qa-preview');expect(previewBytes).toHaveBeenCalledWith(base+'/DOCUMENT-000001/stages/'+staged.id+'/preview',1048576);
  expect(vi.mocked(api).mock.calls.find(([path])=>path.endsWith('/content'))![2]).toBeInstanceOf(File);
  await click('Сохранить и принять');expect(host.textContent).toContain('Комплект принят — версия 2');expect(host.textContent).toContain('Текущая редакция 1');expect(host.textContent).toContain('Версия: 2');
  const command=vi.mocked(api).mock.calls.find(([path])=>path.endsWith('/accept'))!;
  expect(command[2]).toMatchObject({expected_version:1,files:[{stage_id:'a'.repeat(64)}]});
  expect(writes().filter(([path,method])=>method==='PUT'&&!path.endsWith('/content'))).toHaveLength(0);
});
it('retains candidates and same idempotency key after 503; cancelled navigation keeps the native File',async()=>{
  await create();await click('+ Добавить файл');await pickCandidate();const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await follow('Отмена / к документам родителя');expect(host.textContent).toContain('candidate.png');expect(confirm).toHaveBeenCalled();
  fileFailures.push(new ApiError(503,'injected'));await click('Сохранить и принять');expect(host.textContent).toContain('candidate.png');expect(records[0].version).toBe(1);
  const first=vi.mocked(api).mock.calls.find(([path])=>path.endsWith('/accept'))![2];await click('Сохранить и принять');
  const calls=vi.mocked(api).mock.calls.filter(([path])=>path.endsWith('/accept'));expect(calls[1][2]).toEqual(first);expect(records[0].version).toBe(2);
  expect(vi.mocked(api).mock.calls.filter(([path])=>path.endsWith('/content'))).toHaveLength(1);
});
it('keeps stale file draft on 409 and compares/reloads only on confirmation; 403 removes write actions',async()=>{
  await create();await click('+ Добавить файл');await pickCandidate();records[0]={...records[0],version:2};filePage={...filePage,document_version:2};
  await click('Сохранить и принять');expect(host.textContent).toContain('candidate.png');await click('Сравнить состав с сервером');expect(host.textContent).toContain('Состав на сервере — версия 2');
  const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);await click('Перезагрузить состав');expect(host.textContent).toContain('candidate.png');
  confirm.mockReturnValue(true);await click('Перезагрузить состав');expect(host.querySelector('[aria-label="Черновик состава"]')).toBeNull();expect(host.textContent).toContain('Версия: 2');
  await click('+ Добавить файл');await pickCandidate();fileFailures.push(new ApiError(403,'revoked'));await click('Сохранить и принять');expect(host.textContent).toContain('Файлы доступны только для просмотра');expect([...host.querySelectorAll('button')].some(button=>button.textContent==='Сохранить и принять')).toBe(false);
});

it('rasterizes PDF pages without scripting/annotations, keeps sandbox and destroys the local parser on close',async()=>{
  await create();await click('+ Добавить файл');await pickCandidate(new File(['pdf'],'sample.pdf',{type:'application/pdf'}));
  staged={...staged,state:'ready',media_type:'application/pdf',extension:'pdf',original_upload_name:'sample.pdf'};
  const blob=new Blob(['pdf']);Object.defineProperty(blob,'arrayBuffer',{value:async()=>new Uint8Array([37,80,68,70]).buffer});vi.mocked(previewBytes).mockResolvedValueOnce(blob);
  pdfMocks.destroy.mockClear();pdfMocks.render.mockClear();pdfMocks.getPage.mockReset();
  pdfMocks.getPage.mockImplementation(async()=>({getViewport:({scale}:{scale:number})=>({width:300*scale,height:200*scale}),render:pdfMocks.render,cleanup:vi.fn()}));
  pdfMocks.getDocument.mockReturnValue({promise:Promise.resolve({numPages:2,getPage:pdfMocks.getPage}),destroy:pdfMocks.destroy});
  vi.spyOn(HTMLCanvasElement.prototype,'toBlob').mockImplementation(callback=>callback(new Blob(['png'],{type:'image/png'})));
  await click('Открыть файл');expect(host.textContent).toContain('Страница PDF 1 из 2');expect(host.querySelector('iframe')!.getAttribute('sandbox')).toBe('');
  expect(pdfMocks.getDocument).toHaveBeenCalledWith(expect.objectContaining({enableXfa:false,disableFontFace:true,useWorkerFetch:false,disableAutoFetch:true,disableRange:true,disableStream:true}));
  expect(pdfMocks.render).toHaveBeenCalledWith(expect.objectContaining({annotationMode:0}));await click('Следующая страница PDF');expect(host.textContent).toContain('Страница PDF 2 из 2');expect(pdfMocks.getPage).toHaveBeenLastCalledWith(2);
  await click('Закрыть просмотр');expect(pdfMocks.destroy).toHaveBeenCalledTimes(1);expect(host.querySelector('iframe')).toBeNull();expect(records[0].version).toBe(1);expect(vi.mocked(api).mock.calls.filter(([path])=>path.endsWith('/accept'))).toHaveLength(0);
});
