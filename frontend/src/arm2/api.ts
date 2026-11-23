import { api } from '../api';
import { ApiError } from '../api';
import { apiUrl } from '../workspace';

// Read verified ARM bytes; never frame an authenticated API response (global X-Frame-Options: DENY).
// Writes continue to use the existing api()/CSRF implementation unchanged.
export async function previewBytes(path: string, maximum: number): Promise<Blob> {
  const response = await fetch(apiUrl(path), {credentials: 'same-origin', redirect: 'error'});
  if (!response.ok) {
    const error = await response.json().catch(() => ({detail:'Файл временно недоступен'}));
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(response.status, error.detail);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError(503, 'Не удалось прочитать файл');
  const chunks: Uint8Array<ArrayBuffer>[] = []; let size = 0;
  try {
    for (;;) {const {done, value} = await reader.read(); if (done) break; size += value.byteLength; if (size > maximum) throw new ApiError(413, 'Файл превышает установленный предел'); chunks.push(new Uint8Array(value));}
  } finally {await reader.cancel();}
  const type = response.headers.get('Content-Type')?.split(';')[0] || 'application/octet-stream';
  const disposition = response.headers.get('Content-Disposition') || '';
  let filename: string | undefined;
  try {
    const encoded = /filename\*=utf-8''([^;]+)/i.exec(disposition);
    filename = encoded ? decodeURIComponent(encoded[1]) : /filename="([^"]+)"/i.exec(disposition)?.[1];
  } catch { /* Missing/malformed metadata retains the caller's existing display fallback. */ }
  if (filename && filename.length <= 255 && !/[\x00-\x1f\x7f/\\:]/.test(filename) && !['.', '..'].includes(filename))
    return new File(chunks, filename, {type});
  return new Blob(chunks, {type});
}

export type Kind = 'TYPE' | 'VEH' | 'CP' | 'ORG';
export type QuestionnaireField = {current: unknown; proposed: unknown; selected: boolean; equal: boolean; error: string | null;clear_candidate?:boolean;clear_selected?:boolean;reason?:string;current_display?:string;proposed_display?:string};
export type QuestionnaireChoices = {cp_target?: string | null; cp_fields?: string[];cp_clears?:string[];cp_values?:{type?:string|null;legal_form?:string|null};confirm_identity?:boolean;
  representatives?: Record<string,{target?: string | null; fields?: string[]; signer?: boolean; sources?: Record<string,'passport'|'list'>}>;
  vehicles?: Record<string,{fields?: string[]; include?: boolean; type_id?: string | null; without_type?: boolean; confirm_owner?: boolean}>;
  documents?: Record<string,{upload_id?: string | null; parent_key?: string | null; kind_id?: string | null; document_id?: string | null; replace_file_id?: string | null; append?: boolean; accept?: boolean}>};
export type QuestionnaireReceipt = {without_documents?:boolean;documents_pending?:boolean;command_id: string; draft_id: string; cp_id: string | null; filled_date: string | null; uploaded_at: string; accepted_at: string; correlation_id: string; no_op: boolean; event_ids: number[]; changes?:{kind:string;id:string;operation:string;url?:string;fields:Record<string,{before:unknown;after:unknown}>}[];objects: {kind: string; name?:string;url?:string; id?: string; document_id?: string; version: number}[]};
export type QuestionnaireRow = {key: string; row: number; id: string | null; plate_number: string | null; version: number | null; fields: Record<string,QuestionnaireField>; errors: string[]; withdrawal: boolean; confirm_owner: boolean; without_type: boolean; source: string; volume_proposal: string; volume_notice: string; legacy_hint?: string; diagnostic_hint?: string};
export type QuestionnaireDocument = {key: string; upload_id?: string; name: string; attachment_options?:{id:string;name:string}[]; parent_key: string | null; role: string; parent_type?: 'CP'|'VEH'; parent_id?: string; document_id?: string | null; kind_id?: string | null; version?: number; revision_id?: string; replace_file_id?: string | null; selected: boolean; equal?: boolean; error?: string | null; sha256?: string; media_type?: string; candidates?: {id:string; name:string; kind_id:string}[]; current_files?: {id:string; name:string; sha256:string}[]};
export type QuestionnaireDraft = {id:string; revision:number; status:'OPEN'|'ACCEPTED'|'CANCELLED'; approval_reset?:boolean; documents_pending?:boolean; filename?:string; choices?:QuestionnaireChoices; preview_id?:string; receipt?:QuestionnaireReceipt;
  comparison?:{cp:{key:string;id:string|null;target:string|null;version:number|null;error:string|null;hint:string;identity_conflict?:boolean;name?:string;identity_conflict_reasons?:{field:string;label:string;current:unknown;proposed:unknown}[];notices?:string[];candidates:{id:string;name:string;inn:string|null}[];fields:Record<string,QuestionnaireField>};
    representatives:{key:string;id:string|null;target:string|null;version:number|null;error:string|null;signer:boolean;source_id?:string|null;source_row?:number|null;source_comment?:string|null;signer_hint?:boolean;conflicts?:Record<string,{passport:string;list:string}>;sources?:Record<string,'passport'|'list'>;fields:Record<string,QuestionnaireField>;candidates:{id:string;name:string}[]}[];
    vehicles:QuestionnaireRow[];documents:QuestionnaireDocument[];types:{id:string;name:string;plate_category:string}[];document_kinds:{id:string;name:string;parents:string[]}[];
    document_kind_notice?:string|null;warnings:string[];filled_date:string|null;uploaded_at:string}};
export const questionnaireApi = {
  list:()=>api<{id:string;filename:string;revision:number;uploaded_at:string}[]>('/arm2/questionnaires'),
  receipts:()=>api<QuestionnaireReceipt[]>('/arm2/questionnaires/receipts'),
  upload:(file:File)=>api<QuestionnaireDraft>('/arm2/questionnaires?filename='+encodeURIComponent(file.name),'POST',file),
  reopen:(id:string)=>api<QuestionnaireDraft>('/arm2/questionnaires/'+encodeURIComponent(id)),
  edit:(draft:QuestionnaireDraft,choices:QuestionnaireChoices)=>api<QuestionnaireDraft>('/arm2/questionnaires/'+draft.id,'PUT',{revision:draft.revision,choices}),
  apply:(draft:QuestionnaireDraft,command_id:string,without_documents=false)=>api<QuestionnaireReceipt>('/arm2/questionnaires/'+draft.id+'/apply','POST',{command_id,revision:draft.revision,preview_id:draft.preview_id,...(without_documents?{without_documents:true}:{})}),
  cancel:(id:string)=>api<{id:string;status:string;cleanup?:{pending:number}}>('/arm2/questionnaires/'+id,'DELETE'),
  cleanupCandidates:()=>api<{id:string;filename:string;status:string}[]>('/arm2/questionnaires/cleanup'),
  cleanup:(id:string)=>api<{pending:number}>('/arm2/questionnaires/'+id+'/cleanup','POST',{}),
};
export type ObjectType = Kind | 'CPREP' | 'ORGREP' | 'DOC-KIND' | 'DOCUMENT' | 'APPROVAL' | 'APPROVAL-ITEM';
export interface DocumentRecord extends RecordData {parent_type: DocumentParent; parent_id: string; kind_ref: string; kind_name: string; description: string | null; archive_state: 'Неархивный' | 'Архивный'; display_state: 'Не загружен' | 'Доступен' | 'Файл недоступен' | 'Архивный'}
export type DocumentParent = 'CP' | 'ORG' | 'VEH';
export type FileNamingPreset = 'DOCUMENT_NAME' | 'PARENT_AND_KIND' | 'VEH_CALIBRATION';
export interface DocumentKind {id: string; name: string; applicable_object_types: DocumentParent[]; max_current_documents: number | null; version: number; file_naming_template?: FileNamingPreset | null}
export interface DocumentFile {download_name?: string; id: string; original_upload_name: string; stored_name: string; size: number; sha256: string; media_type: string; extension: string; position: number; correction?: {operation: 'CORRECT'; reason_code: 'incorrect_upload'; actor_id: number; occurred_at: string; history_event_id: number} | null}
export interface DocumentRevision {id: string; number: number; document_id: string; parent_type: DocumentParent; parent_id: string; author_id: number; occurred_at: string; source: string; files: DocumentFile[]}
export interface FileBundle {document_id: string; document_version: number; current_revision_id: string | null; display_state: string; revisions: DocumentRevision[]; max_files: number; max_file_bytes: number}
export interface FileStage {download_name?: string; id: string; original_upload_name: string; size: number; sha256: string | null; media_type: string | null; extension: string | null; state: 'empty' | 'ready' | 'consumed'}
export type Scalar = string | number | boolean | null;
export interface RecordData { id: string; version: number; name: string; [field: string]: Scalar }
export interface VehicleType extends RecordData { plate_category: 'Автомобильный' | 'Самоходная техника' | null; requires_sts: boolean | null; requires_calibration: boolean | null; requires_wialon: boolean | null; status: string }
export interface Vehicle extends RecordData { plate_number: string; type_id: string | null; owner_cp_id: string | null; owner_org_id: string | null; accounting_state: string; model: string | null; wialon_unit_id: string | null }
export interface OwnerIdentity {kind: 'CP' | 'ORG' | null; id: string | null}
export interface OwnerCommand {expected_version: number; target_kind: OwnerIdentity['kind']; target_id: string | null; expected_owner_kind: OwnerIdentity['kind']; expected_owner_id: string | null; confirm_reassignment: boolean; initiator_kind?: 'CP' | 'ORG'; initiator_id?: string}
export const ownerOf = (record: RecordData): OwnerIdentity => record.owner_cp_id ? {kind:'CP',id:String(record.owner_cp_id)} : record.owner_org_id ? {kind:'ORG',id:String(record.owner_org_id)} : {kind:null,id:null};
export const sameOwner = (a: OwnerIdentity,b: OwnerIdentity) => a.kind===b.kind && a.id===b.id;
export const setOperationalOwner = (id: string, command: OwnerCommand) => api<Vehicle & {no_op:boolean}>(`/arm2/vehicles/${encodeURIComponent(id)}/operational-owner`,'POST',command);
export interface Party extends RecordData { primary_signer_id: string | null }
export interface Representative extends RecordData { parent_id: string; user_id: number | null; state: string }
export interface EventSummary { event_id: number; object_type: ObjectType; object_id: string; operation: string; occurred_at: string; actor_kind: string; actor_id: string; source: string; version_before: number | null; version_after: number | null }
export interface EventDetail extends EventSummary { changes: Record<string, {before: unknown; after: unknown}> }
export const catalogs = {
  TYPE: { path: '/arm2/types', label: 'Типы техники', singular: 'Тип техники', permission: 'arm2_type' },
  VEH: { path: '/arm2/vehicles', label: 'Техника', singular: 'Машина', permission: 'arm2_veh' },
  CP: { path: '/arm2/counterparties', label: 'Контрагенты', singular: 'Контрагент', permission: 'arm2_cp' },
  ORG: { path: '/arm2/organizations', label: 'Собственные организации', singular: 'Собственная организация', permission: 'arm2_org' },
} as const;
export const pageSize = 100;
export interface SearchPage {facets?: Record<string, number>; without_owner?: number; without_wialon?:number; items: RecordData[]; total: number; matched: number; limit: number; offset: number}
export const searchPage = (path: string, params: URLSearchParams, signal?: AbortSignal) => api<SearchPage>(`${path}/search?${params}`, 'GET', undefined, signal);
export const list = <T,>(path: string, offset = 0, signal?: AbortSignal) => api<T[]>(`${path}?limit=${pageSize}&offset=${offset}`, 'GET', undefined, signal);
export const detail = <T,>(path: string, signal?: AbortSignal) => api<T>(path, 'GET', undefined, signal);
export const save = (path: string, body: Record<string, Scalar>, editing: boolean) => api<RecordData>(path, editing ? 'PUT' : 'POST', body);
// Selectors need the complete permitted dictionary, never just its first page.
export async function all<T>(path: string, signal?: AbortSignal): Promise<T[]> {
  const result: T[] = [];
  for (let offset = 0; ; offset += pageSize) {
    const page = await list<T>(path, offset, signal); result.push(...page);
    if (page.length < pageSize) return result;
  }
}
