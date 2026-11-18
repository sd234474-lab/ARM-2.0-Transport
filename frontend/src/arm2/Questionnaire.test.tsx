// @vitest-environment jsdom
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {api, ApiError} from '../api';
import type {QuestionnaireDraft, QuestionnaireReceipt} from './api';
import Questionnaire, {QuestionnaireExport} from './Questionnaire';

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
async function acceptPhone(){await act(async()=>{host.querySelector<HTMLInputElement>('input[aria-label="Принять Официальный телефон"]')!.click();await tick();});}

it('T01 opens comparison on file selection with no second upload confirmation and no domain save',async()=>{
  await mount();
  await act(async()=>{const input=host.querySelector<HTMLInputElement>('input[type=file]')!;Object.defineProperty(input,'files',{configurable:true,value:[new File(['synthetic'],'synthetic.xlsx')]});input.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
  expect(host.textContent).toContain('Черновик сохранён на сервере');
  expect(host.textContent).toContain('Hint only');
  expect(api).toHaveBeenCalledWith('/arm2/questionnaires?filename=synthetic.xlsx','POST',expect.any(File));
  expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/apply'))).toBe(false);
});

it('T03/T05 only explicitly selected unequal fields are persisted in the draft before atomic SAVE',async()=>{
  await mount('/arm2/questionnaires?draft='+draft.id);
  expect(host.textContent).toContain('Соответствует');
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
  expect(host.textContent).toContain('Результат запроса требует проверки');
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
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Принять Официальный телефон"]')!.disabled).toBe(true);
  await click('Повторно открыть сверку');
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Принять Официальный телефон"]')!.checked).toBe(true);
  expect(host.querySelector<HTMLInputElement>('input[aria-label="Принять Официальный телефон"]')!.disabled).toBe(false);
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
