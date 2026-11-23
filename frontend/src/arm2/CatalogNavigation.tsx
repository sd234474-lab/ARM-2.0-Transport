import { Building2, FileCheck2, Files, Map, Truck, Users, Wrench } from 'lucide-react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import type { Can } from './Workspace';

const unavailable = [
  {label: 'Договоры', description: 'Редакции, стороны и цены', Icon: FileCheck2},
  {label: 'Территории', description: 'Районы, клетки и зоны', Icon: Map},
  {label: 'Нормативы', description: 'Работы и объекты', Icon: Wrench},
];

export function CatalogNavigation({can}: {can: Can}) {
  const {pathname} = useLocation();
  const [, , route] = pathname.split('/');
  const vehicleView = can('arm2_veh_view');
  const typeView = can('arm2_type_view');
  const cpView = can('arm2_cp_view');
  const documentView = can('arm2_document_view');
  const vehicleSection = ['veh', 'type', 'approvals'].includes(route);
  const cpSection = ['cp', 'questionnaires'].includes(route);
  const sections = [
    {label: 'Техника', description: 'Машины, типы и согласования', Icon: Truck, to: vehicleView ? '/arm2/veh' : '/arm2/type', visible: vehicleView || typeView, active: vehicleSection},
    {label: 'Организации', description: 'Собственные реквизиты', Icon: Building2, to: '/arm2/org', visible: can('arm2_org_view'), active: route === 'org'},
    {label: 'Контрагенты', description: 'Подрядчики, анкеты и документы', Icon: Users, to: '/arm2/cp', visible: cpView, active: cpSection},
    {label: 'Типы документов', description: 'Применимость и имена файлов', Icon: Files, to: '/arm2/document-kinds', visible: can('arm2_doc_kind_view'), active: route === 'document-kinds'},
  ];

  return <>
    <nav className="arm2-sections" aria-label="Справочники АРМ 2.0">
      {sections.filter(section => section.visible).map(({label, description, Icon, to, active}) =>
        <Link className="arm2-section" key={to} to={to} aria-label={label} aria-current={active ? 'location' : undefined}>
          <Icon size={20} aria-hidden="true"/><span><strong>{label}</strong><small>{description}</small></span>
        </Link>)}
      {unavailable.map(({label, description, Icon}) => <span className="arm2-section" aria-disabled="true" title="Пока не реализовано" key={label}>
        <Icon size={20} aria-hidden="true"/><span><strong>{label}</strong><small>{description}</small></span>
      </span>)}
    </nav>
    {vehicleSection && <nav className="arm2-subsections" aria-label="Подразделы техники">
      {vehicleView && <NavLink to="/arm2/veh">Техника</NavLink>}
      {typeView && <NavLink to="/arm2/type">Типы техники</NavLink>}
      {vehicleView && documentView && <NavLink to="/arm2/approvals">Согласование с заказчиком</NavLink>}
    </nav>}
    {cpSection && cpView && <nav className="arm2-subsections" aria-label="Подразделы контрагентов">
      <Link to="/arm2/cp" aria-current={route === 'cp' && !pathname.startsWith('/arm2/cp/documents') ? 'page' : undefined}>Контрагенты</Link>
      <NavLink to="/arm2/questionnaires">Анкеты</NavLink>
      {documentView && <NavLink to="/arm2/cp/documents">Документы</NavLink>}
    </nav>}
    {route === 'org' && can('arm2_org_view') && <nav className="arm2-subsections" aria-label="Подразделы организаций">
      <Link to="/arm2/org" aria-current={!pathname.startsWith('/arm2/org/documents') ? 'page' : undefined}>Организации</Link>
      {documentView && <NavLink to="/arm2/org/documents">Документы</NavLink>}
    </nav>}
  </>;
}
