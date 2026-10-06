const renderPage=require('./page.cjs');
const serveLocale = require('./locale-route.cjs');
/* Screenshots of the main screen and every settings section on a mocked API (no audio, no network).
   Usage: node tests/settings-screens.cjs [--out DIR]
   Default DIR is docs/screens; files are settings-<screen>-<size>-<scheme>.png. */
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const args=process.argv.slice(2);
const out=path.resolve(args.includes('--out')?args[args.indexOf('--out')+1]:path.join(root,'docs/screens'));
fs.mkdirSync(out,{recursive:true});

const prefs={language:'ru',catalog_language:'ru',speech_language:'ru-RU',voice_id:'db89e349112e44dca6820e0cb2d414cc',favorite_ids:[],input_device:'MIC',output_device:'AI Voice',output_gain_db:0,normalize_loudness:true,monitor_enabled:false,monitor_device:null,monitor_gain_db:0,input_gain_db:0,monthly_char_limit:50000,asr_engine:'apple'};
const voices=[{id:'db89e349112e44dca6820e0cb2d414cc',name:'Быков',language:'ru',source:'local',avatar_url:null,tags:['male'],task_count:1200},
  {id:'00000000000000000000000000000001',name:'Женский голос',language:'ru',source:'fish',avatar_url:null,tags:['female'],task_count:900}];
const storage={total:2350000000,categories:[
  {id:'vc_voices',label:'Мои голоса',bytes:800000000,clearable:true,note:'',items:[{id:'v1',label:'Диктор',bytes:500000000},{id:'v2',label:'Мой голос',bytes:300000000}]},
  {id:'vc_models',label:'Модели Живого голоса',bytes:900000000,clearable:false,note:'нужны для работы Живого голоса'},
  {id:'vc_env',label:'Окружение Живого голоса',bytes:400000000,clearable:false,note:'нужно для работы Живого голоса'},
  {id:'asr_gigaam',label:'Распознавание GigaAM',bytes:200000000,clearable:true,note:'скачается снова при выборе GigaAM'},
  {id:'logs',label:'Логи',bytes:34000,clearable:true,note:''},
  {id:'uploads',label:'Временные загрузки',bytes:12000000,clearable:true,note:''},
  {id:'cache',label:'Кэш каталога',bytes:6000,clearable:true,note:''},
  {id:'app',label:'Приложение',bytes:38000000,clearable:false,note:''}]};
const status={active:false,state:'stopped',mode:'mic',message:'Остановлено',transcript_partial:'',transcript_final:'',monitor_errors:0,
  usage:{today:1240,month:18700,limit:50000},history:[{id:'h1',text:'Привет, как дела?'},{id:'h2',text:'Сейчас подключусь к звонку'}]};

const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');if(serveLocale(url,res,root))return;for await(const _ of req);
  const reply=(data,code=200)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(url.pathname==='/'){
    const dir=path.join(root,'macos/desktop');
    const html=renderPage(root);
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end(html);
  }
  if(url.pathname==='/api/voices')return reply({items:voices,preferences:prefs,language:prefs.language,speech_languages:[{id:'en-US'},{id:'ru-RU'}],speech_language:prefs.speech_language,devices:{inputs:['MIC','MacBook Pro Microphone'],outputs:['AI Voice','MacBook Pro Speakers'],monitors:['MacBook Pro Speakers'],default_input:'MIC',default_output:'AI Voice',virtual:['AI Voice']}});
  if(url.pathname==='/api/keys')return reply({fish:true,hf:false});
 if(url.pathname==='/api/permissions')return reply({microphone:'authorized',speech:'authorized'});
 if(url.pathname==='/api/driver/status')return reply({available:false});
  if(url.pathname==='/api/voice_metadata')return reply({items:voices,pending:false});
  if(url.pathname==='/api/status')return reply({...status,monitor_active:false});
  if(url.pathname==='/api/storage')return reply(storage);
  if(url.pathname==='/api/search')return reply({items:[],has_more:false,page:1});
  if(url.pathname==='/api/vc/voices')return reply({items:[],training:null,last_done:null,runtime_ok:true});
  if(url.pathname==='/api/preferences')return reply(prefs);
  return reply({error:'Unknown route'},404);
});

const screens=['main','general','audio','asr','storage','usage'];
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
        await page.goto(base,{waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>document.querySelectorAll('.voice-row').length>0,{timeout:8000});
        await page.waitForTimeout(1300);
        for(const name of screens){
          if(name==='main')await page.evaluate(()=>document.querySelector('#settings-close').click());
          else await page.evaluate(section=>window.aiVoiceOpenSettings(section),name);
          if(name==='storage')await page.waitForSelector('#storage-bar .storage-seg');
          await page.waitForTimeout(300);
          const file=path.join(out,`settings-${name}-${w}x${h}-${scheme}.png`);await page.screenshot({path:file});shots.push(file);
        }
        await context.close();
      }
    }
    console.log(JSON.stringify({pass:errors.length===0,count:shots.length,out,pageErrors:errors}));
    if(errors.length)process.exitCode=1;
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exit(1);});
