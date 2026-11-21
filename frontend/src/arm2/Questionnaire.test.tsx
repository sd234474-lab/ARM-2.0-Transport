// @vitest-environment jsdom
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {api, ApiError} from '../api';
import type {QuestionnaireDraft, QuestionnaireReceipt} from './api';
import * as questionnaireTransport from './api';
import Questionnaire, {QuestionnaireExport, QuestionnairePreview} from './Questionnaire';
import {errorText} from './model';
import {readFileSync} from 'node:fs';
import {getDocument, GlobalWorkerOptions} from 'pdfjs-dist';

vi.mock('pdfjs-dist',()=>({getDocument:vi.fn(),GlobalWorkerOptions:{workerSrc:''}}));

vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn()}));
let host:HTMLDivElement,root:ReturnType<typeof createRoot>,draft:QuestionnaireDraft,receipt:QuestionnaireReceipt;
let lost=false,applied=false,stale=false;
const clone=<T,>(value:T):T=>structuredClone(value);
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
beforeEach(()=>{
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
  host=document.createElement('div');document.body.append(host);root=createRoot(host);
  lost=applied=stale=false;
  draft={id:'00000000-0000-4000-8000-000000000005',revision:1,status:'OPEN',filename:'synthetic.xlsx',preview_id:'a'.repeat(64),choices:{cp_fields:[]},comparison:{
    cp:{key:'CP',id:null,target:null,version:null,hint:'Hint only',error:'Choose CP',candidates:[{id:'CP-000001',name:'Synthetic CP',inn:null}],fields:{phone:{current:'old',proposed:'new',selected:false,equal:false,error:null},name:{current:'Same',proposed:'Same',selected:false,equal:true,error:null}}},
    representatives:[],vehicles:[],documents:[],types:[],document_kinds:[],warnings:[],filled_date:'2026-10-21',uploaded_at:'2026-11-18T08:00:00+00:00'}};
  receipt={command_id:'',draft_id:draft.id,cp_id:'CP-000001',filled_date:'2026-10-21',uploaded_at:'2026-11-18T08:00:00+00:00',accepted_at:'2026-11-18T09:00:00+00:00',correlation_id:'correlation',no_op:false,event_ids:[17],objects:[{kind:'CP',id:'CP-000001',version:2}]};
  vi.mocked(api).mockReset();vi.mocked(api).mockImplementation(async(path,method='GET',body)=>{
    if(path==='/arm2/questionnaires'&&method==='GET')return [{id:draft.id,filename:draft.filename,revision:draft.revision,uploaded_at:draft.comparison?.uploaded_at}];
    if(path==='/arm2/questionnaires/receipts')return applied?[clone(receipt)]:[];
    if(path.startsWith('/arm2/questionnaires?')&&method==='POST')return clone(draft);
    if(path==='/arm2/questionnaires/'+draft.id&&method==='GET')return applied?{...clone(draft),status:'ACCEPTED',receipt:clone(receipt)}:clone(draft);
    if(path==='/arm2/questionnaires/'+draft.id&&method==='PUT'){
      draft.choices=(body as {choices:QuestionnaireDraft['choices']}).choices;
      draft.revision++;
      draft.comparison!.cp.target=draft.choices?.cp_target||null;
      draft.comparison!.cp.id=draft.choices?.cp_target==='CP-000001'?'CP-000001':null;
      draft.comparison!.cp.error=draft.choices?.cp_target?null:'Choose CP';
      for(const [field,value] of Object.entries(draft.comparison!.cp.fields))value.selected=!!draft.choices?.cp_fields?.includes(field);
      for(const value of draft.comparison!.representatives){
        const choice=draft.choices?.representatives?.[value.key];value.sources=choice?.sources||{};value.target=choice?.target||null;
        for(const [field,f] of Object.entries(value.fields)){
          f.selected=!!choice?.fields?.includes(field);
          const source=choice?.sources?.[field],alternatives=value.conflicts?.[field];
          if(alternatives&&source){f.proposed=alternatives[source];f.error=null;f.equal=f.proposed===f.current;}
        }
      }
      return clone(draft);
    }
    if(path.endsWith('/apply')){
      if(stale)throw new ApiError(409,'Сверка устарела');
      receipt.command_id=(body as {command_id:string}).command_id;
      applied=true;
      if(lost){lost=false;throw new Error('Connection lost after commit');}
      return clone(receipt);
    }
    if(method==='DELETE')return {status:'CANCELLED'};
    if(path==='/arm2/counterparties/CP-000001')return {id:'CP-000001',name:'Synthetic CP'};
    throw new Error('Unexpected '+method+' '+path);
  });
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();});
async function mount(path='/arm2/questionnaires',child?:React.ReactNode){await act(async()=>{root.render(<MemoryRouter initialEntries={[path]}>{child||<Questionnaire can={()=>true}/>}</MemoryRouter>);await tick();});}
async function click(text:string){await act(async()=>{const button=[...host.querySelectorAll<HTMLButtonElement>('button')].find(b=>b.textContent===text);if(!button)throw new Error('Missing '+text);button.click();await tick();});}
async function selectCP(){await act(async()=>{const select=host.querySelector<HTMLSelectElement>('select[aria-label="Решение о контрагенте"]')!;select.value='CP-000001';select.dispatchEvent(new Event('change',{bubbles:true}));await tick();});}
async function acceptPhone(){await act(async()=>{const control=host.querySelector<HTMLSelectElement>('select[aria-label="Решение Официальный телефон"]')!;control.value='set';control.dispatchEvent(new Event('change',{bubbles:true}));await tick();});}

it('CSP permits only same-origin/blob PDF workers; renderer errors retain the original download',async()=>{
  const nginx=readFileSync('nginx.conf','utf8');
  expect(nginx).toContain("worker-src 'self' blob:");
  expect(nginx).toMatch(/location \/assets\/\s*\{[^}]*location ~ \\\.mjs\$\s*\{\s*types\s*\{\s*application\/javascript mjs;/s);
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({arrayBuffer:async()=>new ArrayBuffer(8)}));
  vi.mocked(getDocument).mockImplementation(()=>({promise:Promise.reject(new Error('renderer failed')),destroy:vi.fn()}) as never);
  await act(async()=>{root.render(<QuestionnairePreview value={{url:'blob:verified',name:'synthetic.pdf',media:'application/pdf',blob:{arrayBuffer:async()=>new ArrayBuffer(8)} as Blob}} close={()=>{}}/>);await tick();});
  expect(GlobalWorkerOptions.workerSrc).toContain('pdf.worker');
  expect(new URL(GlobalWorkerOptions.workerSrc).searchParams.get('module')).toBe('1');
  expect(fetch).not.toHaveBeenCalled(); // Reuse the verified Blob; connect-src need not allow blob fetch.
  expect(host.textContent).toContain('Не удалось отобразить PDF во встроенном просмотре');
  expect(host.textContent).not.toContain('Не удалось связаться с сервером');
  expect(host.querySelector('a[download="synthetic.pdf"]')?.getAttribute('href')).toBe('blob:verified');
  vi.unstubAllGlobals();
});

it('PDF rendering uses verified bytes and opaque files offer download without inline rendering',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({arrayBuffer:async()=>new ArrayBuffer(8)}));
  const render=vi.fn().mockReturnValue({promise:Promise.resolve(),cancel:vi.fn()});
  vi.mocked(getDocument).mockReturnValue({promise:Promise.resolve({numPages:1,getPage:async()=>({getViewport:()=>({width:100,height:100}),render})}),destroy:vi.fn()} as never);
  await act(async()=>{root.render(<QuestionnairePreview value={{url:'blob:verified',name:'synthetic.pdf',media:'application/pdf'}} close={()=>{}}/>);await tick();});
  await act(async()=>{await tick();});
  expect(render).toHaveBeenCalled();
  expect(host.querySelector('[role="alert"]')).toBeNull();
  await act(async()=>{root.render(<QuestionnairePreview key="opaque" value={{url:'blob:opaque',name:'qqq.doc',media:'application/octet-stream'}} close={()=>{}}/>);await tick();});
  expect(host.textContent).toContain('Формат доступен скачиванием оригинала');
  expect(host.querySelector('canvas,iframe,object')).toBeNull();
  vi.unstubAllGlobals();
});

it('opens the verified candidate in a separate tab without moving the questionnaire, and releases it on close',async()=>{
  draft.comparison!.documents=[{key:'file',name:'synthetic.doc',parent_key:'CP',role:'Карточка',upload_id:'upload',selected:false,media_type:'application/octet-stream'}];
  const tabDocument=document.implementation.createHTMLDocument('');
  const close=vi.fn();const tab={document:tabDocument,closed:false,close,opener:window} as unknown as Window;
  const open=vi.spyOn(window,'open').mockReturnValue(tab);
  const bytes=vi.spyOn(questionnaireTransport,'previewBytes').mockImplementation(async()=>{
    expect(open).toHaveBeenCalledWith('about:blank','_blank');
    return new Blob(['synthetic'],{type:'application/octet-stream'});
  });
  Object.defineProperty(URL,'createObjectURL',{configurable:true,writable:true,value:vi.fn().mockReturnValue('blob:verified-tab')});
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,writable:true,value:vi.fn()});
  await mount('/arm2/questionnaires?draft='+draft.id);
  await click('Открыть кандидат');
  expect(bytes).toHaveBeenCalledWith(`/arm2/questionnaires/${draft.id}/files/upload/preview`,128*1024*1024);
  expect(tab.opener).toBeNull();expect(tabDocument.title).toBe('synthetic.doc');
  expect(host.querySelector('[aria-label="Просмотр вложения"]')).toBeNull();
  expect(tabDocument.querySelector('a[download="synthetic.doc"]')?.getAttribute('href')).toBe('blob:verified-tab');
  expect(tabDocument.querySelector('iframe,object')).toBeNull();
  await act(async()=>{tabDocument.querySelector<HTMLButtonElement>('button')!.click();await tick();});
  expect(close).toHaveBeenCalled();expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:verified-tab');
  expect(tabDocument.querySelector('[aria-label="Просмотр вложения"]')).toBeNull();
});

it('reports a blocked preview tab without fetching the file',async()=>{
  draft.comparison!.documents=[{key:'file',name:'synthetic.pdf',parent_key:'CP',role:'Карточка',upload_id:'upload',selected:false,media_type:'application/pdf'}];
  vi.spyOn(window,'open').mockReturnValue(null);
  const bytes=vi.spyOn(questionnaireTransport,'previewBytes');
  await mount('/arm2/questionnaires?draft='+draft.id);await click('Открыть кандидат');
  expect(bytes).not.toHaveBeenCalled();
  expect(host.textContent).toContain('Разрешите открытие новой вкладки');
});

it('409 preserves safe backend detail while unknown client errors are not called network failures',()=>{
  expect(errorText(new ApiError(409,'Отдельно подтвердите конфликт личности, изменение типа или ОПФ'))).toContain('Отдельно подтвердите');
  expect(errorText(new ApiError(409,'<html>private</html>'))).not.toContain('<html>');
  expect(errorText(new Error('renderer failed'))).toBe('Не удалось выполнить операцию');
  expect(errorText(new TypeError('Failed to fetch'))).toContain('Не удалось связаться с сервером');
});

it('physical-file selector shows the resolved file rather than an obsolete manual workaround',async()=>{
  draft.choices!.documents={calibration:{upload_id:'old-sts',accept:false}};
  draft.comparison!.documents=[{key:'calibration',name:'calibration.pdf',parent_key:'CP',role:'Тарировка',
    upload_id:'resolved-calibration',selected:false,error:'Choose kind',attachment_options:[
      {id:'old-sts',name:'sts.pdf'},{id:'resolved-calibration',name:'calibration.pdf'}]}];
  await mount('/arm2/questionnaires?draft='+draft.id);
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="Физический файл Тарировка"]')!.value).toBe('resolved-calibration');
  expect(vi.mocked(api).mock.calls.some(([,method])=>method==='PUT'||method==='POST')).toBe(false);
});

it('new package groups documents with each vehicle, keeps decisions and warns if every vehicle is excluded',async()=>{
  const fields={name:{current:null,proposed:'Truck',selected:true,equal:false,error:null},owner:{current:null,proposed:['CP','$CP'],proposed_display:'Synthetic CP',selected:true,equal:false,error:null}};
  draft.choices={cp_target:'new',vehicles:{'v:5':{include:true,fields:['name','owner']},'v:6':{include:true,fields:['name','owner']}},documents:{a:{accept:true},b:{accept:true}}};
  draft.comparison!.vehicles=[5,6].map(n=>({key:'v:'+n,row:n,id:null,plate_number:n===5?'А234ВС86':'1234АВ86',version:null,fields,errors:[],withdrawal:false,confirm_owner:false,without_type:false,source:'',volume_proposal:'',volume_notice:''}));
  draft.comparison!.documents=[5,6].map((n,i)=>({key:i?'b':'a',name:i?'abc.pdf':'qqq.doc',parent_key:'v:'+n,role:'СТС',upload_id:'upload'+n,selected:true,media_type:'application/pdf'}));
  await mount('/arm2/questionnaires?draft='+draft.id);
  const row5=host.querySelector('[aria-label="Строка техники 5"]')!;
  expect(row5.textContent).toContain('qqq.doc');expect(row5.textContent).not.toContain('abc.pdf');
  expect(host.querySelector('[aria-label="Будет принято"]')!.textContent).toContain('Машины: 2 из 2');
  expect(row5.textContent).not.toContain('$CP');
  for(const n of [5,6])await act(async()=>{const select=host.querySelector<HTMLSelectElement>(`select[aria-label="Добавить машину строка ${n}"]`)!;select.value='skip';select.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
  expect(host.textContent).toContain('Машины не будут сохранены: 0 из 2');
  expect(draft.choices?.vehicles?.['v:5'].include).toBe(false);
});

it('CP clear is an explicit draft action, mutually exclusive with set, and identity needs its own approval',async()=>{
  draft.comparison!.cp.identity_conflict=true;
  draft.comparison!.cp.fields.legal_form={current:'ИП',proposed:'ИП',equal:true,selected:false,error:null,clear_candidate:true};
  await mount('/arm2/questionnaires?draft='+draft.id);await selectCP();
  expect(draft.choices?.confirm_identity).not.toBe(true);
  await act(async()=>{const control=host.querySelector<HTMLSelectElement>('select[aria-label="Решение Организационно-правовая форма"]')!;control.value='clear';control.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
  expect(draft.choices?.cp_clears).toEqual(['legal_form']);
  expect(draft.choices?.cp_fields).not.toContain('legal_form');
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/apply'))).toBe(false);
  await act(async()=>{[...host.querySelectorAll('label')].find(l=>l.textContent?.startsWith('Подтверждаю использование выбранного контрагента'))!.querySelector<HTMLInputElement>('input')!.click();await tick();});
  expect(draft.choices?.confirm_identity).toBe(true);
  await click('Отклонить загрузку целиком');
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/apply'))).toBe(false);
});

it('receipt displays actual before/after fields and a direct card link',async()=>{
  receipt.changes=[{kind:'CP',id:'CP-000001',operation:'UPDATED',url:'/arm2/cp/CP-000001',fields:{phone:{before:'old phone',after:'new phone'}}}];
  await mount('/arm2/questionnaires?draft='+draft.id);await selectCP();await acceptPhone();await click('Принять изменения');
  const table=host.querySelector('[aria-label="Протокол приёмки анкеты"] table')!;
  expect(table.textContent).toContain('Официальный телефон');expect(table.textContent).toContain('old phone');expect(table.textContent).toContain('new phone');
  expect(table.querySelector('a')!.getAttribute('href')).toBe('/arm2/cp/CP-000001');
});

it('V3 signer conflict needs an explicit source and a separate field approval; no access controls',async()=>{
  draft.comparison!.representatives=[{key:'0',id:'CPREP-000001',target:'CPREP-000001',version:1,error:null,signer:false,
    source_id:'CPREP-000001',source_comment:'Synthetic review note',signer_hint:true,conflicts:{name:{passport:'Passport name',list:'List name'}},sources:{},
    fields:{name:{current:'Current name',proposed:'List name',selected:false,equal:false,error:'Choose source'}},
    candidates:[{id:'CPREP-000001',name:'Current name'}]}];
  draft.choices!.representatives={'0':{target:'CPREP-000001',fields:[]}};
  await mount('/arm2/questionnaires?draft='+draft.id);
  expect(host.textContent).toContain('Паспорт: Passport name');
  expect(host.textContent).toContain('Представители: List name');
  expect(host.textContent).toContain('Пометка анкеты: Synthetic review note');
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Принять Рабочее имя"]')!.disabled).toBe(true);
  await act(async()=>{const select=host.querySelector<HTMLSelectElement>('select[aria-label="Источник подписанта Рабочее имя"]')!;select.value='passport';select.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
  expect(draft.choices!.representatives!['0'].sources).toEqual({name:'passport'});
  expect(draft.choices!.representatives!['0'].fields).toEqual([]);
  await act(async()=>{host.querySelector<HTMLInputElement>('input[aria-label="Принять Рабочее имя"]')!.click();await tick();});
  expect(draft.choices!.representatives!['0'].fields).toEqual(['name']);
  expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/apply'))).toBe(false);
  expect(host.querySelector('input[name="user_id"]')).toBeNull();
});

it('T01 opens comparison on file selection with no second upload confirmation and no domain save',async()=>{
  await mount();
  await act(async()=>{const input=host.querySelector<HTMLInputElement>('input[type=file]')!;Object.defineProperty(input,'files',{configurable:true,value:[new File(['synthetic'],'synthetic.xlsx')]});input.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
  expect(host.textContent).toContain('Черновик сохранён на сервере');
  expect(host.textContent).not.toContain('Hint only');
  expect(api).toHaveBeenCalledWith('/arm2/questionnaires?filename=synthetic.xlsx','POST',expect.any(File));
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/apply'))).toBe(false);
});

it('T03/T05 only explicitly selected unequal fields are persisted in the draft before atomic SAVE',async()=>{
  await mount('/arm2/questionnaires?draft='+draft.id);
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1); // Equal values need no second approval.
  await selectCP();await acceptPhone();
  const edits=vi.mocked(api).mock.calls.filter(([,method])=>method==='PUT');
  expect((edits.at(-1)![2] as {choices:{cp_fields:string[]}}).choices.cp_fields).toEqual(['phone']);
  expect(applied).toBe(false);
  await click('Принять изменения');
  expect(host.textContent).toContain('Изменения приняты');
  expect(host.textContent).toContain('События истории: 17');
});

it('T04 a lost response retries the exact same command UUID and never uses per-VEH bulk apply',async()=>{
  await mount('/arm2/questionnaires?draft='+draft.id);await selectCP();await acceptPhone();lost=true;
  await click('Принять изменения');
  expect(host.textContent).toContain('Проверьте результат запроса');
  await click('Повторить ту же команду');
  const calls=vi.mocked(api).mock.calls.filter(([p])=>p.endsWith('/apply'));
  expect(calls).toHaveLength(2);expect(calls[1][2]).toEqual(calls[0][2]);
  expect(calls.every(([p])=>p.startsWith('/arm2/questionnaires/'))).toBe(true);
  expect(host.textContent).toContain('Изменения приняты');
});

it('T24 a 409 preserves the server draft and offers explicit reopen before changing decisions',async()=>{
  await mount('/arm2/questionnaires?draft='+draft.id);await selectCP();await acceptPhone();stale=true;
  await click('Принять изменения');
  expect(host.textContent).toContain('Сверка устарела');
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="Решение Официальный телефон"]')!.disabled).toBe(true);
  await click('Повторно открыть сверку');
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="Решение Официальный телефон"]')!.value).toBe('set');
  expect(host.querySelector<HTMLSelectElement>('select[aria-label="Решение Официальный телефон"]')!.disabled).toBe(false);
});

it('T23 cancelling the whole package clears the review without calling SAVE',async()=>{
  await mount('/arm2/questionnaires?draft='+draft.id);await click('Отклонить загрузку целиком');
  expect(api).toHaveBeenCalledWith('/arm2/questionnaires/'+draft.id,'DELETE');
  expect(host.querySelector('[aria-label="Сверка анкеты"]')).toBeNull();
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/apply'))).toBe(false);
});

it('T25/T28 reopen shows separate completion/upload dates and accepted receipt date',async()=>{
  applied=true;
  await mount('/arm2/questionnaires?draft='+draft.id);
  expect(host.textContent).toContain('2026-10-21');expect(host.textContent).toContain('2026-11-18T08:00:00');expect(host.textContent).toContain('2026-11-18T09:00:00');
  expect(host.querySelector('[aria-label="Сверка анкеты"]')).toBeNull();
});

it('T27 incomplete ZIP is never downloaded silently and requires a typed reason',async()=>{
  vi.mocked(api).mockImplementation(async(_p,_m,body)=>{
    if((body as {incomplete_reason?:string}).incomplete_reason)return {data:'eA==',filename:'questionnaire.zip',media_type:'application/zip'};
    return {confirmation_required:true,missing:[{name:'missing.pdf',file_id:'FILE-000001',document_id:'DOCUMENT-000001'}]};
  });
  vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:vi.fn(()=> 'blob:synthetic'),revokeObjectURL:vi.fn()}));
  vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
  await mount('/',<QuestionnaireExport cpId="CP-000001"/>);await click('Выгрузить ZIP с текущими документами');
  expect(host.textContent).toContain('missing.pdf');
  const button=[...host.querySelectorAll<HTMLButtonElement>('button')].find(b=>b.textContent==='Явно выдать неполный ZIP')!;
  expect(button.disabled).toBe(true);
  await act(async()=>{const input=host.querySelector('input')!;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,'Acknowledged by operator');input.dispatchEvent(new Event('input',{bubbles:true}));await tick();});
  await click('Явно выдать неполный ZIP');
  expect(vi.mocked(api).mock.calls.at(-1)![2]).toEqual({format:'zip',incomplete_reason:'Acknowledged by operator'});
});
