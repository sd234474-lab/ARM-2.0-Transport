export interface CatalogFilter {
  label: string;
  count?: number;
  active: boolean;
  select: () => void;
}

export function CatalogFilters({items}: {items: CatalogFilter[]}) {
  return <div className="arm2-catalog-filters" role="group" aria-label="Быстрые фильтры">
    {items.map(({label, count, active, select}) => <button key={label} type="button" aria-pressed={active} onClick={select}>
      {label}{count !== undefined && <>: <strong>{count}</strong></>}
    </button>)}
  </div>;
}
