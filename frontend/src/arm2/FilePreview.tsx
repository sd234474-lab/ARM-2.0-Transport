import {useEffect,useRef,useState} from 'react';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import type {PDFDocumentProxy} from 'pdfjs-dist';
export function FilePreview({value,close}:{value:{url:string;name:string;media:string;blob:Blob};close:()=>void}) {
  const [pdf,setPdf]=useState<PDFDocumentProxy>(),[page,setPage]=useState(1),[error,setError]=useState<unknown>(),[busy,setBusy]=useState(false);
  const canvas=useRef<HTMLCanvasElement>(null);
  useEffect(()=>{
    if(value.media!=='application/pdf')return;
    let active=true,dispose:(()=>void)|undefined;
    setBusy(true);
    void(async()=>{const renderer=await import('pdfjs-dist');const workerUrl=new URL(pdfWorkerUrl,window.location.href);workerUrl.searchParams.set('module','1');renderer.GlobalWorkerOptions.workerSrc=workerUrl.href;const data=new Uint8Array(await value.blob.arrayBuffer());if(!active)return;const task=renderer.getDocument({data,enableXfa:false,disableFontFace:true,useSystemFonts:true,useWorkerFetch:false,useWasm:false,stopAtErrors:true,disableWorkerRendering:true,disableAutoFetch:true,disableRange:true,disableStream:true,maxImageSize:20000000,canvasMaxAreaInBytes:16000000});dispose=()=>{void Promise.resolve(task.destroy()).catch(()=>{});};const doc=await task.promise;if(doc.numPages>1000)throw new Error('PDF page limit');if(active)setPdf(doc);})().catch(e=>{if(active)setError(e);}).finally(()=>{if(active)setBusy(false);});
    return()=>{active=false;dispose?.();};
  },[value.url,value.media]);
  useEffect(()=>{
    if(!pdf||!canvas.current)return;
    let active=true,cancel:(()=>void)|undefined;
    setBusy(true);
    void(async()=>{const source=await pdf.getPage(page),base=source.getViewport({scale:1});if(!active)return;if(!Number.isFinite(base.width*base.height)||base.width<=0||base.height<=0)throw new Error('Некорректный размер PDF');const scale=Math.min(1.5,1600/base.width,2400/base.height,Math.sqrt(4000000/(base.width*base.height))),viewport=source.getViewport({scale}),target=canvas.current!;target.width=Math.max(1,Math.ceil(viewport.width));target.height=Math.max(1,Math.ceil(viewport.height));const task=source.render({canvas:target,viewport,annotationMode:0});cancel=()=>task.cancel();await task.promise;})().catch(e=>{if(active)setError(e);}).finally(()=>{if(active)setBusy(false);});
    return()=>{active=false;cancel?.();};
  },[pdf,page]);
  return <section aria-label="Просмотр вложения"><h3>{value.name}</h3>{!!error&&<p role="alert">Не удалось отобразить PDF во встроенном просмотре. Проверенный оригинал доступен для скачивания</p>}{busy&&<p role="status">Открываем проверенный PDF…</p>}{value.media.startsWith('image/')?<img src={value.url} alt={'Вложение '+value.name} style={{maxWidth:'100%'}}/>:value.media==='application/pdf'?<><canvas ref={canvas} aria-label={'PDF '+value.name} style={{maxWidth:'100%'}}/>{pdf&&<nav aria-label="Страницы вложения"><button disabled={busy||page<=1} onClick={()=>setPage(p=>p-1)}>Предыдущая страница</button><span>{page} / {pdf.numPages}</span><button disabled={busy||page>=pdf.numPages} onClick={()=>setPage(p=>p+1)}>Следующая страница</button></nav>}</>:<p>Формат доступен скачиванием оригинала.</p>}<a href={value.url} download={value.name}>Скачать проверенный оригинал</a><button onClick={close}>Закрыть просмотр вложения</button></section>;
}
