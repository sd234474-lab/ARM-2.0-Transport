// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import PermissionEditor from '../PermissionEditor';
import Deletion, { DeletionMark, DeletionPermissions } from './Deletion';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));

const marked = {
  object_type: 'TYPE', object_id: 'TYPE-000123', name: 'Тестовый тип', version: 4,
  path: '/arm2/type/TYPE-000123', mark: '7bf9e2f6-5c4e-4df4-8908-d348a35c5965',
};
const base = '/arm2/deletion';
let host: HTMLDivElement, root: ReturnType<typeof createRoot>;
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const render = async (element: React.ReactNode, permissions: string[]) => {
  await act(async () => {
    root.render(<MemoryRouter><DeletionPermissions.Provider value={permission => permissions.includes(permission)}>
      {element}
    </DeletionPermissions.Provider></MemoryRouter>);
    await tick();
  });
};
const click = async (el: HTMLElement) => {
  await act(async () => {el.click(); await tick();});
};
const button = (label: string) => [...host.querySelectorAll('button')].find(el => el.textContent?.includes(label))!;

beforeEach(() => {
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?: boolean}).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(api).mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

it('shows a server mark on reopening and uses the same control to remove it', async () => {
  let mark: string | null = marked.mark;
  vi.mocked(api).mockImplementation(async (_path, method) => {
    if (method === 'PUT') mark = null;
    return {...marked, mark};
  });
  const card = <DeletionMark kind="TYPE" id={marked.object_id} version={4} editable/>;
  await render(card, []);
  expect(vi.mocked(api)).not.toHaveBeenCalled();
  await render(card, ['arm2_delete_mark']);
  expect(button('Снять пометку удаления').getAttribute('aria-pressed')).toBe('true');
  await click(button('Снять пометку удаления'));
  expect(vi.mocked(api)).toHaveBeenCalledWith(base + '/marks/TYPE/TYPE-000123', 'PUT',
    {expected_version: 4, expected_mark: marked.mark, marked: false});
  expect(button('Пометить на удаление').getAttribute('aria-pressed')).toBe('false');
});

it('displays the blocking card and requires confirmation before deleting a checked set', async () => {
  let mode: 'blocked' | 'ready' | 'deleted' = 'blocked';
  const receipt = {id: 'test-set', state: 'FILES_PENDING', objects: [marked],
    cleanup: [{object_id: 'FILE-000123', state: 'QUEUED'}], executed_at: '2026-11-24T04:00:00Z'};
  vi.mocked(api).mockImplementation(async (path, method) => {
    if (path === base + '/marks?offset=0') return {items: mode === 'deleted' ? [] : [marked], has_more: false};
    if (path === base + '/batches?offset=0') return {items: mode === 'deleted' ? [receipt] : [], has_more: false};
    if (path === base + '/preview') return {id: 'test-set', objects: {[marked.object_id]: marked},
      can_execute: mode === 'ready', dependencies: mode === 'blocked' ? [{
        target_id: marked.object_id, object_id: 'VEH-000456', path: '/arm2/veh/VEH-000456',
        field: 'type_id', reason: 'Исправьте связь', blocking: true,
      }] : []};
    if (path.endsWith('/execute') && method === 'POST') {mode = 'deleted'; return receipt;}
    if (path.endsWith('/test-set')) return receipt;
    throw new Error(path);
  });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await render(<Deletion/>, ['arm2_delete_mark', 'arm2_delete_execute']);
  await click(host.querySelector<HTMLInputElement>('input[type=checkbox]')!);
  await click(button('Проверить набор'));
  expect(host.querySelector('a[href="/arm2/veh/VEH-000456"]')?.textContent).toBe('VEH-000456');
  expect(button('Подтвердить удаление')).toBeUndefined();
  mode = 'ready';
  await click(button('Проверить набор'));
  await click(button('Подтвердить удаление'));
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/execute'))).toBe(false);
  confirm.mockReturnValue(true);
  await click(button('Подтвердить удаление'));
  expect(vi.mocked(api)).toHaveBeenCalledWith(base + '/batches/test-set/execute', 'POST', {confirmed:true});
  expect(host.textContent).toContain('Записи удалены. Очистка файлов продолжается');
});

it('uses the batch-level retry capability and refreshes it after a permissions denial', async () => {
  let allowed = false;
  const receipt = {id: 'test-set', state: 'FILES_BLOCKED', objects: [marked],
    cleanup: [{object_id: 'FILE-000123', state: 'FAILED'}], executed_at: '2026-11-24T04:00:00Z'};
  vi.mocked(api).mockImplementation(async (path, method) => {
    if (path === base + '/marks?offset=0') return {items: [], has_more: false};
    if (path === base + '/batches?offset=0') return {items: [receipt], has_more: false};
    if (path.endsWith('/retry') && method === 'POST') {
      allowed = false;
      throw new ApiError(403, 'Нет актуальных прав');
    }
    if (path.endsWith('/test-set')) return {...receipt, can_retry: allowed};
    throw new Error(path);
  });
  await render(<Deletion/>, ['arm2_delete_mark', 'arm2_delete_execute']);
  await click(button('· 1'));
  expect(button('Повторить сверку файлов')).toBeUndefined();
  allowed = true;
  await click(button('Обновить состояние'));
  await click(button('Повторить сверку файлов'));
  expect(button('Повторить сверку файлов')).toBeUndefined();
});

it('groups open work, retained history and owned records and explicitly adds an editable blocker', async () => {
  const vehicle = {object_type: 'VEH', object_id: 'VEH-000456', version: 3, name: 'Самосвал',
    path: '/arm2/veh/VEH-000456', mark: null};
  const row = {target_id: marked.object_id, blocking: true};
  const draftPath = '/arm2/veh/bulk?session=operator-draft';
  vi.mocked(api).mockImplementation(async (path, method) => {
    if (path.endsWith('?offset=0')) return {items: path.includes('/marks') ? [marked] : [], has_more: false};
    if (path.endsWith('/preview')) return {id: 'checked', objects: {[marked.object_id]: marked}, can_execute: false,
      dependencies: [
        {...row, purpose: 'OPEN_OPERATION', name: 'Черновик Техника.xlsx', field: 'payload',
          at: '2026-11-24T04:00:00Z', path: draftPath, reason: 'Завершите операцию'},
        {...row, purpose: 'ACTIVE_DOMAIN', name: vehicle.name, selectable: vehicle, reason: 'Исправьте связь'},
        {...row, purpose: 'HISTORY', name: 'Квитанция', blocking: false, reason: 'Сохранится в истории'},
        {...row, purpose: 'OWNED', name: 'Файл редакции', blocking: false, reason: 'Обрабатывается с владельцем'},
      ]};
    if (path.endsWith('/marks/VEH/VEH-000456') && method === 'PUT') return {...vehicle, mark: 'new-mark'};
    throw Error(path);
  });
  await render(<Deletion/>, ['arm2_delete_mark', 'arm2_delete_execute']);
  await click(host.querySelector<HTMLInputElement>('input[type=checkbox]')!);
  await click(button('Проверить набор'));
  expect(host.querySelector(`a[href="${draftPath}"]`)?.getAttribute('target')).toBe('_blank');
  expect(host.textContent).not.toContain('payload');
  expect(host.querySelector('[aria-label="История сохраняется"]')?.textContent).toContain('Квитанция');
  expect(host.querySelector('[aria-label="Обрабатываются в составе набора"]')?.textContent).toContain('Файл редакции');
  await click(button('Пометить и добавить в набор'));
  expect(vi.mocked(api)).toHaveBeenCalledWith(base + '/marks/VEH/VEH-000456', 'PUT',
    {expected_version: 3, expected_mark: null, marked: true});
  expect(button('Проверить набор').textContent).toContain('2');
  expect(button('Подтвердить удаление')).toBeUndefined();
});

it('shows separate deletion permissions under the ARM2 group and keeps the prerequisite', async () => {
  const catalog = [
    {code: 'arm2_delete_mark', name: 'Пометка объектов на удаление', group: 'АРМ 2.0',
      requires: [], default_operator: false},
    {code: 'arm2_delete_execute', name: 'Удаление помеченных объектов', group: 'АРМ 2.0',
      requires: ['arm2_delete_mark'], default_operator: false},
  ];
  const change = vi.fn();
  await render(<PermissionEditor catalog={catalog} value={[]} onChange={change} administrator={false}/>, []);
  expect(host.querySelector('legend')?.textContent).toBe('АРМ 2.0');
  const labels = [...host.querySelectorAll('fieldset label')];
  expect(labels.map(label => label.textContent)).toEqual(catalog.map(item => item.name));
  await click(labels[1].querySelector('input')!);
  expect(change).toHaveBeenCalledWith(expect.arrayContaining(['arm2_delete_mark', 'arm2_delete_execute']));
});
