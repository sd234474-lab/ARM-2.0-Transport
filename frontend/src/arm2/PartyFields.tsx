import type {RecordData, Scalar} from './api';
import {labels} from './model';
import {display, Field} from './shared';

export const person='Физическое лицо', legal='Юридическое лицо';
export const fio=['last_name','first_name','middle_name'];
export function rawParty(row:RecordData,field:string):Scalar|undefined {
  const retained=(row as unknown as {inapplicable_values?:Record<string,string>}).inapplicable_values;
  if(row.type===person&&['full_name','short_name'].includes(field))return retained?.[field]??null;
  return row[field];
}
export function applicable(row:RecordData,field:string) {
  if(fio.includes(field))return row.type===person;
  if(['full_name','short_name','kpp'].includes(field))return row.type===legal;
  if(field==='ogrn')return row.type===legal||row.legal_form==='ИП';
  return true;
}
export function PartyFields({kind,draft,original,change,confirmed,confirm,editable}:{kind:'CP'|'ORG';draft:RecordData;original:RecordData;change:(field:string,value:Scalar)=>void;confirmed:boolean;confirm:(value:boolean)=>void;editable:boolean}) {
  const cp=kind==='CP',changed=cp&&!!original.id&&(draft.type!==original.type||draft.legal_form!==original.legal_form);
  const candidates=cp?['legal_form',...fio,'full_name','short_name','kpp','ogrn'].filter(f=>rawParty(original,f)!=null&&(!applicable(draft,f)||(draft.type!==original.type&&['legal_form','ogrn'].includes(f)))):[];
  const fields=['name',...(cp?fio:[]),'full_name','short_name','inn','kpp','ogrn','legal_address','postal_address','phone','email'];
  const full=cp&&draft.type===person?[draft.last_name,draft.first_name,draft.middle_name].filter(Boolean).join(' '):null;
  return <section aria-label="Реквизиты контрагента"><h3>Наименования и реквизиты</h3><div className="arm2-grid">
    {cp&&<Field field="type" value={draft.type} required change={v=>change('type',v)} options={[{value:'',label:'Выберите тип'},...[person,legal].map(v=>({value:v,label:v}))]}/>}
    <Field field="legal_form" value={draft.legal_form} change={v=>change('legal_form',v)} {...(cp&&draft.type===person?{options:[{value:'',label:'ИП не указан'},{value:'ИП',label:'ИП'},...(draft.legal_form&&draft.legal_form!=='ИП'?[{value:String(draft.legal_form),label:String(draft.legal_form)+' — неприменимо, решите очистку',disabled:true}]:[])]}:{})}/>
    {fields.map(f=><div key={f}>{cp&&!applicable(draft,f)?<label className="arm2-field"><span>{labels[f]} — неприменимо / производное</span><input disabled value={String(rawParty(draft,f)??'')}/></label>:<Field field={f} value={draft[f]} required={f==='name'} change={v=>change(f,v)}/>}</div>)}
  </div>{full!==null&&<p aria-label="Официальное представление ФЛ">{draft.legal_form==='ИП'?'Индивидуальный предприниматель ':''}{full||'ФИО не заполнено'}. Представления рассчитываются из первичных ФИО.</p>}
  {!!candidates.length&&<section aria-label="Предложения очистки"><h4>Было → будет: неприменимые реквизиты и регистрация</h4>{candidates.map(f=><label key={f}>{labels[f]}: {display(rawParty(original,f))} → {display(draft[f])}<select disabled={!editable} aria-label={'Решение об очистке '+labels[f]} value={draft[f]==null?'clear':draft[f]===rawParty(original,f)?'keep':'set'} onChange={e=>{if(e.target.value!=='set')change(f,e.target.value==='clear'?null:rawParty(original,f)??null);}}><option value="keep">Оставить прежнее</option><option value="clear">Явно очистить</option>{draft[f]!=null&&draft[f]!==rawParty(original,f)&&<option value="set">Установить введённое значение</option>}</select></label>)}</section>}
  {changed&&<label><input type="checkbox" disabled={!editable} checked={confirmed} onChange={e=>confirm(e.target.checked)}/>Подтверждаю изменение типа / ОПФ и решения об очистке: {display(original.type)} / {display(original.legal_form)} → {display(draft.type)} / {display(draft.legal_form)}</label>}
  </section>;
}
