// Native UI smoke only: real auth/CSRF and DOM actions, no API injection or mocks.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const modulePath=path.join(here,'qa-browser/node_modules/playwright');
if(JSON.parse(fs.readFileSync(path.join(modulePath,'package.json'),'utf8')).version!=='1.65.1')throw new Error('Pinned Playwright required');
const {chromium,expect}=await import(pathToFileURL(path.join(modulePath,'test.mjs')).href);
const s=JSON.parse(process.env.ARM2_ISSUE72);
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(s.origin)||!['core','fresh'].includes(s.phase))throw new Error('Only local disposable smoke');
const browser=await chromium.launch({headless:true,...(process.env.ARM2_QA_BROWSER_EXECUTABLE?{executablePath:process.env.ARM2_QA_BROWSER_EXECUTABLE}:{})});
const context=await browser.newContext({viewport:{width:1440,height:1100},acceptDownloads:true});
const page=await context.newPage();page.setDefaultTimeout(20000);
// Keep observed response bodies across renderer navigation (Edge's inspector cache
// otherwise evicts an upload response while React opens its durable draft route).
const network=await context.newCDPSession(page);
await network.send('Network.enable',{maxTotalBufferSize:104857600,maxResourceBufferSize:33554432,enableDurableMessages:true});
const errors=[],observed=[];let draft;
const pathname=r=>new URL(r.url()).pathname;
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(pathname(r).startsWith('/api/arm2/'))observed.push({path:pathname(r),method:r.request().method(),status:r.status()});});
const region=name=>page.getByRole('region',{name,exact:true});
async function login(){
  await page.goto(s.origin+'/arm2/questionnaires');
  await page.getByLabel('Логин',{exact:true}).fill('integration-qa');
  await page.getByLabel('Пароль',{exact:true}).fill(s.password);
  const response=page.waitForResponse(r=>pathname(r)==='/api/auth/login'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Войти в систему',exact:true}).click();
  expect((await response).status()).toBe(200);
}
async function saveCP(method){
  const response=page.waitForResponse(r=>pathname(r).startsWith('/api/arm2/counterparties')&&r.request().method()===method);
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  const actual=await response;expect(actual.status()).toBe(method==='POST'?201:200);
  const value=await actual.json();
  if(method==='POST'){await expect(page).toHaveURL(new RegExp('/cp/'+value.id+'$'));await expect(page.getByLabel(/^Рабочее имя/)).toHaveValue(value.name);}
  else await expect(page.getByRole('status').filter({hasText:'Сохранено сервером'})).toBeVisible();
  return value;
}
async function upload(file){
  const response=page.waitForResponse(r=>pathname(r)==='/api/arm2/questionnaires'&&r.request().method()==='POST').then(async r=>({status:r.status(),value:await r.json()}));
  await page.getByLabel('Файл анкеты XLSX/ZIP',{exact:true}).setInputFiles(file);
  const actual=await response;expect(actual.status).toBe(201);draft=actual.value;
  await ready();
}
async function ready(){await expect(page.getByText('Выполняется операция…',{exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'Принять изменения',exact:true})).toBeEnabled();}
async function mutate(action){
  const response=page.waitForResponse(r=>pathname(r)==='/api/arm2/questionnaires/'+draft.id&&r.request().method()==='PUT');
  await action();const actual=await response;expect(actual.status()).toBe(200);draft=await actual.json();await ready();
}
async function select(locator,value){if(await locator.inputValue()!==value)await mutate(()=>locator.selectOption(value));}
async function tick(locator){
  // Controlled checkboxes reflect the durable PUT response, not an optimistic toggle.
  if(!await locator.isChecked())await mutate(()=>locator.click());
  await expect(locator).toBeChecked();
}
async function apply(){
  const response=page.waitForResponse(r=>pathname(r)==='/api/arm2/questionnaires/'+draft.id+'/apply'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Принять изменения',exact:true}).click();
  const actual=await response;expect(actual.status()).toBe(200);return actual.json();
}
async function core(){
  await login();
  await page.goto(s.origin+'/arm2/cp/new');
  await page.getByLabel('Тип контрагента',{exact:false}).selectOption('Физическое лицо');
  await page.getByRole('combobox',{name:/^Организационно-правовая форма/}).selectOption('ИП');
  await page.getByLabel(/^Рабочее имя/).fill('Smoke free CP name');
  await page.getByLabel('Фамилия',{exact:true}).fill('Тестов');await page.getByLabel('Имя',{exact:true}).fill('Иван');
  await page.getByLabel('ОГРН / ОГРНИП',{exact:true}).fill('000000000000001');
  let personal=await saveCP('POST');
  expect(personal.type).toBe('Физическое лицо');expect(personal.full_name).toBe('Индивидуальный предприниматель Тестов Иван');expect(personal).not.toHaveProperty('status');
  await page.getByRole('button',{name:'Банковские счета',exact:true}).click();
  await page.getByLabel(/^БИК/).fill('001234567');await page.getByLabel(/^Расчётный счёт/).fill('00000000000000000001');
  personal=await saveCP('PUT');
  await page.getByRole('button',{name:'Основное',exact:true}).click();
  const writes=observed.filter(r=>r.method==='PUT').length;
  await page.getByLabel(/^Тип контрагента/).selectOption('Юридическое лицо');
  await page.getByRole('button',{name:'Отменить изменения',exact:true}).click();
  await expect(page.getByLabel(/^Тип контрагента/)).toHaveValue('Физическое лицо');
  expect(observed.filter(r=>r.method==='PUT').length).toBe(writes);
  await page.getByLabel(/^Тип контрагента/).selectOption('Юридическое лицо');
  await expect(page.getByLabel('Полное наименование',{exact:true})).toHaveValue('');
  await page.getByRole('textbox',{name:/^Организационно-правовая форма/}).fill('АО');
  await page.getByLabel('Полное наименование',{exact:true}).fill('Synthetic independent legal full');
  await page.getByLabel('Краткое наименование',{exact:true}).fill('Synthetic legal short');
  for(const label of ['Фамилия','Имя','ОГРН / ОГРНИП'])await page.getByRole('combobox',{name:'Решение об очистке '+label,exact:true}).selectOption('clear');
  await page.getByRole('checkbox',{name:/Подтверждаю изменение типа/}).check();
  const changed=await saveCP('PUT');expect(changed.id).toBe(personal.id);expect(changed.name).toBe(personal.name);
  expect(changed.last_name).toBeNull();expect(changed.ogrn).toBeNull();expect(changed.bik).toBe('001234567');
  expect(changed.full_name).toBe('Synthetic independent legal full');
  expect(observed.some(r=>r.path.startsWith('/api/arm2/history/CP/'+changed.id))).toBe(false);
  await page.getByRole('button',{name:'История',exact:true}).click();
  await expect(region('История текущего объекта')).toBeVisible();
  await region('История текущего объекта').getByRole('button').first().click();await expect(region('Изменения события')).toContainText('Тестов');
  await page.screenshot({path:path.join(s.directory,'native-cp-transition.png'),fullPage:true});

  await page.goto(s.origin+'/arm2/veh');
  await expect(page.getByRole('combobox',{name:/^Учётное состояние/})).toHaveValue('Действующая');
  await expect(page.getByText('Без операционного владельца: 1',{exact:true})).toBeVisible();
  await page.getByRole('combobox',{name:/^Учётное состояние/}).selectOption('ALL');await page.reload();
  await expect(page.getByRole('combobox',{name:/^Учётное состояние/})).toHaveValue('ALL');
  await expect(page.getByText('Без операционного владельца: 2',{exact:true})).toBeVisible();
  await expect(page.getByText('Без Wialon ID: 2',{exact:true})).toBeVisible();
  await page.getByLabel('Поиск по рабочему имени / госномеру',{exact:true}).fill('ABSENT_SMOKE');
  await expect(page.getByText('Без Wialon ID: 0',{exact:true})).toBeVisible();
  await expect(page.getByText('Без операционного владельца: 0',{exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  const widths=await page.locator('.arm2-sections > *').evaluateAll(es=>es.map(e=>Math.round(e.getBoundingClientRect().width)));
  expect(Math.max(...widths)-Math.min(...widths)).toBeLessThanOrEqual(1);
  await page.screenshot({path:path.join(s.directory,'native-mobile-vehicles.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1100});
  await page.goto(s.origin+'/arm2/questionnaires');await upload(s.input_path);const first=draft.id;
  await upload(s.input_path);expect(draft.id).toBe(first);
  await select(page.getByRole('combobox',{name:'Решение о контрагенте',exact:true}),s.cp);
  for(const label of ['Официальный телефон','Расчётный счёт'])await select(page.getByRole('combobox',{name:'Решение '+label,exact:true}),'set');
  const rep=region('Представитель 0');await select(rep.getByLabel(/^Запись представителя/),s.rep);
  await tick(rep.getByRole('checkbox',{name:'Принять Должность',exact:true}));
  const veh=region('Строка техники 5');await tick(veh.getByRole('checkbox',{name:'Включить строку 5',exact:true}));
  await tick(veh.getByRole('checkbox',{name:'Принять Модель',exact:true}));
  const file=region('Файл company-replacement.pdf');await select(file.getByLabel(/^Вид документа/),s.kind);
  await select(file.getByLabel(/^Карточка документа/),s.document);
  await select(file.getByLabel(/^Заменить принятый файл/),s.replace_file);
  await file.getByRole('button',{name:'Открыть кандидат',exact:true}).click();
  const viewer=region('Просмотр вложения');await expect(viewer.locator('canvas')).toBeVisible();
  await expect(viewer.getByRole('button',{name:'Следующая страница',exact:true})).toBeEnabled();
  await viewer.getByRole('button',{name:'Следующая страница',exact:true}).click();await expect(viewer.getByRole('navigation')).toContainText('2 / 2');
  await viewer.getByRole('button',{name:'Закрыть просмотр вложения',exact:true}).click();
  await tick(file.getByRole('checkbox',{name:'Принять файл и выбранную привязку',exact:true}));
  const receipt=await apply();expect(receipt.no_op).toBe(false);
  expect(new Set(receipt.objects.map(o=>o.kind))).toEqual(new Set(['CP','CPREP','VEH','DOCUMENT']));
  const protocol=region('Протокол приёмки анкеты').first();await expect(protocol).toContainText('After questionnaire');
  const documentLinks=protocol.locator('a').filter({hasText:'DOCUMENT'});
  expect(await documentLinks.count()).toBeGreaterThan(0);
  for(const link of await documentLinks.all())await expect(link).toHaveAttribute('href',`/arm2/cp/${s.cp}/documents/${s.document}`);
  await page.screenshot({path:path.join(s.directory,'native-v4-receipt.png'),fullPage:true});
  await page.goto(s.origin+'/arm2/cp/'+s.cp);
  const downloading=page.waitForEvent('download');await page.getByRole('button',{name:'Выгрузить ZIP с текущими документами',exact:true}).click();
  const output=path.join(s.directory,'roundtrip-v4.zip');await(await downloading).saveAs(output);
  await page.goto(s.origin+'/arm2/questionnaires');await upload(output);
  await select(page.getByRole('combobox',{name:'Решение о контрагенте',exact:true}),s.cp);
  const noOp=await apply();expect(noOp.no_op).toBe(true);expect(noOp.event_ids).toEqual([]);
  return {result:'PASS',native_cp_create:true,manual_cancel_without_write:true,explicit_transition_clears:true,
    preserved_id_name_bank:true,lazy_history:true,active_default:true,all_after_reload:true,two_counts_and_empty:true,
    mobile_equal_tile_widths:widths,native_v4_upload_dedup:true,pdf_two_pages:true,receipt,roundtrip_noop:noOp};
}
async function fresh(){
  await login();await page.goto(s.origin+'/arm2/questionnaires');
  await expect(page.getByText('Серверные незавершённые разборы (0)',{exact:true})).toBeVisible();
  await upload(s.fresh_path);await select(page.getByRole('combobox',{name:'Решение о контрагенте',exact:true}),'new');
  for(const label of ['Тип контрагента','Рабочее имя','Организационно-правовая форма'])await select(page.getByRole('combobox',{name:'Решение '+label,exact:true}),'set');
  const receipt=await apply();expect(receipt.cp_id).toBeTruthy();
  await page.goto(s.origin+'/arm2/cp/'+receipt.cp_id);await expect(page.getByLabel(/^Рабочее имя/)).toHaveValue('After reset CP');
  await expect(page.getByLabel('Организационно-правовая форма',{exact:true})).toHaveValue('МУП');
  return {result:'PASS',bootstrap_login_preserved:true,empty_draft_list:true,fresh_v4_created:true,cp_id:receipt.cp_id};
}
try{
  const result=await(s.phase==='core'?core():fresh());expect(errors).toEqual([]);
  fs.writeFileSync(path.join(s.directory,s.phase+'.json'),JSON.stringify({...result,page_errors:errors,observed},null,2));
  console.log(s.phase+': native Issue72 UI PASS');
}catch(error){
  await page.screenshot({path:path.join(s.directory,s.phase+'-failure.png'),fullPage:true,mask:[page.locator('input[type=password]')]}).catch(()=>{});
  throw error;
}finally{await context.close();await browser.close();}
