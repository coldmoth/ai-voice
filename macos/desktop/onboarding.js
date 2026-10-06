// First-launch onboarding overlay. Runs after app.js in the same inline script and uses its globals
// (api, t, applyI18n, langName, displayLang, buildKeyForm) plus the window.aiVoiceApp hook object.
// Dictionary values only go through textContent / attributes.
(function () {
  const OB = {step: 1, keys: null, perms: null, devices: null, driver: null, formState: 'idle', pending: {},
    pollTimer: null, requestDoneAt: 0, token: 0, raf: 0, frames: 0, hideTimer: 0, error: ''};
  const $id = id => document.getElementById(id);
  const mk = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const btn = (cls, text, id) => { const b = mk('button', cls, text); b.type = 'button'; if (id) b.id = id; return b; };
  const link = (href, text) => { const a = mk('a', '', text); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; };
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const isOpen = () => !$id('onboarding').hidden;
  const apiPrefs = () => window.aiVoiceApp.prefs();

  // ---- Background: slow blurred blobs on a canvas ----
  const PAL = ['#3d7bff', '#9b5cff', '#19d3c5', '#ff5fa2'];
  const SPEED = 0.3, PARALLAX = 0.12, EASE = 0.02;
  const bg = {W: 0, H: 0, mx: .5, my: .5, sx: .5, sy: .5, t: 0, resizing: false, listening: false};
  function bgFit() {
    // The blobs are soft, so a quarter of the CSS size is enough; full Retina resolution starved the main thread.
    const cv = $id('ob-canvas');
    bg.W = cv.width = Math.max(1, Math.round(cv.clientWidth / 4));
    bg.H = cv.height = Math.max(1, Math.round(cv.clientHeight / 4));
  }
  function bgDraw(animate) {
    const ctx = $id('ob-canvas').getContext('2d'), {W, H} = bg;
    if (animate) { bg.t += .016 * SPEED; bg.sx += (bg.mx - bg.sx) * EASE; bg.sy += (bg.my - bg.sy) * EASE; }
    const time = bg.t;
    ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 5; i++) {
      const x = W * (.5 + .38 * Math.sin(time * .9 + i * 1.7) + (bg.sx - .5) * PARALLAX * (i % 2 ? 1 : -1));
      const y = H * (.5 + .35 * Math.cos(time * .7 + i * 2.3) + (bg.sy - .5) * PARALLAX);
      const r = Math.min(W, H) * (.5 + .08 * Math.sin(time * 1.3 + i));
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, PAL[i % 4] + '8c'); g.addColorStop(1, PAL[i % 4] + '00');
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    }
    OB.frames++;
  }
  function bgFrame() {
    OB.raf = 0;
    if (document.hidden || !isOpen()) return;
    bgDraw(true);
    OB.raf = requestAnimationFrame(bgFrame);
  }
  function bgStart() {
    bgFit();
    if (reduced()) { bgDraw(false); return; }
    if (!bg.listening) {
      bg.listening = true;
      $id('onboarding').addEventListener('mousemove', e => {
        const r = $id('onboarding').getBoundingClientRect();
        bg.mx = (e.clientX - r.left) / r.width; bg.my = (e.clientY - r.top) / r.height;
      });
    }
    if (!OB.raf) OB.raf = requestAnimationFrame(bgFrame);
  }
  function bgStop() { if (OB.raf) cancelAnimationFrame(OB.raf); OB.raf = 0; }
  window.addEventListener('resize', () => {
    if (!isOpen() || bg.resizing) return;
    bg.resizing = true;
    requestAnimationFrame(() => { bg.resizing = false; bgFit(); if (!OB.raf) bgDraw(false); });
  });
  document.addEventListener('visibilitychange', () => { if (isOpen() && !document.hidden && !reduced() && !OB.raf) OB.raf = requestAnimationFrame(bgFrame); });

  // ---- Footer ----
  function renderFooter() {
    $id('ob-back').hidden = OB.step === 1;
    $id('ob-skip').hidden = OB.step === 5;
    $id('ob-next').textContent = t(OB.step === 5 ? 'onboarding.done.start' : 'onboarding.next');
    $id('ob-next').disabled = OB.step === 2 && !['ok', 'warning', 'connected'].includes(OB.formState);
    [...$id('ob-dots').children].forEach((dot, i) => {
      dot.className = i + 1 === OB.step ? 'active' : i + 1 < OB.step ? 'done' : '';
    });
  }

  // ---- Steps ----
  const title = key => { const h = mk('h2', '', t(key)); h.id = 'ob-title'; h.tabIndex = -1; return h; };
  const errorLine = () => { const p = mk('p', 'ob-error'); p.setAttribute('role', 'alert'); p.textContent = OB.error; p.hidden = !OB.error; return p; };

  function stepLanguage() {
    const row = mk('div', 'ob-lang-row');
    ['en', 'ru'].forEach(code => {
      const b = btn('ob-lang', langName(code)); b.dataset.lang = code;
      b.setAttribute('aria-pressed', String(I18N.lang === code));
      b.addEventListener('click', () => window.aiVoiceApp.setLanguage(code));
      row.append(b);
    });
    return [title('onboarding.lang.title'), mk('p', 'ob-tagline', t('onboarding.lang.tagline')), row];
  }

  function stepFish() {
    const list = mk('ol', 'ob-steps');
    const s2 = mk('li'); s2.append(link('https://fish.audio/app/api-keys', t('onboarding.fish.step2')));
    list.append(mk('li', '', t('onboarding.fish.step1')), s2, mk('li', '', t('onboarding.fish.step3')));
    const form = buildKeyForm({kind: 'fish', connected: OB.keys.fish === true, idPrefix: 'ob-fish', onChange: state => {
      OB.formState = state;
      if (state === 'ok' || state === 'warning') { OB.keys = {...OB.keys, fish: true}; window.aiVoiceApp.refreshKeys(); }
      renderFooter();
    }});
    const note = mk('p', 'ob-note');
    note.append(link('https://docs.fish.audio/developer-guide/getting-started/api-key', t('onboarding.fish.help')),
      link('https://fish.audio/app/billing', t('onboarding.fish.billing')));
    return [title('onboarding.fish.title'), mk('p', 'ob-lead', t('onboarding.fish.lead')), list, form, note];
  }

  const PERMS = [
    {kind: 'microphone', name: 'onboarding.perms.microphone', reason: 'onboarding.perms.microphone_reason', href: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone'},
    {kind: 'speech', name: 'onboarding.perms.speech', reason: 'onboarding.perms.speech_reason', href: 'x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition'},
  ];
  const CHIP = {
    authorized: ['ok', 'onboarding.perms.allowed'], denied: ['bad', 'onboarding.perms.denied'], restricted: ['bad', 'onboarding.perms.denied'],
    not_determined: ['', 'onboarding.perms.not_asked'], unknown: ['', 'onboarding.perms.unknown'],
  };
  function paintPerm(spec) {
    const row = $id('ob-perm-' + spec.kind); if (!row) return;
    const state = OB.perms?.[spec.kind];
    const key = (CHIP[state] ? state : '') + (state === 'not_determined' ? ':' + !!OB.pending[spec.kind] : '');
    if (row.dataset.painted === key) return;
    row.dataset.painted = key;
    const chip = row.querySelector('.ob-chip'), action = row.querySelector('.ob-perm-action');
    chip.className = 'ob-chip'; chip.textContent = '';
    action.replaceChildren();
    if (!CHIP[state]) return;
    if (CHIP[state][0]) chip.classList.add(CHIP[state][0]);
    chip.textContent = t(CHIP[state][1]);
    if (state === 'not_determined') {
      const allow = btn('ob-allow', t('onboarding.perms.allow')); allow.disabled = !!OB.pending[spec.kind];
      allow.addEventListener('click', () => requestPerm(spec.kind));
      action.append(allow);
    } else if (state !== 'authorized') {
      action.append(link(spec.href, t('onboarding.perms.open_settings')));
    }
  }
  function applyPerms(data) {
    if (!data) return;
    OB.perms = OB.perms || {};
    PERMS.forEach(spec => {
      const state = data[spec.kind];
      if (OB.pending[spec.kind] && state === 'unknown') return; // a pending request makes polls answer unknown: keep the last state
      if (state !== undefined) OB.perms[spec.kind] = state;
    });
    PERMS.forEach(paintPerm);
  }
  async function pollPerms() {
    const started = performance.now();
    try { const data = await api('/api/permissions'); if (started >= OB.requestDoneAt) applyPerms(data); } catch {}
  }
  async function requestPerm(kind) {
    OB.pending[kind] = true; PERMS.forEach(paintPerm);
    try { const data = await api('/api/permissions/request', {kind}); OB.pending[kind] = false; OB.requestDoneAt = performance.now(); applyPerms(data); }
    catch { OB.pending[kind] = false; OB.requestDoneAt = performance.now(); PERMS.forEach(paintPerm); }
  }
  function stepPerms() {
    const nodes = [title('onboarding.perms.title')];
    PERMS.forEach(spec => {
      const row = mk('div', 'ob-perm'); row.id = 'ob-perm-' + spec.kind;
      const text = mk('div', 'ob-perm-text');
      text.append(mk('div', 'ob-perm-name', t(spec.name)), mk('p', 'ob-reason', t(spec.reason)));
      row.append(text, mk('span', 'ob-chip'), mk('div', 'ob-perm-action'));
      nodes.push(row);
    });
    PERMS.forEach(paintPerm);
    const field = mk('div', 'ob-field');
    const label = mk('label', '', t('settings.asr.speech_language')); label.htmlFor = 'ob-speech-language';
    const select = mk('select'); select.id = 'ob-speech-language';
    const speech = window.aiVoiceApp.speech();
    [...new Set(speech.languages.map(l => l.id).filter(Boolean))].sort((a, b) => displayLang(a).localeCompare(displayLang(b), I18N.lang))
      .forEach(id => { const o = mk('option', '', displayLang(id)); o.value = id; select.append(o); });
    select.value = speech.selected;
    const err = errorLine();
    select.addEventListener('change', async () => {
      try { await api('/api/preferences', {speech_language: select.value}); window.aiVoiceApp.setSpeechLanguage(select.value); err.hidden = true; }
      catch (error) { err.textContent = error.message; err.hidden = false; select.value = window.aiVoiceApp.speech().selected; }
    });
    field.append(label, select);
    nodes.push(field, err);
    return nodes;
  }

  const BUILTIN = 'AI Voice Mic', BLACKHOLE = 'BlackHole 2ch';
  let deviceError = '', driverError = '';
  async function chooseDevice(name) {
    try { await api('/api/preferences', {output_device: name}); deviceError = ''; await window.aiVoiceApp.refreshDevices(); }
    catch (error) { deviceError = error.message; }
    drawDevices();
  }
  async function installBuiltin() {
    driverError = '';
    OB.driver = {...(OB.driver || {}), job: {state: 'running', action: 'install'}};
    drawDevices();
    try {
      const status = await window.aiVoiceApp.driverAction('install');
      OB.driver = status;
      if (status.job?.state === 'done' && status.device_present) { await chooseDevice(BUILTIN); return; }
    } catch (error) {
      driverError = error.message;
      OB.driver = await window.aiVoiceApp.driverStatus().catch(() => OB.driver);
    }
    drawDevices();
  }
  function drawDevices() {
    const box = $id('ob-devices'); if (!box) return;
    const focused = document.activeElement?.id;
    const outputs = OB.devices?.outputs || [], current = apiPrefs().output_device ?? null;
    const showBuiltin = OB.driver?.available === true, installed = outputs.includes(BLACKHOLE);
    const card = (id, selected, titleText) => {
      const wrap = mk('div', 'ob-card' + (selected ? ' sel' : ''));
      const main = btn('ob-card-main', '', id); main.setAttribute('role', 'radio'); main.setAttribute('aria-checked', String(selected));
      main.append(mk('span', 'ob-card-title', titleText));
      wrap.append(main);
      return {wrap, main};
    };
    const cards = mk('div', 'ob-cards'); cards.setAttribute('role', 'radiogroup');
    if (showBuiltin) {
      const drv = OB.driver || {}, drvState = drv.job?.state || 'idle', drvRunning = drvState === 'running';
      const c = card('ob-dev-builtin', current === BUILTIN, t('onboarding.device.builtin'));
      if (drv.device_present) {
        c.main.firstChild.append(mk('span', 'ob-chip ok', t('onboarding.device.installed')));
        c.main.addEventListener('click', () => chooseDevice(BUILTIN));
        if (current === BUILTIN) c.wrap.append(mk('p', 'ob-hint', t('settings.driver.discord_restart')));
      } else {
        c.main.setAttribute('aria-disabled', 'true');
        const extra = mk('div', 'ob-card-extra');
        if (drvRunning) {
          c.wrap.setAttribute('aria-busy', 'true');
          const busy = btn('ob-btn-primary', t('settings.driver.installing'), 'ob-dev-install');
          busy.disabled = true;
          extra.append(busy);
        } else if (drv.installed) {
          extra.append(mk('p', 'ob-hint', t('settings.driver.not_loaded')));
        } else {
          const install = btn('ob-btn-primary', t('settings.driver.install'), 'ob-dev-install');
          install.addEventListener('click', installBuiltin);
          extra.append(install, mk('p', 'ob-hint', t('settings.driver.caption')));
        }
        c.wrap.append(extra);
      }
      const problem = driverError || (drvState === 'failed' ? t('settings.driver.failed') : '');
      if (problem) { const err = mk('p', 'ob-error', problem); err.setAttribute('role', 'alert'); c.wrap.append(err); }
      cards.append(c.wrap);
    }
    const bh = card('ob-dev-blackhole', installed && current === BLACKHOLE, t('onboarding.device.blackhole'));
    if (installed) {
      bh.main.firstChild.append(mk('span', 'ob-chip ok', t('onboarding.device.installed')));
      bh.main.addEventListener('click', () => chooseDevice(BLACKHOLE));
    } else {
      bh.main.setAttribute('aria-disabled', 'true');
      const extra = mk('div', 'ob-card-extra');
      const code = mk('code', '', 'brew install blackhole-2ch');
      const copy = btn('ob-copy', t('onboarding.device.copy'));
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText('brew install blackhole-2ch'); } catch {} });
      const recheck = btn('ob-recheck', t('onboarding.device.check_again'), 'ob-dev-recheck');
      recheck.addEventListener('click', async () => {
        try { await api('/api/devices/rescan', {}); } catch {}
        try { const data = await api('/api/voices'); OB.devices = data.devices || {outputs: []}; window.aiVoiceApp.refreshDevices().catch(() => {}); } catch {}
        drawDevices();
      });
      extra.append(link('https://existential.audio/blackhole/', t('onboarding.device.blackhole_link')), code, copy, recheck);
      bh.wrap.append(extra);
    }
    cards.append(bh.wrap);
    const otherSelected = current !== null && current !== BLACKHOLE && !(current === BUILTIN && showBuiltin);
    const other = card('ob-dev-other', otherSelected, t('onboarding.device.other'));
    const select = mk('select'); select.id = 'ob-dev-other-select';
    const none = mk('option', '', t('onboarding.device.choose')); none.value = ''; select.append(none);
    const names = otherSelected && !outputs.includes(current) ? [...outputs, current] : outputs;
    names.forEach(name => { const o = mk('option', '', name); o.value = name; select.append(o); });
    select.value = otherSelected ? current : '';
    select.disabled = !outputs.length;
    select.setAttribute('aria-label', t('onboarding.device.other'));
    select.addEventListener('change', () => { if (select.value) chooseDevice(select.value); });
    other.main.addEventListener('click', () => { if (select.value) chooseDevice(select.value); else select.focus(); });
    const extra = mk('div', 'ob-card-extra'); extra.append(select); other.wrap.append(extra);
    cards.append(other.wrap);
    const nodes = [cards];
    if (deviceError) { const p = mk('p', 'ob-error', deviceError); p.setAttribute('role', 'alert'); nodes.push(p); }
    if (current !== null) nodes.push(mk('p', 'ob-hint', t('onboarding.device.discord_hint', {device: current})));
    box.replaceChildren(...nodes);
    if (focused && $id(focused)) $id(focused).focus();
  }
  function stepDevice(fresh, token) {
    const box = mk('div'); box.id = 'ob-devices';
    deviceError = ''; driverError = '';
    if (fresh) api('/api/driver/status').then(data => { OB.driver = data; }, () => {})
      .then(() => { if (token === OB.token) drawDevices(); });
    if (fresh || !OB.devices) {
      api('/api/voices').then(data => { OB.devices = data.devices || {outputs: []}; }, () => { OB.devices = OB.devices || {outputs: []}; })
        .then(() => { if (token === OB.token) drawDevices(); });
    } else queueMicrotask(drawDevices);
    return [title('onboarding.device.title'), mk('p', 'ob-lead', t('onboarding.device.lead')), box];
  }

  function stepDone(token) {
    const list = mk('ul', 'ob-check');
    const err = errorLine();
    Promise.all([api('/api/keys').catch(() => null), api('/api/permissions').catch(() => null)]).then(([keys, perms]) => {
      if (token !== OB.token) return;
      if (keys) OB.keys = keys;
      if (perms) applyPerms(perms);
      const permsOk = PERMS.every(spec => OB.perms?.[spec.kind] === 'authorized');
      const items = [[true, t('onboarding.done.language'), ''],
        [OB.keys?.fish === true, t('onboarding.done.fish'), t('onboarding.done.fish_skipped')],
        [permsOk, t('onboarding.done.perms'), t('onboarding.done.perms_skipped')],
        [apiPrefs().output_device != null, t('onboarding.done.device'), t('onboarding.done.device_skipped')]];
      list.replaceChildren(...items.map(([ok, label, skipped]) => {
        const li = mk('li');
        li.append(mk('span', ok ? 'ob-tick ok' : 'ob-tick', ok ? '✓' : '–'), mk('span', '', label));
        if (!ok) li.append(mk('span', 'ob-skipped', skipped));
        return li;
      }));
    });
    return [title('onboarding.done.title'), list, mk('p', 'ob-disclaimer', t('onboarding.done.disclaimer')), err];
  }

  // ---- Navigation ----
  const focusKey = node => !node ? '' : node.id ? '#' + node.id : node.dataset?.lang ? '[data-lang="' + node.dataset.lang + '"]' : '';
  function stopPoll() { clearInterval(OB.pollTimer); OB.pollTimer = null; }
  function renderStep(fresh) {
    const holder = $id('ob-step');
    const before = fresh ? '' : focusKey(document.activeElement);
    const token = ++OB.token;
    stopPoll();
    OB.error = '';
    const builders = {1: stepLanguage, 2: stepFish, 3: stepPerms, 4: () => stepDevice(fresh, token), 5: () => stepDone(token)};
    holder.replaceChildren(...builders[OB.step]());
    renderFooter();
    if (OB.step === 3) { applyPerms(OB.perms); pollPerms(); OB.pollTimer = setInterval(pollPerms, 2000); }
    if (fresh && !reduced()) holder.animate([{opacity: 0, transform: 'translateY(8px)'}, {opacity: 1, transform: 'none'}], {duration: 180, easing: 'ease-out'});
    const target = before && holder.querySelector(before);
    (target || $id('ob-title')).focus();
  }
  function go(step) { OB.step = Math.min(5, Math.max(1, step)); renderStep(true); }
  async function finish() {
    const next = $id('ob-next'); next.disabled = true;
    try {
      await api('/api/preferences', {onboarding_completed: true});
      hide();
      window.aiVoiceApp.refreshKeys();
    } catch (error) {
      const err = $id('ob-step').querySelector('.ob-error');
      if (err) { err.textContent = error.message; err.hidden = false; }
      next.disabled = false;
    }
  }
  async function show(step = 1) {
    const token = ++OB.token;
    const [keys, driver] = await Promise.all([api('/api/keys').catch(() => null), api('/api/driver/status').catch(() => null)]);
    if (token !== OB.token) return;
    OB.keys = keys || {fish: false, hf: false};
    OB.driver = driver || {available: false};
    OB.step = Math.min(5, Math.max(1, step));
    const overlay = $id('onboarding');
    clearTimeout(OB.hideTimer);
    overlay.classList.remove('ob-fading');
    overlay.hidden = false;
    bgStart();
    renderStep(true);
  }
  function hide() {
    const overlay = $id('onboarding');
    stopPoll(); bgStop(); OB.token++;
    const done = () => { overlay.hidden = true; overlay.classList.remove('ob-fading'); };
    if (reduced()) done(); else { overlay.classList.add('ob-fading'); OB.hideTimer = setTimeout(done, 200); }
  }
  function rerender() { if (isOpen()) renderStep(false); }

  $id('ob-back').addEventListener('click', () => go(OB.step - 1));
  $id('ob-skip').addEventListener('click', () => go(OB.step + 1));
  $id('ob-next').addEventListener('click', () => { if (OB.step === 5) finish(); else go(OB.step + 1); });
  window.addEventListener('focus', () => { if (isOpen() && OB.step === 3) pollPerms(); });
  document.addEventListener('keydown', event => {
    if (!isOpen()) return;
    if (event.key === 'Escape') { event.preventDefault(); return; }
    if (event.key !== 'Tab') return;
    const items = [...$id('ob-glass').querySelectorAll('button,a[href],input,select,[tabindex="0"]')]
      .filter(n => !n.disabled && n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden');
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1], inside = items.includes(document.activeElement);
    if (event.shiftKey && (document.activeElement === first || !inside)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !inside)) { event.preventDefault(); first.focus(); }
  });
  window.aiVoiceOnboarding = {show, hide, rerender, isOpen, frames: () => OB.frames};
})();
