import { Link } from 'react-router-dom';
import { all, catalogs } from './api';
import type { Kind, RecordData, Scalar } from './api';
import type { Can } from './Workspace';
import { shortRepresentativeName } from './model';
import { display, ErrorMessage, Field, useLoad } from './shared';

export default function Representatives({kind, parent, signer, changeSigner, editable}: {kind: Kind; parent: RecordData; signer: Scalar | undefined; changeSigner: (value: Scalar) => void; editable: boolean; can?: Can}) {
  const path = `${catalogs[kind].path}/${encodeURIComponent(parent.id)}/representatives`;
  const result = useLoad(signal => all<RecordData>(path, signal), [path]);
  const link = `/arm2/${kind.toLowerCase()}/${encodeURIComponent(parent.id)}/representatives/`;
  return <section><header className="arm2-actions"><h3>Представители</h3>{editable && <Link className="arm2-create" to={link + 'new'}>Добавить представителя</Link>}</header>
    <p className="arm2-note">Контакты представителей отделены от официальных контактов организации. Связь с учётной записью не предоставляет права доступа.</p>
    <ErrorMessage error={result.error} retry={result.reload}/>{result.loading && <p role="status">Загружаем представителей…</p>}
    {result.data && <><div className="arm2-table"><table><thead><tr><th>Фамилия И.О. / Имя</th><th>Должность</th><th>Телефон</th><th>Email</th></tr></thead><tbody>{result.data.map(row => <tr key={row.id}><td><Link title={row.name} to={link + encodeURIComponent(row.id)}>{shortRepresentativeName(row.name)}</Link>{row.state === 'Архивный' && <small className="arm2-badge">Архивный</small>}</td><td>{display(row.position)}</td><td>{display(row.phone)}</td><td>{display(row.email)}</td></tr>)}</tbody></table></div>{!result.data.length && <p>Представителей пока нет</p>}
      <Field field="primary_signer_id" value={signer} change={changeSigner} options={[{value: '', label: 'Не назначен'}, ...(signer && !result.data.some(row => row.id === signer) ? [{value: String(signer), label: 'Текущий подписант недоступен', disabled: true}] : []), ...result.data.map(row => ({value: row.id, label: row.name}))]}/></>}
  </section>;
}
