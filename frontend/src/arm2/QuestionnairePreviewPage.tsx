import {useEffect,useState} from 'react';
import {previewBytes} from './api';
import {QuestionnairePreview} from './QuestionnairePreview';
import {ErrorMessage} from './shared';

export function QuestionnairePreviewPage() {
  const [parameters]=useState(()=>new URLSearchParams(window.location.hash.slice(1)));
  const [value,setValue]=useState<{url:string;name:string;media:string;blob:Blob}>();
  const [error,setError]=useState<unknown>();
  const path=parameters.get('path')||'',name=parameters.get('name')||'Вложение';
  // Only the existing authenticated preview endpoints, never a supplied URL.
  const valid=/^\/arm2\/(?:questionnaires\/[A-Za-z0-9-]+\/files\/[A-Za-z0-9-]+|(?:cp|veh|cprep|org)\/[A-Za-z0-9-]+\/documents\/[A-Za-z0-9-]+\/revisions\/[A-Za-z0-9-]+\/files\/[A-Za-z0-9-]+)\/preview$/.test(path);
  useEffect(()=>{
    document.title=name;
    if(!valid)return;
    let active=true,url:string|undefined;
    void previewBytes(path,128*1024*1024).then(blob=>{
      if(!active)return;
      url=URL.createObjectURL(blob);setValue({url,name,media:blob.type,blob});
    }).catch(e=>{if(active)setError(e);});
    return()=>{active=false;if(url)URL.revokeObjectURL(url);};
  },[path,name,valid]);
  return <main className="arm2-questionnaire-preview">
    {!valid?<p role="alert">Некорректная ссылка на вложение анкеты</p>:error?<ErrorMessage error={error}/>:value?
      <QuestionnairePreview value={value} close={()=>window.close()}/>:<p role="status">Загружаем проверенный оригинал…</p>}
  </main>;
}
