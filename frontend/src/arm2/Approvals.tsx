import {useEffect, useRef, useState} from 'react';
import {Link, useNavigate, useParams} from 'react-router-dom';
import {api, ApiError} from '../api';
import {apiUrl} from '../workspace';
import type {CalibrationState} from './Calibration';
import {openFilePreview} from './filePreviewWindow';
import {ErrorMessage, History, Pagination, useAlive, useLoad} from './shared';
import {useDraftCache, useDraftGuard} from './useDraftGuard';

interface Decision {id: string; vehicle_id: string; plate_number: string; volume_m3: number; state: string; version: number; response_file_id: string | null; decided_at: string | null; decided_by: number | null}
interface Approval {id: string; version: number; contract_number_1: string; contract_number_2: string; created_at: string; author_id: number; letter_file_id: string; package_file_id: string; response_file_ids: string[]; decisions: Decision[]}
const endpoint = '/arm2/approvals';
const stateName = (value: string) => ({PENDING: 'Ожидает ответа', APPROVED: 'Согласовано', REJECTED: 'Отклонено'}[value] ?? value);
export default function Approvals({editable}: {editable: boolean}) {
  const [offset, setOffset] = useState(0);
  const result = useLoad(signal => api<Approval[]>(endpoint + '?offset=' + offset, 'GET', undefined, signal), [offset]);
  return <section><header className="arm2-actions"><h2>Согласование с заказчиком</h2>{editable && <Link to="/arm2/approvals/new">Новое письмо</Link>}</header><ErrorMessage error={result.error} retry={result.reload}/>{result.data && <><ul>{result.data.map(row => <li key={row.id}><Link to={'/arm2/approvals/' + row.id}>{row.id} · {row.contract_number_1} / {row.contract_number_2}</Link> · {new Date(row.created_at).toLocaleString('ru-RU')} · Машин: {row.decisions.length}</li>)}</ul>{!result.data.length && <p>Заявок на этой странице нет</p>}<Pagination offset={offset} count={result.data.length} change={setOffset}/></>}</section>;
}

interface LetterDraft {first: string; second: string; selection: CalibrationState[]; key: string}
export function NewApproval() {
  const cache = useDraftCache<LetterDraft>();
  useEffect(() => {cache.delete("approval:letter");}, [cache]);
  const [draft, setDraft] = useState<LetterDraft>(() => cache.get("approval:letter") ?? {first: '', second: '', selection: [], key: crypto.randomUUID()});
  const [offset, setOffset] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>();
  const inFlight = useRef(false), alive = useAlive(), navigate = useNavigate();
  const dirty = !!draft.first || !!draft.second || !!draft.selection.length || busy;
  const guard = useDraftGuard(dirty, () => {cache.set("approval:letter", draft);}, () => {cache.delete("approval:letter");});
  const result = useLoad(signal => api<CalibrationState[]>(endpoint + '/candidates?offset=' + offset, 'GET', undefined, signal), [offset]);
  const change = (next: Partial<LetterDraft>) => {setDraft({...draft, ...next, key: crypto.randomUUID()}); setError(undefined);};
  const submit = async () => {
    if (inFlight.current || !draft.first.trim() || !draft.second.trim() || !draft.selection.length) return;
    inFlight.current = true; setBusy(true); setError(undefined);
    try {
      const value = await api<{request_id: string}>(endpoint + '/letter', 'POST', {command_id: draft.key,
        contract_number_1: draft.first, contract_number_2: draft.second,
        vehicles: draft.selection.map(row => ({vehicle_id: row.vehicle_id, vehicle_version: row.vehicle_version}))});
      if (alive.current) {guard.current = false; cache.delete("approval:letter"); navigate('/arm2/approvals/' + value.request_id);}
    } catch (error) {if (alive.current) setError(error);}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  return <section><Link to="/arm2/approvals">← К заявкам</Link><h2>Одно письмо по двум контрактам</h2><ErrorMessage error={error}/><ErrorMessage error={result.error} retry={result.reload}/><fieldset disabled={busy}>
    <div className="arm2-grid"><label>Первый номер контракта *<input maxLength={160} value={draft.first} onChange={e => change({first: e.target.value})}/></label><label>Второй номер контракта *<input maxLength={160} value={draft.second} onChange={e => change({second: e.target.value})}/></label></div>
    <p>Оба номера войдут в шапку одного письма. Решение заказчика принимается один раз по каждой машине и её характеристикам.</p>
    {result.data?.map(row => <div className="arm2-actions" key={row.vehicle_id}><label><input type="checkbox" disabled={!row.letter_eligible || (draft.selection.length >= 100 && !draft.selection.some(v => v.vehicle_id === row.vehicle_id))} checked={draft.selection.some(v => v.vehicle_id === row.vehicle_id)} onChange={e => change({selection: e.target.checked ? [...draft.selection, row] : draft.selection.filter(v => v.vehicle_id !== row.vehicle_id)})}/>{row.name} · {row.plate_number} · {row.volume_m3 ?? '—'} м³</label>{row.document_id && <Link to={`/arm2/veh/${row.vehicle_id}/documents/${row.document_id}`}>Тарировка</Link>}{!row.letter_eligible && <span>{row.reasons.join('; ')}</span>}</div>)}
    {result.data && <Pagination offset={offset} count={result.data.length} change={setOffset}/>}
    <p>Выбрано: {draft.selection.length}. Это допуск к письму; полная готовность к работе здесь не подтверждается.</p>
    <button type="button" disabled={!draft.first.trim() || !draft.second.trim() || !draft.selection.length} onClick={() => void submit()}>{busy ? 'Выпускаем письмо и комплект…' : error ? 'Повторить ту же команду' : 'Выпустить PDF и ZIP'}</button>
    <button type="button" onClick={() => {guard.current = false; cache.delete("approval:letter"); setDraft({first: '', second: '', selection: [], key: crypto.randomUUID()});}}>Очистить выбор</button>
  </fieldset></section>;
}

interface ReplyDraft {file?: File; stageId: string; key: string; choices: Record<string, string>}
export function ApprovalRoute({editable}: {editable: boolean}) {
  const {id = ''} = useParams();
  const result = useLoad(signal => api<Approval>(endpoint + '/' + id, 'GET', undefined, signal), [id]);
  return <><Link to="/arm2/approvals">← К заявкам</Link><ErrorMessage error={result.error} retry={result.reload}/>{result.data && <ApprovalCard key={id + ':' + result.data.version} row={result.data} editable={editable} reload={result.reload}/>}</>;
}
function ApprovalCard({row, editable, reload}: {row: Approval; editable: boolean; reload: () => void}) {
  const path = endpoint + '/' + row.id;
  const replies = useDraftCache<ReplyDraft>();
  useEffect(() => {replies.delete("approval:reply:" + row.id);}, [replies, row.id]);
  const [draft, setDraft] = useState<ReplyDraft>(() => replies.get("approval:reply:" + row.id) ?? {stageId: crypto.randomUUID(), key: crypto.randomUUID(), choices: {}});
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [history, setHistory] = useState(false);
  const inFlight = useRef(false), alive = useAlive();
  const guard = useDraftGuard(!!draft.file || !!Object.keys(draft.choices).length || busy, () => replies.set("approval:reply:" + row.id, draft), () => replies.delete("approval:reply:" + row.id));
  const previewReply = async () => {
    if (!draft.file || inFlight.current) return;
    if (draft.file.size > 2 * 1024 * 1024) {setError(new ApiError(413, 'Ответ должен быть не больше 2 МиБ')); return;}
    inFlight.current = true; setBusy(true); setError(undefined);
    let preview: ReturnType<typeof openFilePreview> | undefined;
    try {
      preview = openFilePreview();
      await api(path + '/response-stages/' + draft.stageId + '?name=' + encodeURIComponent(draft.file.name), 'PUT', draft.file);
      preview.navigate(path + '/response-stages/' + draft.stageId + '/preview', draft.file.name);
    } catch (error) {preview?.close(); if (alive.current) setError(error);}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  const submit = async () => {
    if (inFlight.current || !editable || !draft.file || !Object.keys(draft.choices).length) return;
    if (draft.file.size > 2 * 1024 * 1024) {setError(new ApiError(413, 'Ответ должен быть не больше 2 МиБ')); return;}
    inFlight.current = true; setBusy(true); setError(undefined);
    try {
      await api(path + '/response-stages/' + draft.stageId + '?name=' + encodeURIComponent(draft.file.name), 'PUT', draft.file);
      await api(path + '/response', 'POST', {command_id: draft.key, expected_version: row.version, stage_id: draft.stageId,
        decisions: row.decisions.filter(d => draft.choices[d.vehicle_id]).map(d => ({vehicle_id: d.vehicle_id, expected_version: d.version, state: draft.choices[d.vehicle_id]}))});
      if (alive.current) {guard.current = false; replies.delete("approval:reply:" + row.id); setDraft({stageId: crypto.randomUUID(), key: crypto.randomUUID(), choices: {}}); reload();}
    } catch (error) {if (alive.current) setError(error);}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  const file = (id: string, label: string, preview = true) => <span className="arm2-actions" key={id}>{preview && <button type="button" onClick={() => openFilePreview(`${path}/files/${id}/preview`, label)}>{label}</button>}<a href={apiUrl(`${path}/files/${id}/download`)} download>{preview ? 'Скачать оригинал' : label}</a></span>;
  return <section><h2>{row.id}</h2><p>Контракты: {row.contract_number_1} / {row.contract_number_2} · Автор {row.author_id} · {new Date(row.created_at).toLocaleString('ru-RU')} · Версия {row.version}</p>
    <div className="arm2-actions">{file(row.letter_file_id, 'Открыть письмо')}{file(row.package_file_id, 'Скачать ZIP со справками', false)}</div>
    <ErrorMessage error={error}/><fieldset disabled={!editable || busy}><table><thead><tr><th>Машина / госномер</th><th>Объём</th><th>Текущее решение</th><th>Принять из ответа</th></tr></thead><tbody>{row.decisions.map(d => <tr key={d.id}><td><Link to={'/arm2/veh/' + d.vehicle_id}>{d.vehicle_id} · {d.plate_number}</Link></td><td>{d.volume_m3} м³</td><td>{stateName(d.state)}{d.decided_at && <small> · {new Date(d.decided_at).toLocaleString('ru-RU')} · Автор {d.decided_by}</small>}</td><td><select aria-label={'Решение ' + d.plate_number} value={draft.choices[d.vehicle_id] ?? ''} onChange={e => {const choices = {...draft.choices}; if (e.target.value) choices[d.vehicle_id] = e.target.value; else delete choices[d.vehicle_id]; setDraft({...draft, choices, key: crypto.randomUUID()});}}><option value="">Не изменять</option><option value="APPROVED">Согласовано</option><option value="REJECTED">Отклонено</option></select></td></tr>)}</tbody></table>
    <label>Ответ заказчика (PDF/JPG/PNG, до 2 МиБ)<input type="file" accept=".pdf,.jpg,.jpeg,.png" onChange={e => setDraft({...draft, file: e.target.files?.[0], stageId: crypto.randomUUID(), key: crypto.randomUUID()})}/></label>{draft.file && <p>{draft.file.name}</p>}
    <button type="button" disabled={!draft.file} onClick={() => void previewReply()}>Проверить оригинал ответа</button>
    <button type="button" disabled={!draft.file || !Object.keys(draft.choices).length} onClick={() => void submit()}>{busy ? 'Сохраняем ответ…' : error ? 'Повторить ту же команду' : 'Сохранить ответ и решения'}</button>
    <button type="button" onClick={() => {guard.current = false; replies.delete("approval:reply:" + row.id); setDraft({stageId: crypto.randomUUID(), key: crypto.randomUUID(), choices: {}});}}>Отменить выбор</button>
    </fieldset>{row.response_file_ids.map(id => file(id, 'Открыть ответ заказчика'))}
    <button type="button" onClick={() => setHistory(!history)}>История</button>{history && <><History type="APPROVAL" id={row.id}/>{row.decisions.map(d => <details key={d.id}><summary>История решения {d.plate_number}</summary><History type="APPROVAL-ITEM" id={d.id}/></details>)}</>}
  </section>;
}
