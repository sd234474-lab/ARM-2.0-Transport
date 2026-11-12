import { api } from '../api';

export type Kind = 'TYPE' | 'VEH' | 'CP' | 'ORG';
export type ObjectType = Kind | 'CPREP' | 'ORGREP';
export type Scalar = string | number | boolean | null;
export interface RecordData { id: string; version: number; name: string; [field: string]: Scalar }
export interface VehicleType extends RecordData { requires_sts: boolean | null; requires_calibration: boolean | null; requires_wialon: boolean | null; status: string }
export interface Vehicle extends RecordData { plate_number: string; type_id: string | null; owner_cp_id: string | null; owner_org_id: string | null; accounting_state: string; model: string | null; wialon_unit_id: string | null }
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
