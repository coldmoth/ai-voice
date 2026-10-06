const renderPage = require('./page.cjs');
/* Spec E, task 3: built-in driver UI (onboarding card + Settings -> Audio row) against a local mocked API. */
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const serveLocale = require('./locale-route.cjs');
const root = path.resolve(__dirname, '..');
const shots = path.join(root, 'state/research/ui-qa'); fs.mkdirSync(shots, {recursive: true});
const dictionaries = Object.fromEntries(['en', 'ru'].map(lang => [lang,
  JSON.parse(fs.readFileSync(path.join(root, `src/ai_voice/locales/${lang}.json`), 'utf8'))]));
const BUILTIN = 'AI Voice Mic';
let prefs, driver, outputs, posts, outcome, jobPolls;
function reset(language = 'en', onboarding = false) {
  prefs = {language, catalog_language: language, speech_language: 'en-US', asr_engine: 'apple', favorite_ids: [],
    input_device: null, output_device: null, onboarding_completed: !onboarding, update_auto: false};
  outputs = ['Speakers'];
  posts = []; outcome = 'done'; jobPolls = 0;
  driver = {available: true, installed: false, device_present: false, bundled_version: '0.7.1', installed_version: null,
    job: {state: 'idle', action: null}};
}
reset();
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (serveLocale(url, res, root)) return;
  let text = ''; for await (const chunk of req) text += chunk;
  const body = text ? JSON.parse(text) : {};
  const reply = (data, code = 200) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  if (req.method === 'POST') posts.push({path: url.pathname, body});
  if (url.pathname === '/') { res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'}); return res.end(renderPage(root)); }
  if (url.pathname === '/api/voices') return reply({items: [], language: prefs.language, preferences: prefs,
    speech_languages: [{id: 'ru-RU'}, {id: 'en-US'}], speech_language: prefs.speech_language,
    devices: {inputs: ['MIC'], outputs, monitors: [], default_input: 'MIC', default_output: 'Speakers', virtual: []}});
  if (url.pathname === '/api/preferences') { prefs = {...prefs, ...body}; return reply(prefs); }
  if (url.pathname === '/api/driver/status') {
    if (driver.job.state === 'running') { // second poll finishes the job
      jobPolls += 1;
      if (jobPolls >= 2) {
        const action = driver.job.action;
        if (outcome === 'done') {
          driver.job = {state: 'done', action};
          if (action === 'install') {
            driver.installed = true; driver.device_present = true; driver.installed_version = driver.bundled_version;
            if (!outputs.includes(BUILTIN)) outputs.push(BUILTIN);
          } else {
            driver.installed = false; driver.device_present = false; driver.installed_version = null;
            outputs = outputs.filter(name => name !== BUILTIN);
          }
        } else driver.job = {state: outcome, action};
      }
    }
    return reply(driver);
  }
  if (url.pathname === '/api/driver/install' || url.pathname === '/api/driver/uninstall') {
    driver.job = {state: 'running', action: url.pathname === '/api/driver/install' ? 'install' : 'uninstall'};
    jobPolls = 0;
    return reply({ok: true});
  }
  if (url.pathname === '/api/devices/rescan') return reply({ok: true});
  if (url.pathname === '/api/keys') return reply({fish: false, hf: false});
  if (url.pathname === '/api/permissions') return reply({microphone: 'authorized', speech: 'authorized'});
  if (url.pathname === '/api/status') return reply({version: '0.4.0', active: false, state: 'stopped', mode: 'mic', message: 'Stopped'});
  if (url.pathname === '/api/voice_metadata') return reply({items: [], pending: false});
  if (url.pathname === '/api/vc/voices') return reply({items: [], training: null, last_done: null});
  if (url.pathname === '/api/search') return reply({items: [], has_more: false, page: 1});
  return reply({});
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port + '/';
  let browser;
  try {
    browser = await chromium.launch({executablePath: process.env.AI_VOICE_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
    const page = await browser.newPage({viewport: {width: 1220, height: 800}});
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    const en = dictionaries.en;
    const load = async onboarding => {
      await page.goto(base, {waitUntil: 'domcontentloaded'});
      if (onboarding) { await page.waitForSelector('#onboarding:not([hidden])'); await page.waitForSelector('#ob-title'); }
      else await page.waitForFunction(() => window.aiVoiceApp?.prefs().language === document.documentElement.lang);
    };
    const step4 = async () => {
      await page.evaluate(() => window.aiVoiceOnboarding.show(4));
      await page.waitForSelector('#ob-dev-blackhole');
    };
    const settingsAudio = async () => {
      await page.evaluate(() => window.aiVoiceOpenSettings('audio'));
      await page.waitForSelector('[data-pane="audio"]', {state: 'visible'});
      await page.waitForSelector('#driver-row:not([hidden])');
    };
    const statusText = text => page.waitForFunction(value => document.getElementById('driver-status')?.textContent === value, text);
    const busyCard = text => page.waitForFunction(value => {
      const b = document.getElementById('ob-dev-install');
      return b && b.disabled && b.textContent === value;
    }, text);
    const idleCard = text => page.waitForFunction(value => {
      const b = document.getElementById('ob-dev-install');
      return b && !b.disabled && b.textContent === value;
    }, text);
    const busyRow = text => page.waitForFunction(value => {
      const buttons = [...document.querySelectorAll('#driver-actions button')];
      return buttons.length > 0 && buttons.every(b => b.disabled) &&
        document.getElementById('driver-status')?.textContent === value;
    }, text);

    // Card hidden when the build has no bundled driver.
    reset('en', true); driver.available = false;
    await load(true); await step4();
    await page.waitForTimeout(400);
    assert.equal(await page.locator('#ob-dev-builtin').count(), 0);

    // Idle -> install -> running -> done + present: the card becomes selected.
    reset('en', true);
    await load(true); await step4();
    const card = page.locator('#ob-dev-builtin');
    await card.waitFor({state: 'visible'});
    assert.equal(await card.getAttribute('aria-disabled'), 'true');
    assert.equal(await page.locator('#ob-dev-install').textContent(), en['settings.driver.install']);
    await page.locator('#ob-dev-install').click();
    await busyCard(en['settings.driver.installing']);
    assert.equal(await page.locator('.ob-card', {has: page.locator('#ob-dev-builtin')}).getAttribute('aria-busy'), 'true');
    await page.waitForFunction(() => document.getElementById('ob-dev-builtin')?.getAttribute('aria-checked') === 'true', null, {timeout: 9000});
    assert.ok(posts.some(p => p.path === '/api/driver/install'));
    assert.deepEqual(posts.find(p => p.path === '/api/preferences' && p.body.output_device === BUILTIN)?.body, {output_device: BUILTIN});
    assert.equal(await page.locator('#ob-dev-builtin .ob-chip.ok').textContent(), en['onboarding.device.installed']);
    assert.equal(await page.locator('.ob-card', {has: page.locator('#ob-dev-builtin')}).locator('.ob-hint').textContent(), en['settings.driver.discord_restart']);

    // Cancelled admin prompt: back to idle, no message, no selection.
    reset('en', true); outcome = 'cancelled';
    await load(true); await step4();
    await page.locator('#ob-dev-install').click();
    await busyCard(en['settings.driver.installing']);
    await idleCard(en['settings.driver.install']);
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#ob-devices [role="alert"]').count(), 0);
    assert.equal(await card.getAttribute('aria-checked'), 'false');
    assert.equal(posts.some(p => p.path === '/api/preferences' && p.body.output_device === BUILTIN), false);

    // Failed job: alert with the failed copy.
    reset('en', true); outcome = 'failed';
    await load(true); await step4();
    await page.locator('#ob-dev-install').click();
    await busyCard(en['settings.driver.installing']);
    const alert = page.locator('#ob-devices [role="alert"]');
    await alert.waitFor({state: 'visible', timeout: 9000});
    assert.equal(await alert.textContent(), en['settings.driver.failed']);
    await idleCard(en['settings.driver.install']);

    // Settings row: installed + present, two-click remove.
    reset('en');
    driver.installed = true; driver.device_present = true; driver.installed_version = '0.7.1'; outputs = ['Speakers', BUILTIN];
    await load(false); await settingsAudio();
    await statusText(en['settings.driver.installed']);
    assert.equal(await page.locator('#driver-caption').isHidden(), true);
    const driverButtons = () => page.locator('#driver-actions button');
    assert.deepEqual(await driverButtons().allTextContents(), [en['settings.driver.remove']]);
    await driverButtons().first().click();
    assert.equal(await driverButtons().first().textContent(), en['settings.driver.remove_confirm']);
    assert.equal(posts.some(p => p.path === '/api/driver/uninstall'), false);
    await driverButtons().first().click();
    assert.ok(posts.some(p => p.path === '/api/driver/uninstall'));
    await busyRow(en['settings.driver.removing']);
    await statusText(en['settings.driver.not_installed']);
    assert.equal(await page.locator('#driver-caption').isHidden(), false);
    assert.deepEqual(await driverButtons().allTextContents(), [en['settings.driver.install']]);
    await driverButtons().first().click();
    await busyRow(en['settings.driver.installing']);
    await statusText(en['settings.driver.installed']);

    // Settings row: older installed version shows the update button; not loaded shows the hint text.
    reset('en');
    driver.installed = true; driver.device_present = true; driver.installed_version = '0.6.0'; outputs = ['Speakers', BUILTIN];
    await load(false); await settingsAudio();
    await statusText(en['settings.driver.installed']);
    assert.deepEqual(await driverButtons().allTextContents(), [en['settings.driver.update'], en['settings.driver.remove']]);
    reset('en');
    driver.installed = true; driver.device_present = false; driver.installed_version = '0.7.1';
    await load(false); await settingsAudio();
    await statusText(en['settings.driver.not_loaded']);

    // Screenshots: onboarding card (idle, installing) and Settings row (installed, armed remove), EN/RU x 1220/850.
    let screenshots = 0;
    for (const lang of ['en', 'ru']) for (const width of [1220, 850]) {
      const dict = dictionaries[lang];
      await page.setViewportSize({width, height: width === 1220 ? 800 : 650});
      reset(lang, true);
      await load(true); await step4();
      await page.locator('#ob-dev-install').waitFor({state: 'visible'});
      await page.waitForFunction(() => getComputedStyle(document.querySelector('.ob')).opacity === '1');
      await page.screenshot({path: path.join(shots, `driver-${lang}-${width}-onboarding-idle.png`)}); screenshots++;
      await page.locator('#ob-dev-install').click();
      await busyCard(dict['settings.driver.installing']);
      await page.screenshot({path: path.join(shots, `driver-${lang}-${width}-onboarding-installing.png`)}); screenshots++;
      reset(lang);
      driver.installed = true; driver.device_present = true; driver.installed_version = '0.7.1'; outputs = ['Speakers', BUILTIN];
      await load(false); await settingsAudio();
      await statusText(dict['settings.driver.installed']);
      await page.screenshot({path: path.join(shots, `driver-${lang}-${width}-settings-installed.png`)}); screenshots++;
      await page.locator('#driver-actions button').first().click();
      assert.equal(await page.locator('#driver-actions button').first().textContent(), dict['settings.driver.remove_confirm']);
      await page.screenshot({path: path.join(shots, `driver-${lang}-${width}-settings-remove-confirm.png`)}); screenshots++;
    }
    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({pass: true, pageErrors, screenshots}));
  } finally { if (browser) await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
