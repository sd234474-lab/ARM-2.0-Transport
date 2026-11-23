import {useState} from 'react';
import type {QuestionnaireField} from './api';
import {labels} from './model';
import {legal, person} from './PartyFields';
import {display} from './shared';

export type FieldAction = 'keep' | 'set' | 'clear' | 'edit';
export type FieldEditor = {scope: string; field: string};

const choices: Record<string, string[]> = {
  type: [person, legal], state: ['Действующий', 'Архивный'], accounting_state: ['Действующая', 'Архивная'],
};

export function fieldDecision(fields: string[] = [], edits: Record<string,string> = {}, field: string, action: FieldAction, value?: string) {
  const next = {...edits};
  delete next[field];
  if (action === 'edit' && value !== undefined) next[field] = value;
  return {fields: [...fields.filter(key => key !== field), ...(action === 'set' || action === 'edit' ? [field] : [])], edits: next};
}

function ValueEditor({field, label, initial, save, cancel, busy}: {
  field: string; label: string; initial: string; save: (value: string) => void; cancel: () => void; busy: boolean;
}) {
  const [value,setValue] = useState(initial);
  return <div className="arm2-questionnaire-edit">
    {choices[field] ? <select autoFocus aria-label={`Исправить ${label}`} value={value} onChange={event => setValue(event.target.value)}>
      <option value="">Выберите значение</option>{choices[field].map(option => <option key={option}>{option}</option>)}
    </select> : <input autoFocus aria-label={`Исправить ${label}`} value={value} maxLength={field === 'calibration_m3' ? 40 : 10000}
      onChange={event => setValue(event.target.value)} onKeyDown={event => {
        if (event.key === 'Enter' && value.trim() && !busy) {event.preventDefault();save(value.trim());}
        if (event.key === 'Escape') cancel();
      }}/>}
    <button type="button" disabled={busy || !value.trim()} onClick={() => save(value.trim())}>Сохранить правку</button>
    <button type="button" disabled={busy} onClick={cancel}>Отмена</button>
  </div>;
}

export function QuestionnaireFields({fields, scope, edits, editor, setEditor, decide, busy, canEdit = () => true}: {
  fields: Record<string,QuestionnaireField>; scope: string; edits?: Record<string,string>; editor?: FieldEditor;
  setEditor: (editor: FieldEditor | undefined) => void; busy: boolean;
  decide: (field: string, action: FieldAction, value?: string) => void; canEdit?: (field: string) => boolean;
}) {
  const entries = Object.entries(fields).filter(([,f]) => !f.equal || f.clear_candidate);
  if (!entries.length) return null;
  return <div className="arm2-table arm2-questionnaire-fields"><table>
    <thead><tr><th>Реквизит</th><th>В карточке</th><th>В анкете / правка</th><th>Действие</th></tr></thead>
    <tbody>{entries.map(([field,f]) => {
      const label = field === 'owner' ? 'Операционный владелец' : field === 'calibration_m3' ? 'Объём по тарировке, м³' : labels[field] || field;
      const editing = editor?.scope === scope && editor.field === field;
      const manual = edits?.[field];
      const action = editing || (f.selected && manual !== undefined) ? 'edit' : f.clear_selected ? 'clear' : f.selected ? 'set' : 'keep';
      return <tr key={field}><th scope="row">{label}</th><td>{f.current_display ?? display(f.current)}</td><td>
        {editing ? <ValueEditor field={field} label={label} initial={manual ?? String(f.proposed ?? '')}
          busy={busy} save={value => decide(field,'edit',value)} cancel={() => setEditor(undefined)}/> : <>
          {f.proposed_display ?? display(f.proposed)}
          {manual !== undefined && <button type="button" className="arm2-edit-again" disabled={busy || !!editor}
            onClick={() => setEditor({scope,field})}>Исправить</button>}
        </>}
        {f.error && <small role="alert">{f.error}</small>}
      </td><td><select aria-label={`Решение ${label}`} value={action} disabled={busy || !!editor} onChange={event => {
        const next = event.target.value as FieldAction;
        if (next === 'edit') setEditor({scope,field});
        else decide(field,next);
      }}>
        <option value="keep">Оставить действующее</option><option value="set" disabled={!!f.error}>Принять из анкеты</option>
        {canEdit(field) && <option value="edit">Редактировать</option>}
        {f.clear_candidate && <option value="clear">Явно очистить</option>}
      </select></td></tr>;
    })}</tbody>
  </table></div>;
}
