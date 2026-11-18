// AUTHOR-RUN only. All operator actions, including login and competing B2, use the real UI.
// HTTP responses are observed for assertions; no request client, API injection or mocks.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';

const here=path.dirname(fileURLToPath(import.meta.url));
const modulePath=process.env.ARM2_QA_PLAYWRIGHT||path.join(here,'qa-browser/node_modules/playwright');
if(JSON.parse(fs.readFileSync(path.join(modulePath,'package.json'),'utf8')).version!=='1.65.1')throw new Error('QA requires pinned Playwright 1.65.1');
const {chromium,expect}=await import(pathToFileURL(path.join(modulePath,'test.mjs')).href);
const state=JSON.parse(process.env.ARM2_B5_QA);
if(!['review','accept','reopen'].includes(state.phase)||!/^http:\/\/127\.0\.0\.1:\d+$/.test(state.origin))throw new Error('Only local disposable B5 QA');
const browser=await chromium.launch({headless:true,...(process.env.ARM2_QA_BROWSER_EXECUTABLE?{executablePath:process.env.ARM2_QA_BROWSER_EXECUTABLE}:{})});
const context=await browser.newContext({viewport:{width:1440,height:1100},acceptDownloads:true});
const page=await context.newPage();
page.setDefaultTimeout(30000);
const errors=[],observations=[];
let draft;
const pathname=response=>new URL(response.url()).pathname;
function observe(p){
  p.on('pageerror',error=>errors.push(error.message));
  p.on('response',response=>{if(pathname(response).startsWith('/api/arm2/'))observations.push({method:response.request().method(),path:pathname(response),status:response.status()});});
}
observe(page);
const endpoint=()=>'/api/arm2/questionnaires/'+draft.id;
const region=(name,p=page)=>p.getByRole('region',{name,exact:true});
const cpGroup=()=>page.getByRole('group',{name:'Контрагент',exact:true});
const repGroup=()=>region('Представитель 0');
const rowGroup=row=>region('Строка техники '+row);
const fileGroup=name=>region('Файл '+name);
const labels={phone:'Официальный телефон',last_name:'Фамилия',first_name:'Имя',middle_name:'Отчество',bik:'БИК',settlement_account:'Расчётный счёт',name:'Рабочее имя',position:'Должность',authority_basis:'Основание полномочий',model:'Модель',type_id:'Тип техники',owner:'Операционный владелец',accounting_state:'Состояние учёта'};

async function login(p,loginName='integration-qa'){
  await p.goto(state.origin+'/arm2/questionnaires');
  await p.getByLabel('Логин',{exact:true}).fill(loginName);
  await p.getByLabel('Пароль',{exact:true}).fill(state.password);
  const response=p.waitForResponse(r=>pathname(r)==='/api/auth/login'&&r.request().method()==='POST');
  await p.getByRole('button',{name:'Войти в систему',exact:true}).click();
  expect((await response).status()).toBe(200);
  await p.goto(state.origin+'/arm2/questionnaires');
  await expect(p.getByRole('heading',{name:'Загрузка и сверка анкеты',exact:true})).toBeVisible();
}
async function ready(){
  await expect(region('Сверка анкеты').getByRole('status')).toContainText(`Редакция ${draft.revision}`);
  await expect(page.getByText('Выполняется операция…',{exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Принять изменения',exact:true})).toBeEnabled();
}
async function mutate(action){
  const response=page.waitForResponse(r=>pathname(r)===endpoint()&&r.request().method()==='PUT');
  await action();
  const actual=await response;
  expect(actual.status()).toBe(200);
  draft=await actual.json();
  await ready();
}
async function select(locator,value){if(await locator.inputValue()!==value)await mutate(()=>locator.selectOption(value));}
async function check(locator,on=true){
  if(await locator.isChecked()!==on)await mutate(()=>locator.click());
  // Controlled checkboxes reflect the persisted draft response, not an optimistic toggle.
  await expect(locator).toBeChecked({checked:on});
}
async function fields(scope,names){for(const field of names)await check(scope.getByRole('checkbox',{name:'Принять '+labels[field],exact:true}));}
async function refresh(){
  const response=page.waitForResponse(r=>pathname(r)===endpoint()&&r.request().method()==='GET');
  await page.getByRole('button',{name:'Повторно открыть сверку',exact:true}).click();
  expect((await response).status()).toBe(200);
  draft=await(await response).json();await ready();
}
async function upload(file){
  const posted=page.waitForResponse(r=>pathname(r)==='/api/arm2/questionnaires'&&r.request().method()==='POST');
  const opened=page.waitForResponse(r=>/^\/api\/arm2\/questionnaires\/[\da-f-]{36}$/.test(pathname(r))&&r.request().method()==='GET');
  await page.getByLabel('Файл анкеты XLSX/ZIP',{exact:true}).setInputFiles(file);
  const response=await posted;expect(response.status()).toBe(201);
  const restored=await opened;expect(restored.status()).toBe(200);draft=await restored.json();
  await ready();
}
async function configure(confirmOwner=true){
  await select(cpGroup().getByRole('combobox',{name:'Решение о контрагенте',exact:true}),state.cp);
  await fields(cpGroup(),['phone','last_name','first_name','middle_name','bik','settlement_account']);
  await select(repGroup().getByLabel(/^Запись представителя/),'new');
  await fields(repGroup(),['name','position','authority_basis']);
  await check(repGroup().getByRole('checkbox',{name:'Назначить выбранную запись подписантом CP',exact:true}));
  for(const [row,selected] of [[5,['model','owner']],[6,['name','model','type_id','owner']],[7,['accounting_state','owner']]]){
    const group=rowGroup(row);
    await check(group.getByRole('checkbox',{name:'Включить строку '+row,exact:true}));
    await fields(group,selected);
  }
  await check(rowGroup(5).getByRole('checkbox',{name:'Отдельно подтверждаю смену прежнего владельца → нового',exact:true}),confirmOwner);
  await expect(rowGroup(8).getByRole('checkbox',{name:'Включить строку 8',exact:true})).toBeDisabled();
  await expect(rowGroup(5)).toContainText('37.5 — review only');
  for(const [name,kind] of [['company.pdf','Карточка предприятия'],['updated-sts.pdf','СТС'],['new-sts.jpg','СТС'],['calibration.png','Тарировка']]){
    const group=fileGroup(name);
    await select(group.getByLabel(/^Вид документа/),state.kinds[kind]);
    if(name==='updated-sts.pdf'){
      await select(group.getByLabel(/^Карточка документа/),state.document);
      await select(group.getByLabel(/^Заменить принятый файл/),state.replace_file);
    }
    await check(group.getByRole('checkbox',{name:'Принять файл и выбранную привязку',exact:true}));
  }
  await expect(fileGroup('unsupported.zip')).toContainText('Неподдерживаемый формат');
  await expect(fileGroup('unsupported.zip').getByRole('checkbox',{name:'Принять файл и выбранную привязку',exact:true})).toBeDisabled();
}
async function preview(name,type,button='Открыть кандидат'){
  await fileGroup(name).getByRole('button',{name:button,exact:true}).first().click();
  const view=region('Просмотр вложения');await expect(view).toBeVisible();
  if(type==='pdf'){
    const canvas=view.locator('canvas');
    await expect.poll(()=>canvas.evaluate(c=>{if(!c.width||!c.height)return false;const p=c.getContext('2d').getImageData(Math.floor(c.width/2),Math.floor(c.height/2),1,1).data;return p[3]>0&&[...p].slice(0,3).some(v=>v<245);})).toBe(true);
    if(name==='updated-sts.pdf'&&button==='Открыть кандидат'){
      await view.getByRole('button',{name:'Следующая страница',exact:true}).click();
      await expect(view.getByRole('navigation',{name:'Страницы вложения',exact:true})).toContainText('2 / 2');
    }
  }else await expect.poll(()=>view.locator('img').evaluate(img=>img.complete&&img.naturalWidth>0)).toBe(true);
  await view.screenshot({path:path.join(state.directory,`${state.phase}-preview-${name.replace(/[^a-zA-Z0-9.]/g,'_')}-${button==='Открыть кандидат'?'candidate':'current'}.png`)});
  await view.getByRole('button',{name:'Закрыть просмотр вложения',exact:true}).click();
  await expect(view).toHaveCount(0);
}
async function save(expected){
  const response=page.waitForResponse(r=>pathname(r)===endpoint()+'/apply'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Принять изменения',exact:true}).click();
  const actual=await response;expect(actual.status()).toBe(expected);
  return {value:await actual.json(),command:actual.request().postDataJSON()};
}
async function closedReady(){
  await expect(page.getByText('Выполняется операция…',{exact:true})).toHaveCount(0);
  await expect(page.getByText('Серверные незавершённые разборы (0)',{exact:true})).toBeVisible();
}
async function cleanClosed(){
  const response=page.waitForResponse(r=>pathname(r)===endpoint()+'/cleanup'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Очистить временные файлы завершённого пакета',exact:true}).click();
  const actual=await response;expect(actual.status()).toBe(200);expect((await actual.json()).pending).toBe(0);
  await closedReady();
}
async function competingB2(){
  const rival=await browser.newContext({viewport:{width:1440,height:1100}});
  const other=await rival.newPage();observe(other);
  try{
    await login(other,'integration-rival');
    const forbidden=other.waitForResponse(r=>pathname(r)===endpoint()&&r.request().method()==='GET');
    await other.goto(state.origin+'/arm2/questionnaires?draft='+draft.id);
    expect((await forbidden).status()).toBe(403);
    await expect(other.getByText('Черновик доступен только его автору',{exact:true})).toBeVisible();
    await other.goto(state.origin+'/arm2/org/'+state.org+'?tab=vehicles');
    await other.getByRole('button',{name:'Добавить технику',exact:true}).click();
    const search=region('Поиск существующей техники',other);
    await search.getByLabel('Поиск существующей техники по имени / госномеру',{exact:true}).fill('Existing VEH');
    const found=search.locator('tr').filter({hasText:'Existing VEH'});
    await expect(found).toHaveCount(1);
    await expect(found.getByRole('link',{name:'Existing VEH',exact:true})).toHaveAttribute('href','/arm2/veh/'+state.vehicle);
    await found.getByRole('button',{name:'Выбрать технику',exact:true}).click();
    const confirmation=region('Подтверждение операционного владельца',other);
    await expect(confirmation).toContainText('Synthetic CP A');
    await expect(confirmation).toContainText('Synthetic intervening ORG');
    await confirmation.getByRole('checkbox',{name:'Подтверждаю переназначение от прежнего владельца к указанному получателю',exact:true}).check();
    const response=other.waitForResponse(r=>pathname(r)===`/api/arm2/vehicles/${state.vehicle}/operational-owner`&&r.request().method()==='POST');
    await confirmation.getByRole('button',{name:'Подтвердить назначение',exact:true}).click();
    expect((await response).status()).toBe(200);
    await expect(other.getByRole('status').filter({hasText:'Связь сохранена сервером'})).toBeVisible();
    await other.screenshot({path:path.join(state.directory,'review-competing-b2.png'),fullPage:true});
  }finally{await rival.close();}
}
async function review(){
  await login(page);
  await upload(state.input_path);
  await configure(false);
  await preview('updated-sts.pdf','pdf');
  await preview('updated-sts.pdf','pdf','Открыть действующий');
  const missing=await save(409);
  expect(missing.value.detail).toContain('Отдельно подтвердите');
  await refresh();await configure(true);
  await competingB2();
  await save(409);
  await refresh();
  expect(draft.approval_reset).toBe(true);
  expect(draft.choices.cp_fields).toEqual([]);
  expect(Object.values(draft.choices.vehicles).every(v=>!v.include&&!v.confirm_owner&&!v.fields.length)).toBe(true);
  expect(Object.values(draft.choices.documents).every(v=>!v.accept)).toBe(true);
  await configure(true);
  expect(draft.comparison.vehicles[0].fields.owner.current).toEqual(['ORG',state.org]);
  await page.reload();
  const reopened=page.waitForResponse(r=>pathname(r)===endpoint()&&r.request().method()==='GET');
  // A further UI reopen proves choices are server-side, not merely component state.
  await page.getByRole('button',{name:'Повторно открыть сверку',exact:true}).click();
  const actual=await(await reopened).json();expect(actual.choices).toEqual(draft.choices);draft=actual;await ready();
  await page.screenshot({path:path.join(state.directory,'review-durable-choices.png'),fullPage:true});
  return {draft:draft.id,revision:draft.revision,preview_id:draft.preview_id,choices:draft.choices,
    native_login:true,upload_opens_comparison:true,pdf_candidate_and_current_preview:true,
    missing_owner_confirmation_409:true,foreign_draft_403:true,competing_b2_via_ui:true,
    stale_save_409:true,approval_reset_verified:true,server_choices_after_reload:true,
    successful_final_b5_save_performed:false,rejected_save_attempts:2};
}
async function accept(){
  await login(page);
  const opened=page.waitForResponse(r=>pathname(r)===`/api/arm2/questionnaires/${state.draft}`&&r.request().method()==='GET');
  await page.goto(state.origin+'/arm2/questionnaires?draft='+state.draft);
  draft=await(await opened).json();await ready();
  expect(draft.choices).toEqual(state.review.choices);
  expect(draft.preview_id).toBe(state.review.preview_id);
  expect(draft.comparison.vehicles[0].fields.owner.current).toEqual(['ORG',state.org]);
  await preview('updated-sts.pdf','pdf');await preview('calibration.png','png');await preview('new-sts.jpg','jpeg');
  const result=await save(200),receipt=result.value;
  expect(receipt.no_op).toBe(false);expect(receipt.event_ids.length).toBeGreaterThan(0);
  const protocol=region('Протокол приёмки анкеты').first();
  await expect(protocol.getByRole('heading',{name:'Изменения приняты',exact:true})).toBeVisible();
  await expect(protocol).toContainText(receipt.command_id);await expect(protocol).toContainText('2026-10-21');
  await closedReady();
  await page.screenshot({path:path.join(state.directory,'accept-receipt.png'),fullPage:true});
  await cleanClosed();
  await page.goto(state.origin+'/arm2/cp/'+state.cp);
  const downloading=page.waitForEvent('download');
  await page.getByRole('button',{name:'Выгрузить ZIP с текущими документами',exact:true}).click();
  const zip=path.join(state.directory,'roundtrip-v3.zip');await(await downloading).saveAs(zip);
  await page.goto(state.origin+'/arm2/questionnaires');await upload(zip);
  await select(cpGroup().getByRole('combobox',{name:'Решение о контрагенте',exact:true}),state.cp);
  expect(draft.comparison.representatives).toHaveLength(2);
  expect(draft.comparison.representatives.every(r=>Object.values(r.fields).every(f=>f.equal))).toBe(true);
  expect(draft.comparison.documents.every(d=>d.equal)).toBe(true);
  const noOp=await save(200);expect(noOp.value.no_op).toBe(true);expect(noOp.value.event_ids).toEqual([]);
  await expect(region('Протокол приёмки анкеты').first()).toContainText('Соответствует — предметных изменений нет');
  await closedReady();await cleanClosed();
  await page.screenshot({path:path.join(state.directory,'accept-v3-roundtrip-noop.png'),fullPage:true});
  return {receipt,apply_command:result.command,restored_review_choices_equal:true,restored_staged_pdf_png_jpeg_preview:true,
    one_final_domain_command:true,exported_v3_zip:true,v3_reimport_no_duplicates:true,closed_packets_cleanup_pending:0,
    no_op_receipt:noOp.value};
}
async function reopen(){
  await login(page);
  await expect(page.getByText('Серверные незавершённые разборы (0)',{exact:true})).toBeVisible();
  await page.goto(state.origin+'/arm2/questionnaires?draft='+state.draft);
  const protocol=region('Протокол приёмки анкеты').first();
  await expect(protocol).toContainText(state.accepted.receipt.command_id);
  await expect(region('Сверка анкеты')).toHaveCount(0);
  await page.goto(state.origin+'/arm2/cp/'+state.cp);
  await expect(page.getByLabel('Полное наименование',{exact:true})).toHaveValue('Synthetic sole trader');
  await expect(page.getByLabel('Официальный телефон',{exact:true})).toHaveValue('Synthetic B5 contact');
  await page.getByRole('button',{name:'Банковские счета',exact:true}).click();
  await expect(page.getByLabel(/^БИК/)).toHaveValue('001234567');
  await expect(page.getByLabel(/^Расчётный счёт/)).toHaveValue('00000000000000000001');
  await page.getByRole('button',{name:'Контакты',exact:true}).click();
  const signer=state.accepted.receipt.objects.find(o=>o.kind==='CPREP').id;
  await expect(page.getByLabel(/^Основной подписант/)).toHaveValue(signer);
  await expect(page.getByRole('link',{name:'Synthetic B5 signer',exact:true})).toHaveCount(2);
  await page.goto(state.origin+`/arm2/cp/${state.cp}/representatives/${signer}`);
  await expect(page.getByLabel('Должность',{exact:true})).toHaveValue('Operator-selected signer');
  await expect(page.getByText('Связанная учётная запись: Не заполнено. Информационная связь не предоставляет права доступа.',{exact:true})).toBeVisible();
  await page.screenshot({path:path.join(state.directory,'reopen-representative.png'),fullPage:true});
  for(const id of [state.vehicle,state.archived]){
    await page.goto(state.origin+'/arm2/veh/'+id);
    await expect(page.getByRole('combobox',{name:'Операционный владелец',exact:true})).toHaveValue(/B5 Target CP/);
    if(id===state.vehicle)await expect(page.getByLabel('Модель',{exact:true})).toHaveValue('B5 model');
    else await expect(page.getByLabel(/^Состояние учёта/)).toHaveValue('Действующая');
  }
  const newVehicle=state.accepted.receipt.objects.find(o=>o.kind==='VEH'&&![state.vehicle,state.archived].includes(o.id)).id;
  await page.goto(state.origin+'/arm2/veh/'+newVehicle);
  await expect(page.getByLabel('Модель',{exact:true})).toHaveValue('New B5 model');
  await page.goto(state.origin+'/arm2/veh/'+state.vehicle);
  await page.getByRole('button',{name:'История',exact:true}).click();
  const events=region('История текущего объекта');
  const eventRow=events.locator('tr').filter({hasText:'arm2-questionnaire'}).first();
  await expect(eventRow).toBeVisible();
  const detail=page.waitForResponse(r=>/^\/api\/arm2\/history\/events\/\d+$/.test(pathname(r)));
  await eventRow.getByRole('button').click();
  expect((await(await detail).json()).correlation_id).toBe(state.accepted.receipt.command_id);
  await expect(region('Изменения события')).toBeVisible();
  await page.goto(state.origin+`/arm2/veh/${state.vehicle}/documents/${state.document}`);
  await expect(page.getByRole('heading',{name:'Текущая редакция 2',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Предыдущие редакции',exact:true}).click();
  await page.locator('summary').filter({hasText:'Предыдущая редакция 1'}).click();
  const download=page.waitForEvent('download');
  await page.locator('li').filter({hasText:state.replace_file}).getByRole('link',{name:'Скачать файл',exact:true}).click();
  const historical=path.join(state.directory,'restored-historical-original.pdf');await(await download).saveAs(historical);
  const digest=createHash('sha256').update(fs.readFileSync(historical)).digest('hex');expect(digest).toBe(state.original_sha256);
  await page.screenshot({path:path.join(state.directory,'reopen-historical-original.png'),fullPage:true});
  return {native_login:true,closed_draft_receipt_reopened:true,no_active_completed_task:true,cp_reopened:true,
    representative_reopened:true,representative_user_link_unchanged:true,one_primary_signer_two_distinct_contacts:true,
    exact_bank_strings_visible:true,existing_new_archived_veh_reopened:true,
    history_event_correlation_verified:true,current_revision_visible:true,historical_revision_visible:true,
    historical_original_download_sha256:digest};
}
try{
  const result=await({review,accept,reopen}[state.phase])();expect(errors).toEqual([]);
  fs.writeFileSync(path.join(state.directory,state.phase+'.json'),JSON.stringify({classification:'AUTHOR-RUN synthetic native UI; not UAT',result:'PASS',...result,page_errors:errors,observed_http:observations},null,2));
  console.log(`${state.phase}: native B5 UI PASS`);
}catch(error){
  await page.screenshot({path:path.join(state.directory,state.phase+'-failure.png'),fullPage:true,mask:[page.locator('input[type=password]')]}).catch(()=>{});
  throw error;
}finally{await context.close();await browser.close();}
