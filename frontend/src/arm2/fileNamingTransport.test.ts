// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
import {previewBytes} from './api';

afterEach(()=>vi.unstubAllGlobals());
const source='/arm2/veh/VEH-000001/documents/DOCUMENT-000001/revisions/r/files/FILE-000001/preview';
function response(disposition:string){
  const read=vi.fn().mockResolvedValueOnce({done:false,value:new Uint8Array([1,2,3])}).mockResolvedValue({done:true});
  const cancel=vi.fn();
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,body:{getReader:()=>({read,cancel})},
    headers:new Headers({'Content-Type':'application/pdf','Content-Disposition':disposition})}));
  return {read,cancel};
}
it('preserves authenticated bounded bytes and the UTF-8 name supplied by the platform',async()=>{
  const name='Тарировка Е679РН186 25м3-FILE-000001.pdf';
  const {cancel}=response("inline; filename*=utf-8''"+encodeURIComponent(name));
  const blob=await previewBytes(source,10);
  expect(blob).toBeInstanceOf(File);expect((blob as File).name).toBe(name);
  expect(blob.size).toBe(3);expect(blob.type).toBe('application/pdf');
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining(source),{credentials:'same-origin',redirect:'error'});
  expect(cancel).toHaveBeenCalledOnce();
});
it.each(['',"inline; filename*=utf-8''%zz",'inline; filename="../private.pdf"',
  "inline; filename*=utf-8''bad%0Aname.pdf"])(
  'retains the caller fallback for unsafe or absent metadata: %s',async header=>{
    response(header);const blob=await previewBytes(source,10);
    expect(blob).toBeInstanceOf(Blob);expect(blob).not.toBeInstanceOf(File);expect(blob.size).toBe(3);
  });
it('still rejects oversized bytes and cancels the stream',async()=>{
  const {cancel}=response('inline; filename="valid.pdf"');
  await expect(previewBytes(source,2)).rejects.toMatchObject({status:413});
  expect(cancel).toHaveBeenCalledOnce();
});
