import {isFilePreviewPath} from './filePreviewWindow';
import {useEffect,useState} from 'react';
import {previewBytes} from './api';
import {FilePreview} from './FilePreview';
import {ErrorMessage} from './shared';

export function FilePreviewPage() {
  const [parameters]=useState(()=>new URLSearchParams(window.location.hash.slice(1)));
  const [value,setValue]=useState<{url:string;name:string;media:string;blob:Blob}>();
  const [error,setError]=useState<unknown>();
  const path=parameters.get('path')||'',name=parameters.get('name')||'Вложение';
  // Only the existing authenticated preview endpoints, never a supplied URL.
  const valid=isFilePreviewPath(path);
  useEffect(()=>{
    document.title=name;
    if(!valid)return;
    let active=true,url:string|undefined;
    void previewBytes(path,128*1024*1024).then(blob=>{
      if(!active)return;
      const currentName=blob instanceof File?blob.name:name;
      document.title=currentName;
      url=URL.createObjectURL(blob);setValue({url,name:currentName,media:blob.type,blob});
    }).catch(e=>{if(active)setError(e);});
    return()=>{active=false;if(url)URL.revokeObjectURL(url);};
  },[path,name,valid]);
  return <main className="arm2-questionnaire-preview">
    {!valid?<p role="alert">Некорректная ссылка на файл</p>:error?<ErrorMessage error={error}/>:value?
      <FilePreview value={value} close={()=>window.close()}/>:<p role="status">Загружаем проверенный оригинал…</p>}
  </main>;
}
