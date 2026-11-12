import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError } from '../api';
import { detail, pageSize } from './api';
import type { EventDetail, EventSummary, ObjectType, RecordData, Scalar } from './api';
import { errorText, labels } from './model';

export function useAlive() {
  const alive = useRef(true);
  useEffect(() => {alive.current = true; return () => {alive.current = false;};}, []);
  return alive;
}

export function useLoad<T>(load: (signal: AbortSignal) => Promise<T>, dependencies: unknown[]) {
  const [data, setData] = useState<T>(), [error, setError] = useState<unknown>(), [loading, setLoading] = useState(true), [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setData(undefined); setError(undefined); setLoading(true);
    load(controller.signal).then(value => { if (!controller.signal.aborted) setData(value); }).catch(e => { if (!controller.signal.aborted) setError(e); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  // The caller provides the primitive identity of the request.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, revision]);
  return { data, error, loading, reload: () => setRevision(x => x + 1) };
}
export function ErrorMessage({error, retry}: {error: unknown; retry?: () => void}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (error) ref.current?.focus(); }, [error]);
  return error ? <div ref={ref} tabIndex={-1} role="alert" className="error">{errorText(error)} {retry && <button type="button" onClick={retry}>Повторить</button>}</div> : null;
}
export function Pagination({offset, count, change}: {offset: number; count: number; change: (offset: number) => void}) {
  return <nav className="arm2-actions" aria-label="Страницы"><button type="button" disabled={offset === 0} onClick={() => change(Math.max(0, offset - pageSize))}>Предыдущая страница</button><span>Страница {offset / pageSize + 1}</span><button type="button" disabled={count < pageSize} onClick={() => change(offset + pageSize)}>Следующая страница</button></nav>;
}
const Disabled = createContext(false);
export function Field({field, value, change, options, label, required = false}: {field: string; value: Scalar | undefined; change: (value: Scalar) => void; options?: {value: string; label: string; disabled?: boolean}[]; label?: string; required?: boolean}) {
  const disabled = useContext(Disabled);
  const text = value == null ? '' : String(value);
  const pattern = field === 'bik' ? '[0-9]{9}' : ['settlement_account', 'correspondent_account'].includes(field) ? '[0-9]{20}' : undefined;
  return <label className="arm2-field"><span>{label || labels[field] || field}{required ? ' *' : ''}</span>{options ? <select disabled={disabled} value={text} onChange={e => change(e.target.value || null)} required={required}>{options.map(option => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select> : <input disabled={disabled} type={field === 'email' ? 'email' : 'text'} value={text} pattern={pattern} inputMode={pattern ? 'numeric' : undefined} title={pattern ? (field === 'bik' ? '9 цифр' : '20 цифр') : undefined} required={required} onChange={e => change(e.target.value === '' && field !== 'name' && field !== 'plate_number' ? null : e.target.value)}/>}{pattern && <small>{field === 'bik' ? '9 цифр' : '20 цифр'}, ведущие нули сохраняются</small>}</label>;
}
export function Unavailable() { return <p className="empty">Функция пока не реализована</p>; }
export function Tabs({tabs, active, setActive}: {tabs: string[]; active: string; setActive: (tab: string) => void}) {
  return <nav className="arm2-tabs" aria-label="Разделы карточки">{tabs.map(tab => <button key={tab} type="button" aria-pressed={active === tab} onClick={() => setActive(tab)}>{tab}</button>)}</nav>;
}
export function display(value: unknown): string { return value === null || value === undefined || value === '' ? 'Не заполнено' : value === true ? 'Да' : value === false ? 'Нет' : typeof value === 'object' ? JSON.stringify(value) : String(value); }
export function History({type, id}: {type: ObjectType; id: string}) {
  const [offset, setOffset] = useState(0), [eventId, setEventId] = useState<number>();
  const events = useLoad(signal => detail<EventSummary[]>(`/arm2/history/${type}/${encodeURIComponent(id)}?limit=${pageSize}&offset=${offset}`, signal), [type, id, offset]);
  return <section aria-label="История текущего объекта" className="arm2-history"><h3>История текущего объекта</h3><ErrorMessage error={events.error} retry={events.reload}/>{events.loading && <p role="status">Загружаем историю…</p>}{events.data && <><p>События только этой карточки</p>{events.data.length === 0 && <p>Событий на этой странице нет</p>}<div className="arm2-table"><table><thead><tr><th>Время</th><th>Автор</th><th>Источник</th><th>Версия</th><th>Событие</th></tr></thead><tbody>{events.data.map(event => <tr key={event.event_id}><td>{new Date(event.occurred_at).toLocaleString('ru-RU')}</td><td>{event.actor_kind} {event.actor_id}</td><td>{event.source}</td><td>{display(event.version_before)} → {display(event.version_after)}</td><td><button type="button" onClick={() => setEventId(event.event_id)}>Открыть событие {event.operation}</button></td></tr>)}</tbody></table></div><Pagination offset={offset} count={events.data.length} change={next => {setOffset(next); setEventId(undefined);}}/></>}{eventId !== undefined && <HistoryDetail key={eventId} id={eventId}/>}</section>;
}
function HistoryDetail({id}: {id: number}) {
  const event = useLoad(signal => detail<EventDetail>(`/arm2/history/events/${id}`, signal), [id]);
  return <section aria-label="Изменения события"><h4>Было → стало</h4><ErrorMessage error={event.error} retry={event.reload}/>{event.data && <div className="arm2-table"><table><thead><tr><th>Поле</th><th>Было</th><th>Стало</th></tr></thead><tbody>{Object.entries(event.data.changes).map(([field, change]) => <tr key={field}><th>{labels[field] || field}</th><td>{display(change.before)}</td><td>{display(change.after)}</td></tr>)}</tbody></table></div>}</section>;
}
export function SaveForm({resource, original, allowed, submit, reload, children, showSubmit = true}: {resource: string; original: RecordData; allowed: boolean; submit: () => Promise<void>; reload: () => void; children: (editable: boolean) => ReactNode; showSubmit?: boolean}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [denied, setDenied] = useState(false), [saved, setSaved] = useState(false), [comparison, setComparison] = useState<RecordData>();
  const mounted = useRef(true);
  useEffect(() => {mounted.current = true; return () => { mounted.current = false; };}, []);
  const editable = allowed && !denied;
  return <form onChange={() => setSaved(false)} onSubmit={async e => {e.preventDefault(); if (!editable || busy || !showSubmit) return; setBusy(true); setError(undefined); setSaved(false); try {await submit(); if (mounted.current) {setSaved(true); setComparison(undefined);}} catch (e) {if (mounted.current) {setError(e); if (e instanceof ApiError && e.status === 403) setDenied(true);}} finally {if (mounted.current) setBusy(false);}}}>
    {!editable && <p role="status">Только просмотр — нет права редактирования</p>}<ErrorMessage error={error}/>{error instanceof ApiError && error.status === 409 && original.id && <div className="arm2-actions"><button type="button" onClick={async () => {try {setComparison(await detail<RecordData>(resource));} catch (e) {setError(e);}}}>Сравнить с сервером</button><button type="button" onClick={() => {if (window.confirm('Перезагрузить карточку и заменить несохранённые изменения данными сервера?')) reload();}}>Перезагрузить карточку</button></div>}
    {comparison && <section aria-label="Версия на сервере"><h3>Версия на сервере (ваша форма ниже сохранена)</h3><dl>{Object.entries(comparison).filter(([field]) => !['id', 'version'].includes(field)).map(([field, value]) => <div key={field}><dt>{labels[field] || field}</dt><dd>{display(value)}</dd></div>)}</dl></section>}
    <Disabled.Provider value={!editable || busy}>{children(editable && !busy)}</Disabled.Provider>{editable && showSubmit && <button type="submit" disabled={busy}>{busy ? 'Сохраняем…' : 'Сохранить'}</button>}{saved && <p role="status">Сохранено сервером</p>}
  </form>;
}
