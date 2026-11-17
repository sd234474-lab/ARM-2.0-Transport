import { createContext, useContext, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { catalogs, detail, save, searchPage } from './api';
import type { Kind, RecordData, Scalar } from './api';
import SearchCatalog from './SearchCatalog';
import BulkVehicles from './BulkVehicles';
import DocumentKinds from './DocumentKinds';
import Documents, { DocumentRoute } from './Documents';
import Representatives from './Representatives';
import { OwnerPicker, TypePicker } from './Selectors';
import { emptyRecord, isParty, labels, missingRequirements, payload, repFields, requirementLabel, requirements } from './model';
import { display, ErrorMessage, Field, History, Pagination, SaveForm, Tabs, Unavailable, useAlive, useLoad } from './shared';

export type Can = (permission: string) => boolean;
const kinds: Kind[] = ['VEH', 'TYPE', 'ORG', 'CP'];
const UserLookupAllowed = createContext(false);
const link = (kind: Kind, id?: string) => `/arm2/${kind.toLowerCase()}${id ? '/' + encodeURIComponent(id) : ''}`;
export default function Workspace({can, userLookupAllowed = false}: {can: Can; userLookupAllowed?: boolean}) {
  const docView = can('arm2_doc_kind_view');
  const docEdit = docView && can('arm2_doc_kind_edit');
  const visible = kinds.filter(kind => can(catalogs[kind].permission + '_view'));
  return <UserLookupAllowed.Provider value={userLookupAllowed}><section className="panel arm2"><h1>АРМ 2.0</h1><nav className="arm2-sections" aria-label="Справочники АРМ 2.0"><div className="arm2-section"><strong>Техника</strong><div>{visible.filter(kind => kind === 'VEH' || kind === 'TYPE').map(kind => <NavLink key={kind} to={link(kind)}>{catalogs[kind].label}</NavLink>)}<span aria-disabled="true">Согласование с заказчиком <small>Пока не реализовано</small></span></div></div>{visible.filter(kind => kind === 'ORG' || kind === 'CP').map(kind => <NavLink className="arm2-section" key={kind} to={link(kind)}>{catalogs[kind].label}</NavLink>)}{docView && <NavLink className="arm2-section" to="/arm2/document-kinds">Типы документов</NavLink>}{['Договоры', 'Территории', 'Нормативы'].map(name => <span className="arm2-section" aria-disabled="true" key={name}>{name}<small>Пока не реализовано</small></span>)}</nav><Routes>
    {visible.map(kind => <Route key={kind} path={kind.toLowerCase()}><Route index element={<Catalog key={kind} kind={kind} can={can}/>}/><Route path=":id" element={<CardRoute kind={kind} can={can}/>}/>{kind !== 'TYPE' && <Route path=":parentId/documents/:id" element={<DocumentRoute parentType={kind} can={can}/>}/>}{isParty(kind) && <Route path=":parentId/representatives/:id" element={<RepRoute kind={kind} can={can}/>}/>}</Route>)}
    {can('arm2_veh_view') && can('arm2_veh_edit') && <Route path="veh/bulk" element={<BulkVehicles admin={userLookupAllowed}/>}/>}
    {docView && <Route path="document-kinds/*" element={<DocumentKinds editable={docEdit}/>}/>}
    <Route path="*" element={visible[0] ? <Navigate to={link(visible[0])} replace/> : docView ? <Navigate to="/arm2/document-kinds" replace/> : <p>Нет прав на просмотр справочников АРМ 2.0</p>}/>
  </Routes></section></UserLookupAllowed.Provider>;
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
  return <><header className="arm2-actions"><h2>{catalogs[kind].label}</h2>{can('arm2_type_edit') && <Link to={link(kind, 'new')}>Создать</Link>}</header>
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
  const alive = useAlive();
  const [tabParams, setTabParams] = useSearchParams();
  const [original, setOriginal] = useState(initial), [draft, setDraft] = useState(initial), [tab, setTab] = useState(tabParams.get('tab') === 'documents' && kind !== 'TYPE' ? 'Документы' : 'Основное'), [history, setHistory] = useState(false);
  const navigate = useNavigate();
  const change = (field: string, value: Scalar) => setDraft(current => ({...current, [field]: value}));
  const fields = (keys: string[]) => <div className="arm2-grid">{keys.map(field => <Field key={field} field={field} value={draft[field]} change={value => change(field, value)} required={field === 'name' || field === 'plate_number'}/>)}</div>;
  const tabs = isParty(kind) ? ['Основное', 'Контакты', 'Банковские счета', 'Документы', 'Техника'] : kind === 'TYPE' ? ['Основное', 'Виды работ'] : ['Основное', 'Документы'];
  return <article><h2>{catalogs[kind].singular}: {original.id ? original.name : 'создание'}</h2>{kind !== 'VEH' && <div className="arm2-card-status"><p role="status">Состояние описания: {original.id ? original.status === 'CONFIRMED' ? 'Описание заполнено (CONFIRMED)' : 'Описание не завершено (REVIEW)' : 'будет рассчитано при сохранении'}</p><details><summary>Условия полного описания</summary>{isParty(kind) ? <p>Нужны рабочее имя, полное наименование и юридический адрес. Заполненные реквизиты проверяются сервером. Не заполнено: {['name', 'full_name', 'legal_address'].filter(key => !draft[key]).map(key => labels[key]).join(', ') || 'основные поля заполнены'}.</p> : <p>Не заполнено: {missingRequirements(draft) || 'все требования заполнены'}. Тип REVIEW нельзя вновь назначить машине.</p>}</details></div>}
    <Tabs tabs={tabs} active={tab} setActive={value => {setTab(value); setTabParams(current => {const next = new URLSearchParams(current); if (value === 'Документы') next.set('tab', 'documents'); else next.delete('tab'); return next;}, {replace: true});}}/>
    <SaveForm resource={`${catalogs[kind].path}/${encodeURIComponent(original.id)}`} original={original} allowed={can(catalogs[kind].permission + '_edit')} showSubmit={!['Документы', 'Виды работ', 'Техника'].includes(tab)} reload={reload} submit={async () => {
      const saved = await save(`${catalogs[kind].path}${original.id ? '/' + encodeURIComponent(original.id) : ''}`, payload(kind, draft, original), !!original.id);
      if (!alive.current) return;
      setOriginal(saved); setDraft(saved);
      if (!original.id) navigate(link(kind, saved.id), {replace: true});
    }}>{editable => <>
      {tab === 'Основное' && kind === 'VEH' && <div className="arm2-grid arm2-vehicle-main">{['name', 'plate_number', 'model', 'wialon_unit_id'].map(field => <Field key={field} field={field} value={draft[field]} required={field === 'name' || field === 'plate_number'} change={value => change(field, value)}/>)}<Field field="accounting_state" value={draft.accounting_state} change={value => change('accounting_state', value)} options={['Действующая', 'Архивная'].map(value => ({value, label: value}))}/><VehicleSelectors draft={draft} change={change} can={can} editable={editable} currentType={original.type_id}/></div>}
      {tab === 'Основное' && kind === 'TYPE' && <><div className="arm2-grid arm2-type-main"><Field field="name" value={draft.name} required change={value => change('name', value)}/><Field field="plate_category" value={draft.plate_category} change={value => change('plate_category', value || null)} options={[{value: '', label: 'Не заполнено'}, ...['Автомобильный', 'Самоходная техника'].map(value => ({value, label: value}))]}/></div><h3>Требования к технике этого типа</h3><div className="arm2-grid">{requirements.map(field => <Field key={field} field={field} value={draft[field] == null ? '' : String(draft[field])} change={value => change(field, value === 'true' ? true : value === 'false' ? false : null)} options={field === 'requires_sts' ? [...(draft.requires_sts == null ? [{value: '', label: 'Исторически не заполнено — выберите «Требуется»'}] : []), {value: 'true', label: 'Требуется'}] : [{value: '', label: 'Не заполнено'}, {value: 'true', label: 'Требуется'}, {value: 'false', label: 'Не требуется'}]}/>)}</div></>}
      {tab === 'Основное' && isParty(kind) && <><section><h3>Наименования и реквизиты</h3>{fields(['name', 'full_name', 'short_name', 'legal_form', 'inn', 'kpp', 'ogrn'])}</section><section><h3>Адреса и официальные контакты</h3>{fields(['legal_address', 'postal_address', 'phone', 'email'])}</section></>}
      {tab === 'Банковские счета' && <><p>Один актуальный комплект реквизитов</p>{fields(['bank_name', 'bik', 'settlement_account', 'correspondent_account'])}</>}
      {tab === 'Виды работ' && <Unavailable/>}
      {tab === 'Контакты' && (original.id ? <Representatives kind={kind} parent={original} signer={draft.primary_signer_id} changeSigner={value => change('primary_signer_id', value)} editable={editable}/> : <p>Сначала сохраните карточку, затем добавьте представителей и подписанта.</p>)}
      {tab === 'Техника' && (original.id ? <OwnedVehicles kind={kind} id={original.id} can={can}/> : <p>Сначала сохраните карточку.</p>)}
    </>}</SaveForm>
    {tab === 'Документы' && kind !== 'TYPE' && (original.id ? <Documents parentType={kind} parentId={original.id} can={can}/> : <p>Сначала сохраните родительскую карточку, затем добавьте документ без файла.</p>)}
    {original.id && <div className="arm2-history-control"><button type="button" aria-expanded={history} onClick={() => setHistory(x => !x)}>{history ? 'Закрыть историю' : 'История'}</button>{history && <History type={kind} id={original.id}/>}</div>}
  </article>;
}
function VehicleSelectors({draft, change, can, editable, currentType}: {draft: RecordData; change: (field: string, value: Scalar) => void; can: Can; editable: boolean; currentType: Scalar | undefined}) {
  return <><TypePicker selected={draft.type_id} change={value => change('type_id', value)} currentType={currentType} can={can} editable={editable}/><OwnerPicker cp={draft.owner_cp_id} org={draft.owner_org_id} change={(cp, org) => {change('owner_cp_id', cp); change('owner_org_id', org);}} can={can} editable={editable}/></>;
}
function OwnedVehicles({kind, id, can}: {kind: Kind; id: string; can: Can}) {
  return <SearchCatalog kind="VEH" owner={{kind: kind === 'CP' ? 'CP' : 'ORG', id}} can={can}/>;
}
function RepRoute({kind, can}: {kind: Kind; can: Can}) {
  const {id = 'new', parentId = ''} = useParams(); return <RepLoader key={`${kind}:${parentId}:${id}`} kind={kind} can={can} id={id} parentId={parentId}/>;
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
