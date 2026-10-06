const renderPage = require('./page.cjs');
/* Spec G, task 2: live-voice engine card (Live voice) and the Settings -> Storage row against a local mocked API. */
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const serveLocale = require('./locale-route.cjs');
const root = path.resolve(__dirname, '..');
const shots = path.join(root, 'state/research/ui-qa'); fs.mkdirSync(shots, {recursive: true});
const dictionaries = Object.fromEntries(['en', 'ru'].map(lang => [lang,
  JSON.parse(fs.readFileSync(path.join(root, `src/ai_voice/locales/${lang}.json`), 'utf8'))]));
const fill = (text, vars) => Object.entries(vars).reduce((s, [k, v]) => s.split('{' + k + '}').join(v), text);
let prefs, engine, runtimeOk, engineEndpoint, postError, posts, engineGets, storage;
function reset(language = 'en') {
  prefs = {language, catalog_language: language, speech_language: 'en-US', asr_engine: 'apple', favorite_ids: [],
    input_device: null, output_device: null, onboarding_completed: true, update_auto: false};
  engine = {installed: false, version: null, status: 'idle', step: '', done_bytes: 0, total_bytes: 3.4e9, error: null,
    supported: true, free_bytes: 5e9};
  runtimeOk = false; engineEndpoint = true; postError = null; posts = []; engineGets = 0;
  storage = {total: 3.5e9, categories: [
    {id: 'vc_engine', label: dictionaries[language]['settings.storage.vc_engine'], bytes: 3.4e9, clearable: true},
    {id: 'logs', label: 'Logs', bytes: 1e8, clearable: true}]};
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
    devices: {inputs: ['MIC'], outputs: ['Speakers'], monitors: [], default_input: 'MIC', default_output: 'Speakers', virtual: []}});
  if (url.pathname === '/api/preferences') { prefs = {...prefs, ...body}; return reply(prefs); }
  if (engineEndpoint && url.pathname === '/api/vc/engine') { engineGets++; return reply(engine); }
  if (engineEndpoint && url.pathname.startsWith('/api/vc/engine/')) {
    if (postError) return reply({error: postError}, postError === 'busy' ? 409 : 400);
    const action = url.pathname.split('/').pop();
    if (action === 'install') engine = {...engine, status: 'downloading', step: 'uv', done_bytes: 1.2e9, error: null};
    if (action === 'cancel') engine = {...engine, status: 'cancelled', step: '', error: null};
    if (action === 'remove') engine = {...engine, version: null, status: 'idle'};
    return reply(engine);
  }
  if (url.pathname === '/api/storage') return reply(storage);
  if (url.pathname === '/api/storage/clear') {
    storage = {total: 1e8, categories: storage.categories.filter(c => c.id !== body.category)};
    return reply(storage);
  }
  if (url.pathname === '/api/keys') return reply({fish: false, hf: false});
  if (url.pathname === '/api/permissions') return reply({microphone: 'authorized', speech: 'authorized'});
  if (url.pathname === '/api/status') return reply({version: '0.4.0', active: false, state: 'stopped', mode: 'mic', message: 'Stopped'});
  if (url.pathname === '/api/voice_metadata') return reply({items: [], pending: false});
  if (url.pathname === '/api/vc/voices') return reply({items: [], training: null, last_done: null, runtime_ok: runtimeOk});
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
    const card = page.locator('#vc-engine-card'), line = page.locator('#vc-engine-line'), error = page.locator('#vc-engine-error');
    const install = page.locator('#vc-engine-install');
    const loadVc = async () => {
      await page.goto(base, {waitUntil: 'domcontentloaded'});
      await page.waitForFunction(() => window.aiVoiceApp?.prefs().language === document.documentElement.lang);
      await page.click('.mode-btn[data-mode="vc"]');
    };
    const showCard = async () => { await loadVc(); await card.waitFor({state: 'visible'}); };
    const lineText = value => page.waitForFunction(v => document.getElementById('vc-engine-line')?.textContent === v, value);
    const gb = (lang, v) => v + ' ' + dictionaries[lang]['common.gb'];
    let checks = 0;

    // runtime_ok true: no card, the normal panel renders.
    reset(); runtimeOk = true;
    await loadVc(); await page.waitForSelector('#vc-name'); await page.waitForTimeout(400);
    assert.equal(await card.count(), 0);
    assert.equal(await page.locator('.vc-stage').isVisible(), true); checks++;

    // Older build / mock without the endpoint: no card, the existing "environment missing" error stays.
    reset(); engineEndpoint = false;
    await loadVc(); await page.waitForSelector('#vc-error:not([hidden])'); await page.waitForTimeout(400);
    assert.equal(await card.count(), 0);
    assert.equal(await page.locator('#vc-error').textContent(), en['errors.vc_environment_missing']); checks++;

    // Idle card: texts, link, panel hidden, no polling while idle.
    reset();
    await showCard();
    assert.equal(await page.locator('#vc-engine-title').textContent(), en['engine.title']);
    assert.equal(await page.locator('#vc-engine-about').textContent(), fill(en['engine.about'], {size: gb('en', '3.4')}));
    assert.equal(await install.textContent(), en['engine.download']);
    assert.equal(await page.locator('#vc-engine-actions a').getAttribute('href'), 'https://github.com/coldmoth/ai-voice#live-voice-engine');
    assert.equal(await page.locator('#vc-engine-actions a').textContent(), en['engine.what']);
    assert.equal(await page.locator('#vc-engine-actions a').evaluate(n => getComputedStyle(n).opacity), '1');
    assert.equal(await page.locator('.vc-stage').isHidden(), true);
    assert.equal(await line.isHidden(), true); assert.equal(await error.isHidden(), true);
    assert.ok(await card.evaluate(n => n.classList.contains('glass-panel')));
    const idleGets = engineGets; await page.waitForTimeout(1500);
    assert.equal(engineGets, idleGets, 'no polling while idle'); checks++;

    // Download -> progress text and determinate bar -> uv step (indeterminate) -> cancel.
    await install.click();
    assert.ok(posts.some(p => p.path === '/api/vc/engine/install'));
    await lineText(fill(en['engine.progress'], {step: en['engine.step.downloading'], done: gb('en', '1.2'), total: gb('en', '3.4')}));
    assert.equal(await page.locator('#vc-engine-bar').getAttribute('aria-valuenow'), '35');
    assert.equal(await page.locator('#vc-engine-bar > i').evaluate(n => n.style.width), '35%');
    assert.equal(await page.locator('#vc-engine-cancel').textContent(), en['common.cancel']);
    engine = {...engine, status: 'installing', step: 'Installing packages', done_bytes: 3.4e9};
    await lineText(en['engine.step.packages']);
    assert.ok(await page.locator('#vc-engine-bar').evaluate(n => n.classList.contains('vc-indeterminate')));
    assert.notEqual(await page.locator('#vc-engine-bar > i').evaluate(n => getComputedStyle(n).animationName), 'none');
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await page.locator('#vc-engine-bar > i').evaluate(n => getComputedStyle(n).animationName), 'none');
    await page.emulateMedia({reducedMotion: 'no-preference'}); checks++;
    await page.locator('#vc-engine-cancel').click();
    assert.ok(posts.some(p => p.path === '/api/vc/engine/cancel'));
    await install.waitFor({state: 'visible'});
    assert.equal(await install.textContent(), en['engine.download']);
    assert.equal(await line.isHidden(), true); checks++;

    // Error texts by code; polling stops on error.
    const errorCases = [['network', en['engine.error.network']], ['checksum', en['engine.error.checksum']],
      ['disk', fill(en['engine.error.disk'], {need: gb('en', '7.5'), have: gb('en', '5.0')})],
      ['uv', fill(en['engine.error.other'], {code: 'uv'})]];
    for (const [code, expected] of errorCases) {
      reset(); engine.status = 'downloading';
      await showCard(); await line.waitFor({state: 'visible'});
      engine = {...engine, status: 'error', error: code};
      await error.waitFor({state: 'visible'});
      assert.equal(await error.textContent(), expected);
      assert.equal(await install.textContent(), en['engine.retry']);
      assert.equal(await line.isHidden(), true);
    }
    const errorGets = engineGets; await page.waitForTimeout(1500);
    assert.equal(engineGets, errorGets, 'polling stops on error');
    await install.click(); await lineText(fill(en['engine.progress'], {step: en['engine.step.downloading'], done: gb('en', '1.2'), total: gb('en', '3.4')}));
    assert.equal(await error.isHidden(), true); checks++;

    // A refused POST (409 busy) shows the generic text with the code.
    reset(); postError = 'busy';
    await showCard(); await install.click();
    await error.waitFor({state: 'visible'});
    assert.equal(await error.textContent(), fill(en['engine.error.other'], {code: 'busy'})); checks++;

    // Unsupported Mac: explanation, no button.
    reset(); engine.supported = false;
    await showCard();
    assert.equal(await page.locator('#vc-engine-about').textContent(), en['engine.unsupported']);
    assert.equal(await page.locator('#vc-engine-actions button').count(), 0); checks++;

    // Version mismatch: "Update components" removes, then installs.
    reset(); engine.version = '0.9';
    await showCard();
    assert.equal(await install.textContent(), en['engine.update']);
    await install.click(); await line.waitFor({state: 'visible'});
    assert.deepEqual(posts.filter(p => p.path.startsWith('/api/vc/engine/')).map(p => p.path), ['/api/vc/engine/remove', '/api/vc/engine/install']); checks++;

    // Success hides the card and renders the normal panel without a reload.
    reset(); engine.status = 'verifying';
    await showCard(); await lineText(en['engine.step.verifying']);
    engine = {...engine, status: 'idle', installed: true, version: '1'}; runtimeOk = true;
    await card.waitFor({state: 'detached'});
    await page.waitForSelector('.vc-stage #vc-name');
    assert.equal(await page.locator('.vc-stage').isVisible(), true);
    const doneGets = engineGets; await page.waitForTimeout(1500);
    assert.equal(engineGets, doneGets, 'polling stops on success'); checks++;

    // Settings -> Storage: "Voice engine" row with Remove and the shared two-click confirm.
    reset();
    await page.goto(base, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => window.aiVoiceApp?.prefs().language === document.documentElement.lang);
    await page.evaluate(() => window.aiVoiceOpenSettings('storage'));
    const remove = page.locator('[data-clear="vc_engine"]');
    await remove.waitFor({state: 'visible'});
    assert.equal(await page.locator('.storage-row[data-cat="vc_engine"] .storage-label').textContent(), en['settings.storage.vc_engine']);
    assert.equal(await remove.textContent(), en['engine.remove']);
    await remove.click();
    assert.equal(await remove.textContent(), en['common.confirm']);
    assert.equal(posts.some(p => p.path === '/api/storage/clear'), false);
    await remove.click();
    await remove.waitFor({state: 'detached'});
    assert.deepEqual(posts.find(p => p.path === '/api/storage/clear')?.body, {category: 'vc_engine'}); checks++;

    // Screenshots: idle, downloading, error, EN/RU x 1220/850.
    let screenshots = 0;
    for (const lang of ['en', 'ru']) for (const width of [1220, 850]) {
      const dict = dictionaries[lang];
      await page.setViewportSize({width, height: width === 1220 ? 800 : 650});
      reset(lang);
      await showCard();
      const overflow = await page.evaluate(() => [...document.querySelectorAll('#vc-engine-card *')]
        .filter(n => n.scrollWidth > n.clientWidth + 1 && getComputedStyle(n).overflow !== 'hidden').map(n => n.id || n.className));
      assert.deepEqual(overflow, [], 'no horizontal overflow in the card');
      await page.screenshot({path: path.join(shots, `vc-engine-${lang}-${width}-idle.png`)}); screenshots++;
      await install.click();
      await lineText(fill(dict['engine.progress'], {step: dict['engine.step.downloading'], done: gb(lang, (1.2).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {minimumFractionDigits: 1})), total: gb(lang, (3.4).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {minimumFractionDigits: 1}))}));
      await page.screenshot({path: path.join(shots, `vc-engine-${lang}-${width}-downloading.png`)}); screenshots++;
      engine = {...engine, status: 'error', error: 'network'};
      await error.waitFor({state: 'visible'});
      assert.equal(await error.textContent(), dict['engine.error.network']);
      await page.screenshot({path: path.join(shots, `vc-engine-${lang}-${width}-error.png`)}); screenshots++;
    }
    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({pass: true, checks, pageErrors, screenshots}));
  } finally { if (browser) await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
