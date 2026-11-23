// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import Workspace from './Workspace';
import { emptyRecord } from './model';
import type { RecordData } from './api';

vi.mock('../api',async original=>({...await original<typeof import('../api')>(),api:vi.fn()}));
let host:HTMLDivElement,root:ReturnType<typeof createRoot>,vehicles:RecordData[],failures:ApiError[],grants:Set<string>,mountId=0;
const parents:Record<string,RecordData>={
  'CP-000001':{...emptyRecord('CP'),id:'CP-000001',name:'Источник',version:1,status:'REVIEW'},
  'CP-000002':{...emptyRecord('CP'),id:'CP-000002',name:'Получатель',version:1,status:'REVIEW'},
  'ORG-000001':{...emptyRecord('ORG'),id:'ORG-000001',name:'Своя организация',version:1,status:'REVIEW'},
};
const tick=()=>new Promise(resolve=>setTimeout(resolve,5)),clone=<T,>(v:T):T=>JSON.parse(JSON.stringify(v));
beforeEach(()=>{
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
  host=document.createElement('div');document.body.append(host);root=createRoot(host);
  grants=new Set(['arm2_veh_view','arm2_veh_edit','arm2_cp_view','arm2_cp_edit','arm2_org_view','arm2_org_edit']);
  failures=[];vehicles=Array.from({length:105},(_,i)=>({...emptyRecord('VEH'),id:'VEH-'+String(i+1).padStart(6,'0'),name:'Авто '+String(i+1).padStart(3,'0'),plate_number:`А${String(i+1).padStart(3,'0')}ВС86`,version:1,model:'Модель',wialon_unit_id:'unit-'+i,owner_cp_id:i===0?'CP-000001':null}));
  vi.mocked(api).mockReset();vi.mocked(api).mockImplementation(async(path,method='GET',body)=>{
    const url=new URL(path,'http://qa');
    if(url.pathname.endsWith('/operational-owner')){
      if(failures.length)throw failures.shift()!;
      const id=url.pathname.split('/').at(-2),record=vehicles.find(row=>row.id===id)!;
      const b=body as {expected_version:number;expected_owner_kind:string|null;expected_owner_id:string|null;target_kind:string|null;target_id:string|null;confirm_reassignment:boolean};
      const oldKind=record.owner_cp_id?'CP':record.owner_org_id?'ORG':null,oldId=record.owner_cp_id||record.owner_org_id||null;
      if(record.version!==b.expected_version||oldKind!==b.expected_owner_kind||oldId!==b.expected_owner_id)throw new ApiError(409,'stale owner');
      const changed=oldKind!==b.target_kind||oldId!==b.target_id;
      if(changed&&oldKind&&!b.confirm_reassignment)throw new ApiError(409,'confirmation');
      if(changed){record.owner_cp_id=b.target_kind==='CP'?b.target_id:null;record.owner_org_id=b.target_kind==='ORG'?b.target_id:null;record.version++;}
      return {...clone(record),no_op:!changed};
    }
    if(url.pathname==='/arm2/vehicles/search'){
      const q=(url.searchParams.get('q')||'').toLowerCase(),offset=Number(url.searchParams.get('offset')||0),kind=url.searchParams.get('owner_kind'),owner=url.searchParams.get('owner_id');
      const items=vehicles.filter(row=>(row.name.toLowerCase().includes(q)||String(row.plate_number).toLowerCase().includes(q))&&(!kind||(kind==='NONE'?!row.owner_cp_id&&!row.owner_org_id:row[kind==='CP'?'owner_cp_id':'owner_org_id']===owner)));
      return {items:items.slice(offset,offset+100).map(row=>({...clone(row),owner_kind:row.owner_cp_id?'CP':row.owner_org_id?'ORG':null,owner_name:row.owner_cp_id&&grants.has('arm2_cp_view')?parents[String(row.owner_cp_id)].name:row.owner_org_id&&grants.has('arm2_org_view')?parents[String(row.owner_org_id)].name:null})),matched:items.length,total:vehicles.length,offset,limit:100};
    }
    if(url.pathname.endsWith('/search')){const kind=url.pathname.includes('counterparties')?'CP':'ORG';const items=Object.values(parents).filter(row=>row.id.startsWith(kind));return {items:clone(items),matched:items.length,total:items.length,offset:0,limit:100};}
    const id=url.pathname.split('/').at(-1)!;
    if(parents[id])return clone(parents[id]);
    const vehicle=vehicles.find(row=>row.id===id);
    if(vehicle){if(method==='PUT'){const b=body as Record<string,unknown>;if(b.expected_version!==vehicle.version)throw new ApiError(409,'stale');Object.assign(vehicle,b,{version:vehicle.version+1});}return clone(vehicle);}
    if(path.startsWith('/arm2/history'))return [];
    throw new ApiError(404,'not found');
  });
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();});
function Back(){const nav=useNavigate();return <button onClick={()=>nav(-1)}>Browser back</button>;}
async function mount(path='/arm2/cp/CP-000002?tab=vehicles'){await act(async()=>{root.render(<MemoryRouter key={++mountId} initialEntries={['/arm2/veh',path]} initialIndex={1}><Routes><Route path="/arm2/*" element={<Workspace can={p=>grants.has(p)}/>}/></Routes><Back/></MemoryRouter>);await tick();});}
async function click(text:string){await act(async()=>{const button=[...host.querySelectorAll<HTMLButtonElement>('button')].find(node=>node.textContent===text);if(!button)throw Error('Missing '+text);button.click();await tick();});}
function field(label:string){const element=[...host.querySelectorAll('label')].find(node=>node.textContent?.startsWith(label))?.querySelector<HTMLInputElement>('input');if(!element)throw Error('Missing field '+label);return element;}
async function change(label:string,value:string){await act(async()=>{const input=field(label);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}));await tick();});}
async function select(name:string){await act(async()=>{const region=host.querySelector('[aria-label="Поиск существующей техники"]')!;const row=[...region.querySelectorAll('tbody tr')].find(row=>row.querySelector('a')?.textContent===name)!;const button=[...row.querySelectorAll<HTMLButtonElement>('button')].find(node=>node.textContent==='Выбрать технику')!;button.click();await tick();});}
async function approve(){await act(async()=>{const checkbox=host.querySelector<HTMLInputElement>('.arm2-owner-confirm input')!;checkbox.click();await tick();});}
const posts=()=>vi.mocked(api).mock.calls.filter(([,method])=>method==='POST'||method==='PUT');

it('uses the existing server search beyond 100, confirms a free vehicle and refreshes the owner-filtered table',async()=>{
  await mount();await click('Добавить технику');await click('Следующая страница поиска');
  expect(vi.mocked(api).mock.calls.some(([url])=>url.includes('offset=100')&&!url.includes('owner_kind'))).toBe(true);
  await select('Авто 105');expect(posts()).toHaveLength(0);expect(host.textContent).toContain('Получатель');expect(host.querySelector('[aria-label="Подтверждение операционного владельца"]')?.textContent).toContain('Без владельца');
  await click('Подтвердить назначение');expect(vehicles[104]).toMatchObject({id:'VEH-000105',owner_cp_id:'CP-000002',owner_org_id:null,version:2,model:'Модель',wialon_unit_id:'unit-104'});
  expect(posts()).toHaveLength(1);expect(posts()[0][2]).toEqual({expected_version:1,target_kind:'CP',target_id:'CP-000002',expected_owner_kind:null,expected_owner_id:null,confirm_reassignment:false,initiator_kind:'CP',initiator_id:'CP-000002'});
  expect(host.querySelector('[aria-label="Техника владельца"] table')?.textContent).toContain('Авто 105');expect(host.querySelectorAll('table th')).toHaveLength(5);
});

it('shows the real previous owner, does nothing on cancel and needs a second transfer confirmation',async()=>{
  await mount();await click('Добавить технику');await select('Авто 001');expect(host.textContent).toContain('Контрагент · Источник · CP-000001');
  await click('Подтвердить назначение');expect(posts()).toHaveLength(0);await click('Отмена изменения связи');expect(vehicles[0].owner_cp_id).toBe('CP-000001');
  await select('Авто 001');await approve();await click('Подтвердить назначение');expect(vehicles[0]).toMatchObject({owner_cp_id:'CP-000002',owner_org_id:null,version:2});
  await mount('/arm2/org/ORG-000001?tab=vehicles');await click('Добавить технику');await select('Авто 001');await approve();await click('Подтвердить назначение');expect(vehicles[0]).toMatchObject({owner_cp_id:null,owner_org_id:'ORG-000001',version:3});
});

it('clears a link with no reason and keeps the vehicle in the general registry',async()=>{
  await mount('/arm2/cp/CP-000001?tab=vehicles');await click('Снять привязку');await approve();await click('Подтвердить снятие привязки');expect(vehicles[0]).toMatchObject({owner_cp_id:null,owner_org_id:null,version:2});expect(host.textContent).toContain('По заданным условиям записей нет');expect(posts()[0][2]).not.toHaveProperty('operator_reason');
  await mount('/arm2/veh/VEH-000001');expect(host.textContent).toContain('Авто 001');expect(field('Операционный владелец').placeholder).toBe('Без владельца');
});

it('preserves a stale choice on 409 and adopts a new version only with a fresh explicit confirmation',async()=>{
  await mount();await click('Добавить технику');await select('Авто 002');vehicles[1]={...vehicles[1],owner_cp_id:'CP-000001',version:2};
  await click('Подтвердить назначение');expect(host.textContent).toContain('Авто 002');await click('Сравнить владельца с сервером');expect(host.textContent).toContain('Версия на сервере: 2');expect(host.querySelector('[aria-label="Подтверждение операционного владельца"]')?.textContent).toContain('Версия 1');
  await click('Принять актуальную версию и подтвердить заново');await click('Подтвердить назначение');expect(posts()).toHaveLength(1);await approve();await click('Подтвердить назначение');expect(vehicles[1].owner_cp_id).toBe('CP-000002');expect(posts()[1][2]).toMatchObject({expected_version:2,expected_owner_kind:'CP',expected_owner_id:'CP-000001',confirm_reassignment:true});
});

it('retains the exact pending request after 503 and blocks mutation after 403',async()=>{
  await mount();await click('Добавить технику');await select('Авто 002');failures.push(new ApiError(503,'qa'));await click('Подтвердить назначение');const first=clone(posts()[0][2]);expect(host.textContent).toContain('Авто 002');await click('Подтвердить назначение');expect(posts()[1][2]).toEqual(first);
  await click('Добавить технику');await select('Авто 003');failures.push(new ApiError(403,'qa'));await click('Подтвердить назначение');expect(host.textContent).toContain('Нет прав на изменение этой связи');expect(host.querySelector('[aria-label="Подтверждение операционного владельца"] fieldset')?.hasAttribute('disabled')).toBe(true);expect(host.textContent).toContain('Авто 003');
});

it('cancelled Back retains the selection and a CP main draft is never included in the VEH command',async()=>{
  await mount('/arm2/cp/CP-000002');await change('Рабочее имя','Несохранённое имя');await click('Техника');await click('Добавить технику');await select('Авто 002');const confirmation=vi.spyOn(window,'confirm').mockReturnValue(false);await click('Browser back');expect(host.textContent).toContain('Авто 002');expect(confirmation).toHaveBeenCalled();await click('Подтвердить назначение');expect(posts()[0][2]).not.toHaveProperty('name');await click('Основное');expect(field('Рабочее имя').value).toBe('Несохранённое имя');expect(parents['CP-000002'].name).toBe('Получатель');
});

it('VIEW-only cannot mutate and a hidden previous owner is not resolved or selectable',async()=>{
  grants.delete('arm2_veh_edit');await mount();expect(host.textContent).not.toContain('Добавить технику');expect(posts()).toHaveLength(0);
  grants.add('arm2_veh_edit');grants.delete('arm2_org_view');grants.delete('arm2_org_edit');vehicles[0]={...vehicles[0],owner_cp_id:null,owner_org_id:'ORG-000001'};await mount();await click('Добавить технику');const row=[...host.querySelectorAll('[aria-label="Поиск существующей техники"] tbody tr')].find(row=>row.querySelector('a')?.textContent==='Авто 001')!;expect(row.textContent).toContain('Нет доступа к имени');expect(row.querySelector('button')?.disabled).toBe(true);expect(vi.mocked(api).mock.calls.some(([url])=>url==='/arm2/organizations/ORG-000001')).toBe(false);
});

it('the existing VEH OwnerPicker warns before replacement and cancellation changes no fields or requests',async()=>{
  await mount('/arm2/veh/VEH-000001');const confirmation=vi.spyOn(window,'confirm').mockReturnValue(false);await act(async()=>{field('Операционный владелец').focus();await tick();});
  await act(async()=>{const choice=[...host.querySelectorAll<HTMLButtonElement>('[role=option]')].find(node=>node.textContent?.startsWith('Организация · Своя организация'))!;choice.click();await tick();});
  expect(confirmation).toHaveBeenCalledWith(expect.stringContaining('Контрагент · Источник · CP-000001'));expect(vehicles[0].owner_cp_id).toBe('CP-000001');expect(posts()).toHaveLength(0);expect(field('Операционный владелец').value).toContain('Источник');
});

it('unlink from VEH is disabled while its main fields are unsaved and does not send them implicitly',async()=>{
  await mount('/arm2/veh/VEH-000001');await change('Имя','Мой черновик');expect([...host.querySelectorAll<HTMLButtonElement>('button')].find(node=>node.textContent==='Снять привязку')?.disabled).toBe(true);expect(posts()).toHaveLength(0);expect(host.textContent).toContain('Сначала сохраните изменения основной карточки');
});

it('a slow preview GET cannot reopen an owner choice after the operator cancels it',async()=>{
  await mount();await click('Добавить технику');
  const original=vi.mocked(api).getMockImplementation()!;let release!:(value:RecordData)=>void;
  vi.mocked(api).mockImplementation((path,...args)=>path==='/arm2/vehicles/VEH-000002'?new Promise(resolve=>{release=resolve;}):original(path,...args));
  await select('Авто 002');expect(host.textContent).toContain('Актуальная запись ещё не загружена');
  await click('Отмена изменения связи');
  await act(async()=>{release(clone(vehicles[1]));await tick();});
  expect(host.querySelector('[aria-label="Подтверждение операционного владельца"]')).toBeNull();expect(posts()).toHaveLength(0);
});

it('read-only embedded veh_ filters keep the pending selection without a leave-card prompt',async()=>{
  await mount();await click('Добавить технику');await select('Авто 002');
  const confirmation=vi.spyOn(window,'confirm').mockReturnValue(false);
  await change('Поиск по рабочему имени / госномеру','Авто');
  expect(confirmation).not.toHaveBeenCalled();expect(host.querySelector('[aria-label="Подтверждение операционного владельца"]')?.textContent).toContain('Авто 002');
  const request=vi.mocked(api).mock.calls.filter(([url])=>url.startsWith('/arm2/vehicles/search?')&&url.includes('owner_kind=CP')).at(-1)![0];
  expect(new URL(request,'http://qa').searchParams.get('owner_id')).toBe('CP-000002');expect(posts()).toHaveLength(0);
});
