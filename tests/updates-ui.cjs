const renderPage = require('./page.cjs');
/* Spec D, task 2: update controls against a local mocked API. */
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const serveLocale = require('./locale-route.cjs');
const root = path.resolve(__dirname, '..');
const shots = path.join(root, 'state/research/ui-qa');
const dictionaries = Object.fromEntries(['en', 'ru'].map(lang => [lang,
  JSON.parse(fs.readFileSync(path.join(root, `src/ai_voice/locales/${lang}.json`), 'utf8'))]));
const release = {status: 'available', version: '0.10.0', url: 'https://github.com/coldmoth/ai-voice/releases/tag/v0.10.0'};
let job = {state: 'idle', percent: 0, error: null}, prefs, posts, result, checkCode, pendingCheck, holdCheck, version, checkArrived;
function reset(language = 'en', onboarding = false) {
  prefs = {language, catalog_language: language, speech_language: 'en-US', asr_engine: 'apple', favorite_ids: [],
    input_device: null, output_device: null, onboarding_completed: !onboarding, update_auto: true};
  posts = []; result = {...release}; checkCode = 200; pendingCheck = null; holdCheck = false; checkArrived = null; version = '0.4.0';
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
  if (url.pathname === '/api/update-check') {
    if (holdCheck && body.force) { pendingCheck = () => reply(result, checkCode); checkArrived(); return; }
    return reply(result, checkCode);
  }
  if (url.pathname === '/api/update-install') return reply({ok: true});
  if (url.pathname === '/api/update-status') return reply(job);
  if (url.pathname === '/api/update-skip') return reply({ok: true});
  if (url.pathname === '/api/status') return reply({version, active: false, state: 'stopped', mode: 'mic', message: 'Stopped'});
  if (url.pathname === '/api/keys') return reply({fish: false, hf: false});
  if (url.pathname === '/api/permissions') return reply({microphone: 'authorized', speech: 'authorized'});
  if (url.pathname === '/api/driver/status') return reply({available: false});
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
    const banner = page.locator('#update-banner'), button = page.locator('#update-check-btn');
    const status = page.locator('#update-check-status');
    const checks = () => posts.filter(p => p.path === '/api/update-check');
    const load = async () => {
      await page.goto(base, {waitUntil: 'domcontentloaded'});
      await page.waitForFunction(() => window.aiVoiceApp?.prefs().language === document.documentElement.lang);
      await page.waitForSelector('#fish-banner', {state: 'visible'});
    };
    const settings = async () => {
      await page.evaluate(() => window.aiVoiceOpenSettings('general'));
      await page.waitForSelector('[data-pane="general"]', {state: 'visible'});
      await page.waitForFunction(v => document.querySelector('#update-version').textContent === v, version);
    };
    const waitStatus = async text => page.waitForFunction(value => document.querySelector('#update-check-status').textContent === value, text);

    // Boot is delayed; both banners coexist; close is session-only and sends no skip.
    await load();
    assert.equal(await banner.isVisible(), false);
    assert.equal(checks().length, 0);
    await banner.waitFor({state: 'visible'});
    assert.deepEqual(checks().map(p => p.body), [{force: false}]);
    assert.equal(await page.locator('#update-banner-text').textContent(), 'AI Voice 0.10.0 is available');
    assert.equal(await page.locator('#update-download').getAttribute('href'), release.url);
    assert.equal(await page.locator('#update-download').getAttribute('target'), '_blank');
    assert.equal(await page.locator('#update-download').getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await page.locator('#fish-banner').isVisible(), true);
    await page.click('#update-close');
    assert.equal(await banner.isVisible(), false);
    assert.ok(!posts.some(p => p.path === '/api/update-skip'));
    await page.waitForTimeout(1100);
    assert.equal(await banner.isVisible(), false);
    assert.equal(checks().length, 1);

    // A fresh launch checks again; skip persists the exact version through its endpoint.
    reset(); await load(); await banner.waitFor({state: 'visible'});
    await page.click('#update-skip'); await banner.waitFor({state: 'hidden'});
    assert.deepEqual(posts.findLast(p => p.path === '/api/update-skip').body, {version: release.version});

    // At timer fire an open onboarding overlay suppresses the check without retry.
    reset('en', true); await load();
    await page.waitForSelector('#onboarding:not([hidden])');
    await page.waitForTimeout(3400);
    assert.equal(checks().length, 0);
    await page.evaluate(() => window.aiVoiceOnboarding.show(5));
    await page.click('#ob-next'); await page.waitForSelector('#onboarding', {state: 'hidden'});
    await page.waitForTimeout(3400);
    assert.equal(checks().length, 0);

    // Manual current response, pending state, six-second clearing, version fetch and checkbox sync.
    reset(); result = {status: 'current', version}; await load(); await settings();
    assert.equal(await page.locator('#update-auto').isChecked(), true);
    await page.uncheck('#update-auto');
    await page.waitForFunction(() => window.aiVoiceApp.prefs().update_auto === false);
    assert.deepEqual(posts.findLast(p => p.path === '/api/preferences').body, {update_auto: false});
    await page.check('#update-auto');
    await page.waitForFunction(() => window.aiVoiceApp.prefs().update_auto === true);
    assert.deepEqual(posts.findLast(p => p.path === '/api/preferences').body, {update_auto: true});
    holdCheck = true;
    const arrived = new Promise(resolve => { checkArrived = resolve; });
    await button.click();
    assert.equal(await button.isDisabled(), true);
    assert.equal(await button.textContent(), dictionaries.en['update.settings.checking']);
    await arrived;
    assert.equal(typeof pendingCheck, 'function');
    pendingCheck(); holdCheck = false;
    await waitStatus(dictionaries.en['update.settings.current']);
    assert.deepEqual(checks().find(p => p.body.force).body, {force: true});
    assert.equal(await status.getAttribute('role'), 'status');
    assert.equal(await status.getAttribute('aria-live'), 'polite');
    await page.waitForTimeout(6100);
    assert.equal(await status.textContent(), '');
    assert.equal(await button.isDisabled(), false);
    assert.equal(await button.textContent(), dictionaries.en['update.settings.check']);
    version = '0.4.1'; await settings(); // Reopening reads status again, independently of polling.

    // Available, API errors and thrown HTTP failure all use the specified copy.
    for (const [response, key, code] of [
      [release, 'update.settings.available', 200],
      [{status: 'error', error: 'network'}, 'update.settings.error_network', 200],
      [{status: 'error', error: 'github'}, 'update.settings.error_github', 200],
      [{error: 'mock failure'}, 'update.settings.error_network', 503],
    ]) {
      reset(); result = response; checkCode = code; await load(); await settings();
      await button.click(); await waitStatus(dictionaries.en[key].replace('{version}', release.version));
      if (response.status === 'available') {
        assert.equal(await banner.isVisible(), true);
        assert.equal(await page.locator('#update-download').getAttribute('href'), release.url);
      } else {
        await page.waitForTimeout(3200); // Automatic errors (including thrown requests) stay silent.
        assert.equal(await banner.isVisible(), false);
      }
    }

    // Version strings are text, including malicious mock data in the banner and Settings.
    reset(); version = '<script>window.updateInjected=true</script>'; result = {...release, version};
    await load(); await settings(); await button.click();
    await waitStatus(dictionaries.en['update.settings.available'].replace('{version}', version));
    assert.equal(await page.locator('#update-banner-text').textContent(), dictionaries.en['update.banner.text'].replace('{version}', version));
    assert.equal(await page.locator('#update-banner script, #update-version script, #update-check-status script').count(), 0);
    assert.equal(await page.evaluate(() => window.updateInjected), undefined);

    // Install button: progress labels, error + retry; the banner nodes are never rebuilt.
    {
      const en = dictionaries.en, install = page.locator('#update-install'), text = page.locator('#update-banner-text');
      const label = async value => page.waitForFunction(v => document.querySelector('#update-install').textContent === v, value);
      const installs = () => posts.filter(p => p.path === '/api/update-install');
      job = {state: 'idle', percent: 0, error: null};
      reset(); await load(); await banner.waitFor({state: 'visible'});
      assert.equal(await install.textContent(), en['update.banner.install']);
      await page.evaluate(() => { window.__nodes = [document.querySelector('#update-install'), document.querySelector('#update-banner-text'), document.querySelector('#update-banner')]; });
      job = {state: 'downloading', percent: 42, error: null};
      await install.click();
      await label(en['update.banner.downloading'].replace('{percent}', '42'));
      assert.equal(installs().length, 1);
      assert.equal(await install.isDisabled(), true);
      assert.equal(await page.locator('#update-skip').isDisabled(), true);
      assert.equal(await page.locator('#update-close').isDisabled(), true);
      job = {state: 'installing', percent: 100, error: null};
      await label(en['update.banner.installing']);
      job = {state: 'error', percent: 0, error: 'checksum'};
      await label(en['update.banner.retry']);
      assert.equal(await text.textContent(), en['update.error.checksum']);
      assert.equal(await install.isDisabled(), false);
      assert.equal(await page.locator('#update-skip').isDisabled(), false);
      assert.equal(await page.locator('#update-download').isVisible(), true);
      job = {state: 'downloading', percent: 5, error: null};
      await install.click();
      await label(en['update.banner.downloading'].replace('{percent}', '5'));
      assert.equal(installs().length, 2);
      assert.equal(await page.evaluate(() => window.__nodes[0] === document.querySelector('#update-install')
        && window.__nodes[1] === document.querySelector('#update-banner-text') && window.__nodes[2] === document.querySelector('#update-banner')), true);
      job = {state: 'idle', percent: 0, error: null};
    }

    // EN/RU banner and General screenshots at reviewer sizes; explicit 900 px overflow check.
    fs.mkdirSync(shots, {recursive: true});
    let screenshots = 0;
    for (const lang of ['en', 'ru']) for (const width of [1220, 850, 900]) {
      reset(lang); await page.setViewportSize({width, height: width === 1220 ? 800 : 650});
      await load(); await banner.waitFor({state: 'visible'});
      const dict = dictionaries[lang];
      assert.equal(await page.locator('#update-banner-text').textContent(), dict['update.banner.text'].replace('{version}', release.version));
      assert.equal(await page.locator('#update-download').textContent(), dict['update.banner.download']);
      assert.equal(await page.locator('#update-skip').textContent(), dict['update.banner.skip']);
      assert.equal(await page.locator('#update-install').textContent(), dict['update.banner.install']);
      const overflow = async () => page.evaluate(() => {
        const nodes = [document.documentElement, document.querySelector('#update-banner'), document.querySelector('[data-pane="general"]')];
        return nodes.filter(n => n.getClientRects().length).map(n => ({id: n.id || n.tagName, overflow: n.scrollWidth - n.clientWidth}));
      });
      assert.ok((await overflow()).every(n => n.overflow <= 0), `banner overflow ${lang} ${width}`);
      await page.screenshot({path: path.join(shots, `updates-${lang}-${width}-banner.png`)}); screenshots++;
      await settings();
      assert.equal(await button.textContent(), dict['update.settings.check']);
      assert.equal(await page.locator('label[for="update-auto"]').textContent(), dict['update.settings.auto']);
      await button.click(); await waitStatus(dict['update.settings.available'].replace('{version}', release.version));
      assert.ok((await overflow()).every(n => n.overflow <= 0), `General overflow ${lang} ${width}`);
      await page.screenshot({path: path.join(shots, `updates-${lang}-${width}-general.png`)}); screenshots++;
    }
    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({pass: true, pageErrors, screenshots}));
  } finally { if (browser) await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
