import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ApiError } from '../api';
import { apiUrl } from '../workspace';
import { labels } from './model';
import { ErrorMessage, HistoryDetail } from './shared';

const base = '/arm2/bulk';
const columns: Record<string, string> = {id: 'VEH-ID', plate_number: 'Госномер', name: 'Рабочее имя', model: 'Модель', type_id: 'TYPE — ID', type_name: 'TYPE — наименование', owner_kind: 'Владелец — категория CP/ORG', owner_id: 'Владелец — ID', owner_name: 'Владелец — наименование', owner_cp_id: 'Владелец CP — ID', owner_org_id: 'Владелец ORG — ID', accounting_state: 'Учётное состояние', wialon_unit_id: 'Wialon ID', version: 'Техническая версия (служебная)'};
const clearable: Record<string, string> = {type_id: 'Тип техники', owner: 'Операционный владелец CP/ORG', model: 'Модель', wialon_unit_id: 'Wialon ID'};
type Option = {value: unknown; label: string; current: unknown; current_label: string; original: unknown; has_original: boolean; error: string | null; conflict: boolean; clear: boolean; sources: number[]};
type Plan = {key: string; vehicle_id?: string; sources: number[]; action: string; errors: string[]; has_snapshot?: boolean; matched?: {name: string; plate_number: string}; similar?: {id: string; plate: string}[]; fields: Record<string, Option[]>};
type Outcome = {command_id: string; row_key: string; vehicle_id: string; event_id: number | null; version_after: number; status: string; fields: string[]; at: string};
export type BulkWorkspace = {id: string; source: string; revision: number; headers: string[]; rows: string[][]; mapping: string[]; excluded: number[]; clears: Record<string, string[]>; decisions: Record<string, Record<string, number>>; confirmations: Record<string, Record<string, boolean>>; warning: string; failures?: Record<string, {row_key: string; status: number; message: string}>; plans: Plan[]; preview_id: string; results: Outcome[]; blocked: boolean; approval_reset: boolean};
type Workspace = BulkWorkspace;
type Session = {id: string; source: string; revision: number; created_at: string};
type Protocol = {id: string; source: string; author: number; results: Outcome[]};
type Cleanup = {id: string; source: string; author: number; created_at: string};
const draftBody = (w: Workspace, revision: number) => ({revision, ...Object.fromEntries(['headers', 'rows', 'mapping', 'excluded', 'clears', 'decisions', 'confirmations'].map(k => [k, w[k as keyof Workspace]]))});

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function download(path: string, filename: string) {
  const response = await fetch(apiUrl(path), {credentials: 'same-origin'});
  if (!response.ok) {const error = await response.json().catch(() => ({detail: 'Не удалось скачать файл'})); throw new ApiError(response.status, error.detail);}
  saveBlob(await response.blob(), filename);
}
export function VehicleExport({query}: {query: string}) {
  const [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  return <><button type="button" disabled={busy} onClick={async () => {setBusy(true); setError(undefined); try {const p = new URLSearchParams(query); p.delete('offset'); p.delete('limit'); await download(`${base}/vehicles/export?${p}`, 'vehicles.xlsx');} catch (e) {setError(e);} finally {setBusy(false);}}}>Выгрузить в Excel</button><ErrorMessage error={error}/></>;
}

function Results({results}: {results: Outcome[]}) {
  const [event, setEvent] = useState<number>();
  const committed = results.filter(r => r.status === 'SAVED');
  return <section aria-label="Фактические результаты"><h3>Фактические результаты</h3><p>Сохранено VEH: {new Set(committed.map(r => r.vehicle_id)).size} · Записей HIST: {committed.length} · Без изменений: {results.filter(r => r.status === 'SKIP').length} · Неопределённо: {results.filter(r => r.status === 'UNKNOWN').length}</p>
    <ul>{results.map(r => <li key={r.command_id}>{r.status} · <Link to={`/arm2/veh/${r.vehicle_id}`}>{r.vehicle_id}</Link> · версия {r.version_after} · {r.fields.map(f => labels[f] || clearable[f] || f).join(', ')} · {r.event_id ? <button onClick={() => setEvent(r.event_id!)}>HIST {r.event_id}</button> : 'Без нового HIST'} · {new Date(r.at).toLocaleString('ru-RU')}</li>)}</ul>{event && <HistoryDetail id={event}/>}</section>;
}

export default function BulkVehicles({admin = false}: {admin?: boolean}) {
  const [params, setParams] = useSearchParams();
  const [sessions, setSessions] = useState<Session[]>([]), [protocols, setProtocols] = useState<Protocol[]>([]), [cleanup, setCleanup] = useState<Cleanup[]>();
  const [current, setCurrent] = useState<Workspace>(), [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false), [pending, setPending] = useState(0);
  const [paste, setPaste] = useState(''), [file, setFile] = useState<File>(), [sheets, setSheets] = useState<string[]>([]), [sheet, setSheet] = useState('');
  const [selected, setSelected] = useState<number[]>([]), [batchColumn, setBatchColumn] = useState('0'), [batchValue, setBatchValue] = useState(''), [clearField, setClearField] = useState('type_id');
  const [reportReady, setReportReady] = useState(false), [recovery, setRecovery] = useState(false), [message, setMessage] = useState('');
  const state = useRef(current), revision = useRef(0), queue = useRef(Promise.resolve()), failed = useRef(false), alive = useRef(true);
  const sessionId = params.get('session');
  useEffect(() => {alive.current = true; return () => {alive.current = false;};}, []);
  const accept = (value: Workspace) => {state.current = value; revision.current = value.revision; setCurrent(value); setRecovery(false); failed.current = false;};
  const refreshList = async () => {const [s, p] = await Promise.all([api<Session[]>(`${base}/sessions`), api<Protocol[]>(`${base}/protocols`)]); if (alive.current) {setSessions(s); setProtocols(p);}};
  useEffect(() => {refreshList().catch(setError);}, []);
  useEffect(() => {
    if (!sessionId || state.current?.id === sessionId) return;
    let active = true;
    setBusy(true); setError(undefined);
    api<Workspace>(`${base}/sessions/${encodeURIComponent(sessionId)}`).then(w => {if (active) {accept(w); setReportReady(false); setSelected([]);}}).catch(e => {if (active) setError(e);}).finally(() => {if (active) setBusy(false);});
    return () => {active = false;};
  }, [sessionId]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {if (pending || failed.current) {e.preventDefault(); e.returnValue = '';}};
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [pending]);

  const edit = (changes: Partial<Workspace>, data = false) => {
    const previous = state.current;
    if (!previous) return;
    const snapshot = {...previous, ...changes, ...(data ? {decisions: {}, confirmations: {}} : {})};
    state.current = snapshot; setCurrent(snapshot); setPending(n => n + 1); setReportReady(false);
    queue.current = queue.current.then(async () => {
      if (failed.current) return;
      const w = await api<Workspace>(`${base}/sessions/${snapshot.id}`, 'PUT', draftBody(snapshot, revision.current));
      revision.current = w.revision;
      if (alive.current && state.current === snapshot) accept(w);
    }).catch(e => {failed.current = true; if (alive.current) {setError(e); setRecovery(true);}}).finally(() => {if (alive.current) setPending(n => n - 1);});
  };
  const open = async (id: string) => {await queue.current; if (failed.current && !window.confirm('Локальные правки не сохранены. Открыть серверный набор и отбросить локальные правки?')) return; state.current = undefined; setCurrent(undefined); setParams({session: id}); if (id === sessionId) {setBusy(true); try {accept(await api<Workspace>(`${base}/sessions/${id}`));} catch (e) {setError(e);} finally {setBusy(false);}}};
  const recheck = async () => {await queue.current; if (failed.current && !window.confirm('Не все локальные правки сохранены. Открыть сохранённую серверную таблицу?')) return; const id = state.current?.id; if (!id) return; setBusy(true); try {accept(await api<Workspace>(`${base}/sessions/${id}`)); setError(undefined); setReportReady(false);} catch (e) {setError(e);} finally {setBusy(false);}};
  const upload = async (source: Blob, filename: string, chosen?: string) => {
    setBusy(true); setError(undefined);
    try {
      const p = new URLSearchParams({filename}); if (chosen) p.set('sheet', chosen);
      const result = await api<Workspace | {sheets: string[]}>(`${base}/sessions?${p}`, 'POST', source);
      if ('sheets' in result) {setSheets(result.sheets); setSheet(result.sheets[0] || '');}
      else {accept(result); setParams({session: result.id}); setSheets([]); setSelected([]); setReportReady(false); setMessage('Таблица загружена в серверный черновик. Записей VEH ещё не создано'); await refreshList();}
    } catch (e) {setError(e);} finally {setBusy(false);}
  };
  const decide = (key: string, field: string, index: number) => {
    const w = state.current!;
    const fields = {...w.decisions[key]}; if (index < 0) delete fields[field]; else fields[field] = index;
    edit({decisions: {...w.decisions, [key]: fields}});
  };
  const chooseValid = () => {
    const w = state.current!, decisions = {...w.decisions};
    w.plans.filter(p => !p.errors.length && (!selected.length || p.sources.some(s => selected.includes(s-2)))).forEach(p => {
      const fields = {...decisions[p.key]}; Object.entries(p.fields).forEach(([f, options]) => {if (options.length === 1 && !options[0].error && !options[0].conflict) fields[f] = 0;}); decisions[p.key] = fields;
    }); edit({decisions});
  };
  const apply = async () => {
    await queue.current; const w = state.current; if (!w || failed.current) return;
    const plans = w.plans.filter(p => Object.keys(w.decisions[p.key] || {}).some(f => f in p.fields) && (!selected.length || p.sources.some(s => selected.includes(s-2))));
    if (!plans.length) {setMessage('Сначала выберите изменения полей'); return;}
    setBusy(true); setError(undefined); let failure: unknown;
    for (const p of plans) {
      const fields = Object.fromEntries(Object.entries(w.decisions[p.key]).filter(([f]) => f in p.fields));
      try {await api<Outcome>(`${base}/sessions/${w.id}/apply`, 'POST', {command_id: crypto.randomUUID(), preview_id: w.preview_id, row_key: p.key, fields, confirm_owner: w.confirmations[p.key]?.owner || false, confirm_plate: w.confirmations[p.key]?.plate_number || false});}
      catch (e) {failure = e; if (!(e instanceof ApiError) || e.status >= 500 || e.status === 403 || e.status === 401) {setRecovery(true); break;}}
    }
    try {accept(await api<Workspace>(`${base}/sessions/${w.id}`)); await refreshList();} catch (e) {setRecovery(true); failure = e;}
    setError(failure); setMessage('Проверьте фактические результаты ниже. Непринятые поля остаются в черновике'); setBusy(false); setReportReady(false);
  };
  const finish = async (cancel: boolean) => {
    const w = state.current; if (!w) return;
    if (!window.confirm(cancel ? 'Удалить временный набор? Уже сохранённые VEH и история останутся.' : 'Завершить работу и удалить временную таблицу? Уже сохранённые записи останутся.')) return;
    setBusy(true); setError(undefined);
    try {const result = await api<{message: string}>(`${base}/sessions/${w.id}/finish`, 'POST', {revision: w.revision, cancel}); state.current = undefined; setCurrent(undefined); setParams({}); setMessage(result.message); await refreshList();} catch (e) {setError(e);} finally {setBusy(false);}
  };
  const locked = busy || !!pending || recovery || failed.current;
  return <article className="arm2-bulk"><Link to="/arm2/veh">← К технике</Link><h2>Табличный обмен — техника</h2><p>Один лист XLSX, CSV, TSV или вставка из Excel. До 1000 строк, 32 столбцов, 5 МиБ. Пустая ячейка ничего не очищает. Запись выполняется только по кнопке подтверждения.</p>
    <ErrorMessage error={error}/>{message && <p role="status">{message}</p>}
    <section aria-label="Источник таблицы"><h3>Загрузить таблицу</h3><input aria-label="Файл таблицы" type="file" accept=".xlsx,.csv,.tsv" disabled={busy || !!pending} onChange={e => {const f = e.target.files?.[0]; setFile(f); setSheets([]); if (f) upload(f, f.name);}}/>
      {sheets.length > 0 && <><label>Лист<select value={sheet} onChange={e => setSheet(e.target.value)}>{sheets.map(s => <option key={s}>{s}</option>)}</select></label><button disabled={busy} onClick={() => file && upload(file, file.name, sheet)}>Открыть выбранный лист</button></>}
      <label>Вставка из Excel (первая строка — заголовки)<textarea value={paste} onChange={e => setPaste(e.target.value)} rows={3}/></label><button disabled={busy || !!pending || !paste.trim()} onClick={() => upload(new Blob([paste], {type: 'text/tab-separated-values'}), 'paste.tsv')}>Открыть вставленную таблицу</button></section>
    <section><h3>Мои незавершённые загрузки</h3><ul>{sessions.map(s => <li key={s.id}><button disabled={busy || !!pending} onClick={() => open(s.id)}>{s.source} · {new Date(s.created_at).toLocaleString('ru-RU')}</button></li>)}</ul></section>
    {current && <section aria-label="Рабочий набор"><h3>{current.source}</h3><p>{current.warning}</p><p role="status">{pending ? 'Сохраняем правки на сервере…' : failed.current ? 'Есть несохранённые локальные правки' : 'Черновик сохранён на сервере; доступен только вам'}</p>{current.approval_reset && <p role="alert">Сравнение изменилось. Выбор полей сброшен; проверьте и подтвердите заново.</p>}
      {current.blocked && <p role="alert">Есть неопределённые результаты. Повторное применение и завершение заблокированы до проверки.</p>}
      <button disabled={busy || !!pending} onClick={recheck}>Проверить таблицу и фактические результаты</button>
      <div className="arm2-actions"><button disabled={locked} onClick={() => setSelected(current.rows.map((_, i) => i))}>Выделить все строки</button><button disabled={locked} onClick={() => setSelected([])}>Снять выделение</button><button disabled={locked || !selected.length} onClick={() => edit({excluded: [...new Set([...current.excluded, ...selected])]}, true)}>Исключить выделенные строки</button><button disabled={locked || !current.excluded.length} onClick={() => edit({excluded: []}, true)}>Вернуть исключённые строки</button></div>
      <div className="arm2-actions"><label>Столбец<select value={batchColumn} onChange={e => setBatchColumn(e.target.value)}>{current.headers.map((h, i) => <option value={i} key={i}>{h || `Столбец ${i+1}`}</option>)}</select></label><label>Значение для выделенных строк<input value={batchValue} onChange={e => setBatchValue(e.target.value)}/></label><button disabled={locked || !selected.length} onClick={() => edit({rows: current.rows.map((r, i) => selected.includes(i) ? r.map((v, c) => c === Number(batchColumn) ? batchValue : v) : r)}, true)}>Задать значение</button>
        <label>Явная очистка<select value={clearField} onChange={e => setClearField(e.target.value)}>{Object.entries(clearable).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select></label><button disabled={locked || !selected.length} onClick={() => edit({clears: {...current.clears, ...Object.fromEntries(selected.map(i => [i, [...new Set([...(current.clears[i] || []), clearField])]]))}}, true)}>CLEAR выделенным строкам</button><button disabled={locked || !selected.length} onClick={() => edit({clears: Object.fromEntries(Object.entries(current.clears).filter(([i]) => !selected.includes(Number(i))))}, true)}>Отменить CLEAR выделенным</button></div>
      <div className="arm2-table"><table><thead><tr><th>Выбор / строка</th>{current.headers.map((header, c) => <th key={c}>{header || `Столбец ${c+1}`}<label>Укажите реквизит<select aria-label={`Укажите реквизит: ${c+1}`} value={current.mapping[c]} disabled={busy} onChange={e => edit({mapping: current.mapping.map((v, i) => i === c ? e.target.value : v)}, true)}><option value="">Не загружать</option>{Object.entries(columns).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label></th>)}<th>CLEAR</th></tr></thead><tbody>{current.rows.map((row, i) => <tr key={i} className={current.excluded.includes(i) ? 'arm2-bulk-excluded' : ''}><td><input type="checkbox" aria-label={`Выделить строку ${i+2}`} checked={selected.includes(i)} onChange={e => setSelected(e.target.checked ? [...selected, i] : selected.filter(v => v !== i))}/>{i+2}{current.excluded.includes(i) && ' · исключена'}</td>{row.map((value, c) => <td key={c}><input aria-label={`Строка ${i+2}, столбец ${c+1}`} value={value} maxLength={1000} disabled={busy} onChange={e => edit({rows: current.rows.map((r, j) => j === i ? r.map((v, k) => k === c ? e.target.value : v) : r)}, true)}/></td>)}<td>{(current.clears[i] || []).map(f => clearable[f]).join(', ')}</td></tr>)}</tbody></table></div>
      <h3>Сравнение с ARM</h3><p>Источники — номера строк. CREATE требует принятия госномера; имя по умолчанию равно номеру, учётное состояние — «Действующая». Спорные поля выбирайте отдельно. Невыбранное останется без изменения.</p>
      <button disabled={locked || current.blocked} onClick={chooseValid}>Принять проверенные поля {selected.length ? 'выделенных VEH' : 'всего набора'}</button>
      {current.plans.map(p => <section className="arm2-bulk-plan" key={p.key} aria-label={`Сравнение ${p.key}`}><h4>{p.action} · {p.vehicle_id || 'Новая VEH'} · строки {p.sources.join(', ')} {p.matched && `· ${p.matched.name} · ${p.matched.plate_number}`}</h4><p>{p.has_snapshot ? 'Есть подписанный исходный снимок ARM' : 'Строка без снимка: принимаются только явно выбранные отличия'}</p>{p.errors.map(e => <p role="alert" key={e}>{e}</p>)}{p.similar && p.similar.length > 0 && <p>Похожие номера (не объединяются): {p.similar.map(s => `${s.plate} (${s.id})`).join(', ')}</p>}
        {Object.entries(p.fields).map(([field, options]) => <div className="arm2-bulk-diff" key={field}><strong>{labels[field] || clearable[field] || field}</strong><p>ARM: {options[0].current_label}</p>{options.some(o => o.has_original) && <p>Исходное: {JSON.stringify(options.find(o => o.has_original)?.original)}</p>}
          {options.map((o, i) => <p key={i} className={o.error || o.conflict ? 'arm2-bulk-warning' : ''}>Excel: {o.clear ? 'CLEAR → пусто' : o.label} · строки {o.sources.join(', ')} {o.error || (o.conflict ? '· конфликт: выберите значение либо оставьте ARM' : '')}</p>)}
          <label>Решение: {labels[field] || clearable[field] || field}<select disabled={locked || !!p.errors.length} value={current.decisions[p.key]?.[field] ?? -1} onChange={e => decide(p.key, field, Number(e.target.value))}><option value={-1}>Оставить ARM / не принимать</option>{options.map((o, i) => <option value={i} key={i} disabled={!!o.error}>{o.clear ? 'CLEAR → пусто' : o.label}{o.conflict ? ' — явно разрешить конфликт' : ''}</option>)}</select></label>
          {((field === 'owner' && options[0].current) || (field === 'plate_number' && p.vehicle_id)) && <label><input type="checkbox" disabled={locked} checked={current.confirmations[p.key]?.[field] || false} onChange={e => edit({confirmations: {...current.confirmations, [p.key]: {...current.confirmations[p.key], [field]: e.target.checked}}})}/>Отдельно подтверждаю {options[0].current_label} → {options[current.decisions[p.key]?.[field] ?? 0]?.label} {field === 'owner' ? '(владелец)' : '(госномер)'}</label>}
        </div>)}</section>)}
      <button className="primary" disabled={locked || current.blocked} onClick={apply}>Сохранить выбранные изменения</button>
      {Object.entries(current.failures || {}).map(([id, f]) => <p role="alert" key={id}>{f.row_key}: {f.status} · {f.message}</p>)}<Results results={current.results}/>
      <div className="arm2-actions"><button disabled={locked || current.blocked} onClick={async () => {setBusy(true); try {await download(`${base}/sessions/${current.id}/report`, 'vehicle-results.xlsx'); setReportReady(true);} catch (e) {setError(e);} finally {setBusy(false);}}}>Скачать отказы в Excel</button><button disabled={locked || current.blocked || !reportReady} onClick={() => finish(false)}>Завершить и удалить временный набор</button><button disabled={locked || current.blocked || (!!current.results.length && !reportReady)} onClick={() => finish(true)}>Отменить загрузку</button></div><p>Сначала скачайте отчёт. Отмена после частичного сохранения не является откатом.</p>
    </section>}
    <section><h3>Мои постоянные протоколы</h3>{protocols.map(p => <details key={p.id}><summary>{p.source} · автор {p.author} · {p.id}</summary><Results results={p.results}/></details>)}</section>
    {admin && <section><h3>Административная очистка</h3><button disabled={busy || !!pending} onClick={async () => {try {setCleanup(await api<Cleanup[]>(`${base}/cleanup`));} catch (e) {setError(e);}}}>Показать временные наборы для очистки</button>{cleanup?.map(s => <p key={s.id}>{s.source} · автор {s.author} · {s.id} <button disabled={busy || !!pending} onClick={async () => {if (!window.confirm(`Удалить только временный набор ${s.id}, автор ${s.author}? Принятые VEH и HIST сохранятся.`)) return; setBusy(true); try {await api(`${base}/cleanup/${s.id}`, 'POST', {confirm_session: s.id}); setCleanup(list => list?.filter(item => item.id !== s.id)); if (current?.id === s.id) {state.current = undefined; setCurrent(undefined); setParams({});} await refreshList();} catch (e) {setError(e);} finally {setBusy(false);}}}>Очистить</button></p>)}<p>Администратор может удалить заброшенный временный набор с аудитом, но не продолжить чужую загрузку.</p></section>}
  </article>;
}
