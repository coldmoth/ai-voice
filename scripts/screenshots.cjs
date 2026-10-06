#!/usr/bin/env node
/* Generates the public docs images in site/assets (spec F, task 3).
   App screenshots run against a local mocked API (no audio, no network, EN locale,
   1220x800, dark, deviceScaleFactor 2; a shot over 400 KB is retaken at factor 1).
   Usage: node scripts/screenshots.cjs [--banner]
   --banner renders banner.png, og.png and favicon.png instead. */
const {chromium} = require('playwright');
const fs = require('node:fs'), path = require('node:path');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'site', 'assets');
fs.mkdirSync(assets, {recursive: true});
const MAX_BYTES = 400 * 1024;
const CHROME = process.env.AI_VOICE_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

// Same assembly as desktop.render_page(): inline style + scripts into index.html.
function renderPage() {
  const dir = path.join(root, 'macos', 'desktop');
  const read = name => fs.readFileSync(path.join(dir, name), 'utf8');
  return read('index.html')
    .replace('__STYLE__', () => read('style.css') + '\n' + read('onboarding.css'))
    .replace('__SCRIPT__', () => 'const APP_TOKEN = "shots-token";\n' + read('app.js') + '\n' + read('onboarding.js'));
}

// Mock data: neutral, invented voice names only.
const voices = [
  {id: 'a1000000000000000000000000000001', name: 'Narrator', language: 'en', source: 'fish', avatar_url: null, tags: ['narration'], task_count: 48200},
  {id: 'a2000000000000000000000000000002', name: 'Aurora', language: 'en', source: 'fish', avatar_url: null, tags: ['female'], task_count: 31500},
  {id: 'a3000000000000000000000000000003', name: 'Captain', language: 'en', source: 'fish', avatar_url: null, tags: ['male'], task_count: 27400},
  {id: 'a4000000000000000000000000000004', name: 'Meadow', language: 'en', source: 'local', avatar_url: null, tags: ['calm'], task_count: 9800},
  {id: 'a5000000000000000000000000000005', name: 'Sage', language: 'en', source: 'fish', avatar_url: null, tags: ['documentary'], task_count: 7300},
  {id: 'a6000000000000000000000000000006', name: 'Atlas', language: 'en', source: 'fish', avatar_url: null, tags: ['deep'], task_count: 5100}];
const catalog = ['Echo', 'Juniper', 'Marlin', 'Pepper', 'Willow'].map((name, i) => ({
  id: 'c' + i + '0'.repeat(30), name, language: 'en', source: 'fish', avatar_url: null,
  tags: ['en'], task_count: 60000 - i * 9000, url: 'https://fish.audio/c' + i}));
const devices = {inputs: ['MacBook Pro Microphone'], outputs: ['AI Voice Mic', 'MacBook Pro Speakers'],
  monitors: ['External Headphones'], default_input: 'MacBook Pro Microphone', default_output: 'AI Voice Mic', virtual: ['AI Voice Mic']};
let prefs, keys, runtimeOk, engine, updateResult;
const driver = {available: true, installed: true, device_present: true, bundled_version: '0.7.1',
  installed_version: '0.7.1', job: {state: 'idle', action: null}};
function reset(overrides = {}) {
  prefs = {language: 'en', catalog_language: 'en', speech_language: 'en-US', voice_id: voices[0].id,
    favorite_ids: [voices[1].id], input_device: 'MacBook Pro Microphone', output_device: 'AI Voice Mic',
    output_gain_db: 0, normalize_loudness: true, monitor_enabled: false, monitor_device: null,
    monitor_gain_db: 0, input_gain_db: 0, monthly_char_limit: 50000, asr_engine: 'apple',
    onboarding_completed: true, update_auto: false, ...overrides};
  keys = {fish: true, hf: false};
  runtimeOk = true;
  engine = {installed: false, version: null, status: 'idle', step: '', done_bytes: 0, total_bytes: 3.4e9,
    error: null, supported: true, free_bytes: 8e9};
  updateResult = {status: 'none'};
}
const status = {version: '0.4.0', active: false, state: 'stopped', mode: 'mic', message: 'Stopped',
  transcript_partial: '', transcript_final: '', monitor_errors: 0, monitor_active: false,
  usage: {today: 1240, month: 18700, limit: 50000},
  history: [{id: 'h1', text: 'Hey, ready for the match?'}, {id: 'h2', text: 'Give me one minute'}]};

// The whole mocked backend as a pure function; served through Playwright route
// interception so no port is opened.
function handle(requestUrl, postText) {
  const url = new URL(requestUrl);
  const locale = /^\/locales\/(en|ru)\.json$/.exec(url.pathname);
  if (locale) return {contentType: 'application/json; charset=utf-8',
    body: fs.readFileSync(path.join(root, 'src/ai_voice/locales', locale[1] + '.json'))};
  if (url.pathname === '/') return {contentType: 'text/html; charset=utf-8', body: renderPage()};
  const body = postText && postText.trim().startsWith('{') ? JSON.parse(postText) : {};
  const json = data => ({contentType: 'application/json', body: JSON.stringify(data)});
  if (url.pathname === '/api/voices') return json({items: voices, preferences: prefs, language: prefs.language,
    speech_languages: [{id: 'en-US'}, {id: 'ru-RU'}], speech_language: prefs.speech_language, devices});
  if (url.pathname === '/api/preferences') { prefs = {...prefs, ...body}; return json(prefs); }
  if (url.pathname === '/api/status') return json(status);
  if (url.pathname === '/api/keys') return json(keys);
  if (url.pathname === '/api/permissions') return json({microphone: 'authorized', speech: 'authorized'});
  if (url.pathname === '/api/driver/status') return json(driver);
  if (url.pathname === '/api/voice_metadata') return json({items: voices, pending: false});
  if (url.pathname === '/api/vc/voices') return json({items: [], training: null, last_done: null, runtime_ok: runtimeOk});
  if (url.pathname === '/api/vc/engine') return json(engine);
  if (url.pathname === '/api/search') return json({items: catalog, has_more: false, page: 1});
  if (url.pathname === '/api/update-check') return json(updateResult);
  if (url.pathname === '/api/update-skip') return json({ok: true});
  return json({});
}

const pageErrors = [];
const BASE = 'http://ai-voice.local/';
async function capture(browser, name, scenario) {
  for (const dsf of [2, 1]) {
    const context = await browser.newContext({viewport: {width: 1220, height: 800},
      deviceScaleFactor: dsf, colorScheme: 'dark'});
    await context.route('**/*', route => {
      const result = handle(route.request().url(), route.request().postData() || '');
      route.fulfill({status: 200, contentType: result.contentType, body: result.body});
    });
    const page = await context.newPage();
    page.on('pageerror', e => pageErrors.push(name + ': ' + e.message));
    await scenario(page);
    const file = path.join(assets, name + '.png');
    await page.screenshot({path: file});
    const size = fs.statSync(file).size;
    await context.close();
    if (size <= MAX_BYTES || dsf === 1) {
      console.log(`${name}.png ${size} bytes (deviceScaleFactor ${dsf})`);
      return;
    }
  }
}

async function screenshots() {
  const browser = await chromium.launch({executablePath: CHROME, headless: true});
  try {
    const load = async page => {
      await page.goto(BASE, {waitUntil: 'domcontentloaded'});
      await page.waitForSelector('.voice-row', {timeout: 10000});
    };
    await capture(browser, 'main', async page => {
      reset(); await load(page); await page.waitForTimeout(1200);
    });
    await capture(browser, 'catalog', async page => {
      reset(); await load(page);
      await page.click('.tab[data-tab="catalog"]');
      await page.waitForSelector('a.fish-link', {state: 'attached', timeout: 10000});
      await page.waitForTimeout(400);
    });
    await capture(browser, 'settings-keys', async page => {
      reset(); await load(page);
      await page.evaluate(() => window.aiVoiceOpenSettings('api'));
      await page.waitForSelector('[data-pane="api"]', {state: 'visible', timeout: 10000});
      await page.waitForTimeout(400);
    });
    await capture(browser, 'onboarding-fish', async page => {
      reset({onboarding_completed: false}); keys = {fish: false, hf: false};
      await page.goto(BASE, {waitUntil: 'domcontentloaded'});
      await page.waitForSelector('#onboarding:not([hidden])', {timeout: 10000});
      await page.evaluate(() => window.aiVoiceOnboarding.show(2));
      await page.waitForSelector('#ob-fish-key-input', {state: 'visible', timeout: 10000});
      await page.waitForTimeout(500);
    });
    await capture(browser, 'onboarding-device', async page => {
      reset({onboarding_completed: false}); keys = {fish: true, hf: false};
      await page.goto(BASE, {waitUntil: 'domcontentloaded'});
      await page.waitForSelector('#onboarding:not([hidden])', {timeout: 10000});
      await page.evaluate(() => window.aiVoiceOnboarding.show(4));
      await page.waitForSelector('#ob-dev-builtin', {state: 'visible', timeout: 10000});
      await page.waitForTimeout(500);
    });
    await capture(browser, 'update', async page => {
      reset({update_auto: true});
      updateResult = {status: 'available', version: '0.4.1',
        url: 'https://github.com/coldmoth/ai-voice/releases/tag/v0.4.1'};
      await load(page);
      await page.waitForSelector('#update-banner', {state: 'visible', timeout: 15000});
      await page.waitForTimeout(400);
    });
    await capture(browser, 'engine', async page => {
      reset(); runtimeOk = false;
      await load(page);
      await page.click('.mode-btn[data-mode="vc"]');
      await page.waitForSelector('#vc-engine-card', {state: 'visible', timeout: 10000});
      await page.waitForTimeout(500);
    });
  } finally { await browser.close(); }
}

function bannerHtml(iconData) {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
  *{margin:0;box-sizing:border-box}html,body{height:100%}
  body{background:#05070d;color:#eef3ff;display:flex;align-items:center;justify-content:center;overflow:hidden;
    font-family:-apple-system,'SF Pro Display','Helvetica Neue',Arial,sans-serif;position:relative}
  .glow{position:absolute;border-radius:50%;filter:blur(90px);opacity:.5}
  .g1{width:720px;height:720px;left:-200px;top:-240px;background:radial-gradient(circle,#4c7dff 0%,transparent 65%)}
  .g2{width:620px;height:620px;right:-180px;bottom:-280px;background:radial-gradient(circle,#2743a8 0%,transparent 65%)}
  .wrap{position:relative;display:flex;align-items:center;gap:40px;padding:0 64px}
  img{width:180px;height:180px;border-radius:42px;box-shadow:0 18px 60px rgba(76,125,255,.35)}
  h1{font-size:92px;letter-spacing:-2px;font-weight:700}
  p{font-size:34px;color:#b9c6ee;margin-top:12px}
  </style></head><body>
  <div class="glow g1"></div><div class="glow g2"></div>
  <div class="wrap"><img src="${iconData}" alt=""><div><h1>AI Voice</h1><p>Speak with any voice in any app</p></div></div>
  </body></html>`;
}
async function banner() {
  const tmp = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'ai-voice-icon-'));
  const iconPng = path.join(tmp, 'icon.png');
  execFileSync('sips', ['-s', 'format', 'png', '-z', '256', '256',
    path.join(root, 'macos', 'AppIcon.icns'), '--out', iconPng], {stdio: 'inherit'});
  execFileSync('sips', ['-z', '64', '64', iconPng, '--out', path.join(assets, 'favicon.png')], {stdio: 'inherit'});
  const iconData = 'data:image/png;base64,' + fs.readFileSync(iconPng).toString('base64');
  const browser = await chromium.launch({executablePath: CHROME, headless: true});
  try {
    for (const [name, width, height] of [['banner', 1280, 640], ['og', 1200, 630]]) {
      const page = await browser.newPage({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: 'dark'});
      page.on('pageerror', e => pageErrors.push(name + ': ' + e.message));
      await page.setContent(bannerHtml(iconData), {waitUntil: 'load'});
      await page.waitForTimeout(200);
      const file = path.join(assets, name + '.png');
      await page.screenshot({path: file});
      console.log(`${name}.png ${fs.statSync(file).size} bytes`);
      await page.close();
    }
    console.log(`favicon.png ${fs.statSync(path.join(assets, 'favicon.png')).size} bytes`);
  } finally { await browser.close(); fs.rmSync(tmp, {recursive: true, force: true}); }
}

(async () => {
  try {
    if (process.argv.includes('--banner')) {
      await banner();
    } else {
      await screenshots();
    }
    if (pageErrors.length) { console.error(JSON.stringify({pageErrors})); process.exitCode = 1; }
  } catch (e) { console.error(e); process.exit(1); }
})();
