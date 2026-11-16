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
  return new Blob(chunks, {type:response.headers.get('Content-Type')?.split(';')[0] || 'application/octet-stream'});
}

export type Kind = 'TYPE' | 'VEH' | 'CP' | 'ORG';
export type ObjectType = Kind | 'CPREP' | 'ORGREP' | 'DOC-KIND' | 'DOCUMENT';
export interface DocumentRecord extends RecordData {parent_type: DocumentParent; parent_id: string; kind_ref: string; kind_name: string; description: string | null; archive_state: 'Неархивный' | 'Архивный'; display_state: 'Не загружен' | 'Доступен' | 'Файл недоступен' | 'Архивный'}
export type DocumentParent = 'CP' | 'ORG' | 'VEH';
export type FileNamingPreset = 'DOCUMENT_NAME' | 'PARENT_AND_KIND';
export interface DocumentKind {id: string; name: string; applicable_object_types: DocumentParent[]; max_current_documents: number | null; version: number; file_naming_template?: FileNamingPreset | null}
export interface DocumentFile {id: string; original_upload_name: string; stored_name: string; size: number; sha256: string; media_type: string; extension: string; position: number; correction?: {operation: 'CORRECT'; reason_code: 'incorrect_upload'; actor_id: number; occurred_at: string; history_event_id: number} | null}
export interface DocumentRevision {id: string; number: number; document_id: string; parent_type: DocumentParent; parent_id: string; author_id: number; occurred_at: string; source: string; files: DocumentFile[]}
export interface FileBundle {document_id: string; document_version: number; current_revision_id: string | null; display_state: string; revisions: DocumentRevision[]; max_files: number; max_file_bytes: number}
export interface FileStage {id: string; original_upload_name: string; size: number; sha256: string | null; media_type: string | null; extension: string | null; state: 'empty' | 'ready' | 'consumed'}
export type Scalar = string | number | boolean | null;
export interface RecordData { id: string; version: number; name: string; [field: string]: Scalar }
export interface VehicleType extends RecordData { requires_sts: boolean | null; requires_calibration: boolean | null; requires_wialon: boolean | null; status: string }
export interface Vehicle extends RecordData { plate_number: string; type_id: string | null; owner_cp_id: string | null; owner_org_id: string | null; accounting_state: string; model: string | null; wialon_unit_id: string | null }
export interface OwnerIdentity {kind: 'CP' | 'ORG' | null; id: string | null}
export interface OwnerCommand {expected_version: number; target_kind: OwnerIdentity['kind']; target_id: string | null; expected_owner_kind: OwnerIdentity['kind']; expected_owner_id: string | null; confirm_reassignment: boolean; initiator_kind?: 'CP' | 'ORG'; initiator_id?: string}
export const ownerOf = (record: RecordData): OwnerIdentity => record.owner_cp_id ? {kind:'CP',id:String(record.owner_cp_id)} : record.owner_org_id ? {kind:'ORG',id:String(record.owner_org_id)} : {kind:null,id:null};
export const sameOwner = (a: OwnerIdentity,b: OwnerIdentity) => a.kind===b.kind && a.id===b.id;
export const setOperationalOwner = (id: string, command: OwnerCommand) => api<Vehicle & {no_op:boolean}>(`/arm2/vehicles/${encodeURIComponent(id)}/operational-owner`,'POST',command);
export interface Party extends RecordData { status: string; primary_signer_id: string | null }
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
export interface SearchPage {items: RecordData[]; total: number; matched: number; limit: number; offset: number}
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
