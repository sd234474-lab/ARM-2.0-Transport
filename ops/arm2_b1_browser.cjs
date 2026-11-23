/* Real Edge UI/API/storage checks; only generated B1 secrets and synthetic data. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const local = path.join(root, '.local/b1');
const {chromium} = require(path.join(local, 'browser/node_modules/playwright'));
const cfg = JSON.parse(fs.readFileSync(path.join(local, 'sandbox.json'), 'utf8'));
const seed = JSON.parse(fs.readFileSync(path.join(local, 'evidence/seed.json'), 'utf8'));
const base = 'http://127.0.0.1:18102';
const before = 'http://127.0.0.1:18104';
const output = path.join(root, 'docs/evidence/arm2-ui-b1');
fs.mkdirSync(output, {recursive:true});
(async()=>{
 const browser = await chromium.launch({executablePath: path.join(process.env['ProgramFiles(x86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe'),
   headless:true, args:['--disable-background-networking','--disable-sync','--no-first-run','--disable-component-update']});
 try {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*', route => [base,before].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  async function login(page, user='b1-admin') {
   await page.goto(base+'/arm2');
   await page.getByLabel('Логин',{exact:true}).fill(user);
   await page.getByLabel('Пароль',{exact:true}).fill(cfg.admin_password);
   await page.getByRole('button',{name:'Войти в систему'}).click();
   await page.waitForURL('**/arm2/veh');
  }
  async function read(resource) {
   const response=await page.request.get(base+'/api'+resource);
   assert.equal(response.status(),200,resource);
   return response.json();
  }
  async function capture(origin,route,name) {
   await page.goto(origin+'/arm2/'+route);
   await page.locator('h2').first().waitFor();
   if (!route.includes('/')) await page.locator('tbody tr').first().waitFor();
   await page.evaluate(()=>document.fonts.ready);
   await page.waitForLoadState('networkidle');
   await page.screenshot({path:path.join(output,name),fullPage:false});
  }
  await login(page);
  if(process.argv.includes('--reopen')) {
   await page.getByLabel('Поиск по рабочему имени / госномеру',{exact:true}).fill('машина за сотой 130');
   await page.getByRole('link',{name:'Машина за сотой 130',exact:true}).waitFor();
   for(const [catalog,q] of [['types','самосвал'],['counterparties','контрагент 110'],['organizations','организация 105']]) {
    assert.equal((await read(`/arm2/${catalog}/search?q=${encodeURIComponent(q)}`)).matched,1);
   }
   assert.equal((await page.request.get(base+'/api/arm2/vehicles/search?q=%00')).status(),422);
   const saved=JSON.parse(fs.readFileSync(path.join(local,'evidence/browser.json'),'utf8'));
   const vehicle=await read('/arm2/vehicles/'+saved.created_vehicle);
   assert.equal(vehicle.model,'Сохранено после конфликта');
   await page.goto(base+'/arm2/veh/'+vehicle.id);
   assert.equal(await page.getByLabel('Рабочее имя',{exact:false}).inputValue(),'B1 созданная машина');
   await page.getByRole('button',{name:'История',exact:true}).click();
   await page.getByRole('button',{name:/Открыть событие/}).first().click();
   await page.getByRole('region',{name:'Изменения события'}).waitFor();
   for(const kind of ['CP','ORG']) {
    const parent=kind==='CP'?'counterparties':'organizations';
    const rep=await read(`/arm2/${parent}/${kind==='CP'?seed.cp:seed.org}/representatives/${seed.representatives[kind]}`);
    assert.deepEqual([rep.last_name,rep.first_name,rep.middle_name],['СохранённаяФамилия','СохранённоеИмя','СохранённоеОтчество']);
   }
   fs.writeFileSync(path.join(local,'evidence/restart.json'),JSON.stringify({persisted:true,history:true,legacy_representative_parts:true}));
   console.log('PASS: B1 Russian lowercase search, NUL rejection, saved record/history/legacy values after scoped restart');
   return;
  }
  for(const [route,name] of [['','01-landing'],['veh/'+seed.vehicle,'02-vehicle'],['type/'+seed.type,'03-type'],['cp/'+seed.cp,'04-party']]) {
   await capture(before,route,name+'-before.png');
   await capture(base,route,name+'-after.png');
  }
  for(const [origin,suffix] of [[before,'before'],[base,'after']]) {
   await page.goto(origin+'/arm2/cp/'+seed.cp);await page.getByRole('button',{name:'Контакты',exact:true}).click();
   await page.getByRole('link',{name:origin===base?'Иванов И.И.':'Иванов Иван Иванович',exact:true}).waitFor();
   await page.screenshot({path:path.join(output,'05-representatives-'+suffix+'.png')});
  }
  await page.goto(base+'/arm2/veh');
  await page.getByLabel('Поиск по рабочему имени / госномеру',{exact:true}).fill('a 130 bc 86');
  await page.getByRole('link',{name:'Машина за сотой 130',exact:true}).waitFor();
  assert.equal((await read('/arm2/vehicles/search?q=a%20130%20bc%2086')).matched,1);
  for(const [kind,name] of [['CP','Контрагент 001'],['ORG','Организация 001']]) {
   await page.goto(base+'/arm2/veh');
   await page.getByRole('combobox',{name:'Фильтр: операционный владелец',exact:true}).fill(name);
   await page.getByRole('option',{name:(kind==='CP'?'Контрагент':'Организация')+' · '+name,exact:true}).click();
   const result=await read(`/arm2/vehicles/search?owner_kind=${kind}&owner_id=${kind==='CP'?seed.cp:seed.org}`);
   assert.ok(result.matched>0);
   assert.ok(result.items.every(row=>row[kind==='CP'?'owner_cp_id':'owner_org_id']===(kind==='CP'?seed.cp:seed.org)));
  }
  await page.goto(base+'/arm2/cp');
  await page.getByLabel('Поиск по имени / полному наименованию / ИНН',{exact:true}).fill('Контрагент 110');
  await page.getByRole('link',{name:'Контрагент 110',exact:true}).waitFor();
  await page.goto(base+'/arm2/org');
  await page.getByLabel('Поиск по имени / полному наименованию / ИНН',{exact:true}).fill('Организация 105');
  await page.getByRole('link',{name:'Организация 105',exact:true}).waitFor();
  await page.goto(base+'/arm2/cp/'+seed.cp);await page.getByRole('button',{name:'Техника',exact:true}).click();
  await page.locator('tbody tr').first().waitFor();
  assert.equal(await page.getByRole('link',{name:'Добавить технику',exact:true}).count(),0);
  await page.screenshot({path:path.join(output,'06-owned-vehicles-after.png')});
  await page.goto(base+'/arm2/veh/new');
  await page.getByLabel('Рабочее имя',{exact:false}).fill('B1 созданная машина');
  let serial=777; while((await read('/arm2/vehicles/search?q=А'+serial+'ВС86')).matched && serial<900)serial++;
  await page.getByLabel('Государственный номер',{exact:false}).fill('A'+serial+'BC86');
  await page.getByRole('combobox',{name:'Тип техники',exact:true}).fill('Погрузчик');
  await page.getByRole('option',{name:'Погрузчик',exact:true}).click();
  await page.getByRole('combobox',{name:'Операционный владелец',exact:true}).fill('Организация 105');
  await page.getByRole('option',{name:'Организация · Организация 105',exact:true}).click();
  const creating=page.waitForResponse(r=>r.url().endsWith('/api/arm2/vehicles')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  const createdResponse=await creating;assert.equal(createdResponse.status(),201);const created=await createdResponse.json();
  await page.waitForURL('**/arm2/veh/'+created.id);
  await page.goto(base+'/arm2/veh/'+created.id);
  assert.equal(await page.getByLabel('Рабочее имя',{exact:false}).inputValue(),'B1 созданная машина');
  assert.equal((await read('/arm2/vehicles/'+created.id)).owner_org_id,'ORG-000105');
  // Real concurrent update, then the existing UI-A1 compare/cancel/confirmed reload flow.
  const csrf=decodeURIComponent((await context.cookies()).find(c=>c.name==='csrf').value);
  const {id,version,...body}=await read('/arm2/vehicles/'+created.id);
  const concurrent=await page.request.put(base+'/api/arm2/vehicles/'+id,{headers:{Origin:base,'X-CSRF-Token':csrf},data:{...body,expected_version:version,model:'Серверное изменение'}});
  assert.equal(concurrent.status(),200);
  await page.getByLabel('Модель',{exact:true}).fill('Мой черновик');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByRole('button',{name:'Сравнить с сервером',exact:true}).click();
  assert.equal(await page.getByLabel('Модель',{exact:true}).inputValue(),'Мой черновик');
  await page.screenshot({path:path.join(output,'07-version-conflict-after.png')});
  page.once('dialog',d=>d.dismiss());await page.getByRole('button',{name:'Перезагрузить карточку',exact:true}).click();
  assert.equal(await page.getByLabel('Модель',{exact:true}).inputValue(),'Мой черновик');
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Перезагрузить карточку',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('label')].find(label=>label.textContent==='Модель')?.querySelector('input')?.value==='Серверное изменение');
  assert.equal(await page.getByLabel('Модель',{exact:true}).inputValue(),'Серверное изменение');
  await page.getByLabel('Модель',{exact:true}).fill('Сохранено после конфликта');
  const updating=page.waitForResponse(r=>r.url().endsWith('/api/arm2/vehicles/'+id)&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal((await updating).status(),200);
  await page.getByRole('button',{name:'История',exact:true}).click();
  await page.getByRole('button',{name:/Открыть событие/}).first().click();
  await page.getByRole('region',{name:'Изменения события'}).waitFor();
  for(const kind of ['CP','ORG']) {
   const parent=kind==='CP'?'counterparties':'organizations', parentId=kind==='CP'?seed.cp:seed.org;
   const resource=`/arm2/${parent}/${parentId}/representatives/${seed.representatives[kind]}`;
   await page.goto(base+`/arm2/${kind.toLowerCase()}/${parentId}/representatives/${seed.representatives[kind]}`);
   await page.getByLabel('ФИО / Имя',{exact:false}).waitFor();
   assert.equal(await page.getByLabel('Фамилия',{exact:true}).count(),0);
   await page.getByLabel('Должность',{exact:true}).fill('B1 проверенная должность');
   const saving=page.waitForResponse(r=>r.url().endsWith('/api'+resource)&&r.request().method()==='PUT');
   await page.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal((await saving).status(),200);
   const reread=await read(resource);
   assert.deepEqual([reread.last_name,reread.first_name,reread.middle_name],['СохранённаяФамилия','СохранённоеИмя','СохранённоеОтчество']);
   assert.equal(reread.authority_basis,'Тестовое основание');assert.ok(reread.user_id);
   if(kind==='CP')await page.screenshot({path:path.join(output,'08-representative-card-after.png')});
  }
  await page.goto(base+'/arm2/cp/'+seed.cp);await page.getByRole('button',{name:'Контакты',exact:true}).click();
  await page.getByRole('link',{name:'Добавить представителя',exact:true}).click();
  await page.getByLabel('ФИО / Имя',{exact:false}).fill('Эдуард');
  const adding=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/representatives'));
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();const repResponse=await adding;assert.equal(repResponse.status(),201);const newRep=await repResponse.json();
  await page.waitForURL('**/representatives/'+newRep.id);
  await page.getByRole('link',{name:'← К родительской карточке',exact:true}).click();await page.getByRole('button',{name:'Контакты',exact:true}).click();
  await page.getByLabel('Основной подписант',{exact:false}).selectOption(newRep.id);
  const signer=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().endsWith('/counterparties/'+seed.cp));
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();assert.equal((await signer).status(),200);
  assert.equal((await read('/arm2/counterparties/'+seed.cp)).primary_signer_id,newRep.id);
  await page.setViewportSize({width:390,height:844});
  await capture(base,'veh/'+created.id,'09-vehicle-narrow-after.png');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  await capture(base,'veh','10-list-narrow-after.png');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  const viewerContext=await browser.newContext();await viewerContext.route('**/*', route => [base,before].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());const viewer=await viewerContext.newPage();
  await login(viewer,'b1-viewer');
  assert.equal(await viewer.getByRole('link',{name:'+ Новая запись',exact:true}).count(),0);
  const denied=await viewer.request.get(base+'/api/arm2/counterparties/search');assert.equal(denied.status(),403);
  const visible=await viewer.request.get(base+'/api/arm2/vehicles/search');assert.equal(visible.status(),200);
  assert.ok((await visible.json()).items.every(row=>row.type_name===null&&row.owner_name===null));
  assert.deepEqual(errors,[]);
  const evidence={ui_login:true,search_beyond100:true,owner_filters_cp_org:true,party_search_beyond100:true,
    ui_create_reopen:true,created_vehicle:created.id,version_conflict_real:true,history_read:true,
    representative_round_trip_cp_org:true,representative_create:true,primary_signer:true,new_rep:newRep.id,
    restricted_view_no_related_names:true,narrow_no_page_overflow:true,browser_errors:errors};
  fs.writeFileSync(path.join(local,'evidence/browser.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence));
 } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
