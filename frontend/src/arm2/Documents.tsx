import { useContext, useEffect, useRef, useState } from 'react';
import { Link, UNSAFE_NavigationContext, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api';
import { catalogs } from './api';
import type { DocumentKind, DocumentParent, DocumentRecord, RecordData } from './api';
import type { Can } from './Workspace';
import { ErrorMessage, History, Pagination, useAlive, useLoad } from './shared';

interface Scope {parentType: DocumentParent; parentId: string; can: Can}
const endpoint = (type: DocumentParent, id: string) => `/arm2/${type.toLowerCase()}/${encodeURIComponent(id)}/documents`;
const parentLink = (type: DocumentParent, id: string) => `/arm2/${type.toLowerCase()}/${encodeURIComponent(id)}?tab=documents`;
const view = ({parentType, can}: Pick<Scope, 'parentType' | 'can'>) => can(catalogs[parentType].permission + '_view') && can('arm2_document_view');
const edit = (scope: Pick<Scope, 'parentType' | 'can'>) => view(scope) && scope.can(catalogs[scope.parentType].permission + '_edit') && scope.can('arm2_document_edit');

export default function Documents(scope: Scope) {
  return view(scope) ? <DocumentList {...scope}/> : <p>Нет прав просмотра документов этой карточки.</p>;
}
function DocumentList(scope: Scope) {
  const {parentType, parentId} = scope;
  const [archive, setArchive] = useState('non_archived'), [offset, setOffset] = useState(0);
  const path = endpoint(parentType, parentId);
  const result = useLoad(signal => api<DocumentRecord[]>(`${path}?${new URLSearchParams({archive, limit: '100', offset: String(offset)})}`, 'GET', undefined, signal), [path, archive, offset]);
  return <section aria-label="Документы родителя"><header className="arm2-actions"><h3>Документы</h3>{edit(scope) && <Link to={path + '/new'}>Добавить документ</Link>}</header>
    <label className="arm2-field">Архивность<select value={archive} onChange={event => {setArchive(event.target.value); setOffset(0);}}><option value="non_archived">Неархивные</option><option value="archived">Архивные</option><option value="all">Все</option></select></label>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем документы…</p>}
    {result.data && <><div className="arm2-table"><table><thead><tr><th>Тип документа</th><th>Наименование документа</th><th>Состояние документа</th></tr></thead><tbody>{result.data.map(row => <tr key={row.id}><td>{row.kind_name}</td><td><Link to={path + '/' + encodeURIComponent(row.id)}>{row.name}</Link></td><td>{row.display_state}</td></tr>)}</tbody></table></div>
      {!result.data.length && <p>Документов по заданным условиям нет</p>}<Pagination offset={offset} count={result.data.length} change={setOffset}/></>}
  </section>;
}

export function DocumentRoute({parentType, can}: {parentType: DocumentParent; can: Can}) {
  const {parentId = '', id = ''} = useParams();
  const scope = {parentType, parentId, can};
  if (!view(scope) || (id === 'new' && !edit(scope))) return <p>Нет прав для этой операции с документом.</p>;
  return <DocumentLoader key={parentType + ':' + parentId + ':' + id} {...scope} id={id}/>;
}
function DocumentLoader({id, ...scope}: Scope & {id: string}) {
  const path = endpoint(scope.parentType, scope.parentId), back = parentLink(scope.parentType, scope.parentId);
  const result = useLoad(async signal => ({
    parent: await api<RecordData>(`${catalogs[scope.parentType].path}/${encodeURIComponent(scope.parentId)}`, 'GET', undefined, signal),
    document: id === 'new' ? undefined : await api<DocumentRecord>(path + '/' + encodeURIComponent(id), 'GET', undefined, signal),
  }), [path, id]);
  return <><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Открываем документ…</p>}{result.data ? <DocumentForm {...scope} parent={result.data.parent} initial={result.data.document}/> : <Link to={back}>← К документам родителя</Link>}</>;
}

interface Draft {kind_ref: string; name: string; description: string | null}
interface Parked {original?: DocumentRecord; draft: Draft; denied: boolean; error: unknown}
const parkedDrafts = new Map<string, Parked>();
const draftOf = (record?: DocumentRecord): Draft => ({kind_ref: record?.kind_ref || '', name: record?.name || '', description: record?.description ?? null});

function useDraftGuard(blocked: boolean, park: () => void, discard: () => void) {
  const {navigator} = useContext(UNSAFE_NavigationContext);
  const location = useLocation();
  const dirty = useRef(blocked); dirty.current = blocked;
  const callbacks = useRef({park, discard}); callbacks.current = {park, discard};
  const index = useRef<number | undefined>(window.history.state?.idx);
  useEffect(() => {index.current = window.history.state?.idx;}, [location.key]);
  useEffect(() => {
    const mountedHref = window.location.href;
    const leave = () => !dirty.current || window.confirm('Покинуть карточку и потерять несохранённые изменения?');
    const push = navigator.push, replace = navigator.replace, go = navigator.go;
    const guardedPush: typeof push = (...args) => {if (leave()) {dirty.current = false; push.apply(navigator, args);}};
    const guardedReplace: typeof replace = (...args) => {if (leave()) {dirty.current = false; replace.apply(navigator, args);}};
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



function KindPicker({path, value, change, can}: {path: string; value: string; change: (id: string) => void; can: Can}) {
  const [q, setQ] = useState(''), [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<DocumentKind>();
  const result = useLoad(signal => api<DocumentKind[]>(`${path}/kinds?${new URLSearchParams({q, limit: '100', offset: String(offset)})}`, 'GET', undefined, signal), [path, q, offset]);
  const options = result.data || [];
  return <section aria-label="Выбор типа документа">
    <label className="arm2-field">Поиск доступного типа документа<input value={q} maxLength={200} onChange={event => {setQ(event.target.value); setOffset(0);}}/></label>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем доступные типы…</p>}
    <label className="arm2-field">Тип документа *<select value={value} required onChange={event => {const next = event.target.value; setSelected(options.find(option => option.id === next)); change(next);}}><option value="">Выберите существующий тип</option>
      {value && !options.some(option => option.id === value) && <option value={value}>{selected?.id === value ? selected.name : value}</option>}{options.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}</select></label>
    {result.data && <><Pagination offset={offset} count={result.data.length} change={setOffset}/>{!options.length && <p>Подходящих типов на этой странице нет. Измените поиск или создайте применимый тип в справочнике.</p>}</>}
    {can('arm2_doc_kind_view') && <Link to="/arm2/document-kinds">Открыть справочник типов документов</Link>}
  </section>;
}

function DocumentForm({initial, parent, ...scope}: Scope & {initial?: DocumentRecord; parent: RecordData}) {
  const path = endpoint(scope.parentType, scope.parentId), back = parentLink(scope.parentType, scope.parentId);
  const key = path + '/' + (initial?.id || 'new');
  const [parked] = useState(() => parkedDrafts.get(key));
  const [original, setOriginal] = useState(parked?.original || initial), [draft, setDraft] = useState(() => parked?.draft || draftOf(initial));
  const [busy, setBusy] = useState(false), [denied, setDenied] = useState(parked?.denied || false), [error, setError] = useState<unknown>(parked?.error);
  useEffect(() => {parkedDrafts.delete(key);}, [key]);
  const [validation, setValidation] = useState(''), [status, setStatus] = useState(''), [comparison, setComparison] = useState<DocumentRecord>();
  const [readError, setReadError] = useState<unknown>(), [history, setHistory] = useState(false);
  const alive = useAlive(), saving = useRef(false), navigate = useNavigate();
  const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(original));
  const guard = useDraftGuard(changed || busy, () => parkedDrafts.set(key, {original, draft, denied, error}), () => parkedDrafts.delete(key));
  const canEdit = edit(scope) && !denied;
  const change = (value: Partial<Draft>) => {guard.current = true; setDraft(current => ({...current, ...value})); setStatus(''); setValidation('');};
  const readFresh = async (reload: boolean) => {
    if (!original || (reload && !window.confirm('Перезагрузить карточку и заменить несохранённые изменения данными сервера?'))) return;
    setReadError(undefined);
    try {
      const record = await api<DocumentRecord>(path + '/' + encodeURIComponent(original.id));
      if (!alive.current) return;
      if (reload) {guard.current = false; setOriginal(record); setDraft(draftOf(record)); setError(undefined); setComparison(undefined); setStatus(''); setValidation('');}
      else setComparison(record);
    } catch (error) {if (alive.current) setReadError(error);}
  };
  const save = async () => {
    if (!canEdit || saving.current) return;
    if (!draft.name.trim()) {setValidation('Укажите наименование документа.'); return;}
    if (!draft.kind_ref) {setValidation('Выберите существующий тип документа.'); return;}
    const body = {name: draft.name.trim(), description: draft.description};
    saving.current = true; guard.current = true; setBusy(true); setError(undefined); setStatus(''); setValidation('');
    try {
      const record = await api<DocumentRecord>(path + (original ? '/' + encodeURIComponent(original.id) : ''), original ? 'PUT' : 'POST', original ? {...body, expected_version: original.version} : {...body, kind_ref: draft.kind_ref});
      if (!alive.current) return;
      guard.current = false; setOriginal(record); setDraft(draftOf(record)); setComparison(undefined);
      setStatus(original && original.version === record.version ? `Изменений нет — версия ${record.version}` : `Сохранено сервером — версия ${record.version}`);
      if (!original) navigate(path + '/' + encodeURIComponent(record.id), {replace: true});
    } catch (error) {if (alive.current) {setError(error); if (error instanceof ApiError && error.status === 403) setDenied(true);}}
    finally {saving.current = false; if (alive.current) setBusy(false);}
  };
  return <article className="arm2-document"><Link to={back}>{changed ? 'Отмена / к документам родителя' : '← К документам родителя'}</Link><h2>{original ? 'Документ: ' + original.name : 'Добавить документ'}</h2>
    <p>Родитель: {parent.name} · {scope.parentType} · {scope.parentId}</p><p>Документ сохраняется без файла. Добавление файлов будет доступно на следующем этапе.</p>
    {original && <><p>Постоянный ID: {original.id} · Версия: {original.version}</p><p>Тип документа: {original.kind_name} · {original.kind_ref}</p><p>Архивность: {original.archive_state} · Состояние документа: {original.display_state}</p></>}{!canEdit && <p role="status">Только просмотр — нет права редактирования</p>}
    <ErrorMessage error={error}/><ErrorMessage error={readError}/>{validation && <p role="alert" className="error">{validation}</p>}
    {error instanceof ApiError && error.status === 409 && original && <div className="arm2-actions"><button type="button" onClick={() => void readFresh(false)}>Сравнить с сервером</button><button type="button" onClick={() => void readFresh(true)}>Перезагрузить карточку</button></div>}
    {comparison && <section aria-label="Версия на сервере"><h3>Версия на сервере (черновик ниже сохранён)</h3><p>{comparison.name} · Версия: {comparison.version}</p><p>{comparison.description ?? 'Описание не заполнено'}</p></section>}
    <form noValidate onSubmit={event => {event.preventDefault(); void save();}}><fieldset disabled={!canEdit || busy}>
      {!original && <KindPicker path={path} value={draft.kind_ref} change={kind_ref => change({kind_ref})} can={scope.can}/>}
      <label className="arm2-field">Наименование документа *<input value={draft.name} required onChange={event => change({name: event.target.value})}/></label>
      <label className="arm2-field">Описание документа<textarea rows={4} value={draft.description ?? ''} onChange={event => change({description: event.target.value})}/></label>
    </fieldset>{canEdit && <button type="submit" disabled={busy}>{busy ? 'Сохраняем…' : error instanceof ApiError && error.status >= 500 ? 'Повторить сохранение' : 'Сохранить'}</button>}{status && <p role="status">{status}</p>}</form>
    {original && <div className="arm2-history-control"><button type="button" aria-expanded={history} onClick={() => setHistory(value => !value)}>{history ? 'Закрыть историю' : 'История'}</button>{history && <History type="DOCUMENT" id={original.id}/>}</div>}
  </article>;
}
