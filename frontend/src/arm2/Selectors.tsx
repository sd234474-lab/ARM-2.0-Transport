import { useId, useRef, useState } from 'react';
import { catalogs, detail, searchPage } from './api';
import type { Kind, RecordData, Scalar } from './api';
import type { Can } from './Workspace';
import { missingRequirements } from './model';
import { ErrorMessage, useAlive, useLoad } from './shared';

type Option = {id: string; name: string; disabled?: boolean; hint?: string};
function Picker({label, selected, load, resolve, change, disabled = false, identity, clearLabel = 'Не назначено'}: {
  label: string; selected: string; load: (q: string, signal: AbortSignal) => Promise<Option[]>;
  resolve: (signal: AbortSignal) => Promise<string>; change: (value: string) => void; disabled?: boolean; identity: string; clearLabel?: string;
}) {
  const [query, setQuery] = useState(''), [open, setOpen] = useState(false), [active, setActive] = useState(0);
  const id = useId();
  const selectedLabel = useLoad(resolve, [selected, identity]);
  const result = useLoad(signal => open && !disabled ? load(query, signal) : Promise.resolve([]), [open, query, disabled, identity]);
  const choices: Option[] = [...(result.data || []), {id: '', name: clearLabel}];
  const choose = (item: Option) => {if (!item.disabled) {change(item.id); setOpen(false); setQuery('');}};
  return <div className="arm2-picker" onBlur={e => {if (!e.currentTarget.contains(e.relatedTarget)) {setOpen(false); setQuery('');}}}>
    <label className="arm2-field"><span>{label}</span><input role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={id} aria-activedescendant={open ? `${id}-${active}` : undefined}
      disabled={disabled} value={open ? query : selected ? selectedLabel.data || 'Текущее назначение' : ''} placeholder={clearLabel}
      onFocus={() => {setOpen(true); setQuery(''); setActive(0);}} onClick={() => {if (!open) {setOpen(true); setQuery(''); setActive(0);}}} onChange={e => {setQuery(e.target.value); setOpen(true); setActive(0);}}
      onKeyDown={e => {if (e.key === 'Escape') {setOpen(false); setQuery('');} else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {e.preventDefault(); setOpen(true); setActive(x => Math.max(0, Math.min(choices.length - 1, x + (e.key === 'ArrowDown' ? 1 : -1))));} else if (e.key === 'Enter' && open) {e.preventDefault(); const item = choices[active]; if (item && (!query.trim() || item.id) && !result.loading && !result.error) choose(item);}}}/></label>
    <ErrorMessage error={result.error || selectedLabel.error} retry={result.reload}/>
    {open && <div className="arm2-options" role="listbox" id={id} aria-label={label}>{result.loading ? <p role="status">Поиск…</p> : choices.map((item, index) => <button type="button" role="option" id={`${id}-${index}`} aria-selected={item.id === selected} disabled={item.disabled || !!result.error} key={item.id} onMouseDown={e => e.preventDefault()} onMouseEnter={() => setActive(index)} onClick={() => choose(item)}>{item.name}{item.hint && <small>{item.hint}</small>}</button>)}{!result.loading && result.data?.length === 0 && <p>Совпадений нет</p>}</div>}
    {open && <small>Поиск по всему справочнику. Выберите вариант; ввод текста сам по себе назначение не меняет.</small>}
  </div>;
}

export function PartyPicker({kind, selected, change}: {kind: 'CP' | 'ORG'; selected: string; change: (id: string) => void}) {
  const path = catalogs[kind].path;
  return <Picker label={kind === 'CP' ? 'Контрагент' : 'Организация'} selected={selected} identity={kind}
    clearLabel={kind === 'CP' ? 'Выберите контрагента' : 'Выберите организацию'} change={change}
    resolve={signal => selected ? detail<RecordData>(`${path}/${encodeURIComponent(selected)}`, signal).then(row => row.name) : Promise.resolve('')}
    load={async (q, signal) => (await searchPage(path, new URLSearchParams({q, limit: '100'}), signal)).items.map(row => ({
      id: row.id, name: row.name, hint: row.inn ? `ИНН ${row.inn}` : row.id,
    }))}/>;
}

export function TypePicker({selected, change, can, editable, currentType, label = 'Тип техники', filter = false}: {
  selected: Scalar | undefined; change: (value: Scalar) => void; can: Can; editable: boolean; currentType?: Scalar; label?: string; filter?: boolean;
}) {
  const permitted = can('arm2_type_view'), value = String(selected || '');
  return <Picker label={label} selected={value} disabled={!permitted || !editable} identity={`TYPE:${permitted}:${currentType}:${filter}`} change={id => change(id || null)}
    resolve={signal => value && permitted ? detail<RecordData>(`${catalogs.TYPE.path}/${encodeURIComponent(value)}`, signal).then(row => row.name) : Promise.resolve(value ? 'Текущее назначение (нет доступа)' : '')}
    load={async (q, signal) => (await searchPage(catalogs.TYPE.path, new URLSearchParams({q, limit: '100'}), signal)).items.map(row => ({id: row.id, name: row.name,
      disabled: !filter && row.status !== 'CONFIRMED' && row.id !== currentType,
      hint: row.status !== 'CONFIRMED' ? `REVIEW: ${missingRequirements(row) || 'описание не завершено'}${row.id === currentType ? ' — текущее назначение сохраняется' : ''}` : undefined}))}/>;
}

export function OwnerPicker({cp, org, change, can, editable, label = 'Операционный владелец', none = false, unowned = false, beforeChange}: {
  cp: Scalar | undefined; org: Scalar | undefined; change: (cp: string | null, org: string | null, empty?: boolean) => void;
  can: Can; editable: boolean; label?: string; none?: boolean; unowned?: boolean;
  beforeChange?: (cp:string|null,org:string|null)=>Promise<boolean>;
}) {
  const [pending,setPending]=useState(false),[error,setError]=useState<unknown>();
  const inFlight=useRef(false),alive=useAlive();
  const value = unowned ? 'NONE' : cp ? `CP:${cp}` : org ? `ORG:${org}` : '';
  const cpAllowed = can('arm2_cp_view'), orgAllowed = can('arm2_org_view');
  return <><ErrorMessage error={error}/><Picker label={label} selected={value} identity={`OWNER:${cpAllowed}:${orgAllowed}`} disabled={pending || !editable || (!cpAllowed && !orgAllowed)}
    clearLabel={none ? 'Любой владелец' : 'Без владельца'}
    change={id => {if(inFlight.current)return;const [kind,selected]=id.split(':');const nextCp=kind==='CP'?selected:null,nextOrg=kind==='ORG'?selected:null;
      if(!beforeChange){change(nextCp,nextOrg,id==='NONE');return;}
      inFlight.current=true;setPending(true);setError(undefined);
      void beforeChange(nextCp,nextOrg).then(confirmed=>{if(confirmed&&alive.current)change(nextCp,nextOrg,id==='NONE');}).catch(error=>{if(alive.current)setError(error);}).finally(()=>{inFlight.current=false;if(alive.current)setPending(false);});}}
    resolve={async signal => {const kind = cp ? 'CP' : 'ORG'; if (value === 'NONE') return 'Без владельца'; if (!value) return ''; if (!can(catalogs[kind].permission + '_view')) return 'Текущий владелец (нет доступа)'; const row = await detail<RecordData>(`${catalogs[kind].path}/${encodeURIComponent(String(cp || org))}`, signal); return `${kind === 'CP' ? 'Контрагент' : 'Организация'} · ${row.name}`;}}
    load={async (q, signal) => {
      const kinds: Kind[] = [...(cpAllowed ? ['CP' as const] : []), ...(orgAllowed ? ['ORG' as const] : [])];
      const pages = await Promise.all(kinds.map(async kind => (await searchPage(catalogs[kind].path, new URLSearchParams({q, limit: '100'}), signal)).items.map(row => ({id: `${kind}:${row.id}`, name: `${kind === 'CP' ? 'Контрагент' : 'Организация'} · ${row.name}`}))));
      return [...(none ? [{id: 'NONE', name: 'Без владельца'}] : []), ...pages.flat()];
    }}/>{pending && <p role="status">Проверяем прежнего и нового владельца…</p>}</>;
}
