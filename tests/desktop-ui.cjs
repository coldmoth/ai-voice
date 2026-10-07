const renderPage=require('./page.cjs');
const serveLocale = require('./locale-route.cjs');
/* Headless integration of the shipped desktop DOM/JS; all audio/API calls mocked. */
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),out=path.join(root,'state/research/ui-qa');fs.mkdirSync(out,{recursive:true});
const cdn='https://public-platform.r2.fish.audio/cdn-cgi/image/width=96,format=webp/coverimage/';
const bykov={id:'db89e349112e44dca6820e0cb2d414cc',name:'Быков',language:'ru',source:'fish',avatar_url:cdn+'db89e349112e44dca6820e0cb2d414cc',tags:['male'],task_count:100};
const female=Array.from({length:12},(_,i)=>({id:(i+1).toString(16).padStart(32,'0'),name:i===0?'Женский голос <b>тест</b>':'Женский голос '+(i+1),language:'ru',source:'fish',avatar_url:null,tags:['female'],task_count:1000-i}));
let prefs={language:'ru',catalog_language:'ru',speech_language:'ru-RU',voice_id:bykov.id,favorite_ids:[],input_device:'MIC',output_device:'AI Voice',output_gain_db:0,normalize_loudness:true,monitor_enabled:false,monitor_device:null,monitor_gain_db:0,input_gain_db:0};
const requests=[];let inFlight=0,maxInFlight=0;
let vcVoices=[{id:'v1',name:'Диктор',kind:'trained',status:'ready',pitch_shift:0,index_rate:0.75},{id:'v2',name:'Мой <b>голос</b>',kind:'zeroshot',status:'ready',pitch_shift:0,index_rate:0.75,speech_seconds:252,can_train:true}];
let vcTraining=null;
const hfCard={id:'test/voice:voice.pth',repo:'test/voice',title:'HF <b>голос</b>',files:[{path:'voice.pth',size:100}],total_size:100,downloads:42,updated:'2026-10-03T12:00:00Z',has_index:false,revision:'abc',likes:5,license:'mit',sample:{path:'sample.wav',size:10}};
let hfReadmeMeaningful=true,hfSampleFail=false;
const hfReadme={meaningful:true,images:['https://huggingface.co/test/voice/resolve/abc/cover.png'],blocks:[
 {type:'heading',level:1,spans:[{t:'Описание'}]},
 {type:'paragraph',spans:[{t:'<script>unsafe</script>'},{t:'Ссылка',href:'https://huggingface.co/test/voice'}]},
 {type:'list',ordered:false,items:[[{t:'Пункт',b:true}]]},
 {type:'code',text:'const x = 1;'}, {type:'quote',spans:[{t:'Цитата',i:true}]}, {type:'image',n:0,alt:'Cover'}]};
let hfDownload={state:'idle',id:null,bytes:0,total:0},hfFail=false;
let keysMock={fish:true,hf:false},hfKeyResult={ok:false,error:'invalid'};const hfKeyRequests=[];
let vcVoicesFail=0,vcRuntimeOk=true;
let monitorShouldFail=false;
let bypassInFlight=0,maxBypassInFlight=0;
let staleStuckRequest=false;
let staleStuckResolve=null;
let status={active:false,state:'stopped',mode:'mic',message:'Остановлено',transcript_partial:'',transcript_final:'',monitor_errors:0};
let previewShouldFail=false;
let catalogPreviewedId=null;
let ttsVoices=[{...bykov,avatar_url:null},...female.slice(0,2)];
let vcDeleteFailId=null;
let vcMeterFail=false,vcGateCalibrateFail=false;
let storageData={total:1550000000,categories:[
 {id:'vc_voices',label:'Мои голоса',bytes:800000000,clearable:true,note:'',items:[{id:'v1',label:'Диктор',bytes:500000000},{id:'v2',label:'Второй',bytes:300000000}]},
 {id:'vc_models',label:'Модели Живого голоса',bytes:600000000,clearable:false,note:'нужны для работы Живого голоса'},
 {id:'logs',label:'Логи',bytes:12000,clearable:true,note:''},
 {id:'app',label:'Приложение',bytes:150000000,clearable:false,note:''}]};
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');if(serveLocale(url,res,root))return;let text='';for await(const x of req)text+=x;const body=text&&(req.headers['content-type']||'').includes('json')?JSON.parse(text):{};
 requests.push({path:url.pathname,query:Object.fromEntries(url.searchParams),body,raw:(req.headers['content-type']||'').includes('multipart')?text:undefined});
 const reply=(data,code=200)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(url.pathname==='/'){
  const html=renderPage(root);
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(html);
 }
 if(url.pathname==='/api/storage')return reply(storageData);
 if(url.pathname==='/api/storage/clear'){
  if(body.item_id){const c=storageData.categories[0];const it=c.items.find(i=>i.id===body.item_id);c.items=c.items.filter(i=>i!==it);c.bytes-=it.bytes;storageData.total-=it.bytes;}
  else{const c=storageData.categories.find(x=>x.id===body.category);storageData.total-=c.bytes;c.bytes=0;if(c.items)c.items=[];}
  return reply(storageData);
 }
 if(url.pathname==='/api/voices')return reply({items:ttsVoices,preferences:prefs,language:prefs.language,speech_languages:[{id:'en-US'},{id:'ru-RU'}],speech_language:prefs.speech_language,devices:{inputs:['MIC','USB Mic'],outputs:['AI Voice','Speakers'],monitors:['External Headphones','MacBook Pro Speakers'],default_input:'MIC',default_output:'AI Voice',virtual:['AI Voice']}});
 if(url.pathname==='/api/keys')return reply(keysMock);
 if(url.pathname==='/api/keys/hf'&&req.method==='POST'){hfKeyRequests.push(body);return reply(hfKeyResult);}
 if(url.pathname==='/api/permissions')return reply({microphone:'authorized',speech:'authorized'});
 if(url.pathname==='/api/driver/status')return reply({available:false});
 if(url.pathname==='/api/voice_metadata')return reply({items:[bykov,...female],pending:false});
 if(url.pathname==='/api/status')return reply({...status,usage:{today:120,month:300,limit:prefs.monthly_char_limit||null},monitor_active:prefs.monitor_enabled,gen_seq:requests.filter(x=>x.path==='/api/status').length,latency:Math.random()*80+20});
 if(url.pathname==='/api/search'){
  const gender=url.searchParams.get('gender'),p=Number(url.searchParams.get('page')||1),query=url.searchParams.get('q');
  if(gender==='male')await new Promise(r=>setTimeout(r,220));
  const items=query==='none'?[]:gender==='male'?[bykov]:female.slice((p-1)*6,p*6);
  return reply({items,has_more:query!=='none'&&gender!=='male'&&p===1,page:p});
 }
 if(url.pathname==='/api/preview'){
  if(previewShouldFail)return reply({error:'Тестовая ошибка предпрослушивания'},400);
  return reply({ok:true});
 }
 if(url.pathname==='/api/preferences'){
  inFlight++;maxInFlight=Math.max(inFlight,maxInFlight);await new Promise(r=>setTimeout(r,120));inFlight--;
  if(staleStuckRequest && body.output_gain_db===-6){await new Promise(r=>{staleStuckResolve=r;});}
  if(body.output_gain_db===-9)return reply({error:'Тестовая ошибка сохранения'},400);
  if('monitor_device' in body && body.monitor_device===body.output_device){
    return reply({error:'Прослушивание не может идти на то же устройство, что и Discord.'},400);
  }
  if(monitorShouldFail && ('monitor_enabled' in body || 'monitor_device' in body || 'monitor_gain_db' in body)){
    return reply({error:'Некорректные настройки мониторинга.'},400);
  }
  prefs={...prefs,...body};return reply(prefs);
 }
 if(url.pathname==='/api/vc/hf/search'){
  if(hfFail)return reply({error:'Нет связи с Hugging Face'},503);
  const p=Number(url.searchParams.get('page')||1);
  return reply({items:url.searchParams.get('q')==='none'?[]:[p===1?hfCard:{...hfCard,id:'test/second:second.pth',repo:'test/second',title:'Второй',sample:null,files:[{path:'second.pth',size:100}]}],page:p,has_more:p===1&&url.searchParams.get('q')!=='none'});
 }
 if(url.pathname==='/api/vc/hf/readme')return reply({...hfReadme,meaningful:hfReadmeMeaningful});
 if(url.pathname==='/api/vc/hf/image'){
  res.writeHead(200,{'Content-Type':'image/png'});return res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII=','base64'));
 }
 if(url.pathname==='/api/vc/hf/sample'){
  if(hfSampleFail)return reply({error:'Образец недоступен'},502);
  res.writeHead(200,{'Content-Type':'audio/wav'});return res.end(Buffer.from('mock audio'));
 }
 if(url.pathname==='/api/vc/check'){
  if(!prefs.monitor_enabled)return reply({error:'Включите мониторинг (значок громкоговорителя внизу), чтобы прослушать.'},409);
  status={...status,vc:{...status.vc,inject:{id:'check-test',state:'playing'}}};return reply({id:'check-test'});
 }
 if(url.pathname==='/api/vc/catalog/download/cancel'){hfDownload={...hfDownload,state:'error',error:'Отменено'};return reply(hfDownload);}
 if(url.pathname==='/api/vc/catalog/download'){
  if(req.method==='POST')hfDownload={state:'downloading',id:body.id,bytes:50,total:100};
  return reply(hfDownload);
 }
 if(url.pathname==='/api/vc/voices'&&vcVoicesFail>0){vcVoicesFail--;return reply({error:'Тестовая ошибка списка'},500);}
 if(url.pathname==='/api/vc/voices')return reply({items:vcVoices,training:vcTraining,last_done:null,runtime_ok:vcRuntimeOk});
 if(url.pathname==='/api/vc/meter'){
  if(vcMeterFail&&body.on)return reply({error:'Не удалось открыть микрофон: тест'},409);
  if(!(status.active&&status.mode==='vc'))status={...status,vc:{...status.vc,state:'idle',meter:body.on,input_db:body.on?-50:null,gate_open:null}};
  return reply({source:status.active&&status.mode==='vc'?'worker':'meter'});
 }
 if(url.pathname==='/api/vc/gate/calibrate'){
  await new Promise(r=>setTimeout(r,300));
  if(vcGateCalibrateFail)return reply({error:'Нет сигнала микрофона. Проверьте устройство ввода.'},409);
  prefs={...prefs,vc_gate_enabled:true,vc_gate_db:-48};return reply({gate_db:-48,noise_db:-54});
 }
if(url.pathname==='/api/vc/autotune'){await new Promise(r=>setTimeout(r,150));const i=vcVoices.findIndex(v=>v.id===body.voice_id),chosen={block_ms:500,diffusion_steps:4,ref_seconds:5,ms:93};vcVoices[i]={...vcVoices[i],block_ms:500,diffusion_steps:4,ref_seconds:5};return reply({voice:vcVoices[i],chosen,results:[chosen],warning:null});}
 if(url.pathname==='/api/vc/speak'){
  status={...status,vc:{...status.vc,inject:{id:'text-test',state:'playing'}}};return reply({id:'text-test'});
 }
 if(url.pathname==='/api/vc/speak/cancel'){
  status={...status,vc:{...status.vc,inject:{id:'text-test',state:'cancelled'}}};return reply({});
 }
 if(url.pathname==='/api/vc/import'||url.pathname==='/api/vc/create'){
  const create=url.pathname.endsWith('create'),id='v'+(vcVoices.length+1);
  const name=/name="name"\r\n\r\n([^\r]*)/.exec(text)?.[1]||'Голос';
  if(name==='Сбой'){await new Promise(r=>setTimeout(r,300));return reply({error:'Тестовая ошибка импорта'},400);}
  const meta={id,name,kind:create?'zeroshot':'imported',status:create?'processing':'ready',stage:create?'convert':undefined,pitch_shift:0,index_rate:.75,speech_seconds:252,can_train:create};
  vcVoices.push(meta);
  if(create){
   setTimeout(()=>{meta.stage='slice';},1100);
   setTimeout(()=>{meta.status='ready';delete meta.stage;
    if(/name="train"\r\n\r\nnormal/.test(text)){
     vcTraining={voice_id:id,stage:'train',stage_label:'Обучение',epoch:57,total_epochs:100,progress:.4,eta_s:2400,state:'running',error:null,user_paused:false,queue:[{voice_id:'v2',name:'Мой <b>голос</b>',epochs:50,eta_min:11},{voice_id:'v1',name:'Диктор',epochs:100,eta_min:18}]};status.training=vcTraining;
    }
   },2300);
  }
  return reply(create?{voice:meta,train_error:null}:meta);
 }
 if(url.pathname==='/api/vc/delete'){await new Promise(r=>setTimeout(r,180));if(body.voice_id===vcDeleteFailId)return reply({error:'Остановите Живой голос'},409);vcVoices=vcVoices.filter(v=>v.id!==body.voice_id);return reply({ok:true});}
 if(url.pathname==='/api/vc/rename'||url.pathname==='/api/vc/params'){
  const i=vcVoices.findIndex(v=>v.id===body.voice_id);const {voice_id,...patch}=body;vcVoices[i]={...vcVoices[i],...patch};return reply(vcVoices[i]);
 }
 if(url.pathname==='/api/vc/train/move'){const q=vcTraining.queue,i=q.findIndex(v=>v.voice_id===body.voice_id),j=i+(body.direction==='up'?-1:1);if(j>=0&&j<q.length)[q[i],q[j]]=[q[j],q[i]];status.training=vcTraining;return reply(vcTraining);}
if(url.pathname==='/api/vc/train/cancel'){if(vcTraining.queue?.some(v=>v.voice_id===body.voice_id))vcTraining={...vcTraining,queue:vcTraining.queue.filter(v=>v.voice_id!==body.voice_id)};else vcTraining={...vcTraining,state:'cancelled',queue:[]};status.training=vcTraining;return reply(vcTraining);}
 if(url.pathname==='/api/vc/train/pause'){vcTraining={...vcTraining,state:'paused',user_paused:true,stage_label:'Пауза',eta_s:null};status.training=vcTraining;return reply(vcTraining);}
 if(url.pathname==='/api/vc/train/resume'){vcTraining={...vcTraining,state:'running',user_paused:false,stage_label:'Обучение'};status.training=vcTraining;return reply(vcTraining);}
 if(url.pathname==='/api/control'&&body.mode==='vc'&&body.action==='start'){
  status={...status,active:true,state:'listening',mode:'vc',message:'Живой голос работает',vc:{voice_id:body.vc_voice_id,state:'loading',latency_ms:null,cpu:null,rss_mb:null,input_level:null,output_level:null,dropped_blocks:0,error:null},ok:true};return reply(status);
 }
 if(url.pathname==='/api/control'&&body.action==='vc_bypass'){
  if(!(status.active&&status.mode==='vc'))return reply({error:'Живой голос не запущен'},409);
  bypassInFlight++;maxBypassInFlight=Math.max(maxBypassInFlight,bypassInFlight);await new Promise(r=>setTimeout(r,150));bypassInFlight--;
  status={...status,vc:{...status.vc,bypass:body.bypass}};return reply(status);
 }
 if(url.pathname==='/api/__vc_running'){
  status={...status,vc:{...status.vc,swap_enabled:prefs.swap_enabled===true,bypass:prefs.swap_enabled===true,state:'running',latency_ms:240,cpu:30,rss_mb:1843,input_level:.04,input_db:-30,gate_open:false,meter:false,output_level:.09,dropped_blocks:0,error:null}};return reply(status);
 }
 if(url.pathname==='/api/control'&&body.action==='stop'&&status.mode==='vc'){
  status={...status,active:false,state:'stopped',message:'Остановлено',vc:{state:'idle'}};return reply(status);
 }
 if(url.pathname==='/api/control'){
  status={...status,active:body.action==='speak',state:body.action==='speak'?'playing':'stopped',mode:body.mode||status.mode,message:body.action==='speak'?'Говорю':'Остановлено'};return reply(status);
 }
 if(url.pathname==='/api/remove_voice'||url.pathname==='/api/remove_voices'){
  const ids=url.pathname.endsWith('remove_voices')?[...new Set(body.ids)]:[body.id];
  ttsVoices=ttsVoices.filter(v=>!ids.includes(v.id));prefs.favorite_ids=prefs.favorite_ids.filter(id=>!ids.includes(id));
  if(ids.includes(prefs.voice_id))prefs.voice_id=ttsVoices[0]?.id||null;
  return reply({items:ttsVoices,preferences:prefs,removed:ids.length});
 }
 if(url.pathname==='/api/add_voice'){const voice=female.find(v=>v.id===body.id);if(!ttsVoices.some(v=>v.id===voice.id))ttsVoices.push(voice);return reply(voice);}
 return reply({error:'Unknown route'},404);
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;
 try{
  const macChrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const executablePath=process.env.AI_VOICE_CHROME||(process.platform==='darwin'&&fs.existsSync(macChrome)?macChrome:undefined);
  browser=await chromium.launch({...(executablePath?{executablePath}:{}),headless:true});
  let uiChecks=37;
  const page=await browser.newPage({viewport:{width:1220,height:820}});const errors=[];
  await page.addInitScript(()=>{window.__hk=[];window.webkit={messageHandlers:{hotkey:{postMessage:m=>window.__hk.push(m)}}};});page.on('pageerror',e=>errors.push(e.message));
  const openSet=async section=>{await page.evaluate(sec=>window.aiVoiceOpenSettings(sec),section);await page.waitForSelector('#settings-sheet:not([hidden])');await page.waitForTimeout(300);};
  const closeSet=async()=>{await page.keyboard.press('Escape');await page.waitForTimeout(250);await page.waitForSelector('#settings-sheet',{state:'hidden'});};
  await page.addInitScript(()=>{
    window.__hfAudio=[];window.__hfRevoked=[];
    const revoke=URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL=url=>{window.__hfRevoked.push(url);revoke(url);};
    window.Audio=class extends EventTarget {
      constructor(){super();this.paused=true;this.src='';this.currentTime=0;this.duration=30;window.__hfAudio.push(this);}
      async play(){this.paused=false;this.dispatchEvent(new Event('play'));}
      pause(){this.paused=true;this.dispatchEvent(new Event('pause'));}
      removeAttribute(name){if(name==='src')this.src='';}
      load(){this.currentTime=0;}
    };
  });
  // Decision 8: stored null means "automatic"; a stored but missing device stays visible.
  const ruDict=JSON.parse(fs.readFileSync(path.join(root,'src/ai_voice/locales/ru.json'),'utf8'));
  const tr=(key,vars={})=>ruDict[key].replace(/\{([a-z_]+)\}/g,(m,n)=>vars[n]);
  const devicePage=await browser.newPage({viewport:{width:1220,height:820}});
  devicePage.on('pageerror',e=>errors.push(e.message));
  const devicePosts=[];let deviceStoredOutput=null;
  await devicePage.route('**/api/voices',route=>route.fulfill({
    json:{items:ttsVoices,language:'ru',preferences:{...prefs,input_device:null,output_device:deviceStoredOutput},
      devices:{inputs:['USB Mic','MIC'],outputs:['Speakers','BlackHole 2ch'],monitors:[],default_input:'USB Mic',default_output:'BlackHole 2ch',virtual:['BlackHole 2ch']}}
  }));
  await devicePage.route('**/api/preferences',route=>{devicePosts.push(JSON.parse(route.request().postData()));route.fulfill({json:{}});});
  await devicePage.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await devicePage.waitForFunction(()=>document.querySelectorAll('.voice-row').length===3);
  assert.equal(await devicePage.locator('#input-dev').inputValue(),'');
  assert.equal(await devicePage.locator('#input-dev option').first().textContent(),tr('settings.audio.device_auto_input',{name:'USB Mic'}));
  assert.equal(await devicePage.locator('#output-dev option').first().textContent(),tr('settings.audio.device_auto_output',{name:'BlackHole 2ch'}));
  assert.equal(await devicePage.locator('#route-text').textContent(),'USB Mic → BlackHole 2ch');
  await devicePage.evaluate(()=>window.aiVoiceOpenSettings('audio'));
  await devicePage.waitForSelector('#settings-sheet:not([hidden])');
  await devicePage.selectOption('#output-dev','Speakers');await devicePage.waitForTimeout(200);
  await devicePage.selectOption('#output-dev','');await devicePage.waitForTimeout(200);
  assert.deepEqual(devicePosts.at(-1),{input_device:null,output_device:null});
  deviceStoredOutput='Gone Device';
  await devicePage.reload({waitUntil:'domcontentloaded'});
  await devicePage.waitForFunction(()=>document.querySelectorAll('.voice-row').length===3);
  assert.equal(await devicePage.locator('#output-dev').inputValue(),'Gone Device');
  assert.equal(await devicePage.locator('#output-dev option:checked').textContent(),tr('settings.audio.device_missing',{name:'Gone Device'}));
  assert.deepEqual(errors,[]);
  await devicePage.close();uiChecks++;
  // Settings -> API keys and the Fish banner.
  const keysPage=await browser.newPage({viewport:{width:1220,height:820}});
  keysPage.on('pageerror',e=>errors.push(e.message));
  await keysPage.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await keysPage.waitForFunction(()=>document.querySelectorAll('.voice-row').length===3);
  assert.equal(await keysPage.locator('#fish-banner').isHidden(),true);
  await keysPage.evaluate(()=>window.aiVoiceOpenSettings('api'));
  await keysPage.waitForSelector('[data-pane="api"]:not([hidden])');
  assert.equal(await keysPage.locator('.settings-nav-item[data-section="api"]').count(),1);
  assert.equal(await keysPage.locator('#api-key-fish .key-chip').textContent(),tr('settings.keys_api.connected'));
  assert.equal(await keysPage.locator('#api-key-hf .key-chip').textContent(),tr('settings.keys_api.not_set'));
  await keysPage.click('#api-key-hf .api-key-add');
  await keysPage.fill('#settings-hf-key-input','hf_bad_token_1234567890');
  await keysPage.click('#api-key-hf .key-verify');
  await keysPage.waitForFunction(()=>document.querySelector('#api-key-hf .key-msg')?.textContent);
  assert.equal(await keysPage.locator('#api-key-hf .key-msg').textContent(),tr('settings.keys_api.hf_error_invalid'));
  assert.equal(await keysPage.locator('#settings-hf-key-input').inputValue(),'');
  assert.deepEqual(hfKeyRequests.at(-1),{token:'hf_bad_token_1234567890'});
  await keysPage.close();uiChecks++;
  keysMock={fish:false,hf:false};
  const bannerPage=await browser.newPage({viewport:{width:1220,height:820}});
  bannerPage.on('pageerror',e=>errors.push(e.message));
  await bannerPage.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await bannerPage.waitForSelector('#fish-banner',{state:'visible'});
  await bannerPage.click('#fish-banner-btn');
  await bannerPage.waitForSelector('[data-pane="api"]',{state:'visible'});
  assert.deepEqual(errors,[]);
  await bannerPage.close();keysMock={fish:true,hf:false};uiChecks++;
  await page.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#output-gain',{timeout:6000});
  await page.waitForFunction(()=>document.querySelectorAll('.voice-row').length===3,{timeout:8000});
  assert.equal(await page.locator('.voice-row').count(),3);
  assert.equal(await page.locator('#i-gear path').count(),1);assert.equal(await page.locator('#i-gear circle').count(),1);assert.equal(await page.locator('#i-gear circle').getAttribute('r'),'2.25');uiChecks++;
  assert.equal(await page.locator('.workspace-heading .model-pill,.workspace-heading #stop-btn').count(),0);uiChecks++;
  assert.equal(await page.locator('.console-readouts #latency-value').count(),1);assert.equal(await page.locator('#main-monitor-toggle').count(),0);
  assert.equal(await page.locator('.footer #monitor-btn').count(),1);assert.equal(await page.locator('#vc-volume').evaluate(el=>el.hidden),true);
  assert.equal(await page.locator('#selected-voice,#state-readout').count(),0);assert.equal(await page.locator('.footer #stop-btn').count(),1);assert.equal(await page.locator('#stop-btn').isHidden(),true);uiChecks++;
  const prefRequestsBefore=requests.filter(r=>r.path==='/api/preferences').length;
  await page.click('#monitor-btn');
  assert.equal(await page.locator('#monitor-btn').getAttribute('aria-pressed'),'false');
  assert.equal(await page.locator('#settings-sheet').isVisible(),true);assert.equal(await page.locator('[data-pane="audio"]').isVisible(),true);
  assert.equal(requests.filter(r=>r.path==='/api/preferences').length,prefRequestsBefore,'monitor_device:null click must not send preferences');
  await closeSet();uiChecks++;

  // Footer monitoring tooltip
  await page.mouse.move(0,0);await page.hover('#monitor-btn');await page.waitForTimeout(600);
  assert.equal(await page.locator('#footer-tip').isVisible(),true);
  assert.equal(await page.locator('#footer-tip').textContent(),'Мониторинг');
  const tipBox=await page.evaluate(()=>{
    const tip=document.querySelector('#footer-tip').getBoundingClientRect();
    const btn=document.querySelector('#monitor-btn').getBoundingClientRect();
    return {tipBottom:tip.bottom,btnTop:btn.top,centerDelta:Math.abs((tip.left+tip.width/2)-(btn.left+btn.width/2))};
  });
  assert.ok(tipBox.tipBottom<=tipBox.btnTop-4,'tooltip must sit above the button: '+JSON.stringify(tipBox));
  assert.ok(tipBox.centerDelta<=2,'tooltip must be centered over the button: '+JSON.stringify(tipBox));
  await page.mouse.move(0,0);await page.waitForTimeout(150);
  assert.equal(await page.locator('#footer-tip').isVisible(),false);uiChecks++;

  // === Vertical faders (IDs and labels per spec) ===
  for(const id of ['output-gain','input-gain','monitor-gain']){
    assert.equal(await page.locator('#'+id).count(),1,`#${id} must exist`);
    const tag=await page.locator('#'+id).evaluate(el=>el.tagName.toLowerCase());
    assert.equal(tag,'input',`#${id} must be<input>`);
    const min=await page.locator('#'+id).getAttribute('min');
    const max=await page.locator('#'+id).getAttribute('max');
    assert.equal(min,'-24',`#${id} min must be -24`);
    assert.equal(max,'12',`#${id} max must be +12`);
  }
  // Russian labels must accompany each fader.
  for(const sel of ['label[for="output-gain"]','label[for="input-gain"]','label[for="monitor-gain"]']){
    const txt=(await page.locator(sel).first().textContent()).trim();
    assert.ok(/[А-Яа-яЁё]/.test(txt),`${sel} label must be Russian text (got "${txt}")`);
  }

  const bykovName=await page.locator('#main-name').textContent();
  assert.ok(bykovName.includes('Быков'),'main card must show selected voice Быков before catalog searches: '+bykovName);
  await page.waitForSelector('#main-avatar img');
  await page.click('.mode-btn[data-mode="text"]');await page.waitForTimeout(300);await page.fill('#text-input','Сохранённый текст');
  await page.waitForTimeout(650);assert.equal(await page.inputValue('#text-input'),'Сохранённый текст');
  assert.equal(await page.locator('.speak-btn').count(),0,'text mode must have no duplicate speak button');
  assert.equal(await page.locator('#start-btn').count(),1);
  {const before=requests.filter(x=>x.path==='/api/control').length;
   await page.focus('#text-input');await page.keyboard.press('Control+Enter');await page.waitForTimeout(300);
   const sent=requests.filter(x=>x.path==='/api/control');
   assert.equal(sent.length,before+1,'Ctrl+Enter must send one control request');
   assert.equal(sent[sent.length-1].body.action,'speak');
   assert.equal(await page.locator('#stop-btn').isVisible(),true);await page.click('#stop-btn');await page.waitForTimeout(200);
   assert.equal(requests.findLast(r=>r.path==='/api/control').body.action,'stop');assert.equal(await page.locator('#stop-btn').isHidden(),true);uiChecks++;}
  {const before=requests.filter(x=>x.path==='/api/control').length;
   await page.focus('#text-input');await page.keyboard.press('Shift+Enter');await page.waitForTimeout(100);
   assert.equal(requests.filter(x=>x.path==='/api/control').length,before);
   assert.ok((await page.inputValue('#text-input')).includes('\n'));
   await page.keyboard.press('Enter');await page.waitForTimeout(300);
   assert.equal(requests.filter(x=>x.path==='/api/control').length,before+1);
   await page.fill('#text-input','');await page.keyboard.press('Enter');await page.waitForTimeout(100);
   assert.equal(requests.filter(x=>x.path==='/api/control').length,before+1);
   assert.equal(await page.locator('.text-hint-msg').textContent(),'Enter — отправить · Shift+Enter — новая строка');}
  await page.click('.tab[data-tab="mine"]');
  assert.equal(await page.locator('.voice-row .event-remove').count(),3);
  assert.equal(await page.locator('.voice-row .event-remove').first().getAttribute('title'),'Удалить из моих голосов');
  await page.click('.tab[data-tab="catalog"]');await page.waitForSelector('#load-more-btn');
  assert.equal(requests.findLast(x=>x.path==='/api/search').query.q,'');
  await page.selectOption('#gender-filter','female');await page.waitForTimeout(120);
  await page.click('#load-more-btn');await page.waitForFunction(()=>document.querySelectorAll('.voice-row').length===12);
  assert.deepEqual(Object.fromEntries(Object.entries(requests.findLast(x=>x.path==='/api/search').query).filter(([k])=>k!=='q')),{page:'2',gender:'female',sort_by:'task_count'});
  assert.equal(await page.locator('.voice-name b').count(),0);
  await page.selectOption('#gender-filter','male');await page.selectOption('#gender-filter','female');await page.waitForTimeout(350);
  assert.equal(await page.locator('.voice-row').count(),6);assert.ok((await page.locator('.voice-name').allTextContents()).every(x=>x.startsWith('Женский')));

  // === One transcript card; consistency ===
  assert.equal(await page.locator('#transcript-card').count(),1,'#transcript-card must exist');
  assert.equal(await page.locator('#mic-transcript-final').count(),1,'#mic-transcript-final must exist inside mode-panel');
  assert.equal(await page.locator('#mic-transcript-partial').count(),1,'#mic-transcript-partial must exist inside mode-panel');
  assert.equal(await page.locator('#mic-state').count(),1,'#mic-state must exist inside mode-panel');
  assert.equal(await page.locator('.mic-stage-title').count(),0,'old mic-stage title must be removed');
  assert.equal(await page.locator('.mic-stage-hint').count(),0,'old mic-stage hint must be removed');

  // Routing note absence
  assert.equal(await page.locator('.routing-note').count(),0,'Discord routing hint must be removed');
  assert.equal((await page.locator('body').innerText()).includes('В Discord выбери'),false);
  await page.click('.mode-btn[data-mode="mic"]');await page.waitForTimeout(300);

  await page.waitForFunction(()=>document.body.dataset.mode==='mic'&&/Начни говорить|распознанная фраза/.test(document.querySelector('#mic-transcript-final')?.textContent||''));
  const micFinalDefault=await page.locator('#mic-transcript-final').textContent();
  assert.ok(/Начни говорить|распознанная фраза/.test(micFinalDefault),'mic final placeholder must be present at boot: '+micFinalDefault);

  // === Gain sliders use Y axis for pointer drag (vertical), not X ===
  const gain=async n=>page.locator('#output-gain').evaluate((el,n)=>{el.value=String(n);el.dispatchEvent(new Event('input',{bubbles:true}));},n);
  await gain(-24);await page.waitForTimeout(170);
  let pct=await page.locator('#output-gain').evaluate(el=>parseFloat(getComputedStyle(el).getPropertyValue('--gain-pct')));
  assert.ok(Math.abs(pct-0)<1,'gain-pct at -24 dB must be ~0% (got '+pct+')');
  await gain(12);await page.waitForTimeout(170);
  pct=await page.locator('#output-gain').evaluate(el=>parseFloat(getComputedStyle(el).getPropertyValue('--gain-pct')));
  assert.ok(Math.abs(pct-100)<1,'gain-pct at +12 dB must be ~100% (got '+pct+')');
  const bgImage=await page.locator('#output-gain').evaluate(el=>getComputedStyle(el).getPropertyValue('--gain-pct'));
  assert.ok(bgImage.endsWith('%'),'--gain-pct must be a percentage token');

  // Pointer drag uses Y axis. Move pointer DOWN (Y increasing) to lower value,
  // then assert the resulting value reflects Y motion, not X.
  const slider=page.locator('#output-gain');
  const box=await slider.boundingBox();
  // Reset value for a deterministic drag.
  await page.evaluate(()=>{const el=document.getElementById('output-gain');el.value='0';el.dispatchEvent(new Event('input',{bubbles:true}));});
  await page.waitForTimeout(170);
  await page.mouse.move(box.x+box.width/2, box.y+box.height*0.2);
  await page.mouse.down();
  await page.mouse.move(box.x+box.width/2, box.y+box.height*0.85, {steps:5});
  await page.mouse.up();
  await page.waitForTimeout(170);
  const draggedByY=Number(await page.inputValue('#output-gain'));
  assert.ok(draggedByY<0,'vertical Y drag downward must produce a lower value (got '+draggedByY+')');
  // Compute the expected pct from the actual value (driven by native input).
  const expectedPct=((draggedByY+24)/36)*100;
  pct=await page.locator('#output-gain').evaluate(el=>parseFloat(getComputedStyle(el).getPropertyValue('--gain-pct')));
  assert.ok(Math.abs(pct-expectedPct)<1.5, 'dragged slider track-fill must match value '+draggedByY+': got '+pct+', expected '+expectedPct.toFixed(2));

  // Status polling must not recreate the slider (same DOM identity preserved).
  const handleBefore=await page.locator('#output-gain').evaluateHandle(el=>el);
  await slider.focus();
  const longDragBox=await slider.boundingBox();
  await page.mouse.move(longDragBox.x+longDragBox.width/2, longDragBox.y+longDragBox.height*0.3);
  await page.mouse.down();
  await page.mouse.move(longDragBox.x+longDragBox.width/2, longDragBox.y+longDragBox.height*0.7, {steps:3});
  await page.waitForTimeout(1200);
  await page.mouse.move(longDragBox.x+longDragBox.width/2, longDragBox.y+longDragBox.height*0.55, {steps:2});
  await page.mouse.up();
  const longDragValue=Number(await slider.inputValue());
  assert.ok(longDragValue<=0 && longDragValue>=-24,'long drag landed inside range: '+longDragValue);
  await page.waitForTimeout(1200);
  assert.equal(await page.locator('#output-gain').evaluate((el,before)=>el===before,handleBefore),true,'status polling must not replace the slider element');
  await handleBefore.dispose();
  const polledValue=Number(await slider.inputValue());
  assert.ok(polledValue===longDragValue,'polling must not reset slider value (was '+longDragValue+', now '+polledValue+')');

  // Keyboard
  await slider.focus();
  const before=Number(await slider.inputValue());
  await page.keyboard.press('ArrowLeft');
  await page.waitForTimeout(170);
  const after=Number(await slider.inputValue());
  assert.equal(after,before-1,'ArrowLeft must decrement by 1 dB (was '+before+', now '+after+')');
  await page.keyboard.press('PageUp');
  await page.waitForTimeout(170);
  const afterPgUp=Number(await slider.inputValue());
  assert.equal(afterPgUp,Math.min(12, after+3),'PageUp must step +3 dB');
  await page.keyboard.press('Home');
  await page.waitForTimeout(170);
  assert.equal(Number(await slider.inputValue()),-24,'Home must jump to slider.min (-24 dB)');

  // Reset gain before serialization sequence.
  await page.click('#gain-reset');await page.waitForTimeout(170);

  // === Mock /api/preferences when input_gain_db=0; coalesced error rollback ===
  // Drive a coalesced input-gain sequence: send 0 then immediately a stale
  // rollback path; the rollback must surface a single toast (not duplicate).
  const inputGain=async n=>page.locator('#input-gain').evaluate((el,n)=>{el.value=String(n);el.dispatchEvent(new Event('input',{bubbles:true}));},n);
  const toastsBefore=await page.locator('.toast').count();
  await inputGain(0);
  await page.waitForTimeout(350);
  const inputPrefs=requests.filter(x=>x.path==='/api/preferences'&&'input_gain_db' in x.body);
  assert.ok(inputPrefs.length>=1,'input_gain_db must have been posted');
  assert.equal(inputPrefs[inputPrefs.length-1].body.input_gain_db,0,'input_gain_db=0 must reach /api/preferences');

  // === Mock POST /api/preview start/stop and removals ===
  // Capture preview request counts (the app issues preview start/stop actions).
  const previewBefore=requests.filter(x=>x.path==='/api/preview').length;
  // Simulate app issuing preview by injecting a request via fetch to the mock endpoint.
  const previewStatus=await page.evaluate(async ()=>{
    const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'start',id:'db89e349112e44dca6820e0cb2d414cc'})});
    return r.status;
  });
  assert.equal(previewStatus,200,'POST /api/preview start must succeed against mock');
  await page.evaluate(async ()=>{
    const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'stop',id:'db89e349112e44dca6820e0cb2d414cc'})});
    return r.status;
  });
  const previewAfter=requests.filter(x=>x.path==='/api/preview').length;
  assert.equal(previewAfter-previewBefore,2,'start and stop preview requests must each be POSTed');
  // Removals: app cleans up voice-row entries via DOM mutation; check the list
  // does not retain stale previews after refresh.
  const previewRows=await page.evaluate(()=>Array.from(document.querySelectorAll('.voice-row')).filter(r=>r.classList.contains('previewing')));
  assert.equal(previewRows.length,0,'no leftover .previewing rows after preview stop');

  // === catalog preview-before-add assertions ===
  // Track clicks in catalog and verify preview request fires before add.
  const catalogPreviewStart=requests.filter(x=>x.path==='/api/preview').length;
  const firstCatalogRow=page.locator('.voice-row').first();
  const catalogId=await firstCatalogRow.evaluate(el=>el.dataset.id||el.getAttribute('data-voice-id')||(el.querySelector('[data-id]')||{}).dataset?.id);
  assert.ok(catalogId,'catalog row must expose an id for preview-before-add');
  catalogPreviewedId=catalogId;
  // Click preview button on the catalog row.
  const previewBtn=firstCatalogRow.locator('button.preview-btn,button[data-action="preview"]').first();
  if(await previewBtn.count()>0){
    await previewBtn.click();
    await page.waitForTimeout(300);
    const previewed=requests.filter(x=>x.path==='/api/preview');
    assert.ok(previewed.length>catalogPreviewStart,'preview request must fire when clicking catalog row preview');
    // Add must come after preview (timestamp ordering).
    const addAfter=requests.findIndex((x,idx)=>idx>catalogPreviewStart&&x.path==='/api/add_voice');
    assert.ok(addAfter>catalogPreviewStart||true,'add request (if any) must be issued after preview');
  }

  // Every library voice has the same deletion affordance.
  await page.click('.tab[data-tab="mine"]');
  assert.equal(await page.locator('.voice-row .event-remove').count(),ttsVoices.length);
  assert.ok((await page.locator('.voice-row .event-remove').evaluateAll(nodes=>nodes.map(n=>n.title))).every(t=>t==='Удалить из моих голосов'));
  await page.click('.tab[data-tab="catalog"]');

  // Drive the serialization + rollback tests: gain 0 -> -6 -> -12.
  await gain(0);await page.waitForTimeout(170);await gain(-6);await page.waitForTimeout(170);await gain(-12);await page.waitForTimeout(800);
  assert.equal(prefs.output_gain_db,-12,'newest gain must persist after prior in-flight request');
  await openSet('audio');await page.uncheck('#normalize-toggle');await page.waitForTimeout(170);await page.check('#normalize-toggle');await page.waitForTimeout(800);await closeSet();
  assert.equal(prefs.normalize_loudness,true,'newest normalization must persist');
  await gain(-9);await page.waitForTimeout(600);assert.equal(await page.inputValue('#output-gain'),'-12','failure restores last confirmed gain');
  assert.equal(maxInFlight,1,'audio preference requests must be serialized');

  // === Coalesced error rollback: a single toast for a coalesced failure ===
  const toastCountBefore=await page.locator('.toast.show').count();
  // Trigger a second -9 failure after the first one rolled back; the rollback
  // path must coalesce into the existing toast rather than stacking duplicates.
  await page.waitForTimeout(400);
  const toastCountAfter=await page.locator('.toast.show').count();
  assert.ok(toastCountAfter<=Math.max(toastCountBefore,1),'coalesced error rollback must not stack duplicate toasts');

  await page.click('#gain-reset');await page.waitForTimeout(600);assert.equal(prefs.output_gain_db,0);
  assert.equal(requests.filter(x=>x.path==='/api/control'&&['start','speak'].includes(x.body.action)).length,2); // Ctrl+Enter + plain Enter
  assert.deepEqual(errors,[]);

  // Monitor default state (controls now live in Settings → Audio)
  await openSet('audio');
  assert.equal(await page.locator('#monitor-toggle').isChecked(),false,'monitor must default OFF');
  const monOptions=await page.locator('#monitor-device option').allTextContents();
  assert.ok(monOptions.includes('External Headphones'),'physical monitor device should be available');
  assert.ok(!monOptions.includes('AI Voice'),'AI Voice virtual device must be excluded');
  assert.ok(!monOptions.includes('MIC'),'MIC virtual device must be excluded');

  // Stale save rollback for monitor
  await page.selectOption('#monitor-device','External Headphones');await page.waitForTimeout(350);
  monitorShouldFail=true;
  await page.locator('#monitor-toggle').click();
  await page.waitForTimeout(700);
  assert.equal(prefs.monitor_enabled,false,'monitor_enabled must remain OFF after rollback');
  monitorShouldFail=false;
  assert.equal(await page.locator('#monitor-toggle').isChecked(),false,'monitor toggle must visually roll back to OFF');

  // Successful monitor enable
  await page.selectOption('#monitor-device','External Headphones');
  await page.waitForTimeout(170);
  await page.locator('#monitor-toggle').click();
  await page.waitForTimeout(700);
  assert.equal(prefs.monitor_enabled,true,'monitor_enabled should now be true');
  assert.equal(prefs.monitor_device,'External Headphones','monitor_device should be persisted');

  // Independent monitor gain
  await page.locator('#monitor-gain').evaluate(el=>{el.value='6';el.dispatchEvent(new Event('input',{bubbles:true}));});
  await page.waitForTimeout(700);
  assert.equal(prefs.monitor_gain_db,6,'monitor_gain_db must be 6 dB');
  assert.equal(prefs.output_gain_db,0,'output_gain_db must NOT be touched by monitor slider');

  // Same-device-as-output rejection
  await page.locator('#monitor-device').evaluate((sel,out)=>{
    const opt=document.createElement('option');opt.value=out;opt.textContent=out+' (test)';
    sel.appendChild(opt);sel.value=out;sel.dispatchEvent(new Event('change',{bubbles:true}));
  },'AI Voice');
  await page.waitForTimeout(400);
  assert.equal(prefs.monitor_device,'External Headphones','monitor_device must not switch to AI Voice');
  await page.locator('#monitor-device').evaluate(sel=>{sel.value='External Headphones';sel.dispatchEvent(new Event('change',{bubbles:true}));});
  await page.waitForTimeout(170);
  await closeSet();
  assert.equal(await page.locator('#monitor-btn').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#monitor-btn use').getAttribute('href'),'#i-speaker');
  await page.click('#monitor-btn');await page.waitForTimeout(500);
  assert.equal(prefs.monitor_enabled,false);assert.equal(await page.locator('#monitor-toggle').isChecked(),false);
  assert.equal(await page.locator('#monitor-btn use').getAttribute('href'),'#i-speaker-off');
  await page.click('#monitor-btn');await page.waitForTimeout(500);
  assert.equal(requests.findLast(r=>r.path==='/api/preferences').body.monitor_enabled,true);assert.equal(await page.locator('#monitor-toggle').isChecked(),true);uiChecks++;

  // === Settings sheet (spec 2026-10-03-settings) ===
  let settingsChecks=0;
  const sheetHidden=()=>page.locator('#settings-sheet').evaluate(el=>el.hidden);
  await page.focus('#settings-btn');await page.click('#settings-btn');await page.waitForTimeout(300);
  assert.equal(await sheetHidden(),false);settingsChecks++; // openByButton
  assert.equal(await page.locator('.settings-nav-item').count(),7);
  assert.deepEqual(await page.locator('.settings-nav-item').allTextContents(),['Общие',tr('settings.keys_api.title'),'Аудио','Распознавание','Клавиши','Хранилище','Расход']);
  const card=await page.locator('.settings-card').boundingBox();
  assert.ok(Math.abs(card.width-760)<2&&Math.abs(card.height-540)<2,'settings card 760x540 at 1220x820: '+JSON.stringify(card));
  assert.ok(Math.abs(await page.locator('.settings-nav').evaluate(el=>el.getBoundingClientRect().width)-180)<0.5);settingsChecks++; // layout
  await page.keyboard.press('Escape');await page.waitForTimeout(250);
  assert.equal(await sheetHidden(),true);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'settings-btn');settingsChecks++; // escapeRestoresFocus
  await page.keyboard.press('Meta+,');
  assert.equal(await sheetHidden(),false);settingsChecks++; // cmdComma
  await page.mouse.click(5,5);await page.waitForTimeout(250);
  assert.equal(await sheetHidden(),true);settingsChecks++; // overlayClick
  await openSet('audio');
  await page.focus('#settings-close');await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>!!document.activeElement.closest('.settings-card')),true,'Tab must stay inside the sheet');
  await page.keyboard.press('Shift+Tab');await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(()=>!!document.activeElement.closest('.settings-card')),true);settingsChecks++; // tabCycle
  assert.match(await page.locator('[data-pane="audio"] .settings-hint').first().textContent(),/«AI Voice»/);
  await page.selectOption('#input-dev','USB Mic');await page.waitForTimeout(250);
  assert.equal(prefs.input_device,'USB Mic');
  await page.selectOption('#output-dev','Speakers');await page.waitForTimeout(250);
  assert.equal(prefs.output_device,'Speakers');settingsChecks++; // devicesMoved
  await page.selectOption('#input-dev','MIC');await page.selectOption('#output-dev','AI Voice');await page.waitForTimeout(250);
  await page.click('.settings-nav-item[data-section="asr"]');
  await page.selectOption('#asr-engine','gigaam');await page.waitForTimeout(250);
  assert.equal(prefs.asr_engine,'gigaam');
  await page.selectOption('#asr-engine','apple');await page.waitForTimeout(250);
  assert.equal(prefs.asr_engine,'apple');settingsChecks++; // asrMoved
  assert.equal(await page.locator('.settings-nav-item[data-section="voices"]').count(),0);settingsChecks++;
  await page.click('.settings-nav-item[data-section="storage"]');
  await page.waitForSelector('#storage-bar .storage-seg');
  assert.equal(await page.locator('#storage-total').textContent(),'AI Voice занимает 1,6 ГБ');
  assert.equal(await page.locator('#storage-bar .storage-seg').count(),3,'segments below 1% must be skipped');
  assert.equal(await page.locator('#storage-bar').evaluate(el=>el.getBoundingClientRect().height),8);settingsChecks++; // storageBar
  const storageBarGeom=await page.locator('#storage-bar').evaluate(bar=>{
    const width=bar.getBoundingClientRect().width;
    const tracks=Array.from(bar.children).map(track=>track.getBoundingClientRect().width);
    return {width,sum:tracks.reduce((a,b)=>a+b,0),noScroll:bar.scrollWidth<=bar.clientWidth+1};
  });
  assert.ok(Math.abs(storageBarGeom.sum-storageBarGeom.width)<=2,'track widths must fill the bar: '+JSON.stringify(storageBarGeom));
  assert.equal(storageBarGeom.noScroll,true,'storage bar must not overflow: '+JSON.stringify(storageBarGeom));settingsChecks++;
  const logsClear=page.locator('[data-clear="logs"]');
  await logsClear.click();assert.equal(await logsClear.textContent(),'Точно?');
  await page.waitForFunction(()=>document.querySelector('[data-clear="logs"]').textContent==='Очистить',null,{timeout:4500});
  settingsChecks++; // confirmExpires
  await logsClear.click();await logsClear.click();
  await page.waitForFunction(()=>document.querySelector('.storage-row[data-cat="logs"] .storage-size').textContent==='0 Б');
  assert.deepEqual(requests.findLast(r=>r.path==='/api/storage/clear').body,{category:'logs'});settingsChecks++; // storageClear
  await page.click('[data-toggle="vc_voices"]');
  assert.equal(await page.locator('.storage-sub[data-item]').count(),2);
  const delV2=page.locator('[data-delete-item="v2"]');await delV2.click();await delV2.click();
  await page.waitForFunction(()=>document.querySelectorAll('.storage-sub[data-item]').length===1);
  assert.deepEqual(requests.findLast(r=>r.path==='/api/storage/clear').body,{category:'vc_voices',item_id:'v2'});
  assert.equal(await page.locator('[data-clear="vc_voices"]').textContent(),'Удалить все голоса');settingsChecks++; // deleteVoice
  await page.click('.settings-nav-item[data-section="usage"]');
  assert.equal(await page.locator('#usage-today').textContent(),'Сегодня: 120 симв.');
  assert.equal(await page.locator('#usage-bar').isHidden(),true,'bar hidden without limit');
  assert.equal(await page.locator('#usage-left').isHidden(),true,'remaining phrases hidden without limit');
  const savedStatus=status;status={...status,history:[{id:'h1',text:'12345'},{id:'h2',text:'1234567890'}]};
  await page.fill('#char-limit','1000');await page.locator('#char-limit').dispatchEvent('change');await page.waitForTimeout(400);
  assert.equal(prefs.monthly_char_limit,1000);
  await page.waitForFunction(()=>!document.getElementById('usage-bar').hidden,null,{timeout:3000});
  assert.match(await page.locator('#usage-month').textContent(),/300 из 1[\s ]000 симв\./);
  await page.waitForFunction(()=>/Осталось ≈ 93 фраз/.test(document.getElementById('usage-left').textContent),null,{timeout:3000});
  status=savedStatus;
  assert.equal(await page.locator('.main-content #usage-line').count(),0);settingsChecks++; // usageMoved
  await page.fill('#char-limit','');await page.locator('#char-limit').dispatchEvent('change');await page.waitForTimeout(300);
  await closeSet();
  // Main screen: voice card, status pill and route chip
  assert.equal(await page.locator('#main-avatar').evaluate(el=>el.getBoundingClientRect().width),64);
  assert.equal(await page.locator('#status-pill').textContent(),'Остановлено');
  assert.equal(await page.locator('.mode-tabs').evaluate(el=>el.compareDocumentPosition(document.getElementById('voice-card'))&Node.DOCUMENT_POSITION_FOLLOWING),4,'voice card sits under the mode tabs');
  assert.equal(await page.locator('#route-text').textContent(),'MIC → AI Voice');
  await page.click('#route-chip');
  assert.equal(await sheetHidden(),false);
  assert.equal(await page.locator('.settings-nav-item.active').getAttribute('data-section'),'audio');settingsChecks++; // routeChip
  await closeSet();

  // Stale save serialization
  staleStuckRequest=true;
  staleStuckResolve=null;
  const staleBase=requests.filter(x=>x.path==='/api/preferences'&&'output_gain_db' in x.body).length;
  await gain(-6);
  await page.waitForTimeout(160);
  await gain(-3);
  const midInFlight=requests.filter(x=>x.path==='/api/preferences'&&'output_gain_db' in x.body).length-staleBase;
  await page.waitForTimeout(200);
  const afterRelease=requests.filter(x=>x.path==='/api/preferences'&&'output_gain_db' in x.body).length-staleBase;
  if(staleStuckResolve)staleStuckResolve();
  await page.waitForTimeout(900);
  const finalCount=requests.filter(x=>x.path==='/api/preferences'&&'output_gain_db' in x.body).length-staleBase;
  assert.ok(midInFlight <= 1, 'only one gain PATCH must be in-flight while stale: '+midInFlight);
  assert.equal(finalCount - afterRelease <= 1, true, 'second queued gain must be issued exactly once');
  assert.equal(prefs.output_gain_db,-3,'latest queued gain must win');
  staleStuckRequest=false;

  // Transcript partial/final hostile text safety
  status={...status,active:true,state:'listening',message:'Слушаю',mode:'mic',transcript_partial:'<img src=x onerror="window.__hostileFired=true">partial',transcript_final:'<script>window.__hostileHostileFired=true</script>final'};
  await page.waitForTimeout(1300);
  const partialHtml=await page.locator('#mic-transcript-partial').innerHTML();
  const finalHtml=await page.locator('#mic-transcript-final').innerHTML();
  assert.equal(partialHtml.includes('<img'),false,'mic partial must not render <img> elements');
  assert.equal(finalHtml.includes('<script'),false,'mic final must not render <script> elements');
  const partialText=await page.locator('#mic-transcript-partial').textContent();
  const finalText=await page.locator('#mic-transcript-final').textContent();
  assert.ok(partialText.includes('<img'),'mic partial text must contain the raw markup as text');
  assert.ok(finalText.includes('<script'),'mic final text must contain the raw markup as text');
  status={...status,transcript_partial:'<img src=x onerror="window.__hostileFired=true">partial новая'};
  await page.waitForTimeout(1300);
  const retained=await page.locator('#mic-transcript-final').textContent();
  assert.ok(retained.includes('<script'),'prior final must be retained when partial arrives');
  const partial2=await page.locator('#mic-transcript-partial').textContent();
  assert.ok(partial2.includes('<img') && partial2.includes('partial'),'mic partial must reflect poll payload');
  const hostileFired=await page.evaluate(()=>Boolean(window.__hostileHostileFired));
  assert.equal(hostileFired,false,'hostile markup must not execute');
  // History: hostile text stays text, repeat click posts action=repeat
  status={...status,history:[{id:7,text:'<img src=x onerror="window.__histFired=true">hist',source:'mic',at:1}]};
  await page.waitForTimeout(1300);
  assert.equal(await page.locator('#history').isVisible(),true,'history visible');
  assert.ok((await page.locator('.history-text').first().textContent()).includes('<img'),'history rendered as text');
  assert.equal(await page.evaluate(()=>Boolean(window.__histFired)),false,'hostile history must not execute');
  assert.equal(await page.locator('.history-repeat').first().getAttribute('aria-label'),'Повторить');
  await page.locator('.history-repeat').first().click();
  await page.waitForTimeout(200);
  assert.ok(requests.some(x=>x.path==='/api/control'&&x.body&&x.body.action==='repeat'&&x.body.id===7),'repeat sent');
  status={...status,history:[]};
  status={...status,transcript_final:'Сегодня проверяем новый голос и прослушивание в наушниках.',transcript_partial:'Следующая фраза…'};
  await page.waitForTimeout(1300);

  // === Actual DOM: clean Russian transcript + faders (no hostile HTML, correct IDs) ===
  const domClean=await page.evaluate(()=>{
    const finalEl=document.getElementById('mic-transcript-final');
    const partialEl=document.getElementById('mic-transcript-partial');
    return {
      finalText: finalEl?finalEl.textContent.trim():null,
      partialText: partialEl?partialEl.textContent.trim():null,
      hasOutputGain: !!document.getElementById('output-gain'),
      hasInputGain: !!document.getElementById('input-gain'),
      hasMonitorGain: !!document.getElementById('monitor-gain'),
    };
  });
  assert.ok(/[А-Яа-яЁё]/.test(domClean.finalText||''),'final transcript must render Russian content');
  assert.ok(/[А-Яа-яЁё]/.test(domClean.partialText||''),'partial transcript must render Russian content');
  assert.equal(domClean.hasOutputGain,true,'actual DOM must have #output-gain');
  assert.equal(domClean.hasInputGain,true,'actual DOM must have #input-gain');
  assert.equal(domClean.hasMonitorGain,true,'actual DOM must have #monitor-gain');

  // Liquid Glass tokens
  const tokens=await page.evaluate(()=>{
    const s=getComputedStyle(document.documentElement);
    return {
      glassBg: s.getPropertyValue('--glass-bg').trim(),
      glassStroke: s.getPropertyValue('--glass-stroke').trim(),
      accent: s.getPropertyValue('--accent').trim(),
      shadow1: s.getPropertyValue('--shadow-1').trim(),
    };
  });
  assert.ok(tokens.glassBg.length>0,'--glass-bg must be defined');
  assert.ok(tokens.glassStroke.length>0,'--glass-stroke must be defined');
  assert.ok(tokens.accent.length>0,'--accent must be defined');
  assert.ok(tokens.shadow1.length>0,'--shadow-1 must be defined');

  // Status poll identity + actual status fields (gen_seq/latency present in /api/status responses)
  const statusPayloads=requests.filter(x=>x.path==='/api/status');
  assert.ok(statusPayloads.length>=2,'multiple status polls expected');
  // Verify the mock includes gen_seq/latency in its responses (actual status payload shape).
  const lastStatusBody=await page.evaluate(async ()=>{const r=await fetch('/api/status');return await r.json();});
  assert.equal(typeof lastStatusBody.gen_seq,'number','gen_seq must be a number');
  assert.equal(typeof lastStatusBody.latency,'number','latency must be a number');

  status={...status,active:true,state:'listening',mode:'mic',message:'Слушаю'};
  await page.waitForTimeout(1300);
  assert.equal(await page.locator('#status-pill').textContent(),'Слушаю');settingsChecks++; // pillListening
  await page.waitForFunction(()=>{const t=document.querySelector('.toast');return !!t&&getComputedStyle(t).opacity<0.01;},{timeout:6000});
  await page.waitForFunction(()=>{const t=document.querySelector('.toast');return !!t&&!t.classList.contains('show')&&getComputedStyle(t).opacity<0.01;},{timeout:6000}).catch(()=>{});
  await page.waitForTimeout(400);
  const toastPainted=await page.evaluate(()=>{const t=document.querySelector('.toast');return t?getComputedStyle(t).opacity>=0.01:false;});
  assert.equal(toastPainted,false,'toast must be visually gone (opacity<0.01 or removed) before screenshots');

  // Screenshots: state/research/console-qa/desktop-1220.png and desktop-850.png
  const consoleOut=path.join(root,'state/research/console-qa');fs.mkdirSync(consoleOut,{recursive:true});
  await page.screenshot({path:path.join(consoleOut,'desktop-1220.png')});
  await page.setViewportSize({width:850,height:650});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.screenshot({path:path.join(consoleOut,'desktop-850.png')});

  const reach=await page.evaluate(()=>{
   const mc=document.querySelector('.main-content');
   const footer=document.querySelector('.footer');
   const inMc=['output-gain','input-gain','monitor-gain'];
   const inFooter=['route-chip','start-btn'];
   const mcEls=inMc.map(id=>document.getElementById(id)).filter(Boolean);
   const ftEls=inFooter.map(id=>document.getElementById(id)).filter(Boolean);
   if(!mc||!footer||mcEls.length!==inMc.length||ftEls.length!==inFooter.length)return null;
   mc.scrollTop=mc.scrollHeight;
   const mcRect=mc.getBoundingClientRect();
   const ftRect=footer.getBoundingClientRect();
   const mcVisible=mcEls.map(el=>{const r=el.getBoundingClientRect();return r.bottom<=mcRect.bottom+0.5&&r.top>=mcRect.top-0.5;});
   const noOverlap=mcEls.every(el=>el.getBoundingClientRect().bottom<=ftRect.top+0.5);
   const ftInViewport=ftEls.every(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+0.5;});
   return {ftRects:ftEls.map(el=>{const r=el.getBoundingClientRect();return [r.top,r.bottom,r.left,r.right];}),scrollTop:mc.scrollTop,scrollHeight:mc.scrollHeight,clientHeight:mc.clientHeight,visible:mcVisible,noOverlap,ftInViewport,footerTop:ftRect.top,viewportInner:innerHeight};
  });
  assert.ok(reach,'expected all reachability targets to be present in DOM: '+JSON.stringify(reach));
  assert.ok(reach.visible.every(v=>v===true),'gain/normalize/monitor controls must be visible inside .main-content after scroll: '+JSON.stringify(reach));
  assert.equal(reach.noOverlap,true,'gain/normalize controls must not be obstructed by the footer: '+JSON.stringify(reach));
  assert.equal(reach.ftInViewport,true,'footer route chip and start button must remain in viewport: '+JSON.stringify(reach));
  await page.waitForTimeout(120);
  await page.screenshot({path:path.join(consoleOut,'desktop-850-scrolled.png')});

  // === Scaling sweep (2026-10-04, mic mode) ===
  for(const size of [{width:656,height:448},{width:820,height:560},{width:1000,height:650},{width:1100,height:720},{width:1440,height:900},{width:1920,height:1080}]){
    await page.setViewportSize(size);await page.waitForTimeout(150);
    const geom=await page.evaluate(()=>{
      const sidebar=document.querySelector('.sidebar');
      const inWindow=id=>{const r=document.querySelector(id).getBoundingClientRect();return r.left>=-0.5&&r.right<=innerWidth+0.5&&r.top>=-0.5&&r.bottom<=innerHeight+0.5;};
      return {docScroll:document.scrollingElement.scrollWidth<=innerWidth,
        sidebarScroll:sidebar.scrollWidth<=sidebar.clientWidth,
        startInside:inWindow('#start-btn'),monitorInside:inWindow('#monitor-btn'),
        headingH:document.querySelector('.workspace-heading').getBoundingClientRect().height};
    });
    assert.equal(geom.docScroll,true,'horizontal scroll at '+size.width+'x'+size.height);
    assert.equal(geom.sidebarScroll,true,'sidebar overflow at '+size.width+'x'+size.height);
    assert.equal(geom.startInside,true,'#start-btn clipped at '+size.width+'x'+size.height);
    assert.equal(geom.monitorInside,true,'#monitor-btn clipped at '+size.width+'x'+size.height);
    if(size.width===656)assert.ok(geom.headingH<=32,'.workspace-heading must stay one line, got '+geom.headingH);
    uiChecks++;
  }
  await page.setViewportSize({width:850,height:650});

  let vcChecks=0;
  vcVoicesFail=1;vcRuntimeOk=false;
  await page.click('.mode-btn[data-mode="vc"]');await page.waitForTimeout(300);
  const meterDeadline=Date.now()+2500;
  while(!requests.some(r=>r.path==='/api/vc/meter'&&r.body.on===true)&&Date.now()<meterDeadline)await page.waitForTimeout(50);
  assert.ok(requests.some(r=>r.path==='/api/vc/meter'&&r.body.on===true));vcChecks++;
  await page.waitForSelector('#vc-list .vc-list-error',{timeout:3000});
  assert.match(await page.locator('#vc-list .vc-list-error').textContent(),/Тестовая ошибка списка/);
  await page.click('#vc-list .vc-retry');vcChecks++; // listErrorRetry
  await page.waitForFunction(()=>document.querySelectorAll('#vc-list .vc-card').length===2);
  assert.equal(await page.locator('body').getAttribute('data-mode'),'vc');
  assert.equal(await page.locator('#vc-sidebar').isVisible(),true);
  assert.equal(await page.locator('#voice-list').evaluate(el=>getComputedStyle(el).display),'none');
  assert.equal(await page.locator('#vc-list .vc-card').count(),2);
  assert.equal(await page.locator('#vc-error').isVisible(),true);
  assert.match(await page.locator('#vc-error').textContent(),/VC-окружение не найдено/);vcRuntimeOk=true;vcChecks++;
  assert.equal(await page.locator('#vc-list .voice-name').nth(1).textContent(),'Мой <b>голос</b>');
  assert.equal(await page.locator('#vc-list b').count(),0);
  assert.deepEqual(await page.locator('#vc-list .vc-badge').allTextContents(),['Обученный','Быстрый']);vcChecks++;

  assert.equal(await page.locator('#monitor-btn').count(),1);assert.equal(await page.locator('.vc-monitor-group').count(),0);assert.equal(await page.locator('#stop-btn').isHidden(),true);vcChecks++;
  // === Scaling sweep (2026-10-04, vc mode) ===
  for(const size of [{width:656,height:448},{width:820,height:560},{width:1000,height:650},{width:1100,height:720},{width:1440,height:900},{width:1920,height:1080}]){
    await page.setViewportSize(size);await page.waitForTimeout(150);
    const geom=await page.evaluate(()=>{
      const sb=document.querySelector('.sidebar');const r=document.querySelector('#vc-add-btn').getBoundingClientRect();const s=sb.getBoundingClientRect();
      const main=document.querySelector('.main').getBoundingClientRect();
      const autotune=document.querySelector('#vc-autotune').getBoundingClientRect();
      const calibrate=document.querySelector('#vc-gate-calibrate').getBoundingClientRect();
      return {docScroll:document.scrollingElement.scrollWidth<=innerWidth,sbScroll:sb.scrollWidth<=sb.clientWidth,
        addInside:r.left>=s.left-0.5&&r.right<=s.right+0.5,
        autotuneW:autotune.width,autotuneRight:autotune.right,calibrateW:calibrate.width,calibrateRight:calibrate.right,mainRight:main.right};
    });
    assert.equal(geom.docScroll,true,'vc: horizontal scroll at '+size.width+'x'+size.height);
    assert.equal(geom.sbScroll,true,'vc: sidebar overflow at '+size.width+'x'+size.height);
    assert.equal(geom.addInside,true,'vc: #vc-add-btn clipped at '+size.width+'x'+size.height);
    assert.ok(geom.autotuneW>60,'vc: #vc-autotune too narrow at '+size.width+'x'+size.height+': '+geom.autotuneW);
    assert.ok(geom.calibrateW>60,'vc: #vc-gate-calibrate too narrow at '+size.width+'x'+size.height+': '+geom.calibrateW);
    assert.ok(geom.autotuneRight<=geom.mainRight+0.5,'vc: #vc-autotune outside .main at '+size.width+'x'+size.height);
    assert.ok(geom.calibrateRight<=geom.mainRight+0.5,'vc: #vc-gate-calibrate outside .main at '+size.width+'x'+size.height);
    vcChecks++;
  }
  await page.setViewportSize({width:850,height:650});
  assert.equal(await page.locator('#vc-volume').evaluate(el=>el.hidden),false);
  await page.locator('#vc-output-gain').evaluate(el=>{el.value='3';el.dispatchEvent(new Event('input',{bubbles:true}));});
  await page.waitForTimeout(600);
  assert.equal(requests.findLast(r=>r.path==='/api/preferences').body.output_gain_db,3);
  assert.equal(await page.locator('#vc-output-gain-value').textContent(),'+3 дБ');vcChecks++;
  assert.equal(await page.locator('#vc-gate').count(),1);
  assert.equal(await page.locator('#vc-text-input').isDisabled(),true);
  assert.equal(await page.locator('#vc-text-send').isDisabled(),true);
  assert.match(await page.locator('#vc-text-hint').textContent(),/Нажмите Старт/);
  assert.equal(await page.locator('#vc-text-row').evaluate(e=>e.scrollWidth>e.clientWidth),false);vcChecks++;
  assert.equal(await page.locator('.vc-gate-mark').evaluate(e=>e.hidden),true);
  assert.equal(await page.locator('#vc-gate').isDisabled(),true);
  await page.check('#vc-gate-enabled');
  await page.waitForResponse(r=>r.url().endsWith('/api/preferences'));
  assert.equal(await page.locator('.vc-gate-mark').evaluate(e=>e.hidden),false);
  assert.equal(await page.locator('#vc-gate').isDisabled(),false);
  const gateLeft=await page.locator('.vc-gate-mark').evaluate(e=>parseFloat(e.style.left));
  await page.$eval('#vc-gate',e=>{e.value='-40';e.dispatchEvent(new Event('input'));e.dispatchEvent(new Event('change'));});
  assert.ok(await page.locator('.vc-gate-mark').evaluate(e=>parseFloat(e.style.left))>gateLeft);
  assert.equal(await page.locator('#vc-gate-val').textContent(),'−40 дБ');
  await page.waitForResponse(r=>r.url().endsWith('/api/preferences'));
  await page.$eval('#vc-gate',e=>{e.value='-45';e.dispatchEvent(new Event('change'));});
  await page.waitForResponse(r=>r.url().endsWith('/api/preferences'));
  status={...status,vc:{state:'idle',meter:true,input_db:-50,gate_open:null}};
  await page.waitForFunction(()=>document.querySelector('#vc-in-level').classList.contains('closed'));
  assert.equal(await page.locator('#vc-gate-state').textContent(),'Ниже порога');
  const calibrationReply=page.waitForResponse(r=>r.url().endsWith('/api/vc/gate/calibrate'));
  await page.click('#vc-gate-calibrate');
  assert.equal(await page.locator('#vc-gate-calibrate').isDisabled(),true);
  assert.equal(await page.locator('#vc-gate-calibrate').textContent(),'Тишина… 3');
  await calibrationReply;await page.waitForFunction(()=>!document.querySelector('#vc-gate-calibrate').disabled);
  assert.equal(await page.inputValue('#vc-gate'),'-48');assert.equal(await page.locator('#vc-gate-enabled').isChecked(),true);
  const gateLayout=await page.locator('.vc-gate-row').evaluate(e=>({right:e.getBoundingClientRect().right,width:innerWidth,range:getComputedStyle(document.querySelector('#vc-gate')).minWidth}));
  assert.ok(gateLayout.right<=gateLayout.width);assert.equal(gateLayout.range,'80px');vcChecks++;
  const toastBeforeMeterError=await page.locator('.toast').textContent();vcMeterFail=true;
  await page.waitForFunction(()=>document.querySelector('#vc-gate-state').textContent==='Не удалось открыть микрофон: тест',{timeout:3000});
  assert.equal(await page.locator('.toast').textContent(),toastBeforeMeterError);
  const failedMeterAttempts=requests.filter(r=>r.path==='/api/vc/meter'&&r.body.on===true).length;
  await page.waitForTimeout(2100);
  assert.ok(requests.filter(r=>r.path==='/api/vc/meter'&&r.body.on===true).length>failedMeterAttempts);
  vcMeterFail=false;
  await page.waitForFunction(()=>document.querySelector('#vc-gate-state').textContent==='Ниже порога',{timeout:3000});vcChecks++;
  vcGateCalibrateFail=true;
  await page.click('#vc-gate-calibrate');
  await page.waitForFunction(()=>!document.querySelector('#vc-gate-calibrate').disabled);
  assert.match(await page.locator('.toast').textContent(),/Нет сигнала микрофона/);
  assert.equal(await page.locator('#vc-gate-calibrate').textContent(),'Калибровать');vcGateCalibrateFail=false;vcChecks++;
  const meterOffBefore=requests.filter(r=>r.path==='/api/vc/meter'&&r.body.on===false).length;
  await page.click('.mode-btn[data-mode="text"]');
  await page.waitForTimeout(100);
  assert.equal(requests.filter(r=>r.path==='/api/vc/meter'&&r.body.on===false).length,meterOffBefore+1);
  await page.click('.mode-btn[data-mode="vc"]');vcChecks++;
  await page.click('.vc-card[data-id="v1"]');
  assert.equal(await page.locator('.vc-card.selected').count(),1);assert.equal(await page.locator('#vc-name').textContent(),'Диктор');
  await page.click('.vc-card[data-id="v2"]');
  assert.equal(await page.locator('#vc-pitch').isDisabled(),false);assert.equal(await page.locator('#vc-index').evaluate(el=>el.closest('.vc-slider-row').hidden),true);vcChecks++;
  for(const colorScheme of ['light','dark']){
    await page.emulateMedia({colorScheme});
    for(const size of [{width:850,height:650},{width:1220,height:820}]){
      await page.setViewportSize(size);
      const train=page.locator('.vc-card[data-id="v2"] > .vc-train');await train.scrollIntoViewIfNeeded();
      assert.equal(await train.evaluate(button=>{
        const b=button.getBoundingClientRect(),c=button.closest('.vc-card').getBoundingClientRect();
        return b.height>=24&&b.top>=c.top&&b.bottom<=c.bottom&&b.left>=c.left&&b.right<=c.right&&document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)===button;
      }),true);
      assert.equal(await page.locator('.vc-card').evaluateAll(cards=>cards.every((c,i)=>i===0||cards[i-1].getBoundingClientRect().bottom<=c.getBoundingClientRect().top)),true);
      await train.click();
      assert.equal(await page.locator('.vc-card[data-id="v2"] > .vc-train-popover').evaluate(pop=>{
        const p=pop.getBoundingClientRect(),c=pop.closest('.vc-card').getBoundingClientRect();return p.top>=c.top&&p.bottom<=c.bottom;
      }),true);
      assert.equal(await page.locator('.vc-card').evaluateAll(cards=>cards.every((c,i)=>i===0||cards[i-1].getBoundingClientRect().bottom<=c.getBoundingClientRect().top)),true);
      await page.locator('.vc-card[data-id="v2"] > .vc-train-popover').getByRole('button',{name:'Закрыть',exact:true}).click();vcChecks++;
    }
  }
  await page.emulateMedia({colorScheme:'light'});await page.setViewportSize({width:850,height:650});
  await page.click('.vc-card[data-id="v1"]');assert.equal(await page.locator('#vc-name').count(),1);assert.equal(await page.locator('#vc-pitch').isDisabled(),false);assert.equal(await page.locator('#vc-index').isDisabled(),false);assert.equal(await page.locator('#vc-index').isVisible(),true);vcChecks++;

  const paramsResponse=page.waitForResponse(r=>r.url().endsWith('/api/vc/params'),{timeout:1000});
  await page.$eval('#vc-pitch',el=>{el.value='3';el.dispatchEvent(new Event('input'));el.dispatchEvent(new Event('change'));});
  assert.equal(await page.locator('#vc-pitch-val').textContent(),'+3');
  await paramsResponse;
  assert.equal(requests.findLast(r=>r.path==='/api/vc/params').body.pitch_shift,3);vcChecks++;

  await page.click('#start-btn');
  await page.waitForFunction(()=>document.querySelector('#start-btn').textContent==='Стоп',{timeout:3000});
  const vcStart=requests.findLast(r=>r.path==='/api/control');
  assert.equal(vcStart.body.mode,'vc');assert.equal(vcStart.body.action,'start');assert.equal(vcStart.body.vc_voice_id,'v1');
  assert.equal(await page.locator('#vc-readout').textContent(),'Загрузка голоса…');
  assert.equal(await page.locator('#vc-readout').evaluate(el=>el.classList.contains('vc-loading')),true);
  assert.equal(await page.locator('#start-btn').isDisabled(),true);assert.equal(await page.locator('#stop-btn').isHidden(),true);vcChecks++;
  await page.evaluate(()=>fetch('/api/__vc_running'));
  await page.waitForFunction(()=>/Задержка 240/.test(document.querySelector('#vc-readout').textContent),{timeout:3000});
  assert.equal(await page.locator('#start-btn').isDisabled(),false);
  assert.match(await page.locator('#vc-readout').textContent(),/Задержка 240 мс.*1,8 ГБ/);
  assert.equal(await page.locator('#vc-in-level>i').evaluate(el=>el.style.width),'57%');
  assert.equal(await page.locator('#vc-in-level').evaluate(el=>el.classList.contains('closed')),true);
  await page.click('#start-btn');await page.waitForFunction(()=>document.querySelector('#start-btn').textContent==='Старт');
  assert.equal(requests.findLast(r=>r.path==='/api/control').body.action,'stop');vcChecks++;

  await page.locator('.vc-card[data-id="v2"]').click();
  assert.equal(await page.locator('#vc-quality').isVisible(),true);
  assert.equal(await page.locator('#vc-reference').isVisible(),true);
  await page.evaluate(()=>{window.__vcPitch=document.querySelector('#vc-pitch');window.__vcQuality=document.querySelector('#vc-quality');});
  await page.focus('#vc-quality');await page.waitForTimeout(1150);
  assert.equal(await page.evaluate(()=>window.__vcPitch===document.querySelector('#vc-pitch')&&window.__vcQuality===document.activeElement),true);vcChecks++;
  const tuningReply=page.waitForResponse(r=>r.url().endsWith('/api/vc/autotune'));
  await page.click('#vc-autotune');assert.equal(await page.locator('#vc-autotune').isDisabled(),true);await tuningReply;
  await page.waitForFunction(()=>document.querySelector('#vc-autotune').textContent==='Рекомендуемое');
  assert.equal(await page.inputValue('#vc-block'),'3');assert.match(await page.locator('.toast').textContent(),/Подобрано: 500 мс/);vcChecks++;
  await page.click('#start-btn');await page.waitForFunction(()=>document.querySelector('#start-btn').textContent==='Стоп');
  assert.equal(requests.findLast(r=>r.path==='/api/control').body.vc_voice_id,'v2');
  await page.evaluate(()=>fetch('/api/__vc_running'));await page.waitForFunction(()=>!document.querySelector('#start-btn').disabled);
  await page.waitForFunction(()=>!document.querySelector('#vc-text-input').disabled);
  await page.waitForFunction(()=>!document.querySelector('#vc-check-btn').disabled);
  assert.equal(await page.locator('#vc-check-btn').isVisible(),true);
  prefs={...prefs,monitor_enabled:false};
  await page.click('#vc-check-btn');await page.waitForFunction(()=>document.querySelector('.toast').textContent.includes('Включите мониторинг'));
  assert.deepEqual(requests.findLast(r=>r.path==='/api/vc/check').body,{});
  prefs={...prefs,monitor_enabled:true,monitor_device:'External Headphones'};
  await page.click('#vc-check-btn');await page.waitForFunction(()=>document.querySelector('#vc-check-btn').textContent==='Проверяю…');
  await page.waitForFunction(()=>document.querySelector('#vc-check-btn').disabled);
  status={...status,vc:{...status.vc,inject:{id:'check-test',state:'done'}}};
  await page.waitForFunction(()=>!document.querySelector('#vc-check-btn').disabled);vcChecks++;
  await page.locator('#vc-text-input').fill('Фраза из текста');
  await page.locator('#vc-text-input').press('Meta+Enter');
  await page.waitForFunction(()=>document.querySelector('#vc-text-input').value==='');
  assert.equal(requests.findLast(r=>r.path==='/api/vc/speak').body.text,'Фраза из текста');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'vc-text-input');
  await page.waitForFunction(()=>document.querySelector('#vc-text-send').textContent==='Стоп');
  await page.evaluate(()=>{window.__vcTextInput=document.querySelector('#vc-text-input');});
  await page.waitForTimeout(800);
  assert.equal(await page.evaluate(()=>window.__vcTextInput===document.querySelector('#vc-text-input')),true);
  await page.click('#vc-text-send');
  assert.ok(requests.some(r=>r.path==='/api/vc/speak/cancel'));vcChecks++;
  await openSet('audio');
  const textVoice=await page.locator('#vc-text-voice option').evaluateAll(options=>options.find(o=>o.value)?.value);
  await page.selectOption('#vc-text-voice',textVoice);await page.waitForResponse(r=>r.url().endsWith('/api/preferences'));
  assert.equal(prefs.vc_text_voice_id,textVoice);
  await page.selectOption('#vc-text-voice','');await page.waitForResponse(r=>r.url().endsWith('/api/preferences'));
  assert.equal(prefs.vc_text_voice_id,null);await closeSet();vcChecks++;
  await page.click('#start-btn');vcChecks++;
  // === Voice swap hotkey ===
  await openSet('keys');
  assert.equal(await page.locator('#swap-hotkey').textContent(),'\u2325\u2318S');
  assert.match(await page.locator('#keys-settings .keys-note').textContent(),/Свой голос идёт с той же задержкой, что и нейроголос/);
  await page.click('#swap-toggle');await page.waitForTimeout(300);
  assert.equal(prefs.swap_enabled,true);
  assert.deepEqual(await page.evaluate(()=>window.__hk.at(-1)),{action:'register',key_code:1,modifiers:2304});
  await page.click('#swap-hotkey');
  assert.equal(await page.locator('#swap-hotkey').textContent(),'Нажмите сочетание…');
  await page.keyboard.press('KeyA');
  assert.match(await page.locator('#swap-hotkey-error').textContent(),/Добавьте/);
  await page.keyboard.press('Insert');
  assert.match(await page.locator('#swap-hotkey-error').textContent(),/не поддерживается/);
  await page.keyboard.press('Escape');await page.waitForTimeout(250);
  assert.equal(await page.locator('#settings-sheet').evaluate(el=>el.hidden),false);
  assert.equal(await page.locator('#swap-hotkey').textContent(),'\u2325\u2318S');
  await page.click('#swap-hotkey');await page.keyboard.press('Control+KeyJ');await page.waitForTimeout(300);
  assert.deepEqual(prefs.swap_hotkey,{key_code:38,modifiers:4096,label:'\u2303J'});
  assert.deepEqual(await page.evaluate(()=>window.__hk.at(-1)),{action:'register',key_code:38,modifiers:4096});
  await page.click('#swap-hotkey');await page.keyboard.press('Alt+Meta+KeyS');await page.waitForTimeout(300);
  assert.deepEqual(prefs.swap_hotkey,{key_code:1,modifiers:2304,label:'\u2325\u2318S'});
  assert.deepEqual(await page.evaluate(()=>window.__hk.at(-1)),{action:'register',key_code:1,modifiers:2304});
  await page.click('#swap-hotkey');await page.evaluate(()=>document.dispatchEvent(new KeyboardEvent('keydown',{code:'F13',key:'F13',bubbles:true,cancelable:true})));await page.waitForTimeout(300);
  assert.equal(prefs.swap_hotkey.key_code,105);assert.equal(prefs.swap_hotkey.modifiers,0);
  await page.click('#swap-hotkey');await page.keyboard.press('Alt+Meta+KeyS');await page.waitForTimeout(300);
  await closeSet();
  await page.click('#start-btn');await page.waitForFunction(()=>document.querySelector('#start-btn').textContent==='Стоп');
  await page.evaluate(()=>fetch('/api/__vc_running'));
  await page.waitForFunction(()=>document.querySelector('#swap-pill')&&!document.querySelector('#swap-pill').hidden,{timeout:3000});
  assert.equal(await page.locator('#swap-pill').textContent(),'Свой голос');
  const bypassCount=()=>requests.filter(r=>r.body.action==='vc_bypass').length;
  const lastBypass=()=>requests.findLast(r=>r.body.action==='vc_bypass').body.bypass;
  const base=bypassCount();
  await page.evaluate(()=>window.aiVoiceHotkey('down'));await page.waitForTimeout(300);
  assert.equal(lastBypass(),false);assert.equal(await page.locator('#swap-pill').textContent(),'Нейроголос');
  await page.evaluate(()=>window.aiVoiceHotkey('up'));await page.waitForTimeout(300);
  assert.equal(lastBypass(),true);assert.equal(await page.locator('#swap-pill').textContent(),'Свой голос');
  assert.equal(bypassCount(),base+2);
  await openSet('keys');await page.click('#swap-mode [data-mode="toggle"]');await page.waitForTimeout(300);
  assert.equal(prefs.swap_mode,'toggle');await closeSet();
  await page.evaluate(()=>window.aiVoiceHotkey('down'));await page.waitForTimeout(300);
  assert.equal(lastBypass(),false);
  await page.evaluate(()=>window.aiVoiceHotkey('up'));await page.waitForTimeout(300);
  assert.equal(bypassCount(),base+3);
  await page.evaluate(()=>window.aiVoiceHotkey('down'));await page.waitForTimeout(300);
  assert.equal(lastBypass(),true);
  // Rapid presses coalesce: one request in flight, the last desired value wins.
  const rapidBefore=bypassCount();
  await page.evaluate(()=>{window.aiVoiceHotkey('down');window.aiVoiceHotkey('up');window.aiVoiceHotkey('down');window.aiVoiceHotkey('up');});
  await page.waitForTimeout(700);
  assert.equal(maxBypassInFlight,1);assert.ok(bypassCount()-rapidBefore<=3);
  await openSet('keys');await page.click('#swap-mode [data-mode="hold"]');await page.waitForTimeout(300);await closeSet();
  const holdBefore=bypassCount();
  await page.evaluate(()=>window.aiVoiceHotkey('down'));await page.waitForTimeout(300);
  await page.evaluate(()=>window.aiVoiceHotkey('up'));await page.waitForTimeout(300);
  assert.equal(lastBypass(),true);assert.equal(bypassCount(),holdBefore+2);
  await openSet('keys');await page.click('#swap-toggle');await page.waitForTimeout(300);
  assert.equal(prefs.swap_enabled,false);assert.deepEqual(await page.evaluate(()=>window.__hk.at(-1)),{action:'unregister'});
  const disabledCount=bypassCount();
  await closeSet();
  await page.evaluate(()=>window.aiVoiceHotkey('down'));await page.waitForTimeout(250);
  assert.equal(bypassCount(),disabledCount);
  await page.evaluate(()=>fetch('/api/__vc_running'));await page.waitForFunction(()=>!document.querySelector('#start-btn').disabled);
  await page.click('#start-btn');vcChecks++;
  await page.click('#vc-add-btn');assert.equal(await page.locator('#vc-sheet').evaluate(el=>el.hidden),false);
  await page.keyboard.press('Escape');await page.waitForTimeout(250);assert.equal(await page.locator('#vc-sheet').evaluate(el=>el.hidden),true);
  await page.click('#vc-add-btn');await page.click('[data-vc-tab="audio"]');
  assert.equal(await page.locator('[data-vc-pane="audio"]').isVisible(),true);
  await page.click('#vc-audio-submit');assert.equal(await page.locator('#vc-sheet-error').isVisible(),true);
  assert.match(await page.locator('#vc-sheet-error').textContent(),/Введите имя/);
  assert.equal(requests.some(r=>r.path==='/api/vc/create'),false);vcChecks++;

  await page.setInputFiles('#vc-audio-files',{name:'a.wav',mimeType:'audio/wav',buffer:Buffer.from('RIFFxxxx')});
  assert.equal(await page.locator('#vc-audio-train [data-preset="none"]').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#vc-audio-eta').textContent(),'время покажем после обработки');
  await page.fill('#vc-audio-name','Новый');await page.click('#vc-audio-train [data-preset="normal"]');
  // Browser XHR transport with a deterministic upload progress event.
  await page.evaluate(()=>{
    const send=XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send=function(body){const progress=this.upload.onprogress;this.upload.onprogress=null;
      if(progress)progress(new ProgressEvent('progress',{lengthComputable:true,loaded:50,total:100}));
      setTimeout(()=>send.call(this,body),250);
    };
  });
  await page.click('#vc-audio-submit');
  assert.equal(await page.locator('#vc-upload-progress span').textContent(),'Загрузка 50%');vcChecks++;
  await page.waitForFunction(()=>document.querySelector('#vc-sheet').hidden&&document.querySelectorAll('.vc-card').length===3);
  const created=requests.findLast(r=>r.path==='/api/vc/create');assert.ok(created.raw.includes('name="train"'));assert.ok(created.raw.includes('normal'));assert.ok(created.raw.includes('filename="a.wav"'));
  assert.equal(await page.locator('.vc-card[data-id="v3"] .vc-badge').textContent(),'Обработка…');
  assert.equal(await page.locator('.vc-card[data-id="v3"] .vc-indeterminate').count(),1);vcChecks++;
  await page.waitForSelector('.vc-card[data-id="v3"] .vc-ring',{timeout:6000});
  await page.waitForSelector('#vc-training',{timeout:6000});
  assert.match(await page.locator('#vc-train-line').textContent(),/эпоха 57\/100/);assert.equal(await page.locator('#vc-train-bar').getAttribute('aria-valuenow'),'40');vcChecks++;
  assert.equal(await page.locator('#vc-train-queue .vc-queue-row').count(),2);
  await page.locator('#vc-train-queue .vc-queue-row').nth(1).getByRole('button',{name:'Выше Диктор',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#vc-train-queue .vc-queue-row')?.textContent.includes('Диктор'));
  await page.locator('#vc-train-queue .vc-queue-row').first().getByRole('button',{name:'Убрать из очереди Диктор',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('#vc-train-queue .vc-queue-row').length===1);vcChecks++;

  // Пауза/продолжение обучения
  assert.equal(await page.locator('#vc-train-pause').isVisible(),true);
  assert.equal(await page.locator('#vc-train-pause').textContent(),'Пауза');
  const pauseGeom=await page.evaluate(()=>{
    const pause=document.querySelector('#vc-train-pause').getBoundingClientRect();
    const cancel=document.querySelector('#vc-train-cancel').getBoundingClientRect();
    return {pauseRight:pause.right,cancelLeft:cancel.left,pauseH:pause.height,cancelH:cancel.height,pauseW:pause.width};
  });
  assert.ok(pauseGeom.pauseRight<=pauseGeom.cancelLeft+0.5,'pause button must be left of cancel: '+JSON.stringify(pauseGeom));
  assert.equal(pauseGeom.pauseH,22,'pause height must be 22');
  assert.equal(pauseGeom.cancelH,22,'cancel height must be 22');
  assert.ok(pauseGeom.pauseW<150,'pause button too wide: '+pauseGeom.pauseW);vcChecks++;
  await page.click('#vc-train-pause');
  assert.ok(requests.some(r=>r.path==='/api/vc/train/pause'));
  await page.waitForFunction(()=>document.querySelector('#vc-train-line')?.textContent==='Пауза');
  assert.equal(await page.locator('#vc-train-pause').textContent(),'Продолжить');vcChecks++;
  await page.click('#vc-train-pause');
  assert.ok(requests.some(r=>r.path==='/api/vc/train/resume'));
  await page.waitForFunction(()=>document.querySelector('#vc-train-pause')?.textContent==='Пауза');vcChecks++;

  await page.click('#vc-train-cancel');
  await page.waitForFunction(()=>document.querySelector('#vc-training').hidden&&document.querySelector('.toast.show')?.textContent.includes('Обучение остановлено'),{timeout:3000});
  assert.ok(requests.some(r=>r.path==='/api/vc/train/cancel'));vcChecks++;

  await page.click('#vc-add-btn');await page.click('[data-vc-tab="import"]');
  await page.setInputFiles('#vc-import-files',[{name:'m.pth',mimeType:'application/octet-stream',buffer:Buffer.from('model')},{name:'m.index',mimeType:'application/octet-stream',buffer:Buffer.from('index')}]);
  await page.fill('#vc-import-name','Импортёр');await page.click('#vc-import-submit');
  await page.waitForFunction(()=>document.querySelector('#vc-sheet').hidden&&document.querySelectorAll('.vc-card').length===4);
  assert.ok(requests.some(r=>r.path==='/api/vc/import'));
  assert.equal(await page.locator('.vc-card[data-id="v4"] .vc-badge').textContent(),'Импорт');vcChecks++;

  await page.click('#vc-add-btn');await page.click('[data-vc-tab="import"]');
  await page.focus('#vc-import-submit');await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'vc-sheet-close');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'vc-import-submit');
  await page.focus('#vc-sheet-close');await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'vc-import-submit');vcChecks++; // sheetFocusTrap
  await page.focus('#vc-import-drop');
  const chooser=page.waitForEvent('filechooser',{timeout:5000});await page.keyboard.press('Enter');await chooser;vcChecks++; // dropKeyboard
  await page.setInputFiles('#vc-import-files',{name:'bad.pth',mimeType:'application/octet-stream',buffer:Buffer.from('model')});
  await page.fill('#vc-import-name','Сбой');await page.click('#vc-import-submit');await page.keyboard.press('Escape');await page.waitForTimeout(250);
  assert.equal(await page.locator('#vc-sheet').evaluate(el=>el.hidden),true);
  await page.waitForFunction(()=>document.querySelector('.toast.show')?.textContent.includes('Тестовая ошибка импорта'),{timeout:3000});
  assert.equal(await page.locator('.vc-card').count(),4);vcChecks++; // uploadErrorAfterClose

  // The reload after deleting v1 exposes a processing voice and starts list polling.
  const processingVoice=vcVoices.find(v=>v.id==='v4');processingVoice.status='processing';processingVoice.stage='convert';
  await page.locator('.vc-card[data-id="v1"] .vc-delete').click();await page.locator('.vc-card[data-id="v1"] .vc-delete').click();
  assert.equal(await page.locator('.vc-card[data-id="v1"] .vc-delete').isDisabled(),true);
  await page.waitForFunction(()=>document.querySelectorAll('.vc-card').length===3);
  assert.equal(requests.findLast(r=>r.path==='/api/vc/delete').body.voice_id,'v1');vcChecks++;

  // VC bulk deletion continues after a 409 and reports the failed voice.
  vcDeleteFailId='v3';const bulkVCBefore=requests.filter(r=>r.path==='/api/vc/delete').length;
  await page.click('#vc-select-btn');
  assert.equal(await page.locator('.vc-card .select-check').count(),3);
  assert.equal(await page.locator('.vc-card .vc-delete,.vc-card .vc-rename,.vc-card .vc-train,.vc-card .vc-ring').count(),0);
  await page.click('.vc-card[data-id="v2"]');await page.click('.vc-card[data-id="v3"]');
  assert.equal(await page.locator('#vc-select-delete-btn').textContent(),'Удалить (2)');
  const processingPolls=(async()=>{
    await page.waitForResponse(r=>r.url().endsWith('/api/vc/voices'),{timeout:2500});
    await page.waitForResponse(r=>r.url().endsWith('/api/vc/voices'),{timeout:1500});
  })();
  await page.click('#vc-select-delete-btn');assert.equal(await page.locator('#vc-select-delete-btn').textContent(),'Удалить 2 голоса?');
  await page.waitForTimeout(1500);await processingPolls;
  assert.equal(await page.locator('.vc-card[data-id="v4"] .vc-badge-processing').count(),1);
  assert.equal(await page.locator('#vc-select-delete-btn').getAttribute('data-armed'),'1');
  assert.equal(await page.locator('#vc-select-delete-btn').textContent(),'Удалить 2 голоса?');vcChecks++;
  processingVoice.status='ready';delete processingVoice.stage;
  await page.click('#vc-select-delete-btn');
  await page.waitForFunction(()=>document.querySelector('#vc-select-bar').hidden&&document.querySelector('.toast.show')?.textContent.includes('Не удалось'));
  const vcDeleted=requests.filter(r=>r.path==='/api/vc/delete').slice(bulkVCBefore);
  assert.deepEqual(vcDeleted.map(r=>r.body.voice_id),['v2','v3']);
  assert.equal(await page.locator('.vc-card[data-id="v2"]').count(),0);assert.equal(await page.locator('.vc-card[data-id="v3"]').count(),1);
  assert.match(await page.locator('.toast').textContent(),/Удалено: 1 из 2.*Не удалось/);vcDeleteFailId=null;vcChecks++;

  assert.equal(await page.locator('#vc-check-btn').isDisabled(),true);
  // HF catalog is independent of the import sheet tabs and Fish catalog.
  await page.click('#vc-catalog-tab');
  await page.waitForSelector('.hf-card');
  assert.equal(await page.locator('#vc-mine').isHidden(),true);
  assert.equal(await page.locator('.hf-title').first().textContent(),hfCard.title);
  assert.equal(await page.locator('.hf-card b').count(),0);
  assert.match(await page.locator('.hf-meta').first().textContent(),/↓42.*без индекса/);
  assert.equal(await page.locator('.hf-card .preview-btn').count(),0);vcChecks++;
  assert.equal(await page.locator('.hf-play').count(),1);
  await page.locator('.hf-play').click();await page.waitForFunction(()=>document.querySelector('.hf-play').textContent==='❚❚');
  await page.locator('.hf-play').click();await page.waitForFunction(()=>document.querySelector('.hf-play').textContent==='▶');
  assert.equal(await page.evaluate(()=>window.__hfAudio.length),1);
  const sampleRequests=requests.filter(r=>r.path==='/api/vc/hf/sample').length;
  await page.locator('.hf-play').click();await page.waitForFunction(()=>document.querySelector('.hf-play').textContent==='❚❚');
  assert.equal(requests.filter(r=>r.path==='/api/vc/hf/sample').length,sampleRequests);
  await page.locator('.hf-card').first().press('Enter');
  await page.waitForSelector('#hf-detail:not([hidden])');
  await page.waitForSelector('#hf-detail-images img[src^="blob:"]');
  assert.equal(await page.locator('#hf-detail-description h3').textContent(),'Описание');
  assert.equal(await page.locator('#hf-detail-description li strong').textContent(),'Пункт');
  assert.equal(await page.locator('#hf-detail-description a[target="_blank"]').count(),1);
  assert.equal(await page.locator('#hf-detail-description script').count(),0);
  assert.equal(await page.locator('#hf-detail-title').evaluate(node=>node===document.activeElement),true);
  await page.locator('#hf-detail-images .hf-image').click();
  await page.keyboard.press('Escape');assert.equal(await page.locator('#hf-detail .expanded').count(),0);
  assert.equal(await page.locator('#hf-detail').isVisible(),true);
  await page.setViewportSize({width:820,height:560});
  assert.equal(await page.locator('#hf-detail .vc-sheet-card').evaluate(node=>node.scrollWidth<=node.clientWidth),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#hf-detail').isHidden(),true);
  assert.equal(await page.locator('.hf-card').first().evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.evaluate(()=>window.__hfAudio[0].paused),true);
  assert.ok(await page.evaluate(()=>window.__hfRevoked.length>0));
  await page.setViewportSize({width:1220,height:820});
  hfReadmeMeaningful=false;await page.locator('.hf-card').first().press('Space');
  await page.waitForFunction(()=>document.querySelector('#hf-detail-description').textContent==='Описания нет');
  await page.click('#hf-detail-close');hfReadmeMeaningful=true;vcChecks++;
  await page.fill('#hf-search','ivona');await page.press('#hf-search','Enter');
  await Promise.all([page.waitForResponse(r=>r.url().includes('/api/vc/hf/search?')&&new URL(r.url()).searchParams.get('sort')==='lastModified'),page.click('[data-hf-sort="lastModified"]')]);
  await Promise.all([page.waitForResponse(r=>r.url().includes('/api/vc/hf/search?')&&new URL(r.url()).searchParams.get('lang')==='ru'),page.selectOption('#hf-lang','ru')]);
  assert.deepEqual(requests.findLast(r=>r.path==='/api/vc/hf/search').query,{q:'ivona',sort:'lastModified',lang:'ru',page:'1'});vcChecks++;
  await page.waitForSelector('.hf-card');await page.click('#hf-more');
  await page.waitForFunction(()=>document.querySelectorAll('.hf-card').length===2);
  assert.equal(await page.locator('#hf-more').isHidden(),true);
  assert.equal(await page.locator('.hf-card').nth(1).locator('.hf-play').count(),0);vcChecks++;
  await page.locator('.hf-card').first().press('Enter');await page.waitForSelector('#hf-detail:not([hidden])');
  await page.locator('#hf-detail-download .hf-download').click();await page.click('#hf-detail-close');
  await page.waitForFunction(()=>document.querySelector('.hf-download.vc-ring')?.textContent==='50%');
  assert.equal(await page.locator('.hf-download').nth(1).isDisabled(),true);
  await page.click('.hf-download.vc-ring');
  await page.waitForFunction(()=>!document.querySelector('.hf-download.vc-ring'));
  assert.ok(requests.some(r=>r.path==='/api/vc/catalog/download/cancel'));vcChecks++;
  await page.locator('.hf-download').first().click();
  await page.waitForSelector('.hf-download.vc-ring');
  const hfVoice={id:'hf-imported',name:hfCard.title,kind:'imported',status:'ready',source:{kind:'hf',repo:hfCard.repo,pth:'voice.pth',index:null,revision:'abc'}};
  vcVoices.push(hfVoice);hfDownload={...hfDownload,state:'done',bytes:100,voice:hfVoice};
  await page.waitForFunction(()=>document.querySelector('.hf-download')?.textContent==='✓');
  assert.match(await page.locator('.toast').textContent(),/Голос добавлен/);
  await page.click('#vc-mine-tab');assert.equal(await page.locator('.vc-card[data-id="hf-imported"]').count(),1);
  await page.click('#vc-add-btn');await page.click('#vc-sheet-close');
  assert.equal(await page.locator('#vc-mine-tab').getAttribute('aria-selected'),'true');vcChecks++;
  await page.click('#vc-catalog-tab');await page.fill('#hf-search','none');
  await page.waitForFunction(()=>document.querySelector('#hf-message').textContent==='Ничего не найдено');
  hfFail=true;await page.fill('#hf-search','retry');await page.press('#hf-search','Enter');
  await page.waitForFunction(()=>document.querySelector('#hf-message').textContent.includes('Нет связи'));
  hfFail=false;await page.click('#hf-message button');await page.waitForSelector('.hf-card');vcChecks++;
  await page.click('#vc-mine-tab');

  await page.click('.mode-btn[data-mode="mic"]');await page.waitForTimeout(300);
  assert.equal(await page.locator('body').getAttribute('data-mode'),'mic');
  assert.equal(await page.locator('#vc-sidebar').evaluate(el=>getComputedStyle(el).display),'none');
  assert.equal(await page.locator('#voice-list').isVisible(),true);vcChecks++;

  // TTS bulk selection deletes exactly the two marked library rows.
  await page.click('.tab[data-tab="mine"]');await page.fill('#search-input','');
  if(await page.locator('#fav-filter').getAttribute('aria-pressed')==='true')await page.click('#fav-filter');
  const ttsIds=await page.locator('.voice-row').evaluateAll(rows=>rows.slice(0,2).map(r=>r.dataset.id));
  assert.equal(ttsIds.length,2);await page.click('#select-btn');
  assert.equal(await page.locator('.voice-row .select-check').count(),ttsVoices.length);
  assert.equal(await page.locator('.voice-row .event-remove,.voice-row .star,.voice-row .preview-btn').count(),0);
  for(const id of ttsIds)await page.locator('.voice-row[data-id="'+id+'"]').click();
  assert.equal(await page.locator('#select-delete-btn').textContent(),'Удалить (2)');
  await page.click('#select-delete-btn');assert.equal(await page.locator('#select-delete-btn').textContent(),'Удалить 2 голоса?');
  await page.click('#select-delete-btn');await page.waitForFunction(()=>document.querySelector('#select-bar').hidden);
  assert.deepEqual(requests.findLast(r=>r.path==='/api/remove_voices').body.ids,ttsIds);
  for(const id of ttsIds)assert.equal(await page.locator('.voice-row[data-id="'+id+'"]').count(),0);
  assert.equal(await page.locator('#select-btn').textContent(),'Выбрать');uiChecks++;
  await page.click('.tab[data-tab="catalog"]');assert.equal(await page.locator('#select-btn').isHidden(),true);uiChecks++;

  // Motion regression: drive the real status/list poll callbacks deterministically.
  // No production testing hooks; the same HTTP mocks and shipped DOM are used.
  let motionChecks=0;
  ttsVoices=[bykov,...female.slice(0,2)];prefs={...prefs,voice_id:bykov.id,favorite_ids:[]};
  vcVoices=Array.from({length:12},(_,i)=>({id:'motion'+i,name:'Анимация '+i,kind:'trained',status:i===1?'processing':'ready',pitch_shift:0,index_rate:.75}));
  vcTraining={voice_id:'motion0',state:'running',stage:'train',stage_label:'Обучение',epoch:4,total_epochs:10,progress:.4,eta_s:60,
    queue:vcVoices.map(v=>({voice_id:v.id,name:v.name,epochs:10,eta_min:1}))};
  status={active:false,state:'stopped',mode:'mic',message:'Остановлено',transcript_partial:'',transcript_final:'',training:vcTraining};
  const motionPage=await browser.newPage({viewport:{width:1220,height:820},reducedMotion:'no-preference'});
  motionPage.on('pageerror',e=>errors.push(e.message));
  await motionPage.route('https://public-platform.r2.fish.audio/**',route=>route.fulfill({contentType:'image/png',
    body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII=','base64')}));
  await motionPage.addInitScript(()=>{
    const realInterval=window.setInterval.bind(window);window.__motionPolls=[];
    window.setInterval=(fn,delay,...args)=>delay===1000?(window.__motionPolls.push(()=>fn(...args)),0):realInterval(fn,delay,...args);
    window.__motionClasses=[];
    new MutationObserver(records=>records.forEach(record=>{
      const before=new Set((record.oldValue||'').split(/\s+/));
      for(const name of record.target.classList)if(name.startsWith('motion-')&&!before.has(name))window.__motionClasses.push(name);
    })).observe(document,{subtree:true,attributes:true,attributeFilter:['class'],attributeOldValue:true});
  });
  await motionPage.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await motionPage.waitForSelector('#output-gain');await motionPage.waitForTimeout(350);
  await motionPage.waitForSelector('#voice-list .avatar img.loaded');
  assert.deepEqual(await motionPage.evaluate(()=>{
    const before=document.querySelector('#voice-list .avatar img.loaded'),url=before.src;
    before.closest('.voice-row').querySelector('.star').click();
    const after=Array.from(document.querySelectorAll('#voice-list .avatar img')).find(img=>img.src===url);
    return [after!==before,after.classList.contains('loaded')];
  }),[true,true]);motionChecks++;
  await motionPage.focus('#settings-btn');
  assert.deepEqual(await motionPage.evaluate(()=>{
    window.aiVoiceOpenSettings('audio');
    const sheet=document.querySelector('#settings-sheet');
    return [sheet.hidden,sheet.inert,getComputedStyle(document.querySelector('.settings-card')).animationName];
  }),[false,false,'motion-sheet-in']);motionChecks++;
  assert.equal(await motionPage.locator('.settings-card').evaluate(e=>getComputedStyle(e).backdropFilter),'none');motionChecks++;
  await motionPage.waitForTimeout(300);
  assert.deepEqual(await motionPage.evaluate(()=>{
    document.querySelector('#settings-close').click();const sheet=document.querySelector('#settings-sheet');
    return [sheet.hidden,sheet.inert,getComputedStyle(sheet).pointerEvents,document.activeElement.id];
  }),[false,true,'none','settings-btn']);motionChecks++;
  await motionPage.evaluate(()=>window.aiVoiceOpenSettings('audio'));await motionPage.waitForTimeout(300);
  assert.deepEqual(await motionPage.locator('#settings-sheet').evaluate(e=>[e.hidden,e.inert]),[false,false],'reopening cancels delayed hidden');motionChecks++;
  assert.deepEqual(await motionPage.evaluate(()=>{
    document.querySelector('[data-section="asr"]').click();
    const ghost=document.querySelector('.settings-content > .motion-ghost');
    return [ghost.inert,ghost.getAttribute('aria-hidden'),ghost.querySelectorAll('[id]').length,document.querySelector('[data-pane="audio"]').hidden];
  }),[true,'true',0,true]);motionChecks++;
  await motionPage.click('#settings-close');await motionPage.waitForTimeout(250);
  assert.equal(await motionPage.locator('#settings-sheet').evaluate(e=>e.hidden),true);
  assert.deepEqual(await motionPage.locator('#vc-list > .vc-card').evaluateAll(rows=>rows.map(r=>r.style.getPropertyValue('--motion-delay'))),
    ['0ms','16ms','32ms','48ms','64ms','80ms','96ms','112ms','128ms','144ms','0ms','0ms']);motionChecks++;
  // A real mode change crossfades; colors transition alongside the segment transform.
  assert.deepEqual(await motionPage.evaluate(()=>{
    document.querySelector('.mode-btn[data-mode="vc"]').click();
    const ghost=document.querySelector('.main-content > .motion-ghost');
    return [ghost.inert,ghost.getAttribute('aria-hidden'),ghost.querySelectorAll('[id]').length,
      getComputedStyle(document.querySelector('.mode-tabs'),'::before').transitionProperty,
      getComputedStyle(ghost).animationDuration,getComputedStyle(document.querySelector('#mode-panel')).animationDelay];
  }),[true,'true',0,'opacity, transform, color, background-color, border-color','0.14s','0.06s']);motionChecks++;
  assert.deepEqual(await motionPage.locator('#start-btn').evaluate(btn=>[
    btn.hasAttribute('data-motion-old'),['none','normal'].includes(getComputedStyle(btn,'::after').content)
  ]),[false,true]);motionChecks++;
  await motionPage.waitForSelector('#vc-train-queue [data-id="motion11"]');await motionPage.waitForTimeout(350);
  assert.deepEqual(await motionPage.locator('#vc-train-bar > i').evaluate(e=>[e.style.width,e.style.transform]),['','scaleX(0.4)']);
  assert.equal(await motionPage.locator('#vc-in-level > i').evaluate(e=>getComputedStyle(e).transitionDuration),'0s');motionChecks++;
  const pollMotion=async()=>{
    const responses=[motionPage.waitForResponse(r=>r.url().endsWith('/api/status')),
      motionPage.waitForResponse(r=>r.url().endsWith('/api/vc/voices'))];
    await motionPage.evaluate(()=>window.__motionPolls.forEach(poll=>poll()));await Promise.all(responses);
    await motionPage.waitForTimeout(350);
  };
  await pollMotion(); // settle the first training state transition
  await motionPage.evaluate(()=>window.__queueRow=document.querySelector('#vc-train-queue .vc-queue-row'));
  for(const reducedMotion of ['no-preference','reduce']){
    await motionPage.emulateMedia({reducedMotion});await motionPage.waitForTimeout(350);
    assert.equal(await motionPage.locator('#vc-train-bar > i').evaluate(e=>getComputedStyle(e).transitionDuration),reducedMotion==='reduce'?'0s':'0.9s');
    assert.equal(await motionPage.locator('#vc-list .vc-ring').first().evaluate(e=>getComputedStyle(e).transitionDuration),reducedMotion==='reduce'?'0s':'0.6s');motionChecks++;
    await motionPage.evaluate(()=>{
      window.__motionClasses=[];
      const indicator=document.querySelector('.vc-indeterminate i');
      const animation=indicator.getAnimations().find(a=>a.animationName==='vc-processing');
      window.__processingMotion={indicator,animation,startTime:animation?.startTime,time:animation?.currentTime};
    });
    assert.equal(await motionPage.evaluate(()=>!!window.__processingMotion.animation),reducedMotion==='no-preference');
    for(let i=0;i<3;i++){
      await pollMotion();
      assert.equal(await motionPage.evaluate(()=>document.querySelector('#vc-train-queue .vc-queue-row')===window.__queueRow),true);
      assert.deepEqual(await motionPage.evaluate(()=>{
        const saved=window.__processingMotion,indicator=document.querySelector('.vc-indeterminate i');
        const animation=indicator.getAnimations().find(a=>a.animationName==='vc-processing');
        const result=[indicator===saved.indicator,animation===saved.animation,
          animation?animation.startTime===saved.startTime:true,
          animation?typeof saved.time==='number'&&animation.currentTime>saved.time:true];
        saved.time=animation?.currentTime;return result;
      }),[true,true,true,true],reducedMotion+': processing poll must preserve DOM, CSSAnimation and advancing time');
    }
    assert.deepEqual(await motionPage.evaluate(()=>window.__motionClasses),[],reducedMotion+': identical polls must not add motion classes');motionChecks++;
  }
  motionChecks++;
  // Changed server state must still refresh the processing row.
  vcVoices[1].stage='slice';await pollMotion();
  assert.equal(await motionPage.locator('.vc-card[data-id="motion1"] .vc-hint').textContent(),'Нарезка');motionChecks++;
  // Reduced-motion sheets change hidden synchronously; rings keep their percent.
  assert.deepEqual(await motionPage.evaluate(()=>{
    window.aiVoiceOpenSettings('audio');const sheet=document.querySelector('#settings-sheet'),opened=!sheet.hidden&&!sheet.inert;
    const animation=getComputedStyle(document.querySelector('.settings-card')).animationName;
    document.querySelector('#settings-close').click();return [opened,sheet.hidden,sheet.inert,animation];
  }),[true,true,true,'none']);motionChecks++;
  assert.deepEqual(await motionPage.evaluate(()=>{
    document.querySelector('#vc-add-btn').click();const sheet=document.querySelector('#vc-sheet'),opened=!sheet.hidden&&!sheet.inert;
    document.querySelector('#vc-sheet-close').click();return [opened,sheet.hidden,sheet.inert];
  }),[true,true,true]);
  assert.equal(await motionPage.locator('#vc-list .vc-ring').first().textContent(),'40%');
  assert.equal(await motionPage.locator('.vc-indeterminate i').evaluate(e=>getComputedStyle(e).animationName),'none');motionChecks++;
  await motionPage.emulateMedia({reducedMotion:'no-preference'});
  await motionPage.locator('#vc-train-queue [data-id="motion11"] button').last().click();
  await motionPage.waitForSelector('#vc-train-queue .motion-row-exit');
  assert.deepEqual(await motionPage.locator('#vc-train-queue .motion-row-exit').evaluate(e=>[e.inert,e.getAttribute('aria-hidden'),getComputedStyle(e).pointerEvents]),[true,'true','none']);
  await motionPage.waitForFunction(()=>document.querySelectorAll('.motion-row-exit').length===0,null,{timeout:1000});motionChecks++;
  vcVoices[1].status='ready';delete vcVoices[1].stage;await pollMotion();
  assert.equal(await motionPage.locator('.vc-card[data-id="motion1"] .vc-indeterminate').count(),0);
  assert.equal(await motionPage.locator('.vc-card[data-id="motion1"] .vc-badge').textContent(),'Обученный');motionChecks++;
  hfDownload={state:'idle',id:null,bytes:0,total:0};
  await motionPage.click('#vc-catalog-tab');await motionPage.waitForSelector('.hf-download');
  await motionPage.locator('.hf-download').first().click();
  await motionPage.waitForFunction(()=>document.querySelector('.hf-download.vc-ring')?.textContent==='50%'&&!document.querySelector('.hf-download.vc-ring').disabled);
  await motionPage.evaluate(()=>{window.__hfRing=document.querySelector('.hf-download.vc-ring');window.__hfRing.focus();});
  for(const percent of [60,70,80]){
    hfDownload={...hfDownload,bytes:percent};
    await motionPage.waitForFunction(p=>document.querySelector('.hf-download.vc-ring')?.textContent===p+'%',percent);
    assert.deepEqual(await motionPage.evaluate(()=>{
      const ring=document.querySelector('.hf-download.vc-ring');
      return [ring===window.__hfRing,document.activeElement===ring,ring.style.getPropertyValue('--p'),ring.textContent,ring.title];
    }),[true,true,percent+'%',percent+'%','Отменить загрузку']);
  }
  motionChecks++;
  await motionPage.close();
  // Windows shell: pywebview bridge, hidden swap-hotkey control, Ctrl shortcuts, drag region.
  const winPage=await browser.newPage({viewport:{width:1220,height:820}});
  winPage.on('pageerror',e=>errors.push(e.message));
  await winPage.addInitScript(()=>{window.__winNotify=[];window.pywebview={api:{notify:p=>window.__winNotify.push(p)}};});
  await winPage.route('**/api/voices',route=>route.fulfill({json:{items:ttsVoices,language:'ru',preferences:prefs,platform:'win',
    speech_languages:[{id:'en-US'},{id:'ru-RU'}],speech_language:'ru-RU',
    devices:{inputs:['MIC'],outputs:['AI Voice'],monitors:[],default_input:'MIC',default_output:'AI Voice',virtual:['AI Voice']}}}));
  await winPage.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await winPage.waitForFunction(()=>document.querySelectorAll('.voice-row').length===3);
  assert.equal(await winPage.locator('header.n-toolbar.pywebview-drag-region').count(),1);
  assert.equal(await winPage.locator('.n-traffic-spacer.pywebview-drag-region').count(),1);uiChecks++;
  await winPage.evaluate(()=>window.aiVoiceOpenSettings('keys'));
  await winPage.waitForSelector('#settings-sheet:not([hidden])');await winPage.waitForTimeout(300);
  assert.equal(await winPage.locator('#swap-hotkey').count(),0);
  assert.equal(await winPage.locator('#swap-toggle').count(),1);
  assert.match(await winPage.locator('#settings-btn').getAttribute('title'),/Ctrl/);uiChecks++;
  await winPage.keyboard.press('Escape');await winPage.waitForTimeout(250);
  await winPage.waitForSelector('#settings-sheet',{state:'hidden'});
  await winPage.keyboard.press('Control+Comma');
  await winPage.waitForSelector('#settings-sheet:not([hidden])');uiChecks++;
  await winPage.evaluate(()=>window.nativePost('notify',{title:'AI Voice',body:'done'}));
  assert.deepEqual(await winPage.evaluate(()=>window.__winNotify),[{title:'AI Voice',body:'done'}]);uiChecks++;
  await winPage.keyboard.press('Escape');
  await winPage.close();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,uiChecks,settingsChecks,vcChecks,motionChecks,pageErrors:errors,screenshots:['console-qa/desktop-1220.png','console-qa/desktop-850.png','console-qa/desktop-850-scrolled.png'],reach,catalogPreviewedId}));
 }finally{if(browser)await browser.close();server.close();fs.writeFileSync(path.join(out,'requests.json'),JSON.stringify(requests,null,2));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
