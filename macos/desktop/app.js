const I18N = {lang: 'en', dict: {}, fallback: {}};
let APP_PLATFORM = 'mac';
// JS -> native bridge: WKWebView handlers on macOS, pywebview js_api on Windows.
function nativePost(name, payload) {
  try {
    const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers[name];
    if (handler) handler.postMessage(payload);
    else if (window.pywebview && window.pywebview.api && window.pywebview.api[name]) window.pywebview.api[name](payload);
  } catch {}
}
window.nativePost = nativePost;
async function loadLocale(lang) {
  const selected = lang === 'ru' ? 'ru' : 'en';
  const fetchDict = async code => {
    try {
      const dict = await api('/locales/' + code + '.json');
      return dict && typeof dict === 'object' && !Array.isArray(dict) ? dict : {};
    } catch { return {}; }
  };
  const [dict, fallback] = await Promise.all([fetchDict(selected), fetchDict('en')]);
  I18N.lang = selected;
  I18N.dict = dict;
  I18N.fallback = fallback;
  document.documentElement.lang = selected;
}
function t(key, vars = {}) {
  const winKey = key + '_win';
  const resolved = APP_PLATFORM === 'win' && (I18N.dict[winKey] !== undefined || I18N.fallback[winKey] !== undefined) ? winKey : key;
  const value = I18N.dict[resolved] ?? I18N.fallback[resolved] ?? resolved;
  return value.replace(/\{([a-z_][a-z0-9_]*)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match);
}
function tp(key, n, vars = {}) {
  return t(key + '_' + new Intl.PluralRules(I18N.lang).select(n), {...vars, n});
}
function applyI18n(root = document) {
  const attrs = {'data-i18n':null, 'data-i18n-title':'title', 'data-i18n-placeholder':'placeholder', 'data-i18n-aria':'aria-label'};
  for (const [source, target] of Object.entries(attrs)) {
    const nodes = [...root.querySelectorAll('[' + source + ']')];
    if (root.matches?.('[' + source + ']')) nodes.unshift(root);
    nodes.forEach(node => {
      const value = t(node.getAttribute(source));
      if (target) node.setAttribute(target, value); else node.textContent = value;
    });
  }
}
function fmtLocale() { return I18N.lang === 'ru' ? 'ru-RU' : 'en-US'; }
function langName(code) {
  try {
    const name = new Intl.DisplayNames([code], {type:'language'}).of(code);
    return name ? name.charAt(0).toLocaleUpperCase(code) + name.slice(1) : code;
  } catch { return code; }
}
function displayLang(code) {
  try {
    const name = new Intl.DisplayNames([I18N.lang], {type:'language'}).of(code);
    return name ? name.charAt(0).toLocaleUpperCase(I18N.lang) + name.slice(1) : code;
  } catch { return code; }
}
async function api(url, body) {
  const response = await fetch(url, {method: body ? 'POST' : 'GET', headers: {'X-AI-Voice-Token': APP_TOKEN, ...(body ? {'Content-Type':'application/json'} : {})}, ...(body ? {body: JSON.stringify(body)} : {})});
  let data;
  try { data = await response.json(); }
  catch { throw new Error(t('errors.audio_connection')); }
  if (!response.ok) throw new Error(data.error || t('errors.command_failed'));
  return data;
}
// Shared Fish / Hugging Face key form (onboarding step 2 and Settings -> API keys). The typed value
// only lives in the input; it is cleared as soon as the request returns.
function buildKeyForm({kind, connected, idPrefix, onChange, onCancel}) {
  const mk = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const btn = (cls, text) => { const b = mk('button', cls, text); b.type = 'button'; return b; };
  const ERRORS = {invalid: kind === 'fish' ? 'onboarding.fish.error_invalid' : 'settings.keys_api.hf_error_invalid',
    format: 'onboarding.fish.error_format', network: 'onboarding.fish.error_network', keychain: 'settings.keys_api.error_keychain'};
  const root = mk('div', 'key-form');
  root.dataset.kind = kind;
  const done = mk('div', 'key-connected');
  const replace = btn('key-link key-replace', t('settings.keys_api.replace'));
  done.append(mk('span', 'key-chip ok', t('settings.keys_api.connected')), replace);
  const fields = mk('div', 'key-fields');
  const row = mk('div', 'key-input-row');
  const input = mk('input', 'key-input');
  input.type = 'password'; input.id = idPrefix + '-key-input'; input.maxLength = 200;
  input.autocomplete = 'off'; input.spellcheck = false;
  input.placeholder = t(kind === 'fish' ? 'onboarding.fish.placeholder' : 'settings.keys_api.hf_placeholder');
  const paste = btn('key-paste', t('onboarding.fish.paste'));
  const eye = btn('key-eye'); eye.setAttribute('aria-pressed', 'false'); eye.setAttribute('aria-label', t('onboarding.fish.show_key'));
  eye.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8Z"/><circle cx="8" cy="8" r="2"/></svg>';
  const verify = btn('key-verify', t('onboarding.fish.verify'));
  const msg = mk('p', 'key-msg'); msg.setAttribute('role', 'status');
  row.append(input, paste, eye);
  const actions = mk('div', 'key-actions');
  actions.append(verify);
  if (onCancel) {
    const cancel = btn('key-paste key-cancel', t('common.cancel'));
    cancel.addEventListener('click', () => { input.value = ''; setState(connected ? 'connected' : 'idle'); onCancel(); });
    actions.append(cancel);
  }
  fields.append(row, actions, msg);
  root.append(done, fields);
  function setState(state, text) {
    root.dataset.state = state;
    done.hidden = state !== 'connected';
    fields.hidden = state === 'connected';
    const busy = state === 'verifying';
    [input, paste, eye, verify].forEach(n => { n.disabled = busy; });
    verify.querySelector('.key-spinner')?.remove();
    if (busy) { const sp = mk('span', 'key-spinner'); sp.setAttribute('aria-hidden', 'true'); verify.prepend(sp); }
    msg.textContent = text || '';
    if (onChange) onChange(state);
  }
  async function doVerify() {
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    setState('verifying');
    let result;
    try { result = await api('/api/keys/' + kind, kind === 'fish' ? {key: value} : {token: value}); }
    catch { result = {ok: false, error: 'network'}; }
    finally { input.value = ''; }
    if (result.ok && result.warning === 'no_credit') setState('warning', t('onboarding.fish.no_credit'));
    else if (result.ok) setState('ok', t('settings.keys_api.connected'));
    else setState('error', t(ERRORS[result.error] || ERRORS.network));
    if (!result.ok) input.focus();
  }
  verify.addEventListener('click', doVerify);
  input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); doVerify(); } });
  eye.addEventListener('click', () => {
    const shown = input.type === 'password';
    input.type = shown ? 'text' : 'password';
    eye.setAttribute('aria-pressed', String(shown));
  });
  paste.addEventListener('click', async () => {
    try { input.value = await navigator.clipboard.readText(); input.focus(); }
    catch { input.focus(); msg.textContent = t('onboarding.fish.paste_hint'); }
  });
  replace.addEventListener('click', () => { setState('idle'); input.focus(); });
  setState(connected ? 'connected' : 'idle');
  return root;
}
(function () {
  let speechLanguages = [], speechLanguage = null, speechRestartPending = false;
  let languageBusy = false;
  async function setLanguage(lang) {
    if (languageBusy) return;
    languageBusy = true;
    $('#ui-language-select').disabled = true;
    try {
      const prefs = await api('/api/preferences', {language:lang});
      prefsState.language = prefs.language;
      await loadLocale(lang);
      applyI18n();
      rerenderAll();
      nativePost('language', lang);
    } catch (error) { showToast(error.message); }
    finally { languageBusy = false; $('#ui-language-select').disabled = false; $('#ui-language-select').value = I18N.lang; }
  }
  function rerenderAll() {
    voiceCardKey = '';
    renderList();
    vcRenderList();
    hfRender();
    renderMode();
    // Keep device controls and their listeners; rebuild dynamic settings copy only.
    const normalizeRow = $('#normalize-toggle')?.closest('.audio-row');
    if (normalizeRow) normalizeRow.replaceWith(buildNormalizeRow());
    const monitorRow = $('#monitor-card');
    if (monitorRow) monitorRow.replaceWith(buildMonitorRow());
    const textVoiceRow = $('#vc-text-voice')?.closest('.audio-row');
    if (textVoiceRow) textVoiceRow.replaceWith(vcBuildTextVoiceRow());
    $('#asr-engine-card')?.replaceWith(buildEngineRow());
    $('#keys-settings').replaceChildren(buildSwapRows());
    buildApiKeysPane();
    refillDevices();
    $('#usage-settings').replaceChildren(); buildUsageSettings();
    syncLanguageControls();
    fillMonitors($('#monitor-device'), lastMonitors);
    vcFileList('import'); vcFileList('audio');
    if (vcSheetError.translate) $('#vc-sheet-error').textContent = vcSheetError.translate();
    ['#vc-import-submit', '#vc-audio-submit'].forEach(id => {
      if ($(id).disabled) $(id).textContent = t('common.loading');
    });
    const uploadLabel = $('#vc-upload-progress span');
    if (uploadLabel.dataset.percent) uploadLabel.textContent = t('vc.upload_percent', {n:uploadLabel.dataset.percent});
    if (hfDetailCard) hfRefreshDetailCopy();
    if (showToast.translate) toast.textContent = showToast.translate();
    if (storageData) renderStorage();
    if (driverLastStatus) renderDriverRow(driverLastStatus);
    displayStatus(audioState);
    renderUpdateCopy();
    window.aiVoiceOnboarding?.rerender();
  }
  let commandBusy = false;
  let audioState = {active:false,state:'stopped',mode:'mic', transcript_partial:'', transcript_final:'', monitor_active:false, monitor_errors:0, monitor_error:'', latency_seconds:null, generation_seq:0};
  let lastMonitors = [];
  const ICON_PLAY = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 3.2v9.6c0 .45.52.71.88.43l7.2-4.8a.58.58 0 0 0 0-.96l-7.2-4.8A.58.58 0 0 0 4 3.2Z"/></svg>';
  const ICON_STOP = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.2"/></svg>';
  // Native fader is vertical: higher value = higher on screen. Cap top (px-safe) for a 30px cap.
  function capTopCss(db, min, max) {
    const f = Math.max(0, Math.min(1, (db - min) / (max - min)));
    return 'calc((100% - 30px) * ' + (1 - f).toFixed(5) + ')';
  }
  let lastPreviewState = 'idle';
  function syncPreviewFromStatus(data) {
    const st = data && data.preview_state;
    if (typeof st !== 'string') return;
    if (st === 'loading' || st === 'active') {
      const id = data.clickedid || data.preview_id;
      document.querySelectorAll('.preview-btn').forEach(b => {
        const on = b.dataset.previewId === id;
        if (on !== (b.dataset.state === 'active')) setPreviewVisual(b, on ? 'active' : '', false, '');
      });
    } else if (lastPreviewState === 'loading' || lastPreviewState === 'active') {
      setAllPreviewIdle();
      if (st === 'error') showTranslatedToast(() => t('errors.voice_demo_failed'));
    }
    lastPreviewState = st;
  }
  function vcLoading(data) { return !!(data && data.mode === 'vc' && data.vc && data.vc.state === 'loading'); }
  function displayStatus(data) {
    audioState = data;
    syncPreviewFromStatus(data);
    document.querySelector('#status-line').textContent = data.message;
    document.body.classList.toggle('is-running',data.active);
    const stopBtn = document.querySelector('#stop-btn');
    stopBtn.hidden = !(mode === 'text' && data.active && data.mode === 'text');
    stopBtn.disabled = !data.active;
    document.querySelector('#start-btn').disabled = commandBusy || vcLoading(data);
    ['#input-dev','#output-dev'].forEach(id => document.querySelector(id).disabled=data.active);
    motionStartLabel(mode === 'vc' ? (data.active && data.mode === 'vc' ? t('common.stop') : t('common.start')) : mode === 'text' ? t('main.speak_text') : (data.active && data.mode === 'mic' ? t('main.stop_microphone') : t('main.start_microphone')));
    updateTranscriptUI(data);
    updateMonitorUI(data);
    updateVoiceCard();
    updateStatusPill(data);
    updateSwapPill(data);
    updateRouteChip();
    renderHistory(data.history);
    renderUsage(data);
    syncGainUI();
    syncMonitorUI();
    syncInputGainUI();
    updateLatencyReadout(data);
    vcOnStatus(data);
    syncSpeechLanguageHint();
  }
  async function command(data) {
    if(commandBusy) return;
    commandBusy=true;
    document.querySelector('#start-btn').disabled=true;
    try { const state=await api('/api/control',data);displayStatus(state);return state; }
    catch(error){showToast(error.message);document.querySelector('#status-line').textContent=error.message;throw error;}
    finally{commandBusy=false;document.querySelector('#start-btn').disabled=vcLoading(audioState);}
  }
  const $ = s => document.querySelector(s);
  // Motion is presentation-only. Timers are cancellable, and polls compare values
  // before adding classes; reduced motion also skips the JS animation lifecycle.
  const motionMedia = window.matchMedia('(prefers-reduced-motion: reduce)');
  const avatarLoaded = new Set();
  const motionJobs = new WeakMap();
  function motionCancel(node) {
    const cancel = motionJobs.get(node);
    if (cancel) cancel();
  }
  function motionFinish(node, duration, done) {
    motionCancel(node);
    let timer;
    const cancel = () => {
      clearTimeout(timer); node.removeEventListener('animationend', end);
      motionJobs.delete(node);
    };
    const finish = () => { cancel(); done(); };
    const end = event => { if (event.target === node) finish(); };
    node.addEventListener('animationend', end);
    timer = setTimeout(finish, duration + 40);
    motionJobs.set(node, cancel);
  }
  function motionPulse(node, cls = 'motion-badge', delay = 0) {
    if (!node || motionMedia.matches) return;
    motionCancel(node);
    node.classList.remove(cls);
    node.style.setProperty('--motion-delay', delay + 'ms');
    // A frame boundary resets a previous pulse without a forced layout read.
    const frame = requestAnimationFrame(() => {
      motionJobs.delete(node);
      if (!node.isConnected || motionMedia.matches) return;
      node.classList.add(cls);
      motionFinish(node, 200 + delay, () => node.classList.remove(cls));
    });
    motionJobs.set(node, () => { cancelAnimationFrame(frame); motionJobs.delete(node); });
  }
  function motionText(node, text, cls = 'motion-badge') {
    if (!node || node.textContent === text) return;
    node.textContent = text;
    motionPulse(node, cls);
  }
  function motionSheet(node, open) {
    if (open && !node.hidden && !node.classList.contains('motion-sheet-close')) return;
    if (!open && (node.hidden || node.classList.contains('motion-sheet-close'))) return;
    motionCancel(node);
    node.classList.remove('motion-sheet-open', 'motion-sheet-close');
    node.inert = !open;
    if (open) node.removeAttribute('aria-hidden'); else node.setAttribute('aria-hidden', 'true');
    if (motionMedia.matches) { node.hidden = !open; return; }
    node.hidden = false;
    const cls = open ? 'motion-sheet-open' : 'motion-sheet-close';
    node.classList.add(cls);
    motionFinish(node, open ? 280 : 160, () => {
      node.classList.remove(cls);
      node.hidden = !open;
    });
  }
  function motionInert(node) {
    node.inert = true; node.setAttribute('aria-hidden', 'true');
    [node, ...node.querySelectorAll('*')].forEach(child => {
      motionCancel(child);
      child.removeAttribute('id'); child.removeAttribute('autofocus');
      // Exiting snapshots must not participate in keyed lookups or form labels.
      child.removeAttribute('data-id'); child.removeAttribute('for');
    });
    node.querySelectorAll('[class*="motion-"]').forEach(child => {
      [...child.classList].filter(c => c.startsWith('motion-')).forEach(c => child.classList.remove(c));
    });
  }
  function motionCrossfade(node, panel = false) {
    const parent = node.parentElement;
    parent.querySelectorAll(':scope > .motion-ghost').forEach(old => { motionCancel(old); old.remove(); });
    if (motionMedia.matches || !node.getClientRects().length || !node.childNodes.length) return () => {};
    const rect = node.getBoundingClientRect(), bounds = parent.getBoundingClientRect();
    const ghost = node.cloneNode(true);
    motionInert(ghost);
    ghost.classList.remove('motion-panel-in', 'motion-content-in');
    ghost.classList.add('motion-ghost', panel ? 'motion-panel-out' : 'motion-content-out');
    ghost.style.width = rect.width + 'px';
    ghost.style.left = (rect.left - bounds.left + parent.scrollLeft) + 'px';
    ghost.style.top = (rect.top - bounds.top + parent.scrollTop) + 'px';
    return next => {
      parent.append(ghost);
      motionFinish(ghost, panel ? 140 : 120, () => ghost.remove());
      const incoming = next || node, cls = panel ? 'motion-panel-in' : 'motion-content-in';
      motionCancel(incoming); incoming.classList.add(cls);
      motionFinish(incoming, panel ? 320 : 240, () => incoming.classList.remove(cls));
    };
  }
  function motionStartLabel(text) {
    const button = $('#start-btn');
    if (button.textContent === text) return;
    motionCancel(button);
    button.replaceChildren(el('span', 'motion-button-label', text));
    button.setAttribute('aria-label', text);
    button.classList.remove('motion-button-change');
    if (motionMedia.matches) return;
    button.classList.add('motion-button-change');
    motionFinish(button, 180, () => { button.classList.remove('motion-button-change'); });
  }
  function motionProgress(node, fraction) {
    if (!node) return;
    const value = 'scaleX(' + Math.max(0, Math.min(1, Number(fraction) || 0)) + ')';
    if (node.style.transform !== value) node.style.transform = value;
  }
  function motionRows(target, liveIds, scope = '') {
    const sameScope = target._motionScope === scope;
    target._motionScope = scope;
    const previous = new Map(sameScope ? Array.from(target.children).filter(n => n.dataset.id).map(n => [n.dataset.id, n]) : []);
    const exits = sameScope ? Array.from(target.querySelectorAll(':scope > .motion-row-exit')) : [];
    return () => {
      const current = new Set(); let added = 0;
      Array.from(target.children).forEach(row => {
        const id = row.dataset.id; if (!id) return;
        current.add(id);
        const old = previous.get(id);
        if (!old) motionPulse(row, 'motion-row-in', added < 10 ? added++ * 16 : 0);
        else {
          ['.vc-badge', '.star', '.event-add', '.hf-download'].forEach(selector => {
            const before = old.querySelector(selector), after = row.querySelector(selector);
            if (before && after && before.textContent !== after.textContent && !after.classList.contains('vc-ring')) motionPulse(after);
          });
        }
      });
      if (motionMedia.matches) return;
      exits.forEach(exit => target.append(exit));
      if (!liveIds) return;
      let index = 0;
      previous.forEach((row, id) => {
        if (!current.has(id) && !liveIds.has(id)) {
          motionCancel(row); row.classList.remove('motion-row-in'); motionInert(row);
          const wrap = el('div', 'motion-row-exit'), clip = el('div', 'motion-row-clip');
          wrap.inert = true; wrap.setAttribute('aria-hidden', 'true'); clip.append(row); wrap.append(clip);
          target.insertBefore(wrap, target.children[index] || null);
          motionFinish(wrap, 200, () => wrap.remove());
        }
        index++;
      });
    };
  }
  const list = $('#voice-list'), toast = $('#toast'), panel = $('#mode-panel');
  const input = $('#search-input'), favButton = $('#fav-filter');
  const STORE_FAV = 'avr_favs_v1', STORE_LOCAL = 'avr_local_v1';
  const validId = id => typeof id === 'string' && /^[a-f0-9]{32}$/i.test(id);
  function readStore(key) { try { const v = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(v) ? v : []; } catch { return []; } }
  function save(key,data,onFail){ if(key===STORE_FAV)api('/api/preferences',{favorite_ids:data}).catch(error=>{showToast(error.message);if(onFail)onFail();}); }
  function normalize(v) { return v && validId(v.id) && typeof v.name === 'string' && v.name.trim() ? {id:v.id.toLowerCase(), name:v.name.trim(), language: typeof v.language === 'string' && /^[a-z]{2,3}$/.test(v.language) ? v.language : null, source:v.source === 'local' ? 'local' : 'fish', url:'https://fish.audio/m/'+v.id.toLowerCase()+'/', avatar_url: typeof v.avatar_url === 'string' ? v.avatar_url : null, tags: Array.isArray(v.tags) ? v.tags : [], task_count: typeof v.task_count === 'number' ? v.task_count : null} : null; }
  const voiceSourceMeta = v => (v.language ? displayLang(v.language) + ' · ' : '') + 'Fish Audio';
  const clean = values => (Array.isArray(values) ? values : []).map(normalize).filter(Boolean);
  const unique = values => [...new Map(values.map(v => [v.id, v])).values()];
  const favs = new Set();
  let saved = [], voices = [], selected = null;
  let ttsSelect = {on:false, ids:new Set()};
  let vcSelect = {on:false, ids:new Set()};
  let tab = 'mine', mode = 'mic', onlyFavs = false, text = '';
  let queries = {mine:'',catalog:''}, results = [], page = 1, hasMore = false, busy = false, searchError = '', generation = 0;
  const filters = {gender:'all', sort_by:'task_count'};
  let deviceInfo = {inputs:[], outputs:[], default_input:null, default_output:null, virtual:[]};
  let keyState = {fish:null, hf:null};
  let prefsState = {output_gain_db:0, normalize_loudness:true, voice_id:null, input_device:null, output_device:null, monitor_enabled:false, monitor_device:null, monitor_gain_db:0, input_gain_db:0, vc_gate_enabled:false, vc_gate_db:-45, update_auto:true};
  // Preview state — row-keyed (id + tab), so concurrent previews of different rows don't clobber.
  const previewState = new Map();
  function setPreviewVisual(btn, state, busy, errMsg) {
    if (!btn) return;
    btn.dataset.state = state;
    btn.dataset.busy = busy ? 'true' : 'false';
    btn.setAttribute('aria-pressed', state === 'active' ? 'true' : 'false');
    if (state === 'active') {
      btn.innerHTML = ICON_STOP;
      btn.setAttribute('aria-label', (btn.dataset.label || t('common.listen')) + " " + t('common.stop_suffix'));
    } else {
      btn.innerHTML = ICON_PLAY;
      btn.setAttribute('aria-label', btn.dataset.label || t('common.listen'));
    }
    if (state === 'error' && errMsg) btn.title = errMsg;
  }
  function previewKey(btn) {
    const id = btn && btn.dataset && btn.dataset.previewId;
    return id ? id : null;
  }
  function setAllPreviewIdle() {
    document.querySelectorAll('.preview-btn').forEach(b => {
      b.dataset.state = '';
      b.dataset.busy = 'false';
      b.innerHTML = ICON_PLAY;
      b.setAttribute('aria-label', b.dataset.label || t('common.listen'));
    });
  }
  function setPreviewActive(btn, activeId) {
    document.querySelectorAll('.preview-btn').forEach(b => {
      if (b.dataset.previewId === activeId) {
        setPreviewVisual(b, 'active', false, '');
      } else {
        setPreviewVisual(b, '', false, '');
      }
    });
  }
  async function apiPreview(action, id) {
    try {
      const data = await api('/api/preview', {action, id});
      const state = data && data.preview_state;
      const err = data && data.preview_error;
      return {state: state || 'idle', error: err || '', preview_id: data && data.preview_id || null};
    } catch (e) {
      return {state: 'error', error: e.message || String(e), preview_id: null};
    }
  }

  // Generic gain helper — vertical fader DOM state and save-queue wiring.
  // identity: stable element id used to re-find DOM across re-render.
  // latestKey / confirmedKey: keys in audioQueue used to read latest/confirmed DB.
  // prefsKey: backend key name posted to /api/preferences.
  // resetDb: default value used by the reset button.
  function attachVerticalFader(spec) {
    const track = document.getElementById(spec.trackId);
    const input = document.getElementById(spec.inputId);
    const cap = document.getElementById(spec.capId);
    const readout = document.getElementById(spec.readoutId);
    const reset = document.getElementById(spec.resetId);
    const detent = document.getElementById(spec.detentId);
    if (!track || !input || !cap || !readout) return null;
    const min = Number(input.min);
    const max = Number(input.max);
    function clamp(v) { return Math.max(min, Math.min(max, v)); }
    function capPct(db) {
      const p = (db - min) / (max - min);
      return Math.max(0, Math.min(1, p)) * 100;
    }
    function setVisual(db) {
      input.value = String(db);
      cap.style.setProperty('--cap-top', capTopCss(db, min, max));
      readout.textContent = formatGain(db);
      const cell = track.parentElement;
      if (cell) cell.dataset.detent = (db === spec.detentValue) ? 'true' : 'false';
      if (detent) detent.style.top = 'calc(' + capTopCss(spec.detentValue, min, max) + ' + 15px)';
    }
    function getValue() { return clamp(Math.round(Number(input.value) || 0)); }
    let timer = null;
    let lastSent = NaN;
    input.addEventListener('input', () => {
      const db = getValue();
      setVisual(db);
      audioQueue[spec.latestKey] = db;
      // Visual optimistic sync of --gain-pct / --input-pct for any CSS that still references it.
      const p = capPct(db);
      input.style.setProperty('--gain-pct', p.toFixed(2) + '%');
      input.style.setProperty('--input-pct', p.toFixed(2) + '%');
      input.setAttribute('aria-valuenow', String(db));
      if (spec.onInput) spec.onInput(db);
      // Latest-wins coalesce: send via shared audioQueue.
      audioQueue.enqueueGeneric(spec.prefsKey, db);
      lastSent = db;
    });
    input.addEventListener('keydown', event => {
      const step = 1;
      const big = 3;
      let v = getValue();
      let handled = true;
      if (event.key === 'ArrowUp') v = clamp(v + step);
      else if (event.key === 'ArrowDown') v = clamp(v - step);
      else if (event.key === 'PageUp') v = clamp(v + big);
      else if (event.key === 'PageDown') v = clamp(v - big);
      else if (event.key === 'Home') v = min;
      else if (event.key === 'End') v = max;
      else handled = false;
      if (handled) {
        event.preventDefault();
        input.value = String(v);
        input.dispatchEvent(new Event('input', {bubbles: true}));
      }
    });
    if (reset) {
      reset.addEventListener('click', () => {
        input.value = String(spec.resetDb);
        input.dispatchEvent(new Event('input', {bubbles: true}));
      });
    }
    // Pointer fallback for environments where vertical-lr native mapping is broken:
    // We still rely on the native vertical input for primary drag; this only adds
    // keyboard handling and the reset.
    return {setVisual, getValue, min, max};
  }
  const audioQueue = {
    timer:null, inflight:false, pending:{}, revisions:{output_gain_db:0, normalize_loudness:0, monitor_enabled:0, monitor_device:0, monitor_gain_db:0, input_gain_db:0},
    confirmed:{gain:0, norm:true, monEnabled:false, monDevice:null, monGain:0, inputGain:0},
    gainLatest:0, normLatest:true, monEnabledLatest:false, monDeviceLatest:null, monGainLatest:0, inputGainLatest:0,
    _latest: {}, _confirmed: {},
    setGain(db) { this.gainLatest=db; this.enqueue('output_gain_db',db); syncGainUI(); },
    setNorm(value) { this.normLatest=value; this.enqueue('normalize_loudness',value); syncNormUI(); },
    setMonitorEnabled(value) { this.monEnabledLatest=!!value; this.enqueue('monitor_enabled',this.monEnabledLatest); syncMonitorUI(); },
    setMonitorDevice(value) { this.monDeviceLatest = value || null; this.enqueue('monitor_device',this.monDeviceLatest); syncMonitorUI(); },
    setMonitorGain(db) { this.monGainLatest=db; this.enqueue('monitor_gain_db',db); syncMonitorUI(); },
    setInputGain(db) { this.inputGainLatest=db; this.enqueue('input_gain_db',db); syncInputGainUI(); },
    setLatest(key, value) {
      if (key === 'output_gain_db') this.gainLatest = value;
      else if (key === 'normalize_loudness') this.normLatest = value;
      else if (key === 'monitor_enabled') this.monEnabledLatest = !!value;
      else if (key === 'monitor_device') this.monDeviceLatest = value || null;
      else if (key === 'monitor_gain_db') this.monGainLatest = value;
      else if (key === 'input_gain_db') this.inputGainLatest = value;
    },
    enqueueGeneric(key, value) {
      const map = {output_gain_db:'output_gain_db', normalize_loudness:'normalize_loudness', monitor_enabled:'monitor_enabled', monitor_device:'monitor_device', monitor_gain_db:'monitor_gain_db', input_gain_db:'input_gain_db'};
      const k = map[key] || key;
      this.enqueue(k, value);
    },
    enqueue(key,value) {
      this.pending[key]=value; this.revisions[key]=(this.revisions[key]||0)+1;
      clearTimeout(this.timer); this.timer=setTimeout(()=>this.flush(),120);
    },
    async flush() {
      clearTimeout(this.timer); this.timer=null;
      if(this.inflight || !Object.keys(this.pending).length) return;
      const batch=this.pending, revision={...this.revisions}; this.pending={}; this.inflight=true;
      try {
        const prefs=await api('/api/preferences',batch);
        prefsState={...prefsState,...prefs};
        if('output_gain_db' in prefs) this.confirmed.gain=prefs.output_gain_db;
        if('normalize_loudness' in prefs) this.confirmed.norm=!!prefs.normalize_loudness;
        if('monitor_enabled' in prefs) this.confirmed.monEnabled=!!prefs.monitor_enabled;
        if('monitor_device' in prefs) this.confirmed.monDevice = prefs.monitor_device || null;
        if('monitor_gain_db' in prefs) this.confirmed.monGain = prefs.monitor_gain_db;
        if('input_gain_db' in prefs) this.confirmed.inputGain=prefs.input_gain_db;
        if('output_gain_db' in batch && revision.output_gain_db===this.revisions.output_gain_db) this.gainLatest=this.confirmed.gain;
        if('normalize_loudness' in batch && revision.normalize_loudness===this.revisions.normalize_loudness) this.normLatest=this.confirmed.norm;
        if('monitor_enabled' in batch && revision.monitor_enabled===this.revisions.monitor_enabled) this.monEnabledLatest=this.confirmed.monEnabled;
        if('monitor_device' in batch && revision.monitor_device===this.revisions.monitor_device) this.monDeviceLatest=this.confirmed.monDevice;
        if('monitor_gain_db' in batch && revision.monitor_gain_db===this.revisions.monitor_gain_db) this.monGainLatest=this.confirmed.monGain;
        if('input_gain_db' in batch && revision.input_gain_db===this.revisions.input_gain_db) this.inputGainLatest=this.confirmed.inputGain;
      } catch(error) {
        if('output_gain_db' in batch && revision.output_gain_db===this.revisions.output_gain_db) this.gainLatest=this.confirmed.gain;
        if('normalize_loudness' in batch && revision.normalize_loudness===this.revisions.normalize_loudness) this.normLatest=this.confirmed.norm;
        if('monitor_enabled' in batch && revision.monitor_enabled===this.revisions.monitor_enabled) this.monEnabledLatest=this.confirmed.monEnabled;
        if('monitor_device' in batch && revision.monitor_device===this.revisions.monitor_device) this.monDeviceLatest=this.confirmed.monDevice;
        if('monitor_gain_db' in batch && revision.monitor_gain_db===this.revisions.monitor_gain_db) this.monGainLatest=this.confirmed.monGain;
        if('input_gain_db' in batch && revision.input_gain_db===this.revisions.input_gain_db) this.inputGainLatest=this.confirmed.inputGain;
        showToast(error.message);
      } finally {
        this.inflight=false; syncGainUI(); syncNormUI(); syncMonitorUI(); syncInputGainUI();
        if(Object.keys(this.pending).length) this.flush();
      }
    }
  };
  function gainPct(db, min=-24, max=12){
    const low = Number.isFinite(Number(min)) ? Number(min) : -24;
    const high = Number.isFinite(Number(max)) ? Number(max) : 12;
    return Math.max(0, Math.min(100, ((Number(db) - low) / (high - low)) * 100));
  }
  function syncGainUI() {
    const slider = document.getElementById('output-gain');
    const value = document.getElementById('gain-value');
    const cap = document.getElementById('output-cap');
    const v = audioQueue.gainLatest;
    const vcSlider = document.getElementById('vc-output-gain');
    const vcValue = document.getElementById('vc-output-gain-value');
    if (vcSlider && vcValue && !vcGainDragging) {
      vcSlider.value = String(v);
      vcValue.textContent = formatGain(v).replace('-', '−');
      vcSlider.setAttribute('aria-valuenow', String(v));
    }
    if (!slider || !value) return;
    slider.value = String(v);
    value.textContent = formatGain(v);
    slider.setAttribute('aria-valuenow', String(v));
    slider.style.setProperty('--gain-pct', gainPct(v, slider.min, slider.max).toFixed(2) + '%');
    slider.style.setProperty('--db', String(v));
    if (cap) {
      const min = Number(slider.min), max = Number(slider.max);
      cap.style.setProperty('--cap-top', capTopCss(v, min, max));
      const cell = slider.closest('.fader-cell');
      if (cell) cell.dataset.detent = (v === 0) ? 'true' : 'false';
    }
  }
  function syncNormUI() {
    const cb = document.getElementById('normalize-toggle');
    if (!cb) return;
    cb.checked = audioQueue.normLatest;
    cb.setAttribute('aria-checked', String(cb.checked));
  }
  function syncInputGainUI() {
    const slider = document.getElementById('input-gain');
    const value = document.getElementById('input-gain-value');
    const cap = document.getElementById('input-cap');
    if (!slider || !value) return;
    const v = audioQueue.inputGainLatest;
    slider.value = String(v);
    value.textContent = formatGain(v);
    slider.setAttribute('aria-valuenow', String(v));
    slider.style.setProperty('--gain-pct', gainPct(v, slider.min, slider.max).toFixed(2) + '%');
    slider.style.setProperty('--input-pct', gainPct(v, slider.min, slider.max).toFixed(2) + '%');
    slider.style.setProperty('--db', String(v));
    if (cap) {
      const min = Number(slider.min), max = Number(slider.max);
      cap.style.setProperty('--cap-top', capTopCss(v, min, max));
      const cell = slider.closest('.fader-cell');
      if (cell) cell.dataset.detent = (v === 0) ? 'true' : 'false';
    }
  }
  function syncMonitorUI() {
    syncMainMonitor(audioState);
    const toggle = document.getElementById('monitor-toggle');
    const sel = document.getElementById('monitor-device');
    const slider = document.getElementById('monitor-gain');
    const value = document.getElementById('monitor-gain-value');
    const cap = document.getElementById('monitor-cap');
    if (!toggle || !sel || !slider || !value) return;
    const stateEl = document.getElementById('monitor-state');
    if (stateEl) stateEl.textContent = monitorLabel(audioState);
    const enabled = audioQueue.monEnabledLatest;
    const device = audioQueue.monDeviceLatest;
    const gain = audioQueue.monGainLatest;
    toggle.checked = enabled;
    toggle.setAttribute('aria-checked', String(enabled));
    if (device && Array.from(sel.options).some(o => o.value === device)) sel.value = device;
    slider.value = String(gain);
    value.textContent = formatGain(gain);
    slider.setAttribute('aria-valuenow', String(gain));
    slider.style.setProperty('--gain-pct', gainPct(gain, slider.min, slider.max).toFixed(2) + '%');
    if (cap) {
      const min = Number(slider.min), max = Number(slider.max);
      cap.style.setProperty('--cap-top', capTopCss(gain, min, max));
    }
  }
  function formatLatency(s) {
    if (s === null || s === undefined || isNaN(s)) return '—';
    if (s < 1) return Math.round(s * 1000) + " " + t('common.ms');
    return s.toFixed(2) + " " + t('common.seconds');
  }
  function updateLatencyReadout(data) {
    const val = document.getElementById('latency-value');
    if (!val) return;
    const s = (data && typeof data.latency_seconds === 'number') ? data.latency_seconds : null;
    val.textContent = formatLatency(s);
    val.dataset.empty = (s === null) ? 'true' : 'false';
  }
  function latencyBreakdownText(l) {
    if (!l || typeof l.total_ms !== 'number') return '';
    const parts = [];
    if (typeof l.asr_ms === 'number') parts.push(t('main.recognition_prefix') + " " + l.asr_ms + " " + t('common.ms'));
    if (typeof l.tts_first_ms === 'number' && !l.cached) parts.push(t('main.fish_latency', {n:l.tts_first_ms}));
    parts.push(t('main.total_prefix') + " " + l.total_ms + " " + t('common.ms'));
    if (l.cached) parts.push(t('main.cached'));
    return parts.join(' · ');
  }
  function updateLatencyBreakdown(data) {
    const partial = document.getElementById('mic-transcript-partial');
    if (!partial || !partial.parentNode) return;
    let node = document.getElementById('latency-breakdown');
    if (!node || node.parentNode !== partial.parentNode) {
      if (node) node.remove();
      node = el('p','latency-breakdown'); node.id='latency-breakdown';
      partial.after(node);
    }
    const text = latencyBreakdownText(data && data.latency);
    node.textContent = text;
    node.hidden = !text;
  }
  let lastGenerationSeq = 0;
  let lastFinalText = '';
  function updateTranscriptUI(data) {
    const p = typeof data.transcript_partial === 'string' ? data.transcript_partial : '';
    const f = typeof data.transcript_final === 'string' ? data.transcript_final : '';
    let label = t('main.ready');
    let datasetState = 'idle';
    if (!data.active) { label = t('status.stopped'); datasetState = 'stopped'; }
    else if (data.mode === 'mic' && data.state === 'listening') { label = t('main.listening'); datasetState = 'listening'; }
    else if (data.state === 'recognizing') { label = t('main.recognizing'); datasetState = 'recognizing'; }
    else if (data.state === 'synthesizing' || data.state === 'playing') { label = t('main.synthesizing'); datasetState = 'synthesizing'; }
    const micFinal = document.getElementById('mic-transcript-final');
    if (micFinal && mode !== 'text') {
      motionText(micFinal, f || (data.active ? t('main.last_voice_hint') : t('main.microphone_hint')), 'motion-phrase');
    }
    const micPartial = document.getElementById('mic-transcript-partial');
    if (micPartial) {
      if (p && !micPartial.textContent) motionPulse(micPartial, 'motion-phrase');
      // Bounded DOM text node update: never innerHTML for untrusted content.
      const node = micPartial.firstChild;
      const newContent = p ? '… ' + p : '';
      if (node && node.nodeType === 3) {
        node.nodeValue = newContent;
      } else {
        while (micPartial.firstChild) micPartial.removeChild(micPartial.firstChild);
        micPartial.appendChild(document.createTextNode(newContent));
      }
    }
    updateLatencyBreakdown(data);
    const micState = document.getElementById('mic-state');
    if (micState && micState.dataset.state !== datasetState) {
      motionPulse(micState);
      micState.dataset.state = datasetState;
      micState.textContent = '';
      const dot = el('span','dot'); micState.append(dot, document.createTextNode(' ' + label));
    }
    const card = document.getElementById('transcript-card');
    if (card) {
      const partial = card.querySelector('.transcript-partial');
      const final = card.querySelector('.transcript-final');
      const state = card.querySelector('.transcript-state');
      if (partial) {
        const node = partial.firstChild;
        const newContent = p ? '… ' + p : '';
        if (node && node.nodeType === 3) {
          node.nodeValue = newContent;
        } else {
          while (partial.firstChild) partial.removeChild(partial.firstChild);
          partial.appendChild(document.createTextNode(newContent));
        }
      }
      if (final && f && mode !== 'text') motionText(final, f, 'motion-phrase');
      if (state && state.dataset.state !== datasetState) {
        motionPulse(state);
        state.dataset.state = datasetState;
        state.textContent = '';
        const dot = el('span','dot'); state.append(dot, document.createTextNode(' ' + label));
      }
      card.classList.toggle('has-partial', !!p);
      card.classList.toggle('has-final', !!f);
      // Transition attribute only flips once per new generation_seq integer.
      const seq = (typeof data.generation_seq === 'number') ? data.generation_seq : 0;
      const transition = (typeof data.transition === 'string') ? data.transition : '';
      if (seq !== lastGenerationSeq && transition) {
        lastGenerationSeq = seq;
        lastFinalText = f || '';
        card.dataset.transition = transition;
        const finalEl = card.querySelector('.transcript-final');
        if (finalEl && f) {
          runGenerationFlight(card, f);
        }
        // Clear after the animation has finished.
        setTimeout(() => { card.dataset.transition = ''; }, 320);
      } else if (f && f !== lastFinalText) {
        // Same seq but new final text — just update in place, no flight.
        lastFinalText = f;
      }
    }
  }
  function runGenerationFlight(card, finalText) {
    if (!card) return;
    let layer = card.querySelector('.generation-flight');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'generation-flight';
      layer.setAttribute('aria-hidden', 'true');
      card.appendChild(layer);
    }
    // Clean any previous flight.
    layer.classList.remove('run');
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    // Pull a small representative slice from the final text (≤ 4 chars),
    // anchored bottom-left, flying toward the upper-right of the card.
    const slice = String(finalText).replace(/\s+/g,' ').trim().slice(-4) || ' ';
    const rect = card.getBoundingClientRect();
    const startX = Math.min(rect.width - 30, Math.max(20, rect.width * 0.08));
    const startY = rect.height - 18;
    const endX = rect.width - 30;
    const endY = 24;
    const chars = Array.from(slice);
    const stagger = Math.min(70, 480 / Math.max(1, chars.length));
    chars.forEach((ch, i) => {
      const node = document.createElement('span');
      node.className = 'glyph';
      node.textContent = ch;
      node.style.setProperty('--gx', startX + 'px');
      node.style.setProperty('--gy', startY + 'px');
      node.style.setProperty('--tx', (endX - i * 6) + 'px');
      node.style.setProperty('--ty', (endY - i * 2) + 'px');
      node.style.animationDelay = (i * stagger) + 'ms';
      layer.appendChild(node);
    });
    // Trigger the animation on the next frame so CSS sees .run.
    requestAnimationFrame(() => { layer.classList.add('run'); });
  }
  function monitorLabel(data) {
    data = data || audioState;
    const errors = (data.monitor_errors|0);
    const enabled = audioQueue.monEnabledLatest;
    if (!enabled) return t('settings.monitor.off');
    if (data.monitor_active) return t('settings.monitor.prefix') + " " + (audioQueue.monDeviceLatest || t('common.device'));
    if (data.monitor_error) return t('settings.monitor.error_prefix') + " " + data.monitor_error;
    if (errors > 0) return t('settings.monitor.error');
    return data.active ? t('settings.monitor.connecting') : t('settings.monitor.start_hint');
  }
  function updateMonitorUI(data) {
    syncMainMonitor(data);
    const card = document.getElementById('monitor-card');
    if (!card) return;
    const state = card.querySelector('.monitor-state');
    const errors = (data.monitor_errors|0);
    if (state) {
      state.textContent = monitorLabel(data);
    }
    card.dataset.active = data.monitor_active ? 'true' : 'false';
    card.classList.toggle('is-active', !!data.monitor_active);
    card.classList.toggle('is-error', !data.monitor_active && (errors > 0 || !!data.monitor_error));
  }
  function initial(name) { const w = String(name||'').replace(/[()\[\]<>\s]/g,''); return w ? Array.from(w)[0].toUpperCase() : '—'; }
  function initials(name) { return name.replace(/[()\[\]<>]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>Array.from(w)[0]).join('').toUpperCase(); }
  function showToast(message) { showToast.translate=null; toast.textContent=message; toast.classList.add('show'); clearTimeout(showToast.timer); showToast.timer=setTimeout(()=>toast.classList.remove('show'),2800); }
  window.showToast = showToast;
  function showTranslatedToast(translate) { showToast(translate()); showToast.translate=translate; }
  function pluralVoices(n) {
    return tp('catalog.voice_word', n);
  }
  async function armBulkDelete(button, count, action) {
    if (button.disabled || count===0) return;
    if (button.dataset.armed !== '1') {
      button.dataset.armed = '1';
      button.textContent = tp('catalog.delete_voices', count);
      button._armTimer = setTimeout(()=>{button.textContent=t('common.delete_count_prefix')+count+')';delete button.dataset.armed;},3000);
      return;
    }
    clearTimeout(button._armTimer); delete button.dataset.armed; button.disabled=true;
    try { await action(); }
    catch(error) { showToast(error.message); }
    finally { if(button.isConnected) button.disabled=false; }
  }
  function selectionCheck(name, checked) {
    const check=el('span','select-check');check.setAttribute('role','checkbox');
    check.setAttribute('aria-checked',String(checked));check.setAttribute('aria-label',t('common.select_prefix') + " "+name);
    return check;
  }
  function el(tag, className, content) { const node=document.createElement(tag); if(className) node.className=className; if(content!==undefined) node.textContent=content; return node; }
  function avatarEl(name, url, large, eager) {
    const box = el('div', large ? 'avatar-lg' : 'avatar');
    box.textContent = initial(name);
    if (url && /^https:\/\/public-platform\.r2\.fish\.audio\//.test(url)) {
      const img = document.createElement('img');
      img.alt = '';
      img.loading = eager ? 'eager' : 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.src = url;
      if (avatarLoaded.has(url)) img.classList.add('loaded');
      img.addEventListener('load', () => { avatarLoaded.add(url); img.classList.add('loaded'); });
      img.addEventListener('error', () => { img.remove(); });
      box.append(img);
    }
    return box;
  }
  function previewBtn(v) {
    const btn = el('button','preview-btn');
    btn.type = 'button';
    btn.dataset.previewId = v.id;
    btn.dataset.label = t('catalog.demo_prefix') + " " + v.name;
    btn.setAttribute('aria-label', btn.dataset.label);
    btn.title = t('catalog.demo_title');
    btn.innerHTML = ICON_PLAY;
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      const id = v.id;
      // If this exact id is already active — stop.
      if (btn.dataset.state === 'active') {
        setPreviewVisual(btn, '', true, '');
        await apiPreview('stop', id);
        setPreviewVisual(btn, '', false, '');
        return;
      }
      // If something else is busy/active — refuse politely.
      const anyActive = Array.from(document.querySelectorAll('.preview-btn')).some(b => b.dataset.state === 'active');
      if (anyActive) {
        showTranslatedToast(() => t('catalog.stop_preview_first'));
        return;
      }
      setPreviewVisual(btn, 'active', true, '');
      const res = await apiPreview('start', id);
      if (res.state === 'active' || res.state === 'loading') {
        setPreviewActive(btn, id);
        // Visual stays active; backend may transition later via /api/status poll
        // (status will reflect preview_state). We keep polling button in sync.
      } else if (res.state === 'error') {
        setPreviewVisual(btn, 'error', false, res.error);
        showTranslatedToast(() => res.error || t('errors.demo_start'));
      } else {
        setPreviewVisual(btn, '', false, '');
      }
    });
    return btn;
  }
  function makeRow(v, catalog) {
    const row=el('div','voice-row'+(selected?.id===v.id?' selected':'')); row.dataset.id=v.id;
    row.setAttribute('role','option'); row.setAttribute('aria-selected',String(selected?.id===v.id)); row.tabIndex=0;
    if(ttsSelect.on && !catalog) { row.classList.add('selecting');row.classList.toggle('checked',ttsSelect.ids.has(v.id));row.append(selectionCheck(v.name,ttsSelect.ids.has(v.id))); }
    row.append(avatarEl(v.name, v.avatar_url, false, false));
    const info=el('div','voice-info');
    const voiceName=el('div','voice-name',v.name); voiceName.title=v.name;
    const voiceMeta=el('div','voice-meta',voiceSourceMeta(v)); voiceMeta.title=voiceMeta.textContent;
    info.append(voiceName,voiceMeta); row.append(info);
    if(catalog) {
      const link=el('a','fish-link','↗'); link.href=v.url; link.target='_blank'; link.rel='noopener noreferrer'; link.setAttribute('aria-label',t('catalog.open_voice', {name:v.name})); row.append(link);
      const add=el('button','event-add',voices.some(x=>x.id===v.id)?'✓':'+'); add.type='button'; add.dataset.id=v.id; add.setAttribute('aria-label',t('common.add_prefix') + " "+v.name); row.append(add);
      row.append(previewBtn(v));
    } else if(!ttsSelect.on) {
      const star=el('button','star'+(favs.has(v.id)?' fav':''),favs.has(v.id)?'★':'☆'); star.type='button'; star.dataset.star=v.id;
      star.setAttribute('aria-label',t('catalog.favorite_prefix') + " "+v.name); star.setAttribute('aria-pressed',String(favs.has(v.id))); row.append(star);
      {
        const rm=el('button','event-remove','×'); rm.type='button'; rm.dataset.remove=v.id;
        rm.setAttribute('aria-label',t('catalog.remove_voice', {name:v.name}));
        rm.title=t('catalog.remove_title');
        row.append(rm);
      }
      row.append(previewBtn(v));
    }
    return row;
  }
  function ttsCandidates() { return voices.filter(v=>(!onlyFavs||favs.has(v.id))&&v.name.toLowerCase().includes(queries.mine.trim().toLowerCase())); }
  function renderList() {
    const finishMotion = motionRows(list, tab === 'mine' ? new Set(voices.map(v => v.id)) : null, tab);
    list.replaceChildren();
    const catalogFilters = $('#catalog-filters');
    if (catalogFilters) catalogFilters.hidden = tab !== 'catalog';
    favButton.hidden=tab!=='mine'; favButton.setAttribute('aria-pressed',String(onlyFavs));
    const candidates=tab==='mine' ? ttsCandidates() : results;
    $('#voice-count').textContent=tab==='mine' ? tp('catalog.voice_count', candidates.length) : t('catalog.fish_voices');
    $('#select-btn').hidden=tab!=='mine'||!voices.length;
    $('#select-btn').textContent=ttsSelect.on?t('common.done'):t('common.select_prefix');
    $('#select-bar').hidden=!ttsSelect.on;
    const deleteBtn=$('#select-delete-btn'),count=String(ttsSelect.ids.size);
    if(!ttsSelect.on||deleteBtn.dataset.count!==count){clearTimeout(deleteBtn._armTimer);delete deleteBtn.dataset.armed;}
    deleteBtn.dataset.count=count;
    deleteBtn.textContent=deleteBtn.dataset.armed==='1'?tp('catalog.delete_voices', Number(count)):t('common.delete_count_prefix')+count+')';
    deleteBtn.disabled=ttsSelect.ids.size===0;
    $('#select-all-btn').textContent=candidates.length && candidates.every(v=>ttsSelect.ids.has(v.id))?t('common.deselect'):t('common.all');
    if(ttsSelect.on){$('#voice-count').textContent=t('common.selected_prefix') + " "+ttsSelect.ids.size;favButton.hidden=true;}
    if(tab==='mine' && !voices.length) {
      const empty = $('#voice-empty-state').content.cloneNode(true); applyI18n(empty); list.append(empty);
      $('#find-voice-btn').addEventListener('click',()=>{
        $('.tab[data-tab="catalog"]').click();
        input.focus();
      });
    }
    else if(searchError && tab==='catalog') list.append(el('div','error-msg',searchError));
    else if(busy && !results.length && tab==='catalog') list.append(el('div','empty-state',t('catalog.searching')));
    else if(!candidates.length && !searchError) list.append(el('div','empty-state',tab==='catalog' ? (queries.catalog?t('catalog.no_results'):t('catalog.search_hint')) : (onlyFavs?t('catalog.favorites_hint'):t('catalog.empty_search'))));
    candidates.forEach(v=>list.append(makeRow(v,tab==='catalog')));
    if(tab==='catalog' && hasMore) { const more=el('button','load-more',busy?t('common.loading'):t('catalog.show_more')); more.type='button'; more.id='load-more-btn'; more.disabled=busy; list.append(more); }
    $('#search-btn').disabled=busy && tab==='catalog';
    finishMotion();
  }
  let voiceCardKey = '';
  function setCardAvatar(name, url) {
    const av = $('#main-avatar'); if (!av) return;
    av.replaceChildren(); av.textContent = initial(name);
    if (url && /^https:\/\/public-platform\.r2\.fish\.audio\//.test(url)) { const img = document.createElement('img'); img.alt=''; img.loading='eager'; img.decoding='async'; img.referrerPolicy='no-referrer'; img.src=url; if(avatarLoaded.has(url))img.classList.add('loaded'); img.addEventListener('load',()=>{avatarLoaded.add(url);img.classList.add('loaded');}); img.addEventListener('error',()=>img.remove()); av.append(img); }
  }
  function updateVoiceCard() {
    if (!$('#voice-card')) return;
    let name, meta, url = null;
    if (mode === 'vc') {
      const v = vcVoices.find(x => x.id === vcSelectedId);
      name = v ? v.name : t('main.choose_voice');
      meta = v ? (vcKindBadge(v.kind).label || t('main.live_voice')) : t('main.no_voice');
    } else if (selected) {
      name = selected.name; url = selected.avatar_url || null;
      meta = voiceSourceMeta(selected) + (typeof selected.task_count === 'number' ? ' ' + t('catalog.synthesis_count', {n:selected.task_count.toLocaleString(fmtLocale())}) : '');
    } else { name = t('main.choose_voice'); meta = t('main.no_voice'); }
    const key = JSON.stringify([mode, name, meta, url]);
    if (key === voiceCardKey) return;
    voiceCardKey = key;
    setCardAvatar(name, url);
    $('#main-name').textContent = name;
    $('#main-sub').textContent = meta;
  }
  const updateMainCard = () => updateVoiceCard();
  function updateStatusPill(data) {
    const pill = $('#status-pill'); if (!pill) return;
    let state = 'stopped', label = t('status.stopped');
    if (data && data.active) {
      if (data.state === 'starting' || vcLoading(data)) { state = 'loading'; label = t('main.loading'); }
      else if (data.state === 'synthesizing' || data.state === 'playing') { state = 'speaking'; label = t('main.speaking'); }
      else if (data.state === 'listening') { state = 'listening'; label = t('main.listening'); }
    } else if (vcLoading(data)) { state = 'loading'; label = t('main.loading'); }
    if (pill.dataset.state !== state) pill.dataset.state = state;
    motionText(pill, label);
  }
  const effectiveInput = () => $('#input-dev').value || deviceInfo.default_input || '';
  const effectiveOutput = () => $('#output-dev').value || deviceInfo.default_output || '';
  function updateRouteChip() {
    const text = $('#route-text'); if (!text) return;
    const label = (effectiveInput() || t('settings.audio.no_input')) + ' → ' + (effectiveOutput() || t('settings.audio.no_output'));
    if (text.textContent !== label) text.textContent = label;
  }
  async function select(v,apply=true){
    try {
      if(apply){displayStatus(await api('/api/control',{action:'voice',voice_id:v.id}));}
      selected=v;
      updateMainCard(v);
      renderList();
    } catch(error){showToast(error.message);}
  }
  function fillMonitors(select, names) {
    if (!select) return;
    select.replaceChildren();
    const list = Array.isArray(names) ? names.slice() : [];
    lastMonitors = list.slice();
    list.forEach(name => {
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      select.append(o);
    });
    if (!list.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = t('settings.audio.no_devices');
      select.append(o);
    }
  }
  // Fader definitions shared across re-renders.
  const FADER_SPECS = {
    input:  {trackId:'input-track',  inputId:'input-gain',  capId:'input-cap',  readoutId:'input-gain-value',  resetId:'input-gain-reset',  detentId:'input-detent',  prefsKey:'input_gain_db',  latestKey:'inputGainLatest', resetDb:0, detentValue:0, get label() { return t('settings.audio.input'); }},
    output: {trackId:'output-track', inputId:'output-gain', capId:'output-cap', readoutId:'gain-value',       resetId:'gain-reset',      detentId:'output-detent', prefsKey:'output_gain_db', latestKey:'gainLatest',     resetDb:0, detentValue:0, get label() { return t('settings.audio.output'); }},
    monitor:{trackId:'monitor-track',inputId:'monitor-gain',capId:'monitor-cap',readoutId:'monitor-gain-value',resetId:'monitor-gain-reset',detentId:'monitor-detent',prefsKey:'monitor_gain_db',latestKey:'monGainLatest',resetDb:0, detentValue:0, get label() { return t('settings.audio.monitoring'); }}
  };
  function buildFaderRack() {
    const rack = el('div','fader-rack');
    const rackLabel = el('span','fader-rack-label',t('main.console'));
    rack.append(rackLabel);
    const labels = {input:t('settings.audio.input'), output:t('settings.audio.output'), monitor:t('settings.audio.monitoring')};
    ['input','output','monitor'].forEach(key => {
      const spec = FADER_SPECS[key];
      const cell = el('div','fader-cell'); cell.dataset.fader = key;
      const name = el('label','fader-name', labels[key]);
      name.htmlFor = spec.inputId;
      const wrap = el('div','fader-track-wrap');
      const scale = el('div','fader-scale');
      // Major dB marks: +12, +6, 0, −6, −12, −18, −24. 0 highlighted.
      [12, 6, 0, -6, -12, -18, -24].forEach(v => {
        const s = el('span', v === 0 ? 'major zero' : ''); s.textContent = (v > 0 ? '+' : '') + v;
        scale.append(s);
      });
      const track = el('div','fader-track'); track.id = spec.trackId;
      const detent = el('div','detent'); detent.id = spec.detentId;
      track.append(detent);
      const slider = el('input','fader-input'); slider.type = 'range';
      slider.id = spec.inputId;
      slider.min = '-24'; slider.max = '12'; slider.step = '1';
      slider.setAttribute('aria-label', labels[key] + " " + t('settings.audio.decibels_suffix'));
      slider.setAttribute('aria-orientation', 'vertical');
      slider.setAttribute('aria-valuemin','-24');
      slider.setAttribute('aria-valuemax','12');
      track.append(slider);
      const cap = el('div','fader-cap'); cap.id = spec.capId;
      track.append(cap);
      wrap.append(scale, track);
      const tipText = key === 'input'
        ? t('settings.audio.input_hint')
        : (key === 'monitor' ? t('settings.audio.monitor_gain_hint') : t('settings.audio.output_gain_hint'));
      const tip = el('div','fader-tooltip', tipText);
      cell.append(name, wrap, tip);
      const readout = el('div','fader-readout');
      const valSpan = el('span','fader-readout-value'); valSpan.id = spec.readoutId;
      valSpan.textContent = formatGain(spec.resetDb);
      const unit = el('span','fader-readout-unit',t('common.db'));
      readout.append(valSpan, unit);
      cell.append(readout);
      const reset = el('button','fader-reset',t('settings.audio.zero_db')); reset.type='button';
      reset.id = spec.resetId;
      reset.setAttribute('aria-label',t('settings.audio.reset_gain', {name:labels[key].toLowerCase()}));
      cell.append(reset);
      rack.append(cell);
    });
    return rack;
  }
  function syncMainMonitor(data) {
    const btn = $('#monitor-btn');
    if (!btn) return;
    const enabled = audioQueue.monEnabledLatest;
    btn.setAttribute('aria-pressed', String(enabled));
    const use = btn.querySelector('use');
    if (use) use.setAttribute('href', enabled ? '#i-speaker' : '#i-speaker-off');
    const state = data || audioState;
    btn.classList.toggle('warn', !!(enabled && state && state.active && state.monitor_active === false));
  }
  function buildConsoleReadouts() {
    const wrap = el('div','console-readouts');
    const mk = (id, label, hint, value) => {
      const r = el('div','readout');
      const l = el('div','readout-label', label);
      const v = el('div','readout-value'); v.id = id; v.textContent = value; v.dataset.empty = 'true';
      const h = el('div','readout-hint', hint);
      r.append(l, v, h);
      wrap.append(r);
    };
    mk('latency-value', t('main.latency_title'), t('main.latency_hint'), '—');
    return wrap;
  }
  function renderMode() {
    if(mode!=='vc')hfCloseDetail();
    vcSyncMeter();
    document.querySelectorAll('.mode-btn').forEach(b=>{ b.classList.toggle('active',b.dataset.mode===mode); b.setAttribute('aria-selected',String(b.dataset.mode===mode)); });
    const target = $('#mode-panel');
    if(!target) return;
    const changed = document.body.dataset.mode !== mode;
    const finishMotion = changed ? motionCrossfade(target, true) : () => {};
    target.replaceChildren();
    document.body.dataset.mode = mode;
    const vcVolume = $('#vc-volume');
    if (vcVolume) vcVolume.hidden = mode !== 'vc';
    $('.mode-tabs').style.setProperty('--motion-segment', String(['mic','text','vc'].indexOf(mode)));
    if(mode==='vc') { vcRenderPanel(target); displayStatus(audioState); finishMotion(); return; }
    if(mode==='mic') {
      const stage = el('div','mode-stage mic-stage');
      const card = el('div','transcript-card'); card.id = 'transcript-card';
      const head = el('div','transcript-head');
      const eyebrow = el('span','eyebrow',t('main.recognition_heading'));
      const state = el('span','transcript-state'); state.id='mic-state'; state.dataset.state = audioState && audioState.active ? 'listening' : 'idle';
      const dot = el('span','dot'); state.append(dot, document.createTextNode(' ' + (audioState && audioState.active ? t('status.listening') : t('main.ready_to_start'))));
      head.append(eyebrow, state);
      card.append(head);
      const finalEl = el('p','transcript-final'); finalEl.id='mic-transcript-final';
      finalEl.textContent = (audioState && audioState.transcript_final) ? audioState.transcript_final : t('main.microphone_hint');
      card.append(finalEl);
      const partialEl = el('p','transcript-partial'); partialEl.id='mic-transcript-partial';
      partialEl.appendChild(document.createTextNode((audioState && audioState.transcript_partial) ? '… ' + audioState.transcript_partial : ''));
      card.append(partialEl);
      stage.append(card);
      target.append(stage);
    } else {
      const card = el('div','transcript-card'); card.id = 'transcript-card';
      const head = el('div','transcript-head');
      const eyebrow = el('span','eyebrow',t('main.text_heading'));
      const state = el('span','transcript-state'); state.id='mic-state'; state.dataset.state = audioState && audioState.active ? 'synthesizing' : 'idle';
      const dot = el('span','dot'); state.append(dot, document.createTextNode(' ' + (audioState && audioState.active ? t('main.synthesizing') : t('main.ready'))));
      head.append(eyebrow, state);
      card.append(head);
      const finalEl = el('p','transcript-final'); finalEl.id='mic-transcript-final';
      finalEl.textContent = text ? text : t('main.text_hint');
      card.append(finalEl);
      const partialEl = el('p','transcript-partial'); partialEl.id='mic-transcript-partial';
      partialEl.appendChild(document.createTextNode(''));
      card.append(partialEl);
      target.append(card);
      const textLabel = el('label','text-label',t('main.text_label')); textLabel.htmlFor='text-input';
      const textarea = el('textarea'); textarea.id='text-input'; textarea.maxLength=1000; textarea.placeholder=t('main.text_placeholder');
      const hint = el('div','text-hint'); hint.append(el('span','text-hint-msg',t('main.text_shortcuts')));
      target.append(textLabel, textarea, hint);
      const field=target.querySelector('#text-input'); field.value=text; field.addEventListener('input',()=>text=field.value);
      field.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.altKey&&!event.metaKey&&!event.ctrlKey&&!event.isComposing){event.preventDefault();if(text.trim()&&!commandBusy)audioAction();}});
    }
    // Console shell: side stack + fader rack.
    const shell = el('div','console-shell');
    const rack = buildFaderRack();
    const readouts = buildConsoleReadouts();
    const rackCol = el('div','rack-col'); rackCol.append(rack, readouts);
    shell.append(rackCol);
    target.append(shell);
    // Attach vertical faders (preserve DOM identity across re-render via stable IDs).
    attachVerticalFader(FADER_SPECS.input);
    attachVerticalFader(FADER_SPECS.output);
    // Monitor gain attaches directly (kept here for parity, uses same pattern).
    const monSlider = document.getElementById('monitor-gain');
    const monCap = document.getElementById('monitor-cap');
    const monVal = document.getElementById('monitor-gain-value');
    if (monSlider && monCap && monVal) {
      monSlider.addEventListener('input', () => {
        const db = Number(monSlider.value);
        monVal.textContent = formatGain(db);
        monSlider.setAttribute('aria-valuenow', String(db));
        const min = Number(monSlider.min), max = Number(monSlider.max);
        monCap.style.setProperty('--cap-top', capTopCss(db, min, max));
        audioQueue.setMonitorGain(db);
      });
      monSlider.addEventListener('keydown', event => {
        const step = 1;
        let v = Number(monSlider.value);
        let handled = true;
        const min = Number(monSlider.min), max = Number(monSlider.max);
        const clamp = (x) => Math.max(min, Math.min(max, x));
        if (event.key === 'ArrowUp') v = clamp(v + step);
        else if (event.key === 'ArrowDown') v = clamp(v - step);
        else if (event.key === 'PageUp') v = clamp(v + 3);
        else if (event.key === 'PageDown') v = clamp(v - 3);
        else if (event.key === 'Home') v = min;
        else if (event.key === 'End') v = max;
        else handled = false;
        if (handled) { event.preventDefault(); monSlider.value = String(v); monSlider.dispatchEvent(new Event('input')); }
      });
      const monReset = document.getElementById('monitor-gain-reset');
      if (monReset) monReset.addEventListener('click', () => {
        monSlider.value = '0';
        monSlider.dispatchEvent(new Event('input'));
      });
    }
    // Re-fill monitor options now that the DOM exists; preserve selected device.
    const sel = $('#monitor-device');
    if (sel) {
      fillMonitors(sel, lastMonitors);
      if (audioQueue.monDeviceLatest && Array.from(sel.options).some(o => o.value === audioQueue.monDeviceLatest)) {
        sel.value = audioQueue.monDeviceLatest;
      }
    }
    syncMonitorUI();
    syncGainUI();
    syncInputGainUI();
    updateLatencyReadout(audioState);
    displayStatus(audioState);
    finishMotion();
  }
  function buildAudioSettings() {
    const box = $('#audio-settings');
    if (!box || $('#normalize-toggle')) return;
    box.append(buildNormalizeRow(), buildMonitorRow(), vcBuildTextVoiceRow());
    $('#asr-settings').append(buildEngineRow(), buildSpeechLanguageRow());
    $('#keys-settings').append(buildSwapRows());
    syncSwapHotkey();
    buildUsageSettings();
  }
  function syncLanguageControls() {
    const ui = $('#ui-language-select');
    ui.replaceChildren(...['en', 'ru'].map(code => { const option = el('option', null, langName(code)); option.value = code; return option; }));
    ui.value = I18N.lang;
    $('#catalog-language-filter').value = prefsState.catalog_language || I18N.lang;
    const speech = $('#speech-language-select');
    if (!speech) return;
    const codes = [...new Set(speechLanguages.map(locale => locale.id).filter(Boolean))];
    codes.sort((a, b) => displayLang(a).localeCompare(displayLang(b), fmtLocale()));
    speech.replaceChildren(...codes.map(code => { const option = el('option', null, displayLang(code)); option.value = code; return option; }));
    speech.value = speechLanguage;
    speech.title = tp('settings.asr.language_count', codes.length);
    syncSpeechLanguageHint();
  }
  function syncSpeechLanguageHint() {
    const select = $('#speech-language-select'), hint = $('#speech-language-hint');
    if (!select || !hint) return;
    const gigaam = prefsState.asr_engine === 'gigaam';
    select.disabled = gigaam || select.dataset.saving === 'true';
    if (!audioState.active) speechRestartPending = false;
    hint.hidden = !gigaam && !speechRestartPending;
    hint.textContent = gigaam ? t('settings.asr.gigaam_ru_only') : t('settings.asr.restart_hint');
  }
  function buildSpeechLanguageRow() {
    const wrap = el('div', 'audio-row');
    const label = el('label', null, t('settings.asr.speech_language'));
    label.htmlFor = 'speech-language-select'; label.dataset.i18n = 'settings.asr.speech_language';
    const select = el('select'); select.id = 'speech-language-select';
    const hint = el('p', 'settings-hint'); hint.id = 'speech-language-hint'; hint.hidden = true;
    select.addEventListener('change', async () => {
      const value = select.value;
      select.dataset.saving = 'true'; syncSpeechLanguageHint();
      try {
        const prefs = await api('/api/preferences', {speech_language:value});
        speechLanguage = prefs.speech_language; prefsState.speech_language = prefs.speech_language;
        speechRestartPending = !!audioState.active;
      } catch (error) { showToast(error.message); }
      finally { delete select.dataset.saving; select.value = speechLanguage; syncSpeechLanguageHint(); }
    });
    wrap.append(label, select, hint);
    return wrap;
  }
  // Voice swap hotkey: the native shell registers the Carbon hot key and calls aiVoiceHotkey('down'|'up').
  const SWAP_KEYS = {KeyA:0,KeyS:1,KeyD:2,KeyF:3,KeyH:4,KeyG:5,KeyZ:6,KeyX:7,KeyC:8,KeyV:9,KeyB:11,KeyQ:12,KeyW:13,KeyE:14,KeyR:15,KeyY:16,KeyT:17,Digit1:18,Digit2:19,Digit3:20,Digit4:21,Digit6:22,Digit5:23,Equal:24,Digit9:25,Digit7:26,Minus:27,Digit8:28,Digit0:29,BracketRight:30,KeyO:31,KeyU:32,BracketLeft:33,KeyI:34,KeyP:35,KeyL:37,KeyJ:38,Quote:39,KeyK:40,Semicolon:41,Backslash:42,Comma:43,Slash:44,KeyN:45,KeyM:46,Period:47,Space:49,Backquote:50,F1:122,F2:120,F3:99,F4:118,F5:96,F6:97,F7:98,F8:100,F9:101,F10:109,F11:103,F12:111,F13:105,F14:107,F15:113,F16:106,F17:64,F18:79,F19:80,F20:90};
  const SWAP_SYMBOLS = {Space:'Space',Backquote:'`',Minus:'-',Equal:'=',BracketLeft:'[',BracketRight:']',Semicolon:';',Quote:"'",Comma:',',Period:'.',Slash:'/',Backslash:'\\'};
  const SWAP_MOD_ONLY = new Set(['MetaLeft','MetaRight','AltLeft','AltRight','ControlLeft','ControlRight','ShiftLeft','ShiftRight','CapsLock','Fn']);
  const SWAP_DEFAULT_HOTKEY = {key_code:1, modifiers:2304, label:'\u2325\u2318S'};
  function swapHotkey() { return prefsState.swap_hotkey || SWAP_DEFAULT_HOTKEY; }
  function swapComboFromEvent(event) {
    const code = SWAP_KEYS[event.code];
    if (code === undefined) return {error:t('settings.keys.unsupported')};
    const modifiers = (event.metaKey ? 256 : 0) | (event.shiftKey ? 512 : 0) | (event.altKey ? 2048 : 0) | (event.ctrlKey ? 4096 : 0);
    const functionKey = /^F(1[3-9]|20)$/.test(event.code);
    if (!functionKey && !(modifiers & (256 | 2048 | 4096))) return {error:t('settings.keys.modifier_required')};
    const keyLabel = SWAP_SYMBOLS[event.code] || event.code.replace(/^(Key|Digit)/, '');
    const label = (event.ctrlKey ? '\u2303' : '') + (event.altKey ? '\u2325' : '') + (event.shiftKey ? '\u21e7' : '') + (event.metaKey ? '\u2318' : '') + keyLabel;
    return {key_code:code, modifiers, label};
  }
  function postHotkey(message) {
    if (APP_PLATFORM === 'win') return; // no swap hotkey on Windows in v1
    nativePost('hotkey', message);
  }
  function syncSwapHotkey() {
    if (prefsState.swap_enabled) { const h = swapHotkey(); postHotkey({action:'register', key_code:h.key_code, modifiers:h.modifiers}); }
    else postHotkey({action:'unregister'});
  }
  window.aiVoiceHotkeyError = () => showTranslatedToast(() => t('settings.keys.shortcut_taken'));
  let swapDesired = null, swapSent = null, swapInFlight = false;
  async function swapPump() {
    if (swapInFlight) return;
    swapInFlight = true;
    try {
      while (swapDesired !== null) {
        const value = swapDesired; swapDesired = null; swapSent = value;
        try { displayStatus(await api('/api/control', {action:'vc_bypass', bypass:value})); }
        catch (error) { swapDesired = null; showToast(error.message); }
      }
    } finally { swapInFlight = false; swapSent = null; }
  }
  function swapRequest(value) { swapDesired = value; swapPump(); }
  window.aiVoiceHotkey = kind => {
    if (!prefsState.swap_enabled || !audioState.active || audioState.mode !== 'vc') return;
    if (kind === 'down') {
      if (prefsState.swap_mode === 'toggle') {
        const current = swapDesired !== null ? swapDesired : (swapSent !== null ? swapSent : !!(audioState.vc && audioState.vc.bypass));
        swapRequest(!current);
      } else swapRequest(false);
    } else if (kind === 'up' && prefsState.swap_mode !== 'toggle') swapRequest(true);
  };
  function updateSwapPill(data) {
    const pill = $('#swap-pill'); if (!pill) return;
    const show = !!(data && data.active && data.mode === 'vc' && data.vc && data.vc.swap_enabled);
    pill.hidden = !show;
    if (!show) return;
    const state = data.vc.bypass ? 'own' : 'neural', label = data.vc.bypass ? t('main.own_voice') : t('main.neural_voice');
    if (pill.dataset.state !== state) pill.dataset.state = state;
    motionText(pill, label);
  }
  function buildSwapRows() {
    const wrap = el('div','keys-block');
    const save = (patch, rollback) => api('/api/preferences', patch).then(p => {
      Object.keys(patch).forEach(key => { prefsState[key] = p[key]; });
      return p;
    }).catch(error => { showToast(error.message); if (rollback) rollback(); throw error; });
    // Enable switch
    const enableRow = el('div','audio-row keys-row');
    enableRow.append(el('span','keys-title',t('settings.keys.swap_toggle')));
    const toggle = el('input'); toggle.type='checkbox'; toggle.id='swap-toggle'; toggle.className='monitor-toggle';
    toggle.setAttribute('aria-label',t('settings.keys.swap_toggle')); toggle.checked = !!prefsState.swap_enabled;
    toggle.addEventListener('change', () => {
      const value = toggle.checked;
      save({swap_enabled:value}, () => { toggle.checked = !!prefsState.swap_enabled; }).then(() => {
        syncSwapHotkey();
        if (audioState.active && audioState.mode === 'vc') swapRequest(value);
      }).catch(() => {});
    });
    enableRow.append(toggle);
    // Mode segment
    const modeRow = el('div','audio-row keys-row');
    modeRow.append(el('span','keys-title',t('settings.keys.method')));
    const segment = el('div','vc-presets'); segment.id='swap-mode'; segment.setAttribute('role','group'); segment.setAttribute('aria-label',t('settings.keys.swap_method'));
    const buttons = [['hold',t('settings.keys.hold')],['toggle',t('settings.keys.toggle')]].map(([value,text]) => {
      const b = el('button',null,text); b.type='button'; b.dataset.mode=value; return b;
    });
    const showMode = () => buttons.forEach(b => { const on = b.dataset.mode === (prefsState.swap_mode === 'toggle' ? 'toggle' : 'hold'); b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
    buttons.forEach(b => b.addEventListener('click', () => {
      const previous = prefsState.swap_mode;
      prefsState.swap_mode = b.dataset.mode; showMode();
      save({swap_mode:b.dataset.mode}, () => { prefsState.swap_mode = previous; showMode(); }).catch(() => {});
    }));
    segment.append(...buttons); showMode(); modeRow.append(segment);
    if (APP_PLATFORM === 'win') { wrap.append(enableRow, modeRow); return wrap; }
    // Hotkey recorder
    const keyRow = el('div','audio-row keys-row');
    keyRow.append(el('span','keys-title',t('settings.keys.shortcut')));
    const record = el('button','keys-record'); record.type='button'; record.id='swap-hotkey'; record.textContent = swapHotkey().label;
    const error = el('p','keys-error'); error.id='swap-hotkey-error'; error.hidden = true; error.setAttribute('role','alert');
    let recording = false;
    const stopRecording = () => {
      recording = false; record.dataset.recording = 'false'; record.textContent = swapHotkey().label;
      document.removeEventListener('keydown', onKey, true);
    };
    function onKey(event) {
      event.preventDefault(); event.stopPropagation();
      if (event.repeat || SWAP_MOD_ONLY.has(event.code)) return;
      if (event.code === 'Escape') { error.hidden = true; stopRecording(); return; }
      const combo = swapComboFromEvent(event);
      if (combo.error) { error.textContent = combo.error; error.hidden = false; return; }
      error.hidden = true; stopRecording();
      const previous = prefsState.swap_hotkey;
      prefsState.swap_hotkey = combo; record.textContent = combo.label;
      save({swap_hotkey:combo}, () => { prefsState.swap_hotkey = previous; record.textContent = swapHotkey().label; })
        .then(syncSwapHotkey).catch(() => {});
    }
    record.addEventListener('click', () => {
      if (recording) return;
      recording = true; record.dataset.recording = 'true'; record.textContent = t('settings.keys.recording'); error.hidden = true;
      document.addEventListener('keydown', onKey, true);
    });
    record.addEventListener('blur', () => { if (recording) { error.hidden = true; stopRecording(); } });
    keyRow.append(record);
    const note = el('p','keys-note',t('settings.keys.latency_hint'));
    wrap.append(enableRow, modeRow, keyRow, error, note);
    return wrap;
  }
  function buildNormalizeRow(){
    const wrap = el('div','audio-row');
    const label = el('label','audio-toggle');
    const cb = el('input'); cb.type='checkbox'; cb.id='normalize-toggle';
    cb.checked = !!audioQueue.normLatest;
    cb.setAttribute('aria-label',t('settings.audio.normalize_aria'));
    cb.setAttribute('aria-checked', String(cb.checked));
    let timer = null;
    cb.addEventListener('change',()=>{
      const value = cb.checked;
      audioQueue.setNorm(value);
      if (timer) clearTimeout(timer);
      timer = setTimeout(()=>{ syncNormUI(); }, 140);
    });
    label.append(cb, document.createTextNode(" " + t('settings.audio.normalize')));
    const hint = el('span','audio-hint'); hint.textContent=t('settings.audio.next_phrase');
    wrap.append(label, hint);
    return wrap;
  }
  function buildEngineRow(){
    const wrap = el('div','audio-row engine-row'); wrap.id='asr-engine-card';
    const title = el('span','engine-title',t('settings.asr.recognition'));
    const hint = el('span','audio-hint',t('settings.asr.next_start'));
    const engine = el('select'); engine.id='asr-engine'; engine.setAttribute('aria-label',t('settings.asr.engine'));
    [['apple',t('settings.asr.apple')],['gigaam',t('settings.asr.gigaam')]].forEach(([v,t]) => { const o=el('option',null,t); o.value=v; engine.append(o); });
    const current = () => prefsState.asr_engine === 'gigaam' ? 'gigaam' : 'apple';
    engine.value = current();
    engine.addEventListener('change', () => {
      api('/api/preferences',{asr_engine:engine.value}).then(p => { prefsState.asr_engine=p.asr_engine; syncSpeechLanguageHint(); })
        .catch(e => { showToast(e.message); engine.value = current(); });
    });
    wrap.append(title, hint, engine);
    return wrap;
  }
  function buildMonitorRow(){
    const wrap = el('section','monitor-panel');
    wrap.id = 'monitor-card';
    wrap.dataset.active = 'false';
    const head = el('div','monitor-head');
    const titleWrap = el('div','monitor-title-wrap');
    const title = el('div','monitor-title');
    const marker = el('span','marker'); title.append(marker);
    const label = el('span','monitor-label'); label.textContent=t('settings.audio.monitoring');
    title.append(label);
    const hint = el('span','monitor-hint'); hint.textContent=t('settings.monitor.headphones_hint');
    titleWrap.append(title, hint);
    const state = el('span','monitor-state'); state.id='monitor-state'; state.textContent=t('settings.monitor.off');
    head.append(titleWrap, state);
    const toggleLabel = el('label','monitor-toggle-label');
    const toggle = el('input'); toggle.type='checkbox'; toggle.id='monitor-toggle';
    toggle.className='monitor-toggle';
    toggle.setAttribute('aria-label',t('settings.monitor.enable_aria'));
    toggle.setAttribute('aria-checked','false');
    toggle.addEventListener('change',()=>{
      const value = toggle.checked;
      const sel = document.getElementById('monitor-device');
      const dev = sel ? sel.value : '';
      const outDev = effectiveOutput();
      if (value && (!dev || dev === outDev)) {
        toggle.checked = false;
        showTranslatedToast(() => t('settings.monitor.choose_device'));
        return;
      }
      audioQueue.setMonitorEnabled(value);
    });
    toggleLabel.append(toggle);
    head.append(toggleLabel);
    wrap.append(head);

    const deviceLabel = el('label','monitor-device-label');
    deviceLabel.htmlFor='monitor-device';
    deviceLabel.textContent=t('settings.monitor.device');
    const sel = el('select','monitor-select'); sel.id='monitor-device';
    sel.setAttribute('aria-label',t('settings.monitor.device_aria'));
    sel.addEventListener('change',()=>{
      const value = sel.value;
      const outDev = effectiveOutput();
      if (value && value === outDev) {
        showTranslatedToast(() => t('settings.monitor.device_in_use'));
        if (audioQueue.confirmed.monDevice && Array.from(sel.options).some(o => o.value === audioQueue.confirmed.monDevice)) {
          sel.value = audioQueue.confirmed.monDevice;
        } else {
          sel.value = '';
        }
        return;
      }
      audioQueue.setMonitorDevice(value || null);
    });
    const selWrap = el('div','monitor-row');
    selWrap.append(deviceLabel, sel);
    wrap.append(selWrap);

    const note = el('p','monitor-note'); note.textContent=t('settings.monitor.note');
    wrap.append(note);
    return wrap;
  }
  let historyKey = '';
  function renderHistory(items) {
    const box = document.getElementById('history'), list = document.getElementById('history-list');
    if (!box || !list) return;
    const rows = Array.isArray(items) ? items.slice(0, 15) : [];
    box.hidden = rows.length === 0;
    const key = I18N.lang + ':' + rows.map(r => r.id + ':' + r.text).join('\n');
    if (key === historyKey) return;
    historyKey = key;
    list.replaceChildren();
    rows.forEach(r => {
      const li = el('li','history-row');
      const text = el('span','history-text', String(r.text));
      const btn = el('button','history-repeat','↻'); btn.type='button';
      btn.setAttribute('aria-label',t('common.retry'));
      btn.addEventListener('click', () => { api('/api/control',{action:'repeat',id:r.id}).then(displayStatus).catch(e => showToast(e.message)); });
      li.append(text, btn); list.append(li);
    });
  }
  function renderUsage(data) {
    const today = document.getElementById('usage-today');
    if (!today) return;
    const fmt = n => Number(n || 0).toLocaleString(fmtLocale());
    const u = (data && data.usage) || {today:0, month:0, limit:null};
    today.textContent = t('settings.usage.today', {n:fmt(u.today)});
    document.getElementById('usage-month').textContent = u.limit
      ? t('settings.usage.month_limit', {n:fmt(u.month), limit:fmt(u.limit)})
      : t('settings.usage.month', {n:fmt(u.month)});
    const bar = document.getElementById('usage-bar'), fill = bar.firstElementChild;
    bar.hidden = !u.limit;
    if (u.limit) motionProgress(fill, u.month / u.limit);
    const history = Array.isArray(data && data.history) ? data.history : [];
    const left = document.getElementById('usage-left');
    const lengths = history.map(r => String((r && r.text) || '').length).filter(n => n > 0);
    if (!u.limit || !lengths.length) { left.hidden = true; return; }
    const average = lengths.reduce((a, b) => a + b, 0) / lengths.length;
    left.textContent = t('settings.usage.remaining', {n:fmt(Math.max(0, Math.floor((u.limit - u.month) / average)))});
    left.hidden = false;
  }
  function buildUsageSettings() {
    const box = $('#usage-settings');
    if (!box || $('#char-limit')) return;
    const row = el('div','audio-row');
    const label = el('label',null,t('settings.usage.monthly_limit')); label.htmlFor = 'char-limit';
    const limit = el('input'); limit.type='number'; limit.min='1'; limit.step='1'; limit.id='char-limit';
    limit.placeholder=t('settings.usage.no_limit'); limit.setAttribute('aria-label',t('settings.usage.monthly_limit'));
    limit.value = prefsState.monthly_char_limit ? String(prefsState.monthly_char_limit) : '';
    limit.addEventListener('change', () => {
      const n = limit.value.trim() === '' ? null : Number(limit.value);
      api('/api/preferences',{monthly_char_limit:n}).then(p => { prefsState.monthly_char_limit=p.monthly_char_limit; })
        .catch(e => { showToast(e.message); limit.value = prefsState.monthly_char_limit ? String(prefsState.monthly_char_limit) : ''; });
    });
    row.append(label, limit);
    box.append(row);
  }
  function formatGain(db){ const sign = db>0?'+':''; return sign+db+" " + t('common.db'); }
  function fillDevices(select, names, stored, kind) {
    const list = Array.isArray(names) ? names : [];
    const name = kind === 'input' ? deviceInfo.default_input : deviceInfo.default_output;
    const autoText = kind === 'input'
      ? (name ? t('settings.audio.device_auto_input', {name}) : t('settings.audio.device_auto_input_unknown'))
      : (name ? t('settings.audio.device_auto_output', {name}) : t('settings.audio.device_auto_output_none'));
    select.replaceChildren();
    const auto = el('option', null, autoText); auto.value = ''; select.append(auto);
    list.forEach(device => { const o = el('option', null, device); o.value = device; select.append(o); });
    if (stored != null && !list.includes(stored)) {
      const o = el('option', null, t('settings.audio.device_missing', {name: stored})); o.value = stored; select.append(o);
    }
    select.value = stored ?? '';
  }
  function refillDevices() {
    fillDevices($('#input-dev'), deviceInfo.inputs, $('#input-dev').value || null, 'input');
    fillDevices($('#output-dev'), deviceInfo.outputs, $('#output-dev').value || null, 'output');
    updateRouteChip();
  }
  async function refreshDevices() {
    const data = await api('/api/voices');
    deviceInfo = Object.assign({}, deviceInfo, data.devices || {});
    const prefs = data.preferences || {};
    prefsState.input_device = prefs.input_device ?? null;
    prefsState.output_device = prefs.output_device ?? null;
    fillDevices($('#input-dev'), deviceInfo.inputs, prefsState.input_device, 'input');
    fillDevices($('#output-dev'), deviceInfo.outputs, prefsState.output_device, 'output');
    updateRouteChip();
    fillMonitors($('#monitor-device'), Array.isArray(deviceInfo.monitors) ? deviceInfo.monitors : lastMonitors);
  }
  const metaState = { pending:false, tries:0, timer:null, ids:new Set(), requested:false };
  function startMetadataPoll() {
    if (metaState.requested) return;
    metaState.requested = true;
    metaState.tries = 0;
    const tick = () => {
      metaState.timer = null;
      if (!metaState.pending && metaState.tries > 0) return;
      if (metaState.tries >= 60) { metaState.pending = false; return; }
      metaState.tries++;
      api('/api/voice_metadata').then(data=>{
        const items = Array.isArray(data && data.items) ? data.items : [];
        const map = new Map(); items.forEach(it=>{ if (it && it.id) map.set(String(it.id).toLowerCase(), it); });
        if (map.size) {
          voices = voices.map(v => {
            const extra = map.get(v.id);
            if (!extra) return v;
            const merged = Object.assign({}, v, extra);
            merged.name = v.name;
            return normalize(merged) || v;
          });
          results = results.map(v => {
            const extra = map.get(v.id);
            if (!extra) return v;
            const merged = Object.assign({}, v, extra);
            merged.name = v.name;
            return normalize(merged) || v;
          });
          if (selected) {
            const extra = map.get(selected.id);
            if (extra) {
              const merged = Object.assign({}, selected, extra);
              merged.name = selected.name;
              selected = normalize(merged) || selected;
              updateMainCard(selected);
            }
          }
          renderList();
        }
        metaState.pending = !!(data && data.pending);
        if (metaState.pending) {
          metaState.timer = setTimeout(tick, 500);
        }
      }).catch(()=>{ /* silent: keep initials */ });
    };
    metaState.timer = setTimeout(tick, 500);
  }
  function maybeRequestMetadataForNew(id) {
    if (!id) return;
    if (metaState.ids.has(id)) return;
    metaState.ids.add(id);
    api('/api/voice_metadata?ids=' + encodeURIComponent(id)).catch(()=>{});
  }
  async function loadInitial() {
    try {
      const data=await api('/api/voices');
      APP_PLATFORM = data.platform === 'win' ? 'win' : 'mac';
      await loadLocale(data.language || 'en');
      applyI18n();
      if (APP_PLATFORM === 'win') document.querySelectorAll('.n-toolbar,.n-traffic-spacer').forEach(node => node.classList.add('pywebview-drag-region'));
      voices=unique(clean(data.items));
      speechLanguages = Array.isArray(data.speech_languages) ? data.speech_languages : [{id:'en-US'}, {id:'ru-RU'}];
      speechLanguage = data.speech_language || 'en-US';
      const prefs=data.preferences||{}; favs.clear();(prefs.favorite_ids||[]).forEach(id=>favs.add(id));
      prefsState = Object.assign({}, prefsState, prefs);
      $('#update-auto').checked = prefsState.update_auto;
      if (typeof prefsState.output_gain_db === 'number') audioQueue.confirmed.gain = prefsState.output_gain_db;
      audioQueue.confirmed.norm = !!prefsState.normalize_loudness;
      audioQueue.confirmed.monEnabled = !!prefsState.monitor_enabled;
      audioQueue.confirmed.monDevice = prefsState.monitor_device || null;
      if (typeof prefsState.monitor_gain_db === 'number') audioQueue.confirmed.monGain = prefsState.monitor_gain_db;
      if (typeof prefsState.input_gain_db === 'number') audioQueue.confirmed.inputGain = prefsState.input_gain_db;
      audioQueue.gainLatest = audioQueue.confirmed.gain;
      audioQueue.normLatest = audioQueue.confirmed.norm;
      audioQueue.monEnabledLatest = audioQueue.confirmed.monEnabled;
      audioQueue.monDeviceLatest = audioQueue.confirmed.monDevice;
      audioQueue.monGainLatest = audioQueue.confirmed.monGain;
      audioQueue.inputGainLatest = audioQueue.confirmed.inputGain;
      // Remember the available monitor list so renderMode can re-populate the
      // select after the DOM is built.
      lastMonitors = Array.isArray(data.devices?.monitors) ? data.devices.monitors.slice() : [];
      deviceInfo = Object.assign({}, deviceInfo, data.devices || {});
      fillDevices($('#input-dev'), deviceInfo.inputs, prefsState.input_device ?? null, 'input');
      fillDevices($('#output-dev'), deviceInfo.outputs, prefsState.output_device ?? null, 'output');
      voices.forEach(v=>metaState.ids.add(v.id));
      buildAudioSettings();
      syncLanguageControls();
      // Build the mode panel BEFORE any device fill so #monitor-device exists.
      renderMode();
      if(voices.length)await select(voices.find(v=>v.id===prefsState.voice_id)||voices[0],false);else renderList();
      const initialStatus = await api('/api/status');
      audioState = Object.assign({}, audioState, initialStatus);
      displayStatus(audioState);
      startMetadataPoll();
      buildApiKeysPane();
      await refreshKeys();
      if (prefsState.onboarding_completed === false) window.aiVoiceOnboarding?.show(1);
      setTimeout(() => {
        if (onboardingOpen()) return;
        api('/api/update-check', {force:false}).then(result => {
          if (result.status === 'available') showUpdate(result);
        }).catch(() => {});
      }, 3000);
    } catch(error) { list.replaceChildren(el('div','error-msg',error.message)); }
  }
  async function search(more=false) {
    const query=queries.catalog.trim();
    const ticket=++generation;
    if(!more){results=[];hasMore=false;page=1;} busy=true;searchError='';renderList();
    const nextPage = more ? page+1 : 1;
    try {
      const url='/api/search?q='+encodeURIComponent(query)+'&page='+nextPage+'&gender='+encodeURIComponent(filters.gender)+'&sort_by='+encodeURIComponent(filters.sort_by);
      const data=await api(url);
      if(ticket!==generation||tab!=='catalog')return;
      results=unique([...(more?results:[]),...clean(data.items)]);hasMore=Boolean(data.has_more);page=data.page||nextPage;
    } catch(error) { if(ticket!==generation||tab!=='catalog')return;searchError=error.message||t('errors.fish_catalog'); }
    finally { if(ticket===generation){busy=false;renderList();} }
  }
  document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{ ttsSelect={on:false,ids:new Set()};generation++;busy=false;tab=b.dataset.tab; input.value=queries[tab];document.querySelectorAll('.tab').forEach(t=>{t.classList.toggle('active',t===b);t.setAttribute('aria-selected',String(t===b));});renderList();if(tab==='catalog'){page=1;search(false);} }));
  input.addEventListener('input',()=>{queries[tab]=input.value;if(tab==='mine')renderList();else{generation++;busy=false;searchError='';results=[];hasMore=false;page=1;renderList();}});
  $('#search-btn').addEventListener('click',()=>{ if(tab==='catalog'){page=1;search(false);} else { renderList(); } });
  input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();if(tab==='catalog'){page=1;search(false);}else{$('#search-btn').click();}}});
  favButton.addEventListener('click',()=>{onlyFavs=!onlyFavs;renderList();});
  function onFilterChange(){ if(tab!=='catalog') return; page=1; search(false); }
  $('#ui-language-select').addEventListener('change', event => setLanguage(event.target.value));
  $('#catalog-language-filter').addEventListener('change', async event => {
    const select = event.target;
    select.disabled = true;
    try {
      const prefs = await api('/api/preferences', {catalog_language:select.value});
      prefsState.catalog_language = prefs.catalog_language;
      onFilterChange();
    } catch (error) { showToast(error.message); select.value = prefsState.catalog_language || I18N.lang; }
    finally { select.disabled = false; }
  });
  document.querySelectorAll('[data-filter]').forEach(node=>{
    node.addEventListener('change',event=>{
      const target = event.target;
      if(!target||!target.dataset||!target.dataset.filterKey) return;
      const key = target.dataset.filterKey;
      const value = target.value;
      filters[key] = value;
      page=1;
      if(tab==='catalog') search(false);
    });
  });
  $('#select-btn').addEventListener('click',()=>{ttsSelect.on=!ttsSelect.on;if(!ttsSelect.on)ttsSelect.ids.clear();renderList();});
  $('#select-all-btn').addEventListener('click',()=>{const candidates=ttsCandidates(),all=candidates.every(v=>ttsSelect.ids.has(v.id));candidates.forEach(v=>all?ttsSelect.ids.delete(v.id):ttsSelect.ids.add(v.id));renderList();});
  $('#select-delete-btn').addEventListener('click',event=>armBulkDelete(event.currentTarget,ttsSelect.ids.size,async()=>{
    const res=await api('/api/remove_voices',{ids:[...ttsSelect.ids]});
    voices=unique(clean(res.items));favs.clear();(res.preferences.favorite_ids||[]).forEach(id=>favs.add(id));prefsState.voice_id=res.preferences.voice_id;
    if(selected&&!voices.some(v=>v.id===selected.id)){selected=null;await select(voices.find(v=>v.id===prefsState.voice_id)||voices[0],false);}
    ttsSelect={on:false,ids:new Set()};renderList();showTranslatedToast(() => t('common.deleted_prefix') + " "+res.removed);
  }));
  list.addEventListener('click',async event=>{
    if(ttsSelect.on){const row=event.target.closest('.voice-row');if(row){const id=row.dataset.id;ttsSelect.ids.has(id)?ttsSelect.ids.delete(id):ttsSelect.ids.add(id);renderList();return;}}
    if(event.target.closest('a'))return;
    const star=event.target.closest('[data-star]'); if(star){ const id=star.dataset.star;const wasFav=favs.has(id);wasFav?favs.delete(id):favs.add(id);save(STORE_FAV,[...favs],()=>{wasFav?favs.add(id):favs.delete(id);renderList();});renderList();return; }
    const add=event.target.closest('.event-add');if(add){add.disabled=true;try{const v=await api('/api/add_voice',{id:add.dataset.id});voices=unique([...voices,normalize(v)]);metaState.ids.add(add.dataset.id);maybeRequestMetadataForNew(add.dataset.id);renderList();showTranslatedToast(() => t('catalog.voice_added'));}catch(error){showToast(error.message);add.disabled=false;}return;}
    const rm=event.target.closest('.event-remove');if(rm){const id=rm.dataset.remove;rm.disabled=true;try{const removed=await api('/api/remove_voice',{id});const prefs=removed.preferences||removed;voices=voices.filter(x=>x.id!==id);results=results.filter(x=>x.id!==id);favs.delete(id);if(prefs&&prefs.favorite_ids){favs.clear();prefs.favorite_ids.forEach(x=>favs.add(x));}if(selected&&selected.id===id){selected=null;const next=voices.find(v=>v.id===(prefs&&prefs.voice_id))||voices[0];if(next){await select(next,false);}else{renderList();}}else{renderList();}showTranslatedToast(() => t('catalog.voice_deleted'));}catch(error){showToast(error.message);}finally{rm.disabled=false;}return;}
    if(event.target.closest('#load-more-btn')){if(!busy)search(true);return;}
    const row=event.target.closest('.voice-row');if(row){const v=(tab==='mine'?voices:results).find(x=>x.id===row.dataset.id);if(v)select(v);}
  });
  list.addEventListener('keydown',event=>{if(event.target.closest('button,a'))return;if(event.key==='Enter'||event.key===' '){const row=event.target.closest('.voice-row');if(row){event.preventDefault();row.click();}}});
  document.addEventListener('click',event=>{
    const modeBtn=event.target.closest('.mode-btn'); if(!modeBtn) return;
    if(commandBusy||mode===modeBtn.dataset.mode) return;
    const target = async () => {
      try { if(audioState.active) await command({action:'stop'}); ttsSelect={on:false,ids:new Set()};vcSelect={on:false,ids:new Set()};mode=modeBtn.dataset.mode; renderList();vcRenderList();renderMode(); displayStatus(audioState); } catch(error){ showToast(error.message); }
    };
    target();
  });
  async function audioAction(){
    if(mode==='vc'){return vcAudioAction();}
    if(mode==='text'&&!text.trim()){showTranslatedToast(() => t('main.enter_text'));return;}
    try{
      const action=mode==='text'?'speak':(audioState.active&&audioState.mode==='mic'?'stop':'start');
      await command({action,mode,voice_id:selected?.id||null,input_device:$('#input-dev').value||null,output_device:$('#output-dev').value||null,...(action==='speak'?{text}: {})});
    }catch{}
  }
  $('#start-btn').addEventListener('click',audioAction);
  $('#stop-btn').addEventListener('click',()=>command({action:'stop'}).catch(()=>{}));
  [$('#input-dev'),$('#output-dev')].forEach(select=>select.addEventListener('change',()=>{updateRouteChip();return api('/api/preferences',{input_device:$('#input-dev').value||null,output_device:$('#output-dev').value||null}).catch(error=>showToast(error.message));}));
  document.addEventListener('keydown',event=>{if(onboardingOpen())return;if((event.metaKey||event.ctrlKey)&&event.key==='Enter'&&mode==='text'){event.preventDefault();audioAction();}});
  let settingsOpener = null;
  const SETTINGS_SECTIONS = ['general','api','audio','asr','keys','storage','usage'];
  const settingsHooks = {};
  function settingsSection(name) {
    const section = SETTINGS_SECTIONS.includes(name) ? name : 'general';
    document.querySelectorAll('.settings-nav-item').forEach(b => {
      const on = b.dataset.section === section;
      b.classList.toggle('active', on);
      if (on) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current');
    });
    const previous = document.querySelector('.settings-pane:not([hidden])');
    const next = document.querySelector('.settings-pane[data-pane="' + section + '"]');
    const finishMotion = previous && previous !== next ? motionCrossfade(previous) : () => {};
    document.querySelectorAll('.settings-pane').forEach(p => { p.hidden = p.dataset.pane !== section; });
    finishMotion(next);
    try { localStorage.setItem('avr_settings_section', section); } catch {}
    if (settingsHooks[section]) settingsHooks[section]();
    return section;
  }
  // ---- Storage section ----
  const ACCENT_TOKENS = ['var(--accent)','var(--green)','var(--orange)','var(--red)'];
  const STORAGE_COLORS = ACCENT_TOKENS.concat(ACCENT_TOKENS.map(c => 'color-mix(in srgb, ' + c + ' 60%, transparent)'));
  let storageData = null, storageLoading = false, storageExpanded = false;
  function formatBytes(n) {
    n = Number(n) || 0;
    const dec = (v, digits) => v.toLocaleString(fmtLocale(), {minimumFractionDigits:digits, maximumFractionDigits:digits});
    if (n >= 1e9) return dec(n / 1e9, 1) + " " + t('common.gb');
    if (n >= 1e6) return Math.round(n / 1e6) + " " + t('common.mb');
    if (n >= 1e3) return Math.round(n / 1e3) + " " + t('common.kb');
    return n + " " + t('common.bytes');
  }
  function confirmClick(button, idleLabel, action) {
    button.addEventListener('click', async () => {
      if (button.dataset.armed === '1') {
        clearTimeout(button._armTimer);
        button.dataset.armed = ''; button.disabled = true;
        try { await action(); } catch (error) { showToast(error.message); }
        finally { if (button.isConnected) { button.disabled = false; button.textContent = idleLabel; } }
        return;
      }
      button.dataset.armed = '1'; button.textContent = t('common.confirm');
      button._armTimer = setTimeout(() => { button.dataset.armed = ''; button.textContent = idleLabel; }, 3000);
    });
  }
  async function storageClear(category, itemId) {
    try { storageData = await api('/api/storage/clear', itemId ? {category, item_id:itemId} : {category}); }
    catch (error) { loadStorage(); throw error; }
    renderStorage();
    if (category === 'vc_engine') vcLoadVoices();
  }
  function renderStorageSkeleton() {
    $('#storage-total').textContent = t('settings.storage.counting');
    $('#storage-bar').replaceChildren();
    const list = $('#storage-list'); list.replaceChildren();
    for (let i = 0; i < 3; i++) list.append(el('div','storage-skeleton'));
  }
  function renderStorage() {
    const data = storageData;
    if (!data) return;
    $('#storage-error').hidden = true;
    $('#storage-total').textContent = t('settings.storage.total', {size:formatBytes(data.total)});
    const bar = $('#storage-bar');
    const oldTracks = new Map(Array.from(bar.children).map(track => [track.dataset.cat, track]));
    const list = $('#storage-list'); list.replaceChildren();
    const cats = data.categories || [];
    const visible = cats.filter(c => data.total > 0 && c.bytes / data.total >= 0.01);
    const visibleBytes = visible.reduce((total, c) => total + c.bytes, 0);
    cats.forEach((c, i) => {
      const color = STORAGE_COLORS[i % STORAGE_COLORS.length];
      if (data.total > 0 && c.bytes / data.total >= 0.01) {
        let track = oldTracks.get(c.id);
        const fresh = !track;
        if (!track) { track = el('span','motion-storage-track'); track.dataset.cat = c.id; track.append(el('i','storage-seg')); }
        oldTracks.delete(c.id);
        const seg = track.firstElementChild, fraction = c.bytes / visibleBytes;
        seg.dataset.cat = c.id; seg.style.background = color;
        track.style.width = (fraction * 100) + '%';
        bar.append(track);
        if (fresh && !motionMedia.matches) {
          motionProgress(seg, 0);
          requestAnimationFrame(() => requestAnimationFrame(() => motionProgress(seg, 1)));
        } else motionProgress(seg, 1);
      }
      const row = el('div','storage-row'); row.dataset.cat = c.id;
      const dot = el('span','storage-dot'); dot.style.background = color;
      const text = el('div','storage-text');
      const label = el('span','storage-label', c.label); text.append(label);
      if (c.note) text.append(el('span','storage-note', c.note));
      const size = el('span','storage-size', formatBytes(c.bytes));
      row.append(dot, text, size);
      if (c.id === 'vc_voices') {
        const toggle = el('button','storage-toggle', storageExpanded ? t('common.hide') : t('common.show')); toggle.type = 'button';
        toggle.setAttribute('aria-expanded', String(storageExpanded)); toggle.dataset.toggle = 'vc_voices';
        toggle.disabled = !(c.items && c.items.length);
        toggle.addEventListener('click', () => { storageExpanded = !storageExpanded; renderStorage(); });
        row.append(toggle);
      } else if (c.clearable) {
        const label = c.id === 'vc_engine' ? t('engine.remove') : t('common.clear');
        const btn = el('button','storage-clear',label); btn.type = 'button'; btn.dataset.clear = c.id;
        confirmClick(btn, label, () => storageClear(c.id));
        row.append(btn);
      }
      list.append(row);
      if (c.id === 'vc_voices' && storageExpanded && c.items && c.items.length) {
        c.items.forEach(item => {
          const sub = el('div','storage-row storage-sub'); sub.dataset.item = item.id;
          const name = el('span','storage-label', item.label);
          const sz = el('span','storage-size', formatBytes(item.bytes));
          const del = el('button','storage-clear',t('common.delete_prefix')); del.type = 'button'; del.dataset.deleteItem = item.id;
          confirmClick(del, t('common.delete_prefix'), () => storageClear('vc_voices', item.id));
          sub.append(name, sz, del); list.append(sub);
        });
        const all = el('div','storage-row storage-sub');
        const delAll = el('button','storage-clear storage-danger',t('settings.storage.delete_all_voices')); delAll.type = 'button'; delAll.dataset.clear = 'vc_voices';
        confirmClick(delAll, t('settings.storage.delete_all_voices'), () => storageClear('vc_voices'));
        all.append(delAll); list.append(all);
      }
    });
    oldTracks.forEach(track => track.remove());
  }
  async function loadStorage() {
    if (storageLoading) return;
    storageLoading = true;
    if (!storageData) renderStorageSkeleton(); else renderStorage();
    try { storageData = await api('/api/storage'); renderStorage(); }
    catch (error) {
      $('#storage-list').replaceChildren(); $('#storage-total').textContent = '';
      const box = $('#storage-error'); box.textContent = error.message; box.hidden = false;
    } finally { storageLoading = false; }
  }
  settingsHooks.storage = loadStorage;
  function openSettings(section) {
    vcFillTextVoices();
    $('#update-auto').checked = prefsState.update_auto;
    api('/api/status').then(status => { $('#update-version').textContent = status.version || ''; }).catch(() => {});
    let target = section;
    if (!target) { try { target = localStorage.getItem('avr_settings_section'); } catch {} }
    if ($('#settings-sheet').hidden || $('#settings-sheet').inert) settingsOpener = document.activeElement;
    motionSheet($('#settings-sheet'), true);
    settingsSection(target);
    const active = document.querySelector('.settings-nav-item.active');
    if (!$('#settings-sheet').contains(document.activeElement)) active.focus();
  }
  function closeSettings() {
    if ($('#settings-sheet').hidden) return;
    motionSheet($('#settings-sheet'), false);
    if (settingsOpener && settingsOpener.isConnected) settingsOpener.focus();
  }
  const onboardingOpen = () => { const overlay = document.getElementById('onboarding'); return !!overlay && !overlay.hidden; };
  window.aiVoiceOpenSettings = section => { if (!onboardingOpen()) openSettings(typeof section === 'string' ? section : undefined); };
  $('#run-setup-btn').addEventListener('click', () => { closeSettings(); window.aiVoiceOnboarding?.show(1); });
  $('#fish-banner-btn').addEventListener('click', () => openSettings('api'));
  // ---- Updates ----
  let updateRelease = null, updateResult = null, updateChecking = false, updateStatusTimer = null;
  const UPDATE_ERRORS = {network:'update.settings.error_network', github:'update.settings.error_github'};
  function renderUpdateCopy() {
    renderUpdateInstall();
    $('#update-check-btn').textContent = t(updateChecking ? 'update.settings.checking' : 'update.settings.check');
    const status = $('#update-check-status');
    status.textContent = '';
    if (updateResult?.status === 'current') status.textContent = t('update.settings.current');
    else if (updateResult?.status === 'available') status.textContent = t('update.settings.available', {version:updateResult.version});
    else if (updateResult?.status === 'error') status.textContent = t(UPDATE_ERRORS[updateResult.error] || UPDATE_ERRORS.network);
  }
  // Install: poll the job and touch only text/disabled, never rebuild the banner.
  let updateJob = {state:'idle'}, updatePollTimer = null;
  function renderUpdateInstall() {
    const busy = updateJob.state === 'downloading' || updateJob.state === 'installing';
    const button = $('#update-install'), text = $('#update-banner-text');
    button.disabled = busy;
    $('#update-skip').disabled = busy; $('#update-close').disabled = busy;
    if (updateJob.state === 'downloading') button.textContent = t('update.banner.downloading', {percent:updateJob.percent || 0});
    else if (updateJob.state === 'installing') button.textContent = t('update.banner.installing');
    else button.textContent = t(updateJob.state === 'error' ? 'update.banner.retry' : 'update.banner.install');
    if (updateJob.state === 'error') text.textContent = t(`update.error.${updateJob.error}`);
    else if (updateRelease) text.textContent = t('update.banner.text', {version:updateRelease.version});
  }
  async function pollUpdate() {
    clearTimeout(updatePollTimer);
    try { updateJob = await api('/api/update-status'); } catch { updateJob = {state:'error', error:'network'}; }
    renderUpdateInstall();
    if (updateJob.state === 'downloading' || updateJob.state === 'installing') updatePollTimer = setTimeout(pollUpdate, 500);
  }
  $('#update-install').addEventListener('click', async () => {
    updateJob = {state:'downloading', percent:0}; renderUpdateInstall();
    try { await api('/api/update-install', {}); } catch (error) { updateJob = {state:'error', error:'failed'}; renderUpdateInstall(); showToast(error.message); return; }
    pollUpdate();
  });
  function showUpdate(result) {
    updateRelease = result;
    $('#update-download').href = result.url;
    renderUpdateCopy();
    $('#update-banner').hidden = false;
  }
  function hideUpdate() { $('#update-banner').hidden = true; }
  $('#update-close').addEventListener('click', hideUpdate);
  $('#update-skip').addEventListener('click', async () => {
    if (!updateRelease) return;
    const button = $('#update-skip'); button.disabled = true;
    try { await api('/api/update-skip', {version:updateRelease.version}); hideUpdate(); }
    catch (error) { showToast(error.message); }
    finally { button.disabled = false; }
  });
  $('#update-auto').addEventListener('change', async () => {
    const checkbox = $('#update-auto'); checkbox.disabled = true;
    try { const prefs = await api('/api/preferences', {update_auto:checkbox.checked}); prefsState.update_auto = prefs.update_auto; }
    catch (error) { showToast(error.message); }
    finally { checkbox.checked = prefsState.update_auto; checkbox.disabled = false; }
  });
  $('#update-check-btn').addEventListener('click', async () => {
    const button = $('#update-check-btn'); button.disabled = true;
    clearTimeout(updateStatusTimer);
    updateChecking = true; updateResult = null; renderUpdateCopy();
    try { updateResult = await api('/api/update-check', {force:true}); }
    catch { updateResult = {status:'error', error:'network'}; }
    updateChecking = false;
    if (updateResult.status === 'available') showUpdate(updateResult);
    renderUpdateCopy();
    updateStatusTimer = setTimeout(() => {
      updateResult = null; button.disabled = false; renderUpdateCopy();
    }, 6000);
  });
  // ---- API keys ----
  const apiRows = {};
  function syncApiKeyRows() {
    ['fish', 'hf'].forEach(kind => {
      const row = apiRows[kind]; if (!row) return;
      const on = keyState[kind] === true;
      row.chip.className = on ? 'key-chip ok' : 'key-chip';
      row.chip.textContent = t(on ? 'settings.keys_api.connected' : 'settings.keys_api.not_set');
      row.add.textContent = t(on ? 'settings.keys_api.replace' : 'settings.keys_api.add');
      row.remove.hidden = !on;
    });
  }
  function buildApiKeyRow(kind) {
    const row = el('div', 'api-key-row'); row.id = 'api-key-' + kind;
    const head = el('div', 'api-key-head');
    const title = el('span', 'api-key-title', t(kind === 'fish' ? 'settings.keys_api.fish_title' : 'settings.keys_api.hf_title'));
    const chip = el('span', 'key-chip');
    const add = el('button', 'settings-action api-key-add'); add.type = 'button';
    const remove = el('button', 'settings-action api-key-remove', t('settings.keys_api.remove')); remove.type = 'button';
    confirmClick(remove, t('settings.keys_api.remove'), () => api('/api/keys/' + kind + '/remove', {}).then(refreshKeys));
    head.append(title, chip, add, remove);
    row.append(head);
    if (kind === 'hf') {
      const hint = el('p', 'settings-hint', t('settings.keys_api.hf_hint'));
      const link = el('a', 'key-link', t('settings.keys_api.hf_link'));
      link.href = 'https://huggingface.co/settings/tokens'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      hint.append(' ', link);
      row.append(hint);
    }
    const formBox = el('div', 'api-key-form'); formBox.hidden = true;
    row.append(formBox);
    add.addEventListener('click', () => {
      if (!formBox.firstChild) formBox.append(buildKeyForm({kind, connected: false, idPrefix: 'settings-' + kind,
        onChange: state => { if (state === 'ok' || state === 'warning') refreshKeys(); },
        onCancel: () => { formBox.hidden = true; add.focus(); }}));
      formBox.hidden = false;
      formBox.querySelector('.key-input')?.focus();
    });
    apiRows[kind] = {chip, add, remove};
    return row;
  }
  function buildApiKeysPane() {
    $('#api-keys-settings').replaceChildren(buildApiKeyRow('fish'), buildApiKeyRow('hf'));
    syncApiKeyRows();
  }
  async function refreshKeys() {
    try { keyState = await api('/api/keys'); } catch { return; }
    $('#fish-banner').hidden = keyState.fish !== false;
    syncApiKeyRows();
  }
  // ---- Built-in virtual microphone (AI Voice Mic) ----
  const DRIVER_BUSY_TEXT = {install: 'settings.driver.installing', uninstall: 'settings.driver.removing'};
  let driverLastStatus = null;
  async function driverStatus() { return api('/api/driver/status'); }
  async function driverAction(action) {
    await api('/api/driver/' + action, {});
    let status = await driverStatus();
    while (status.job && status.job.state === 'running') {
      await new Promise(resolve => setTimeout(resolve, 1000));
      status = await driverStatus();
    }
    await refreshDevices().catch(() => {});
    return status;
  }
  function renderDriverRow(status) {
    driverLastStatus = status;
    const row = $('#driver-row'), caption = $('#driver-caption');
    if (!status || status.available !== true) { row.hidden = true; caption.hidden = true; return; }
    row.hidden = false;
    const job = status.job || {};
    const running = job.state === 'running';
    let textKey = 'settings.driver.installed';
    if (!status.installed) textKey = 'settings.driver.not_installed';
    else if (!status.device_present) textKey = 'settings.driver.not_loaded';
    if (job.state === 'failed') textKey = 'settings.driver.failed';
    if (running) textKey = DRIVER_BUSY_TEXT[job.action] || 'settings.driver.installing';
    const actions = $('#driver-actions');
    actions.replaceChildren();
    const text = el('span', '', t(textKey)); text.id = 'driver-status';
    actions.append(text);
    const run = async action => {
      renderDriverRow({...status, job: {state: 'running', action}});
      try { renderDriverRow(await driverAction(action)); }
      catch (error) { showToast(error.message); renderDriverRow(await driverStatus().catch(() => driverLastStatus)); }
    };
    const button = label => { const b = el('button', 'btn-secondary', label); b.type = 'button'; b.disabled = running; return b; };
    if (!status.installed) {
      caption.hidden = false;
      const install = button(t('settings.driver.install'));
      install.addEventListener('click', () => run('install'));
      actions.append(install);
    } else {
      caption.hidden = true;
      if (status.installed_version != null && status.bundled_version != null && status.installed_version !== status.bundled_version) {
        const update = button(t('settings.driver.update'));
        update.addEventListener('click', () => run('install'));
        actions.append(update);
      }
      const remove = button(t('settings.driver.remove'));
      remove.addEventListener('click', () => {
        if (remove.dataset.confirm !== '1') {
          remove.dataset.confirm = '1';
          remove.textContent = t('settings.driver.remove_confirm');
          setTimeout(() => { if (remove.isConnected) { delete remove.dataset.confirm; remove.textContent = t('settings.driver.remove'); } }, 4000);
          return;
        }
        run('uninstall');
      });
      actions.append(remove);
    }
  }
  settingsHooks.audio = () => { driverStatus().then(renderDriverRow).catch(() => {}); };
  window.aiVoiceApp = {setLanguage, openSettings, refreshDevices, refreshKeys, driverAction, driverStatus, prefs: () => ({...prefsState}), platform: () => APP_PLATFORM,
    speech: () => ({languages: speechLanguages, selected: speechLanguage}),
    setSpeechLanguage: value => { speechLanguage = value; prefsState.speech_language = value; syncLanguageControls(); }};
  $('#settings-btn').addEventListener('click', () => openSettings());
  $('#route-chip').addEventListener('click', () => openSettings('audio'));
  const monitorBtn = $('#monitor-btn'), footerTip = $('#footer-tip');
  monitorBtn.addEventListener('click', () => {
    const value = !audioQueue.monEnabledLatest;
    const dev = audioQueue.monDeviceLatest || prefsState.monitor_device || '';
    const outDev = effectiveOutput();
    if (value && (!dev || dev === outDev)) {
      showTranslatedToast(() => t('settings.monitor.choose_device'));
      openSettings('audio');
      return;
    }
    audioQueue.setMonitorEnabled(value);
  });
  monitorBtn.addEventListener('contextmenu', event => { event.preventDefault(); openSettings('audio'); });
  let footerTipTimer = null;
  function showFooterTip() {
    clearTimeout(footerTipTimer); footerTipTimer = null;
    footerTip.textContent = t('main.monitoring');
    footerTip.classList.remove('show');
    footerTip.hidden = false;
    const rect = monitorBtn.getBoundingClientRect();
    const tipRect = footerTip.getBoundingClientRect();
    footerTip.style.left = Math.round(rect.left + rect.width / 2 - tipRect.width / 2) + 'px';
    footerTip.style.top = Math.round(rect.top - tipRect.height - 6) + 'px';
    requestAnimationFrame(() => footerTip.classList.add('show'));
  }
  function hideFooterTip() {
    clearTimeout(footerTipTimer); footerTipTimer = null;
    footerTip.classList.remove('show');
    footerTip.hidden = true;
  }
  monitorBtn.addEventListener('mouseenter', () => { clearTimeout(footerTipTimer); footerTipTimer = setTimeout(showFooterTip, 500); });
  monitorBtn.addEventListener('mouseleave', hideFooterTip);
  document.addEventListener('pointerover', e => { if (!footerTip.hidden && !monitorBtn.contains(e.target)) hideFooterTip(); });
  monitorBtn.addEventListener('mousedown', hideFooterTip);
  let footerTabFocus = false; // show on focus only when the user tabbed to the button
  document.addEventListener('keydown', e => { footerTabFocus = e.key === 'Tab'; }, true);
  document.addEventListener('pointerdown', () => { footerTabFocus = false; }, true);
  monitorBtn.addEventListener('focus', () => { if (footerTabFocus) showFooterTip(); });
  monitorBtn.addEventListener('blur', hideFooterTip);
  window.addEventListener('scroll', hideFooterTip, true);
  const vcGain = $('#vc-output-gain'), vcGainValue = $('#vc-output-gain-value');
  let vcGainDragging = false;
  vcGain.addEventListener('pointerdown', () => { vcGainDragging = true; });
  window.addEventListener('pointerup', () => { vcGainDragging = false; });
  window.addEventListener('pointercancel', () => { vcGainDragging = false; });
  vcGain.addEventListener('input', () => {
    const db = Number(vcGain.value);
    vcGainValue.textContent = formatGain(db).replace('-', '−');
    audioQueue.setGain(db);
  });
  vcGain.addEventListener('dblclick', () => {
    vcGain.value = '0';
    vcGainValue.textContent = formatGain(0);
    audioQueue.setGain(0);
  });
  $('#settings-close').addEventListener('click', closeSettings);
  document.querySelectorAll('.settings-nav-item').forEach(b => b.addEventListener('click', () => settingsSection(b.dataset.section)));
  $('#settings-sheet').addEventListener('click', event => { if (event.target === $('#settings-sheet')) closeSettings(); });
  document.addEventListener('keydown', event => {
    if (onboardingOpen()) return;
    if ((event.metaKey || (APP_PLATFORM === 'win' && event.ctrlKey)) && event.key === ',') { event.preventDefault(); openSettings(); return; }
    if (event.key === 'Escape' && !$('#settings-sheet').hidden) { event.preventDefault(); closeSettings(); }
  });
  $('#settings-sheet').addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const items = Array.from($('#settings-sheet .settings-card').querySelectorAll('button,input,select,[tabindex="0"]')).filter(node => !node.disabled && node.getClientRects().length);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1], inside = items.includes(document.activeElement);
    if (event.shiftKey && (document.activeElement === first || !inside)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !inside)) { event.preventDefault(); first.focus(); }
  });
  setInterval(()=>{if(!commandBusy)api('/api/status').then(displayStatus).catch(()=>{$('#status-line').textContent=t('errors.window_connection');});},1000);
  let vcVoices=[], vcSelectedId=null, vcTraining=null, vcLastTrainState=null, vcLoaded=false, vcLoadError=null, vcSheetOpener=null, vcRuntimeOk=true, vcPanelKey=null, vcDroppedHistory=[], vcDropVoice=null, vcLastDropAt=0, vcTuningIds=new Set();
  const vcPicked={import:[],audio:[]};
  const vcEpochs={fast:50,normal:100,max:200};let vcAudioPreset='none',vcTrainK=2.3,vcSeenDone=null;
  function vcPresetSelect(group,preset){group.querySelectorAll('[data-preset]').forEach(b=>{const active=b.dataset.preset===preset;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});}
  $('#vc-audio-train').addEventListener('click',event=>{const b=event.target.closest('[data-preset]');if(b){vcAudioPreset=b.dataset.preset;vcPresetSelect($('#vc-audio-train'),vcAudioPreset);}});
  function vcEta(voice,preset){return '≈ '+(Math.ceil(vcEpochs[preset]*vcTrainK*(voice.speech_seconds/60)/60)+2)+" " + t('common.minutes');}
  function vcNotifyDone(done){
    if(!done||!['done','failed'].includes(done.state))return;
    const key=JSON.stringify([done.voice_id,done.state,done.at]);if(key===vcSeenDone)return;vcSeenDone=key;
    const translate = () => done.state==='done'?t('vc.trained_notice', {name:done.name||t('common.voice')}):t('vc.training_failed_notice', {name:done.name||t('common.voice')});
    const body=translate();
    nativePost('notify', {title:'AI Voice',body});showTranslatedToast(translate);
  }
  try { vcSelectedId=localStorage.getItem('avr_vc_selected'); } catch {}
  function vcSaveSelection() { try { localStorage.setItem('avr_vc_selected',vcSelectedId||''); } catch {} }
  function vcKindBadge(kind) {
    return {imported:{label:t('vc.kind.imported'),cls:'vc-badge-imported'},zeroshot:{label:t('vc.kind.quick'),cls:'vc-badge-zeroshot'},trained:{label:t('vc.kind.trained'),cls:'vc-badge-trained'}}[kind]||{label:'',cls:''};
  }
  async function vcUpload(url,formData) {
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest(),progress=$('#vc-upload-progress');
      progress.hidden=false;motionProgress(progress.querySelector('i'),0);progress.querySelector('span').dataset.percent='0';progress.querySelector('span').textContent=t('vc.upload_percent', {n:0});
      xhr.open('POST',url);xhr.setRequestHeader('X-AI-Voice-Token',APP_TOKEN);
      xhr.upload.onprogress=event=>{if(event.lengthComputable){const n=Math.round(event.loaded/event.total*100);motionProgress(progress.querySelector('i'),n/100);progress.querySelector('span').dataset.percent=String(n);progress.querySelector('span').textContent=t('vc.upload_percent', {n});}};
      xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300)throw new Error(data.error||t('errors.command_failed'));resolve(data);}catch(error){reject(error);}};
      xhr.onerror=xhr.onabort=()=>reject(new Error(t('errors.audio_connection')));
      xhr.onloadend=()=>{progress.hidden=true;};xhr.send(formData);
    });
  }
  let vcProcessingPoll=false;
  setInterval(()=>{if(!vcProcessingPoll&&vcVoices.some(v=>v.status==='processing')){vcProcessingPoll=true;vcLoadVoices(true).finally(()=>{vcProcessingPoll=false;});}},1000);
  async function vcLoadVoices(skipUnchanged = false) {
    try {
      const data=await api('/api/vc/voices');
      const restartIds=new Set(vcVoices.filter(v=>v.restart_required).map(v=>v.id));
      vcVoices=data.items.map(v=>restartIds.has(v.id)?{...v,restart_required:true}:v); vcTraining=data.training; vcRuntimeOk=data.runtime_ok!==false;
      vcTrainK=data.training?.k||vcTrainK;
      vcNotifyDone(data.last_done);
      audioState.training=data.training;
      if(!vcVoices.some(v=>v.id===vcSelectedId)) vcSelectedId=vcVoices[0]?.id||null;
      vcSaveSelection(); vcLoaded=true; vcLoadError=null; vcRenderList(skipUnchanged);
      if(mode==='vc') { const key=vcPanelSignature(); if(!$('#vc-name')||key!==vcPanelKey)vcRenderPanel($('#mode-panel')); else {vcFillPanel();vcOnStatus(audioState);} }
    } catch(error) {
      if(vcLoaded) { showToast(error.message); return; }
      vcLoadError=error.message; vcRenderList();
    }
  }
  const hf={items:[],page:0,more:false,sort:'downloads',seq:0,downloadSeq:0,loading:false,error:null,failedPage:1,download:{state:'idle'},pollTimer:null,polling:false,announced:null,pending:false};
  const hfBusy=()=>['downloading','importing'].includes(hf.download.state);
  const hfAudio=new Audio(),hfSamples=new Map();
  let vcCheckPending=false;
  let hfPlayingId=null,hfSampleLoading=false,hfPlaySeq=0,hfDetailCard=null,hfDetailSeq=0,hfDetailOpener=null;
  const hfImageUrls=new Set();
  async function hfBlob(url) {
    const response=await fetch(url,{headers:{'X-AI-Voice-Token':APP_TOKEN}});
    if(!response.ok){let message=t('errors.file_load');try{message=(await response.json()).error||message;}catch{}throw new Error(message);}
    return response.blob();
  }
  function hfUpdatePlayer() {
    document.querySelectorAll('[data-hf-play]').forEach(button=>{
      const current=button.dataset.hfPlay===hfPlayingId,loading=current&&hfSampleLoading,playing=current&&!hfAudio.paused;
      button.classList.toggle('vc-ring',loading);
      button.textContent=(loading?'…':playing?'❚❚':'▶')+(button.classList.contains('hf-detail-play')?" " + t('vc.sample_suffix'):'');
      button.setAttribute('aria-label',playing?t('status.training_paused'):t('vc.listen_sample'));
      button.setAttribute('aria-busy',String(loading));
    });
    const time=$('#hf-sample-time');
    const format=value=>{const seconds=Number.isFinite(value)?Math.floor(value):0;return Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');};
    if(time)time.textContent=hfDetailCard?.id===hfPlayingId?format(hfAudio.currentTime)+' / '+format(hfAudio.duration):'0:00 / 0:00';
  }
  function hfStopSample() {
    ++hfPlaySeq;hfSampleLoading=false;hfAudio.pause();hfAudio.removeAttribute('src');hfAudio.load();hfPlayingId=null;hfUpdatePlayer();
  }
  async function hfPlaySample(cardId) {
    if(hfPlayingId===cardId&&hfSampleLoading){hfStopSample();return;}
    if(hfPlayingId===cardId&&!hfAudio.paused){hfAudio.pause();return;}
    if(hfPlayingId===cardId&&hfAudio.src){try{await hfAudio.play();}catch(error){showToast(error.message);}return;}
    hfStopSample();const seq=++hfPlaySeq;hfPlayingId=cardId;hfSampleLoading=true;hfUpdatePlayer();
    try{
      let url=hfSamples.get(cardId);
      if(!url){const blob=await hfBlob('/api/vc/hf/sample?id='+encodeURIComponent(cardId));if(seq!==hfPlaySeq)return;
        url=URL.createObjectURL(blob);hfSamples.set(cardId,url);
        while(hfSamples.size>5){const [oldId,oldUrl]=hfSamples.entries().next().value;URL.revokeObjectURL(oldUrl);hfSamples.delete(oldId);}
      }else{hfSamples.delete(cardId);hfSamples.set(cardId,url);}
      if(seq!==hfPlaySeq)return;
      hfAudio.src=url;hfSampleLoading=false;hfUpdatePlayer();await hfAudio.play();
    }catch(error){if(seq===hfPlaySeq){hfStopSample();showToast(error.message);}}
  }
  ['play','pause','ended','timeupdate','loadedmetadata'].forEach(name=>hfAudio.addEventListener(name,hfUpdatePlayer));
  hfAudio.addEventListener('error',()=>{if(hfPlayingId){hfStopSample();showTranslatedToast(() => t('errors.sample_play'));}});
  function hfPlayButton(card,detail=false) {
    const button=el('button',detail?'hf-detail-play vc-add-btn':'hf-play','▶'+(detail?" " + t('vc.sample_suffix'):''));button.type='button';button.dataset.hfPlay=card.id;
    button.addEventListener('click',event=>{event.stopPropagation();hfPlaySample(card.id);});return button;
  }
  function hfDownloadButton(card) {
    const downloaded=vcVoices.some(v=>v.source?.kind==='hf'&&v.source.repo===card.repo&&v.source.pth===card.files[0].path);
    const active=hfBusy()&&hf.download.id===card.id;
    const button=el('button','hf-download '+(active?'vc-ring':'vc-add-btn'),active?'':downloaded?'✓':t('vc.download'));button.type='button';button.dataset.id=card.id;
    button.disabled=hf.pending||downloaded||(hfBusy()&&!active);
    button.setAttribute('aria-label',(active?t('vc.cancel_download_prefix') + " ":downloaded?t('vc.downloaded_prefix') + " ":t('vc.download') + " ")+card.title);
    if(active){const percent=hf.download.total?Math.min(100,Math.floor(hf.download.bytes/hf.download.total*100)):0;button.style.setProperty('--p',percent+'%');button.append(el('span','',percent+'%'));button.title=hf.download.state==='importing'?t('vc.import_cancel_hint'):t('vc.cancel_download_prefix');}
    button.addEventListener('click',()=>hfToggleDownload(card));return button;
  }
  async function hfToggleDownload(card) {
    if(hf.pending)return;
    const active=hfBusy()&&hf.download.id===card.id;
    hf.pending=true;++hf.downloadSeq;hfRender();
    try{hf.download=await api('/api/vc/catalog/download'+(active?'/cancel':''),active?{}:{id:card.id});await hfPoll();}
    catch(error){showToast(error.message);}
    finally{hf.pending=false;hfRender();}
  }
  function hfDetailDownload() {
    if(!hfDetailCard)return;
    const target=$('#hf-detail-download'),button=hfDownloadButton(hfDetailCard);
    const signature=JSON.stringify([button.className,button.disabled,button.textContent,button.title]);
    if(target.dataset.signature===signature)return;
    const focused=target.contains(document.activeElement);
    target.dataset.signature=signature;target.replaceChildren(button);if(focused)button.focus({preventScroll:true});
  }
  function hfCloseDetail() {
    hfStopSample();++hfDetailSeq;
    const wasOpen=!$('#hf-detail').hidden;$('#hf-detail').hidden=true;hfDetailCard=null;
    hfImageUrls.forEach(url=>URL.revokeObjectURL(url));hfImageUrls.clear();
    $('#hf-detail-images').replaceChildren();$('#hf-detail-description').replaceChildren();
    $('#hf-detail-sample').replaceChildren();$('#hf-detail-download').replaceChildren();delete $('#hf-detail-download').dataset.signature;
    if(wasOpen){const opener=hfDetailOpener?.isConnected?hfDetailOpener:Array.from($('#hf-list').children).find(row=>row.dataset.id===hfDetailOpener?.dataset.id);opener?.focus({preventScroll:true});}
  }
  function hfSpans(target,spans) {
    (spans||[]).forEach(span=>{
      const node=el(span.href?'a':span.b?'strong':span.i?'em':span.code?'code':'span','',span.t||'');
      if(span.href){node.href=span.href;node.target='_blank';node.rel='noopener noreferrer';}target.append(node);
    });
  }
  function hfImage(n,alt,inline=false) {
    const button=el('button',inline?'hf-image hf-inline-image':'hf-image');button.type='button';button.dataset.imageN=String(n);button.setAttribute('aria-label',alt||t('vc.enlarge_image'));if(!alt)button.dataset.i18nAria='vc.enlarge_image';
    const img=el('img');img.alt=alt||'';button.append(img);
    button.addEventListener('click',()=>{const expanded=button.classList.contains('expanded');$('#hf-detail').querySelectorAll('.hf-image.expanded').forEach(b=>b.classList.remove('expanded'));button.classList.toggle('expanded',!expanded);});
    return button;
  }
  function hfRefreshDetailCopy() {
    const card = hfDetailCard;
    if (!card) return;
    const date=card.updated?new Date(card.updated):null;
    const size=card.total_size>=1024**3?(card.total_size/1024**3).toFixed(1)+" " + t('common.gb'):(card.total_size/1024**2).toFixed(1)+" " + t('common.mb');
    $('#hf-detail-meta').textContent=size+' · ↓'+card.downloads+' · ♥'+(card.likes||0)+' · '+(date&&!isNaN(date)?date.toLocaleDateString(fmtLocale()):'—')+' · '+(card.license||t('common.unknown'))+(card.has_index?'':" " + t('vc.no_index_suffix'));
    const description = $('#hf-detail-description');
    if (description.dataset.i18n) description.textContent = t(description.dataset.i18n);
    hfDetailDownload(); hfUpdatePlayer();
    $('#hf-detail').querySelectorAll('[data-image-n]').forEach(node => {
      if (node.dataset.i18nAria) node.setAttribute('aria-label', t(node.dataset.i18nAria));
    });
  }
  async function hfOpenDetail(card,opener) {
    if(!$('#hf-detail').hidden)hfCloseDetail();hfDetailCard=card;hfDetailOpener=opener;const seq=++hfDetailSeq;
    $('#hf-detail-title').textContent=card.title;
    const link=$('#hf-detail-link');link.href='https://huggingface.co/'+card.repo;link.textContent='huggingface.co/'+card.repo+' ↗';
    hfRefreshDetailCopy();
    const sample=$('#hf-detail-sample');sample.replaceChildren();sample.hidden=!card.sample;
    if(card.sample){const time=el('span','vc-readout','0:00 / 0:00');time.id='hf-sample-time';sample.append(hfPlayButton(card,true),time);}
    const images=$('#hf-detail-images'),description=$('#hf-detail-description');images.hidden=true;description.dataset.i18n='vc.description_loading';description.textContent=t('vc.description_loading');hfDetailDownload();
    $('#hf-detail').hidden=false;$('#hf-detail-title').focus();hfUpdatePlayer();
    let data;
    try{data=await api('/api/vc/hf/readme?id='+encodeURIComponent(card.id));}
    catch{if(seq===hfDetailSeq){description.dataset.i18n='vc.no_description';description.textContent=t('vc.no_description');}return;}
    if(seq!==hfDetailSeq)return;
    (data.images||[]).forEach((url,n)=>images.append(hfImage(n,'')));images.hidden=!images.children.length;
    delete description.dataset.i18n; description.replaceChildren();
    if(!data.meaningful){description.dataset.i18n='vc.no_description';description.textContent=t('vc.no_description');}
    else(data.blocks||[]).forEach(block=>{
      if(block.type==='image'){description.append(hfImage(block.n,block.alt,true));return;}
      const tag=block.type==='heading'?'h'+(Math.min(4,Math.max(1,block.level))+2):block.type==='list'?(block.ordered?'ol':'ul'):block.type==='code'?'pre':block.type==='quote'?'blockquote':'p';
      const node=el(tag);
      if(block.type==='list')(block.items||[]).forEach(spans=>{const item=el('li');hfSpans(item,spans);node.append(item);});
      else if(block.type==='code')node.append(el('code','',block.text||''));else hfSpans(node,block.spans);
      description.append(node);
    });
    for(let n=0;n<(data.images||[]).length;n++){
      try{const blob=await hfBlob('/api/vc/hf/image?id='+encodeURIComponent(card.id)+'&n='+n);if(seq!==hfDetailSeq)return;
        const url=URL.createObjectURL(blob);hfImageUrls.add(url);
        $('#hf-detail').querySelectorAll('[data-image-n="'+n+'"] img').forEach(img=>{img.onerror=()=>{img.parentElement.remove();images.hidden=!images.children.length;};img.src=url;});
      }catch{if(seq!==hfDetailSeq)return;$('#hf-detail').querySelectorAll('[data-image-n="'+n+'"]').forEach(node=>node.remove());}
      images.hidden=!images.children.length;
    }
  }
  $('#hf-detail-close').addEventListener('click',hfCloseDetail);
  $('#hf-detail').addEventListener('click',event=>{if(event.target===$('#hf-detail'))hfCloseDetail();});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!$('#hf-detail').hidden){event.preventDefault();const expanded=$('#hf-detail .hf-image.expanded');if(expanded)expanded.classList.remove('expanded');else hfCloseDetail();}
  });
  $('#hf-detail').addEventListener('keydown',event=>{
    if(event.key!=='Tab')return;
    const items=Array.from($('#hf-detail').querySelectorAll('button,a')).filter(node=>!node.disabled&&node.getClientRects().length);
    const first=items[0],last=items[items.length-1];if(!first)return;
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===$('#hf-detail-title'))){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  });
  function hfSetTab(catalog) {
    hfCloseDetail();
    $('#vc-mine').hidden=catalog; $('#vc-catalog').hidden=!catalog;
    [$('#vc-mine-tab'),$('#vc-catalog-tab')].forEach((button,i)=>{const active=Boolean(i)===catalog;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));});
    if(catalog){if(!hf.page&&!hf.loading)hfSearch();hfPoll();}
  }
  function hfPatchProgress() {
    if (!hfBusy() || hf.pending !== false) return false;
    const rings = $('#hf-list').querySelectorAll('.hf-download.vc-ring');
    if (rings.length !== 1) return false;
    const button = rings[0];
    if (!button.matches('.hf-download') || button.dataset.id !== hf.download.id) return false;
    const percent = hf.download.total ? Math.min(100, Math.floor(hf.download.bytes / hf.download.total * 100)) : 0;
    button.style.setProperty('--p', percent + '%');
    button.querySelector('span').textContent = percent + '%';
    button.title = hf.download.state === 'importing' ? t('vc.import_cancel_hint') : t('vc.cancel_download_prefix');
    hfDetailDownload();
    return true;
  }
  function hfRender() {
    const target=$('#hf-list'),focused=target.contains(document.activeElement)?document.activeElement?.closest('.hf-download')?.dataset.id:null;
    const scroll=target.scrollTop,finishMotion=motionRows(target);target.replaceChildren();
    hf.items.forEach(card=>{
      const row=el('div','hf-card');row.dataset.id=card.id;row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label',t('vc.details_prefix') + " "+card.title);
      const info=el('div','hf-info'),name=el('div','hf-title',card.title);
      name.title=card.title;
      const size=card.total_size>=1024**3?(card.total_size/1024**3).toFixed(1)+" " + t('common.gb'):(card.total_size/1024**2).toFixed(1)+" " + t('common.mb');
      const date=card.updated?new Date(card.updated):null;
      const meta=el('div','hf-meta',size+' · ↓'+card.downloads+' · '+(date&&!isNaN(date)?date.toLocaleDateString(fmtLocale()):'—')+(card.has_index?'':" " + t('vc.no_index_suffix')));
      meta.title=meta.textContent;info.append(name,meta);
      const button=hfDownloadButton(card);
      row.append(el('div','hf-avatar',Array.from(card.title||'?')[0].toUpperCase()),info);
      if(card.sample)row.append(hfPlayButton(card));row.append(button);
      row.addEventListener('click',e=>{if(e.target.closest('button'))return;hfOpenDetail(card,row);});
      row.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&!e.target.closest('button')){e.preventDefault();row.click();}});target.append(row);
    });
    finishMotion();target.scrollTop=scroll;hfDetailDownload();hfUpdatePlayer();
    if(focused)Array.from(target.querySelectorAll('.hf-download')).find(b=>b.dataset.id===focused)?.focus({preventScroll:true});
    const message=$('#hf-message');message.replaceChildren();
    if(hf.loading)message.textContent=t('vc.models_searching');
    else if(hf.error){message.append(el('span','',hf.error+' '));const retry=el('button','vc-add-btn',t('common.retry'));retry.type='button';retry.onclick=()=>hfSearch(hf.failedPage);message.append(retry);}
    else if(!hf.items.length)message.textContent=t('common.nothing_found');
    $('#hf-more').hidden=!hf.more||Boolean(hf.error);$('#hf-more').disabled=hf.loading;
  }
  async function hfSearch(page=1) {
    clearTimeout(hf.searchTimer);
    const seq=++hf.seq;hf.loading=true;hf.error=null;hf.failedPage=page;
    if(page===1){hf.items=[];hf.page=0;hf.more=false;}
    hfRender();
    const params=new URLSearchParams({q:$('#hf-search').value.trim(),sort:hf.sort,lang:$('#hf-lang').value,page:String(page)});
    try{const result=await api('/api/vc/hf/search?'+params);if(seq!==hf.seq)return;hf.items=page===1?result.items:hf.items.concat(result.items);hf.page=result.page;hf.more=result.has_more;}
    catch(error){if(seq===hf.seq)hf.error=error.message;}
    finally{if(seq===hf.seq){hf.loading=false;hfRender();}}
  }
  async function hfPoll() {
    clearTimeout(hf.pollTimer);if(hf.polling)return;hf.polling=true;
    const seq=hf.downloadSeq;
    try{
      const download=await api('/api/vc/catalog/download');
      if(seq!==hf.downloadSeq)return;
      hf.download=download;
      const terminal=hf.download.state==='done'||hf.download.state==='error';
      const stamp=terminal?JSON.stringify(hf.download):null;
      if(terminal&&hf.announced!==stamp){hf.announced=stamp;if(hf.download.state==='done'){await vcLoadVoices();showTranslatedToast(() => t('vc.added_to_library'));}else showTranslatedToast(() => hf.download.error||t('errors.model_download'));}
      if(!terminal)hf.announced=null;
      if(!hfPatchProgress())hfRender();
    }catch(error){showToast(error.message);}
    finally{hf.polling=false;if(hfBusy()||!$('#vc-catalog').hidden)hf.pollTimer=setTimeout(hfPoll,700);}
  }
  $('#vc-mine-tab').addEventListener('click',()=>hfSetTab(false));
  $('#vc-catalog-tab').addEventListener('click',()=>hfSetTab(true));
  $('#hf-search').addEventListener('input',()=>{clearTimeout(hf.searchTimer);++hf.seq;hf.searchTimer=setTimeout(()=>hfSearch(),400);});
  $('#hf-search').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();hfSearch();}});
  $('#hf-lang').addEventListener('change',()=>hfSearch());
  document.querySelectorAll('[data-hf-sort]').forEach(button=>button.addEventListener('click',()=>{hf.sort=button.dataset.hfSort;document.querySelectorAll('[data-hf-sort]').forEach(b=>{const active=b===button;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});hfSearch();}));
  $('#hf-more').addEventListener('click',()=>hfSearch(hf.page+1));

  function vcRenderList(skipUnchanged = false) {
    const target=$('#vc-list');
    // Keep live CSS animations attached during identical polls. Explicit UI
    // actions still rebuild, including Escape/blur that dismiss the rename input.
    const motionKey=JSON.stringify([vcVoices,vcTraining,vcSelectedId,vcSelect.on,
      [...vcSelect.ids],vcLoaded,vcLoadError,vcTrainK]);
    if(skipUnchanged&&target._motionRenderKey===motionKey)return;
    target._motionRenderKey=motionKey;
    const finishMotion=motionRows(target,new Set(vcVoices.map(v=>v.id))); target.replaceChildren();
    $('#vc-select-btn').hidden=!vcLoaded||!vcVoices.length;$('#vc-select-btn').textContent=vcSelect.on?t('common.done'):t('common.select_prefix');
    $('.vc-sidebar-head .eyebrow').textContent=vcSelect.on?t('common.selected_prefix') + " "+vcSelect.ids.size:t('settings.storage.my_voices');
    $('#vc-add-btn').hidden=vcSelect.on;$('#vc-select-bar').hidden=!vcSelect.on;
    const deleteBtn=$('#vc-select-delete-btn'),count=String(vcSelect.ids.size);
    if(!vcSelect.on||deleteBtn.dataset.count!==count){clearTimeout(deleteBtn._armTimer);delete deleteBtn.dataset.armed;}
    deleteBtn.dataset.count=count;
    deleteBtn.textContent=deleteBtn.dataset.armed==='1'?tp('catalog.delete_voices', Number(count)):t('common.delete_count_prefix')+count+')';
    deleteBtn.disabled=vcSelect.ids.size===0;
    $('#vc-select-all-btn').textContent=vcVoices.length&&vcVoices.every(v=>vcSelect.ids.has(v.id))?t('common.deselect'):t('common.all');
    if(!vcLoaded) {
      if(!vcLoadError) { target.append(el('div','empty-state',t('vc.voices_loading'))); return; }
      const box=el('div','empty-state vc-list-error'),retry=el('button','vc-add-btn vc-retry',t('common.retry'));retry.type='button';
      retry.addEventListener('click',()=>{vcLoadError=null;vcRenderList();vcLoadVoices();});
      box.append(el('div','',vcLoadError),retry); target.append(box); return;
    }
    if(!vcVoices.length) { target.append(el('div','empty-state',t('vc.empty_library'))); finishMotion(); return; }
    vcVoices.forEach(v=>{
      const card=el('div','vc-card'+(v.id===vcSelectedId?' selected':''));
      card.dataset.id=v.id; card.setAttribute('role','option'); card.setAttribute('aria-selected',String(v.id===vcSelectedId)); card.tabIndex=0;
      if(vcSelect.on){card.classList.add('selecting');card.classList.toggle('checked',vcSelect.ids.has(v.id));card.append(selectionCheck(v.name,vcSelect.ids.has(v.id)));}
      const info=el('div','voice-info'),badge=v.status==='processing'?{label:t('vc.processing'),cls:'vc-badge-processing'}:v.status==='error'?{label:t('status.error'),cls:'vc-badge-error'}:vcKindBadge(v.kind);
      const vcName=el('div','voice-name',v.name); vcName.title=v.name;
      info.append(vcName,el('span','vc-badge '+badge.cls,badge.label));
      if(v.source?.kind==='hf'){const source=el('span','hf-source','HF · '+v.source.repo);source.title='Hugging Face: '+v.source.repo;info.append(source);}
      if(v.status==='processing'){const bar=el('div','vc-bar vc-indeterminate');bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',t('vc.audio_processing'));bar.append(el('i'));info.append(bar,el('div','vc-hint',v.stage==='slice'?t('vc.slicing'):t('vc.converting')));}
      if(v.status==='error')info.append(el('div','vc-card-error',v.error||t('errors.audio_processing')));
      card.append(avatarEl(v.name,null,false,false),info);
      const actions=el('div','vc-card-actions');
      if(!vcSelect.on && ['running','paused'].includes(vcTraining?.state)&&vcTraining.voice_id===v.id) {
        const ring=el('div','vc-ring'),percent=Math.round(vcTraining.progress*100);
        ring.setAttribute('role','progressbar'); ring.setAttribute('aria-valuemin','0'); ring.setAttribute('aria-valuemax','100');
        ring.setAttribute('aria-valuenow',String(percent)); ring.style.setProperty('--p',percent+'%'); ring.append(el('span','',percent+'%')); actions.append(ring); card.classList.add('training');
      }
      if(!vcSelect.on) [['vc-rename','✎',t('common.rename')],['vc-delete','×',t('common.delete_prefix')]].forEach(([cls,text,label])=>{
        const button=el('button',cls,text); button.type='button'; button.setAttribute('aria-label',label); actions.append(button);
      });
      if(!vcSelect.on) card.append(actions);
      if(!vcSelect.on&&v.kind==='zeroshot'&&v.status==='ready'&&v.can_train&&!v.training&&!(vcTraining?.queue||[]).some(e=>e.voice_id===v.id)){
        const train=el('button','vc-add-btn vc-train',t('vc.train_more'));train.type='button';card.append(train);
        train.addEventListener('click',event=>{event.stopPropagation();if(card.querySelector('.vc-train-popover'))return;
          const popover=el('div','vc-train-popover'),segment=el('div','vc-presets'),eta=el('div','vc-hint',vcEta(v,'normal')),submit=el('button','vc-add-btn',t('vc.train')),close=el('button','vc-add-btn',t('common.close'));let preset='normal';
          segment.setAttribute('role','group');segment.setAttribute('aria-label',t('vc.training_preset'));
          [['fast',t('vc.preset.fast')],['normal',t('vc.preset.normal')],['max',t('vc.preset.max')]].forEach(([id,label])=>{const b=el('button','',label);b.type='button';b.dataset.preset=id;b.addEventListener('click',()=>{preset=id;vcPresetSelect(segment,id);eta.textContent=vcEta(v,id);});segment.append(b);});
          vcPresetSelect(segment,preset);submit.type=close.type='button';
          submit.addEventListener('click',async()=>{submit.disabled=true;try{await api('/api/vc/train',{voice_id:v.id,preset});await vcLoadVoices();}catch(error){showToast(error.message);submit.disabled=false;}});
          close.addEventListener('click',()=>popover.remove());popover.addEventListener('click',e=>e.stopPropagation());popover.append(segment,eta,submit,close);card.append(popover);
        });
      }
      target.append(card);
    });
    finishMotion();
  }
  $('#vc-select-btn').addEventListener('click',()=>{vcSelect.on=!vcSelect.on;if(!vcSelect.on)vcSelect.ids.clear();vcRenderList();});
  $('#vc-select-all-btn').addEventListener('click',()=>{const all=vcVoices.every(v=>vcSelect.ids.has(v.id));vcVoices.forEach(v=>all?vcSelect.ids.delete(v.id):vcSelect.ids.add(v.id));vcRenderList();});
  $('#vc-select-delete-btn').addEventListener('click',event=>armBulkDelete(event.currentTarget,vcSelect.ids.size,async()=>{
    const candidates=vcVoices.filter(v=>vcSelect.ids.has(v.id)),errors=[];let ok=0;
    for(const voice of candidates){try{await api('/api/vc/delete',{voice_id:voice.id});ok++;}catch(error){errors.push({name:voice.name,message:error.message});}}
    await vcLoadVoices();vcSelect={on:false,ids:new Set()};vcRenderList();
    showTranslatedToast(() => errors.length?t('common.deleted_prefix') + " "+ok+" " + t('common.of') + " "+candidates.length+t('vc.delete_failed_suffix') + " "+errors.map(e=>e.name+' — '+e.message).join('; '):t('common.deleted_prefix') + " "+ok);
  }));
  $('#vc-list').addEventListener('click',async event=>{
    if(vcSelect.on){const card=event.target.closest('.vc-card');if(card){const id=card.dataset.id;vcSelect.ids.has(id)?vcSelect.ids.delete(id):vcSelect.ids.add(id);vcRenderList();return;}}
    const card=event.target.closest('.vc-card'); if(!card) return;
    const voice=vcVoices.find(v=>v.id===card.dataset.id); if(!voice) return;
    const remove=event.target.closest('.vc-delete');
    if(remove) {
      if(remove.dataset.confirm!=='1') {
        remove.dataset.confirm='1'; remove.textContent=t('common.delete_confirm');
        setTimeout(()=>{delete remove.dataset.confirm;remove.textContent='×';},3000); return;
      }
      if(remove.disabled)return;remove.disabled=true;
      try { await api('/api/vc/delete',{voice_id:voice.id}); await vcLoadVoices(); } catch(error) { showToast(error.message); } finally {remove.disabled=false;} return;
    }
    if(event.target.closest('.vc-rename')) {
      const name=card.querySelector('.voice-name'),field=el('input','vc-rename-input');
      field.maxLength=40; field.value=voice.name; name.replaceWith(field); field.focus(); field.select();
      let submitted=false;
      field.addEventListener('keydown',async e=>{
        if(e.key==='Escape') { e.preventDefault();vcRenderList(); }
        if(e.key==='Enter'&&!submitted) {
          e.preventDefault();submitted=true;
          try { await api('/api/vc/rename',{voice_id:voice.id,name:field.value.trim()}); await vcLoadVoices(); }
          catch(error) { showToast(error.message);vcRenderList(); }
        }
      });
      field.addEventListener('blur',()=>{if(!submitted)vcRenderList();}); return;
    }
    if(event.target.closest('input')) return;
    vcSelectedId=voice.id; vcSaveSelection();vcRenderList();vcRenderPanel($('#mode-panel'));
  });
  $('#vc-list').addEventListener('keydown',event=>{
    if(event.target.closest('button,input')) return;
    if(event.key==='Enter'||event.key===' ') { const card=event.target.closest('.vc-card');if(card){event.preventDefault();card.click();} }
  });
  function vcFillPanel() {
    const voice=vcVoices.find(v=>v.id===vcSelectedId); if(!$('#vc-name'))return;
    $('#vc-name').textContent=voice?.name||(vcLoaded&&!vcVoices.length?t('vc.add_voice_left'):t('vc.choose_voice_left'));
    const badge=$('.vc-head .vc-badge'),kind=vcKindBadge(voice?.kind);
    if (badge.textContent !== kind.label) { badge.className='vc-badge '+kind.cls;motionText(badge,kind.label); } badge.hidden=!voice;
    const pitch=$('#vc-pitch'),index=$('#vc-index');
    pitch.value=voice?.pitch_shift??0;index.value=voice?.index_rate??0.75;
    pitch.disabled=!voice;pitch.title=voice?.kind==='zeroshot'?t('vc.pitch_hint'):'';
    index.disabled=!voice;index.closest('.vc-slider-row').hidden=voice?.kind==='zeroshot';
    const block=$('#vc-block');if(block){block.value=Math.max(0,vcBlocks.indexOf(voice?.block_ms??256));block.disabled=!voice;}
    for(const [id,key,defaultValue] of [['vc-quality','diffusion_steps',4],['vc-reference','ref_seconds',5]]){const control=$('#'+id);if(control){control.value=voice?.[key]??defaultValue;control.closest('.vc-slider-row').hidden=voice?.kind!=='zeroshot';control.disabled=!voice;}}
    const recommended=$('#vc-autotune');if(recommended)recommended.disabled=!voice||vcTuningIds.has(vcSelectedId);
    vcSliderOutputs();
  }
  const vcBlocks=[160,256,384,500,750,1000];
  function vcSliderOutputs() {
    const pitch=Number($('#vc-pitch').value);
    $('#vc-pitch-val').textContent=pitch>0?'+'+pitch:pitch<0?'−'+Math.abs(pitch):'0';
    $('#vc-index-val').textContent=Number($('#vc-index').value).toLocaleString(fmtLocale(), {minimumFractionDigits:2, maximumFractionDigits:2});
    const block=$('#vc-block');if(block){const ms=vcBlocks[Number(block.value)],running=audioState.active&&audioState.mode==='vc'&&audioState.vc?.state==='running'&&audioState.vc.voice_id===vcSelectedId;$('#vc-block-val').textContent=running?'≈ '+Math.round(2*ms+(audioState.vc.processing_ms||0))+" " + t('common.ms'):'≈ '+(2*ms)+" " + t('vc.processing_ms_suffix');}
    if($('#vc-quality'))$('#vc-quality-val').textContent=$('#vc-quality').value;
    if($('#vc-reference'))$('#vc-reference-val').textContent=$('#vc-reference').value+" " + t('common.seconds');
  }
  let vcTextBusy=false;
  const vcTextErrors=new Set();
  function vcUpdateText(data=audioState) {
    const input=$('#vc-text-input'),send=$('#vc-text-send'),hint=$('#vc-text-hint');
    if(!input||!send||!hint)return;
    const v=data?.vc||{},running=v.state==='running',inject=v.inject;
    input.disabled=!running;send.disabled=!running;
    send.textContent=inject?.state==='synth'?t('common.cancel'):inject?.state==='playing'?t('common.stop'):t('vc.say');
    const voice=voices.find(v=>v.id===(prefsState.vc_text_voice_id||prefsState.voice_id));
    hint.textContent=!running?t('vc.text_start_hint'):inject?.state==='synth'?t('vc.phrase_preparing'):
      inject?.state==='playing'?t('vc.speaking'):t('vc.text_shortcut_prefix') + " "+(voice?.name||t('vc.text_mode_fallback'));
    if(inject?.state==='error'&&!vcTextErrors.has(inject.id)){
      vcTextErrors.add(inject.id);showTranslatedToast(() => inject.error||t('errors.phrase_speak'));
    }
  }
  function vcBuildTextRow() {
    const row=el('div','vc-text-row');row.id='vc-text-row';
    const input=el('textarea');input.id='vc-text-input';input.rows=2;input.maxLength=1000;
    input.placeholder=t('vc.text_placeholder');
    input.setAttribute('aria-label',input.placeholder);
    const resize=()=>{input.style.height='auto';input.style.height=Math.min(120,Math.max(44,input.scrollHeight))+'px';};
    input.addEventListener('input',resize);
    const send=el('button','start-btn',t('vc.say'));send.id='vc-text-send';send.type='button';
    const hint=el('div');hint.id='vc-text-hint';
    async function submit(cancel=false) {
      if(input.disabled)return;
      if(cancel){
        try{await api('/api/vc/speak/cancel',{});displayStatus(await api('/api/status'));}
        catch(error){showToast(error.message);}return;
      }
      if(vcTextBusy)return;
      vcTextBusy=true;
      const text=input.value;
      audioState.vc.inject={id:'pending',state:'synth'};vcUpdateText();
      try {
        const result=await api('/api/vc/speak',{text});
        input.value='';resize();input.focus();
        if(audioState.vc.inject?.id==='pending')audioState.vc.inject={id:result.id,state:'synth'};
      } catch(error) {
        const state=await api('/api/status').catch(()=>null);
        if(state)displayStatus(state);
        if(!state?.vc?.inject||!['error','cancelled'].includes(state.vc.inject.state))showToast(error.message);
      } finally {vcTextBusy=false;vcUpdateText();}
    }
    send.addEventListener('click',()=>submit(['synth','playing'].includes(audioState.vc?.inject?.state)));
    input.addEventListener('keydown',event=>{if((event.metaKey || (APP_PLATFORM === 'win' && event.ctrlKey))&&event.key==='Enter'){event.preventDefault();submit();}});
    row.append(input,send,hint);return row;
  }
  function vcBuildTextVoiceRow() {
    const row=el('div','audio-row'),label=el('label',null,t('settings.audio.live_text_voice'));
    label.htmlFor='vc-text-voice';const select=el('select');select.id='vc-text-voice';
    select.addEventListener('change',async()=>{
      const previous=prefsState.vc_text_voice_id||null;select.disabled=true;
      try{const prefs=await api('/api/preferences',{vc_text_voice_id:select.value||null});prefsState.vc_text_voice_id=prefs.vc_text_voice_id;}
      catch(error){select.value=previous||'';showToast(error.message);}
      finally{select.disabled=false;vcUpdateText();}
    });row.append(label,select);vcFillTextVoices(select);return row;
  }
  function vcFillTextVoices(select=$('#vc-text-voice')) {
    if(!select)return;
    if(prefsState.vc_text_voice_id&&!voices.some(v=>v.id===prefsState.vc_text_voice_id))prefsState.vc_text_voice_id=null;
    select.replaceChildren();
    const fallback=el('option',null,t('settings.audio.text_voice_fallback'));fallback.value='';select.append(fallback);
    voices.slice().sort((a,b)=>a.name.localeCompare(b.name,'ru')).forEach(voice=>{
      const option=el('option',null,voice.name);option.value=voice.id;select.append(option);
    });select.value=prefsState.vc_text_voice_id||'';
  }
  function vcPanelSignature() { const voice=vcVoices.find(v=>v.id===vcSelectedId); return JSON.stringify([voice?.id,voice?.kind,voice?.status,vcTraining?.state,vcTraining?.voice_id,vcRuntimeOk]); }
  let vcMeterTimer=null, vcMeterError='', vcGateTimer=null, vcGatePending={}, vcGateTask=null, vcGateSaved=null;
  function vcSyncMeter(data=audioState) {
    const wanted=mode==='vc'&&document.visibilityState==='visible'&&!(data?.active&&data.mode==='vc')&&data?.vc?.state!=='loading';
    const beat=()=>api('/api/vc/meter',{on:true}).then(()=>{vcMeterError='';vcUpdateGate();}).catch(error=>{vcMeterError=error.message;vcUpdateGate();});
    if(wanted&&!vcMeterTimer){beat();vcMeterTimer=setInterval(beat,2000);}
    else if(!wanted&&vcMeterTimer){clearInterval(vcMeterTimer);vcMeterTimer=null;vcMeterError='';api('/api/vc/meter',{on:false}).catch(()=>{});}
  }
  document.addEventListener('visibilitychange',()=>vcSyncMeter());
  function vcUpdateGate(data=audioState) {
    const meter=$('#vc-in-level'),control=$('#vc-gate'),toggle=$('#vc-gate-enabled'),state=$('#vc-gate-state');
    if(!meter||!control)return;
    const enabled=!!prefsState.vc_gate_enabled,db=Number(prefsState.vc_gate_db??-45),v=data?.vc||{};
    toggle.checked=enabled;control.disabled=!enabled;control.value=db;
    $('#vc-gate-val').textContent=String(db).replace('-', '−')+" " + t('common.db');
    const mark=meter.querySelector('.vc-gate-mark');mark.hidden=!enabled;mark.style.left=((db+70)/70*100)+'%';
    const hasLevel=typeof v.input_db==='number'&&Number.isFinite(v.input_db);
    meter.querySelector('i').style.width=(hasLevel?Math.round(Math.min(1,Math.max(0,(v.input_db+70)/70))*100):0)+'%';
    meter.classList.toggle('closed',enabled&&(v.meter?hasLevel&&v.input_db<db:v.gate_open===false));
    state.textContent=vcMeterError||(v.meter&&hasLevel?(enabled?(v.input_db<db?t('vc.gate.below'):t('vc.gate.above')):t('vc.gate.microphone_check')):
      !hasLevel?t('vc.gate.start_hint'):!enabled?t('vc.gate.off'):v.gate_open===false?t('vc.gate.closed'):t('vc.gate.open'));
  }
  async function vcFlushGate() {
    clearTimeout(vcGateTimer);
    if(vcGateTask){await vcGateTask;return vcFlushGate();}
    if(!Object.keys(vcGatePending).length)return;
    const patch=vcGatePending;vcGatePending={};
    const save=(values,rollback)=>api('/api/preferences',values).catch(error=>{showToast(error.message);rollback();throw error;});
    vcGateTask=save(patch,()=>{Object.keys(patch).forEach(k=>{if(!(k in vcGatePending))prefsState[k]=vcGateSaved[k];});vcUpdateGate();})
      .then(p=>{Object.keys(patch).forEach(k=>{vcGateSaved[k]=p[k];if(!(k in vcGatePending))prefsState[k]=p[k];});vcUpdateGate();})
      .catch(()=>{});
    await vcGateTask;vcGateTask=null;
    if(Object.keys(vcGatePending).length)return vcFlushGate();
  }
  function vcQueueGate(patch,immediate) {
    Object.assign(prefsState,patch);vcGatePending={...vcGatePending,...patch};vcUpdateGate();clearTimeout(vcGateTimer);
    if(immediate)vcFlushGate();else vcGateTimer=setTimeout(vcFlushGate,200);
  }
  function vcBuildGate() {
    if(!vcGateSaved)vcGateSaved={vc_gate_enabled:!!prefsState.vc_gate_enabled,vc_gate_db:Number(prefsState.vc_gate_db??-45)};
    const wrap=el('div','vc-gate-group'),row=el('div','vc-slider-row vc-gate-row'),label=el('label','vc-gate-toggle'),toggle=el('input');
    toggle.type='checkbox';toggle.className='monitor-toggle';toggle.id='vc-gate-enabled';toggle.setAttribute('aria-label',t('vc.gate.title'));
    toggle.addEventListener('change',()=>vcQueueGate({vc_gate_enabled:toggle.checked},true));label.append(toggle,el('span','',t('vc.gate.title')));
    const control=el('input');control.type='range';control.id='vc-gate';control.min=-70;control.max=-10;control.step=1;control.setAttribute('aria-label',t('vc.gate.threshold'));
    control.addEventListener('input',()=>vcQueueGate({vc_gate_db:Number(control.value)},false));control.addEventListener('change',()=>vcQueueGate({vc_gate_db:Number(control.value)},true));
    const value=el('output');value.id='vc-gate-val';
    const calibrate=el('button','vc-add-btn',t('vc.gate.calibrate'));calibrate.id='vc-gate-calibrate';calibrate.type='button';
    calibrate.addEventListener('click',async()=>{
      calibrate.disabled=true;calibrate.textContent=t('vc.gate.silence_initial');let remaining=3;
      const countdown=setInterval(()=>{remaining--;if(remaining>0)calibrate.textContent='… '+remaining;},1000);
      try{await vcFlushGate();const result=await api('/api/vc/gate/calibrate',{});
        Object.assign(prefsState,{vc_gate_enabled:true,vc_gate_db:result.gate_db});Object.assign(vcGateSaved,{vc_gate_enabled:true,vc_gate_db:result.gate_db});vcUpdateGate();
        showTranslatedToast(() => t('vc.gate.calibrated', {threshold:String(result.gate_db).replace('-','−'), noise:String(result.noise_db).replace('-','−')}));
      }catch(error){showToast(error.message);}finally{clearInterval(countdown);calibrate.textContent=t('vc.gate.calibrate');calibrate.disabled=false;}
    });
    row.append(label,control,value,calibrate);const state=el('div','vc-gate-state');state.id='vc-gate-state';wrap.append(row,state);return wrap;
  }
  function vcRenderPanel(target) {
    vcPanelKey=vcPanelSignature();
    target.replaceChildren();
    if(!vcLoaded&&!vcLoadError) { vcRenderList(); vcLoadVoices(); }
    const stage=el('div','mode-stage vc-stage'),head=el('div','vc-head');
    const name=el('h2');name.id='vc-name'; head.append(el('span','eyebrow',t('vc.live_heading')),name,el('span','vc-badge'));
    const check=el('button','vc-add-btn',t('vc.check'));check.type='button';check.id='vc-check-btn';
    check.addEventListener('click',async()=>{vcCheckPending=true;check.disabled=true;check.textContent=t('vc.checking');
      try{await api('/api/vc/check',{});}catch(error){showToast(error.message);}finally{vcCheckPending=false;vcUpdateCheck(audioState);}});
    head.append(check);stage.append(head);
    const group=el('div','vc-group'),sliders=el('div','vc-sliders');
    let timer,pending={},saving=false,saveTask=null;const panelVoiceId=vcSelectedId;
    async function flushParams(){clearTimeout(timer);if(saving){try{await saveTask;}catch{}return flushParams();}if(!Object.keys(pending).length)return;saving=true;const patch=pending;pending={};
      try{saveTask=api('/api/vc/params',{voice_id:panelVoiceId,...patch});const meta=await saveTask;const i=vcVoices.findIndex(v=>v.id===panelVoiceId);if(i>=0){if(vcVoices[i].restart_required)meta.restart_required=true;vcVoices[i]=meta;}if(panelVoiceId===vcSelectedId&&$('#vc-restart-row'))$('#vc-restart-row').hidden=!meta.restart_required;}
      catch(error){showToast(error.message);if(vcSelectedId===panelVoiceId&&!Object.keys(pending).length)vcFillPanel();}
      finally{saving=false;if(Object.keys(pending).length)flushParams();}}
    function queueParams(patch,immediate){pending={...pending,...patch};clearTimeout(timer);if(immediate)flushParams();else timer=setTimeout(flushParams,300);}
    [['vc-pitch',t('vc.pitch'),-12,12,1],['vc-index',t('vc.index_similarity'),0,1,0.05]].forEach(([id,text,min,max,step])=>{
      const row=el('div','vc-slider-row'),label=el('label','',text),control=el('input'),output=el('output');
      label.htmlFor=id;control.id=id;control.type='range';control.min=min;control.max=max;control.step=step;output.id=id+'-val';
      function changed(immediate){vcSliderOutputs();queueParams({[id==='vc-pitch'?'pitch_shift':'index_rate']:Number(control.value)},immediate);}
      control.addEventListener('input',()=>changed(false));control.addEventListener('change',()=>changed(true));
      row.append(label,control,output);sliders.append(row);
    });group.append(sliders);
    group.append(el('h3','eyebrow',t('vc.voice_settings')));
    const tuning=el('div','vc-sliders vc-tuning');
    [['vc-block',t('vc.latency'),0,5,1,'block_ms'],['vc-quality',t('vc.quality'),2,10,1,'diffusion_steps'],['vc-reference',t('vc.sample_length'),3,15,1,'ref_seconds']].forEach(([id,title,min,max,step,key])=>{
      const row=el('div','vc-slider-row'),label=el('label','',title),control=el('input'),output=el('output');
      label.htmlFor=id;control.id=id;control.type='range';control.min=min;control.max=max;control.step=step;output.id=id+'-val';
      function changed(immediate){vcSliderOutputs();queueParams({[key]:key==='block_ms'?vcBlocks[Number(control.value)]:Number(control.value)},immediate);}
      control.addEventListener('input',()=>changed(false));control.addEventListener('change',()=>changed(true));
      row.append(label,control,output);
      if(id==='vc-quality'){const captions=el('div','vc-quality-captions');captions.append(el('span','',t('vc.faster')),el('span','',t('vc.better')));row.append(captions);}
      tuning.append(row);
    });group.append(tuning);
    const recommended=el('button','vc-add-btn',t('vc.recommended'));recommended.id='vc-autotune';recommended.type='button';
    recommended.addEventListener('click',async()=>{if(recommended.disabled)return;recommended.disabled=true;vcTuningIds.add(panelVoiceId);recommended.textContent=t('vc.tuning');
      try{await flushParams();const result=await api('/api/vc/autotune',{voice_id:panelVoiceId});const i=vcVoices.findIndex(v=>v.id===panelVoiceId);if(i>=0)vcVoices[i]=result.voice;if(vcSelectedId===panelVoiceId)vcFillPanel();const chosen=result.chosen;showTranslatedToast(() => result.warning||(t('vc.tuned_prefix') + " "+chosen.block_ms+" " + t('common.ms')+(result.voice.kind==='zeroshot'?" " + t('vc.quality_suffix') + " "+chosen.diffusion_steps+" " + t('vc.sample_prefix') + " "+chosen.ref_seconds+" " + t('common.seconds'):'')));}
      catch(error){showToast(error.message);}finally{vcTuningIds.delete(panelVoiceId);recommended.disabled=false;recommended.textContent=t('vc.recommended');}});group.append(recommended);
    const restartRow=el('div','vc-restart-row'),restart=el('button','vc-add-btn',t('common.restart'));restartRow.id='vc-restart-row';restartRow.hidden=!vcVoices.find(v=>v.id===panelVoiceId)?.restart_required;restart.type='button';
    restart.addEventListener('click',async()=>{restart.disabled=true;try{await flushParams();await command({action:'stop'});await command({action:'start',mode:'vc',vc_voice_id:panelVoiceId,input_device:$('#input-dev').value||null,output_device:$('#output-dev').value||null});const voice=vcVoices.find(v=>v.id===panelVoiceId);if(voice)delete voice.restart_required;restartRow.hidden=true;}catch{}finally{restart.disabled=false;}});
    restartRow.append(el('span','',t('vc.restart_hint')),restart);group.append(restartRow);

    const meters=el('div','vc-meters');
    [['vc-in-level',t('settings.audio.input')],['vc-out-level',t('settings.audio.output')]].forEach(([id,label])=>{
      const row=el('div','vc-meter-row'),meter=el('div','vc-meter');meter.id=id;meter.append(el('i'));if(id==='vc-in-level')meter.append(el('b','vc-gate-mark'));row.append(el('span','',label),meter);meters.append(row);
    });group.append(meters,vcBuildGate(),vcBuildTextRow());
    const dropHint=el('div','vc-drop-hint',t('vc.drop_hint'));dropHint.id='vc-drop-hint';dropHint.hidden=true;group.append(dropHint);
    const cpuWarning=el('div','vc-drop-hint',t('vc.cpu_warning'));cpuWarning.id='vc-cpu-warning';cpuWarning.setAttribute('role','status');cpuWarning.hidden=true;group.append(cpuWarning);
    const readout=el('div','vc-readout',t('vc.readout_empty'));readout.id='vc-readout';
    const error=el('div','vc-error');error.id='vc-error';error.setAttribute('role','alert');error.hidden=true;group.append(readout);stage.append(group,error);
    const training=el('section','vc-training');training.id='vc-training';training.hidden=true;
    const trainName=el('div');trainName.id='vc-train-name';const stages=el('ol','vc-stages');stages.id='vc-stages';
    [['prepare',t('status.training_prepare')],['pitch',t('status.training_pitch')],['features',t('status.training_features')],['train',t('status.training_train')],['index',t('status.training_index')]].forEach(([id,label])=>{const li=el('li','',label);li.dataset.stage=id;stages.append(li);});
    const bar=el('div','vc-bar');bar.id='vc-train-bar';bar.setAttribute('role','progressbar');bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.setAttribute('aria-valuenow','0');bar.append(el('i'));
    const line=el('div','vc-train-line');line.id='vc-train-line';const cancel=el('button','',t('common.stop_action'));cancel.id='vc-train-cancel';cancel.type='button';
    cancel.addEventListener('click',()=>api('/api/vc/train/cancel',{voice_id:vcTraining&&vcTraining.voice_id}).then(()=>vcLoadVoices()).catch(e=>showToast(e.message)));
    const pauseBtn=el('button','',t('status.training_paused'));pauseBtn.id='vc-train-pause';pauseBtn.type='button';pauseBtn.hidden=true;
    pauseBtn.addEventListener('click',async()=>{
      pauseBtn.disabled=true;
      try {
        const updated=await api('/api/vc/train/'+(vcTraining&&vcTraining.user_paused?'resume':'pause'),{});
        audioState.training=updated;vcOnStatus(audioState);
      } catch(error){showToast(error.message);}
      finally{if(pauseBtn.isConnected)pauseBtn.disabled=false;}
    });
    const queue=el('div','vc-train-queue');queue.id='vc-train-queue';
    const trainActions=el('div','vc-train-actions');trainActions.append(pauseBtn,cancel);
    training.append(el('h3','eyebrow',t('vc.training_heading')),trainName,stages,bar,line,trainActions,queue);stage.append(training);target.append(stage);
    vcFillPanel();vcOnStatus(audioState);syncMainMonitor(audioState);
    engineStop();if(!vcRuntimeOk)engineRefresh();
  }
  // ---- Live voice engine card (spec G): replaces the VC panel while runtime_ok is false ----
  const ENGINE_ACTIVE=['checking','downloading','installing','verifying'];
  const ENGINE_CODES=['network','checksum','disk','uv','smoke','dev_runtime','unsupported','busy'];
  let engineTimer=null,engineSeq=0,engineLocalError=null;
  function engineStop(){clearTimeout(engineTimer);engineTimer=null;engineSeq++;}
  function engineErrorText(code,s) {
    if(code==='disk')return t('engine.error.disk',{need:formatBytes(Math.ceil((s.total_bytes||0)*2.2)),have:formatBytes(s.free_bytes)});
    if(code==='network')return t('engine.error.network');
    if(code==='checksum')return t('engine.error.checksum');
    return t('engine.error.other',{code});
  }
  async function engineRefresh() {
    engineStop();const seq=engineSeq;
    let s=null;try{s=await api('/api/vc/engine');}catch{}
    const stage=$('#mode-panel .vc-stage');
    if(seq!==engineSeq||mode!=='vc'||!stage)return;
    // Older builds and test mocks without the endpoint: keep the plain panel with its "environment missing" error.
    if(!s||typeof s.status!=='string'){$('#vc-engine-card')?.remove();stage.hidden=false;return;}
    if(s.installed&&s.status==='idle'){$('#vc-engine-card')?.remove();stage.hidden=false;engineLocalError=null;vcLoadVoices();return;}
    engineRender(s,stage);
    if(ENGINE_ACTIVE.includes(s.status))engineTimer=setTimeout(()=>{if($('#vc-engine-card'))engineRefresh();},1000);
  }
  async function engineAction(paths) {
    engineStop();engineLocalError=null;
    try{for(const path of paths)await api('/api/vc/engine/'+path,{});}
    catch(error){engineLocalError=ENGINE_CODES.includes(error.message)?error.message:'network';const actions=$('#vc-engine-actions');if(actions)actions.dataset.kind='';}
    engineRefresh();
  }
  function engineRender(s,stage) {
    let card=$('#vc-engine-card');
    if(!card) {
      card=el('section','glass-panel vc-group engine-card');card.id='vc-engine-card';
      const title=el('h2','engine-title',t('engine.title'));title.id='vc-engine-title';card.setAttribute('aria-labelledby',title.id);
      const about=el('p','vc-hint');about.id='vc-engine-about';
      const bar=el('div','engine-bar');bar.id='vc-engine-bar';bar.setAttribute('role','progressbar');bar.setAttribute('aria-labelledby','vc-engine-line');bar.append(el('i'));
      const line=el('div','vc-readout');line.id='vc-engine-line';line.setAttribute('aria-live','polite');
      const error=el('div','vc-error engine-error');error.id='vc-engine-error';error.setAttribute('role','alert');
      const actions=el('div','engine-actions');actions.id='vc-engine-actions';
      card.append(title,about,bar,line,error,actions);stage.before(card);
    }
    stage.hidden=true;
    const active=ENGINE_ACTIVE.includes(s.status),unsupported=s.supported===false;
    const code=engineLocalError||(s.status==='error'?s.error||'network':null);
    $('#vc-engine-about').textContent=unsupported?t('engine.unsupported'):t('engine.about',{size:formatBytes(s.total_bytes)});
    const bar=$('#vc-engine-bar'),line=$('#vc-engine-line'),error=$('#vc-engine-error');
    bar.hidden=line.hidden=!active||unsupported;
    const determinate=s.status==='downloading'&&s.total_bytes>0;
    bar.classList.toggle('vc-indeterminate',active&&!determinate);
    const percent=determinate?Math.min(100,Math.round((s.done_bytes||0)/s.total_bytes*100)):0;
    bar.firstElementChild.style.width=determinate?percent+'%':'';
    if(determinate)bar.setAttribute('aria-valuenow',String(percent));else bar.removeAttribute('aria-valuenow');
    line.textContent=s.status==='downloading'?t('engine.progress',{step:t('engine.step.downloading'),done:formatBytes(s.done_bytes),total:formatBytes(s.total_bytes)}):
      s.status==='checking'?t('engine.step.checking'):s.status==='verifying'?t('engine.step.verifying'):
      s.step==='Installing packages'?t('engine.step.packages'):t('engine.step.extracting');
    error.hidden=!code||active||unsupported;error.textContent=error.hidden?'':engineErrorText(code,s);
    const update=!s.installed&&s.version!=null;
    const kind=unsupported?'none':active?'active':code?'retry':update?'update':'download';
    const actions=$('#vc-engine-actions');
    if(actions.dataset.kind===kind)return;
    actions.dataset.kind=kind;actions.replaceChildren();
    if(kind==='none')return;
    if(kind==='active') {
      const cancel=el('button','vc-add-btn',t('common.cancel'));cancel.type='button';cancel.id='vc-engine-cancel';
      cancel.addEventListener('click',()=>{cancel.disabled=true;engineAction(['cancel']);});actions.append(cancel);return;
    }
    const primary=el('button','vc-submit',kind==='retry'?t('engine.retry'):kind==='update'?t('engine.update'):t('engine.download'));primary.type='button';primary.id='vc-engine-install';
    primary.addEventListener('click',()=>{primary.disabled=true;engineAction(update?['remove','install']:['install']);});
    const link=el('a','key-link',t('engine.what'));link.href='https://github.com/coldmoth/ai-voice#live-voice-engine';link.target='_blank';link.rel='noopener noreferrer';
    actions.append(primary,link);
  }
  function vcUpdateCheck(data) {
    const button=$('#vc-check-btn');if(!button)return;
    const vc=data?.vc||{},busy=vcCheckPending||['synth','playing'].includes(vc.inject?.state);
    const enabled=data?.mode==='vc'&&data.active&&vc.state==='running'&&vc.voice_id===vcSelectedId;
    button.disabled=!enabled||busy;button.textContent=busy?t('vc.checking'):t('vc.check');
    button.title=enabled?'':t('vc.check_start_hint');
  }
  function vcOnStatus(data) {
    const cpuWarning=$('#vc-cpu-warning');
    if(cpuWarning){cpuWarning.hidden=data?.vc?.device!=='cpu';cpuWarning.textContent=t('vc.cpu_warning');}
    vcUpdateCheck(data);
    vcSyncMeter(data);
    vcUpdateText(data);
    const training=data&&data.training||null,previous=vcLastTrainState,oldVoice=vcTraining?.voice_id;
    // Record the transition before asynchronously reloading/rebuilding the panel.
    vcLastTrainState=training?training.state:null;vcTraining=training;
    vcTrainK=training?.k||vcTrainK;vcNotifyDone(data?.last_done);
    if(['running','paused'].includes(previous)&&(!training||!['running','paused'].includes(training.state))) {
      vcLoadVoices().then(()=>{
        if(training?.state==='done')showTranslatedToast(() => t('vc.trained_quality_notice', {name:vcVoices.find(v=>v.id===training.voice_id)?.name||t('common.voice')}));
        else if(training?.state==='failed')showTranslatedToast(() => t('vc.training_failed_prefix') + " "+(training.error||t('vc.error_fallback')));
        else if(training?.state==='cancelled')showTranslatedToast(() => t('vc.training_stopped'));
      });
    } else if(['running','paused'].includes(training?.state)&&(!['running','paused'].includes(previous)||oldVoice!==training.voice_id)) vcLoadVoices();
    const percent=Math.round((training?.progress||0)*100);
    document.querySelectorAll('#vc-list .vc-ring').forEach(ring=>{
      if(ring.closest('.vc-card').dataset.id===training?.voice_id&&ring.getAttribute('aria-valuenow')!==String(percent)) {
        ring.style.setProperty('--p',percent+'%');ring.setAttribute('aria-valuenow',String(percent));ring.querySelector('span').textContent=percent+'%';
      }
    });
    if(!$('#vc-training'))return;
    $('#vc-training').hidden=!(training&&(['running','paused','failed'].includes(training.state)||(training.queue||[]).length));
    if(training) {
      const active=!!training.voice_id;['vc-train-name','vc-stages','vc-train-bar','vc-train-line'].forEach(id=>{$('#'+id).hidden=!active;});
      $('#vc-train-name').textContent=vcVoices.find(v=>v.id===training.voice_id)?.name||t('common.voice');
      const items=Array.from($('#vc-stages').children),current=items.findIndex(li=>li.dataset.stage===training.stage);
      items.forEach((li,i)=>{li.classList.toggle('current',i===current);li.classList.toggle('done',i<current);});
      $('#vc-train-line').textContent=training.state==='paused'?(training.user_paused?t('status.training_paused'):t('status.training_paused_live')):training.state==='failed'?training.error:training.stage_label+(training.stage==='train'?" " + t('vc.epoch_prefix') + " "+training.epoch+'/'+training.total_epochs:'')+(training.eta_s==null?" " + t('vc.eta_pending_suffix'):" " + t('vc.remaining_prefix') + " "+Math.max(1,Math.round(training.eta_s/60))+" " + t('common.minutes'));
      motionProgress($('#vc-train-bar>i'),percent/100);$('#vc-train-bar').setAttribute('aria-valuenow',String(percent));$('#vc-train-cancel').hidden=!active||!['running','paused'].includes(training.state);
      const pauseBtn=$('#vc-train-pause');
      if (pauseBtn) {
        pauseBtn.hidden=!((active&&['running','paused'].includes(training.state))||(training.user_paused&&(training.queue||[]).length>0));
        pauseBtn.textContent=training.user_paused?t('common.resume'):t('status.training_paused');
      }
    }
    const queue=$('#vc-train-queue');
    const queueKey=I18N.lang+JSON.stringify(training?.queue||[]);
    if(queue._renderKey!==queueKey){
      queue._renderKey=queueKey;
      const finishMotion=motionRows(queue,new Set((training?.queue||[]).map(e=>e.voice_id)));queue.replaceChildren();(training?.queue||[]).forEach((entry,i,items)=>{
      const row=el('div','vc-queue-row');row.dataset.id=entry.voice_id;row.append(el('span','',entry.name+' · ≈ '+entry.eta_min+" " + t('common.minutes')));
      [['up','↑',t('vc.queue.up')],['down','↓',t('vc.queue.down')],['cancel','×',t('vc.queue.remove')]].forEach(([direction,label,title])=>{const b=el('button','vc-add-btn',label);b.type='button';b.title=title;b.setAttribute('aria-label',title+' '+entry.name);b.disabled=direction==='up'&&i===0||direction==='down'&&i===items.length-1;
        b.addEventListener('click',async()=>{b.disabled=true;try{const updated=await api('/api/vc/train/'+(direction==='cancel'?'cancel':'move'),{voice_id:entry.voice_id,...(direction==='cancel'?{}:{direction})});audioState.training=updated;queue._renderKey=null;vcOnStatus(audioState);await vcLoadVoices();}catch(error){showToast(error.message);b.disabled=false;}});row.append(b);});queue.append(row);
    });
      finishMotion();
    }
    const v=data?.vc||{},running=data?.mode==='vc'&&data.active&&v.state==='running';
    vcUpdateGate(data);
    $('#vc-out-level>i').style.width=(running?Math.min(100,Math.round(Math.sqrt(Math.max(0,Number(v.output_level)||0))*100)):0)+'%';
    const now=Date.now(),drops=Number(v.dropped_blocks)||0;
    if(!running||vcDropVoice!==v.voice_id||(vcDroppedHistory.length&&drops<vcDroppedHistory[vcDroppedHistory.length-1].drops)){vcDroppedHistory=[];vcLastDropAt=0;vcDropVoice=running?v.voice_id:null;}
    if(running){const previous=vcDroppedHistory[vcDroppedHistory.length-1];if(previous&&drops>previous.drops)vcLastDropAt=now;vcDroppedHistory.push({time:now,drops});while(vcDroppedHistory.length>1&&vcDroppedHistory[1].time<now-10000)vcDroppedHistory.shift();if(drops-vcDroppedHistory[0].drops>3)$('#vc-drop-hint').hidden=false;else if(!vcLastDropAt||now-vcLastDropAt>=30000)$('#vc-drop-hint').hidden=true;}else $('#vc-drop-hint').hidden=true;
    vcSliderOutputs();
    const loadingNow=vcLoading(data);$('#vc-readout').classList.toggle('vc-loading',loadingNow);
    $('#vc-readout').textContent=loadingNow?t('status.voice_loading'):v.state==='running'&&typeof v.latency_ms==='number'&&typeof v.rss_mb==='number'?t('vc.latency') + " "+Math.round(v.latency_ms)+" " + t('vc.ram_prefix') + " "+(v.rss_mb/1024).toLocaleString(fmtLocale(), {minimumFractionDigits:1, maximumFractionDigits:1})+" " + t('common.gb')+(v.dropped_blocks>0?" " + t('vc.dropped_prefix') + " "+v.dropped_blocks:''):t('vc.readout_empty');
    const error=$('#vc-error');error.hidden=vcRuntimeOk&&(loadingNow||!(v.state==='error'||data?.state==='error'&&data.mode==='vc'));error.textContent=error.hidden?'':!vcRuntimeOk?t('errors.vc_environment_missing'):v.error||data.message;
  }
  async function vcAudioAction() {
    try {
      if(audioState.active&&audioState.mode==='vc') {await command({action:'stop'});return;}
      if(!vcSelectedId){showTranslatedToast(() => t('vc.choose_my_voice'));return;}
      const voice=vcVoices.find(v=>v.id===vcSelectedId);
      if(!voice||voice.status!=='ready'){showTranslatedToast(() => t('vc.voice_processing'));return;}
      await command({action:'start',mode:'vc',vc_voice_id:vcSelectedId,input_device:$('#input-dev').value||null,output_device:$('#output-dev').value||null});
      delete voice.restart_required;if($('#vc-restart-row'))$('#vc-restart-row').hidden=true;
    } catch {}
  }
  function vcSetTab(tab) {
    document.querySelectorAll('#vc-sheet .vc-tab').forEach(button=>{const active=button.dataset.vcTab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));});
    document.querySelectorAll('.vc-pane').forEach(pane=>{pane.hidden=pane.dataset.vcPane!==tab;});
  }
  function vcOpenSheet(tab) {vcSheetOpener=document.activeElement;motionSheet($('#vc-sheet'),true);vcSetTab(tab);$('#vc-'+tab+'-name').focus();$('#vc-sheet-error').hidden=true;}
  function vcCloseSheet() {
    motionSheet($('#vc-sheet'),false);
    ['import','audio'].forEach(tab=>{vcPicked[tab]=[];$('#vc-'+tab+'-files').value='';$('#vc-'+tab+'-name').value='';vcFileList(tab);});
    vcAudioPreset='none';vcPresetSelect($('#vc-audio-train'),'none');if(vcSheetOpener?.isConnected)vcSheetOpener.focus();
  }
  function vcSheetError(message) {vcSheetError.translate=null;$('#vc-sheet-error').textContent=message;$('#vc-sheet-error').hidden=false;}
    function vcTranslatedSheetError(translate) { vcSheetError(translate()); vcSheetError.translate=translate; }
  function vcFileList(tab) {
    const list=$('#vc-'+tab+'-list');list.replaceChildren();
    vcPicked[tab].forEach((file,i)=>{
      const li=el('li'),remove=el('button','','×');remove.type='button';remove.setAttribute('aria-label',t('common.remove_prefix') + " "+file.name);
      remove.addEventListener('click',()=>{vcPicked[tab].splice(i,1);vcFileList(tab);});
      li.append(el('span','',file.name+' · '+(file.size/1024/1024).toLocaleString(fmtLocale(), {minimumFractionDigits:1, maximumFractionDigits:1})+" " + t('common.mb')),remove);list.append(li);
    });
    if(tab==='audio') {
      const n=vcPicked.audio.length;
      $('#vc-audio-hint').textContent=(n?tp('vc.selected_files', n, {size:(vcPicked.audio.reduce((s,f)=>s+f.size,0)/1024/1024).toLocaleString(fmtLocale(), {minimumFractionDigits:1, maximumFractionDigits:1})})+' ':'')+t('vc.audio_hint');
    }
  }
  function vcPickFiles(tab,files) {
    for(const file of files) {
      if(tab==='import'&&!/\.(pth|index|zip)$/i.test(file.name)){vcTranslatedSheetError(() => t('errors.vc_import_files'));continue;}
      if(!vcPicked[tab].some(f=>f.name===file.name&&f.size===file.size))vcPicked[tab].push(file);
    }vcFileList(tab);
  }
  async function vcSubmit(tab) {
    const name=$('#vc-'+tab+'-name').value.trim(),files=vcPicked[tab],train=vcAudioPreset;
    if(!name||name.length>40){vcTranslatedSheetError(() => t('errors.voice_name'));return;}
    if(!files.length){vcTranslatedSheetError(() => tab==='import'?t('errors.choose_model_files'):t('errors.choose_audio'));return;}
    if(files.reduce((sum,file)=>sum+file.size,0)>500*1024*1024){vcTranslatedSheetError(() => t('errors.upload_size'));return;}
    const fd=new FormData();fd.append('name',name);files.forEach(file=>fd.append('files',file));if(tab==='audio')fd.append('train',train);
    const button=$('#vc-'+tab+'-submit');button.disabled=true;button.textContent=t('common.loading');$('#vc-sheet-error').hidden=true;
    try {
      const r=await vcUpload('/api/vc/'+(tab==='import'?'import':'create'),fd),m=tab==='import'?r:r.voice;
      vcCloseSheet();vcSelectedId=m.id;vcSaveSelection();await vcLoadVoices();
      if(tab==='import')showTranslatedToast(() => t('vc.voice_added', {name:m.name}));
      else showTranslatedToast(() => t('vc.voice_added_processing', {name:m.name}));
    } catch(error) {if($('#vc-sheet').hidden||$('#vc-sheet').inert)showToast(error.message);else vcSheetError(error.message);} finally {button.disabled=false;button.textContent=tab==='import'?t('vc.import'):t('vc.create_voice');}
  }
  $('#vc-add-btn').addEventListener('click',()=>vcOpenSheet('import'));
  $('#vc-sheet-close').addEventListener('click',vcCloseSheet);
  $('#vc-sheet').addEventListener('click',event=>{if(event.target===$('#vc-sheet'))vcCloseSheet();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('#vc-sheet').hidden){event.preventDefault();vcCloseSheet();}});
  $('#vc-sheet').addEventListener('keydown',event=>{
    if(event.key!=='Tab')return;
    const items=Array.from($('#vc-sheet .vc-sheet-card').querySelectorAll('button,input,[tabindex="0"]')).filter(node=>!node.disabled&&node.getClientRects().length);
    if(!items.length)return;
    const first=items[0],last=items[items.length-1],inside=items.includes(document.activeElement);
    if(event.shiftKey&&(document.activeElement===first||!inside)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||!inside)){event.preventDefault();first.focus();}
  });
  document.querySelectorAll('#vc-sheet .vc-tab').forEach(button=>button.addEventListener('click',()=>vcSetTab(button.dataset.vcTab)));
  ['import','audio'].forEach(tab=>{
    $('#vc-'+tab+'-files').addEventListener('change',event=>vcPickFiles(tab,event.target.files));
    const drop=$('#vc-'+tab+'-drop');drop.addEventListener('dragover',event=>{event.preventDefault();drop.classList.add('drag');});
    drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
    drop.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('#vc-'+tab+'-files').click();}});
    drop.addEventListener('drop',event=>{event.preventDefault();drop.classList.remove('drag');vcPickFiles(tab,event.dataTransfer.files);});
    $('#vc-'+tab+'-submit').addEventListener('click',()=>vcSubmit(tab));
  });
  loadInitial();
})();
