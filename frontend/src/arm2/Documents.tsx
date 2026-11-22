import {useDraftGuard} from './useDraftGuard';
import type {ReactNode} from 'react';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api';
import { catalogs, searchPage } from './api';
import { apiUrl } from '../workspace';
import {openFilePreview} from './filePreviewWindow';
import type { DocumentKind, DocumentParent, DocumentRecord, RecordData, DocumentFile, EventDetail, FileBundle, FileStage } from './api';
import type { Can } from './Workspace';
import { ErrorMessage, History, Pagination, useAlive, useLoad } from './shared';

interface Scope {parentType: DocumentParent; parentId: string; can: Can}
const endpoint = (type: DocumentParent, id: string) => `/arm2/${type.toLowerCase()}/${encodeURIComponent(id)}/documents`;
const parentLink = (type: DocumentParent, id: string) => `/arm2/${type.toLowerCase()}/${encodeURIComponent(id)}?tab=documents`;
const view = ({parentType, can}: Pick<Scope, 'parentType' | 'can'>) => can(catalogs[parentType].permission + '_view') && can('arm2_document_view');
const edit = (scope: Pick<Scope, 'parentType' | 'can'>) => view(scope) && scope.can(catalogs[scope.parentType].permission + '_edit') && scope.can('arm2_document_edit');

export default function Documents(scope: Scope) {
  return view(scope) ? <DocumentList {...scope}/> : <p>Нет прав просмотра документов этой карточки.</p>;
}

function DocumentFiles({path, original, editable, blocked, draft, setDraft, refreshed, onDenied}: {
  path: string; original: DocumentRecord; editable: boolean; blocked: boolean; draft?: FileDraft;
  setDraft: (value: FileDraft | undefined | ((current: FileDraft | undefined) => FileDraft | undefined)) => void; refreshed: () => Promise<void>;
  onDenied: () => void;
}) {
  const [fileHistory,setFileHistory]=useState(false);
  const result = useLoad(signal => api<FileBundle>(path + (fileHistory ? '/files' : '/files?history=false'), 'GET', undefined, signal), [path, original.version, fileHistory]);
  const current = result.data?.revisions?.find(row => row.id === result.data?.current_revision_id);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(), [status, setStatus] = useState('');
  const [denied, setDenied] = useState(false), [comparison, setComparison] = useState<FileBundle>();
  const input = useRef<HTMLInputElement>(null), replacing = useRef<number | undefined>(undefined), inFlight = useRef(false), alive = useAlive();
  const allowed = editable && !denied;
  const begin = () => {setError(undefined); setStatus(''); setDraft({key: crypto.randomUUID(), expectedVersion: original.version, candidates: (current?.files || []).map(file => ({key: file.id, current: file}))});};
  const beginCorrect = (file: DocumentFile) => {
    if (!allowed || blocked || busy || !current) return;
    setError(undefined); setStatus('');
    setDraft({key:crypto.randomUUID(),expectedVersion:original.version,
      candidates:current.files.map(row=>({key:row.id,current:row})),
      correction:{fileId:file.id,position:file.position,action:'replace'}});
  };
  const change = (candidates: FileCandidate[]) => {if (draft) setDraft({...draft, key: crypto.randomUUID(), candidates}); setStatus(''); setError(undefined); setComparison(undefined);};
  const updateStage = (candidate: FileCandidate, stage: FileStage) => setDraft(value => value ? {...value, candidates: value.candidates.map(row => row.key === candidate.key ? {...row, stage} : row)} : value);
  const staged = async (candidate: FileCandidate) => {
    if (!candidate.file) throw new Error('Выберите файл повторно');
    let stage = candidate.stage;
    if (stage?.state === 'ready') return stage;
    if (stage) stage = await api<FileStage>(path + '/stages/' + stage.id);
    else {stage = await api<FileStage>(path + '/stages', 'POST', {original_upload_name: candidate.file.name, size: candidate.file.size}); if (alive.current) updateStage(candidate, stage);}
    if (stage.state !== 'ready') stage = await api<FileStage>(path + '/stages/' + stage.id + '/content', 'PUT', candidate.file);
    if (alive.current) updateStage(candidate, stage);
    return stage;
  };
  const operation = async (action: () => Promise<void>, preserveError = false) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); if (!preserveError) setError(undefined); setStatus('');
    try {await action();} catch (e) {if (alive.current) {setError(e); if (e instanceof ApiError && e.status === 403) {setDenied(true);onDenied();}}}
    finally {inFlight.current = false; if (alive.current) setBusy(false);}
  };
  const openCandidate = (candidate: FileCandidate) => {
    if (candidate.current && current) {
      try {openFilePreview(`${path}/revisions/${current.id}/files/${candidate.current.id}/preview`, candidate.current.original_upload_name);}
      catch (e) {setError(e);}
      return;
    }
    if (inFlight.current) return;
    // Reserve the tab in the click handler, before staging performs any await.
    let tab: ReturnType<typeof openFilePreview>;
    try {tab = openFilePreview();} catch (e) {setError(e); return;}
    void operation(async () => {
      try {
        const stage = await staged(candidate);
        tab.navigate(`${path}/stages/${stage.id}/preview`, stage.original_upload_name);
      } catch (e) {tab.close(); throw e;}
    });
  };
  const accept = () => void operation(async () => {
    if (!draft || !allowed || blocked) return;
    if (draft.correction) {
      const candidate = draft.candidates[draft.correction.position];
      if (draft.correction.action === 'replace' && !candidate?.file) throw new ApiError(422,'Выберите верный файл для замены');
      if (!window.confirm(draft.correction.action === 'replace' ? 'Исправить ошибочную загрузку выбранным файлом? Ошибочный оригинал останется только в ограниченном аудиторском хранении.' : 'Исключить ошибочную загрузку без замены? Оригинал останется только в ограниченном аудиторском хранении.')) return;
      const stageId = draft.correction.action === 'replace' ? (await staged(candidate)).id : null;
      await api(path+'/corrections','POST',{expected_version:draft.expectedVersion,idempotency_key:draft.key,file_id:draft.correction.fileId,stage_id:stageId});
      if (!alive.current) return;
      await refreshed(); result.reload(); setComparison(undefined); setStatus('Ошибочная загрузка исправлена сервером');
      return;
    }
    const selection = [];
    for (const candidate of draft.candidates) selection.push(candidate.current ? {file_id: candidate.current.id} : {stage_id: (await staged(candidate)).id});
    const response = await api<{no_op: boolean; document_version: number}>(path + '/accept', 'POST', {expected_version: draft.expectedVersion, idempotency_key: draft.key, files: selection});
    if (!alive.current) return;
    await refreshed(); result.reload(); setComparison(undefined);
    setStatus(response.no_op ? `Состав не изменился — версия ${response.document_version}` : `Комплект принят — версия ${response.document_version}`);
  });
  const compare = () => void operation(async () => {const page = await api<FileBundle>(path + '/files'); if (alive.current) setComparison(page);}, true);
  const reload = () => {
    if (!window.confirm('Заменить несохранённый состав и сведения документа данными сервера?')) return;
    void operation(async () => {await refreshed(); if (alive.current) {setDraft(undefined); setComparison(undefined); result.reload();}});
  };
  const selectFile = (file: File) => {
    if (!draft || !allowed || busy) return;
    const maximum = result.data?.max_file_bytes || 0;
    if (!file.size || file.size > maximum) {setError(new ApiError(422, 'Недопустимый размер файла')); return;}
    const candidate: FileCandidate = {key: crypto.randomUUID(), file};
    if (draft.correction && current) {
      const correction=draft.correction;
      setDraft({...draft,key:crypto.randomUUID(),correction:{...correction,action:'replace'},
        candidates:current.files.map((row,index)=>index===correction.position?candidate:{key:row.id,current:row})});
      setError(undefined);return;
    }
    if (replacing.current !== undefined) change(draft.candidates.map((row, index) => index === replacing.current ? candidate : row));
    else if (draft.candidates.length < (result.data?.max_files || 32)) change([...draft.candidates, candidate]);
    else setError(new ApiError(422, 'Достигнут предел файлов в комплекте'));
  };
  const fileRows = (files: DocumentFile[], revisionId: string) => <ol className="arm2-file-list">{files.map(file => <li key={file.id}>
    <div className="arm2-file-name"><span>{file.original_upload_name}</span><small>{file.extension.toUpperCase()} · {Math.max(1,Math.round(file.size/1024))} КБ</small></div>
    <div className="arm2-file-actions">{file.correction ? <span className="arm2-correction">Исправленная ошибочная загрузка · {new Date(file.correction.occurred_at).toLocaleString('ru-RU')} · Автор {file.correction.actor_id} · Событие {file.correction.history_event_id}</span> : <>
      <button type="button" onClick={() => {try {openFilePreview(`${path}/revisions/${revisionId}/files/${file.id}/preview`, file.original_upload_name);} catch (e) {setError(e);}}}>Открыть файл</button>
      <a href={apiUrl(`${path}/revisions/${revisionId}/files/${file.id}/download`)}>Скачать файл</a>
      <details className="arm2-file-more"><summary>Ещё</summary>{allowed && revisionId === current?.id && <button type="button" disabled={busy || blocked || !!draft} onClick={()=>beginCorrect(file)}>Исправить ошибочную загрузку</button>}</details>
    </>}</div>
    <details className="arm2-file-technical"><summary>Сведения о файле</summary><small>{revisionId === current?.id ? `Текущая редакция ${current.number}` : `Редакция ${result.data?.revisions.find(row=>row.id===revisionId)?.number}`} · {file.id} · {file.size} байт · SHA-256: {file.sha256}</small></details>
  </li>)}</ol>;

  return <section className="arm2-files" aria-label="Файлы и редакции"><div className="arm2-document-file-heading"><h3>Файлы</h3><button type="button" className="arm2-document-link" disabled={busy} onClick={result.reload}>Проверить доступность</button></div>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем состав документа…</p>}<ErrorMessage error={error}/>{status && <p role="status">{status}</p>}
    {blocked && <p>Сначала сохраните изменения сведений документа.</p>}{!allowed && <p>Файлы доступны только для просмотра.</p>}
    {result.data && <>{result.data.display_state !== original.display_state && <p className="arm2-file-availability" role="status">Состояние комплекта: {result.data.display_state}</p>}
      {!draft && <>{current?.source === 'arm2-api-correct' && <p>Редакция создана исправлением ошибочной загрузки.</p>}{current && fileRows(current.files, current.id)}{!current?.files.length && <p>Комплект не содержит файлов.</p>}{allowed && <button type="button" disabled={blocked || busy || result.loading} onClick={begin}>{current ? 'Изменить файлы' : '+ Добавить файл'}</button>}</>}
      {draft && <div aria-label="Черновик состава"><h4>{draft.correction ? 'Исправить ошибочную загрузку' : current ? 'Изменение состава' : 'Добавление файлов'}</h4><p>{draft.correction ? 'Ошибочный файл: '+current?.files.find(row=>row.id===draft.correction?.fileId)?.original_upload_name : 'Добавляйте файлы по одному. Откройте кандидаты для проверки; принятие публикует весь комплект одним действием.'}</p>
        {draft.correction && allowed && <div className="arm2-actions"><button type="button" disabled={busy} onClick={()=>{replacing.current=draft.correction!.position;input.current?.click();}}>Заменить верным файлом</button><button type="button" disabled={busy} onClick={()=>setDraft({...draft,key:crypto.randomUUID(),candidates:current!.files.filter(row=>row.id!==draft.correction!.fileId).map(row=>({key:row.id,current:row})),correction:{...draft.correction!,action:'exclude'}})}>Исключить без замены</button></div>}
        <input ref={input} type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.docx,.xlsx,.pptx" aria-label="Выбор одного файла" disabled={!allowed || busy} onChange={event => {const file = event.target.files?.[0]; if (file) selectFile(file); event.target.value = '';}}/>
        <ol className="arm2-file-list">{draft.candidates.map((candidate,index) => <li key={candidate.key}><span>{candidate.file?.name || candidate.current?.original_upload_name}</span><button type="button" disabled={busy} onClick={() => openCandidate(candidate)}>Открыть файл</button>
          {allowed && !draft.correction && <><button type="button" disabled={busy} onClick={() => {replacing.current = index; input.current?.click();}}>Заменить файл</button><button type="button" disabled={busy} onClick={() => change(draft.candidates.filter((_,i) => i !== index))}>Исключить файл</button></>}</li>)}</ol>
        {allowed && <>{!draft.correction && <button type="button" disabled={busy || draft.candidates.length >= result.data.max_files} onClick={() => {replacing.current = undefined; input.current?.click();}}>+ Добавить файл</button>}<button type="button" disabled={busy || blocked} onClick={accept}>{busy ? 'Сохраняем комплект…' : draft.correction ? 'Исправить и сохранить' : current ? 'Сохранить новую редакцию' : 'Сохранить и принять'}</button></>}
        <button type="button" disabled={busy} onClick={() => {if (window.confirm('Отменить несохранённые изменения состава?')) {setDraft(undefined); setError(undefined);}}}>Отменить изменение состава</button>
      </div>}
      {error instanceof ApiError && error.status === 409 && <div className="arm2-actions"><button type="button" onClick={compare}>Сравнить состав с сервером</button><button type="button" onClick={reload}>Перезагрузить состав</button></div>}
      {comparison && <section aria-label="Состав на сервере"><h4>Состав на сервере — версия {comparison.document_version}; ваш черновик сохранён</h4>{comparison.revisions.find(row => row.id === comparison.current_revision_id)?.files.map(file => <p key={file.id}>{file.original_upload_name}</p>)}</section>}
      <button type="button" className="arm2-document-link arm2-revision-link" aria-expanded={fileHistory} onClick={()=>setFileHistory(value=>!value)}>{fileHistory?'Закрыть предыдущие редакции':'Предыдущие редакции'}</button>
      {fileHistory && result.data.revisions.filter(row => row.id !== result.data?.current_revision_id).map(revision => <details key={revision.id}><summary>Предыдущая редакция {revision.number} · {new Date(revision.occurred_at).toLocaleString('ru-RU')} · Автор {revision.author_id}</summary><p>{revision.id} · {revision.source === 'arm2-api-correct' ? 'Исправление ошибочной загрузки' : revision.source} · Родитель при создании: {revision.parent_id}</p>{fileRows(revision.files,revision.id)}</details>)}
    </>}

  </section>;
}
function DocumentList(scope: Scope) {
  const {parentType, parentId} = scope;
  const [archive, setArchive] = useState('non_archived'), [offset, setOffset] = useState(0);
  const [moves,setMoves]=useState(false);
  const path = endpoint(parentType, parentId);
  const result = useLoad(signal => api<DocumentRecord[]>(`${path}?${new URLSearchParams({archive, limit: '100', offset: String(offset)})}`, 'GET', undefined, signal), [path, archive, offset]);
  return <section aria-label="Документы родителя"><header className="arm2-actions"><h3>Документы</h3>{edit(scope) && <Link to={path + '/new'}>Добавить документ</Link>}</header>
    <label className="arm2-field">Архивность<select value={archive} onChange={event => {setArchive(event.target.value); setOffset(0);}}><option value="non_archived">Неархивные</option><option value="archived">Архивные</option><option value="all">Все</option></select></label>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем документы…</p>}
    {result.data && <><div className="arm2-table"><table><thead><tr><th>Тип документа</th><th>Название карточки</th><th>Состояние документа</th></tr></thead><tbody>{result.data.map(row => <tr key={row.id}><td>{row.kind_name}</td><td><Link to={path + '/' + encodeURIComponent(row.id)}>{row.name}</Link></td><td>{row.display_state}</td></tr>)}</tbody></table></div>
      {!result.data.length && <p>Документов по заданным условиям нет</p>}<Pagination offset={offset} count={result.data.length} change={setOffset}/></>}
    <button type="button" aria-expanded={moves} onClick={()=>setMoves(value=>!value)}>{moves?'Закрыть историю перемещений':'История перемещений документов'}</button>{moves && <DocumentMoveHistory scope={scope}/>}
  </section>;
}

function DocumentMoveHistory({scope}:{scope:Scope}) {
  const [offset,setOffset]=useState(0);
  const result=useLoad(signal=>api<EventDetail[]>(`/arm2/history/${scope.parentType}/${scope.parentId}/document-moves?limit=100&offset=${offset}`,'GET',undefined,signal),[scope.parentType,scope.parentId,offset]);
  return <section aria-label="История перемещений документов"><h4>Перемещения документов</h4><ErrorMessage error={result.error} retry={result.reload}/>{result.data && <>{result.data.map(event=><p key={event.event_id}>{new Date(event.occurred_at).toLocaleString('ru-RU')} · Автор {event.actor_id} · {event.object_id} · {String(event.changes.parent_id?.before)} → {String(event.changes.parent_id?.after)}</p>)}{!result.data.length && <p>Перемещений нет</p>}<Pagination offset={offset} count={result.data.length} change={setOffset}/></>}</section>;
}

export function DocumentRoute({parentType, can}: {parentType: DocumentParent; can: Can}) {
  const {parentId = '', id = ''} = useParams();
  const scope = {parentType, parentId, can};
  if (!view(scope) || (id === 'new' && !edit(scope))) return <p>Нет прав для этой операции с документом.</p>;
  return <DocumentLoader key={parentType + ':' + parentId + ':' + id} {...scope} id={id}/>;
}
function DocumentLoader({id, ...scope}: Scope & {id: string}) {
  const path = endpoint(scope.parentType, scope.parentId), back = parentLink(scope.parentType, scope.parentId);
  const result = useLoad(async signal => ({
    parent: await api<RecordData>(`${catalogs[scope.parentType].path}/${encodeURIComponent(scope.parentId)}`, 'GET', undefined, signal),
    document: id === 'new' ? undefined : await api<DocumentRecord>(path + '/' + encodeURIComponent(id), 'GET', undefined, signal),
  }), [path, id]);
  return <><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Открываем документ…</p>}{result.data ? <DocumentForm {...scope} parent={result.data.parent} initial={result.data.document}/> : <Link to={back}>← К документам родителя</Link>}</>;
}

interface Draft {kind_ref: string; name: string; description: string | null}
interface FileCandidate {key: string; current?: DocumentFile; file?: File; stage?: FileStage}
interface FileDraft {key: string; expectedVersion: number; candidates: FileCandidate[]; correction?: {fileId: string; position: number; action: 'replace' | 'exclude'}}
interface OperationDraft {action: 'archive' | 'restore' | 'retype' | 'move'; expectedVersion: number; kind: string; target: string; error?: unknown}
interface Parked {original?: DocumentRecord; draft: Draft; denied: boolean; error: unknown; files?: FileDraft; operation?: OperationDraft}
const parkedDrafts = new Map<string, Parked>();
const draftOf = (record?: DocumentRecord): Draft => ({kind_ref: record?.kind_ref || '', name: record?.name || '', description: record?.description ?? null});

function KindPicker({path, value, change, can}: {path: string; value: string; change: (id: string) => void; can: Can}) {
  const [q, setQ] = useState(''), [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<DocumentKind>();
  const result = useLoad(signal => api<DocumentKind[]>(`${path}/kinds?${new URLSearchParams({q, limit: '100', offset: String(offset)})}`, 'GET', undefined, signal), [path, q, offset]);
  const options = result.data || [];
  return <section aria-label="Выбор типа документа">
    <label className="arm2-field">Поиск доступного типа документа<input value={q} maxLength={200} onChange={event => {setQ(event.target.value); setOffset(0);}}/></label>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем доступные типы…</p>}
    <label className="arm2-field">Тип документа *<select value={value} required onChange={event => {const next = event.target.value; setSelected(options.find(option => option.id === next)); change(next);}}><option value="">Выберите существующий тип</option>
      {value && !options.some(option => option.id === value) && <option value={value}>{selected?.id === value ? selected.name : value}</option>}{options.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}</select></label>
    {result.data && <><Pagination offset={offset} count={result.data.length} change={setOffset}/>{!options.length && <p>Подходящих типов на этой странице нет. Измените поиск или создайте применимый тип в справочнике.</p>}</>}
    {can('arm2_doc_kind_view') && <Link to="/arm2/document-kinds">Открыть справочник типов документов</Link>}
  </section>;
}

function ParentPicker({scope,value,change}:{scope:Scope;value:string;change:(id:string)=>void}) {
  const [q,setQ]=useState(''),[offset,setOffset]=useState(0);
  const result=useLoad(signal=>searchPage(catalogs[scope.parentType].path,new URLSearchParams({q,limit:'100',offset:String(offset)}),signal),[scope.parentType,q,offset]);
  return <section aria-label="Выбор нового родителя"><label className="arm2-field">Поиск получателя<input value={q} maxLength={200} onChange={event=>{setQ(event.target.value);setOffset(0);}}/></label><ErrorMessage error={result.error} retry={result.reload}/><label className="arm2-field">Новый родитель *<select value={value} onChange={event=>change(event.target.value)}><option value="">Выберите получателя</option>{value && !result.data?.items.some(row=>row.id===value) && <option value={value}>{value}</option>}{result.data?.items.filter(row=>row.id!==scope.parentId).map(row=><option key={row.id} value={row.id}>{row.name} · {row.id}</option>)}</select></label>{result.data && <Pagination offset={offset} count={result.data.items.length} change={setOffset}/>}</section>;
}

function DocumentOperations({saveAction,scope,record,draft,setDraft,allowed,blocked,completed,denied,compare,reload}:{saveAction:ReactNode;scope:Scope;record:DocumentRecord;draft?:OperationDraft;setDraft:(value:OperationDraft|undefined)=>void;allowed:boolean;blocked:boolean;completed:(record:DocumentRecord)=>void;denied:()=>void;compare:()=>void;reload:()=>void}) {
  const [busy,setBusy]=useState(false),inFlight=useRef(false),alive=useAlive();
  const labels={archive:'Архивировать',restore:'Восстановить',retype:'Изменить тип',move:'Изменить привязку'};
  const begin=(action:OperationDraft['action'])=>setDraft({action,expectedVersion:record.version,kind:record.kind_ref,target:''});
  const submit=async()=>{
    if (!draft || !allowed || blocked || inFlight.current) return;
    if ((draft.action==='retype'&&!draft.kind)||(draft.action==='move'&&!draft.target)) {setDraft({...draft,error:new ApiError(422,'Выберите тип или получателя')});return;}
    inFlight.current=true;setBusy(true);
    try {
      const values=draft.action==='retype'?{kind_ref:draft.kind}:draft.action==='move'?{target_type:scope.parentType,target_id:draft.target}:{};
      const next=await api<DocumentRecord>(endpoint(scope.parentType,scope.parentId)+'/'+record.id+'/operations/'+draft.action,'POST',{expected_version:draft.expectedVersion,...values});
      if (alive.current) completed(next);
    } catch(error){if(alive.current){setDraft({...draft,error});if(error instanceof ApiError&&error.status===403)denied();}}
    finally {inFlight.current=false;if(alive.current)setBusy(false);}
  };
  return <section className="arm2-document-operations" aria-label="Операции документа"><div className="arm2-actions arm2-document-toolbar">{saveAction}{!draft && allowed && <>{([record.archive_state==='Архивный'?'restore':'archive','retype','move'] as const).map(action=><button type="button" key={action} disabled={blocked||busy} onClick={()=>begin(action)}>{labels[action]}</button>)}</>}</div>{draft && <div className="arm2-operation-panel"><h4>{labels[draft.action]}</h4><ErrorMessage error={draft.error}/><fieldset disabled={!allowed||busy||blocked}>{draft.action==='retype' && <KindPicker path={endpoint(scope.parentType,scope.parentId)} value={draft.kind} change={kind=>setDraft({...draft,kind,error:undefined})} can={scope.can}/>} {draft.action==='move' && <><ParentPicker scope={scope} value={draft.target} change={target=>setDraft({...draft,target,error:undefined})}/><p>Привязка: {scope.parentId} → {draft.target||'получатель не выбран'}. ID, файлы и редакции сохраняются.</p></>}{draft.action==='archive' && <p>Документ останется в архиве с файлами и историей.</p>}{draft.action==='restore' && <p>Сервер повторно проверит применимость типа и свободный лимит.</p>}<button type="button" onClick={()=>void submit()}>{busy?'Выполняем…':draft.action==='move'?'Подтвердить перенос':'Подтвердить операцию'}</button></fieldset>{draft.error instanceof ApiError&&draft.error.status===409 && <div className="arm2-actions"><button type="button" onClick={compare}>Сравнить с сервером</button><button type="button" onClick={reload}>Перезагрузить карточку</button></div>}<button type="button" disabled={busy} onClick={()=>setDraft(undefined)}>Отмена операции</button></div>}</section>;
}

function DocumentForm({initial, parent, ...scope}: Scope & {initial?: DocumentRecord; parent: RecordData}) {
  const path = endpoint(scope.parentType, scope.parentId), back = parentLink(scope.parentType, scope.parentId);
  const key = path + '/' + (initial?.id || 'new');
  const [parked] = useState(() => parkedDrafts.get(key));
  const [original, setOriginal] = useState(parked?.original || initial), [draft, setDraft] = useState(() => parked?.draft || draftOf(initial));
  const [busy, setBusy] = useState(false), [denied, setDenied] = useState(parked?.denied || false), [error, setError] = useState<unknown>(parked?.error);
  useEffect(() => {parkedDrafts.delete(key);}, [key]);
  const [fileDraft, setFileDraft] = useState<FileDraft | undefined>(parked?.files);
  const [operationDraft,setOperationDraft]=useState<OperationDraft|undefined>(parked?.operation);
  const [validation, setValidation] = useState(''), [status, setStatus] = useState(''), [comparison, setComparison] = useState<DocumentRecord>();
  const [readError, setReadError] = useState<unknown>(), [history, setHistory] = useState(false);
  const alive = useAlive(), saving = useRef(false), navigate = useNavigate();
  const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(original));
  const guard = useDraftGuard(changed || busy || !!fileDraft || !!operationDraft, () => parkedDrafts.set(key, {original, draft, denied, error, files: fileDraft,operation:operationDraft}), () => parkedDrafts.delete(key));
  const canEdit = edit(scope) && !denied;
  const change = (value: Partial<Draft>) => {guard.current = true; setDraft(current => ({...current, ...value})); setStatus(''); setValidation('');};
  const readFresh = async (reload: boolean) => {
    if (!original || (reload && !window.confirm('Перезагрузить карточку и заменить несохранённые изменения данными сервера?'))) return;
    setReadError(undefined);
    try {
      const record = await api<DocumentRecord>(path + '/' + encodeURIComponent(original.id));
      if (!alive.current) return;
      if (reload) {guard.current = false; setOriginal(record); setDraft(draftOf(record)); setError(undefined); setComparison(undefined); setStatus(''); setValidation('');setOperationDraft(undefined);}
      else setComparison(record);
    } catch (error) {if (alive.current) setReadError(error);}
  };
  const save = async () => {
    if (!canEdit || saving.current || fileDraft || operationDraft) return;
    if (!draft.name.trim()) {setValidation('Укажите название карточки.'); return;}
    if (!draft.kind_ref) {setValidation('Выберите существующий тип документа.'); return;}
    const body = {name: draft.name.trim(), description: draft.description};
    saving.current = true; guard.current = true; setBusy(true); setError(undefined); setStatus(''); setValidation('');
    try {
      const record = await api<DocumentRecord>(path + (original ? '/' + encodeURIComponent(original.id) : ''), original ? 'PUT' : 'POST', original ? {...body, expected_version: original.version} : {...body, kind_ref: draft.kind_ref});
      if (!alive.current) return;
      guard.current = false; setOriginal(record); setDraft(draftOf(record)); setComparison(undefined);
      setStatus(original && original.version === record.version ? `Изменений нет — версия ${record.version}` : `Сохранено сервером — версия ${record.version}`);
      if (!original) navigate(path + '/' + encodeURIComponent(record.id), {replace: true});
    } catch (error) {if (alive.current) {setError(error); if (error instanceof ApiError && error.status === 403) setDenied(true);}}
    finally {saving.current = false; if (alive.current) setBusy(false);}
  };
  const saveAction = canEdit && <button type="submit" form="arm2-document-form" className="arm2-document-save" disabled={busy || !!fileDraft || !!operationDraft}>{busy ? 'Сохраняем…' : error instanceof ApiError && error.status >= 500 ? 'Повторить сохранение' : 'Сохранить'}</button>;
  return <article className="arm2-document"><Link to={back}>{changed || fileDraft ? 'Отмена / к документам родителя' : '← К документам родителя'}</Link>
    <header className="arm2-document-heading"><h2>{original ? 'Документ: ' + original.name : 'Добавить документ'}</h2>{original && <span className="arm2-document-state">{original.display_state}</span>}</header>
    <div className="arm2-document-summary"><span>Родитель: <Link to={back}>{parent.name}</Link> · {scope.parentType}</span>{original && <span>{original.archive_state}</span>}</div>
    {!original && <p className="arm2-note">Сначала сохраните карточку, затем добавьте файлы.</p>}{!canEdit && <p role="status">Только просмотр — нет права редактирования</p>}
    <ErrorMessage error={error}/><ErrorMessage error={readError}/>{validation && <p role="alert" className="error">{validation}</p>}
    {error instanceof ApiError && error.status === 409 && original && <div className="arm2-actions"><button type="button" onClick={() => void readFresh(false)}>Сравнить с сервером</button><button type="button" onClick={() => void readFresh(true)}>Перезагрузить карточку</button></div>}
    {comparison && <section aria-label="Версия на сервере"><h3>Версия на сервере (черновик ниже сохранён)</h3><p>{comparison.name} · Версия: {comparison.version}</p><p>{comparison.description ?? 'Описание не заполнено'}</p></section>}
    <form id="arm2-document-form" noValidate onSubmit={event => {event.preventDefault(); void save();}}><fieldset disabled={!canEdit || busy || !!fileDraft || !!operationDraft} className="arm2-document-fields">
      <label className="arm2-field">Название карточки *<input value={draft.name} required onChange={event => change({name: event.target.value})}/></label>
      {original ? <div className="arm2-field"><span>Тип документа</span><div className="arm2-document-kind-value">{original.kind_name}</div></div> : <KindPicker path={path} value={draft.kind_ref} change={kind_ref => change({kind_ref})} can={scope.can}/>}
      <label className="arm2-field arm2-document-description">Описание документа<textarea rows={1} placeholder="Добавить описание, если нужно" value={draft.description ?? ''} onChange={event => change({description: event.target.value})}/></label>
    </fieldset></form>
    {!original && <div className="arm2-actions arm2-document-toolbar">{saveAction}</div>}{status && <p role="status">{status}</p>}
    {original && <DocumentOperations saveAction={saveAction} scope={scope} record={original} draft={operationDraft} setDraft={setOperationDraft} allowed={canEdit} blocked={changed||busy||!!fileDraft} denied={()=>setDenied(true)} compare={()=>void readFresh(false)} reload={()=>void readFresh(true)} completed={record=>{guard.current=false;setOperationDraft(undefined);setOriginal(record);setDraft(draftOf(record));setStatus('Операция сохранена сервером — версия '+record.version);if(record.parent_id!==scope.parentId)navigate(endpoint(record.parent_type,record.parent_id)+'/'+record.id,{replace:true});}}/>}
    {original && <DocumentFiles path={path + '/' + encodeURIComponent(original.id)} original={original} editable={canEdit} blocked={changed || busy || !!operationDraft} draft={fileDraft} setDraft={setFileDraft} onDenied={()=>setDenied(true)} refreshed={async () => {
      const record = await api<DocumentRecord>(path + '/' + encodeURIComponent(original.id));
      if (!alive.current) return;
      guard.current = false; setOriginal(record); setDraft(draftOf(record)); setFileDraft(undefined); setStatus('');
    }}/>}
    {original && <details className="arm2-document-technical"><summary>Технические сведения</summary><p>Постоянный ID: {original.id} · Версия: {original.version}</p><p>Тип документа: {original.kind_name} · {original.kind_ref}</p><p>Родитель: {scope.parentType} · {scope.parentId}</p></details>}
    {original && <div className="arm2-history-control"><button type="button" className="arm2-document-link" aria-expanded={history} onClick={() => setHistory(value => !value)}>{history ? 'Закрыть историю' : 'История'}</button>{history && <History type="DOCUMENT" id={original.id}/>}</div>}
  </article>;
}
