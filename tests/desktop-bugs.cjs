const renderPage=require('./page.cjs');
const serveLocale = require('./locale-route.cjs');
/* Regression checks for the 2026-10-02 bug hunt. Mocked API, headless Chrome. */
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const bykov={id:'db89e349112e44dca6820e0cb2d414cc',name:'Быков',language:'ru',source:'fish',avatar_url:null,tags:['male'],task_count:100};
let prefs={language:'ru',catalog_language:'ru',speech_language:'ru-RU',voice_id:bykov.id,favorite_ids:[],input_device:'MIC',output_device:'AI Voice',output_gain_db:0,normalize_loudness:true,monitor_enabled:false,monitor_device:null,monitor_gain_db:0,input_gain_db:0};
let status={active:false,state:'stopped',mode:'mic',message:'Остановлено',transcript_partial:'',transcript_final:''};
let previewState='idle';
let ttsVoices=[bykov];
let rawResponse=false,prefsFail=null,prefsDelay=0;
const requests=[];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');if(serveLocale(url,res,root))return;let text='';for await(const x of req)text+=x;const body=text?JSON.parse(text):{};
 requests.push({path:url.pathname,body});
 const reply=(data,code=200)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
 if(url.pathname==='/'){
  const html=renderPage(root);
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(html);
 }
 if(url.pathname==='/api/voices')return reply({items:ttsVoices,preferences:prefs,language:prefs.language,speech_languages:[{id:'en-US'},{id:'ru-RU'}],speech_language:prefs.speech_language,devices:{inputs:['MIC'],outputs:['AI Voice'],monitors:['X'],default_input:'MIC',default_output:'AI Voice',virtual:['AI Voice']}});
 if(url.pathname==='/api/keys')return reply({fish:true,hf:false});
 if(url.pathname==='/api/permissions')return reply({microphone:'authorized',speech:'authorized'});
 if(url.pathname==='/api/driver/status')return reply({available:false});
 if(url.pathname==='/api/voice_metadata')return reply({items:ttsVoices,pending:false});
 if(url.pathname==='/api/status'){
  if(rawResponse){res.writeHead(500,{'Content-Type':'text/plain'});return res.end('boom');}
  return reply({...status,preview_state:previewState,clickedid:bykov.id});
 }
 if(url.pathname==='/api/search')return reply({items:[],has_more:false,page:1});
 if(url.pathname==='/api/preview'){previewState=body.action==='start'?'loading':'idle';return reply({ok:true,preview_state:previewState,preview_id:body.id});}
 if(url.pathname==='/api/preferences'){
  if(rawResponse){res.writeHead(500,{'Content-Type':'text/plain'});return res.end('boom');}
  if(prefsDelay)await new Promise(r=>setTimeout(r,prefsDelay));
  if(prefsFail)return reply({error:prefsFail},400);
  prefs={...prefs,...body};return reply(prefs);
 }
 if(url.pathname==='/api/control'){
  if((body.action==='start'||body.action==='speak')&&!body.voice_id){
   status={...status,state:'error',message:'Choose a voice first'};
   return reply({error:status.message},400);
  }
  return reply(status);
 }
 return reply({error:'Unknown route'},404);
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;
 try{
  browser=await chromium.launch({executablePath:process.env.AI_VOICE_CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1220,height:820}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port+'/',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#output-gain',{timeout:6000});
  await page.waitForFunction(()=>document.querySelectorAll('.voice-row').length>=1,{timeout:8000});

  // Bug 1: fader value must not roll back to a stale latest value.
  prefsDelay=2500;
  for(const id of ['output-gain','input-gain']){
   await page.locator('#'+id).evaluate(el=>{el.value='9';el.dispatchEvent(new Event('input',{bubbles:true}));});
   await page.waitForTimeout(1600);
   assert.equal(await page.locator('#'+id).inputValue(),'9','bug1: #'+id+' rolled back');
  }
  prefsDelay=0;await page.waitForTimeout(3000);
  // Bug 2: cap moves up as the value grows and stays inside the track.
  for(const [id,cap,track] of [['output-gain','output-cap','output-track'],['input-gain','input-cap','input-track']]){
   const pos=async v=>{await page.locator('#'+id).evaluate((el,v)=>{el.value=String(v);el.dispatchEvent(new Event('input',{bubbles:true}));},v);
    await page.waitForTimeout(300);return page.evaluate(([c,t])=>{const a=document.getElementById(c).getBoundingClientRect(),b=document.getElementById(t).getBoundingClientRect();return {top:a.top,bottom:a.bottom,tt:b.top,tb:b.bottom};},[cap,track]);};
   const hi=await pos(12),lo=await pos(-24);
   assert.ok(hi.top<lo.top,'bug2: '+id+' cap must be higher at +12 than at -24 '+JSON.stringify({hi,lo}));
   assert.ok(hi.top>=hi.tt-1&&lo.bottom<=lo.tb+1,'bug2: '+id+' cap must stay inside track '+JSON.stringify({hi,lo}));
   await pos(0);
  }
  // Bug 3: preview button turns active on 'loading' and returns to idle when the backend finishes.
  const pbtn=page.locator('.preview-btn').first();
  await pbtn.click();
  await page.waitForTimeout(1500);
  assert.equal(await pbtn.getAttribute('data-state'),'active','bug3: button must stay active while backend is loading/active');
  previewState='idle';
  await page.waitForTimeout(1500);
  assert.notEqual(await pbtn.getAttribute('data-state'),'active','bug3: button must reset when backend preview ends');
  // Bug 4: text-mode caption must not be overwritten by mic captions on status polls.
  status={...status,active:true,state:'listening',transcript_final:'Фраза микрофона'};
  await page.locator('.mode-btn[data-mode="text"]').click();
  await page.waitForTimeout(1500);
  const cap4=await page.locator('#mic-transcript-final').textContent();
  assert.ok(!/микрофон|Начни говорить|Выбранный голос|Фраза/.test(cap4),'bug4: text-mode caption overwritten: '+cap4);
  status={...status,active:false,state:'stopped',transcript_final:''};
  // Bug 8: a failed favorites save rolls the star back.
  await page.locator('#fav-filter').count();
  const star=page.locator('[data-star]').first();
  assert.equal(await star.getAttribute('aria-pressed'),'false');
  prefsFail='Некорректный запрос интерфейса.';
  await star.click();
  await page.waitForTimeout(600);
  assert.equal(await page.locator('[data-star]').first().getAttribute('aria-pressed'),'false','bug8: star must roll back after failed save');
  prefsFail=null;
  // Bug 10: non-JSON error responses give a readable message, not 'Unexpected token'.
  rawResponse=true;
  await page.locator('#output-gain').evaluate(el=>{el.value='-5';el.dispatchEvent(new Event('input',{bubbles:true}));});
  await page.waitForTimeout(900);
  const line10=await page.locator('#toast').textContent();
  assert.ok(/потеряна/.test(line10)&&!/Unexpected|JSON/i.test(line10),'bug10: raw parse error leaked or no toast: '+line10);
  rawResponse=false;
  // Bug 2 (CSS): cap centre sits on the scale label of its value (+12 / 0 / -24).
  for(const [id,cap,key] of [['output-gain','output-cap','output'],['input-gain','input-cap','input']]){
   for(const [v,label] of [[12,'+12'],[0,'0'],[-24,'-24']]){
    await page.locator('#'+id).evaluate((el,v)=>{el.value=String(v);el.dispatchEvent(new Event('input',{bubbles:true}));},v);
    await page.waitForTimeout(350);
    const d=await page.evaluate(([c,k,l])=>{const a=document.getElementById(c).getBoundingClientRect();
     const sp=[...document.querySelectorAll('.fader-cell[data-fader="'+k+'"] .fader-scale span')].find(x=>x.textContent===l).getBoundingClientRect();
     return Math.abs((a.top+a.bottom)/2-(sp.top+sp.bottom)/2);},[cap,key,label]);
    assert.ok(d<=2,'bug2css: '+id+' cap at '+v+' is '+d.toFixed(1)+'px off its label');
   }
  }
  // Avatar without photo: neutral fill, one initial, secondary label colour.
  const av=await page.evaluate(()=>{const a=document.getElementById('main-avatar'),cs=getComputedStyle(a);
   return {t:a.textContent,bg:cs.backgroundColor,fs:parseFloat(cs.fontSize),w:a.getBoundingClientRect().width,fw:cs.fontWeight,r:cs.borderRadius,img:a.querySelectorAll('img').length};});
  assert.equal(av.t,'Б','avatar: initial'); assert.ok(av.bg!=='rgb(255, 255, 255)'&&av.bg!=='rgba(0, 0, 0, 0)','avatar: bg '+av.bg);
  assert.ok(Math.abs(av.fs-av.w*.45)<1,'avatar: font-size 45% '+JSON.stringify(av)); assert.equal(av.fw,'600');
  // A fresh library offers catalog search; Start forwards the missing selection.
  ttsVoices=[];prefs={...prefs,voice_id:null,favorite_ids:[]};
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForSelector('#find-voice-btn',{timeout:6000});
  assert.equal(await page.locator('#voice-list .empty-state').textContent(),'No voices yet');
  assert.equal(await page.locator('#find-voice-btn').textContent(),'Find a voice');
  assert.equal(await page.locator('#start-btn').isEnabled(),true);
  await page.click('#start-btn');
  await page.waitForFunction(()=>document.querySelector('#status-line').textContent==='Choose a voice first');
  assert.equal(requests.filter(x=>x.path==='/api/control').at(-1).body.voice_id,null);
  await page.click('.mode-btn[data-mode="text"]');
  await page.fill('#text-input','Hello');
  await page.click('#start-btn');
  await page.waitForFunction(()=>document.querySelector('#status-line').textContent==='Choose a voice first');
  assert.equal(requests.filter(x=>x.path==='/api/control').at(-1).body.action,'speak');
  assert.equal(await page.locator('#start-btn').isEnabled(),true);
  await page.click('#find-voice-btn');
  assert.equal(await page.locator('.tab[data-tab="catalog"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#search-input').evaluate(el=>el===document.activeElement),true);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true}));
 }finally{if(browser)await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
