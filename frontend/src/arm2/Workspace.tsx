import { createContext, useContext, useState } from 'react';
import { Link, Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { all, catalogs, detail, list, save } from './api';
import type { Kind, RecordData, Scalar } from './api';
import { emptyRecord, isParty, labels, missingRequirements, payload, repFields, requirementLabel, requirements } from './model';
import { display, ErrorMessage, Field, History, Pagination, SaveForm, Tabs, Unavailable, useAlive, useLoad } from './shared';

export type Can = (permission: string) => boolean;
const kinds = Object.keys(catalogs) as Kind[];
const UserLookupAllowed = createContext(false);
const link = (kind: Kind, id?: string) => `/arm2/${kind.toLowerCase()}${id ? '/' + encodeURIComponent(id) : ''}`;
export default function Workspace({can, userLookupAllowed = false}: {can: Can; userLookupAllowed?: boolean}) {
  const visible = kinds.filter(kind => can(catalogs[kind].permission + '_view'));
  return <UserLookupAllowed.Provider value={userLookupAllowed}><section className="panel arm2"><h1>АРМ 2.0</h1><nav className="arm2-tabs" aria-label="Справочники АРМ 2.0">{visible.map(kind => <Link key={kind} to={link(kind)}>{catalogs[kind].label}</Link>)}</nav><Routes>
    {visible.map(kind => <Route key={kind} path={kind.toLowerCase()}><Route index element={<Catalog key={kind} kind={kind} can={can}/>}/><Route path=":id" element={<CardRoute kind={kind} can={can}/>}/>{isParty(kind) && <Route path=":parentId/representatives/:id" element={<RepRoute kind={kind} can={can}/>}/>}</Route>)}
    <Route path="*" element={visible[0] ? <Navigate to={link(visible[0])} replace/> : <p>Нет прав на просмотр справочников АРМ 2.0</p>}/>
  </Routes></section></UserLookupAllowed.Provider>;
}
function Catalog({kind, can}: {kind: Kind; can: Can}) {
  const [params, setParams] = useSearchParams();
  const raw = Number(params.get('offset') || 0), offset = Number.isSafeInteger(raw) && raw >= 0 && raw % 100 === 0 ? raw : 0;
  const [search, setSearch] = useState(''), [status, setStatus] = useState('');
  const result = useLoad(signal => list<RecordData>(catalogs[kind].path, offset, signal), [kind, offset]);
  const rows = result.data?.filter(row => row.name.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')) && (!status || row.status === status));
  return <><header className="arm2-actions"><h2>{catalogs[kind].label}</h2>{can(catalogs[kind].permission + '_edit') && <Link to={link(kind, 'new')}>Создать</Link>}</header><div className="arm2-grid"><label className="arm2-field">Поиск по имени на текущей странице<input value={search} onChange={e => setSearch(e.target.value)}/></label>{kind === 'TYPE' && <label className="arm2-field">Состояние на текущей странице<select value={status} onChange={e => setStatus(e.target.value)}><option value="">Все</option><option value="CONFIRMED">Описание заполнено</option><option value="REVIEW">Описание не завершено</option></select></label>}</div><p>Поиск и фильтр относятся только к текущей странице. Другие записи доступны на следующих страницах.</p><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем список…</p>}{rows && <><div className="arm2-table"><table><thead><tr><th>Рабочее имя</th>{kind === 'TYPE' ? <>{requirements.map(key => <th key={key}>{labels[key]}</th>)}<th>Состояние</th></> : kind === 'VEH' ? <><th>Государственный номер</th><th>Состояние учёта</th></> : <><th>ИНН</th><th>Состояние</th></>}</tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><Link to={link(kind, row.id)}>{row.name}</Link></td>{kind === 'TYPE' ? <>{requirements.map(key => <td key={key}>{requirementLabel(row[key])}</td>)}<td>{row.status === 'CONFIRMED' ? 'Описание заполнено' : 'Описание не завершено'}</td></> : kind === 'VEH' ? <><td>{display(row.plate_number)}</td><td>{display(row.accounting_state)}</td></> : <><td>{display(row.inn)}</td><td>{row.status === 'CONFIRMED' ? 'Описание заполнено' : 'Описание не завершено'}</td></>}</tr>)}</tbody></table></div>{rows.length === 0 && <p>На текущей странице подходящих записей нет</p>}<Pagination offset={offset} count={result.data!.length} change={next => setParams({offset: String(next)})}/></>}</>;
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
  const [original, setOriginal] = useState(initial), [draft, setDraft] = useState(initial), [tab, setTab] = useState('Основное'), [history, setHistory] = useState(false);
  const navigate = useNavigate();
  const change = (field: string, value: Scalar) => setDraft(current => ({...current, [field]: value}));
  const fields = (keys: string[]) => <div className="arm2-grid">{keys.map(field => <Field key={field} field={field} value={draft[field]} change={value => change(field, value)} required={field === 'name' || field === 'plate_number'}/>)}</div>;
  const tabs = isParty(kind) ? ['Основное', 'Реквизиты', 'Контакты', 'Банковские счета', 'Документы', 'Техника'] : kind === 'TYPE' ? ['Основное', 'Требования к технике этого типа', 'Виды работ'] : ['Основное'];
  return <article><h2>{catalogs[kind].singular}: {original.id ? original.name : 'создание'}</h2>{kind !== 'VEH' && <div role="status"><p>Состояние по последнему ответу сервера: {original.id ? (original.status === 'CONFIRMED' ? 'Описание заполнено (CONFIRMED)' : 'Описание не завершено (REVIEW)') : 'будет рассчитано при сохранении'}</p>{isParty(kind) && <p>Для CONFIRMED нужны рабочее имя, полное наименование и юридический адрес; указанные реквизиты должны быть валидны. Банк, ИНН, представители и документы сами по себе не обязательны. Не заполнено: {['name', 'full_name', 'legal_address'].filter(key => !draft[key]).map(key => labels[key]).join(', ') || 'основные поля заполнены'}.</p>}{kind === 'TYPE' && <p>Не заполнено: {missingRequirements(draft) || 'все требования заполнены'}. Тип REVIEW нельзя вновь назначить машине.</p>}</div>}
    <Tabs tabs={tabs} active={tab} setActive={setTab}/>
    <SaveForm resource={`${catalogs[kind].path}/${encodeURIComponent(original.id)}`} original={original} allowed={can(catalogs[kind].permission + '_edit')} reload={reload} submit={async () => {
      const saved = await save(`${catalogs[kind].path}${original.id ? '/' + encodeURIComponent(original.id) : ''}`, payload(kind, draft, original), !!original.id);
      if (!alive.current) return;
      setOriginal(saved); setDraft(saved);
      if (!original.id) navigate(link(kind, saved.id), {replace: true});
    }}>{editable => <>
      {tab === 'Основное' && fields(kind === 'TYPE' ? ['name'] : kind === 'VEH' ? ['name', 'plate_number', 'model', 'wialon_unit_id'] : ['name', 'inn', 'legal_form', 'legal_address', 'postal_address', 'phone', 'email'])}
      {kind === 'VEH' && <><Field field="accounting_state" value={draft.accounting_state} change={value => change('accounting_state', value)} options={['Действующая', 'Архивная'].map(value => ({value, label: value}))}/><VehicleSelectors draft={draft} change={change} can={can} editable={editable}/></>}
      {tab === 'Требования к технике этого типа' && <div className="arm2-grid">{requirements.map(field => <Field key={field} field={field} value={draft[field] == null ? '' : String(draft[field])} change={value => change(field, value === 'true' ? true : value === 'false' ? false : null)} options={[{value: '', label: 'Не заполнено'}, {value: 'true', label: 'Требуется'}, ...(field === 'requires_sts' ? [] : [{value: 'false', label: 'Не требуется'}])]}/>)}</div>}
      {tab === 'Реквизиты' && fields(['full_name', 'short_name', 'kpp', 'ogrn'])}
      {tab === 'Банковские счета' && <><p>Один актуальный комплект реквизитов</p>{fields(['bank_name', 'bik', 'settlement_account', 'correspondent_account'])}</>}
      {(tab === 'Документы' || tab === 'Виды работ') && <Unavailable/>}
      {tab === 'Контакты' && (original.id ? <Representatives kind={kind} parent={original} signer={draft.primary_signer_id} changeSigner={value => change('primary_signer_id', value)} editable={editable}/> : <p>Сначала сохраните карточку, затем добавьте представителей и подписанта.</p>)}
      {tab === 'Техника' && (original.id ? <OwnedVehicles kind={kind} id={original.id} can={can}/> : <p>Сначала сохраните карточку.</p>)}
    </>}</SaveForm>
    {original.id && <div className="arm2-history-control"><button type="button" aria-expanded={history} onClick={() => setHistory(x => !x)}>{history ? 'Закрыть историю' : 'История'}</button>{history && <History type={kind} id={original.id}/>}</div>}
  </article>;
}
function Choice({path, field, selected, change, canView, currentType, editable}: {path: string; field: string; selected: Scalar | undefined; change: (value: Scalar) => void; canView: boolean; currentType?: Scalar; editable: boolean}) {
  const [search, setSearch] = useState('');
  const result = useLoad(signal => canView ? all<RecordData>(path, signal) : Promise.resolve([]), [path, canView]);
  const rows = result.data || [], current = rows.find(row => row.id === selected);
  const matches = rows.filter(row => row.id === selected || row.name.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru')));
  return <section className="arm2-choice"><label className="arm2-field">Поиск: {labels[field]} (весь доступный справочник)<input disabled={!canView || !editable} value={search} onChange={e => setSearch(e.target.value)}/></label><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p>Загружаем весь справочник…</p>}{!canView && <p>Нет прав на просмотр справочника. Текущая ссылка сохраняется.</p>}<Field field={field} value={selected} change={change} options={[
    {value: '', label: 'Не назначено'}, ...(selected && !current ? [{value: String(selected), label: 'Текущее назначение (недоступно в справочнике)'}] : []),
    ...matches.map(row => ({value: row.id, label: row.name + (field === 'type_id' && row.status !== 'CONFIRMED' ? ` — REVIEW: не заполнено ${missingRequirements(row) || 'описание'}${row.id === currentType ? ' (текущее назначение сохраняется)' : ''}` : ''), disabled: field === 'type_id' && row.status !== 'CONFIRMED' && row.id !== currentType})),
  ]}/>{(result.loading || !!result.error || !canView) && <p>Для нового назначения дождитесь доступного полного списка. Можно сохранить текущую ссылку.</p>}</section>;
}
function VehicleSelectors({draft, change, can, editable}: {draft: RecordData; change: (field: string, value: Scalar) => void; can: Can; editable: boolean}) {
  const [currentType] = useState(draft.type_id), [ownerKind, setOwnerKind] = useState(draft.owner_cp_id ? 'CP' : draft.owner_org_id ? 'ORG' : '');
  return <><Choice path={catalogs.TYPE.path} field="type_id" selected={draft.type_id} change={value => change('type_id', value)} currentType={currentType} canView={can('arm2_type_view')} editable={editable}/><h3>Операционный владелец</h3><Field field="owner" label="Вид владельца" value={ownerKind} change={value => {setOwnerKind(String(value || '')); change('owner_cp_id', null); change('owner_org_id', null);}} options={[{value: '', label: 'Без владельца'}, {value: 'CP', label: 'Контрагент'}, {value: 'ORG', label: 'Собственная организация'}]}/>{ownerKind && <Choice key={ownerKind} path={ownerKind === 'CP' ? catalogs.CP.path : catalogs.ORG.path} field={ownerKind === 'CP' ? 'owner_cp_id' : 'owner_org_id'} selected={ownerKind === 'CP' ? draft.owner_cp_id : draft.owner_org_id} change={value => change(ownerKind === 'CP' ? 'owner_cp_id' : 'owner_org_id', value)} canView={can(ownerKind === 'CP' ? 'arm2_cp_view' : 'arm2_org_view')} editable={editable}/>}</>;
}
function Representatives({kind, parent, signer, changeSigner, editable}: {kind: Kind; parent: RecordData; signer: Scalar | undefined; changeSigner: (value: Scalar) => void; editable: boolean}) {
  const result = useLoad(signal => all<RecordData>(`${catalogs[kind].path}/${encodeURIComponent(parent.id)}/representatives`, signal), [kind, parent.id]);
  return <section><h3>Представители</h3><p>Контакты представителей отличаются от официальных телефона и email карточки. Связь с учётной записью не предоставляет прав доступа.</p><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p>Загружаем представителей…</p>}{result.data && <><ul>{result.data.map(row => <li key={row.id}><Link to={`${link(kind, parent.id)}/representatives/${encodeURIComponent(row.id)}`}>{row.name}</Link> — {display(row.position)}, {display(row.state)}</li>)}</ul>{result.data.length === 0 && <p>Представителей пока нет</p>}<Field field="primary_signer_id" value={signer} change={changeSigner} options={[{value: '', label: 'Не назначен'}, ...(signer && !result.data.some(row => row.id === signer) ? [{value: String(signer), label: 'Текущий подписант недоступен', disabled: true}] : []), ...result.data.map(row => ({value: row.id, label: row.name}))]}/></>}{editable && <Link to={`${link(kind, parent.id)}/representatives/new`}>Добавить представителя</Link>}</section>;
}
function OwnedVehicles({kind, id, can}: {kind: Kind; id: string; can: Can}) {
  const permitted = can('arm2_veh_view');
  const result = useLoad(signal => permitted ? all<RecordData>(catalogs.VEH.path, signal) : Promise.resolve([]), [permitted, kind, id]);
  return <section><h3>Техника</h3>{!permitted ? <p>Нет права просмотра техники</p> : <><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p>Загружаем все страницы техники…</p>}{result.data && <><p>Техника по текущему операционному владельцу; загружены все доступные страницы.</p><ul>{result.data.filter(row => row[kind === 'CP' ? 'owner_cp_id' : 'owner_org_id'] === id).map(row => <li key={row.id}><Link to={link('VEH', row.id)}>{row.name} — {display(row.plate_number)}</Link></li>)}</ul><p>Назначение и смена владельца — в карточке машины.</p><Link to={link('VEH')}>Открыть справочник техники</Link></>}</>}</section>;
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
  }}>{() => <><div className="arm2-grid">{repFields.filter(field => field !== 'user_id').map(field => <Field key={field} field={field} label={field === 'phone' ? 'Телефон представителя' : field === 'email' ? 'Email представителя' : undefined} value={draft[field]} required={field === 'name'} change={value => setDraft(current => ({...current, [field]: value}))} options={field === 'state' ? ['Действующий', 'Архивный'].map(value => ({value, label: value})) : undefined}/>)}</div><RepresentativeUser selected={draft.user_id} change={value => setDraft(current => ({...current, user_id: value}))} canEdit={can(catalogs[kind].permission + '_edit')}/></>}</SaveForm>{original.id && <><button type="button" onClick={() => setHistory(x => !x)} aria-expanded={history}>История</button>{history && <History type={kind === 'CP' ? 'CPREP' : 'ORGREP'} id={original.id}/>}</>}</article>;
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
