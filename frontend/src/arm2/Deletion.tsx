import { createContext, useContext, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../api';
import type { ObjectType } from './api';
import type { Can } from './Workspace';
import { CatalogHeader, ErrorMessage, History, Pagination, useAlive, useLoad } from './shared';

export const DeletionPermissions = createContext<Can>(() => false);

const endpoint = '/arm2/deletion';
type ObjectKey = {object_type: ObjectType; object_id: string};
type Mark = ObjectKey & {name: string; path: string; mark: string | null; version: number};
type Dependency = {target_id: string; purpose: 'ACTIVE_DOMAIN' | 'OPEN_OPERATION' | 'HISTORY' | 'OWNED' | 'UNKNOWN';
  object_id?: string; name?: string; path?: string; at?: string; blocking: boolean; reason: string; selectable?: Mark};
type Preview = {id: string; objects: Record<string, Mark>; dependencies: Dependency[]; can_execute: boolean};
type Cleanup = {object_id: string; state: string; failure?: {message: string}};
type Receipt = {id: string; state: 'CHECKED' | 'COMPLETED' | 'FILES_PENDING' | 'FILES_BLOCKED' | 'RESTRICTED'; executed_at: string; objects: Mark[]; cleanup: Cleanup[]; can_retry: boolean};
type Page<T> = {items: T[]; has_more: boolean};
const markUrl = (kind: string, id: string) => `${endpoint}/marks/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
const saveMark = (kind: string, id: string, version: number, previous: string | null, marked: boolean) =>
  api<Mark>(markUrl(kind, id), 'PUT', {expected_version: version, expected_mark: previous, marked});

export function DeletionMark({kind, id, version, editable}: {kind: string; id?: string; version?: number; editable: boolean}) {
  const can = useContext(DeletionPermissions);
  return id && version && editable && can('arm2_delete_mark')
    ? <MarkControl kind={kind} id={id} version={version}/> : null;
}

function MarkControl({kind, id, version}: {kind: string; id: string; version: number}) {
  const path = markUrl(kind, id);
  const result = useLoad(signal => api<Mark>(path, 'GET', undefined, signal), [path, version]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const alive = useAlive();
  const change = async () => {
    if (!result.data) return;
    setBusy(true);
    setError(null);
    try {
      await saveMark(kind, id, version, result.data.mark, !result.data.mark);
      if (alive.current) result.reload();
    } catch (failure) {
      if (alive.current) { setError(failure); result.reload(); }
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  return <span className="arm2-deletion-mark">
    {result.data && <button type="button" disabled={busy || result.loading} aria-pressed={!!result.data.mark}
      onClick={() => void change()}>{result.data.mark ? 'Снять пометку удаления' : 'Пометить на удаление'}</button>}
    <ErrorMessage error={error || result.error} retry={result.reload}/>
  </span>;
}

export function DeletionNavigation() {
  const can = useContext(DeletionPermissions);
  return can('arm2_delete_mark') ? <div className="arm2-actions"><Link to="/arm2/deletion">Удаление объектов</Link></div> : null;
}

const keyOf = (object: ObjectKey) => `${object.object_type}:${object.object_id}`;
const labelOf = (row: Mark) => row.name === row.object_id ? row.object_id : `${row.name} · ${row.object_id}`;
const groups: {label: string; filter: (item: Dependency) => boolean}[] = [
  {label: 'Блокирующие связи', filter: item => item.blocking},
  {label: 'История сохраняется', filter: item => !item.blocking && item.purpose === 'HISTORY'},
  {label: 'Обрабатываются в составе набора', filter: item => !item.blocking && item.purpose !== 'HISTORY'},
];
const states = {
  CHECKED: 'Набор проверен',
  RESTRICTED: 'Текущие права ограничивают доступ к составу набора',
  COMPLETED: 'Удаление завершено',
  FILES_PENDING: 'Записи удалены. Очистка файлов продолжается',
  FILES_BLOCKED: 'Записи удалены. Часть файлов требует сверки хранилища',
};

export default function Deletion() {
  const can = useContext(DeletionPermissions);
  const [offset, setOffset] = useState(0);
  const [journalOffset, setJournalOffset] = useState(0);
  const [selected, setSelected] = useState<Record<string, Mark>>({});
  const [preview, setPreview] = useState<Preview>();
  const [showReceipt, setShowReceipt] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const marks = useLoad(signal => api<Page<Mark>>(`${endpoint}/marks?offset=${offset}`, 'GET', undefined, signal), [offset]);
  const journal = useLoad(signal => api<Page<Receipt>>(`${endpoint}/batches?offset=${journalOffset}`, 'GET', undefined, signal), [journalOffset]);
  const selection = Object.values(selected);
  const change = (row: Mark, checked: boolean) => {
    setPreview(undefined);
    setSelected(previous => {
      const next = {...previous};
      if (checked) next[keyOf(row)] = row;
      else delete next[keyOf(row)];
      return next;
    });
  };
  const check = async () => {
    setBusy(true);
    setError(null);
    setPreview(undefined);
    try {
      setPreview(await api<Preview>(endpoint + '/preview', 'POST',
        {objects: selection.map(({object_id, object_type}) => ({object_id, object_type}))}));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const add = async (row: Mark) => {
    setBusy(true);
    setError(null);
    try {
      change(row.mark ? row : await saveMark(row.object_type, row.object_id, row.version, null, true), true);
    } catch (failure) {
      setError(failure);
    } finally {
      marks.reload();
      setBusy(false);
    }
  };
  const execute = async () => {
    if (!preview?.can_execute || !window.confirm(`Физически удалить выбранные записи (${selection.length})?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api(`${endpoint}/batches/${preview.id}/execute`, 'POST', {confirmed: true});
      setShowReceipt(preview.id);
      setSelected({});
      marks.reload();
      journal.reload();
    } catch (failure) {
      setError(failure);
      // The batch receipt resolves a lost answer without executing a second removal.
      setShowReceipt(preview.id);
      marks.reload();
      journal.reload();
    } finally {
      setPreview(undefined);
      setBusy(false);
    }
  };
  return <section className="arm2-deletion">
    <CatalogHeader title="Удаление объектов">
      <button type="button" className="arm2-create" onClick={() => void check()}
        disabled={busy || !selection.length}>Проверить набор · {selection.length}</button>
    </CatalogHeader>
    <ErrorMessage error={error}/>
    <ErrorMessage error={marks.error} retry={marks.reload}/>
    {marks.loading && <p role="status">Загружаем помеченные объекты…</p>}
    {marks.data && (marks.data.items.length ? <div className="arm2-table"><table>
      <thead><tr><th>Выбор</th><th>Объект</th><th>Тип</th><th>Версия</th></tr></thead>
      <tbody>{marks.data.items.map(row => <tr key={keyOf(row)}><td><input type="checkbox"
        aria-label={`Выбрать ${labelOf(row)}`} checked={!!selected[keyOf(row)]}
        disabled={busy || (selection.length >= 100 && !selected[keyOf(row)])}
        onChange={event => change(row, event.target.checked)}/></td><td><Link to={row.path}>{labelOf(row)}</Link></td>
        <td>{row.object_type}</td><td>{row.version}</td></tr>)}</tbody>
    </table></div> : <p>Помеченных объектов нет.</p>)}
    {!!(offset || marks.data?.has_more) && <Pagination offset={offset} count={marks.data?.has_more ? 100 : 0} change={setOffset}/>}
    {!!selection.length && <p>В наборе {selection.length} <button type="button" disabled={busy} onClick={() => {
      setSelected({}); setPreview(undefined);
    }}>Снять выбор</button></p>}
    {preview && <section aria-label="Проверка набора">
      <h3>Результат проверки</h3>
      <p role="status">{preview.dependencies.some(item => item.blocking)
        ? 'В наборе есть блокирующие связи.' : 'Блокирующих связей нет.'}</p>
      {Object.values(preview.objects).map(object => {
        const dependencies = preview.dependencies.filter(item => item.target_id === object.object_id);
        return <details key={object.object_id} open={dependencies.length > 0}>
          <summary>{labelOf(object)} · связей {dependencies.length}</summary>
          {dependencies.length ? groups.map(group => {
            const rows = dependencies.filter(group.filter);
            if (!rows.length) return null;
            return <section key={group.label} aria-label={group.label}>
              <h4>{group.label}</h4>
              <ul>{rows.map((item, index) => {
                const label = item.name ?? item.object_id ?? 'Связь';
                const available = item.selectable;
                return <li key={index}>
                  {item.path ? <Link to={item.path} target="_blank" rel="noopener noreferrer"
                    className="arm2-deletion-link">{label}</Link> : label}
                  {item.at && <> · <time dateTime={item.at}>{new Date(item.at).toLocaleString('ru-RU')}</time></>}
                  {' · '}{item.reason}
                  {available && can('arm2_delete_mark') && <> <button type="button"
                    disabled={busy || selection.length >= 100} onClick={() => void add(available)}>
                    {available.mark ? 'Добавить в набор' : 'Пометить и добавить в набор'}
                  </button></>}
                </li>;
              })}</ul>
            </section>;
          }) : <p>Связей нет.</p>}
        </details>;
      })}
      {can('arm2_delete_execute') && preview.can_execute && <button type="button" disabled={busy}
        onClick={() => void execute()}>Подтвердить удаление</button>}
    </section>}
    {showReceipt && <DeletionReceipt key={showReceipt} id={showReceipt} close={() => setShowReceipt(undefined)}/>}
    <h3>Журнал удаления</h3>
    <ErrorMessage error={journal.error} retry={journal.reload}/>
    {journal.data && (journal.data.items.length ? <ul>{journal.data.items.map(item => <li key={item.id}>
      <button type="button" onClick={() => setShowReceipt(item.id)}>
        {new Date(item.executed_at).toLocaleString('ru-RU')} · {item.objects.length}
      </button>
    </li>)}</ul> : <p>Удалённых наборов пока нет.</p>)}
    {!!(journalOffset || journal.data?.has_more) && <Pagination offset={journalOffset}
      count={journal.data?.has_more ? 100 : 0} change={setJournalOffset}/>}
  </section>;
}

function DeletedHistory({row}: {row: Mark}) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{labelOf(row)} · История</summary>
    {open && <History type={row.object_type} id={row.object_id}/>}
  </details>;
}

function DeletionReceipt({id, close}: {id: string; close: () => void}) {
  const can = useContext(DeletionPermissions);
  const result = useLoad(signal => api<Receipt>(`${endpoint}/batches/${id}`, 'GET', undefined, signal), [id]);
  const {reload, data} = result;
  const [error, setError] = useState<unknown>(null);
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    if (!data?.cleanup.some(row => row.state === 'QUEUED' || row.state === 'RUNNING')) return;
    const timer = window.setTimeout(reload, 4000);
    return () => window.clearTimeout(timer);
  }, [data, reload]);
  const retry = async () => {
    setRetrying(true);
    setError(null);
    try { await api(`${endpoint}/batches/${id}/retry`, 'POST'); reload(); }
    catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && failure.status === 403) reload();
    }
    finally { setRetrying(false); }
  };
  return <section aria-label="Протокол удаления">
    <CatalogHeader title="Протокол удаления"><button type="button" onClick={close}>Закрыть протокол</button></CatalogHeader>
    <ErrorMessage error={error || result.error} retry={reload}/>
    {data && <><p role="status">{states[data.state]}</p>
      {data.state === 'CHECKED' && <p>База данных не зафиксировала удаление этого набора.</p>}
      {data.state !== 'CHECKED' && data.objects.map(row => <DeletedHistory key={row.object_id} row={row}/>)}
      {!!data.cleanup.length && <ul>{data.cleanup.map((row, index) => <li key={index}>
        {row.object_id} · {row.failure?.message ?? (row.state === 'SUCCEEDED' ? 'Убран из защищённого хранилища' : 'Ожидает очистки')}
      </li>)}</ul>}
      {data.can_retry && can('arm2_delete_execute') &&
        <button type="button" disabled={retrying} onClick={() => void retry()}>Повторить сверку файлов</button>}
      <button type="button" onClick={reload}>Обновить состояние</button>
    </>}
  </section>;
}
