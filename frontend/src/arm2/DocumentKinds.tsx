import { CatalogRow } from './CatalogRow';
import { CatalogFilters } from './CatalogFilters';
import {useDraftGuard} from './useDraftGuard';
import { useEffect, useRef, useState } from 'react';
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, ApiError } from '../api';
import type { DocumentKind, DocumentParent } from './api';
import { CatalogHeader, ErrorMessage, History, useAlive, useLoad } from './shared';

const base = '/arm2/document-kinds';
const parents: DocumentParent[] = ['CP', 'ORG', 'VEH'];
const names = {CP: 'Контрагенты', ORG: 'Собственные организации', VEH: 'Техника'};
const canonical = (values: DocumentParent[]) => parents.filter(value => values.includes(value));
const applicability = (values: DocumentParent[]) => canonical(values).map(value => names[value]).join(' / ');
const multiplicity = (value: number | null) => value === null ? 'Без ограничения' : String(value);
interface Page {items: DocumentKind[]; total: number; matched: number; limit: number; offset: number; facets?: Record<string, number>}
interface Draft {name: string; parents: DocumentParent[]; mode: '' | 'unlimited' | 'limited'; maximum: string; template: '' | 'DOCUMENT_NAME' | 'PARENT_AND_KIND' | 'VEH_CALIBRATION'}
interface ParkedDraft {original?: DocumentKind; draft: Draft; denied: boolean; error: unknown}
// Only a cancelled POP may need to remount a form before its listener runs.
// Keep the original version too: restoring a draft must never bypass a 409.
const parkedDrafts = new Map<string, ParkedDraft>();
const draftOf = (record?: DocumentKind): Draft => record ? {template: record.file_naming_template || '', name: record.name, parents: canonical(record.applicable_object_types),
  mode: record.max_current_documents === null ? 'unlimited' : 'limited', maximum: record.max_current_documents === null ? '' : String(record.max_current_documents)} : {name: '', parents: [], mode: '', maximum: '', template: 'DOCUMENT_NAME'};

// BrowserRouter is not a data router: use a local, mounted-form guard rather than
// useBlocker (which requires changing App). No guard is installed for legacy cards.
export default function DocumentKinds({editable}: {editable: boolean}) {
  return <Routes><Route index element={<KindList editable={editable}/>}/>
    <Route path="new" element={editable ? <KindRoute key="new" editable/> : <Navigate to={base} replace/>}/>
    <Route path=":id" element={<KindRoute editable={editable}/>}/><Route path="*" element={<Navigate to={base} replace/>}/></Routes>;
}

function KindList({editable}: {editable: boolean}) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const q = params.get('q') || '', raw = Number(params.get('offset') || 0);
  const offset = Number.isSafeInteger(raw) && raw >= 0 && raw % 100 === 0 ? raw : 0;
  const parent = params.get('parent') || '';
  const query = new URLSearchParams({q, limit:'100', offset:String(offset), ...(parent ? {parent} : {})}).toString();
  const result = useLoad(signal => api<Page>(`${base}/search?${query}`, 'GET', undefined, signal), [query]);
  const change = (search: string, next: number, replace = false) => {
    const updated = new URLSearchParams(params);
    updated.set('q', search);
    updated.set('offset', String(next));
    setParams(updated, {replace});
  };
  const counts = result.data?.facets;
  return <><CatalogHeader title="Типы документов">{editable && <Link className="arm2-create" to={base + '/new'} state={{from: location.pathname + location.search}}>+ Новая запись</Link>}</CatalogHeader>
    <CatalogFilters items={[
      {label:'Все типы документов',count:counts?.all_parents,active:!parent,select:() => setParams({q}, {replace:true})},
      ...parents.map(key => ({label:`Для ${ {CP:'контрагентов', ORG:'организаций', VEH:'техники'}[key] }`,
        count:counts?.[key],active:parent===key,select:() => setParams({q,parent:key}, {replace:true})})),
    ]}/>
    <label className="arm2-field">Поиск по имени во всём справочнике<input value={q} onChange={event => change(event.target.value, 0, true)}/></label>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем типы документов…</p>}
    {result.data && <><p>Найдено: {result.data.matched} / Всего: {result.data.total}</p><div className="arm2-table arm2-catalog-table"><table><thead><tr><th>Имя</th><th>Постоянный ID</th><th>Применимость</th><th>Максимум актуальных документов</th><th>Версия</th></tr></thead><tbody>{result.data.items.map(row => <CatalogRow key={row.id}><td><Link to={base + '/' + encodeURIComponent(row.id)} state={{from: location.pathname + location.search}}>{row.name}</Link></td><td>{row.id}</td><td>{applicability(row.applicable_object_types)}</td><td>{multiplicity(row.max_current_documents)}</td><td>{row.version}</td></CatalogRow>)}</tbody></table></div>
      {!result.data.items.length && <p>По заданным условиям типов документов нет</p>}<nav className="arm2-actions" aria-label="Страницы типов документов"><button type="button" disabled={!offset} onClick={() => change(q, Math.max(0, offset - 100))}>Предыдущая страница</button><span>Страница {offset / 100 + 1}</span><button type="button" disabled={offset + result.data.items.length >= result.data.matched} onClick={() => change(q, offset + 100)}>Следующая страница</button></nav></>}
  </>;
}

function KindRoute({editable}: {editable: boolean}) {
  const {id = 'new'} = useParams();
  const location = useLocation();
  const from = (location.state as {from?: unknown} | null)?.from;
  const back = typeof from === 'string' && (from === base || from.startsWith(base + '?')) ? from : base;
  return id === 'new' ? <KindForm key="new" editable={editable} back={back}/> : <KindLoader key={id} id={id} editable={editable} back={back}/>;
}
function KindLoader({id, editable, back}: {id: string; editable: boolean; back: string}) {
  const result = useLoad(signal => api<DocumentKind>(base + '/' + encodeURIComponent(id), 'GET', undefined, signal), [id]);
  return <><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Открываем тип документа…</p>}{result.data ? <KindForm initial={result.data} editable={editable} back={back}/> : <Link to={back}>← К типам документов</Link>}</>;
}

function KindForm({initial, editable, back}: {initial?: DocumentKind; editable: boolean; back: string}) {
  const key = initial?.id || 'new';
  const [parked] = useState(() => parkedDrafts.get(key));
  const [original, setOriginal] = useState(parked?.original || initial), [draft, setDraft] = useState(() => parked?.draft || draftOf(initial));
  const [busy, setBusy] = useState(false), [denied, setDenied] = useState(parked?.denied || false), [error, setError] = useState<unknown>(parked?.error);
  useEffect(() => {parkedDrafts.delete(key);}, [key]);
  const [validation, setValidation] = useState(''), [status, setStatus] = useState(''), [comparison, setComparison] = useState<DocumentKind>();
  const [readError, setReadError] = useState<unknown>(), [history, setHistory] = useState(false);
  const alive = useAlive(), saving = useRef(false), navigate = useNavigate();
  const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(original));
  const guard = useDraftGuard(changed || busy, () => parkedDrafts.set(key, {original, draft, denied, error}), () => parkedDrafts.delete(key));
  const canEdit = editable && !denied;
  const change = (value: Partial<Draft>) => {guard.current = true; setDraft(current => ({...current, ...value})); setStatus(''); setValidation('');};
  const readFresh = async (reload: boolean) => {
    if (!original || (reload && !window.confirm('Перезагрузить карточку и заменить несохранённые изменения данными сервера?'))) return;
    setReadError(undefined);
    try {
      const record = await api<DocumentKind>(base + '/' + encodeURIComponent(original.id));
      if (!alive.current) return;
      if (reload) {guard.current = false; setOriginal(record); setDraft(draftOf(record)); setError(undefined); setComparison(undefined); setStatus(''); setValidation('');}
      else setComparison(record);
    } catch (error) {if (alive.current) setReadError(error);}
  };
  const save = async () => {
    if (!canEdit || saving.current) return;
    const maximum = draft.mode === 'limited' ? Number(draft.maximum) : null;
    if (!draft.name.trim()) {setValidation('Укажите имя типа документа.'); return;}
    if (!draft.parents.length) {setValidation('Выберите хотя бы один родительский справочник.'); return;}
    if (!draft.mode || (draft.mode === 'limited' && (!/^[0-9]+$/.test(draft.maximum) || !Number.isSafeInteger(maximum) || maximum! <= 0))) {setValidation('Выберите «Без ограничения» либо укажите положительное целое число.'); return;}
    const body = {name: draft.name.trim(), applicable_object_types: canonical(draft.parents), max_current_documents: maximum, file_naming_template: draft.template || null};
    saving.current = true; guard.current = true; setBusy(true); setError(undefined); setStatus(''); setValidation('');
    try {
      const record = await api<DocumentKind>(base + (original ? '/' + encodeURIComponent(original.id) : ''), original ? 'PUT' : 'POST', original ? {...body, expected_version: original.version} : body);
      if (!alive.current) return;
      guard.current = false; setOriginal(record); setDraft(draftOf(record)); setComparison(undefined);
      setStatus(original && original.version === record.version ? `Изменений нет — версия ${record.version}` : `Сохранено сервером — версия ${record.version}`);
      if (!original) navigate(base + '/' + encodeURIComponent(record.id), {replace: true, state: {from: back}});
    } catch (error) {if (alive.current) {setError(error); if (error instanceof ApiError && error.status === 403) setDenied(true);}}
    finally {saving.current = false; if (alive.current) setBusy(false);}
  };
  return <article className="arm2-document-kind"><Link to={back}>{changed ? 'Отмена / к типам документов' : '← К типам документов'}</Link><h2>{original ? 'Тип документа: ' + original.name : 'Создание типа документа'}</h2>
    {original && <p>Постоянный ID: {original.id} · Версия: {original.version}</p>}{!canEdit && <p role="status">Только просмотр — нет права редактирования</p>}
    <ErrorMessage error={error}/><ErrorMessage error={readError}/>{validation && <p role="alert" className="error">{validation}</p>}
    {error instanceof ApiError && error.status === 409 && original && <div className="arm2-actions"><button type="button" onClick={() => void readFresh(false)}>Сравнить с сервером</button><button type="button" onClick={() => void readFresh(true)}>Перезагрузить карточку</button></div>}
    {comparison && <section aria-label="Версия на сервере"><h3>Версия на сервере (черновик ниже сохранён)</h3><p>{comparison.name} · Версия: {comparison.version}</p><p>{applicability(comparison.applicable_object_types)} · {multiplicity(comparison.max_current_documents)}</p></section>}
    <form noValidate onSubmit={event => {event.preventDefault(); void save();}}><fieldset disabled={!canEdit || busy}>
      <label className="arm2-field">Шаблон имени файла<select value={draft.template} onChange={event => change({template: event.target.value as Draft['template']})}>{original && !original.file_naming_template && <option value="">Не настроен (существующий тип)</option>}<option value="DOCUMENT_NAME">Имя документа + FILE-ID</option><option value="PARENT_AND_KIND">Имя родителя + тип документа + FILE-ID</option>{(draft.name.trim().toLocaleLowerCase('ru')==='тарировка'&&draft.parents.length===1&&draft.parents[0]==='VEH'||draft.template==='VEH_CALIBRATION')&&<option value="VEH_CALIBRATION" disabled={draft.name.trim().toLocaleLowerCase('ru')!=='тарировка'||draft.parents.length!==1||draft.parents[0]!=='VEH'}>Тарировка + госномер + подтверждённый объём + FILE-ID</option>}</select></label><p className="arm2-note">Без даты. Сохранённые оригиналы не переименовываются; имя скачивания вычисляется из актуальных данных.</p>
      <label className="arm2-field">Имя типа документа *<input value={draft.name} required onChange={event => change({name: event.target.value})}/></label>
      <fieldset className="arm2-document-parents"><legend>Применимость — выберите хотя бы один справочник</legend>{parents.map(parent => <label key={parent}><input type="checkbox" checked={draft.parents.includes(parent)} onChange={event => change({parents: canonical(event.target.checked ? [...draft.parents, parent] : draft.parents.filter(value => value !== parent))})}/>{names[parent]}</label>)}</fieldset>
      <label className="arm2-field">Максимум актуальных документов *<select value={draft.mode} onChange={event => change({mode: event.target.value as Draft['mode']})}><option value="">Выберите ограничение</option><option value="unlimited">Без ограничения</option><option value="limited">Ограничено</option></select></label>
      {draft.mode === 'limited' && <label className="arm2-field">Положительное целое число *<input inputMode="numeric" value={draft.maximum} onChange={event => change({maximum: event.target.value})}/></label>}
    </fieldset>{canEdit && <button type="submit" disabled={busy}>{busy ? 'Сохраняем…' : error instanceof ApiError && error.status >= 500 ? 'Повторить сохранение' : 'Сохранить'}</button>}{status && <p role="status">{status}</p>}</form>
    {original && <div className="arm2-history-control"><button type="button" aria-expanded={history} onClick={() => setHistory(value => !value)}>{history ? 'Закрыть историю' : 'История'}</button>{history && <History type="DOC-KIND" id={original.id}/>}</div>}
  </article>;
}
