// AUTHOR-RUN: real restored Workspace, native login, no mocks or cloud provider.
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const {chromium, expect} = await import(pathToFileURL(path.join(process.env.ARM2_QA_PLAYWRIGHT, 'test.mjs')).href);
const state = JSON.parse(process.env.ARM2_QA_BROWSER_STATE);
const browser = await chromium.launch({headless:true, ...(process.env.ARM2_QA_BROWSER_EXECUTABLE ? {executablePath:process.env.ARM2_QA_BROWSER_EXECUTABLE} : {})});
const context = await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
const page = await context.newPage();
const errors=[];
page.on('pageerror', e=>errors.push(e.message));
try {
  const login=await context.request.post(state.origin+'/api/auth/login',{data:{login:'integration-qa',password:state.password}});
  expect(login.status()).toBe(200);
  await page.goto(state.origin+'/arm2/veh/bulk?session='+state.workspace);
  await expect(page.getByRole('status').filter({hasText:'Черновик сохранён на сервере'})).toBeVisible();
  await expect(page.getByRole('region',{name:'Рабочий набор'}).getByText(/Сохранено VEH: 1/)).toBeVisible();
  await page.screenshot({path:path.join(state.directory,'01-restored-bulk.png'),fullPage:true});
  await page.goto(state.origin+'/arm2/veh/'+state.vehicle);
  await expect(page.getByLabel('Модель',{exact:true})).toHaveValue('QA imported');
  await expect(page.getByRole('combobox',{name:'Операционный владелец',exact:true})).toHaveValue('Организация · Synthetic ORG');
  await expect(page.getByRole('button',{name:'Документы',exact:true})).toBeVisible();
  await page.screenshot({path:path.join(state.directory,'02-restored-vehicle.png'),fullPage:true});
  await page.getByRole('button',{name:'Документы',exact:true}).click();
  await expect(page.getByText('Integration document',{exact:true})).toBeVisible();
  await page.getByRole('link',{name:'Integration document',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Текущая редакция 2',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Предыдущие редакции',exact:true}).click();
  await page.locator('summary').filter({hasText:'Предыдущая редакция 1'}).click();
  const downloading=page.waitForEvent('download');
  await page.locator('li').filter({hasText:'original-1.pdf'}).getByRole('link',{name:'Скачать файл',exact:true}).click();
  const target=path.join(state.directory,'historical-original.pdf');
  await (await downloading).saveAs(target);
  expect(createHash('sha256').update(fs.readFileSync(target)).digest('hex')).toBe(state.historical_sha256);
  await page.screenshot({path:path.join(state.directory,'03-restored-documents.png'),fullPage:true});
  await page.goto(state.origin+'/arm2/type/'+state.type);
  await expect(page.getByRole('combobox',{name:'Тип государственного номера',exact:true})).toHaveValue('Автомобильный');
  await page.screenshot({path:path.join(state.directory,'04-restored-type.png'),fullPage:true});
  expect(errors).toEqual([]);
  fs.writeFileSync(path.join(state.directory,'browser-result.json'),JSON.stringify({classification:'AUTHOR-RUN, synthetic QA',result:'PASS',nativeLogin:true,restoredBulkReceiptVisible:true,restoredVehicleModelVisible:true,b2OwnerVisible:true,b3DocumentsVisible:true,historicalFileDownloadSha256:state.historical_sha256,typeCategoryVisible:true,pageErrors:errors},null,2));
  console.log('Restored native Workspace / B4 receipt / B2 VEH / B3 documents / TYPE: PASS');
} finally {await browser.close();}
