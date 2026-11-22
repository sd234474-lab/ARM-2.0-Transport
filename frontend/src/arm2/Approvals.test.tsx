// @vitest-environment jsdom
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Route, Routes} from 'react-router-dom';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {api, ApiError} from '../api';
import {ApprovalRoute, NewApproval} from './Approvals';
import {CalibrationConfirmation, CalibrationSummary} from './Calibration';
import {openFilePreview} from './filePreviewWindow';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
vi.mock('./filePreviewWindow', () => ({openFilePreview: vi.fn()}));
let host: HTMLDivElement, root: ReturnType<typeof createRoot>;
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const ready = {vehicle_id: 'VEH-000001', name: '679HOWO', plate_number: 'Е679РН186', volume_m3: 25,
  vehicle_version: 2, document_id: 'DOCUMENT-000001', document_version: 2, revision_id: 'DOCUMENT-REV-000001',
  file_ids: ['FILE-000001'], confirmed_at: '2026-11-22T08:00:00Z', confirmed_by: 1,
  letter_eligible: true, reasons: [], customer: {state: 'APPROVED', request_id: 'APPROVAL-000001'},
  work_readiness_notice: 'Полный допуск к работе не подтверждён'};
const request = {id: 'APPROVAL-000001', version: 1, contract_number_1: 'К-1', contract_number_2: 'К-2',
  author_id: 1, created_at: '2026-11-22T08:00:00Z', letter_file_id: 'FILE-000002', package_file_id: 'FILE-000003',
  response_file_ids: [], decisions: [
    {id: 'APPROVAL-ITEM-000001', vehicle_id: 'VEH-000001', plate_number: 'Е679РН186', volume_m3: 25, state: 'PENDING', version: 1},
    {id: 'APPROVAL-ITEM-000002', vehicle_id: 'VEH-000002', plate_number: 'А001АА186', volume_m3: 27, state: 'PENDING', version: 1}]};
beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  vi.mocked(api).mockReset(); vi.mocked(openFilePreview).mockReset();
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(async () => {await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks();});
async function mount(element: React.ReactNode, path = '/new') {
  await act(async () => {root.render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/new" element={element}/><Route path="/arm2/approvals/:id" element={element}/></Routes></MemoryRouter>); await tick();});
}
async function change(input: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {Object.getOwnPropertyDescriptor(input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(input, value); input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', {bubbles: true})); await tick();});
}
async function click(text: string) {await act(async () => {[...host.querySelectorAll('button')].find(b => b.textContent === text)!.click(); await tick();});}

it('takes eligibility only from the server and sends one letter with two header numbers; retries the same command', async () => {
  const commands: unknown[] = [];
  vi.mocked(api).mockImplementation(async (path, method, body) => {
    if (path.includes('/candidates')) return [ready, {...ready, vehicle_id: 'VEH-000002', name: 'Не готова', letter_eligible: false, reasons: ['Нет подтверждения']}];
    if (method === 'POST') {commands.push(body); throw new ApiError(503, 'Повторите ту же команду');}
    return request;
  });
  await mount(<NewApproval/>);
  expect(host.textContent).toContain('Нет подтверждения');
  const checks = host.querySelectorAll<HTMLInputElement>('input[type=checkbox]');
  expect(checks[1].disabled).toBe(true);
  const inputs = host.querySelectorAll<HTMLInputElement>('input:not([type=checkbox])');
  await change(inputs[0], 'К-1'); await change(inputs[1], 'К-2');
  await act(async () => {checks[0].click(); await tick();});
  await click('Выпустить PDF и ZIP'); await click('Повторить ту же команду');
  expect(commands[1]).toEqual(commands[0]);
  expect(commands[0]).toMatchObject({contract_number_1: 'К-1', contract_number_2: 'К-2', vehicles: [{vehicle_id: 'VEH-000001', vehicle_version: 2}]});
  expect(commands[0]).not.toHaveProperty('contract_ids');
});

it('saves explicit per-machine decisions with one bounded original and reopens through the platform viewer', async () => {
  let saved = false;
  vi.mocked(api).mockImplementation(async (path, method) => {
    if (method === 'PUT') return {stage_id: 'stage'};
    if (method === 'POST') {saved = true; return {request_id: request.id};}
    return {...request, version: saved ? 2 : 1, response_file_ids: saved ? ['FILE-000004'] : [],
      decisions: request.decisions.map((row, i) => ({...row, state: saved ? ['APPROVED', 'REJECTED'][i] : 'PENDING'}))};
  });
  await mount(<ApprovalRoute editable/>, '/arm2/approvals/APPROVAL-000001');
  const choices = host.querySelectorAll('select'); await change(choices[0], 'APPROVED'); await change(choices[1], 'REJECTED');
  const file = new File(['%PDF-test'], 'Ответ.pdf', {type: 'application/pdf'});
  await act(async () => {const input = host.querySelector<HTMLInputElement>('input[type=file]')!; Object.defineProperty(input, 'files', {value: [file]}); input.dispatchEvent(new Event('change', {bubbles: true})); await tick();});
  await click('Сохранить ответ и решения');
  const sent = vi.mocked(api).mock.calls.find(call => call[0].endsWith('/response') && call[1] === 'POST')!;
  expect(sent[2]).toMatchObject({expected_version: 1, decisions: [
    {vehicle_id: 'VEH-000001', expected_version: 1, state: 'APPROVED'},
    {vehicle_id: 'VEH-000002', expected_version: 1, state: 'REJECTED'}]});
  expect(host.textContent).toContain('Согласовано'); expect(host.textContent).toContain('Отклонено');
  await click('Открыть ответ заказчика');
  expect(openFilePreview).toHaveBeenCalledWith('/arm2/approvals/APPROVAL-000001/files/FILE-000004/preview', 'Открыть ответ заказчика');
  expect(host.querySelector('iframe')).toBeNull();
});

it('shows volume as read-only and confirms only after the operator checks an explicit current original', async () => {
  const document = {id: 'DOCUMENT-000001', version: 2, name: 'Тарировка', kind_name: 'Тарировка', kind_ref: 'DOC-KIND-000001',
    parent_type: 'VEH' as const, parent_id: ready.vehicle_id, description: null, archive_state: 'Неархивный' as const, display_state: 'Доступен' as const};
  vi.mocked(api).mockImplementation(async (path, method) => {
    if (method === 'POST') return {no_op: false};
    if (path.includes('/files?')) return {current_revision_id: ready.revision_id, document_version: 2, revisions: [{id: ready.revision_id, files: [{id: 'FILE-000001', original_upload_name: 'Справка.pdf'}]}]};
    return ready;
  });
  await mount(<><CalibrationSummary vehicleId={ready.vehicle_id}/><CalibrationConfirmation vehicleId={ready.vehicle_id} document={document} editable blocked={false}/></>);
  expect(host.textContent).toContain('25 м³'); expect(host.textContent).toContain('Полный допуск');
  await click('Открыть Справка.pdf');
  expect(openFilePreview).toHaveBeenCalledOnce();
  const button = [...host.querySelectorAll('button')].find(b => b.textContent === 'Подтвердить тарировку')!;
  expect(button.disabled).toBe(true);
  await change(host.querySelector<HTMLInputElement>('input[type=number]')!, '27');
  await act(async () => {host.querySelector<HTMLInputElement>('input[type=checkbox]')!.click(); await tick();});
  await click('Подтвердить тарировку');
  expect(vi.mocked(api).mock.calls.find(call => call[1] === 'POST')?.[2]).toMatchObject({volume_m3: 27, plate_number: 'Е679РН186',
    vehicle_version: 2, document_id: document.id, document_version: 2, revision_id: ready.revision_id});
  expect(host.textContent).toContain('Тарировка подтверждена');
});
