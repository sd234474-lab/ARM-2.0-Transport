import type { ReactNode } from 'react';

export function CatalogRow({children}: {children: ReactNode}) {
  return <tr className="arm2-catalog-row" onClick={event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!(event.target instanceof Element) || event.target.closest('a, button, input, select, textarea, label')) return;
    if (window.getSelection()?.toString()) return;
    event.currentTarget.querySelector<HTMLAnchorElement>('a[href]')?.click();
  }}>{children}</tr>;
}
