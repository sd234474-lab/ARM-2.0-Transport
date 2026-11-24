// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '../api';
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
