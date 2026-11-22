// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
import {isFilePreviewPath,openFilePreview} from './filePreviewWindow';

afterEach(()=>vi.restoreAllMocks());
it.each(['cp','veh','org','cprep'])('accepts saved revisions and stages for %s',parent=>{
  const root=`/arm2/${parent}/P-1/documents/DOCUMENT-1`;
  expect(isFilePreviewPath(root+'/revisions/R-1/files/F-1/preview')).toBe(true);
  expect(isFilePreviewPath(root+'/stages/abc123/preview')).toBe(true);
});
it.each(['https://evil.invalid/file','/arm2/cp/x/documents/y/stages/z/preview?url=evil','/arm2/cp/x/documents/y/stages/../preview'])('rejects %s before opening a tab',path=>{
  const open=vi.spyOn(window,'open');
  expect(()=>openFilePreview(path)).toThrow();expect(open).not.toHaveBeenCalled();
});
it('reserves synchronously, detaches opener, navigates after staging and safely closes on failure',()=>{
  const tab={opener:window,closed:false,location:{replace:vi.fn()},close:vi.fn()};
  vi.spyOn(window,'open').mockReturnValue(tab as unknown as Window);
  const preview=openFilePreview();expect(tab.opener).toBeNull();
  preview.navigate('/arm2/cp/CP-1/documents/DOCUMENT-1/stages/abc123/preview','Имя & #.pdf');
  const url=new URL(tab.location.replace.mock.calls[0][0] as string,'http://localhost');
  expect(new URLSearchParams(url.hash.slice(1)).get('name')).toBe('Имя & #.pdf');
  preview.close();expect(tab.close).toHaveBeenCalledOnce();
});
