/* Real Edge UI -> API -> new PostgreSQL only. No credentials/screenshots exported. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = 'C:/ARM2-UAT/ARM2-WIN-RUN-001';
const {chromium} = require(path.join(root, 'tools/browser/node_modules/playwright'));
const base = 'http://127.0.0.1:18082';
(async () => {
  const secret = JSON.parse(fs.readFileSync(path.join(root, 'private/secrets.json'), 'utf8'));
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true,
    args: ['--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run']});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const page = await context.newPage();
    await page.goto(base + '/arm2');
    await page.getByLabel('Логин', {exact: true}).fill(secret.admin_login);
    await page.getByLabel('Пароль', {exact: true}).fill(secret.admin_password);
    await page.getByRole('button', {name: 'Войти в систему'}).click();
    await page.waitForURL('**/arm2/type');
    if (process.argv.includes('--read-existing')) {
      const previous = JSON.parse(fs.readFileSync(path.join(root, 'evidence/browser.json'), 'utf8'));
      await page.goto(base + '/arm2/type/' + previous.id);
      assert.equal(await page.getByLabel('Рабочее имя', {exact: false}).inputValue(), previous.name);
      await page.getByRole('button', {name: 'История', exact: true}).click();
      await page.getByRole('button', {name: /Открыть событие/}).first().click();
      await page.getByRole('region', {name: 'Изменения события'}).waitFor();
      fs.writeFileSync(path.join(root, 'evidence/restart-browser.json'), JSON.stringify({id: previous.id, persisted: true, history: true}));
      console.log('PASS: same saved record and history visible in Edge after stop/start');
      return;
    }
    await page.getByRole('link', {name: 'Создать', exact: true}).click();
    const name = 'NATIVE UAT ' + new Date().toISOString();
    await page.getByLabel('Рабочее имя', {exact: false}).fill(name);
    const saved = page.waitForResponse(r => r.url().endsWith('/api/arm2/types') && r.request().method() === 'POST');
    await page.getByRole('button', {name: 'Сохранить', exact: true}).click();
    const response = await saved;
    assert.equal(response.status(), 201);
    const record = await response.json();
    await page.waitForURL('**/arm2/type/' + record.id);
    await page.goto(base + '/arm2/type');
    await page.getByRole('link', {name, exact: true}).click();
    assert.equal(await page.getByLabel('Рабочее имя', {exact: false}).inputValue(), name);
    await page.getByRole('button', {name: 'История', exact: true}).click();
    await page.getByRole('button', {name: /Открыть событие/}).first().click();
    await page.getByRole('region', {name: 'Изменения события'}).waitFor();
    // Same authenticated browser session/CSRF, same local proxy.
    const result = await page.evaluate(async ({id, name}) => {
      const read = await fetch('/api/arm2/types/' + id).then(r => r.json());
      const history = await fetch('/api/arm2/history/TYPE/' + id).then(r => r.json());
      return {read, history};
    }, {id: record.id, name});
    assert.equal(result.read.name, name);
    assert.equal(result.read.version, 1);
    assert.ok(result.history.length > 0);
    const signer = await page.evaluate(async () => {
      const csrf = decodeURIComponent(document.cookie.split('; ').find(x => x.startsWith('csrf=')).slice(5));
      async function request(url, method = 'GET', body) {
        const response = await fetch('/api/arm2/' + url, {method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: body && JSON.stringify(body)});
        return {status: response.status, body: await response.json()};
      }
      const outcomes = [];
      for (const [kind, prefix] of [['counterparties', 'CP'], ['organizations', 'ORG']]) {
        const a = await request(kind, 'POST', {name: 'Native signer test A ' + prefix});
        const b = await request(kind, 'POST', {name: 'Native signer test B ' + prefix});
        const rep = await request(kind + '/' + b.body.id + '/representatives', 'POST', {name: 'Foreign native test signer'});
        const historyBefore = await request('history/' + prefix + '/' + a.body.id);
        const statuses = [];
        for (const id of [prefix + 'REP-999999999', rep.body.id]) {
          const rejected = await request(kind + '/' + a.body.id, 'PUT', {expected_version: 1, name: a.body.name, primary_signer_id: id});
          statuses.push(rejected.status);
        }
        const read = await request(kind + '/' + a.body.id);
        const historyAfter = await request('history/' + prefix + '/' + a.body.id);
        outcomes.push({kind, id: a.body.id, foreign_id: rep.body.id, setup: [a.status, b.status, rep.status], statuses,
          unchanged: read.body.version === 1 && read.body.primary_signer_id === null && JSON.stringify(historyBefore.body) === JSON.stringify(historyAfter.body)});
      }
      return outcomes;
    });
    for (const item of signer) {
      assert.deepEqual(item.setup, [201, 201, 201]);
      assert.deepEqual(item.statuses, [422, 422]);
      assert.equal(item.unchanged, true);
    }
    const evidence = {ui_login: true, ui_create_save_reopen: true, ui_history_detail: true,
      id: record.id, name, version: result.read.version, event_id: result.history[0].event_id, signer};
    fs.writeFileSync(path.join(root, 'evidence/browser.json'), JSON.stringify(evidence, null, 2));
    console.log(JSON.stringify(evidence));
  } finally { await browser.close(); }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
