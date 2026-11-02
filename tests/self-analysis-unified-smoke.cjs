"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Real hidden Electron + production sandbox bridge, synthetic RPC and visible controls only.
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const http = require("node:http");
const { mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, ".."), worldviewOnly = process.argv.includes("--worldview-only"), attachmentOnly = process.argv.includes("--attachment-only");
const output = path.join(root, ".local/tmp/self-analysis-unified-smoke", ...(worldviewOnly ? ["worldview-only"] : attachmentOnly ? ["attachment-only"] : []));
mkdirSync(output, { recursive: true });
app.setPath("userData", path.join(output, "electron-profile"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
const hostSource = `"use strict";
window.smoke = { context: {account:'synthetic-account-a',user:'',surface:'self-analysis-window',theme:'light'},
  calls:[], hostCalls:[], depth:'deep', outputs:['profile','tags','persona','values','library'], modelConfigured:false, finished:false, starts:0, personaVersion:1, manual:{}, periodsVisible:false, worldviewVisible:false,
  profile:{background:'已保存的合成背景',style:'自然简短',version:1}, run:{state:'idle',steps:[],depth:'deep'} };
const definitions=[['history','同步本人文字'],['library','表达知识库'],['evidence','提取共同证据'],['profile','本人资料'],['tags','标签'],['persona','性格分析'],['values','三观分析'],['semantic','本地语义索引']];
function selected(key,outputs=smoke.run.outputs||smoke.outputs){return key==='history'||key==='evidence'?key==='history'||outputs.some(k=>['profile','tags','persona','values'].includes(k)):outputs.includes(key==='semantic'?'library':key);}
function status(){const s=smoke;return {account:s.context.account,source:{ready:true},build:{state:s.run.state==='running'?'running':'complete',phase:'indexing',processedConversations:8,totalConversations:8,sampleCount:10,messageCount:240},
 coverage:{catalogComplete:true,complete:true,sampleCount:10,messageCount:240,indexedConversations:8,discoveredConversations:8,failedConversations:0},
 embedding:{state:'ready',ready:true,indexState:'ready'},retrieval:{mode:'hybrid',semanticReady:true,indexedSamples:10,pendingSamples:0,totalSamples:10}};}
function evidence(quote,reason,category='support'){return {user:'合成朋友甲',id:'synthetic/m1',time:1791849600000,quote,reason,category,truncated:false,
 contextBefore:[{id:'m0',seq:1,time:1791849599000,side:'other',speaker:'合成朋友甲',text:'你说的是自己的感受吗？',truncated:false}],
 contextAfter:[{id:'m2',seq:3,time:1791849601000,side:'self',speaker:'本人',text:'是的，但只是在这个场景下。',truncated:false}]};}
function persona(){const s=smoke;const keys=['big5_o','big5_c','big5_e','big5_a','big5_n','att_anx','att_avo','pol_axis','pol_ext','pol_econ','pol_lib','val_change','val_self','moral_care','moral_fair','moral_loyal','moral_auth','moral_sanct','moral_liberty'];
 const inferred={
 big5_e:{text:'在所见聊天里偏好先独自整理想法。',confidence:'把握较低',scope:'仅限所见聊天表现',position:-0.3,counts:{low:3,high:1,mixed:0},evidence:[evidence('我想先自己安静一会儿','支持特定场景下偏好独处')],evidenceTotal:2,counter:[evidence('周末聚会我来组织<img src=x onerror="window.hacked=true">','也有主动组织社交的反例','counter')]},
 big5_o:{text:'愿意尝试新的活动和话题。',confidence:'较有把握',scope:'跨多个时期',position:0.5,counts:{low:0,high:5,mixed:0},evidence:[evidence('这个展我想去看看','主动尝试新体验')],counter:[]},
 att_anx:{text:'相处中常直接说明边界，较少反复确认。',confidence:'有一定依据',scope:'仅限所见互动方式',position:-0.25,counts:{low:3,high:1,mixed:0},evidence:[evidence('今晚我想自己休息，我们明天再聊吧','明确表达当下边界')],counter:[]},
 pol_axis:{text:'关心公共服务的平等可及。',confidence:'把握较低',scope:'只反映这段聊天中的议题',position:-0.3,counts:{low:2,high:0,mixed:0},evidence:[evidence('我更关心这个服务能否方便老人使用','表达具体议题关切')],counter:[]},
 moral_care:{text:'评判时常诉诸对弱者的关照。',confidence:'有一定依据',scope:'所见聊天',position:0.6,counts:{low:0,high:4,mixed:0},evidence:[evidence('别让老人家为难','诉诸关照')],counter:[]}};
 return {version:s.personaVersion,dimensions:keys.map(key=>({key,label:key,selfReport:null,inference:s.finished&&inferred[key]?inferred[key]:null,manual:s.manual[key]?{text:s.manual[key],updatedAt:1791849600000}:null})),
  selfReports:{mbti:s.finished?{text:'INFP',confidence:'本人自述',scope:'本人明确自述，未当作测评结论',fromMs:1760000000000,toMs:1791849600000,evidence:[evidence('我之前测出来是 INFP','保留本人自述及其时间范围','selfReport')],counter:[],conflicting:false}:null,attachment:null,politics:null},
  coverage:{ownMessages:120,analyzedMessages:s.finished?120:0,evidenceCount:s.finished?5:0,depth:s.depth}};}
function values(){const labels=[['love','感情观'],['family','家庭观'],['money','金钱观'],['plan','人生规划'],['core','价值底色']];
 return {version:1,dimensions:labels.map(([key,label],index)=>({key,label,selfReport:null,manual:null,inference:smoke.finished&&!index?{text:'所见聊天里更看重长期、稳定的相处，依据有限。',confidence:'把握较低',scope:'仅限所见聊天时期',evidence:[evidence('我还是想慢慢来，找个能长久的人','表达对长期关系的看重')],counter:[]}:null})),coverage:{ownMessages:120,analyzedMessages:smoke.finished?120:0}};}
function tags(){return {account:smoke.context.account,run:{state:smoke.finished?'complete':'idle'},counts:{messages:120,pendingMessages:0},categories:['运动健身','性格习惯','其他'],tags:smoke.finished?[
 {key:'篮球',name:'篮球',category:'运动健身',relation:'mention',level:'possible',manual:false,mentions:9,chats:3,days:8},
 {key:'夜猫子',name:'夜猫子',category:'性格习惯',relation:'trait',level:'strong',manual:false,mentions:3,chats:2,days:2}]:[]};}
function worldview(){return {months:6,coverage:smoke.worldviewVisible?{total:3,completed:2,pending:0,insufficient:1}:{total:0,completed:0,pending:0,insufficient:0},periods:smoke.worldviewVisible?['2025H2','2026H1','2026H2'].map((id,index)=>({id,label:['2025 下半年','2026 上半年','2026 下半年'][index],status:index?'complete':'insufficient',passes:3,items:38,
 factors:Object.fromEntries(['pol_econ','pol_lib','val_change','val_self'].map((key,n)=>[key,{score:index?index===1?2.6+n*.3:3.4-n*.2:null,low:index?index===1?2.4+n*.3:3.2-n*.2:null,high:index?index===1?2.8+n*.3:3.6-n*.2:null,items:index?12:0,total:16,chats:6}])),letters:index?[index===1?'社会民主主义':'保守主义']:[],style:index?index===1?'进取派':'守护派':'',
 views:Object.fromEntries(['love','family','money','plan','core'].map((key,n)=>[key,{item:101+n,text:!index?'':key==='money'?index===1?'想先攒钱':'愿意花在体验上':{love:'希望相处中互相支持',family:'重视家人各自的选择',plan:'逐步建立稳定的生活',core:'尊重不同人的处境'}[key],status:index?'complete':'insufficient',evidence:index?[{id:'view-'+key,k:'自述',s:1,name:'合成朋友',time:1791849600000,text:'这是合成的分期观点依据'}]:[],evidenceTotal:index?key==='money'?3:1:0,evidenceNextOffset:index&&key==='money'?1:null,evidenceVersion:'synthetic-view-v1'}]))})):[]};}
function attachment(){const cases=[['2024H2',null,3,'complete'],['2025H1',2,null,'complete'],['2025H2',null,null,'insufficient'],['2026H1',null,null,'no-target'],['2026H2',2,2.5,'complete']];
 return {months:6,coverage:{total:5,completed:3,insufficient:1,noTarget:1},periods:smoke.attachmentVisible?cases.map(([id,anxiety,avoidance,status])=>({id,label:id.slice(0,4)+(id.endsWith('H1')?' 上半年':' 下半年'),status,passes:3,items:9,
 factors:Object.fromEntries([['att_anx',anxiety],['att_avo',avoidance]].map(([key,score])=>[key,{score,low:score===null?null:score-.1,high:score===null?null:score+.1,items:score===null?0:3,total:3}])),letters:anxiety!==null&&avoidance!==null?['安全']:[],target:{users:status==='no-target'?[]:['synthetic-target'],names:status==='no-target'?[]:['合成对象'],confirmed:true}})):[]};}
function rpc(plugin,method,params,scope){const s=smoke;s.calls.push({method,params,scope});if(plugin!=='kyf.self-analysis'||scope.account!==s.context.account)throw new Error('synthetic scope mismatch');
 if(method==='self.status')return status();
 if(method==='self.analysisSettings'){if(Array.isArray(params.outputs))s.outputs=params.outputs;return {outputs:s.outputs,depth:s.depth};}
 if(method==='self.analysisStatus')return s.run;
 if(method==='self.analyze'){s.starts++;const outputs=params.outputs||s.outputs,ai=outputs.some(k=>['profile','tags','persona','values'].includes(k)),phase=s.modelConfigured&&ai?'evidence':'library';s.run={id:'unified-1',state:'running',depth:s.depth,outputs,pausedOutputs:[],phase,elapsedMs:125000,coverage:{ownMessages:120},
  ...(s.modelConfigured&&ai?{eta:{scope:'phase',label:'共同证据提取与复核',lowMs:180000,highMs:420000,basis:'observed'}}:{}),
  steps:definitions.map(([key,title])=>({key,title,state:!selected(key,outputs)?'skipped':key==='history'||phase==='evidence'&&key==='library'?'done':key===phase?'running':'pending',
   ...(key==='history'?{done:8,total:8,unit:'个聊天'}:key==='library'?{done:phase==='evidence'?8:4,total:8,unit:'个聊天'}:key==='evidence'&&s.modelConfigured?{done:40,total:120,unit:'条',modelCalls:4,activeCallStartedAt:Date.now()-10000}:{}),detail:!selected(key,outputs)?'未选择':''}))};return s.run;}
 if(method==='self.analysisPause'){if(params.output){const output=params.output;s.run={...s.run,pausedOutputs:[...(s.run.pausedOutputs||[]),output],steps:s.run.steps.map(step=>step.key===output||output==='library'&&step.key==='semantic'?{...step,state:'paused'}:step)};}else{s.run={...s.run,state:'paused',steps:s.run.steps.map(step=>['running','pending'].includes(step.state)?{...step,state:'paused'}:step)};}return s.run;}
 if(method==='self.analysisResume'){const output=params.output;s.run={...s.run,pausedOutputs:(s.run.pausedOutputs||[]).filter(key=>key!==output),steps:s.run.steps.map(step=>(step.key===output||output==='library'&&step.key==='semantic')&&step.state==='paused'?{...step,state:step.key===s.run.phase?'running':'pending'}:step)};return s.run;}
 if(method==='self.draftStatus')return s.finished?{id:'profile-draft-1',state:'done',depth:s.depth,coverage:{conversations:8,readConversations:8,messagesRead:240,ownMessages:120,analyzedMessages:120,privateMessages:90,groupMessages:30,catalogComplete:true,analyzedParts:120,totalParts:120},result:{background:'AI 合成资料初稿，供你审阅。',style:'表达简短，常在需要时补充说明。'}}:{state:'idle'};
 if(method==='self.periods')return {periods:s.periodsVisible?['2026H1','2026H2'].map((id,index)=>({id,label:index?'2026 下半年':'2026 上半年',passes:3,items:50,
  factors:Object.fromEntries(['big5_e','big5_a','big5_c','big5_n','big5_o'].map(key=>[key,{score:index?3.2:3.6,items:10,total:10,chats:6}])),letters:index?['I','N','F','J']:['E','N','F','J']})):[]};
 if(method==='self.attachment')return attachment();
 if(method==='self.worldview')return worldview();
 if(method==='self.worldviewDetail')return {period:params.period,key:params.key,label:params.period==='2026H1'?'2026 上半年':'2026 下半年',items:[{item:1001,text:'合成政治题：公共服务应当保障平等机会。',source:'8values 第 1 题',english:'Synthetic question about equal access.',answers:[4,3,4],why:['第一轮：关注公共服务覆盖。','第二轮：也考虑资源分配限制。'],evidence:[{id:'period-one',k:'自述',s:1,name:'合成朋友',time:1791849600000,text:'希望所有人都能方便地使用公共服务。'}],evidenceTotal:3,evidenceNextOffset:1,evidenceVersion:'synthetic-period-v1',status:'complete'}]};
 if(method==='self.worldviewEvidence'&&params.item===103)return {items:[{id:'view-money-two',k:'自述',s:1,name:'合成朋友',time:1791849600100,text:'这是摘要分页返回的第二条合成依据。',counted:true},{id:'view-money-three',k:'自述',s:1,name:'合成朋友',time:1791849600200,text:'这是摘要分页返回的第三条合成依据。',counted:true}],total:3,nextOffset:null,version:'synthetic-view-v1'};
 if(method==='self.worldviewEvidence')return {items:[{id:'period-two',k:'行为',s:-1,name:'合成朋友',time:1791849600100,text:'也要考虑有限预算怎样分配。',counted:true},{id:'period-three',k:'处境',s:1,name:'合成朋友',time:1791849600200,text:'当时只是替朋友转述情况。',counted:false}],total:3,nextOffset:null,version:'synthetic-period-v1'};
 if(method==='self.personaEvidence')return {items:[{...evidence('参加活动时，我也会主动打招呼。','同维度的另一段观察'),id:'synthetic/m3',cited:false}],total:2,nextOffset:null,version:s.personaVersion};
 if(method==='self.judgeShadow')return {mode:'shadow',threshold:null,diagnosticThreshold:0.5,counts:{total:8,done:8,error:0},engines:[{kind:'timeline',engine:'systemone',positiveWindows:4,falseNegativeWindows:1}]};
 if(method==='self.tags')return tags();
 if(method==='self.persona')return persona();
 if(method==='self.values')return values();
 if(method==='self.personaEdit'){if(params.expectedVersion!==s.personaVersion)throw Object.assign(new Error('synthetic conflict'),{code:'conflict'});s.manual[params.key]=params.text;s.personaVersion++;return persona();}
 if(method==='self.exclusions')return {sources:[]};
 throw new Error('unexpected synthetic method: '+method);
}
const host={
 'model.status'(){return {configured:smoke.modelConfigured};},
 'self-profile.read'(){return {...smoke.profile};},
 'self-profile.save'(params){smoke.hostCalls.push({method:'self-profile.save',params});smoke.profile={background:params.background,style:params.style,version:smoke.profile.version+1};return {...smoke.profile};},
 'settings.open'(params){smoke.hostCalls.push({method:'settings.open',params});return {ok:true};}
};
const frame=KyfFrames.create({container:document.getElementById('page'),plugin:'kyf.self-analysis',page:'ui/index.html',context:smoke.context,host,rpc});
smoke.notify=()=>frame.event({type:'shown'});
smoke.finish=()=>{smoke.finished=smoke.modelConfigured;const ai=smoke.run.outputs.some(k=>['profile','tags','persona','values'].includes(k)),totals={history:8,library:8,evidence:120,profile:120,tags:120,persona:120,values:120,semantic:10};smoke.run={...smoke.run,state:smoke.modelConfigured||!ai?'done':'partial',steps:definitions.map(([key,title])=>{const fail=!smoke.modelConfigured&&['evidence','profile','tags','persona','values'].includes(key);return {key,title,state:!selected(key)?'skipped':fail?'error':'done',
 done:fail?0:totals[key],total:totals[key],unit:['history','library'].includes(key)?'个聊天':key==='semantic'?'个样本':'条',detail:!selected(key)?'未选择':fail?'尚未配置回复模型':''};})};smoke.notify();};
smoke.theme=theme=>{smoke.context={...smoke.context,theme};frame.post(smoke.context);};
`;
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, "http://127.0.0.1").pathname;
  if (pathname === "/favicon.ico") { response.writeHead(204); response.end(); return; }
  if (pathname === "/") {
    response.writeHead(200, { "Content-Type": types[".html"] });
    response.end('<!DOCTYPE html><html><head><meta charset="UTF-8"><style>html,body,#page{margin:0;width:100%;height:100%;overflow:hidden}iframe{width:100%;height:100%;border:0;display:block}</style></head><body><div id="page"></div><script src="/plugin-frame.js"></script><script src="/smoke-host.js"></script></body></html>'); return;
  }
  if (pathname === "/smoke-host.js") { response.writeHead(200, { "Content-Type": types[".js"] }); response.end(hostSource); return; }
  let file;
  if (pathname === "/plugin-frame.js") file = path.join(root, "chatui/plugin-frame.js");
  else if (pathname.startsWith("/plugin-sdk/web/")) file = path.join(root, "plugin-sdk/web", pathname.slice("/plugin-sdk/web/".length));
  else if (pathname.startsWith("/plugin-pages/kyf.self-analysis/")) file = path.join(root, "plugins/kyf.self-analysis", pathname.slice("/plugin-pages/kyf.self-analysis/".length));
  if (!file || !path.resolve(file).startsWith(root + path.sep)) { response.writeHead(404); response.end(); return; }
  try { response.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" }); response.end(readFileSync(file)); }
  catch { response.writeHead(404); response.end(); }
});
let win;
const errors = [], captures = [], geometry = [];
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const hostPage = code => win.webContents.executeJavaScript(code);
async function page(code) {
  for (let attempt = 0; attempt < 60; attempt++) {
    const frame = win.webContents.mainFrame.frames[0];
    if (frame) try { return await frame.executeJavaScript(code); } catch { }
    await wait(50);
  }
  throw new Error("Production self-analysis frame did not load");
}
async function until(code) {
  for (let attempt = 0; attempt < 80; attempt++) { if (await page(code)) return; await wait(60); }
  throw new Error("Timed out: " + code + "\n" + await page("document.body.innerText"));
}
async function clickVisible(id) {
  const control = await page(`(()=>{const e=document.getElementById(${JSON.stringify(id)});if(!e)return null;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {x:r.left+r.width/2,y:r.top+r.height/2,width:r.width,height:r.height,disabled:!!e.disabled,display:s.display,visibility:s.visibility,innerWidth,innerHeight};})()`);
  assert.ok(control && control.width > 0 && control.height > 0 && !control.disabled && control.display !== "none" && control.visibility !== "hidden", id + " must be a visible enabled control");
  assert.ok(control.x >= 0 && control.x < control.innerWidth && control.y >= 0 && control.y < control.innerHeight, id + " must be on screen");
  // A hidden native window cannot receive foreground input. Verify real layout
  // first, then activate only the visible control in the production iframe.
  await page(`document.getElementById(${JSON.stringify(id)}).click()`);
  await wait(80);
}
async function clickMatching(selector, id) {
  const found = await page(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.id=${JSON.stringify(id)};e.scrollIntoView({block:'center'});return true;})()`);
  assert.equal(found, true, selector + " must exist");
  await clickVisible(id);
}
async function capture(name, target = null) {
  await page(target ? `document.getElementById(${JSON.stringify(target)}).scrollIntoView({block:'start'})` : "document.getElementById('tabContent').scrollTop=0");
  await wait(80);
  const size = await page("({width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth})");
  assert.ok(size.scrollWidth <= size.width, name + " page overflows horizontally");
  const controls = await page("[...document.querySelectorAll('button,input,textarea,select')].filter(e=>!e.closest('.tabs')).flatMap(e=>{const r=e.getBoundingClientRect();return r.width&&r.height?[{id:e.id,left:r.left,right:r.right}]:[]})");
  for (const control of controls) assert.ok(control.left >= -1 && control.right <= size.width + 1, name + " control escapes viewport: " + JSON.stringify(control));
  const image = await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
  await wait(80);
  const painted = await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }), colors = new Set(), bitmap = painted.toBitmap();
  for (let index = 0; index < bitmap.length; index += 16) colors.add(bitmap.readUInt32LE(index));
  assert.ok(colors.size > 60, name + " image was not painted"); assert.equal(win.isVisible(), false);
  const file = path.join(output, name + ".png"); writeFileSync(file, painted.toPNG()); captures.push(file); geometry.push({ name, ...size });
}
async function finish(error) {
  const result = { ok: !error, errors, geometry, captures, ...(error ? { error: error.stack || String(error) } : {}) };
  writeFileSync(path.join(output, "result.json"), JSON.stringify(result, null, 2));
  process.stdout.write(JSON.stringify({ ok: result.ok, captures: captures.length, output, ...(error ? { error: String(error) } : {}) }) + "\n");
  win?.destroy(); server.close(); app.exit(error ? 1 : 0);
}
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  win = new BrowserWindow({ width: 1160, height: 1350, show: false, useContentSize: true, webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false } });
  win.webContents.on("console-message", details => { if (details.level === "error" && !details.message.includes("favicon")) errors.push(details.message); });
  await win.loadURL("http://127.0.0.1:" + server.address().port + "/");
  await until("!document.getElementById('analysisRun').disabled && document.getElementById('background').value==='已保存的合成背景'");
  if (attachmentOnly) {
    await hostPage("smoke.attachmentVisible=true;smoke.finished=true;smoke.notify()");
    await clickVisible("tab-persona");
    await until("document.querySelectorAll('#attachmentPeriods .kpc-p-col').length===5");
    const dotCount = "document.querySelectorAll('#personaCharts .kpc-card:nth-child(2) .kpc-dot').length";
    assert.equal(await page(dotCount), 1, "the two-axis period has a genuine point");
    await clickMatching("#attachmentPeriods .kpc-p-col:first-child", "smoke-attachment-one-axis");
    await until("document.getElementById('attachmentPeriodContext').innerText.includes('仅回避轴有依据；焦虑轴证据不足')");
    assert.equal(await page(dotCount), 0);
    assert.deepEqual(await page("[...document.querySelectorAll('#attachmentPeriods .kpc-p-value')].map(e=>e.innerText)"), ["—", "3.0"]);
    await capture("light-attachment-single-axis-1160", "attachmentPeriodContext");
    await clickMatching("#attachmentPeriods .kpc-p-value:last-of-type", "smoke-attachment-score");
    const draft = "period-draft-attachment-2024H2-score:att_avo";
    await until(`!!document.getElementById(${JSON.stringify(draft)})`);
    await page(`(()=>{const e=document.getElementById(${JSON.stringify(draft)});e.value='3.7';e.dispatchEvent(new Event('input'));})()`);
    await hostPage("smoke.notify()"); await wait(150);
    assert.equal(await page(`document.getElementById(${JSON.stringify(draft)}).value`), "3.7");
    for (const [index, label, points] of [[2, "仅焦虑轴有依据；回避轴证据不足", 0], [3, "证据不足", 0], [4, "未选定亲密关系", 0], [5, "调研完成", 1]]) {
      await clickMatching(`#attachmentPeriods .kpc-p-col:nth-child(${index})`, "smoke-attachment-select");
      await until(`document.getElementById('attachmentPeriodContext').innerText.includes(${JSON.stringify(label)})`);
      assert.equal(await page(dotCount), points);
    }
    await clickMatching("#attachmentPeriods .kpc-p-col:first-child", "smoke-attachment-back");
    await clickMatching("#attachmentPeriods .kpc-p-value:last-of-type", "smoke-attachment-draft");
    assert.equal(await page(`document.getElementById(${JSON.stringify(draft)}).value`), "3.7");
    await hostPage("smoke.theme('dark')"); win.setContentSize(375, 1000); await wait(120);
    await page("document.querySelector('#personaCharts .kpc-card:nth-child(2)').id='smoke-attachment-chart'");
    await capture("dark-attachment-single-axis-375", "smoke-attachment-chart");
    assert.equal(await hostPage("smoke.calls.some(call=>call.method==='self.attachmentEdit')"), false);
    assert.deepEqual(errors, []);
    await finish(); return;
  }
  if (worldviewOnly) {
    await hostPage("smoke.run={state:'running',outputs:['worldview'],steps:[{key:'worldview',title:'三观时间线',state:'pending'}]};smoke.notify()");
    await clickVisible("tab-values");
    await until("document.getElementById('valuesPeriodLine').innerText.includes('三观时间线排队中')");
    assert.equal(await page("document.getElementById('valuesPeriodLine').hidden"), false);
    await capture("light-worldview-queued-1160");
    await clickVisible("analysisPanelClose");
    await hostPage("smoke.worldviewVisible=true;smoke.run.steps[0].state='running';smoke.notify()");
    await until("document.querySelectorAll('#valuesPeriods .kpc-p-col').length===3");
    await clickMatching("#valuesPeriodViews > .persona-card:nth-child(3) .persona-edit", "smoke-summary-edit-money");
    const draft = "period-draft-worldview-2026H2-view:money";
    await until(`!!document.getElementById(${JSON.stringify(draft)})`);
    await page(`(()=>{const e=document.getElementById(${JSON.stringify(draft)});e.value='摘要分页仍保留的三观修正';e.dispatchEvent(new Event('input'));e.focus();e.setSelectionRange(6,6);})()`);
    await clickMatching("#worldview-2026H2-money summary", "smoke-summary-money-evidence");
    await clickMatching("#worldview-2026H2-money .evidence-more", "smoke-summary-money-more");
    await until("document.querySelectorAll('#worldview-2026H2-money .sample').length===3");
    assert.equal(await page(`document.getElementById(${JSON.stringify(draft)}).value`), "摘要分页仍保留的三观修正");
    assert.equal(await page("document.activeElement.id"), draft);
    assert.equal(await page(`document.getElementById(${JSON.stringify(draft)}).selectionStart`), 6);
    assert.equal(await page("document.querySelectorAll('#worldview-2026H2-money .evidence-more').length"), 0);
    assert.deepEqual(await hostPage("smoke.calls.find(c=>c.method==='self.worldviewEvidence').params"),
      { period: "2026H2", item: 103, offset: 1, limit: 50, expectedVersion: "synthetic-view-v1" });
    await capture("light-worldview-summary-pagination-1160", draft);
    await clickMatching("#valuesPeriods .kpc-p-cols > button:nth-child(2)", "smoke-summary-earlier");
    await until("document.getElementById('valuesPeriodViews').innerText.includes('想先攒钱')");
    assert.equal(await page("document.getElementById('valuesPeriodViews').innerText.includes('摘要分页返回的第二条')"), false);
    assert.deepEqual(errors, []);
    await finish(); return;
  }
  assert.equal(await hostPage("smoke.starts"), 0);
  assert.equal(await page("document.querySelectorAll('[role=tab]').length"), 7);
  assert.equal(await page("document.querySelectorAll('.analysis-step').length"), 0, "no fixed analysis chain sits on the page before a run");
  assert.equal(await page("['draftProfile','tagsRun','tagsStart'].every(id=>document.getElementById(id).getBoundingClientRect().height===0)"), true);
  await capture("light-unified-ready-1160");
  await clickVisible("analysisMenuToggle");
  for (const key of ["profile", "tags", "persona", "values"]) await clickVisible("analysisOutput-" + key);
  await capture("light-unified-menu-1160");
  await until("!document.getElementById('analysisRun').disabled");
  assert.deepEqual(await hostPage("smoke.outputs"), ["library"]);
  await clickVisible("analysisRun");
  assert.equal(await page("document.getElementById('analysisMenu').hidden && !document.getElementById('analysisPanel').hidden"), true, "starting closes the menu and opens the progress overlay");
  assert.deepEqual(await page("[...document.querySelectorAll('.analysis-step .step-title')].map(e=>e.textContent)"), ["同步本人文字", "表达知识库", "本地语义索引"]);
  await hostPage("smoke.finish()"); await until("document.getElementById('analysisRun').textContent==='更新分析'");
  assert.equal(await page("document.querySelectorAll('.analysis-step.error').length"), 0);
  await capture("light-unified-library-only-1160");
  assert.equal(await page("document.getElementById('analysisPanel').hidden"), true, "a clean finish closes the overlay");
  await clickVisible("analysisMenuToggle");
  for (const key of ["profile", "tags", "persona", "values"]) await clickVisible("analysisOutput-" + key);
  await page("document.getElementById('background').value='未保存的手写背景';document.getElementById('background').dispatchEvent(new Event('input'))");
  await clickVisible("analysisRun"); await until("!document.getElementById('analysisPause').hidden");
  await capture("light-unified-running-1160");
  await hostPage("smoke.finish()"); await until("document.getElementById('analysisRun').textContent==='继续分析'");
  assert.equal(await page("document.querySelectorAll('.analysis-step.error').length"), 5);
  assert.equal(await page("document.getElementById('search').disabled"), false, "missing model does not disable existing local samples");
  await hostPage("smoke.modelConfigured=true;smoke.notify()");
  await clickVisible("analysisRun");
  await until("document.getElementById('analysisProgress').getAttribute('aria-label')==='提取共同证据进度'");
  assert.equal(await page("document.getElementById('analysisProgress').max"), 120);
  assert.equal(await page("document.getElementById('analysisProgress').value"), 40);
  assert.equal(await page("document.getElementById('analysisMessage').textContent.endsWith(' · 约还需 3分–7分')"), true);
  assert.equal(await page("[...document.querySelectorAll('.analysis-outputs input')].every(input=>input.disabled)"), true);
  assert.equal(await page("!!document.getElementById('analysisStepPause-evidence')"), false);
  await clickVisible("analysisStepPause-tags"); await until("!!document.getElementById('analysisStepResume-tags')");
  assert.equal(await hostPage("smoke.run.state"), "running");
  await capture("light-unified-step-paused-1160");
  await hostPage("smoke.theme('dark')"); win.setContentSize(375, 1000); await wait(120);
  await capture("dark-unified-step-controls-375");
  await hostPage("smoke.theme('light')"); win.setContentSize(1160, 1350); await wait(120);
  await clickVisible("analysisStepResume-tags"); await until("!!document.getElementById('analysisStepPause-tags')");
  await capture("light-unified-model-progress-1160");
  await clickVisible("analysisPause"); await until("document.getElementById('analysisRun').textContent==='继续分析'");
  await clickVisible("analysisRun"); await hostPage("smoke.finish()"); await until("document.getElementById('analysisRun').textContent==='更新分析'");
  assert.equal(await page("document.getElementById('background').value"), "未保存的手写背景");
  assert.equal(await page("document.getElementById('draftPanel').hidden"), false);
  await clickVisible("tab-persona"); await until("document.querySelectorAll('#personaCharts .kpc-card').length===2");
  assert.equal(await page("document.querySelectorAll('#personaCharts .kpc-dot').length"), 2, "big5_o and big5_e; single-axis attachment has no invented coordinate");
  assert.deepEqual(await page("(()=>{const e=document.querySelector('#personaCharts .kpc-aside');return [e.textContent,e.title];})()"), ["INFP", "本人自述"], "partly known scales are not a type, so the self-report stands in");
  await capture("light-persona-charts-1160");
  await page("(()=>{const e=document.querySelector('#personaCharts .kpc-card');e.id='smoke-big5';return e.id;})()");
  await clickVisible("smoke-big5"); await until("document.querySelectorAll('#personaList .persona-card').length===6");
  assert.equal(await page("document.querySelectorAll('#personaList img').length"), 0);
  assert.equal(await page("document.getElementById('personaList').innerText.includes('反例 1')"), true);
  await capture("light-unified-persona-1160");
  await page("document.getElementById('persona-big5_e').querySelector('details').open=true");
  await capture("light-persona-evidence-1160");
  await clickMatching("#persona-evidence-big5_e-inference-support .evidence-more", "smoke-persona-more");
  await until("document.querySelectorAll('#persona-evidence-big5_e-inference-support .sample').length===2");
  assert.equal(await page("document.getElementById('persona-evidence-big5_e-inference-support').innerText.includes('同维度相关记录，未被本段结论直接引用')"), true);
  assert.equal(await page("document.querySelectorAll('#persona-evidence-big5_e-inference-support .evidence-more').length"), 0);
  assert.deepEqual(await hostPage("smoke.calls.find(c=>c.method==='self.personaEvidence').params"), { key: "big5_e", kind: "inference", side: "evidence", offset: 1, limit: 50, expectedVersion: 1 });
  await capture("light-persona-evidence-expanded-1160");
  await clickVisible("persona-edit-big5_e"); await until("document.getElementById('persona-manual-big5_e')");
  await capture("light-persona-editing-1160");
  await page("document.getElementById('persona-big5_e').querySelector('details').open=false;document.getElementById('persona-manual-big5_e').value='我自己的理解仍保留弹性';document.getElementById('persona-manual-big5_e').dispatchEvent(new Event('input'));document.getElementById('persona-manual-big5_e').focus();document.getElementById('persona-manual-big5_e').setSelectionRange(3,3)");
  await hostPage("smoke.notify()"); await wait(150); await until("document.getElementById('persona-manual-big5_e').value==='我自己的理解仍保留弹性'");
  assert.equal(await page("document.activeElement.id"), "persona-manual-big5_e", "result refresh preserves the manual editor focus");
  assert.equal(await page("document.getElementById('persona-manual-big5_e').selectionStart"), 3);
  const saveId = await page("(()=>{const e=document.getElementById('persona-big5_e').querySelector('.persona-manual button');e.id='smoke-save-persona';e.scrollIntoView({block:'center'});return e.id;})()");
  await clickVisible(saveId); await until("document.getElementById('persona-big5_e').innerText.includes('我的理解：我自己的理解仍保留弹性')");
  assert.equal(await hostPage("smoke.manual.big5_e"), "我自己的理解仍保留弹性");
  await clickVisible("tab-values"); await until("document.querySelectorAll('#valuesCharts .kpc-card').length===2 && document.querySelectorAll('#valuesList .persona-card').length===5");
  await clickMatching("#valuesHistory > summary", "smoke-values-history");
  assert.equal(await page("document.getElementById('valuesList').innerText.includes('长期、稳定')"), true);
  await capture("light-unified-values-1160");
  await clickVisible("tab-persona");
  await hostPage("smoke.theme('dark')"); win.setContentSize(375, 1000); await wait(120);
  await capture("dark-unified-persona-375");
  await capture("dark-persona-mbti-375", "persona-mbti");
  await clickVisible("tab-values"); await wait(120);
  await capture("dark-unified-values-375");
  await clickVisible("analysisStatus");
  await page("document.getElementById('judgeShadowPanel').open=true");
  await clickVisible("judgeShadowRefresh");
  await until("document.getElementById('judgeShadowSummary').textContent.includes('漏判 1 / 4')");
  assert.ok(await page("document.getElementById('judgeShadowSummary').textContent.includes('尚未启用跳过')"));
  await capture("dark-judge-shadow-375");
  await clickVisible("analysisPanelClose");
  await hostPage("smoke.periodsVisible=true;smoke.notify()");
  await clickVisible("tab-persona");
  await until("document.querySelectorAll('#personaPeriods .kpc-p-col').length===2");
  await page("document.getElementById('tabContent').scrollTop=0");
  await clickVisible("analysisMenuToggle");
  const menuHits = await page("[...document.querySelectorAll('#analysisMenu label, #analysisMenu input, #analysisDepthSettings')].map(e=>{const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);return {control:e.id||e.textContent.trim(),hit:hit?.id||hit?.className||hit?.tagName,inMenu:!!hit?.closest('#analysisMenu')};})");
  assert.ok(menuHits.every(hit => hit.inMenu), "menu controls must receive pointer hits above the period chart: " + JSON.stringify(menuHits));
  await clickVisible("analysisMenuToggle");
  await hostPage("smoke.worldviewVisible=true;smoke.theme('light')"); win.setContentSize(1160, 1350); await wait(120);
  await clickVisible("tab-values");
  await until("document.querySelectorAll('#valuesPeriods .kpc-p-col').length===3");
  assert.equal(await page("document.getElementById('valuesPeriodContext').innerText.includes('每 6 个月') && document.getElementById('valuesPeriodContext').innerText.includes('2 / 3')"), true);
  assert.equal(await page("document.getElementById('valuesPeriodViews').innerText.includes('愿意花在体验上')"), true);
  // The full-history section was opened earlier; close it through its summary so the capture shows the per-period result.
  if (await page("document.getElementById('valuesHistory').open")) await clickMatching("#valuesHistory > summary", "smoke-values-history");
  await capture("light-worldview-timeline-1160");
  await clickMatching("#valuesPeriods .kpc-p-cols > button:nth-child(2)", "smoke-worldview-earlier");
  await until("document.getElementById('valuesPeriodViews').innerText.includes('想先攒钱')");
  assert.equal(await page("document.getElementById('valuesChartContext').innerText.includes('2026 上半年')"), true);
  await clickMatching("#valuesCharts .kpc-card", "smoke-period-politics");
  await until("document.querySelector('#valuesChartList > .persona-card')?.innerText.includes('2026 上半年')");
  assert.deepEqual(await page("[...document.querySelectorAll('#valuesChartList > .persona-card > h2')].map(e=>e.textContent)"), ["经济立场", "自由—威权"], "period cards expose only supported questionnaire axes");
  assert.equal(await page("document.querySelector('#valuesChartList > .persona-card').innerText.includes('关心公共服务的平等可及')"), false, "the selected period cannot show an all-history conclusion as its own");
  await clickMatching("#valuesPeriods .kpc-p-cols > button:nth-child(3)", "smoke-worldview-latest");
  await clickMatching("#valuesPeriods .kpc-p-name", "smoke-worldview-questions");
  await until("document.querySelectorAll('#valuesPeriodDetail .persona-card').length===1");
  assert.equal(await page("document.getElementById('valuesPeriodDetail').innerText.includes('第一轮：关注公共服务覆盖。') && document.getElementById('valuesPeriodDetail').innerText.includes('第二轮：也考虑资源分配限制。')"), true);
  await clickMatching("#valuesPeriodViews > .persona-card:nth-child(3) .persona-edit", "smoke-worldview-edit-money");
  await until("!!document.getElementById('period-draft-worldview-2026H2-view:money')");
  await page("(()=>{const e=document.getElementById('period-draft-worldview-2026H2-view:money');e.value='分页时仍要保留的本期三观修正';e.dispatchEvent(new Event('input'));e.focus();e.setSelectionRange(6,6);})()");
  await clickMatching("#valuesPeriodDetail .persona-evidence summary", "smoke-worldview-evidence");
  await clickMatching("#valuesPeriodDetail .evidence-more", "smoke-worldview-more");
  await until("document.querySelectorAll('#valuesPeriodDetail .sample').length===3");
  assert.equal(await page("document.getElementById('period-draft-worldview-2026H2-view:money').value"), "分页时仍要保留的本期三观修正");
  assert.equal(await page("document.activeElement.id"), "period-draft-worldview-2026H2-view:money");
  assert.equal(await page("document.getElementById('period-draft-worldview-2026H2-view:money').selectionStart"), 6);
  assert.equal(await page("document.getElementById('valuesPeriodDetail').innerText.includes('未计入')"), true);
  assert.equal(await page("document.querySelectorAll('#valuesPeriodDetail .evidence-more').length"), 0);
  assert.deepEqual(await hostPage("smoke.calls.find(c=>c.method==='self.worldviewEvidence').params"), { period: "2026H2", item: 1001, offset: 1, limit: 50, expectedVersion: "synthetic-period-v1" });
  await capture("light-worldview-draft-during-pagination-1160", "period-draft-worldview-2026H2-view:money");
  await page("(()=>{const e=document.getElementById('period-draft-worldview-2026H2-view:money').closest('.attachment-picker').querySelector('.actions button:last-child');e.id='smoke-worldview-cancel';e.scrollIntoView({block:'center'});})()");
  await clickVisible("smoke-worldview-cancel");
  await capture("light-worldview-evidence-expanded-1160", "valuesPeriodDetail");
  await clickMatching("#valuesPeriods .kpc-p-cols > button:first-child", "smoke-worldview-insufficient");
  await until("document.getElementById('valuesPeriodContext').innerText.includes('2025 下半年')");
  assert.equal(await page("document.getElementById('valuesPeriodViews').innerText.includes('本期证据不足') && !document.getElementById('valuesPeriodViews').innerText.includes('愿意花在体验上')"), true);
  await capture("light-worldview-insufficient-1160");
  await clickMatching("#valuesPeriods .kpc-p-cols > button:nth-child(3)", "smoke-worldview-final");
  await hostPage("smoke.theme('dark')"); win.setContentSize(375, 1000); await wait(120);
  await capture("dark-worldview-timeline-375");
  const calls = await hostPage("smoke.calls");
  for (const method of ["self.analyze", "self.analysisStatus", "self.analysisSettings", "self.analysisPause", "self.analysisResume", "self.status", "self.draftStatus", "self.tags", "self.persona", "self.personaEdit", "self.values", "self.personaEvidence", "self.worldview", "self.worldviewDetail", "self.worldviewEvidence"]) assert.ok(calls.some(call => call.method === method), method);
  assert.ok(calls.some(call => call.method === "self.analysisPause" && call.params.output === "tags"));
  assert.ok(calls.some(call => call.method === "self.analysisResume" && call.params.output === "tags"));
  assert.ok(calls.filter(call => call.method === "self.analyze").every(call => call.params.outputs.length > 0));
  assert.equal(calls.some(call => ["self.build", "self.pause", "self.resume", "self.draft", "self.draftPause", "self.tagsAnalyze", "self.tagsPause"].includes(call.method)), false);
  assert.equal(await page("!!window.hacked"), false); assert.deepEqual(errors, []);
  await finish();
}).catch(error => { void finish(error); });
