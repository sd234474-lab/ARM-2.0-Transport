// @vitest-environment jsdom
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {previewBytes} from './api';
import {QuestionnairePreviewPage} from './QuestionnairePreviewPage';
import {ApiError} from '../api';

vi.mock('./api',()=>({previewBytes:vi.fn()}));
let host:HTMLDivElement,root:ReturnType<typeof createRoot>;
const path='/arm2/questionnaires/00000000-0000-4000-8000-000000000005/files/00000000-0000-4000-8000-000000000006/preview';
beforeEach(()=>{
  (globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
  vi.mocked(previewBytes).mockReset();
  vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:vi.fn().mockReturnValue('blob:verified-tab'),revokeObjectURL:vi.fn()}));
  host=document.createElement('div');document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();window.history.replaceState(null,'','/');vi.restoreAllMocks();vi.unstubAllGlobals();});
async function mount(source=path){
  window.history.replaceState(null,'','/questionnaire-preview.html#'+new URLSearchParams({path:source,name:'synthetic.doc'}));
  await act(async()=>{root.render(<QuestionnairePreviewPage/>);await new Promise(resolve=>setTimeout(resolve,5));});
}
it('fetches verified bytes in its own page and keeps opaque originals download-only',async()=>{
  vi.mocked(previewBytes).mockResolvedValue(new Blob(['synthetic'],{type:'application/octet-stream'}));
  await mount();
  expect(previewBytes).toHaveBeenCalledWith(path,128*1024*1024);
  expect(document.title).toBe('synthetic.doc');
  expect(host.querySelector('a[download="synthetic.doc"]')?.getAttribute('href')).toBe('blob:verified-tab');
  expect(host.querySelector('iframe,object,canvas')).toBeNull();
  await act(async()=>root.render(null));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:verified-tab');
});
it.each(['https://example.invalid/file','//example.invalid/file','/api/auth/login','/arm2/questionnaires/x/files/y/preview?redirect=1'])('rejects a non-preview source: %s',async source=>{
  await mount(source);expect(previewBytes).not.toHaveBeenCalled();
  expect(host.textContent).toContain('Некорректная ссылка');
});
it('shows a failed authenticated fetch instead of a permanent loading indicator',async()=>{
  vi.mocked(previewBytes).mockRejectedValue(new ApiError(403,'Forbidden'));
  await mount();expect(host.querySelector('[role="alert"]')).not.toBeNull();
  expect(host.querySelector('[role="status"]')).toBeNull();expect(URL.createObjectURL).not.toHaveBeenCalled();
});
