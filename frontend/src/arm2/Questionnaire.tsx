import {openFilePreview} from './filePreviewWindow';
import {useEffect, useRef, useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {api, ApiError} from '../api';
import {questionnaireApi} from './api';

import type {QuestionnaireChoices, QuestionnaireDocument, QuestionnaireDraft, QuestionnaireField, QuestionnaireReceipt} from './api';
import {labels} from './model';
import {QuestionnaireFields, fieldDecision} from './QuestionnaireFields';
import type {FieldAction, FieldEditor} from './QuestionnaireFields';
import {OwnerPicker} from './Selectors';
import {display, ErrorMessage} from './shared';
import type {Can} from './Workspace';
import './Questionnaire.css';

function download(data:string, media:string, filename:string) {
  const url=URL.createObjectURL(new Blob([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],{type:media}));
  const anchor=document.createElement('a');anchor.href=url;anchor.download=filename;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
export function QuestionnaireExport({cpId}:{cpId:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<unknown>(),[missing,setMissing]=useState<{name:string;file_id:string;document_id:string}[]>([]),[reason,setReason]=useState('');
  const exporting=async(format:'xlsx'|'zip',incomplete=false)=>{
    setBusy(true);setError(undefined);
    try {
      const result=await api<{data?:string;media_type:string;filename:string;confirmation_required?:boolean;missing?:typeof missing}>(`/arm2/questionnaires/exports/${encodeURIComponent(cpId)}`,'POST',{format,...(incomplete?{incomplete_reason:reason}:{})});
      if(result.confirmation_required)setMissing(result.missing||[]);
      else if(result.data){download(result.data,result.media_type,result.filename);setMissing([]);}
    }catch(e){setError(e);}finally{setBusy(false);}
  };
  return <section aria-label="Выгрузка анкеты"><h3>Анкета контрагента</h3><Link to="/arm2/questionnaires">Загрузить и сверить анкету</Link><div className="arm2-actions"><button disabled={busy} onClick={()=>void exporting('xlsx')}>Выгрузить анкету XLSX</button><button disabled={busy} onClick={()=>void exporting('zip')}>Выгрузить ZIP с текущими документами</button></div><ErrorMessage error={error}/>{error instanceof ApiError&&typeof error.detail==='string'&&<p role="alert">{error.detail}</p>}{missing.length>0&&<div role="alert"><p>Эти текущие оригиналы недоступны. Исправьте доступность либо явно подтвердите неполную выгрузку.</p><ul>{missing.map(f=><li key={f.file_id}>{f.document_id} · {f.file_id} · {f.name}</li>)}</ul><label>Причина неполной выгрузки<input value={reason} maxLength={500} onChange={e=>setReason(e.target.value)}/></label><button disabled={busy||!reason.trim()} onClick={()=>void exporting('zip',true)}>Явно выдать неполный ZIP</button></div>}</section>;
}

function SignerSources({value,change}:{value:NonNullable<QuestionnaireDraft['comparison']>['representatives'][number];change:(sources:Record<string,'passport'|'list'>)=>void}) {
  return <>{value.source_comment&&<p>Пометка анкеты: {value.source_comment}</p>}{value.signer_hint&&<p>В анкете указан как основной подписант.</p>}{Object.entries(value.conflicts||{}).map(([field,values])=><label key={field}>Расхождение двух представлений: {labels[field]||field}<select aria-label={'Источник подписанта '+(labels[field]||field)} value={value.sources?.[field]||''} onChange={e=>{const sources={...value.sources};if(e.target.value)sources[field]=e.target.value as 'passport'|'list';else delete sources[field];change(sources);}}><option value="">Выберите источник — автоматического приоритета нет</option><option value="passport">Паспорт: {values.passport}</option><option value="list">Представители: {values.list}</option></select></label>)}</>;
}

function Receipt({value}:{value:QuestionnaireReceipt}) {
  const names:Record<string,string>={CP:'Контрагент',CPREP:'Представитель',VEH:'Машина',DOCUMENT:'Документ'};
  const unique=[...new Map((value.changes||[]).map(v=>[v.kind+v.id,v])).values()];
  return <section aria-label="Протокол приёмки анкеты">{value.without_documents&&<p role="status">Данные сохранены без документов. Вложения остаются в черновике для дозагрузки.</p>}<h3>{value.no_op?'Предметных изменений нет':'Изменения приняты'}</h3><p>{Object.entries(names).map(([kind,name])=>`${name}: ${value.objects.filter(o=>o.kind===kind).length}`).join(' · ')}</p><ul>{unique.map(v=><li key={v.kind+v.id}>{v.url?<Link to={v.url}>Открыть: {value.objects.find(o=>o.kind===v.kind&&(o.id||o.document_id)===v.id)?.name||names[v.kind]||'Запись'}</Link>:names[v.kind]||'Запись'}</li>)}</ul>{value.cp_id&&<Link to={'/arm2/cp/'+value.cp_id}>Открыть контрагента</Link>}<details><summary>Технический протокол и история</summary><p>Команда {value.command_id} · Заполнение {value.filled_date||'Не указано'} · Загрузка {value.uploaded_at} · Приёмка {value.accepted_at}</p><div className="arm2-table"><table><thead><tr><th>Объект</th><th>Поле</th><th>Было</th><th>Стало</th></tr></thead><tbody>{value.changes?.flatMap((change,i)=>Object.entries(change.fields).map(([field,v])=><tr key={i+field}><td>{change.url?<Link to={change.url}>{change.kind} {change.id}</Link>:change.id}</td><th>{labels[field]||field}</th><td>{display(v.before)}</td><td>{display(v.after)}</td></tr>))}</tbody></table></div><p>События истории: {value.event_ids.join(', ')||'Не создавались'}</p></details></section>;
}

export default function Questionnaire({can}:{can:Can}) {
  const [params,setParams]=useSearchParams(),identifier=params.get('draft');
  const [draft,setDraft]=useState<QuestionnaireDraft>(),[list,setList]=useState<Awaited<ReturnType<typeof questionnaireApi.list>>>([]),[receipts,setReceipts]=useState<QuestionnaireReceipt[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(),[receipt,setReceipt]=useState<QuestionnaireReceipt>(),[retry,setRetry]=useState(false);
  const [editor,setEditor]=useState<FieldEditor>();
  const uploadedDraft=useRef<string|undefined>(undefined);
  const command=useRef<string|undefined>(undefined),alive=useRef(true);
  const commandMode=useRef(false),commandDraft=useRef<QuestionnaireDraft|undefined>(undefined);
  const storageDialog=useRef<HTMLDialogElement>(null);
  const [garbage,setGarbage]=useState<Awaited<ReturnType<typeof questionnaireApi.cleanupCandidates>>>([]),[notice,setNotice]=useState('');
  const clean=async(id:string)=>{setBusy(true);try{const result=await questionnaireApi.cleanup(id);setNotice(result.pending?'Осталось временных оригиналов: '+result.pending+'. Повторите очистку после восстановления хранилища':'Временные оригиналы очищены; принятые файлы сохранены');setGarbage(await questionnaireApi.cleanupCandidates());}catch(e){setError(e);}finally{setBusy(false);}};
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const refresh=async()=>{const [drafts,protocols]=await Promise.all([questionnaireApi.list(),questionnaireApi.receipts()]);if(alive.current){setList(drafts);setReceipts(protocols);}};
  useEffect(()=>{void refresh().catch(setError);},[]);
  useEffect(()=>{
    if(!identifier)return;
    // The upload response already contains the current review. A second GET
    // can arrive after the operator has started editing and replace the form.
    if(uploadedDraft.current===identifier){uploadedDraft.current=undefined;return;}
    let active=true;setBusy(true);setError(undefined);
    questionnaireApi.reopen(identifier).then(value=>{if(active){setEditor(undefined);setDraft(value);setReceipt(value.receipt);setRetry(false);command.current=undefined;}}).catch(e=>{if(active)setError(e);}).finally(()=>{if(active)setBusy(false);});
    return()=>{active=false;};
  },[identifier]);
  const update=async(choices:QuestionnaireChoices)=>{if(!draft||busy||retry)return false;setBusy(true);setError(undefined);try{const saved=await questionnaireApi.edit(draft,choices);if(alive.current){setDraft(saved);setEditor(undefined);command.current=undefined;return true;}return false;}catch(e){setError(e);setRetry(true);return false;}finally{setBusy(false);}};
  const vehicle=(key:string,values:NonNullable<QuestionnaireChoices['vehicles']>[string])=>update({...draft?.choices,vehicles:{...draft?.choices?.vehicles,[key]:{...draft?.choices?.vehicles?.[key],...values}}});
  const document=(key:string,values:NonNullable<QuestionnaireChoices['documents']>[string])=>void update({...draft?.choices,documents:{...draft?.choices?.documents,[key]:{...draft?.choices?.documents?.[key],...(Object.keys(values).some(k=>k!=='accept')?{accept:false}:{}),...values}}});
  const rep=(key:string,values:NonNullable<QuestionnaireChoices['representatives']>[string])=>update({...draft?.choices,representatives:{...draft?.choices?.representatives,[key]:{...draft?.choices?.representatives?.[key],...values}}});
  const saveField = async (promise: Promise<boolean>) => {if (await promise) setEditor(undefined);};
  const cpField = (field:string, action:FieldAction, value?:string) => {
    const next = fieldDecision(draft?.choices?.cp_fields,draft?.choices?.cp_edits,field,action,value);
    const clears = [...(draft?.choices?.cp_clears || []).filter(key => key !== field), ...(action === 'clear' ? [field] : [])];
    void saveField(update({...draft?.choices,cp_fields:next.fields,cp_edits:next.edits,cp_clears:clears}));
  };
  const repField = (key:string, field:string, action:FieldAction, value?:string) => {
    const choice = draft?.choices?.representatives?.[key];
    void saveField(rep(key,fieldDecision(choice?.fields,choice?.edits,field,action,value)));
  };
  const vehicleField = (key:string, field:string, action:FieldAction, value?:string) => {
    const choice = draft?.choices?.vehicles?.[key];
    void saveField(vehicle(key,fieldDecision(choice?.fields,choice?.edits,field,action,value)));
  };
  const open=async()=>{if(!draft)return;setBusy(true);try{setDraft(await questionnaireApi.reopen(draft.id));setEditor(undefined);setRetry(false);command.current=undefined;setError(undefined);}catch(e){setError(e);}finally{setBusy(false);}};
  const upload=async(file:File)=>{setBusy(true);setError(undefined);try{const value=await questionnaireApi.upload(file);if(alive.current){if(identifier!==value.id)uploadedDraft.current=value.id;setEditor(undefined);setDraft(value);setReceipt(undefined);setRetry(false);setParams({draft:value.id});await refresh();}}catch(e){setError(e);}finally{setBusy(false);}};
  const apply=async(withoutDocuments=commandMode.current)=>{
    if(!draft||editor)return;
    if(!command.current||commandMode.current!==withoutDocuments){
      command.current=crypto.randomUUID();commandMode.current=withoutDocuments;commandDraft.current=draft;
    }
    setBusy(true);setError(undefined);
    try{
      const saved=await questionnaireApi.apply(commandDraft.current||draft,command.current,withoutDocuments);
      const next=saved.documents_pending?await questionnaireApi.reopen(draft.id):{...draft,status:'ACCEPTED' as const,receipt:saved};
      if(alive.current){setReceipt(saved);setDraft(next);setRetry(false);command.current=undefined;commandMode.current=false;void refresh().catch(setError);}
    }catch(e){if(alive.current){setError(e);setRetry(true);}}finally{if(alive.current)setBusy(false);}
  };
  const cancel=async()=>{if(!draft)return;setBusy(true);try{const result=await questionnaireApi.cancel(draft.id);setNotice(result.cleanup?.pending?'Осталось временных оригиналов: '+result.cleanup.pending+'. Повторите очистку после восстановления хранилища':'');setDraft(undefined);setReceipt(undefined);setParams({});setRetry(false);await refresh();}catch(e){setError(e);}finally{setBusy(false);}};
  const show=async(path:string,name:string)=>{
    setError(undefined);setNotice('');
    try {openFilePreview(path,name);} catch(e) {setNotice(e instanceof Error?e.message:'Не удалось открыть просмотр');}
  };
  const c=draft?.comparison,disabled=busy||retry;
  const storageFailure=error instanceof ApiError&&error.status===503&&typeof error.detail==='object'&&error.detail!==null&&'code' in error.detail&&error.detail.code==='QUESTIONNAIRE_STORAGE_UNAVAILABLE';
  useEffect(()=>{
    const dialog=storageDialog.current;
    if(storageFailure&&dialog&&!dialog.open){
      if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');
    }
  },[storageFailure]);
  const returnToReview=()=>{setError(undefined);setRetry(false);command.current=undefined;};
  const cpName=c?.cp.name||c?.cp.candidates.find(v=>v.id===c.cp.id)?.name||'Выбранный контрагент';
  const selectedVehicles=c?.vehicles.filter(v=>draft?.choices?.vehicles?.[v.key]?.include)||[];
  const chosenDocs=c?.documents.filter(d=>d.selected)||[];
  const fieldError = (fields: Record<string,QuestionnaireField>) =>
    Object.values(fields).some(field=>field.selected && !!field.error);
  const hasSelectedFieldError = !!c && (
    fieldError(c.cp.fields) || c.representatives.some(rep=>!!rep.target && fieldError(rep.fields)) ||
    selectedVehicles.some(row=>fieldError(row.fields) || (row.calibration?.selected && !!row.calibration.document_keys.length && !!row.calibration.error))
  );
  const renderDoc=(doc:QuestionnaireDocument)=><DocumentReview key={doc.key} doc={doc} draft={draft!} can={can} disabled={disabled} update={values=>document(doc.key,values)} show={show}/>;
  return <section className="arm2-questionnaire" aria-label="Анкеты контрагентов"><header className="arm2-questionnaire-line"><h2>Загрузка и сверка анкеты</h2><label>Выбрать XLSX/ZIP<input aria-label="Файл анкеты XLSX/ZIP" disabled={busy} type="file" accept=".xlsx,.zip" onChange={e=>{if(e.target.files?.[0])void upload(e.target.files[0]);e.target.value='';}}/></label><span>{draft?.filename}</span></header><ErrorMessage error={storageFailure?undefined:error}/>{busy&&<p role="status">Выполняется операция…</p>}
    <details><summary>Незавершённые загрузки ({list.length})</summary>{list.map(item=><p key={item.id}><button disabled={busy} onClick={()=>{setDraft(undefined);setParams({draft:item.id});if(identifier===item.id)void open();}}>{item.filename}</button></p>)}</details>
    {draft?.status==='OPEN'&&c&&<section aria-label="Сверка анкеты"><div className="arm2-questionnaire-line"><span role="status">Черновик сохранён на сервере</span><button disabled={busy} onClick={()=>void open()}>Повторно открыть сверку</button><button disabled={busy} onClick={()=>void cancel()}>{draft.documents_pending?'Отказаться от дозагрузки документов':'Отклонить загрузку целиком'}</button></div><details><summary>Технические сведения загрузки</summary>{draft.id} · Редакция {draft.revision} · Заполнение {c.filled_date||'Не указано'} · Загрузка {c.uploaded_at}</details>{c.warnings.map((w,i)=><p role="alert" key={i}>{w}</p>)}
      {retry&&!storageFailure&&<div role="alert"><p>Проверьте результат запроса: повторите ту же команду при потере ответа или откройте актуальную сверку при конфликте.</p><button disabled={busy||!command.current} onClick={()=>void apply()}>Повторить ту же команду</button></div>}
      {storageFailure&&<dialog ref={storageDialog} className="arm2-storage-dialog" aria-labelledby="arm2-storage-title" onCancel={e=>{e.preventDefault();if(!busy)returnToReview();}}><section role="alert" aria-label="Яндекс.Диск недоступен">
        <h3 id="arm2-storage-title">Не удалось сохранить документы на Яндекс.Диск</h3>
        <p>Сейчас нет доступа к хранилищу документов на Яндекс.Диске. Выбранные данные и документы не сохранены.</p>
        <p>Можно повторить попытку или сохранить выбранные данные без документов. Вложения останутся в черновике для последующей загрузки.</p>
        <button disabled={busy} onClick={()=>void apply(false)}>Повторить сохранение с документами</button>
        <button disabled={busy} onClick={()=>void apply(true)}>Сохранить выбранные данные без документов</button>
        <button disabled={busy} onClick={returnToReview}>Вернуться к сверке</button>
      </section></dialog>}
      {draft.documents_pending&&<p role="status">Данные сохранены без документов. Вложения сохранены в черновике. Для дозагрузки проверьте привязки и выберите документы повторно.</p>}
      <fieldset disabled={disabled}><legend>Контрагент</legend><div className="arm2-questionnaire-line"><label>Действие<select aria-label="Решение о контрагенте" value={draft.choices?.cp_target||''} onChange={e=>void update({...draft.choices,cp_target:e.target.value||null})}><option value="">Пропустить / выбрать контрагента</option><option value="new">Создать нового контрагента</option>{c.cp.candidates.map(cp=><option key={cp.id} value={cp.id}>Обновить: {cp.name} · {cp.inn||'ИНН не указан'}</option>)}{c.cp.id&&!c.cp.candidates.some(cp=>cp.id===c.cp.id)&&<option value={c.cp.id}>Обновить: {cpName}</option>}</select></label>
        <OwnerPicker cp={draft.choices?.cp_target==='new'?null:draft.choices?.cp_target||null} org={null} can={p=>!p.startsWith('arm2_org_')&&can(p)} editable={!disabled} label="Найти существующего контрагента" change={cp=>void update({...draft.choices,cp_target:cp})}/>
        </div>
        {c.cp.error&&<p role="alert">{c.cp.error}</p>}{c.cp.notices?.map((n,i)=><p role="note" key={i}>{n}</p>)}
        <QuestionnaireFields scope="CP" fields={c.cp.fields} edits={draft.choices?.cp_edits} editor={editor} setEditor={setEditor} busy={disabled} decide={cpField}/>
        {c.cp.identity_conflict&&<section aria-label="Требуется подтверждение"><h3>Требуется подтверждение</h3>{c.cp.identity_conflict_reasons?.map(r=><p key={r.field}>{r.label}: {display(r.current)} → {display(r.proposed)}</p>)}<label><input type="checkbox" checked={!!draft.choices?.confirm_identity} onChange={e=>void update({...draft.choices,confirm_identity:e.target.checked})}/>Подтверждаю использование выбранного контрагента с указанными отличиями</label></section>}
        {c.documents.filter(d=>d.parent_key==='CP').map(renderDoc)}
      </fieldset>
      <fieldset disabled={disabled}><legend>Представители</legend>{c.representatives.map(r=><section key={r.key} aria-label={'Представитель '+r.key}>
        <div className="arm2-questionnaire-line"><label>Действие<select aria-label={'Решение о представителе '+r.key} value={r.target||''} onChange={e=>void rep(r.key,{target:e.target.value||null})}>
          <option value="">Пропустить представителя</option><option value="new">Создать представителя</option>{r.candidates.map(p=><option key={p.id} value={p.id}>Обновить: {p.name}</option>)}
        </select></label><label><input type="checkbox" checked={r.signer} onChange={e=>void rep(r.key,{signer:e.target.checked})}/>Основной подписант</label></div>
        {r.error&&<p role="alert">{r.error}</p>}<SignerSources value={r} change={sources=>void rep(r.key,{sources})}/>
        <QuestionnaireFields scope={'REP:'+r.key} fields={r.fields} edits={draft.choices?.representatives?.[r.key]?.edits}
          editor={editor} setEditor={setEditor} busy={disabled} decide={(field,action,value)=>repField(r.key,field,action,value)}/>
      </section>)}</fieldset>
      <fieldset disabled={disabled}><legend>Техника ({c.vehicles.length})</legend>{c.vehicles.map(row=>{
        const choice = draft.choices?.vehicles?.[row.key];
        const volume = row.calibration;
        const table = row.id ? row.fields : Object.fromEntries(Object.entries(row.fields).filter(([field])=>field!=='owner'));
        return <section key={row.key} aria-label={'Строка техники '+row.row}>
          <div className="arm2-questionnaire-line"><h3>{String(row.fields.name?.proposed||row.plate_number||'Машина без номера')} · {row.plate_number} · строка {row.row}</h3>
            <label>Действие<select aria-label={'Добавить машину строка '+row.row} value={choice?.include?'include':'skip'} disabled={disabled||row.errors.length>0}
              onChange={e=>void vehicle(row.key,{include:e.target.value==='include',...(e.target.value==='skip'?{volume:null}:{}),...(!row.id&&e.target.value==='include'?{fields:Object.entries(row.fields).filter(([,f])=>!f.error).map(([f])=>f)}:{})})}>
              <option value="include">{row.id?'Принять изменения машины':'Добавить машину'}</option><option value="skip">Не добавлять / не изменять</option>
            </select></label>
            <label>Тип техники<select aria-label={'Тип техники строка '+row.row} value={row.without_type?'without_type':choice?.type_id||''}
              onChange={e=>{const value=e.target.value;const fields=(choice?.fields||[]).filter(f=>f!=='type_id');void vehicle(row.key,{
                type_id:value==='without_type'?null:value||null,without_type:value==='without_type',fields:value&&value!=='without_type'?[...fields,'type_id']:fields,
              });}}><option value="">{row.fields.type_id?.proposed_display||'Предложение анкеты / тип не указан'}</option><option value="without_type">Без нового назначения типа</option>
              {c.types.map(t=><option key={t.id} value={t.id}>{t.name} · {t.plate_category}</option>)}
            </select></label>
          </div>
          {row.errors.map((message,i)=><p role="alert" key={i}>{message}</p>)}
          {row.fields.type_id?.error&&<p role="alert">{row.fields.type_id.error}</p>}
          {!row.id&&<p>Владелец новой машины: {row.fields.owner.proposed_display||cpName}</p>}
          <QuestionnaireFields scope={row.key} fields={table} edits={choice?.edits} editor={editor} setEditor={setEditor} busy={disabled}
            canEdit={field=>!row.withdrawal&&['name','model','accounting_state'].includes(field)} decide={(field,action,value)=>vehicleField(row.key,field,action,value)}/>
          {row.id&&!!row.fields.owner.current&&!row.fields.owner.equal&&<label><input type="checkbox" checked={row.confirm_owner} onChange={e=>void vehicle(row.key,{confirm_owner:e.target.checked})}/>Подтверждаю смену операционного владельца</label>}
          {row.withdrawal&&<p>Выбыло из парка: снимается только связь с заявившим владельцем, без архивирования.</p>}
          {c.documents.filter(d=>d.parent_key===row.key).map(renderDoc)}
          {volume&&!volume.document_keys.length&&<small>Выберите справку для приёмки объёма.</small>}
          {volume&&<QuestionnaireFields scope={'VOLUME:'+row.key} editor={editor} setEditor={setEditor} busy={disabled||!volume.document_keys.length}
            fields={{calibration_m3:{current:volume.current,proposed:volume.value,equal:false,selected:volume.selected&&!!volume.document_keys.length,
              error:volume.error}}} edits={choice?.volume!==row.volume_proposal&&choice?.volume?{calibration_m3:choice.volume}:undefined}
            decide={(_,action,value)=>void saveField(vehicle(row.key,{
              volume:action==='edit'?value:action==='set'?row.volume_proposal||null:null,...(action==='edit'||action==='set'?{include:true}:{}),
            }))}/>}
        </section>;
      })}</fieldset>
      {c.document_kind_notice&&<p role="alert">{c.document_kind_notice} Подготовка администратором: открыть справочник «Виды документов», проверить имеющиеся записи по названию и применимости; создать только отсутствующие виды с шаблоном имени. Повторная подготовка не создаёт дубликатов.</p>}
      {!!c.documents.filter(d=>d.parent_key!=='CP'&&!c.vehicles.some(v=>v.key===d.parent_key)).length&&<fieldset disabled={disabled}><legend>Непривязанные документы</legend>{c.documents.filter(d=>d.parent_key!=='CP'&&!c.vehicles.some(v=>v.key===d.parent_key)).map(renderDoc)}</fieldset>}
      <section aria-label="Будет принято"><h3>Будет принято</h3><p>Контрагент: {c.cp.target==='new'?'создать':c.cp.id?'обновить выбранные реквизиты':'пропустить'} · Представители: {c.representatives.filter(r=>r.target).length} · Машины: {selectedVehicles.length} из {c.vehicles.length} · Документы: {chosenDocs.length}</p>{c.vehicles.length>0&&!selectedVehicles.length&&<p role="alert">Машины не будут сохранены: 0 из {c.vehicles.length}</p>}<p>Исключено машин: {c.vehicles.length-selectedVehicles.length}; документов: {c.documents.length-chosenDocs.length}. Ошибки и причины показаны у соответствующей строки.</p></section>
      <button className="arm2-primary" disabled={disabled||!!editor||hasSelectedFieldError||!can('arm2_cp_view')} onClick={()=>void apply()}>Принять изменения</button>
      {hasSelectedFieldError&&<p role="alert">Исправьте выделенные реквизиты или оставьте действующие значения.</p>}
    </section>}
    {notice&&<p role="status">{notice}</p>}{draft?.approval_reset&&<p role="alert">Связанные карточки изменились. Зависимые решения сброшены; проверьте их заново.</p>}
    {receipt&&<Receipt value={receipt}/>}
    <details><summary>Технические протоколы и временные остатки</summary><button disabled={busy} onClick={()=>{questionnaireApi.cleanupCandidates().then(setGarbage).catch(setError);}}>Показать временные остатки закрытых пакетов</button>{garbage.map(item=><p key={item.id}>{item.filename}<button disabled={busy} onClick={()=>void clean(item.id)}>Очистить временные файлы</button></p>)}{receipt&&<button disabled={busy} onClick={()=>void clean(receipt.draft_id)}>Очистить временные файлы завершённого пакета</button>}{receipts.map(r=><Receipt key={r.command_id} value={r}/>)}</details>
  </section>;
}

function DocumentReview({doc,draft,can,disabled,update,show}:{doc:QuestionnaireDocument;draft:QuestionnaireDraft;can:Can;disabled:boolean;update:(v:NonNullable<QuestionnaireChoices['documents']>[string])=>void;show:(path:string,name:string)=>Promise<void>}) {
  const c=draft.comparison!,choices=draft.choices?.documents?.[doc.key];
  const [query,setQuery]=useState(''),[parents,setParents]=useState<{id:string;name:string;plate_number:string}[]>([]);
  useEffect(()=>{let active=true;if(!query.trim()||!can('arm2_veh_view')){setParents([]);return;}api<{items:typeof parents}>('/arm2/vehicles/search?'+new URLSearchParams({q:query,limit:'100'})).then(r=>{if(active)setParents(r.items);}).catch(()=>{if(active)setParents([]);});return()=>{active=false;};},[query,can]);
  const unbound=doc.key.startsWith('unbound:');
  return <section className="arm2-questionnaire-document" aria-label={'Файл '+doc.name}>
    <div className="arm2-questionnaire-document-line">
      <strong title={doc.name}>{doc.role}<span>{doc.name}</span></strong>
      <button type="button" disabled={disabled||!doc.upload_id||!doc.media_type} onClick={()=>void show(`/arm2/questionnaires/${draft.id}/files/${doc.upload_id}/preview`,doc.name)}>Просмотр</button>
      <label>Вид документа<select value={doc.kind_id||''} onChange={e=>update({kind_id:e.target.value||null,document_id:null})}><option value="">Выберите вид</option>{c.document_kinds.filter(k=>k.parents.includes(doc.parent_key==='CP'?'CP':'VEH')).map(k=><option key={k.id} value={k.id}>{k.name}</option>)}</select></label>
      <label>Карточка документа<select value={doc.document_id||choices?.document_id||'new'} onChange={e=>update({document_id:e.target.value})}><option value="new">Создать документ</option>{doc.candidates?.map(d=><option value={d.id} key={d.id}>{d.name}</option>)}</select></label>
      {!doc.equal&&<button type="button" className="arm2-questionnaire-accept" aria-pressed={!!choices?.accept}
        disabled={disabled||!!doc.error&&!choices?.accept} onClick={()=>update({accept:!choices?.accept})}>Принять</button>}
    </div>
    {doc.error&&<p role="alert">{doc.error}</p>}{doc.equal&&<p>Оригинал соответствует действующему — новая редакция не требуется</p>}
    {(doc.error||!doc.upload_id)&&!!doc.attachment_options?.length&&<label>Физическое вложение<select aria-label={'Физический файл '+doc.role} value={doc.upload_id||choices?.upload_id||''} onChange={e=>update({upload_id:e.target.value||null})}><option value="">Выберите конкретный файл из ZIP</option>{doc.attachment_options.map(a=><option key={a.id} value={a.id}>{a.name}</option>)}</select></label>}
    {(unbound||!!doc.current_files?.length)&&<details open={!!doc.error}><summary>Привязка и редакция</summary>
    {unbound?<><label>Родитель файла<select value={doc.parent_key||''} onChange={e=>update({parent_key:e.target.value||null,document_id:null,kind_id:null})}><option value="">Выберите родителя</option><option value="CP">Выбранный контрагент</option>{c.vehicles.map(v=><option value={v.key} key={v.key}>{v.plate_number||'Машина без номера'} · строка {v.row}</option>)}{doc.parent_key?.startsWith('VEH-')&&<option value={doc.parent_key}>Выбранная машина</option>}</select></label><label>Найти другую машину<input disabled={!can('arm2_veh_view')} value={query} onChange={e=>setQuery(e.target.value)} maxLength={200}/></label>{parents.map(parent=><button key={parent.id} onClick={()=>{update({parent_key:parent.id,kind_id:null,document_id:null});setQuery('');}}>{parent.plate_number} · {parent.name}</button>)}</>:null}
    {!!doc.current_files?.length&&<><label>Заменяемый оригинал<select value={doc.replace_file_id||''} onChange={e=>update({replace_file_id:e.target.value||null,append:!e.target.value})}><option value="">Добавить к комплекту</option>{doc.current_files.map(f=><option value={f.id} key={f.id}>{f.name}</option>)}</select></label>{doc.current_files.map(f=><button key={f.id} disabled={disabled} onClick={()=>void show(`/arm2/${doc.parent_type?.toLowerCase()}/${doc.parent_id}/documents/${doc.document_id}/revisions/${doc.revision_id}/files/${f.id}/preview`,f.name)}>Открыть действующий: {f.name}</button>)}</>}
    </details>}<details><summary>Технические сведения файла</summary>{doc.upload_id} · SHA-256 {doc.sha256}</details>
  </section>;
}
