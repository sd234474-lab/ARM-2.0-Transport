import { Link, useSearchParams } from 'react-router-dom';
import { catalogs, pageSize, searchPage } from './api';
import type { Kind, RecordData } from './api';
import type { Can } from './Workspace';
import { OwnerPicker, TypePicker } from './Selectors';
import { display, ErrorMessage, useLoad } from './shared';

export function plateLabel(value: unknown): string {
  const text = String(value || '');
  return text.replace(/^([АВЕКМНОРСТУХ])(\d{3})([АВЕКМНОРСТУХ]{2})(\d{2,3})$/, '$1 $2 $3 $4').replace(/^(\d{4})([АВЕКМНОРСТУХ]{2})(\d{2})$/, '$1 $2 $3') || 'Не заполнено';
}
export function VehicleTable({rows}: {rows: RecordData[]}) {
  return <div className="arm2-table"><table><thead><tr><th>Рабочее имя</th><th>Госномер</th><th>Тип</th><th>Операционный владелец</th><th>Учётное состояние</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><Link to={`/arm2/veh/${encodeURIComponent(row.id)}`}>{row.name}</Link></td><td>{plateLabel(row.plate_number)}</td><td>{row.type_id ? row.type_name || 'Текущее назначение недоступно' : 'Не назначен'}</td><td>{row.owner_cp_id || row.owner_org_id ? `${row.owner_kind === 'ORG' ? 'Организация' : 'Контрагент'} · ${row.owner_name || 'Нет доступа к имени'}` : 'Без владельца'}</td><td>{display(row.accounting_state)}</td></tr>)}</tbody></table></div>;
}

export default function SearchCatalog({kind, can, owner}: {kind: Exclude<Kind, 'TYPE'>; can: Can; owner?: {kind: 'CP' | 'ORG'; id: string}}) {
  const [params, setParams] = useSearchParams();
  // Embedded vehicle table uses distinct URL keys and cannot overwrite a parent form.
  const prefix = owner ? 'veh_' : '';
  const get = (key: string) => params.get(prefix + key) || '';
  const raw = Number(get('offset')), offset = Number.isSafeInteger(raw) && raw >= 0 && raw % pageSize === 0 ? raw : 0;
  const change = (values: Record<string, string>, reset = true) => {const next = new URLSearchParams(params); if (reset) next.delete(prefix + 'offset'); Object.entries(values).forEach(([key, value]) => value ? next.set(prefix + key, value) : next.delete(prefix + key)); setParams(next, {replace: true});};
  const query = new URLSearchParams({limit: String(pageSize), offset: String(offset)});
  const fields = kind === 'VEH' ? ['q', 'type_id', 'owner_kind', 'owner_id', 'state', 'sort', 'direction'] : ['q', 'status', 'legal_form', 'sort', 'direction'];
  fields.forEach(key => {if (get(key)) query.set(key, get(key));});
  if (owner) {query.set('owner_kind', owner.kind); query.set('owner_id', owner.id);}
  const key = query.toString(), permitted = can(catalogs[kind].permission + '_view');
  const result = useLoad(signal => permitted ? searchPage(catalogs[kind].path, new URLSearchParams(key), signal) : Promise.resolve({items: [], total: 0, matched: 0, limit: pageSize, offset}), [kind, key, permitted]);
  if (!permitted) return <p>Нет права просмотра техники</p>;
  const sorting = kind === 'VEH' ? [['name', 'Рабочее имя'], ['plate_number', 'Госномер'], ...(can('arm2_type_view') ? [['type', 'Тип']] : []), ...(can('arm2_cp_view') && can('arm2_org_view') ? [['owner', 'Владелец']] : []), ['accounting_state', 'Учётное состояние']] : [['name', 'Рабочее имя'], ['full_name', 'Полное наименование'], ['legal_form', 'ОПФ'], ['inn', 'ИНН'], ['status', 'Состояние']];
  return <section className="arm2-search-catalog"><header className="arm2-actions"><h2>{owner ? 'Техника' : catalogs[kind].label}</h2>{!owner && can(catalogs[kind].permission + '_edit') && <Link className="arm2-create" to={`/arm2/${kind.toLowerCase()}/new`}>+ Новая запись</Link>}</header>
    <div className="arm2-grid arm2-filters"><label className="arm2-field">{kind === 'VEH' ? 'Поиск по рабочему имени / госномеру' : 'Поиск по имени / полному наименованию / ИНН'}<input type="search" value={get('q')} onChange={e => change({q: e.target.value})}/></label>
      {kind === 'VEH' ? <><TypePicker label="Фильтр: тип техники" selected={get('type_id')} filter can={can} editable change={value => change({type_id: String(value || '')})}/>{!owner && <OwnerPicker label="Фильтр: операционный владелец" cp={get('owner_kind') === 'CP' ? get('owner_id') : null} org={get('owner_kind') === 'ORG' ? get('owner_id') : null} none unowned={get('owner_kind') === 'NONE'} can={can} editable change={(cp, org, empty) => change({owner_kind: empty ? 'NONE' : cp ? 'CP' : org ? 'ORG' : '', owner_id: cp || org || ''})}/>}<label className="arm2-field">Учётное состояние<select value={get('state')} onChange={e => change({state: e.target.value})}><option value="">Все</option><option>Действующая</option><option>Архивная</option></select></label></> : <><label className="arm2-field">Состояние<select value={get('status')} onChange={e => change({status: e.target.value})}><option value="">Все</option><option value="CONFIRMED">Описание заполнено</option><option value="REVIEW">Описание не завершено</option></select></label><label className="arm2-field">ОПФ (точное значение)<input value={get('legal_form')} onChange={e => change({legal_form: e.target.value})}/></label></>}
      <label className="arm2-field">Сортировка<select value={get('sort') || 'name'} onChange={e => change({sort: e.target.value})}>{sorting.map(([value, name]) => <option value={value} key={value}>{name}</option>)}</select></label><label className="arm2-field">Порядок<select value={get('direction') || 'asc'} onChange={e => change({direction: e.target.value})}><option value="asc">По возрастанию</option><option value="desc">По убыванию</option></select></label>
    </div><ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем список…</p>}{result.data && <><p className="arm2-counts">Найдено: {result.data.matched} · Всего доступно: {result.data.total}</p>{kind === 'VEH' ? <VehicleTable rows={result.data.items}/> : <div className="arm2-table"><table><thead><tr><th>Рабочее имя</th><th>Полное наименование</th><th>ОПФ</th><th>ИНН</th><th>Состояние</th></tr></thead><tbody>{result.data.items.map(row => <tr key={row.id}><td><Link to={`/arm2/${kind.toLowerCase()}/${encodeURIComponent(row.id)}`}>{row.name}</Link></td><td>{display(row.full_name)}</td><td>{display(row.legal_form)}</td><td>{display(row.inn)}</td><td>{row.status === 'CONFIRMED' ? 'Описание заполнено' : 'Описание не завершено'}</td></tr>)}</tbody></table></div>}{result.data.items.length === 0 && <p>По заданным условиям записей нет</p>}<nav className="arm2-actions" aria-label="Страницы"><button type="button" disabled={!offset} onClick={() => change({offset: String(Math.max(0, offset - pageSize))}, false)}>Предыдущая страница</button><span>Страница {offset / pageSize + 1}</span><button type="button" disabled={offset + pageSize >= result.data.matched} onClick={() => change({offset: String(offset + pageSize)}, false)}>Следующая страница</button></nav></>}
    {kind === 'VEH' && <p className="arm2-note">Контроль готовности — отдельная зависимость VEH.R02; учётное состояние его не заменяет.</p>}{owner && <p>Ручное назначение техники из этой карточки предусмотрено этапом B2.</p>}
  </section>;
}
