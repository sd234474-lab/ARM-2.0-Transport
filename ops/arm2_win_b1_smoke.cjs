/* Windows Edge smoke against the existing isolated UAT. No original rows edited. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const {chromium} = require(process.env.ARM2_PLAYWRIGHT);
const installed = 'C:/ARM2-UAT/ARM2-WIN-RUN-001';
const secret = JSON.parse(fs.readFileSync(installed+'/private/secrets.json','utf8'));
const identity = JSON.parse(fs.readFileSync(installed+'/private/instance.json','utf8'));
const base = 'http://127.0.0.1:18082';
const prefix = 'B1-UAT-50';
const local = path.join(root,'.local');
const evidence = path.join(root,'docs/evidence/arm2-win-b1-uat');
fs.mkdirSync(evidence,{recursive:true});
(async()=>{
 const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
   headless:true,args:['--disable-background-networking','--disable-sync','--no-first-run','--disable-component-update']});
 try {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
  const page = await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const health = await (await context.request.get(base+'/api/health')).json();
  assert.equal(health.source_sha,'aaba19e5c363a8fee22eb6c49060d3452117bcd9');
  assert.equal(health.instance_id,identity.instance_id);assert.equal(health.migration,'0061');
  assert.equal(health.data_directory.replaceAll('\\','/'),installed+'/db-data');
  for(const resource of ['types','vehicles','counterparties','organizations']) {
   assert.equal((await context.request.get(base+'/api/arm2/'+resource+'/search')).status(),401);
   for(const method of ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS'])
    assert.equal((await context.request.fetch(base+'/api/arm2/'+resource+'/search',{method})).status(),404);
  }
  for(const resource of ['/api/max/webhook','/api/arm2/documents','/api/arm2/vehicles/VEH-000001/documents','/api/ml/train'])
   assert.equal((await context.request.post(base+resource,{data:{}})).status(),404);
  await page.goto(base+'/arm2');
  await page.getByLabel('Логин',{exact:true}).fill(secret.admin_login);
  await page.getByLabel('Пароль',{exact:true}).fill(secret.admin_password);
  await page.getByRole('button',{name:'Войти в систему'}).click();await page.waitForURL('**/arm2/veh');
  const csrf=decodeURIComponent((await context.cookies()).find(c=>c.name==='csrf').value);
  const headers={Origin:base,'X-CSRF-Token':csrf};
  async function read(url) {const r=await context.request.get(base+'/api'+url);assert.equal(r.status(),200,url);return r.json();}
  async function write(method,url,data,status=method==='POST'?201:200) {
   const r=await context.request.fetch(base+'/api'+url,{method,headers,data});
   assert.equal(r.status(),status,method+' '+url+' '+(r.status()===status?'':await r.text()));return r.json();
  }
  async function update(url,changes) {const {id,version,status,parent_id,...body}=await read(url);return write('PUT',url,{...body,...changes,expected_version:version});}
  async function capture(name) {await page.waitForLoadState('networkidle');await page.screenshot({path:path.join(evidence,name),fullPage:false});}
  if(process.argv.includes('--reopen')) {
   const saved=JSON.parse(fs.readFileSync(path.join(local,'windows-smoke.json'),'utf8'));
   assert.equal((await read('/arm2/vehicles/'+saved.vehicle)).model,'B1 после конфликта');
   await page.goto(base+'/arm2/veh/'+saved.vehicle);
   assert.equal(await page.getByLabel('Рабочее имя',{exact:false}).inputValue(),prefix+' UI машина');
   await page.getByRole('button',{name:'История',exact:true}).click();
   await page.getByRole('button',{name:/Открыть событие/}).first().click();
   await page.getByRole('region',{name:'Изменения события'}).waitFor();
   await capture('06-restart-history.png');
   for(const item of saved.representatives) {
    const rep=await read(item.url);
    assert.deepEqual([rep.last_name,rep.first_name,rep.middle_name,rep.user_id],item.legacy);
   }
   fs.writeFileSync(path.join(local,'windows-reopen.json'),JSON.stringify({result:'RESTART_REOPEN_PASS',health,vehicle:saved.vehicle},null,2));
   console.log('RESTART_REOPEN_PASS');return;
  }
  assert.equal((await context.request.post(base+'/api/arm2/vehicles',{data:{name:'must not save'}})).status(),403);
  assert.equal((await read('/arm2/vehicles/search?q='+prefix)).matched,0,'Do not reseed existing smoke records');
  const type=await write('POST','/arm2/types',{name:prefix+' Самосвал',requires_sts:true,requires_calibration:false,requires_wialon:false});
  const cp=await write('POST','/arm2/counterparties',{name:prefix+' Контрагент',full_name:prefix+' Синтетический контрагент',legal_form:'ООО'});
  const org=await write('POST','/arm2/organizations',{name:prefix+' Организация',full_name:prefix+' Синтетическая организация',legal_form:'ООО'});
  const me=await read('/auth/me');
  const reps=[];
  for(const [kind,parent] of [['counterparties',cp],['organizations',org]]) {
   const rep=await write('POST',`/arm2/${kind}/${parent.id}/representatives`,{
    name:'Иванов Иван Иванович',position:'Синтетический представитель',authority_basis:'Тестовое основание',
    phone:'000-00-00',email:'uat@example.invalid',last_name:'СохранённаяФамилия',first_name:'СохранённоеИмя',
    middle_name:'СохранённоеОтчество',user_id:me.id,state:'Действующий'});
   reps.push({url:`/arm2/${kind}/${parent.id}/representatives/${rep.id}`,legacy:[rep.last_name,rep.first_name,rep.middle_name,rep.user_id],id:rep.id});
   await update(`/arm2/${kind}/${parent.id}`,{primary_signer_id:rep.id});
  }
  // Distinct synthetic plates; real API/SAVE generates ID/version/history for each row.
  const vehicles=[];
  for(let i=1;i<=105;i++) {
   const plate='У'+String(i).padStart(3,'0')+'ТУ50';
   assert.equal((await read('/arm2/vehicles/search?q='+encodeURIComponent(plate))).matched,0,'Synthetic plate already exists');
   vehicles.push(await write('POST','/arm2/vehicles',{name:prefix+' Машина '+String(i).padStart(3,'0'),plate_number:plate,
    type_id:type.id,model:'Тестовая модель',owner_cp_id:i%2?cp.id:null,owner_org_id:i%2?null:org.id}));
  }
  const search='/arm2/vehicles/search?q='+prefix;
  const first=await read(search);const beyond=await read(search+'&offset=100');
  assert.equal(first.matched,105);assert.equal(first.items.length,100);assert.equal(beyond.items.length,5);
  for(const [key,value] of [['type_id',type.id],['owner_kind','CP'],['owner_kind','ORG']])
   assert.ok((await read(search+'&'+key+'='+value)).matched>0);
  for(const [kind,id] of [['CP',cp.id],['ORG',org.id]]) {
   const result=await read(search+'&owner_kind='+kind+'&owner_id='+id);
   assert.ok(result.items.every(v=>v[kind==='CP'?'owner_cp_id':'owner_org_id']===id));
  }
  assert.equal((await read('/arm2/vehicles/search?q='+encodeURIComponent('у 105 ту 50'))).matched,1);
  for(const resource of ['types','counterparties','organizations']) assert.equal((await read('/arm2/'+resource+'/search?q='+prefix)).matched,1);
  await page.getByLabel('Поиск по рабочему имени / госномеру',{exact:true}).fill(prefix);
  await page.getByRole('link',{name:prefix+' Машина 001',exact:true}).waitFor();await capture('01-search.png');
  await page.goto(base+'/arm2/veh/new');await page.getByLabel('Рабочее имя',{exact:false}).fill(prefix+' UI машина');
  await page.getByLabel('Государственный номер',{exact:false}).fill('У999ТУ50');
  await page.getByRole('combobox',{name:'Тип техники',exact:true}).fill(prefix);
  await page.getByRole('option',{name:type.name,exact:true}).click();
  await page.getByRole('combobox',{name:'Операционный владелец',exact:true}).fill(org.name);
  await page.getByRole('option',{name:'Организация · '+org.name,exact:true}).click();
  const creating=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/api/arm2/vehicles'));
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();const createdResponse=await creating;
  assert.equal(createdResponse.status(),201);const vehicle=await createdResponse.json();await page.waitForURL('**/arm2/veh/'+vehicle.id);
  await page.goto(base+'/arm2/veh/'+vehicle.id);await page.getByLabel('Модель',{exact:true}).waitFor();
  await update('/arm2/vehicles/'+vehicle.id,{model:'Серверное изменение'});
  await page.getByLabel('Модель',{exact:true}).fill('Локальный черновик');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByRole('button',{name:'Сравнить с сервером',exact:true}).click();await capture('02-conflict.png');
  page.once('dialog',d=>d.dismiss());await page.getByRole('button',{name:'Перезагрузить карточку',exact:true}).click();
  assert.equal(await page.getByLabel('Модель',{exact:true}).inputValue(),'Локальный черновик');
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Перезагрузить карточку',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('label')].find(l=>l.textContent==='Модель')?.querySelector('input')?.value==='Серверное изменение');
  await page.getByLabel('Модель',{exact:true}).fill('B1 после конфликта');
  const saving=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().endsWith('/api/arm2/vehicles/'+vehicle.id));
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal((await saving).status(),200);
  await page.goto(base+'/arm2/veh/'+vehicle.id);assert.equal((await read('/arm2/vehicles/'+vehicle.id)).model,'B1 после конфликта');
  await page.getByRole('button',{name:'История',exact:true}).click();await page.getByRole('button',{name:/Открыть событие/}).first().click();
  await page.getByRole('region',{name:'Изменения события'}).waitFor();await capture('03-history.png');
  for(let i=0;i<reps.length;i++) {
   const item=reps[i];const parent=i?org:cp;const kind=i?'org':'cp';
   await page.goto(base+`/arm2/${kind}/${parent.id}/representatives/${item.id}`);
   await page.getByLabel('Должность',{exact:true}).fill('B1 проверено');
   const save=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().endsWith('/api'+item.url));
   await page.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal((await save).status(),200);
   const reread=await read(item.url);assert.deepEqual([reread.last_name,reread.first_name,reread.middle_name,reread.user_id],item.legacy);
   if(!i)await capture('04-representative.png');
   await page.goto(base+`/arm2/${kind}/${parent.id}`);await page.getByRole('button',{name:'Контакты',exact:true}).click();
   assert.equal(await page.getByLabel('Основной подписант',{exact:false}).inputValue(),item.id);
   await page.getByRole('link',{name:'Иванов И.И.',exact:true}).waitFor();
   await page.getByRole('button',{name:'Техника',exact:true}).click();
   assert.equal(await page.getByRole('link',{name:'Добавить технику',exact:true}).count(),0);
  }
  await page.goto(base+'/arm2/veh/'+vehicle.id);await capture('05-vehicle.png');
  await page.getByRole('button',{name:'Документы',exact:true}).click();
  assert.ok((await page.locator('main').innerText()).includes('этап B3'));
  assert.equal(await page.locator('input[type=file]').count(),0);
  assert.equal(await page.getByRole('button',{name:'Сохранить',exact:true}).count(),0);
  assert.equal(errors.length,0,errors.join('\n'));
  const result={result:'WINDOWS_SMOKE_PASS',health,vehicle:vehicle.id,representatives:reps,
   seeded_vehicles:105,own_cp:cp.id,own_org:org.id,own_type:type.id,legacy_preserved:true,
   pagination_beyond_100:true,conflict_reload_retry:true,history:true,anonymous_401:true,csrf_403:true,
   forbidden_methods_404:true,integrations_404:true,original_rows_edited:false};
  fs.writeFileSync(path.join(local,'windows-smoke.json'),JSON.stringify(result,null,2));console.log('WINDOWS_SMOKE_PASS');
 } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exit(1);});
