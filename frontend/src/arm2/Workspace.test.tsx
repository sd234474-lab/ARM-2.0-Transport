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
const party = {...emptyRecord('CP'), id: 'CP-1', name: 'Партнёр', version: 2, full_name: 'Полное имя', legal_address: 'Адрес', type: 'Юридическое лицо', primary_signer_id: null};
const vehicle = {...emptyRecord('VEH'), id: 'VEH-1', name: 'Машина', plate_number: 'А001АА86', version: 3, type_id: 'TYPE-1', model: 'Модель', owner_cp_id: 'CP-1'};

async function ariaSelect(label:string,value:string) {
  await act(async()=>{const select=element.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;expect(select).not.toBeNull();select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));await tick();});
}
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api).mockReset();
  element = document.createElement('div'); document.body.append(element); root = createRoot(element);
});
afterEach(async () => {await act(async () => root.unmount()); element.remove(); vi.restoreAllMocks();});
async function mount(path: string, can: Can = () => true, userLookupAllowed = false) {await act(async () => {root.render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/arm2/*" element={<Workspace can={can} userLookupAllowed={userLookupAllowed}/>}/></Routes></MemoryRouter>); await tick();});}
function button(text: string) {const found = [...element.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent === text); if (!found) throw Error('No button: ' + text); return found;}
async function click(text: string) {await act(async () => {button(text).click(); await tick();});}
async function follow(text: string) {await act(async () => {const a = [...element.querySelectorAll<HTMLAnchorElement>('a')].find(item => (item.getAttribute('aria-label') || item.textContent) === text); if (!a) throw Error('No link: ' + text); a.click(); await tick();});}
function control(label: string) {const item = [...element.querySelectorAll('label')].find(item => item.textContent?.startsWith(label)); const input = item?.querySelector<HTMLInputElement | HTMLSelectElement>('input,select'); if (!input) throw Error('No field: ' + label); return input;}
async function change(label: string, value: string) {
  await act(async () => {const input = control(label); Object.getOwnPropertyDescriptor(input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', {bubbles: true})); await tick();});
}
async function submit() {await act(async () => {element.querySelector('form')!.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})); await tick();});}
function defaults(path: string) {
  if (path.includes('?')) return path.startsWith('/arm2/types?') ? [type] : path.startsWith('/arm2/counterparties?') ? [party] : path.startsWith('/arm2/organizations?') ? [{...party, id: 'ORG-1', name: 'Своя организация'}] : [];
  return path.includes('/vehicles/') ? vehicle : path.includes('/types/') ? type : party;
}
// Adapt the established legacy-list fixtures to the new read-page contract.
// Calls recorded by api still retain their real /search URL.
function mockAPI(implementation: (...args: Parameters<typeof api>) => Promise<unknown>) {
  vi.mocked(api).mockImplementation(async (...args) => {
    if (/\/veh\/[^/]+\/calibration$/.test(args[0])) return {vehicle_id: vehicle.id, volume_m3: null,
      confirmed_at: null, confirmed_by: null, document_id: null, letter_eligible: false,
      reasons: ['Объём не подтверждён'], customer: {state: null, request_id: null},
      work_readiness_notice: 'Полная готовность к работе не подтверждена'};
    const searching = args[0].includes('/search?');
    const result = await implementation(searching ? args[0].replace('/search?', '?') : args[0], ...args.slice(1) as [string?, unknown?, AbortSignal?]);
    if (!searching) return result;
    const items = Array.isArray(result) ? result : result ? [result] : [];
    return {items, total: items.length, matched: items.length, limit: 100, offset: 0};
  });
}

it.each([
  [() => true, 'Техника'],
  [(permission: string) => permission === 'arm2_type_view', 'Типы техники'],
  [(permission: string) => permission === 'arm2_cp_view', 'Контрагенты'],
])('default route selects the first permitted catalog', async (can, heading) => {
  mockAPI(async path => defaults(path));
  await mount('/arm2', can);
  expect(element.querySelector('h2')?.textContent).toBe(heading);
  expect([...element.querySelectorAll('[aria-disabled=true]')].every(item => item.tagName !== 'A')).toBe(true);
});

it('no catalog permission makes no API requests and exposes no editable deep link', async () => {
  await mount('/arm2/veh/VEH-000001', () => false);
  expect(element.textContent).toContain('Нет прав на просмотр');
  expect(api).not.toHaveBeenCalled();
  expect(element.querySelector('form')).toBeNull();
});

it('opens a vehicle card from the plate cell and keeps the section selected', async () => {
  mockAPI(async path => path.startsWith('/arm2/vehicles?') ? [vehicle] : defaults(path));
  await mount('/arm2/veh');
  const section = element.querySelector('nav[aria-label="Справочники АРМ 2.0"]');
  const tabs = element.querySelector('nav[aria-label="Подразделы техники"]');
  expect([...tabs!.querySelectorAll('a')].map(a => a.textContent)).toEqual(['Техника', 'Типы техники', 'Согласование с заказчиком']);
  expect(section?.querySelector('[aria-current]')?.getAttribute('aria-label')).toBe('Техника');
  const plate = element.querySelector<HTMLTableCellElement>('tr.arm2-catalog-row td:nth-child(2)')!;
  expect(plate.textContent).toContain('001');
  await act(async () => {plate.click(); await tick();});
  expect(api).toHaveBeenCalledWith('/arm2/vehicles/VEH-1', 'GET', undefined, expect.any(AbortSignal));
  expect(section?.querySelector('[aria-current]')?.getAttribute('aria-label')).toBe('Техника');
});

it('loads counterparty documents through the selected parent and keeps tabs scoped', async () => {
  mockAPI(async path => path.startsWith('/arm2/cp/CP-1/documents?') ? [
    {id:'DOCUMENT-1', name:'Карточка предприятия', kind_name:'Реквизиты', display_state:'Доступен'},
  ] : defaults(path));
  await mount('/arm2/cp/documents');
  const tabs = element.querySelector('nav[aria-label="Подразделы контрагентов"]');
  expect([...tabs!.querySelectorAll('a')].map(a => a.textContent)).toEqual(['Контрагенты', 'Анкеты', 'Документы']);
  expect(tabs?.querySelector('[aria-current="page"]')?.textContent).toBe('Документы');
  expect(api).not.toHaveBeenCalled();
  const picker = element.querySelector<HTMLInputElement>('input[role="combobox"]')!;
  await act(async () => {picker.focus(); await tick();});
  const option = [...element.querySelectorAll<HTMLButtonElement>('[role="option"]')].find(b => b.textContent?.startsWith('Партнёр'))!;
  await act(async () => {option.click(); await tick();});
  expect(element.querySelector('tr.arm2-catalog-row a')?.getAttribute('href')).toBe('/arm2/cp/CP-1/documents/DOCUMENT-1');
});

it('does not request documents or show their tab without document access', async () => {
  await mount('/arm2/cp/documents', permission => permission === 'arm2_cp_view');
  expect(element.querySelector('nav[aria-label="Подразделы контрагентов"] a[href="/arm2/cp/documents"]')).toBeNull();
  expect(element.textContent).toContain('Нет прав на просмотр документов контрагентов');
  expect(api).not.toHaveBeenCalled();
});

it('combines clickable counters with server search, preserving the query and resetting the page', async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith('/arm2/vehicles/search?') ? {
    items:[vehicle], total:130, matched:120, limit:100, offset:0,
    facets:{all_states:120, active:80, archived:40, no_owner:60, no_wialon:25},
  } : []);
  await mount('/arm2/veh?q=Машина&offset=100');
  const filters = element.querySelector('[aria-label="Быстрые фильтры"]')!;
  const click = async (label: string) => {
    const target = [...filters.querySelectorAll('button')].find(button => button.textContent?.startsWith(label))!;
    await act(async () => {target.click(); await tick();});
    expect(target.getAttribute('aria-pressed')).toBe('true');
  };
  await click('Архивные: 40');
  await click('Без операционного владельца: 60');
  await click('Без Wialon ID: 25');
  const calls = vi.mocked(api).mock.calls.filter(([path]) => path.startsWith('/arm2/vehicles/search?'));
  const query = new URLSearchParams(calls.at(-1)![0].split('?')[1]);
  expect(Object.fromEntries(query)).toMatchObject({offset:'0', q:'Машина', state:'Архивная', owner_kind:'NONE', wialon:'missing'});
});

it('server search resets pagination, shows exact counters, filtering and empty results', async () => {
  const records: RecordData[] = Array.from({length: 130}, (_, index) => ({...vehicle, id: `VEH-${index + 1}`, name: `Машина ${String(index + 1).padStart(3, '0')}`, accounting_state: index % 2 ? 'Архивная' : 'Действующая'}));
  vi.mocked(api).mockImplementation(async path => {
    const params = new URLSearchParams(path.split('?')[1]);
    const matches = records.filter(row => row.name.includes(params.get('q') || '') && (!params.get('state') || row.accounting_state === params.get('state')));
    return {items: matches.slice(Number(params.get('offset')), Number(params.get('offset')) + 100), matched: matches.length, total: records.length, without_owner: matches.filter(r=>!r.owner_cp_id&&!r.owner_org_id).length, without_wialon:matches.filter(r=>!String(r.wialon_unit_id||'').trim()).length, limit: 100, offset: Number(params.get('offset'))};
  });
  await mount('/arm2/veh', permission => permission === 'arm2_veh_view');
  expect(element.textContent).toContain('Найдено: 65');
  expect(control('Учётное состояние').value).toBe('Действующая');
  await change('Учётное состояние','ALL');
  expect(new URLSearchParams(vi.mocked(api).mock.calls.at(-1)![0].split('?')[1]).has('state')).toBe(false);
  expect(element.textContent).toContain('Без Wialon ID: 130');
  expect(element.textContent).toContain('Найдено: 130');
  await click('Следующая страница'); expect(element.textContent).toContain('Машина 130');
  await change('Поиск по рабочему имени', '130');
  expect(element.textContent).toContain('Найдено: 1'); expect(element.textContent).toContain('Страница 1');
  expect(new URLSearchParams(vi.mocked(api).mock.calls.at(-1)![0].split('?')[1]).get('offset')).toBe('0');
  await change('Поиск по рабочему имени', ''); await change('Учётное состояние', 'Архивная');
  expect(element.textContent).toContain('Найдено: 65');
  await change('Поиск по рабочему имени', 'missing');
  expect(element.textContent).toContain('По заданным условиям записей нет');
  expect(button('Следующая страница').disabled).toBe(true);
  expect([...element.querySelectorAll('a')].some(a => a.textContent === '+ Новая запись')).toBe(false);
});

it('an explicit ALL URL survives opening the list and never sends state=ALL to the API',async()=>{
  vi.mocked(api).mockResolvedValue({items:[],total:0,matched:0,limit:100,offset:0,without_owner:0,without_wialon:0});
  await mount('/arm2/veh?state=ALL',p=>p==='arm2_veh_view');
  expect(control('Учётное состояние').value).toBe('ALL');
  const url=vi.mocked(api).mock.calls.find(([p])=>p.includes('/vehicles/search?'))![0];
  expect(new URLSearchParams(url.split('?')[1]).has('state')).toBe(false);
  expect(element.textContent).toContain('Без операционного владельца: 0');
  expect(element.textContent).toContain('Без Wialon ID: 0');
});

it('CP creation explicitly selects a type and sends primary FIO without a competing name copy',async()=>{
  mockAPI(async(path,method,body)=>method==='POST'?{...emptyRecord('CP'),...body as RecordData,id:'CP-NEW',version:1}:defaults(path));
  await mount('/arm2/cp/new');
  expect(control('Тип контрагента').required).toBe(true);
  await change('Тип контрагента','Физическое лицо');
  await change('Рабочее имя','Свободное имя');
  await change('Фамилия','Тестов');await change('Имя','Иван');
  await submit();
  const body=vi.mocked(api).mock.calls.find(([,m])=>m==='POST')![2];
  expect(body).toMatchObject({type:'Физическое лицо',name:'Свободное имя',last_name:'Тестов',first_name:'Иван'});
  expect(body).not.toHaveProperty('full_name');expect(body).not.toHaveProperty('short_name');
  expect(element.textContent).not.toContain('Состояние описания');
  expect([...element.querySelectorAll('[aria-label="Разделы карточки"] button')].map(x=>x.textContent)).toEqual(['Основное','Контакты','Банковские счета','Документы','Техника']);
});

it('type transition presents clears, keeps the working name and needs a separate confirmation before one SAVE',async()=>{
  let stored={...party,legal_form:'ООО',full_name:'Старое полное',short_name:'Старое краткое',kpp:'001234567',ogrn:'0012345678901'};
  mockAPI(async(_path,method,body)=>{if(method==='PUT'){stored={...stored,...body as RecordData,version:3};return stored;}return stored;});
  await mount('/arm2/cp/CP-1');
  await change('Тип контрагента','Физическое лицо');await change('Организационно-правовая форма','ИП');
  expect(element.textContent).toContain('Старое полное');
  expect(vi.mocked(api).mock.calls.some(([,m])=>m==='PUT')).toBe(false);
  for(const field of ['Полное наименование','Краткое наименование','КПП','ОГРН / ОГРНИП'])await ariaSelect('Решение об очистке '+field,'clear');
  await act(async()=>{[...element.querySelectorAll('label')].find(l=>l.textContent?.startsWith('Подтверждаю изменение типа'))!.querySelector<HTMLInputElement>('input')!.click();await tick();});
  await submit();
  const writes=vi.mocked(api).mock.calls.filter(([,m])=>m==='PUT');expect(writes).toHaveLength(1);
  expect(writes[0][2]).toMatchObject({name:'Партнёр',type:'Физическое лицо',legal_form:'ИП',full_name:null,short_name:null,kpp:null,ogrn:null,confirm_identity_change:true,expected_version:2});
});

it('read-only CP has no editable clear controls and navigation puts TYPE below symmetric top-level tiles',async()=>{
  mockAPI(async()=>({...party,last_name:'Сохранённое неприменимое'}));
  await mount('/arm2/cp/CP-1',p=>p.endsWith('_view'));
  expect(element.querySelector<HTMLSelectElement>('select[aria-label="Решение об очистке Фамилия"]')!.disabled).toBe(true);
  expect(element.querySelector('.arm2-sections')!.textContent).not.toContain('Типы техники');
  expect([...element.querySelectorAll('.arm2-sections > *')].every(e=>e.classList.contains('arm2-section'))).toBe(true);
});

it.each(['cp', 'org'])('%s embedded vehicles use server owner filtering without automatic writes', async kind => {
  vi.mocked(api).mockImplementation(async path => path.includes('/vehicles/search?') ? {items: [vehicle], total: 130, matched: 1, limit: 100, offset: 0} : {...party, id: 'PARENT-1'});
  await mount(`/arm2/${kind}/PARENT-1`); await click('Техника');
  const request = vi.mocked(api).mock.calls.find(([path]) => path.startsWith('/arm2/vehicles/search?'))![0];
  const params = new URLSearchParams(request.split('?')[1]);
  expect(params.get('owner_kind')).toBe(kind.toUpperCase()); expect(params.get('owner_id')).toBe('PARENT-1');
  expect(element.querySelector('table')?.textContent).toContain('Машина');
  expect(element.textContent).toContain('Добавить технику');
  expect(vi.mocked(api).mock.calls.some(([, method]) => method === 'PUT' || method === 'POST')).toBe(false);
  expect([...element.querySelectorAll('button,a')].some(item => item.textContent === 'Добавить технику')).toBe(true);
});

it('TYPE has category and three requirements; documents expose no fake attach action', async () => {
  mockAPI(async path => defaults(path));
  await mount('/arm2/type/TYPE-1');
  expect(element.querySelector('.arm2-type-main')?.querySelectorAll('label')).toHaveLength(2);
  expect(control('Тип государственного номера').querySelectorAll('option')).toHaveLength(3);
  expect(control('СТС').querySelectorAll('option')).toHaveLength(1);
  expect(element.querySelector('[aria-label="Разделы карточки"]')?.textContent).not.toContain('Требования к технике');
  await follow('Техника'); await follow('+ Новая запись'); await click('Документы');
  expect(element.textContent).toContain('Сначала сохраните родительскую карточку'); expect(element.querySelector('input[type=file]')).toBeNull();
  expect([...element.querySelectorAll('button')].some(item => item.textContent === 'Сохранить')).toBe(false);
});

it('Enter in the read-only owned-vehicle search cannot save an unsaved parent draft', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('/vehicles/search?') ? {items: [], total: 0, matched: 0, limit: 100, offset: 0} : {...party});
  await mount('/arm2/cp/CP-1');
  await change('Рабочее имя', 'Несохранённый черновик');
  await click('Техника'); await submit();
  expect(vi.mocked(api).mock.calls.some(([, method]) => method === 'PUT' || method === 'POST')).toBe(false);
  await click('Основное'); expect(control('Рабочее имя').value).toBe('Несохранённый черновик');
});

it('after saving a different TYPE only the new persisted REVIEW link can be retained', async () => {
  let saved = {...vehicle};
  const other = {...type, id: 'TYPE-2', name: 'Другой тип', status: 'CONFIRMED'};
  mockAPI(async (path, method, body) => {
    if (method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; other.status = 'REVIEW'; return saved;}
    if (path.startsWith('/arm2/types?')) return [type, other];
    if (path === '/arm2/types/TYPE-2') return other;
    if (path === '/arm2/types/TYPE-1') return type;
    return path.includes('/vehicles/') ? saved : defaults(path);
  });
  await mount('/arm2/veh/VEH-1');
  await choose('Тип техники', 'Другой тип'); await submit();
  await act(async () => {control('Тип техники').focus(); control('Тип техники').click(); await tick();});
  const options = [...element.querySelectorAll<HTMLButtonElement>('[role=option]')];
  expect(options.find(option => option.textContent?.startsWith('Самосвал'))?.disabled).toBe(true);
  expect(options.find(option => option.textContent?.startsWith('Другой тип'))?.disabled).toBe(false);
  expect(saved.type_id).toBe('TYPE-2');
});
async function choose(label: string, text: string) {
  await act(async () => {control(label).focus(); control(label).click(); await tick();});
  await act(async () => {const option = [...element.querySelectorAll<HTMLButtonElement>('[role=option]')].find(item => item.textContent?.startsWith(text)); if (!option) throw Error('No option: ' + text); option.click(); await tick();});
}
it('requests history only by button, renders event differences without related objects', async () => {
  mockAPI(async path => path.startsWith('/arm2/history/events') ? {changes: {requires_calibration: {before: null, after: false}}} : path.startsWith('/arm2/history/') ? [{event_id: 1, occurred_at: '2026-11-11T04:00:00Z', actor_kind: 'HUMAN', actor_id: '1', source: 'arm2-api', version_before: 3, version_after: 4, operation: 'UPDATE'}] : type);
  await mount('/arm2/type/TYPE-1');
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/history/'))).toBe(false);
  await click('История'); expect(api).toHaveBeenCalledWith('/arm2/history/TYPE/TYPE-1?limit=100&offset=0', 'GET', undefined, expect.any(AbortSignal));
  await click('Открыть событие UPDATE'); expect(element.textContent).toContain('Не заполнено'); expect(element.textContent).toContain('Нет');
});
it('creates REVIEW TYPE then completes requirements using false, reopens server version', async () => {
  let saved: RecordData = {...type};
  mockAPI(async (path, method, body) => {
    if (method === 'POST' || method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; saved.status = saved.plate_category != null && saved.requires_sts === true && saved.requires_calibration != null && saved.requires_wialon != null ? 'CONFIRMED' : 'REVIEW'; return saved;}
    return path.includes('?') ? [saved] : saved;
  });
  await mount('/arm2/type/new'); await change('Рабочее имя', 'Самосвал'); await submit();
  expect(element.textContent).toContain('REVIEW');
  await click('Основное');
  expect([...control('СТС').querySelectorAll('option')].map(option => option.textContent)).not.toContain('Не требуется');
  await change('Тип государственного номера', 'Автомобильный'); await change('Тарировка', 'false'); await change('Wialon', 'true'); await submit();
  expect(saved.requires_calibration).toBe(false); expect(saved.status).toBe('CONFIRMED');
  await follow('← К списку'); await follow('Самосвал'); expect(element.textContent).toContain('CONFIRMED');
  expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2]).toMatchObject({expected_version: 5, plate_category: 'Автомобильный', requires_sts: true, requires_calibration: false});
});
it('VEH keeps existing REVIEW TYPE, disables new REVIEW and switches CP to ORG then clears owner', async () => {
  vi.spyOn(window,'confirm').mockReturnValue(true);
  let saved: RecordData = {...vehicle};
  mockAPI(async (path, method, body) => {
    if (method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; return saved;}
    if (path.startsWith('/arm2/types?')) return [type, {...type, id: 'TYPE-2', name: 'Незавершённый', requires_sts: null}];
    return path.includes('/vehicles/') ? saved : defaults(path);
  });
  await mount('/arm2/veh/VEH-1');
  await act(async () => {control('Тип техники').focus(); await tick();});
  const options = [...element.querySelectorAll<HTMLButtonElement>('[role=option]')];
  expect(options.find(option => option.textContent?.startsWith('Самосвал'))?.disabled).toBe(false); expect(options.find(option => option.textContent?.startsWith('Незавершённый'))?.disabled).toBe(true);
  await choose('Операционный владелец', 'Организация · Своя организация'); await submit();
  expect(saved).toMatchObject({owner_cp_id: null, owner_org_id: 'ORG-1', model: 'Модель', type_id: 'TYPE-1'});
  await change('Рабочее имя', 'Новое имя'); await submit(); expect(saved.owner_org_id).toBe('ORG-1');
  await choose('Операционный владелец', 'Без владельца'); await submit(); expect(saved).toMatchObject({owner_cp_id: null, owner_org_id: null, version: 6});
  expect(element.textContent).not.toContain('Состояние по последнему ответу сервера');
});
it.each(['cp', 'org'])('%s preserves untouched requisites and string bank accounts, then reopens', async kind => {
  let saved: RecordData = {...party, id: kind === 'cp' ? 'CP-1' : 'ORG-1'};
  mockAPI(async (path, method, body) => {if (method === 'PUT') {saved = {...saved, ...body as RecordData, version: saved.version + 1}; return saved;} return path.includes('?') ? [saved] : saved;});
  await mount(`/arm2/${kind}/${saved.id}`); await click('Банковские счета');
  await change('БИК', '000000001'); await change('Расчётный счёт', '00000000000000000001'); await change('Корреспондентский счёт', '00000000000000000002'); await submit();
  const body = vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2];
  expect(body).toEqual({name: 'Партнёр', expected_version: 2, bik: '000000001', settlement_account: '00000000000000000001', correspondent_account: '00000000000000000002'});
  await follow('← К списку'); await follow('Партнёр'); await click('Основное'); expect(control('Полное наименование').value).toBe('Полное имя');
  await click('Банковские счета'); expect(control('БИК').value).toBe('000000001');
  await click('Документы'); expect(element.textContent).toContain('Название карточки'); expect(element.textContent).not.toContain('Загрузить документ');
});
it.each(['cp', 'org'])('%s representative edit sends the complete record and retains user link', async kind => {
  const rep = {...emptyRecord('REP'), id: 'REP-1', name: 'Представитель', version: 9, phone: '123', user_id: 8, last_name: 'Старое', first_name: 'Иван', middle_name: 'Сохранено', state: 'Архивный', authority_basis: 'Доверенность'};
  mockAPI(async (path, method, body) => method === 'PUT' ? {...rep, ...body as RecordData, version: 10} : path.includes('/representatives/') ? rep : party);
  await mount(`/arm2/${kind}/PARENT-1/representatives/REP-1`); await change('Должность', 'Директор'); await submit();
  expect(element.querySelectorAll('label').length).toBeGreaterThan(0);
  expect([...element.querySelectorAll('label')].some(label => label.textContent?.startsWith('Фамилия'))).toBe(false);
  expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2]).toMatchObject({expected_version: 9, user_id: 8, phone: '123', last_name: 'Старое', first_name: 'Иван', position: 'Директор', middle_name: 'Сохранено', state: 'Архивный', authority_basis: 'Доверенность'});
});
it('VIEW-only can navigate tabs and representatives but cannot save or read VEH without permission', async () => {
  mockAPI(async path => path.includes('/representatives?') ? [{...emptyRecord('REP'), id: 'REP-1', name: 'Контакт'}] : party);
  await mount('/arm2/cp/CP-1', permission => permission === 'arm2_cp_view');
  expect(control('Рабочее имя').disabled).toBe(true); expect([...element.querySelectorAll('button')].some(item => item.textContent === 'Сохранить')).toBe(false);
  await click('Контакты'); expect(element.textContent).toContain('Контакт'); expect(control('Основной подписант').disabled).toBe(true);
  await click('Техника'); expect(element.textContent).toContain('Нет права просмотра техники');
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/vehicles'))).toBe(false);
});
it.each([409, 422, 500])('HTTP %s retains draft without reporting success or automatically overwriting', async status => {
  mockAPI(async (path, method) => {if (method === 'PUT') throw new ApiError(status, 'requires_sts must be True when it is specified'); return type;});
  await mount('/arm2/type/TYPE-1'); await change('Рабочее имя', 'Мой черновик'); await submit();
  expect(control('Рабочее имя').value).toBe('Мой черновик'); expect(element.querySelector('[role=alert]')).not.toBeNull(); expect(element.textContent).not.toContain('Сохранено сервером');
  expect(vi.mocked(api).mock.calls.filter(([, method]) => method === 'PUT')).toHaveLength(1);
  if (status === 409) {await click('Сравнить с сервером'); expect(control('Рабочее имя').value).toBe('Мой черновик'); expect(element.textContent).toContain('Версия на сервере');}
});
it('server 403 disables writes after a failed save', async () => {
  mockAPI(async (path, method) => {if (method === 'PUT') throw new ApiError(403, 'denied'); return type;});
  await mount('/arm2/type/TYPE-1'); await submit(); expect(control('Рабочее имя').disabled).toBe(true); expect(element.textContent).toContain('Только просмотр');
});
it('list pagination fetches next API page and search scope stays explicit', async () => {
  vi.mocked(api).mockImplementation(async path => {
    const p = new URLSearchParams(path.split('?')[1]);
    const items = p.get('q') || p.get('status') || p.get('offset') === '100' ? [{...type, id: 'TYPE-101', name: 'На второй странице'}] : Array.from({length: 100}, (_, i) => ({...type, id: String(i)}));
    return {items, total: 101, matched: p.get('q') || p.get('status') ? 1 : 101, limit: 100, offset: Number(p.get('offset'))};
  });
  await mount('/arm2/type'); await click('Следующая страница'); expect(element.textContent).toContain('На второй странице'); expect(element.textContent).not.toContain('только к текущей странице');
  expect(api).toHaveBeenCalledWith('/arm2/types/search?q=&limit=100&offset=100', 'GET', undefined, expect.any(AbortSignal));
  await change('Поиск по имени', 'На второй'); await change('Состояние', 'REVIEW');
  expect(element.textContent).toContain('Найдено: 1');
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining('status=REVIEW'), 'GET', undefined, expect.any(AbortSignal));
});

it.each(['cp', 'org'])('%s create without completeness status → create representative → select scoped primary signer', async kind => {
  const parentId = kind === 'cp' ? 'CP-NEW' : 'ORG-NEW';
  const path = kind === 'cp' ? '/arm2/counterparties' : '/arm2/organizations';
  let saved: RecordData = {...emptyRecord(kind === 'cp' ? 'CP' : 'ORG'), id: parentId, version: 1};
  const reps: RecordData[] = [];
  mockAPI(async (url, method, body) => {
    if (url.includes('/representatives')) {
      if (method === 'POST') {const rep = {...emptyRecord('REP'), ...body as RecordData, id: 'REP-NEW', parent_id: parentId, version: 1}; reps.push(rep); return rep;}
      return url.includes('?') ? reps : reps[0];
    }
    if (method === 'POST' || method === 'PUT') {saved = {...saved, ...body as RecordData, version: method === 'POST' ? 1 : saved.version + 1}; return saved;}
    return url.includes('?') ? [saved] : saved;
  });
  await mount(`/arm2/${kind}/new`); if(kind==='cp')await change('Тип контрагента','Юридическое лицо'); await change('Рабочее имя', 'Новый партнёр'); await submit(); expect(element.textContent).not.toContain('Состояние описания'); expect(saved).not.toHaveProperty('status');
  await change('Юридический адрес', 'Адрес'); await click('Основное'); await change('Полное наименование', 'Полное имя'); await submit(); expect(saved.full_name).toBe('Полное имя'); expect(saved).not.toHaveProperty('status');
  await click('Контакты'); await follow('Добавить представителя'); await change('ФИО / Имя', 'Иван Петров'); await change('Телефон представителя', '123'); await submit();
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
  mockAPI(async path => path.includes('/history/') ? path.includes('offset=100') ? [] : Array.from({length: 100}, (_, i) => ({...event, event_id: i + 1})) : type);
  await mount('/arm2/type/TYPE-1'); await click('История'); await click('Следующая страница');
  expect(api).toHaveBeenCalledWith('/arm2/history/TYPE/TYPE-1?limit=100&offset=100', 'GET', undefined, expect.any(AbortSignal));
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/history/events/'))).toBe(false);
});

it('late create response does not redirect the operator after leaving the card', async () => {
  let finish!: (record: RecordData) => void;
  const pending = new Promise<RecordData>(resolve => {finish = resolve;});
  mockAPI(async (path, method) => method === 'POST' ? pending : path.includes('?') ? [] : type);
  await mount('/arm2/type/new'); await change('Рабочее имя', 'Самосвал'); await submit();
  await follow('Контрагенты'); await act(async () => {finish(type); await tick();});
  expect(element.querySelector('h2')?.textContent).toBe('Контрагенты');
  expect(element.querySelector('form')).toBeNull();
});

it.each(['TYPE', 'CP', 'ORG', 'VEH', 'CPREP', 'ORGREP'] as const)('%s accepts a fresh version only after confirmed reload following 409', async kind => {
  const rep = kind === 'CPREP' || kind === 'ORGREP';
  const catalog = kind === 'TYPE' ? 'types' : kind === 'VEH' ? 'vehicles' : kind === 'CP' || kind === 'CPREP' ? 'counterparties' : 'organizations';
  const route = kind === 'CPREP' ? 'cp' : kind === 'ORGREP' ? 'org' : kind.toLowerCase();
  const resource = `/arm2/${catalog}/${rep ? 'PARENT-1/representatives/' : ''}OBJECT-1`;
  const cardRoute = `/arm2/${route}/${rep ? 'PARENT-1/representatives/' : ''}OBJECT-1`;
  let server: RecordData = {...(rep ? emptyRecord('REP') : kind === 'TYPE' ? type : kind === 'VEH' ? vehicle : party), id: 'OBJECT-1', name: 'Исходное имя', version: 3};
  let conflict = true;
  mockAPI(async (path, method, body) => {
    if (path.startsWith('/arm2/history/')) return [];
    if (path !== resource) return path.includes('?') ? [] : party;
    if (method === 'PUT') {
      if (conflict) {conflict = false; server = {...server, name: 'Актуальное имя сервера', version: 9}; throw new ApiError(409, 'version conflict');}
      expect(body).toMatchObject({expected_version: 9, name: 'Актуальное имя сервера'});
      server = {...server, ...body as RecordData, version: 10}; return server;
    }
    return {...server};
  });
  const nameLabel = rep ? 'ФИО / Имя' : 'Рабочее имя';
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await mount(cardRoute); await change(nameLabel, 'Мой несохранённый черновик'); await submit();
  expect(element.querySelector('[role=alert]')).not.toBeNull(); expect(element.textContent).not.toContain('Сохранено сервером');
  await click('Сравнить с сервером');
  expect(control(nameLabel).value).toBe('Мой несохранённый черновик');
  expect(element.querySelector('[aria-label="Версия на сервере"]')?.textContent).toContain('Актуальное имя сервера');
  const gets = () => vi.mocked(api).mock.calls.filter(([path, method]) => path === resource && method === 'GET').length;
  const beforeCancel = gets(); await click('Перезагрузить карточку');
  expect(confirm).toHaveBeenCalledTimes(1); expect(gets()).toBe(beforeCancel);
  expect(control(nameLabel).value).toBe('Мой несохранённый черновик'); expect(element.querySelector('[role=alert]')).not.toBeNull();
  confirm.mockReturnValue(true); await click('Перезагрузить карточку');
  expect(gets()).toBe(beforeCancel + 1); expect(control(nameLabel).value).toBe('Актуальное имя сервера');
  expect(element.querySelector('[role=alert]')).toBeNull(); expect(element.querySelector('[aria-label="Версия на сервере"]')).toBeNull();
  expect(element.textContent).not.toContain('Сохранено сервером');
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/history/'))).toBe(false);
  await submit(); expect(server.version).toBe(10); expect(element.textContent).toContain('Сохранено сервером');
  await click('История'); expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith(`/arm2/history/${kind}/OBJECT-1?`))).toBe(true);
});

it.each(['cp', 'org'])('%s representative chooses existing integer user A → B → null and reopens after each save', async kind => {
  let stored: RecordData = {...emptyRecord('REP'), id: 'REP-1', name: 'Контакт', version: 1};
  const changes: {before: unknown; after: unknown}[] = [];
  const accounts = [{id: 11, name: 'Пользователь А'}, {id: 22, name: 'Пользователь Б'}];
  mockAPI(async (path, method, body) => {
    if (path === '/users') return accounts;
    if (path.includes('/representatives/REP-1')) {
      if (method === 'PUT') {const next = body as RecordData; expect(next.user_id === null || typeof next.user_id === 'number').toBe(true); changes.push({before: stored.user_id, after: next.user_id}); stored = {...stored, ...next, version: stored.version + 1};}
      return {...stored};
    }
    return party;
  });
  const route = `/arm2/${kind}/PARENT-1/representatives/REP-1`;
  await mount(route, () => true, true);
  for (const value of [11, 22, null]) {
    await change('Связанная учётная запись', value == null ? '' : String(value)); await submit();
    expect(stored.user_id).toBe(value);
    await follow('← К родительской карточке');
    await act(async () => {root.render(<MemoryRouter key={String(value)} initialEntries={[route]}><Routes><Route path="/arm2/*" element={<Workspace can={() => true} userLookupAllowed/>}/></Routes></MemoryRouter>); await tick();});
    expect(control('Связанная учётная запись').value).toBe(value == null ? '' : String(value));
  }
  expect(changes).toEqual([{before: null, after: 11}, {before: 11, after: 22}, {before: 22, after: null}]); expect(stored.version).toBe(4);
  expect(vi.mocked(api).mock.calls.some(([, method]) => method === 'PATCH' || method === 'DELETE')).toBe(false);
  expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/users').every(([, method]) => method === 'GET')).toBe(true);
});

it.each(['no-directory-right', 'directory-403', 'view-only'])('user lookup %s preserves current link and prevents assignment', async mode => {
  const rep = {...emptyRecord('REP'), id: 'REP-1', name: 'Контакт', version: 1, user_id: 11};
  mockAPI(async (path, method, body) => {
    if (path === '/users') throw new ApiError(403, 'denied');
    return path.includes('/representatives/') ? method === 'PUT' ? {...rep, ...body as RecordData, version: 2} : rep : party;
  });
  await mount('/arm2/cp/PARENT-1/representatives/REP-1', permission => mode !== 'view-only' || permission.endsWith('_view'), mode !== 'no-directory-right');
  expect(control('Связанная учётная запись').closest('fieldset')?.disabled).toBe(true);
  expect(control('Связанная учётная запись').value).toBe('11');
  if (mode !== 'directory-403') expect(vi.mocked(api).mock.calls.some(([path]) => path === '/users')).toBe(false);
  if (mode !== 'view-only') {await change('Должность', 'Директор'); await submit(); expect(vi.mocked(api).mock.calls.find(([, method]) => method === 'PUT')?.[2]).toMatchObject({user_id: 11});}
});

it('user assignment conflict preserves selection until confirmed reload accepts server link/version', async () => {
  let stored = {...emptyRecord('REP'), id: 'REP-1', name: 'Контакт', version: 1, user_id: 11};
  let conflict = true;
  mockAPI(async (path, method, body) => {
    if (path === '/users') return [{id: 11, name: 'А'}, {id: 22, name: 'Б'}];
    if (!path.includes('/representatives/')) return party;
    if (method === 'PUT') {
      if (conflict) {conflict = false; stored = {...stored, version: 8}; throw new ApiError(409, 'conflict');}
      expect(body).toMatchObject({expected_version: 8, user_id: 22}); stored = {...stored, ...body as RecordData, user_id: 22, version: 9};
    }
    return {...stored};
  });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await mount('/arm2/cp/PARENT-1/representatives/REP-1', () => true, true); await change('Связанная учётная запись', '22'); await submit();
  await click('Сравнить с сервером'); expect(control('Связанная учётная запись').value).toBe('22');
  await click('Перезагрузить карточку'); expect(control('Связанная учётная запись').value).toBe('22');
  confirm.mockReturnValue(true); await click('Перезагрузить карточку'); expect(control('Связанная учётная запись').value).toBe('11');
  await change('Связанная учётная запись', '22'); await submit(); expect(stored).toMatchObject({user_id: 22, version: 9});
});

it.each([403, 422])('user link save HTTP %s keeps the selected draft without a false success', async status => {
  const stored = {...emptyRecord('REP'), id: 'REP-1', name: 'Контакт', version: 1, user_id: 11};
  mockAPI(async (path, method, body) => {
    if (path === '/users') return [{id: 11, name: 'А'}, {id: 22, name: 'Б'}];
    if (method === 'PUT') {expect(body).toMatchObject({user_id: 22, expected_version: 1}); throw new ApiError(status, 'Representative user_id does not exist');}
    return path.includes('/representatives/') ? {...stored} : party;
  });
  await mount('/arm2/org/PARENT-1/representatives/REP-1', () => true, true);
  await change('Связанная учётная запись', '22'); await submit();
  expect(control('Связанная учётная запись').value).toBe('22'); expect(stored.user_id).toBe(11);
  expect(element.querySelector('[role=alert]')).not.toBeNull(); expect(element.textContent).not.toContain('Сохранено сервером');
  if (status === 403) expect(control('Связанная учётная запись').disabled).toBe(true);
});
