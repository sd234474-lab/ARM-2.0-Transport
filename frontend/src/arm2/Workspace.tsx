import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { Link, NavLink, Navigate, Route, Routes, UNSAFE_NavigationContext, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ApiError } from '../api';
import { catalogs, detail, list, ownerOf, sameOwner, save, searchPage, setOperationalOwner } from './api';
import type { Kind, OwnerIdentity, RecordData, Scalar, Vehicle } from './api';
import SearchCatalog, { plateLabel, VehicleTable } from './SearchCatalog';
import BulkVehicles from './BulkVehicles';
import Questionnaire, {QuestionnaireExport} from './Questionnaire';
import DocumentKinds from './DocumentKinds';
import Documents, { DocumentRoute } from './Documents';
import Representatives from './Representatives';
import {PartyFields,person,legal,rawParty} from './PartyFields';
import { OwnerPicker, TypePicker } from './Selectors';
import { emptyRecord, isParty, labels, missingRequirements, payload, repFields, requirementLabel, requirements } from './model';
import { display, ErrorMessage, Field, History, Pagination, SaveForm, Tabs, Unavailable, useAlive, useLoad } from './shared';

export type Can = (permission: string) => boolean;
const kinds: Kind[] = ['VEH', 'TYPE', 'ORG', 'CP'];
const UserLookupAllowed = createContext(false);
const OwnerDraftContext = createContext<Map<string,OwnerParked>|null>(null);
const link = (kind: Kind, id?: string) => `/arm2/${kind.toLowerCase()}${id ? '/' + encodeURIComponent(id) : ''}`;
export default function Workspace({can, userLookupAllowed = false}: {can: Can; userLookupAllowed?: boolean}) {
  const location=useLocation();
  const [ownerDrafts]=useState(()=>new Map<string,OwnerParked>());
  const docView = can('arm2_doc_kind_view');
  const docEdit = docView && can('arm2_doc_kind_edit');
  const visible = kinds.filter(kind => can(catalogs[kind].permission + '_view'));
  return <OwnerDraftContext.Provider value={ownerDrafts}><UserLookupAllowed.Provider value={userLookupAllowed}><section className="panel arm2"><h1>АРМ 2.0</h1><nav className="arm2-sections" aria-label="Справочники АРМ 2.0">{(visible.includes('VEH')||visible.includes('TYPE'))&&<NavLink className="arm2-section" to={link(visible.includes('VEH')?'VEH':'TYPE')}>Техника</NavLink>}{visible.filter(kind=>kind==='ORG'||kind==='CP').map(kind=><NavLink className="arm2-section" key={kind} to={link(kind)}>{catalogs[kind].label}</NavLink>)}{docView&&<NavLink className="arm2-section" to="/arm2/document-kinds">Типы документов</NavLink>}{['Договоры','Территории','Нормативы'].map(name=><span className="arm2-section" aria-disabled="true" key={name}>{name}<small>Пока не реализовано</small></span>)}</nav><nav className="arm2-subsections" aria-label="Подразделы и путь">{(location.pathname.includes('/arm2/veh')||location.pathname.includes('/arm2/type'))&&<>{can('arm2_veh_view')&&<NavLink to="/arm2/veh">Техника</NavLink>}{can('arm2_type_view')&&<NavLink to="/arm2/type">Типы техники</NavLink>}<span aria-disabled="true">Согласование с заказчиком — пока не реализовано</span></>}{can('arm2_cp_view')&&<Link to="/arm2/questionnaires">Анкеты контрагентов</Link>}</nav><Routes>
    {visible.map(kind => <Route key={kind} path={kind.toLowerCase()}><Route index element={<Catalog key={kind} kind={kind} can={can}/>}/><Route path=":id" element={<CardRoute kind={kind} can={can}/>}/>{kind !== 'TYPE' && <Route path=":parentId/documents/:id" element={<DocumentRoute parentType={kind} can={can}/>}/>}{isParty(kind) && <Route path=":parentId/representatives/:id" element={<RepRoute kind={kind} can={can}/>}/>}</Route>)}
    {can('arm2_veh_view') && can('arm2_veh_edit') && <Route path="veh/bulk" element={<BulkVehicles admin={userLookupAllowed}/>}/>}
    {can('arm2_cp_view') && <Route path="questionnaires" element={<Questionnaire can={can}/>}/>}
    {docView && <Route path="document-kinds/*" element={<DocumentKinds editable={docEdit}/>}/>}
    <Route path="*" element={visible[0] ? <Navigate to={link(visible[0])} replace/> : docView ? <Navigate to="/arm2/document-kinds" replace/> : <p>Нет прав на просмотр справочников АРМ 2.0</p>}/>
  </Routes></section></UserLookupAllowed.Provider></OwnerDraftContext.Provider>;
}
function Catalog({kind, can}: {kind: Kind; can: Can}) {
  return kind === 'TYPE' ? <TypeCatalog kind={kind} can={can}/> : <SearchCatalog kind={kind} can={can}/>;
}
function TypeCatalog({kind, can}: {kind: Kind; can: Can}) {
  const [params, setParams] = useSearchParams();
  const raw = Number(params.get('offset') || 0), offset = Number.isSafeInteger(raw) && raw >= 0 ? raw : 0;
  const search = params.get('q') || '', status = params.get('status') || '';
  const change = (key: string, value: string) => {const next = new URLSearchParams(params); next.delete('offset'); value ? next.set(key, value) : next.delete(key); setParams(next, {replace: true});};
  const query = new URLSearchParams({q: search, limit: '100', offset: String(offset), ...(status ? {status} : {})}).toString();
  const result = useLoad(signal => searchPage(catalogs.TYPE.path, new URLSearchParams(query), signal), [query]);
  return <><header className="arm2-actions"><h2>{catalogs[kind].label}</h2>{can('arm2_type_edit') && <Link className="arm2-create" to={link(kind, 'new')}>+ Новая запись</Link>}</header>
    <div className="arm2-grid"><label>Поиск по имени<input value={search} onChange={e => change('q', e.target.value)}/></label><label>Состояние<select value={status} onChange={e => change('status', e.target.value)}><option value="">Все</option><option value="CONFIRMED">Описание заполнено</option><option value="REVIEW">Описание не завершено</option></select></label></div>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем список…</p>}{result.data && <><p>Найдено: {result.data.matched}</p><div className="arm2-table"><table><thead><tr><th>Рабочее имя</th><th>Тип государственного номера</th>{requirements.map(key => <th key={key}>{labels[key]}</th>)}<th>Состояние</th></tr></thead><tbody>{result.data.items.map(row => <tr key={row.id}><td><Link to={link(kind, row.id)}>{row.name}</Link></td><td>{display(row.plate_category)}</td>{requirements.map(key => <td key={key}>{requirementLabel(row[key])}</td>)}<td>{row.status === 'CONFIRMED' ? 'Описание заполнено' : 'Описание не завершено'}</td></tr>)}</tbody></table></div><Pagination offset={offset} count={offset + 100 < result.data.matched ? 100 : 0} change={next => {const p = new URLSearchParams(params); p.set('offset', String(next)); setParams(p);}}/></>}</>;
}
function CardRoute({kind, can}: {kind: Kind; can: Can}) {
  const {id = 'new'} = useParams(); return <CardLoader key={`${kind}:${id}`} kind={kind} id={id} can={can}/>;
}
function CardLoader({kind, id, can}: {kind: Kind; id: string; can: Can}) {
  const [reloadId, setReloadId] = useState(0);
  const result = useLoad(async signal => ({
    record: id === 'new' ? emptyRecord(kind) : await detail<RecordData>(`${catalogs[kind].path}/${encodeURIComponent(id)}`, signal),
    reloadId,
  }), [kind, id, reloadId]);
  // Accept a reset only with the successful GET for that confirmed reload.
  // Remounting also clears SaveForm errors/comparison/success and selector state.
  return <><Link to={link(kind)}>← К списку</Link><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Открываем карточку…</p>}{result.data && <Card key={result.data.reloadId} kind={kind} initial={result.data.record} can={can} reload={() => setReloadId(value => value + 1)}/>}</>;
}
function Card({kind, initial, can, reload}: {kind: Kind; initial: RecordData; can: Can; reload: () => void}) {
  const ownerDrafts=useContext(OwnerDraftContext)!;
  const alive = useAlive();
  const [tabParams, setTabParams] = useSearchParams();
  const key=kind+':'+initial.id,[parked]=useState(()=>ownerDrafts.get(key));
  const [original, setOriginal] = useState(parked?.original||initial), [draft, setDraft] = useState(parked?.draft||initial), [tab, setTab] = useState(parked?.tab||(tabParams.get('tab') === 'documents' && kind !== 'TYPE' ? 'Документы' : tabParams.get('tab')==='vehicles'&&isParty(kind)?'Техника':'Основное')), [history, setHistory] = useState(false);
  const [ownerChoice,setOwnerChoice]=useState<OwnerChoice|undefined>(parked?.choice),[ownerSelecting,setOwnerSelecting]=useState(parked?.selecting||false),[ownerQuery,setOwnerQuery]=useState(parked?.query||''),[ownerOffset,setOwnerOffset]=useState(parked?.offset||0);
  const [ownerRefresh,setOwnerRefresh]=useState(0);
  useEffect(()=>{ownerDrafts.delete(key);},[key]);
  const discardOwnerDraft=()=>{ownerDrafts.delete(key);if(alive.current){setOwnerChoice(undefined);setOwnerSelecting(false);setDraft(original);}};
  const ownerGuard=useOwnerDraftGuard(!!ownerChoice||ownerSelecting||(kind==='VEH'&&!sameOwner(ownerOf(draft),ownerOf(original))),()=>ownerDrafts.set(key,{original,draft,tab,choice:ownerChoice,selecting:ownerSelecting,query:ownerQuery,offset:ownerOffset}),discardOwnerDraft);
  const navigate = useNavigate();
  const ownerChanged=async(cp:string|null,org:string|null)=>{
    const previous=ownerOf(original),target:OwnerIdentity=cp?{kind:'CP',id:cp}:org?{kind:'ORG',id:org}:{kind:null,id:null};
    if(!previous.kind||sameOwner(previous,target))return true;
    const [before,after]=await Promise.all([ownerLabel(previous,can),ownerLabel(target,can)]);
    return alive.current&&window.confirm(`Изменить операционного владельца: ${before} → ${after}? Это не изменение юридической собственности. Запись произойдёт только после «Сохранить».`);
  };
  const [identityConfirmed,setIdentityConfirmed]=useState(false);
  const change = (field: string, value: Scalar) => {if(['type','legal_form'].includes(field))setIdentityConfirmed(false);setDraft(current => ({...current, ...(kind==='CP'&&field==='type'&&current.type===person&&value===legal?{full_name:rawParty(current,'full_name')??null,short_name:rawParty(current,'short_name')??null}:{}), [field]: value}));};
  const fields = (keys: string[]) => <div className="arm2-grid">{keys.map(field => <Field key={field} field={field} value={draft[field]} change={value => change(field, value)} required={field === 'name' || field === 'plate_number'}/>)}</div>;
  const tabs = isParty(kind) ? ['Основное', 'Контакты', 'Банковские счета', 'Документы', 'Техника'] : kind === 'TYPE' ? ['Основное', 'Виды работ'] : ['Основное', 'Документы'];
  return <article><header className="arm2-actions"><h2>{catalogs[kind].singular}: {original.id ? original.name : 'создание'}</h2>{original.id&&<button type="button" aria-expanded={history} onClick={()=>setHistory(x=>!x)}>{history?'Закрыть историю':'История'}</button>}</header>{kind === 'TYPE' && <div className="arm2-card-status"><p role="status">Состояние описания: {original.id ? original.status === 'CONFIRMED' ? 'Описание заполнено (CONFIRMED)' : 'Описание не завершено (REVIEW)' : 'будет рассчитано при сохранении'}</p><details><summary>Условия полного описания</summary>{isParty(kind) ? <p>Нужны рабочее имя, полное наименование и юридический адрес. Заполненные реквизиты проверяются сервером. Не заполнено: {['name', 'full_name', 'legal_address'].filter(key => !draft[key]).map(key => labels[key]).join(', ') || 'основные поля заполнены'}.</p> : <p>Не заполнено: {missingRequirements(draft) || 'все требования заполнены'}. Тип REVIEW нельзя вновь назначить машине.</p>}</details></div>}
    <Tabs tabs={tabs} active={tab} setActive={value => {if(ownerGuard.current){if(!window.confirm('Несохранённые изменения связи или карточки будут потеряны. Перейти на другую вкладку?'))return;ownerGuard.current=false;discardOwnerDraft();}setTab(value); setTabParams(current => {const next = new URLSearchParams(current); if (value === 'Документы') next.set('tab', 'documents'); else if(value==='Техника')next.set('tab','vehicles');else next.delete('tab'); return next;}, {replace: true});}}/>
    {kind==='CP'&&original.id&&<QuestionnaireExport cpId={original.id}/>}
    <SaveForm resource={`${catalogs[kind].path}/${encodeURIComponent(original.id)}`} original={original} allowed={can(catalogs[kind].permission + '_edit')} showSubmit={!['Документы', 'Виды работ', 'Техника'].includes(tab)&&!(kind==='VEH'&&ownerChoice)} reload={()=>{ownerGuard.current=false;ownerDrafts.delete(key);reload();}} submit={async () => {
      const saved = await save(`${catalogs[kind].path}${original.id ? '/' + encodeURIComponent(original.id) : ''}`, {...payload(kind, draft, original),...(kind==='CP'&&original.id&&(draft.type!==original.type||draft.legal_form!==original.legal_form)?{confirm_identity_change:identityConfirmed}:{})}, !!original.id);
      if (!alive.current) return;
      ownerGuard.current=false;ownerDrafts.delete(key);
      setOriginal(saved); setDraft(saved); setIdentityConfirmed(false);
      if (!original.id) navigate(link(kind, saved.id), {replace: true});
    }}>{editable => <>
      {isParty(kind)&&JSON.stringify(draft)!==JSON.stringify(original)&&<button type="button" disabled={!editable} onClick={()=>{setDraft(original);setIdentityConfirmed(false);}}>Отменить изменения</button>}
      {tab === 'Основное' && kind === 'VEH' && <fieldset className="arm2-vehicle-fields" disabled={!!ownerChoice}><div className="arm2-grid arm2-vehicle-main">{['name', 'plate_number', 'model', 'wialon_unit_id'].map(field => <Field key={field} field={field} value={draft[field]} required={field === 'name' || field === 'plate_number'} change={value => change(field, value)}/>)}<Field field="accounting_state" value={draft.accounting_state} change={value => change('accounting_state', value)} options={['Действующая', 'Архивная'].map(value => ({value, label: value}))}/><VehicleSelectors draft={draft} change={change} can={can} editable={editable&&!ownerChoice} currentType={original.type_id} beforeOwnerChange={ownerChanged}/></div></fieldset>}
      {tab === 'Основное' && kind === 'TYPE' && <><div className="arm2-grid arm2-type-main"><Field field="name" value={draft.name} required change={value => change('name', value)}/><Field field="plate_category" value={draft.plate_category} change={value => change('plate_category', value || null)} options={[{value: '', label: 'Не заполнено'}, ...['Автомобильный', 'Самоходная техника'].map(value => ({value, label: value}))]}/></div><h3>Требования к технике этого типа</h3><div className="arm2-grid">{requirements.map(field => <Field key={field} field={field} value={draft[field] == null ? '' : String(draft[field])} change={value => change(field, value === 'true' ? true : value === 'false' ? false : null)} options={field === 'requires_sts' ? [...(draft.requires_sts == null ? [{value: '', label: 'Исторически не заполнено — выберите «Требуется»'}] : []), {value: 'true', label: 'Требуется'}] : [{value: '', label: 'Не заполнено'}, {value: 'true', label: 'Требуется'}, {value: 'false', label: 'Не требуется'}]}/>)}</div></>}
      {tab === 'Основное' && isParty(kind) && <PartyFields kind={kind as 'CP'|'ORG'} draft={draft} original={original} change={change} confirmed={identityConfirmed} confirm={setIdentityConfirmed} editable={editable}/>}
      {tab === 'Банковские счета' && <><p>Один актуальный комплект реквизитов</p>{fields(['bank_name', 'bik', 'settlement_account', 'correspondent_account'])}</>}
      {tab === 'Виды работ' && <Unavailable/>}
      {tab === 'Контакты' && (original.id ? <Representatives kind={kind} parent={original} signer={draft.primary_signer_id} changeSigner={value => change('primary_signer_id', value)} editable={editable}/> : <p>Сначала сохраните карточку, затем добавьте представителей и подписанта.</p>)}
      {tab === 'Техника' && (original.id ? <OwnedVehicles kind={kind} id={original.id} name={original.name} can={can} active={editable} choice={ownerChoice} setChoice={setOwnerChoice} selecting={ownerSelecting} setSelecting={setOwnerSelecting} query={ownerQuery} setQuery={setOwnerQuery} offset={ownerOffset} setOffset={setOwnerOffset} refresh={ownerRefresh} completed={()=>{ownerGuard.current=false;ownerDrafts.delete(key);setOwnerChoice(undefined);setOwnerSelecting(false);setOwnerRefresh(value=>value+1);}}/> : <p>Сначала сохраните карточку.</p>)}
    </>}</SaveForm>
    {kind==='VEH'&&original.id&&tab==='Основное'&&can('arm2_veh_view')&&can('arm2_veh_edit')&&<section aria-label="Снятие операционного владельца">{!ownerChoice && ownerOf(original).kind && <button type="button" disabled={JSON.stringify(draft)!==JSON.stringify(original)} onClick={()=>setOwnerChoice({record:original,ready:true,action:'clear',confirmed:false})}>Снять привязку</button>}{JSON.stringify(draft)!==JSON.stringify(original)&&<p>Сначала сохраните изменения основной карточки. Снятие привязки их не отправляет.</p>}{ownerChoice&&<OwnerCommandEditor choice={ownerChoice} setChoice={setOwnerChoice} target={{kind:null,id:null}} can={can} active={can('arm2_veh_edit')} completed={record=>{ownerGuard.current=false;ownerDrafts.delete(key);setOwnerChoice(undefined);setOriginal(record);setDraft(record);}}/>}</section>}
    {tab === 'Документы' && kind !== 'TYPE' && (original.id ? <Documents parentType={kind} parentId={original.id} can={can}/> : <p>Сначала сохраните родительскую карточку, затем добавьте документ без файла.</p>)}
    {original.id && <div className="arm2-history-control">{history && <History type={kind} id={original.id}/>}</div>}
  </article>;
}
function VehicleSelectors({draft, change, can, editable, currentType,beforeOwnerChange}: {draft: RecordData; change: (field: string, value: Scalar) => void; can: Can; editable: boolean; currentType: Scalar | undefined;beforeOwnerChange:(cp:string|null,org:string|null)=>Promise<boolean>}) {
  return <><TypePicker selected={draft.type_id} change={value => change('type_id', value)} currentType={currentType} can={can} editable={editable}/><OwnerPicker cp={draft.owner_cp_id} org={draft.owner_org_id} change={(cp, org) => {change('owner_cp_id', cp); change('owner_org_id', org);}} can={can} editable={editable} beforeChange={beforeOwnerChange}/></>;
}
function RepRoute({kind, can}: {kind: Kind; can: Can}) {
  const {id = 'new', parentId = ''} = useParams(); return <RepLoader key={`${kind}:${parentId}:${id}`} kind={kind} can={can} id={id} parentId={parentId}/>;
}

interface OwnerChoice {record:RecordData;ready:boolean;action:'assign'|'clear';confirmed:boolean;error?:unknown;comparison?:RecordData;comparisonLabel?:string;denied?:boolean}
interface OwnerParked {original:RecordData;draft:RecordData;tab:string;choice?:OwnerChoice;selecting:boolean;query:string;offset:number}
type ChoiceSetter=Dispatch<SetStateAction<OwnerChoice|undefined>>;
const ownerPermissions=(owner:OwnerIdentity,can:Can)=>!owner.kind||['view','edit'].every(action=>can(catalogs[owner.kind!].permission+'_'+action));
async function ownerLabel(owner:OwnerIdentity,can:Can,signal?:AbortSignal):Promise<string> {
  if(!owner.kind||!owner.id)return 'Без владельца';
  if(!can(catalogs[owner.kind].permission+'_view'))throw new ApiError(403,'Нет доступа к имени операционного владельца');
  const row=await detail<RecordData>(catalogs[owner.kind].path+'/'+encodeURIComponent(owner.id),signal);
  return `${owner.kind==='CP'?'Контрагент':'Организация'} · ${row.name} · ${row.id}`;
}

function OwnedVehicles({kind,id,name,can,active,choice,setChoice,selecting,setSelecting,query,setQuery,offset,setOffset,refresh,completed}:{kind:Kind;id:string;name:string;can:Can;active:boolean;choice?:OwnerChoice;setChoice:ChoiceSetter;selecting:boolean;setSelecting:(value:boolean)=>void;query:string;setQuery:(value:string)=>void;offset:number;setOffset:(value:number)=>void;refresh:number;completed:()=>void}) {
  const scope={kind:kind==='CP'?'CP' as const:'ORG' as const,id},[status,setStatus]=useState('');
  const allowed=active&&can('arm2_veh_view')&&can('arm2_veh_edit')&&ownerPermissions(scope,can)&&!choice?.denied;
  const alive=useAlive(),ticket=useRef(0);
  const select=async(row:RecordData,action:OwnerChoice['action'])=>{
    if(!allowed)return;
    const token=++ticket.current;
    setStatus('');setChoice({record:row,ready:false,action,confirmed:false});
    try {const record=await detail<Vehicle>(catalogs.VEH.path+'/'+encodeURIComponent(row.id));if(alive.current&&token===ticket.current)setChoice(current=>current?.record.id===row.id&&current.action===action?{...current,record,ready:true,confirmed:false}:current);}
    catch(error){if(alive.current&&token===ticket.current)setChoice(current=>current?.record.id===row.id&&current.action===action?{...current,ready:false,error}:current);}
  };
  return <section aria-label="Техника владельца"><SearchCatalog kind="VEH" owner={scope} can={can} refresh={refresh} action={allowed?row=><button type="button" onClick={()=>void select(row,'clear')}>Снять привязку</button>:undefined}/>
    {allowed&&!selecting&&!choice&&<button type="button" onClick={()=>{setStatus('');setSelecting(true);}}>Добавить технику</button>}
    {status&&<p role="status">{status}</p>}
    {selecting&&!choice&&<VehicleLinkSearch can={can} query={query} change={value=>{setQuery(value);setOffset(0);}} offset={offset} setOffset={setOffset} choose={row=>void select(row,'assign')} close={()=>setSelecting(false)}/>}
    {choice&&<OwnerCommandEditor choice={choice} setChoice={setChoice} can={can} active={allowed} initiator={scope} target={choice.action==='clear'?{kind:null,id:null}:scope} targetName={name} retry={()=>void select(choice.record,choice.action)} completed={(_record,noOp)=>{setStatus(noOp?'Изменений связи нет':'Связь сохранена сервером');completed();}}/>}
  </section>;
}

function VehicleLinkSearch({can,query,change,offset,setOffset,choose,close}:{can:Can;query:string;change:(value:string)=>void;offset:number;setOffset:(value:number)=>void;choose:(row:RecordData)=>void;close:()=>void}) {
  const result=useLoad(signal=>searchPage(catalogs.VEH.path,new URLSearchParams({q:query,limit:'100',offset:String(offset)}),signal),[query,offset]);
  return <section className="arm2-owner-command" aria-label="Поиск существующей техники"><h3>Добавить существующую технику</h3><label className="arm2-field">Поиск существующей техники по имени / госномеру<input type="search" maxLength={200} value={query} onChange={event=>change(event.target.value)}/></label><ErrorMessage error={result.error} retry={result.reload}/>{result.loading&&<p role="status">Ищем технику…</p>}{result.data&&<><p>Найдено: {result.data.matched}</p><VehicleTable rows={result.data.items} action={row=><button type="button" disabled={!ownerPermissions(ownerOf(row),can)} onClick={()=>choose(row)}>Выбрать технику</button>}/><nav className="arm2-actions" aria-label="Страницы выбора техники"><button type="button" disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-100))}>Предыдущая страница поиска</button><span>Страница {offset/100+1}</span><button type="button" disabled={offset+100>=result.data.matched} onClick={()=>setOffset(offset+100)}>Следующая страница поиска</button></nav></>}<button type="button" onClick={close}>Отмена выбора техники</button></section>;
}

function OwnerCommandEditor({choice,setChoice,target,initiator,targetName,can,active,retry,completed}:{choice:OwnerChoice;setChoice:ChoiceSetter;target:OwnerIdentity;initiator?:{kind:'CP'|'ORG';id:string};targetName?:string;can:Can;active:boolean;retry?:()=>void;completed:(record:RecordData,noOp:boolean)=>void}) {
  const [busy,setBusy]=useState(false),inFlight=useRef(false),alive=useAlive();
  const record=choice.record,previous=ownerOf(record),reassignment=!!previous.kind&&!sameOwner(previous,target);
  const visible=[previous,target].every(owner=>!owner.kind||can(catalogs[owner.kind].permission+'_view'));
  const labelsResult=useLoad(async signal=>choice.ready&&visible?{previous:await ownerLabel(previous,can,signal),target:await ownerLabel(target,can,signal)}:{previous:'Владелец недоступен',target:'Назначение недоступно'},[record.id,record.version,previous.kind,previous.id,target.kind,target.id,choice.ready,visible]);
  const misplaced=!!initiator&&choice.action==='clear'&&!sameOwner(previous,initiator);
  const allowed=active&&!choice.denied&&can('arm2_veh_view')&&can('arm2_veh_edit')&&[previous,target,...(initiator?[initiator]:[])].every(owner=>ownerPermissions(owner,can));
  const submit=async()=>{
    if(!allowed||!choice.ready||misplaced||busy||inFlight.current||!labelsResult.data||labelsResult.error||(reassignment&&!choice.confirmed))return;
    inFlight.current=true;setBusy(true);setChoice({...choice,error:undefined});
    try {
      const saved=await setOperationalOwner(record.id,{expected_version:record.version,target_kind:target.kind,target_id:target.id,expected_owner_kind:previous.kind,expected_owner_id:previous.id,confirm_reassignment:reassignment&&choice.confirmed,...(initiator?{initiator_kind:initiator.kind,initiator_id:initiator.id}:{})});
      const fresh=await detail<Vehicle>(catalogs.VEH.path+'/'+encodeURIComponent(record.id));
      if(!sameOwner(ownerOf(fresh),target)||fresh.version<saved.version)throw new ApiError(409,'Связь снова изменилась; сравните с сервером');
      if(alive.current)completed(fresh,saved.no_op);
    } catch(error){if(alive.current)setChoice({...choice,error,denied:error instanceof ApiError&&error.status===403});}
    finally{inFlight.current=false;if(alive.current)setBusy(false);}
  };
  const compare=async()=>{if(inFlight.current)return;try{const comparison=await detail<Vehicle>(catalogs.VEH.path+'/'+encodeURIComponent(record.id));const owner=ownerOf(comparison);const comparisonLabel=!owner.kind||can(catalogs[owner.kind].permission+'_view')?await ownerLabel(owner,can):'Нет доступа к владельцу';if(alive.current)setChoice({...choice,comparison,comparisonLabel});}catch(error){if(alive.current)setChoice({...choice,error});}};
  return <section className="arm2-owner-command" aria-label="Подтверждение операционного владельца"><h3>{choice.action==='clear'?'Снять привязку':'Назначить технику'}</h3><p>{record.name} · {plateLabel(record.plate_number)} · {record.id} · Версия {record.version}</p>{!choice.ready&&<p>Актуальная запись ещё не загружена. Назначение недоступно.</p>}<ErrorMessage error={choice.error}/><ErrorMessage error={labelsResult.error} retry={labelsResult.reload}/>{labelsResult.data&&<><p>Прежний владелец: {labelsResult.data.previous}</p><p>Получатель: {labelsResult.data.target}{targetName&&visible?' ('+targetName+')':''}</p></>}{!allowed&&<p>Нет прав на изменение этой связи. Имена скрытых владельцев не раскрываются.</p>}{misplaced&&<p>Техника больше не принадлежит этой карточке. Снятие здесь запрещено; выберите актуальную запись.</p>}
    <fieldset disabled={busy||!allowed||!choice.ready||misplaced}>{reassignment&&<label className="arm2-owner-confirm"><input type="checkbox" checked={choice.confirmed} onChange={event=>setChoice({...choice,confirmed:event.target.checked})}/>{target.kind?'Подтверждаю переназначение от прежнего владельца к указанному получателю':'Подтверждаю снятие привязки без замены владельца'}</label>}<button type="button" disabled={busy||!labelsResult.data||!!labelsResult.error||(reassignment&&!choice.confirmed)} onClick={()=>void submit()}>{busy?'Сохраняем связь…':choice.action==='clear'?'Подтвердить снятие привязки':'Подтвердить назначение'}</button></fieldset>
    {choice.error instanceof ApiError&&choice.error.status===409&&<><button type="button" disabled={busy} onClick={()=>void compare()}>Сравнить владельца с сервером</button>{choice.comparison&&<section aria-label="Владелец на сервере"><p>Версия на сервере: {choice.comparison.version} · {choice.comparisonLabel}</p><button type="button" disabled={busy} onClick={()=>setChoice({...choice,record:choice.comparison!,ready:true,confirmed:false,error:undefined,comparison:undefined,comparisonLabel:undefined})}>Принять актуальную версию и подтвердить заново</button></section>}</>}
    {!choice.ready&&retry&&<button type="button" disabled={busy} onClick={retry}>Повторить загрузку техники</button>}
    <button type="button" disabled={busy} onClick={()=>setChoice(undefined)}>Отмена изменения связи</button><p>Операционный владелец — учётная связь; это не юридическая собственность.</p>
  </section>;
}
function RepLoader({kind, can, id, parentId}: {kind: Kind; can: Can; id: string; parentId: string}) {
  const [reloadId, setReloadId] = useState(0);
  const path = `${catalogs[kind].path}/${encodeURIComponent(parentId)}/representatives`;
  const result = useLoad(async signal => {
    await detail<RecordData>(`${catalogs[kind].path}/${encodeURIComponent(parentId)}`, signal);
    return {record: id === 'new' ? emptyRecord('REP') : await detail<RecordData>(`${path}/${encodeURIComponent(id)}`, signal), reloadId};
  }, [path, id, reloadId]);
  return <><Link to={link(kind, parentId)}>← К родительской карточке</Link><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p>Открываем представителя…</p>}{result.data && <RepCard key={result.data.reloadId} kind={kind} can={can} initial={result.data.record} path={path} parentId={parentId} reload={() => setReloadId(value => value + 1)}/>}</>;
}
function RepCard({kind, can, initial, path, parentId, reload}: {kind: Kind; can: Can; initial: RecordData; path: string; parentId: string; reload: () => void}) {
  const alive = useAlive();
  const [original, setOriginal] = useState(initial), [draft, setDraft] = useState(initial), [history, setHistory] = useState(false);
  const navigate = useNavigate();
  return <article><h2>Представитель: {original.id ? original.name : 'создание'}</h2><p>Связанная учётная запись: {display(original.user_id)}. Информационная связь не предоставляет права доступа.</p><SaveForm resource={`${path}/${encodeURIComponent(original.id)}`} original={original} allowed={can(catalogs[kind].permission + '_edit')} reload={reload} submit={async () => {
    const saved = await save(`${path}${original.id ? '/' + encodeURIComponent(original.id) : ''}`, payload('REP', draft, original), !!original.id);
    if (!alive.current) return;
    setOriginal(saved); setDraft(saved); if (!original.id) navigate(`${link(kind, parentId)}/representatives/${encodeURIComponent(saved.id)}`, {replace: true});
  }}>{() => <><div className="arm2-grid">{repFields.filter(field => !['user_id', 'last_name', 'first_name', 'middle_name'].includes(field)).map(field => <Field key={field} field={field} label={field === 'name' ? 'ФИО / Имя' : field === 'phone' ? 'Телефон представителя' : field === 'email' ? 'Email представителя' : undefined} value={draft[field]} required={field === 'name'} change={value => setDraft(current => ({...current, [field]: value}))} options={field === 'state' ? ['Действующий', 'Архивный'].map(value => ({value, label: value})) : undefined}/>)}</div><RepresentativeUser selected={draft.user_id} change={value => setDraft(current => ({...current, user_id: value}))} canEdit={can(catalogs[kind].permission + '_edit')}/></>}</SaveForm>{original.id && <><button type="button" onClick={() => setHistory(x => !x)} aria-expanded={history}>История</button>{history && <History type={kind === 'CP' ? 'CPREP' : 'ORGREP'} id={original.id}/>}</>}</article>;
}

function RepresentativeUser({selected, change, canEdit}: {selected: Scalar | undefined; change: (value: number | null) => void; canEdit: boolean}) {
  const allowed = useContext(UserLookupAllowed) && canEdit;
  const [search, setSearch] = useState('');
  // /users is an existing admin-only, unpaginated endpoint in the current workspace.
  // Never request it using ARM2 EDIT as a substitute for that role.
  const users = useLoad(signal => allowed ? detail<{id: number; name: string}[]>('/users', signal) : Promise.resolve([]), [allowed]);
  const rows = users.data || [];
  const matches = rows.filter(row => row.id === selected || row.name.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')));
  return <section><h3>Связь с учётной записью</h3><p>Назначение, смена или снятие связи не изменяет роль и права пользователя.</p>{!allowed && <p>Выбор пользователей доступен только администратору с правом редактирования этой карточки. Текущая связь сохраняется.</p>}<ErrorMessage error={users.error} retry={allowed ? users.reload : undefined}/><fieldset disabled={!allowed || users.loading || !!users.error}><label className="arm2-field">Поиск по всему разрешённому списку пользователей<input value={search} onChange={e => setSearch(e.target.value)}/></label><Field field="user_id" value={selected} change={value => {
    if (value == null) change(null);
    else {const user = rows.find(row => String(row.id) === value); if (user && Number.isSafeInteger(user.id)) change(user.id);}
  }} options={[{value: '', label: 'Не связан'}, ...(selected != null && !rows.some(row => row.id === selected) ? [{value: String(selected), label: 'Текущая связь (данные пользователя недоступны)', disabled: true}] : []), ...matches.map(row => ({value: String(row.id), label: row.name}))]}/></fieldset></section>;
}

function useOwnerDraftGuard(blocked: boolean, park: () => void, discard: () => void) {
  const {navigator} = useContext(UNSAFE_NavigationContext);
  const location = useLocation();
  const dirty = useRef(blocked); dirty.current = blocked;
  const callbacks = useRef({park, discard}); callbacks.current = {park, discard};
  const index = useRef<number | undefined>(window.history.state?.idx);
  useEffect(() => {index.current = window.history.state?.idx;}, [location.key]);
  useEffect(() => {
    const mountedHref = window.location.href, mountedPathname = location.pathname;
    const leave = () => !dirty.current || window.confirm('Покинуть карточку и потерять несохранённые изменения?');
    const push = navigator.push, replace = navigator.replace, go = navigator.go;
    const guardedPush: typeof push = (...args) => {if (leave()) {dirty.current = false; push.apply(navigator, args);}};
    const guardedReplace: typeof replace = (...args) => {const to=args[0];if(typeof to!=='string'&&(!to.pathname||to.pathname===mountedPathname)){replace.apply(navigator,args);return;}if (leave()) {dirty.current = false; replace.apply(navigator, args);}};
    const guardedGo: typeof go = (...args) => {if (leave()) {dirty.current = false; go.apply(navigator, args);}};
    navigator.push = guardedPush; navigator.replace = guardedReplace; navigator.go = guardedGo;
    let restoring = false, unmounted = false;
    const remove = () => {window.removeEventListener('popstate', pop, true); window.removeEventListener('beforeunload', unload);};
    const pop = (event: PopStateEvent) => {
      if (restoring) {restoring = false; if (unmounted) remove(); else event.stopImmediatePropagation(); return;}
      const next = event.state?.idx;
      if (dirty.current && typeof next === 'number' && typeof index.current === 'number' && next !== index.current) {
        if (!leave()) {event.stopImmediatePropagation(); restoring = true; window.history.go(index.current - next); return;}
        dirty.current = false; callbacks.current.discard();
      }
      index.current = next;
    };
    const unload = (event: BeforeUnloadEvent) => {if (dirty.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('popstate', pop, true); window.addEventListener('beforeunload', unload);
    return () => {
      unmounted = true;
      if (navigator.push === guardedPush) navigator.push = push;
      if (navigator.replace === guardedReplace) navigator.replace = replace;
      if (navigator.go === guardedGo) navigator.go = go;
      if (dirty.current && window.location.href !== mountedHref) {
        callbacks.current.park();
        // A Window POP can synchronously unmount us in Router's earlier listener.
        // Finish this event before removing our listener; a refused POP remounts
        // from parked state, while an accepted POP discards it.
        window.setTimeout(() => {if (!restoring) {remove(); callbacks.current.discard();}}, 0);
      } else remove();
    };
  }, [navigator]);
  return dirty;
}
