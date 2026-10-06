const renderPage=require('./page.cjs');
const serveLocale = require('./locale-route.cjs');
/* Screenshots of the voice-conversion UI states on a mocked API (no audio, no network).
   Usage: node tests/vc-screens.cjs [--out DIR] [--with-tts]
   Default DIR is docs/screens; files are vc-<state>-<size>-<scheme>.png. */
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const args=process.argv.slice(2);
const out=path.resolve(args.includes('--out')?args[args.indexOf('--out')+1]:path.join(root,'docs/screens'));
const withTts=args.includes('--with-tts');
fs.mkdirSync(out,{recursive:true});

const prefs={language:'ru',catalog_language:'ru',speech_language:'ru-RU',voice_id:'db89e349112e44dca6820e0cb2d414cc',favorite_ids:[],input_device:'MIC',output_device:'AI Voice',output_gain_db:0,normalize_loudness:true,monitor_enabled:false,monitor_device:null,monitor_gain_db:0,input_gain_db:0};
const ttsVoices=[{id:'db89e349112e44dca6820e0cb2d414cc',name:'Быков',language:'ru',source:'local',avatar_url:null,tags:['male'],task_count:100},
  {id:'00000000000000000000000000000001',name:'Женский голос',language:'ru',source:'fish',avatar_url:null,tags:['female'],task_count:900}];
const trained={id:'v1',name:'Диктор',kind:'trained',status:'ready',pitch_shift:2,index_rate:0.75};
const quick={id:'v2',name:'Мой голос',kind:'zeroshot',status:'ready',pitch_shift:0,index_rate:0.75,speech_seconds:252,can_train:true};
const learning={id:'v3',name:'Голос для стрима',kind:'zeroshot',status:'ready',pitch_shift:0,index_rate:0.75,speech_seconds:540,can_train:true};
const training={voice_id:'v3',stage:'train',stage_label:'Обучение',epoch:57,total_epochs:200,progress:.4,eta_s:2400,state:'running',error:null};
const idle={active:false,state:'stopped',mode:'mic',message:'Остановлено',transcript_partial:'',transcript_final:'',monitor_errors:0};
const vcSnap=(state,extra={})=>({voice_id:'v1',state,latency_ms:null,cpu:null,rss_mb:null,input_level:null,output_level:null,dropped_blocks:0,error:null,...extra});

// Each scene: voices list, training block, /api/status payload, and an optional page action.
const scenes={
  empty:{voices:[],training:null,status:idle},
  list:{voices:[trained,quick,learning],training,status:idle},
  'sheet-import':{voices:[trained,quick],training:null,status:idle,act:async page=>{await page.click('#vc-add-btn');await page.click('[data-vc-tab="import"]');
    await page.setInputFiles('#vc-import-files',[{name:'dictor_e300.pth',mimeType:'application/octet-stream',buffer:Buffer.alloc(1024)},{name:'added_IVF256_Flat.index',mimeType:'application/octet-stream',buffer:Buffer.alloc(512)}]);
    await page.fill('#vc-import-name','Диктор 2');}},
  'sheet-audio':{voices:[trained,quick],training:null,status:idle,act:async page=>{await page.click('#vc-add-btn');await page.click('[data-vc-tab="audio"]');
    await page.setInputFiles('#vc-audio-files',[{name:'stream-2026-09-30.m4a',mimeType:'audio/mp4',buffer:Buffer.alloc(2048)},{name:'podcast.wav',mimeType:'audio/wav',buffer:Buffer.alloc(4096)}]);
    await page.fill('#vc-audio-name','Мой голос 2');}},
  running:{voices:[trained,quick],training:null,status:{...idle,active:true,state:'listening',mode:'vc',message:'Голос→голос работает',
    vc:vcSnap('running',{latency_ms:312,cpu:34,rss_mb:1843,input_level:.05,output_level:.12,processing_ms:56})}},
  loading:{voices:[trained,quick],training:null,status:{...idle,active:true,state:'loading',mode:'vc',message:'Загрузка голоса…',vc:vcSnap('loading')}},
  novenv:{voices:[trained,quick],training:null,status:{...idle,active:false,state:'error',mode:'vc',message:'Окружение .venv-vc не найдено. Установите его по vc_worker/README.md.',
    vc:vcSnap('error',{voice_id:null,error:'Окружение .venv-vc не найдено. Установите его по vc_worker/README.md.'})}},
};
let scene=scenes.empty,live=false; // live=false serves idle status until the vc tab is open

const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');if(serveLocale(url,res,root))return;for await(const _ of req);
  const reply=(data,code=200)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(url.pathname==='/'){
    const dir=path.join(root,'macos/desktop');
    const html=renderPage(root);
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(html);
  }
  if(url.pathname==='/api/voices')return reply({items:ttsVoices,preferences:prefs,language:prefs.language,speech_languages:[{id:'en-US'},{id:'ru-RU'}],speech_language:prefs.speech_language,devices:{inputs:['MIC'],outputs:['AI Voice'],monitors:['MacBook Pro Speakers'],default_input:'MIC',default_output:'AI Voice',virtual:['AI Voice']}});
  if(url.pathname==='/api/keys')return reply({fish:true,hf:false});
 if(url.pathname==='/api/permissions')return reply({microphone:'authorized',speech:'authorized'});
 if(url.pathname==='/api/driver/status')return reply({available:false});
  if(url.pathname==='/api/voice_metadata')return reply({items:ttsVoices,pending:false});
  if(url.pathname==='/api/status')return reply({...(live?scene.status:idle),training:scene.training,monitor_active:false});
  if(url.pathname==='/api/search')return reply({items:[],has_more:false,page:1});
  if(url.pathname==='/api/vc/voices')return reply({items:scene.voices,training:scene.training,last_done:null});
  if(url.pathname==='/api/preferences')return reply(prefs);
  return reply({error:'Unknown route'},404);
});

const sizes=[[850,650],[1220,820]];
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base='http://127.0.0.1:'+server.address().port+'/';
  const browser=await chromium.launch({executablePath:process.env.AI_VOICE_CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  const shots=[],errors=[];
  try{
    for(const scheme of ['light','dark']){
      for(const [w,h] of sizes){
        const context=await browser.newContext({viewport:{width:w,height:h},colorScheme:scheme});
        const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
        const open=async()=>{await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>document.querySelectorAll('.voice-row').length>0,{timeout:8000});};
        if(withTts){
          scene=scenes.empty;live=false;
          for(const mode of ['mic','text']){
            await open();await page.click(`.mode-btn[data-mode="${mode}"]`);await page.waitForTimeout(350);
            const file=path.join(out,`tts-${mode}-${w}x${h}-${scheme}.png`);await page.screenshot({path:file});shots.push(file);
          }
        }
        for(const [name,item] of Object.entries(scenes)){
          scene=item;live=false;await open();
          await page.click('.mode-btn[data-mode="vc"]');
          await page.waitForFunction(n=>document.querySelectorAll('#vc-list .vc-card').length===n||(n===0&&!!document.querySelector('#vc-list .empty-state:not(.vc-list-error)')&&!/Загрузка/.test(document.querySelector('#vc-list').textContent)),item.voices.length,{timeout:5000});
          if(item.voices.length) await page.click(`.vc-card[data-id="${item.voices[0].id}"]`);
          live=true;
          if(item.act) await item.act(page);
          await page.waitForTimeout(1300); // one status poll so readouts and training are filled
          const file=path.join(out,`vc-${name}-${w}x${h}-${scheme}.png`);await page.screenshot({path:file});shots.push(file);
        }
        await context.close();
      }
    }
    console.log(JSON.stringify({pass:errors.length===0,count:shots.length,out,pageErrors:errors}));
    if(errors.length) process.exitCode=1;
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exit(1);});
