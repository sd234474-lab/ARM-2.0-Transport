import {useEffect, useRef, useState} from 'react';
import {Link} from 'react-router-dom';
import {api, ApiError} from '../api';
import type {FileBundle} from './api';
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

export function CalibrationConfirmation({vehicleId, documentId, bundle, editable, blocked}: {
  vehicleId: string; documentId: string; bundle?: FileBundle; editable: boolean; blocked: boolean;
}) {
  const path = `/arm2/veh/${vehicleId}/documents/${documentId}`;
  const parked = useDraftCache<{volume: string; checkedRevision?: string; key: string}>();
  useEffect(() => {parked.delete(path);}, [parked, path]);
  const state = useLoad(signal => api<CalibrationState>(`/arm2/veh/${vehicleId}/calibration`, 'GET', undefined, signal), [path, bundle?.document_version]);
  const [volume, setVolume] = useState(parked.get(path)?.volume ?? '');
  const [checkedRevision, setCheckedRevision] = useState(parked.get(path)?.checkedRevision);
  const [key, setKey] = useState(parked.get(path)?.key ?? crypto.randomUUID());
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [message, setMessage] = useState('');
  const inFlight = useRef(false), alive = useAlive();
  const guard = useDraftGuard(!!volume || !!checkedRevision || busy, () => parked.set(path, {volume, checkedRevision, key}), () => parked.delete(path));
  const current = bundle?.revisions.find(revision => revision.id === bundle.current_revision_id);
  const checked = !!current && checkedRevision === current.id;
  const clear = () => {
    guard.current = false;
    parked.delete(path);
    setVolume('');
    setCheckedRevision(undefined);
    setKey(crypto.randomUUID());
    setError(undefined);
  };
  const change = (value: string) => {setVolume(value); setKey(crypto.randomUUID()); setMessage('');};
  const submit = async () => {
    if (!editable || blocked || !checked || !current?.files.length || !bundle || !state.data || inFlight.current) return;
    if (!Number.isFinite(Number(volume)) || Number(volume) <= 0) {setError(new ApiError(422, 'Введите положительный объём')); return;}
    inFlight.current = true; setBusy(true); setError(undefined);
    try {
      const result = await api<{no_op: boolean}>(`/arm2/veh/${vehicleId}/calibration/confirm`, 'POST', {command_id: key,
        vehicle_version: state.data.vehicle_version, document_id: documentId, document_version: bundle.document_version,
        revision_id: current.id, plate_number: state.data.plate_number, volume_m3: Number(volume)});
      if (alive.current) {clear(); setMessage(result.no_op ? 'Тарировка уже подтверждена — изменений нет' : 'Тарировка подтверждена'); state.reload();}
    } catch (error) {if (alive.current) setError(error);}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  if (!current?.files.length) return null;
  return <section className="arm2-calibration-confirmation" aria-label="Подтверждение объёма">
    <ErrorMessage error={state.error} retry={state.reload}/><ErrorMessage error={error}/>
    <div className="arm2-calibration-line">
      {state.data && <div className="arm2-calibration-context">
        <strong>{state.data.volume_m3 == null ? 'Объём не подтверждён' : `Подтверждено: ${state.data.volume_m3} м³`}</strong>
        <span>Госномер: {state.data.plate_number}</span>
      </div>}
      {editable && <fieldset className="arm2-calibration-controls" disabled={blocked || busy || !state.data}>
        <label>Объём кузова, м³<input type="number" min="0" step="any" value={volume} onChange={e => change(e.target.value)}/></label>
        <label className="arm2-calibration-check"><input type="checkbox" checked={checked} onChange={e => {setCheckedRevision(e.target.checked ? current.id : undefined); setKey(crypto.randomUUID());}}/>Сверил файл и госномер</label>
        <button type="button" disabled={!checked || !volume} onClick={() => void submit()}>{busy ? 'Подтверждаем…' : 'Подтвердить объём'}</button>
        {(volume || checkedRevision) && <button type="button" onClick={clear}>Отмена</button>}
      </fieldset>}
    </div>
    {message && <p role="status">{message}</p>}
  </section>;
}
