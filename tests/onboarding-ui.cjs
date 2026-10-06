const renderPage = require('./page.cjs');
/* Onboarding overlay (spec C, task 6): mocked API, headless Chrome. */
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const serveLocale = require('./locale-route.cjs');
const root = path.resolve(__dirname, '..');
const shots = path.join(root, 'state/research/ui-qa'); fs.mkdirSync(shots, {recursive: true});
const en = JSON.parse(fs.readFileSync(path.join(root, 'src/ai_voice/locales/en.json'), 'utf8'));
const ru = JSON.parse(fs.readFileSync(path.join(root, 'src/ai_voice/locales/ru.json'), 'utf8'));
const BAD = 'bad-key-1234567890', GOOD = 'good-key-abcdef123456';
let prefs, keys, perms, posts, responses;
const voices = [{id: '1'.repeat(32), name: 'Narrator', source: 'fish', language: 'en'}];
function reset(language = 'en') {
  prefs = {language, catalog_language: language, speech_language: 'en-US', asr_engine: 'apple', favorite_ids: [],
    input_device: null, output_device: null, onboarding_completed: false};
  keys = {fish: false, hf: false};
  perms = {microphone: 'not_determined', speech: 'not_determined'};
  posts = []; responses = [];
}
reset();
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (serveLocale(url, res, root)) return;
  let text = ''; for await (const chunk of req) text += chunk;
  const body = text ? JSON.parse(text) : {};
  const reply = (data, code = 200) => { const out = JSON.stringify(data); responses.push(out); res.writeHead(code, {'Content-Type': 'application/json'}); res.end(out); };
  if (req.method === 'POST') posts.push({path: url.pathname, body});
  if (url.pathname === '/') { res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'}); return res.end(renderPage(root)); }
  if (url.pathname === '/api/voices') return reply({items: voices, language: prefs.language, preferences: prefs,
    speech_languages: [{id: 'ru-RU'}, {id: 'en-US'}], speech_language: prefs.speech_language,
    devices: {inputs: ['MIC'], outputs: ['Speakers', 'BlackHole 2ch'], monitors: [], default_input: 'MIC', default_output: 'BlackHole 2ch', virtual: ['BlackHole 2ch']}});
  if (url.pathname === '/api/preferences') { prefs = {...prefs, ...body}; return reply(prefs); }
  if (url.pathname === '/api/keys') return reply(keys);
  if (url.pathname === '/api/keys/fish') {
    if (body.key === BAD) return reply({ok: false, error: 'invalid'});
    keys.fish = true; return reply({ok: true});
  }
  if (url.pathname === '/api/permissions') return reply(perms);
  if (url.pathname === '/api/permissions/request') { perms = {...perms, [body.kind]: 'authorized'}; return reply(perms); }
  if (url.pathname === '/api/driver/status') return reply({available: false});
  if (url.pathname === '/api/status') return reply({active: false, state: 'stopped', mode: 'mic', message: 'Stopped'});
  if (url.pathname === '/api/voice_metadata') return reply({items: voices, pending: false});
  if (url.pathname === '/api/vc/voices') return reply({items: [], training: null, last_done: null});
  if (url.pathname === '/api/search') return reply({items: voices, has_more: false, page: 1});
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
    const title = () => page.locator('#ob-title').textContent();
    const load = async () => { await page.goto(base, {waitUntil: 'domcontentloaded'}); await page.waitForSelector('#onboarding:not([hidden])'); await page.waitForSelector('#ob-title'); };
    const next = async () => { await page.click('#ob-next'); await page.waitForTimeout(260); };
    const skip = async () => { await page.click('#ob-skip'); await page.waitForTimeout(260); };

    // 1-2: overlay shown, language switch is live.
    await load();
    assert.equal(await title(), en['onboarding.lang.title']);
    const url0 = page.url();
    await page.click('.ob-lang[data-lang="ru"]');
    await page.waitForFunction(t => document.querySelector('#ob-title').textContent === t, ru['onboarding.lang.title']);
    assert.ok(posts.some(p => p.path === '/api/preferences' && p.body.language === 'ru'));
    assert.equal(page.url(), url0);
    await page.click('.ob-lang[data-lang="en"]');
    await page.waitForFunction(t => document.querySelector('#ob-title').textContent === t, en['onboarding.lang.title']);

    // 3: Fish step.
    await next();
    assert.equal(await title(), en['onboarding.fish.title']);
    assert.equal(await page.locator('#ob-next').isDisabled(), true);
    await page.fill('#ob-fish-key-input', BAD); await page.click('#ob-step .key-verify');
    await page.waitForFunction(() => document.querySelector('#ob-step .key-msg')?.textContent);
    assert.equal(await page.locator('#ob-step .key-msg').textContent(), en['onboarding.fish.error_invalid']);
    assert.equal(await page.locator('#ob-next').isDisabled(), true);
    assert.equal(await page.locator('#ob-fish-key-input').inputValue(), '');
    await page.fill('#ob-fish-key-input', GOOD); await page.click('#ob-step .key-verify');
    await page.waitForFunction(() => !document.querySelector('#ob-next').disabled);
    assert.equal(await page.locator('#ob-fish-key-input').inputValue(), '');
    assert.ok(!responses.some(r => r.includes(BAD) || r.includes(GOOD)), 'a response echoed the key');
    assert.equal(await page.evaluate(k => [document.documentElement.outerHTML.includes(k)], GOOD).then(r => r[0]), false);

    // 4: permissions.
    await next();
    assert.equal(await title(), en['onboarding.perms.title']);
    // entering the step requests both permissions without a click
    for (const kind of ['microphone', 'speech'])
      await page.waitForFunction(([k, t]) => document.querySelector(`#ob-perm-${k} .ob-chip`)?.textContent === t, [kind, en['onboarding.perms.allowed']]);
    assert.deepEqual(posts.filter(p => p.path === '/api/permissions/request').map(p => p.body.kind), ['microphone', 'speech']);

    // 5: audio device.
    await next();
    assert.equal(await title(), en['onboarding.device.title']);
    assert.equal(await page.locator('#ob-dev-builtin').count(), 0);
    await page.click('#ob-dev-blackhole');
    await page.waitForFunction(() => document.querySelector('.ob-hint')?.textContent.includes('BlackHole 2ch'));
    assert.deepEqual(posts.findLast(p => p.path === '/api/preferences').body, {output_device: 'BlackHole 2ch'});

    // 6: fresh reload, skip everything.
    reset(); await load();
    await skip(); await skip(); await skip(); await skip();
    assert.equal(await title(), en['onboarding.done.title']);
    const doneText = await page.locator('#ob-step').textContent();
    assert.ok(doneText.includes(en['onboarding.done.fish_skipped']) && doneText.includes(en['onboarding.done.device_skipped']));
    await page.click('#ob-next');
    await page.waitForSelector('#onboarding', {state: 'hidden'});
    assert.deepEqual(posts.findLast(p => p.path === '/api/preferences').body, {onboarding_completed: true});
    await page.waitForSelector('#fish-banner', {state: 'visible'});
    await page.click('#fish-banner-btn');
    await page.waitForSelector('[data-pane="api"]', {state: 'visible'});

    // 7: Reduce Motion stops the loop.
    await page.emulateMedia({reducedMotion: 'reduce'});
    reset(); await load();
    const f0 = await page.evaluate(() => window.aiVoiceOnboarding.frames());
    await page.waitForTimeout(500);
    assert.ok(await page.evaluate(() => window.aiVoiceOnboarding.frames()) - f0 <= 1);
    await page.emulateMedia({reducedMotion: 'no-preference'});

    // 8: keyboard only.
    reset(); await load();
    const tabTo = async id => { for (let i = 0; i < 20; i++) { if (await page.evaluate(() => document.activeElement.id) === id) return; await page.keyboard.press('Tab'); } throw new Error('no focus on ' + id); };
    await tabTo('ob-next'); await page.keyboard.press('Enter'); await page.waitForTimeout(260);
    assert.equal(await title(), en['onboarding.fish.title']);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#onboarding').isVisible(), true);
    for (let i = 0; i < 3; i++) { await tabTo('ob-skip'); await page.keyboard.press('Enter'); await page.waitForTimeout(260); }
    assert.equal(await title(), en['onboarding.done.title']);
    await tabTo('ob-next'); await page.keyboard.press('Enter');
    await page.waitForSelector('#onboarding', {state: 'hidden'});

    // 9: screenshots and overflow.
    const shotsList = [];
    for (const lang of ['en', 'ru']) for (const width of [1220, 850]) {
      reset(lang); await page.setViewportSize({width, height: width === 1220 ? 800 : 650}); await load();
      for (let step = 1; step <= 5; step++) {
        await page.evaluate(n => window.aiVoiceOnboarding.show(n), step); await page.waitForTimeout(300);
        const over = await page.evaluate(() => {
          const glass = document.querySelector('#ob-glass'), box = glass.getBoundingClientRect();
          const wide = [...glass.querySelectorAll('*')].filter(n => n.getBoundingClientRect().width > box.width + 0.5).map(n => n.className || n.tagName);
          return {scroll: glass.scrollWidth - glass.clientWidth, wide};
        });
        assert.ok(over.scroll <= 0 && !over.wide.length, `overflow ${lang} ${width} step ${step}: ` + JSON.stringify(over));
        const file = `state/research/ui-qa/onboarding-${lang}-${width}-step${step}.png`;
        await page.screenshot({path: path.join(root, file)}); shotsList.push(file);
      }
    }
    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({pass: true, pageErrors, screenshots: shotsList.length}));
  } finally { if (browser) await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
