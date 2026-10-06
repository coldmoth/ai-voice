const renderPage=require('./page.cjs');
/* Task 6 runtime checks; existing heading/nav translation assertions depend on Task 7. */
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const serveLocale = require('./locale-route.cjs');
const root = path.resolve(__dirname, '..');
const shots = path.join(root, 'state/research/ui-qa'); fs.mkdirSync(shots, {recursive:true});
const en = JSON.parse(fs.readFileSync(path.join(root, 'src/ai_voice/locales/en.json'), 'utf8'));
const ru = JSON.parse(fs.readFileSync(path.join(root, 'src/ai_voice/locales/ru.json'), 'utf8'));
let prefs = {language:'en', catalog_language:'en', speech_language:'en-US', asr_engine:'apple', favorite_ids:[], input_device:'MIC', output_device:'AI Voice'};
let status = {active:false, state:'stopped', mode:'mic', message:'Stopped'};
const posts = [], searches = [], tokens = [];
const speechLanguages = [{id:'ru-RU'}, {id:'en-US'}, {id:'de-DE'}];
const voices = [{id:'1'.repeat(32), name:'Narrator', source:'fish', language:'en'}];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  tokens.push({path:url.pathname, token:req.headers['x-ai-voice-token']});
  if (serveLocale(url, res, root)) return;
  let text = ''; for await (const chunk of req) text += chunk;
  const body = text ? JSON.parse(text) : {};
  const reply = data => { res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(data)); };
  if (url.pathname === '/') {
    const html = renderPage(root);
    res.writeHead(200, {'Content-Type':'text/html; charset=utf-8'}); return res.end(html);
  }
  if (url.pathname === '/api/voices') return reply({items:voices, language:prefs.language, preferences:prefs,
    speech_languages:speechLanguages, speech_language:prefs.speech_language, devices:{inputs:['MIC'], outputs:['AI Voice'], monitors:[], default_input:'MIC', default_output:'AI Voice', virtual:['AI Voice']}});
  if (url.pathname === '/api/keys') return reply({fish:true, hf:false});
  if (url.pathname === '/api/permissions') return reply({microphone:'authorized', speech:'authorized'});
  if (url.pathname === '/api/driver/status') return reply({available:false});
  if (url.pathname === '/api/preferences') { posts.push(body); prefs = {...prefs, ...body}; return reply(prefs); }
  if (url.pathname === '/api/status') return reply(status);
  if (url.pathname === '/api/voice_metadata') return reply({items:voices, pending:false});
  if (url.pathname === '/api/vc/voices') return reply({items:[], training:null, last_done:null});
  if (url.pathname === '/api/search') { searches.push(Object.fromEntries(url.searchParams)); return reply({items:voices, has_more:false, page:1}); }
  return reply({});
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({executablePath:process.env.AI_VOICE_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless:true});
    const page = await browser.newPage({viewport:{width:1220, height:800}}); const errors = [];
    const shot = (lang, name) => page.screenshot({path:path.join(shots, 'i18n-' + lang + '-' + name + '.png')});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port + '/');
    await page.waitForSelector('.voice-row');
    assert.equal(await page.locator('html').getAttribute('lang'), 'en');
    const assertEnglishCopy = async () => {
      const copy = await page.evaluate(() => {
        // Decision 11: the interface language select shows endonyms (Russian name of Russian).
        const endonyms = Array.from(document.querySelectorAll('#ui-language-select option'))
          .map(option => option.textContent).filter(value => /[А-Яа-яЁё]/.test(value));
        let bodyText = document.body.innerText;
        for (const name of endonyms) bodyText = bodyText.split(name).join('');
        return [bodyText,
        ...Array.from(document.querySelectorAll('[title], [placeholder], [aria-label]'))
          .flatMap(node => ['title', 'placeholder', 'aria-label'].map(attr => node.getAttribute(attr) || ''))];
      });
      assert.ok(copy.every(value => !/[А-Яа-яЁё]/.test(value)), 'English UI has no Cyrillic copy');
    };
    await assertEnglishCopy();
    assert.equal(await page.locator('.voice-row .voice-meta').first().textContent(), 'English · Fish Audio');
    await shot('en', 'main');
    await page.click('#settings-btn');
    for (const section of ['general', 'audio', 'asr', 'keys', 'storage', 'usage']) {
      await page.click('[data-section="' + section + '"]');
      await page.waitForFunction(s => !document.querySelector('[data-pane="' + s + '"]').hidden, section);
      await page.waitForTimeout(400);
      await assertEnglishCopy();
      await shot('en', section);
    }
    await page.click('[data-section="general"]');
    await page.keyboard.press('Escape');
    await page.click('[data-tab="catalog"]');
    await page.waitForSelector('.voice-row');
    await assertEnglishCopy();
    await page.click('[data-tab="mine"]');

    // Task 7 supplies data-i18n for these existing Russian elements.
    assert.equal(await page.locator('#settings-title').textContent(), en['settings.title']);
    await page.click('#settings-btn');
    assert.equal(await page.locator('[data-pane="general"]').isVisible(), true);
    assert.equal(await page.locator('#settings-h-general').textContent(), en['settings.general.title']);
    let navigations = 0; page.on('framenavigated', () => navigations++);
    await page.evaluate(() => { window.__nativeLanguages = []; window.webkit = {messageHandlers:{language:{postMessage:lang => window.__nativeLanguages.push(lang)}}}; });
    await page.selectOption('#ui-language-select', 'ru');
    await page.waitForFunction(() => document.documentElement.lang === 'ru');
    assert.deepEqual(posts.find(body => body.language), {language:'ru'});
    assert.equal(await page.locator('#settings-title').textContent(), ru['settings.title']);
    const headingKey = await page.locator('.workspace-heading h2').getAttribute('data-i18n');
    assert.ok(headingKey, 'Task 7 assigns the main heading translation key');
    assert.equal(await page.locator('.workspace-heading h2').textContent(), ru[headingKey]);
    for (const nav of await page.locator('.settings-nav-item').all()) {
      const key = await nav.getAttribute('data-i18n'); assert.ok(key);
      assert.equal(await nav.textContent(), ru[key]);
    }
    assert.equal(await page.locator('#start-btn').textContent(), ru['main.start_microphone']);
    assert.equal(await page.locator('#vc-audio-hint').textContent(), ru['vc.audio_hint']);
    assert.equal(await page.locator('.voice-row .preview-btn').getAttribute('title'), ru['catalog.demo_title']);
    const counters = await page.evaluate(() => ({
      voices:[1,2,5].map(n => tp('catalog.voice_count', n)),
      files:[1,2,5].map(n => tp('vc.selected_files', n, {size:'1,0'}))
    }));
    assert.deepEqual(counters.voices, ['one','few','many'].map((category, index) =>
      ru['catalog.voice_count_' + category].replace('{n}', [1,2,5][index])));
    assert.deepEqual(counters.files, ['one','few','many'].map((category, index) =>
      ru['vc.selected_files_' + category].replace('{n}', [1,2,5][index]).replace('{size}', '1,0')));
    for (const section of ['general', 'audio', 'asr', 'keys', 'storage', 'usage']) {
      await page.click('[data-section="' + section + '"]');
      await page.waitForFunction(s => !document.querySelector('[data-pane="' + s + '"]').hidden, section);
      await page.waitForTimeout(400);
      await shot('ru', section);
    }
    await page.click('[data-section="general"]');
    assert.equal(navigations, 0);
    assert.deepEqual(await page.evaluate(() => window.__nativeLanguages), ['ru']);
    assert.deepEqual(await page.evaluate(() => {
      const saved = I18N.dict;
      const unknown = t('no.such_key');
      const missing = t('settings.asr.language_count_other');
      const plurals = [tp('settings.asr.language_count', 1), tp('settings.asr.language_count', 5)];
      I18N.dict = {};
      const fallback = t('common.ok'); I18N.dict = saved;
      return {unknown, missing, plurals, fallback};
    }), {unknown:'no.such_key', missing:ru['settings.asr.language_count_other'],
      plurals:[ru['settings.asr.language_count_one'].replace('{n}', '1'), ru['settings.asr.language_count_many'].replace('{n}', '5')], fallback:en['common.ok']});
    assert.deepEqual(await page.evaluate(() => {
      const saved = I18N.dict;
      I18N.dict = {...saved, 'common.ok':'<img src=x onerror="window.__injected=true">'};
      const root = document.createElement('div');
      root.dataset.i18n = 'common.ok'; root.dataset.i18nTitle = 'common.ok';
      root.dataset.i18nPlaceholder = 'common.ok'; root.dataset.i18nAria = 'common.ok';
      applyI18n(root);
      I18N.dict = saved;
      return [root.textContent, root.title, root.getAttribute('placeholder'), root.getAttribute('aria-label'), root.childElementCount];
    }), Array(4).fill('<img src=x onerror="window.__injected=true">').concat(0));
    await page.click('[data-section="asr"]');
    assert.equal(await page.locator('#speech-language-select').inputValue(), 'en-US');
    status = {...status, active:true, state:'listening'};
    await page.waitForFunction(() => document.body.classList.contains('is-running'));
    await page.selectOption('#speech-language-select', 'de-DE');
    await page.waitForFunction(() => !document.querySelector('#speech-language-hint').hidden);
    assert.deepEqual(posts.find(body => body.speech_language), {speech_language:'de-DE'});
    assert.equal(await page.locator('#speech-language-hint').textContent(), ru['settings.asr.restart_hint']);
    await page.selectOption('#asr-engine', 'gigaam');
    await page.waitForFunction(() => document.querySelector('#speech-language-select').disabled);
    assert.equal(await page.locator('#speech-language-hint').textContent(), ru['settings.asr.gigaam_ru_only']);
    await page.keyboard.press('Escape');
    await page.click('[data-tab="catalog"]');
    const searchResponse = page.waitForResponse(response => response.url().includes('/api/search'));
    await page.selectOption('#catalog-language-filter', 'all');
    await page.waitForFunction(() => document.querySelector('#catalog-language-filter').value === 'all' && document.querySelector('.voice-row'));
    await searchResponse;
    assert.deepEqual(posts.find(body => body.catalog_language), {catalog_language:'all'});
    assert.ok(searches.every(query => !('language' in query) && !('catalog_language' in query)));
    await page.reload();
    await page.waitForSelector('.voice-row');
    assert.equal(await page.locator('html').getAttribute('lang'), 'ru');
    await page.click('[data-tab="mine"]');
    await shot('ru', 'main');
    assert.equal(await page.locator('#settings-title').textContent(), ru['settings.title']);
    assert.equal(await page.locator('#catalog-language-filter').inputValue(), 'all');
    await page.route('**/locales/ru.json', route => route.fulfill({status:500, json:{error:'Unavailable'}}));
    assert.deepEqual(await page.evaluate(async () => {
      await loadLocale('ru');
      return [I18N.dict, t('common.ok'), document.documentElement.lang];
    }), [{}, en['common.ok'], 'ru']);
    await page.route('**/locales/en.json', route => route.abort());
    assert.deepEqual(await page.evaluate(async () => {
      await loadLocale('ru');
      return [I18N.dict, I18N.fallback, t('common.ok')];
    }), [{}, {}, 'common.ok']);
    assert.ok(tokens.filter(item => item.path.startsWith('/locales/')).every(item => item.token === 'qa-token'));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({pass:true}));
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
