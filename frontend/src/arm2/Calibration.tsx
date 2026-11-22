import {useEffect, useRef, useState} from 'react';
import {Link} from 'react-router-dom';
import {api, ApiError} from '../api';
import type {DocumentRecord, FileBundle} from './api';
import {openFilePreview} from './filePreviewWindow';
import {ErrorMessage, useAlive, useLoad} from './shared';
import {useDraftCache, useDraftGuard} from './useDraftGuard';

export interface CalibrationState {
  vehicle_id: string; name: string; plate_number: string; volume_m3: number | null;
  vehicle_version: number; document_id: string | null; document_version: number | null;
  revision_id: string | null; file_ids: string[]; confirmed_at: string | null; confirmed_by: number | null;
  letter_eligible: boolean; reasons: string[]; customer: {state: string | null; request_id: string | null};
  work_readiness_notice: string;
}
const stateName = (state: string | null) => state === 'APPROVED' ? 'Согласовано' : state === 'REJECTED' ? 'Отклонено' : state === 'PENDING' ? 'Ожидает ответа' : 'Нет решения';

export function CalibrationSummary({vehicleId}: {vehicleId: string}) {
  const result = useLoad(signal => api<CalibrationState>(`/arm2/veh/${vehicleId}/calibration`, 'GET', undefined, signal), [vehicleId]);
  return <section aria-label="Подтверждённая тарировка"><h3>Подтверждённая тарировка</h3><ErrorMessage error={result.error} retry={result.reload}/>{result.data && <>
    <p>Объём: <strong>{result.data.volume_m3 == null ? 'Не подтверждён' : `${result.data.volume_m3} м³`}</strong> · Дата: {result.data.confirmed_at ? new Date(result.data.confirmed_at).toLocaleString('ru-RU') : '—'} · Автор: {result.data.confirmed_by ?? '—'}</p>
    {result.data.document_id && <Link to={`/arm2/veh/${vehicleId}/documents/${result.data.document_id}`}>Открыть источник тарировки</Link>}
    <p>К письму: {result.data.letter_eligible ? 'Допущена' : result.data.reasons.join('; ')}. Заказчик: {stateName(result.data.customer.state)}.</p>
    {result.data.customer.request_id && <Link to={`/arm2/approvals/${result.data.customer.request_id}`}>Открыть согласование</Link>}
    <p className="arm2-note">{result.data.work_readiness_notice}</p>
  </>}</section>;
}

export function CalibrationConfirmation({vehicleId, document, editable, blocked}: {vehicleId: string; document: DocumentRecord; editable: boolean; blocked: boolean}) {
  const path = `/arm2/veh/${vehicleId}/documents/${document.id}`;
  const parked = useDraftCache<{volume: string; checked: boolean; key: string}>();
  useEffect(() => {parked.delete(path);}, [parked, path]);
  const state = useLoad(async signal => ({ready: await api<CalibrationState>(`/arm2/veh/${vehicleId}/calibration`, 'GET', undefined, signal),
    bundle: await api<FileBundle>(path + '/files?history=false', 'GET', undefined, signal)}), [path, document.version]);
  const [volume, setVolume] = useState(parked.get(path)?.volume ?? ''), [checked, setChecked] = useState(parked.get(path)?.checked ?? false);
  const [key, setKey] = useState(parked.get(path)?.key ?? crypto.randomUUID());
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [message, setMessage] = useState('');
  const inFlight = useRef(false), alive = useAlive();
  const guard = useDraftGuard(!!volume || checked || busy, () => parked.set(path, {volume, checked, key}), () => parked.delete(path));
  const current = state.data?.bundle.revisions.find(r => r.id === state.data?.bundle.current_revision_id);
  const change = (value: string) => {setVolume(value); setKey(crypto.randomUUID()); setMessage('');};
  const submit = async () => {
    if (!editable || blocked || !checked || !current || !state.data || inFlight.current) return;
    if (!Number.isFinite(Number(volume)) || Number(volume) <= 0) {setError(new ApiError(422, 'Введите положительный объём')); return;}
    inFlight.current = true; setBusy(true); setError(undefined);
    try {
      const result = await api<{no_op: boolean}>(`/arm2/veh/${vehicleId}/calibration/confirm`, 'POST', {command_id: key,
        vehicle_version: state.data.ready.vehicle_version, document_id: document.id, document_version: state.data.bundle.document_version,
        revision_id: current.id, plate_number: state.data.ready.plate_number, volume_m3: Number(volume)});
      if (alive.current) {guard.current = false; parked.delete(path); setVolume(''); setChecked(false); setKey(crypto.randomUUID()); setMessage(result.no_op ? 'Тарировка уже подтверждена — изменений нет' : 'Тарировка подтверждена'); state.reload();}
    } catch (error) {if (alive.current) setError(error);}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  return <section aria-label="Подтверждение объёма"><h3>Подтвердить объём по оригиналу</h3><ErrorMessage error={state.error} retry={state.reload}/><ErrorMessage error={error}/>
    <p>Проверьте оригинал, госномер и объём. Новая редакция сама по себе не подтверждает объём и не меняет решение заказчика.</p>
    <div className="arm2-actions">{current?.files.map(file => <button type="button" key={file.id} onClick={() => openFilePreview(`${path}/revisions/${current.id}/files/${file.id}/preview`, file.stored_name)}>Открыть {file.original_upload_name}</button>)}</div>
    {state.data && <p>Госномер: {state.data.ready.plate_number} · Подтверждённый объём: {state.data.ready.volume_m3 ?? '—'} м³</p>}
    <fieldset disabled={!editable || blocked || busy || !current?.files.length}><div className="arm2-actions"><label>Объём кузова, м³<input type="number" min="0" step="any" value={volume} onChange={e => change(e.target.value)}/></label><label><input type="checkbox" checked={checked} onChange={e => {setChecked(e.target.checked); setKey(crypto.randomUUID());}}/>Сверил оригинал и госномер</label><button type="button" disabled={!checked || !volume} onClick={() => void submit()}>{busy ? 'Подтверждаем…' : 'Подтвердить тарировку'}</button><button type="button" onClick={() => {guard.current = false; parked.delete(path); setVolume(''); setChecked(false); setError(undefined);}}>Отмена</button></div></fieldset>
    {message && <p role="status">{message}</p>}
  </section>;
}
