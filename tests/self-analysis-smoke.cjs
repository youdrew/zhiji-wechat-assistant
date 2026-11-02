"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Hidden Electron, production sandbox frame/bridge, synthetic accounts and RPC only.
// Run with node_modules/electron/dist/electron.exe tests/self-analysis-smoke.cjs.
const { app, BrowserWindow } = require("electron");
const assert = require("node:assert/strict");
const http = require("node:http");
const { mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, ".."), output = path.join(root, ".local/tmp/self-analysis-smoke");
mkdirSync(output, { recursive: true });
app.setPath("userData", path.join(output, "electron-profile"));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.commandLine.appendSwitch("enable-features", "IsolateSandboxedIframes:grouping/per-document");
const hostSource = `"use strict";
window.smoke = { calls: [], hostCalls: [], profile: { background: '这是合成账号的自述，用于界面验收。', style: '拒绝时简短说明，关心近况时保留对方的选择。', version: 1 },
  context: { account: 'synthetic-account-a', user: '', name: '合成账号', surface: 'self-analysis-window', theme: 'light' },
  build: { state: 'idle', phase: 'done', processedConversations: 0, totalConversations: 0, messageCount: 0, sampleCount: 0, failedCount: 0 },
  modelReady: null, modelConfigured: false, conflict: false, samples: 0, indexedSamples: 0, complete: false, failed: 0, sourceReady: true, excluded: [], searchMode: 'lexical', semanticPartial: false,
  embedding: { state: 'missing', ready: false, indexState: 'idle', modelId: 'synthetic-retrieval-model', revision: 'synthetic', dimensions: 512, totalBytes: 100663296, downloadedBytes: 0, stopping: false } };
window.smoke.examples = [
  { id: 'sample-a', user: 'source-a', name: '合成好友甲', timeMs: 1791849600000, input: '这周末要不要一起吃饭？', reply: '这周末有点安排，这次先不去啦，下次再约～' },
  { id: 'sample-b', user: 'source-b', name: '合成好友乙', timeMs: 1791763200000, input: '最近还好吗？<img src=x onerror="window.hacked=true">', reply: '还不错呀，最近在忙点自己的事。你呢？' }
];
window.smoke.tagRun = { state: 'idle' }; window.smoke.tagEdits = {}; window.smoke.analysisDepth = 'standard'; window.smoke.draftRun = null;
window.smoke.analysis = { state: 'idle', steps: [] }; window.smoke.nextStep = 'tags';
function analysisState() {
  const s=window.smoke;
  if(s.pauseRequested&&!s.embedding.stopping){s.analysis={...s.analysis,state:'paused',stopping:false};s.pauseRequested=false;}
  return {...s.analysis,steps:(s.analysis.steps||[]).map(step=>({...step}))};
}
function startAnalysis(params) {
  const s=window.smoke, phase=s.nextStep;
  const titles={tags:'标签',library:'表达知识库',profile:'本人资料',semantic:'本地语义索引'};
  s.pauseRequested=false;
  if(phase==='tags') s.tagRun={state:'running',phase:'extracting',done:40,total:120};
  if(phase==='library'||phase==='semantic') {
    if(phase==='library') {s.build={...s.build,state:'running',phase:'indexing',processedConversations:2,totalConversations:12,messageCount:240,sampleCount:18};s.samples=18;s.complete=false;}
    s.embedding={...s.embedding,state:s.embedding.ready?'ready':'downloading',downloadedBytes:33554432,
      indexState:s.embedding.ready?'indexing':'waiting',error:'',indexError:'',stopping:false};
  }
  if(phase==='profile') {
    s.draftPolls=0;
    if(s.draftRun?.state==='cancelled'&&s.draftRun.depth===params.depth) s.draftRun={...s.draftRun,state:'running'};
    else s.draftRun={id:'draft-1',state:'running',phase:'reading',depth:params.depth,done:0,total:18,batchesDone:0,
      coverage:{conversations:13,readConversations:4,failedConversations:0,messagesRead:80,ownMessages:7,privateMessages:5,groupMessages:2,analyzedMessages:0,analyzedParts:0,totalParts:0,catalogComplete:false,directoryPartial:false,gaps:[]}};
  }
  s.analysis={id:'unified-'+phase,state:'running',phase,depth:params.depth,steps:[{key:phase,title:titles[phase],state:'running',
    ...(phase==='tags'?{done:40,total:120,unit:'条'}:phase==='profile'?{done:8,total:18,unit:'条'}:{})}]};
  return analysisState();
}
window.smoke.tagList = [
  ['篮球', '运动健身', 'strong', 9, 4, 'interest'], ['跑步', '运动健身', 'possible', 1, 1, 'occasional'], ['象棋', '游戏棋牌', 'strong', 6, 3, 'interest'], ['战略类游戏', '游戏棋牌', 'strong', 4, 2, 'interest'],
  ['科幻电影', '影音娱乐', 'possible', 2, 1, 'mention'], ['不吃香菜', '美食饮食', 'strong', 3, 3, 'trait'], ['前端开发', '工作技能', 'strong', 12, 5, 'trait'], ['夜猫子', '性格习惯', 'possible', 2, 2, 'uncertain']
].map(([name, category, level, mentions, chats, relation]) => ({ key: name, name, category, level, relation, manual: false, mentions, chats, days: mentions, firstMs: 1760000000000, lastMs: 1791849600000 }));
function tagState() { const s = window.smoke, done = s.tagRun.state === 'complete';
  const tags = done ? [...s.tagList, ...Object.values(s.tagEdits).filter(edit => edit.manual)].filter(tag => s.tagEdits[tag.key]?.state !== 'deleted')
    .map(tag => ({ ...tag, ...(s.tagEdits[tag.key]?.state === 'confirmed' ? { level: 'confirmed' } : {}) })) : [];
  return { account: s.context.account, run: { ...s.tagRun }, counts: { conversations: 12, groups: 3, messages: 240, pendingMessages: 0 }, tags,
    categories: ['运动健身', '游戏棋牌', '影音娱乐', '阅读学习', '美食饮食', '旅行出行', '工作技能', '身份生活', '性格习惯', '观点态度', '其他'] }; }
function status() { const s = window.smoke; return { account: s.context.account,
  source: { ready: s.sourceReady, message: s.sourceReady ? '合成聊天来源已连接' : '尚未连接聊天来源' },
  build: { ...s.build }, coverage: { complete: s.complete, catalogComplete: s.build.phase !== 'discovering', sampleCount: s.samples,
    messageCount: s.build.messageCount, discoveredConversations: s.build.totalConversations, indexedConversations: s.build.processedConversations, failedConversations: s.failed },
  model: { ready: s.modelReady, message: s.modelReady === true ? '合成回复模型已准备' : '回复模型在核心模型设置中管理；历史检索无需回复模型。' },
  embedding: { ...s.embedding }, retrieval: { ready: s.samples > 0, mode: s.embedding.ready && s.indexedSamples > 0 ? 'hybrid' : 'lexical',
    semanticReady: s.embedding.ready && s.indexedSamples > 0, indexedSamples: s.indexedSamples, pendingSamples: Math.max(0, s.samples-s.indexedSamples), totalSamples: s.samples } }; }
function rpc(plugin, method, params, scope) {
  const s = window.smoke; s.calls.push({ plugin, method, params, scope });
  if (plugin !== 'kyf.self-analysis' || scope.account !== s.context.account) throw new Error('合成账号范围错误');
  if (method === 'self.analyze') return startAnalysis(params);
  if (method === 'self.analysisStatus') return analysisState();
  if (method === 'self.analysisPause') {
    if(s.build.state==='running') s.build.state='paused';
    if(s.draftRun?.state==='running') s.draftRun={...s.draftRun,state:'cancelled'};
    s.embedding.stopping=s.embedding.ready;s.embedding.indexState='paused';s.pauseRequested=s.embedding.stopping;
    s.analysis={...s.analysis,state:s.pauseRequested?'running':'paused',stopping:s.pauseRequested};return analysisState();
  }
  if (method === 'self.persona') return {version:1,dimensions:[],coverage:{}};
  if (method === 'self.reset') { s.samples = 0; s.indexedSamples=0; s.complete = false; s.failed = 0; s.excluded = []; s.embedding.indexState='idle'; s.embedding.stopping=false; s.build = { state: 'idle', phase: 'done', processedConversations: 0, totalConversations: 0, messageCount: 0, sampleCount: 0, failedCount: 0 }; }
  if (method === 'self.search') return { available: true, mode: s.searchMode, semanticPartial:s.semanticPartial,
    sources: s.examples.filter(item => !s.excluded.includes(item.id)).map((item,index)=>({...item,matchKinds:s.searchMode==='hybrid'?(index?['lexical','semantic']:['semantic']):['lexical']})) };
  if (method === 'self.exclude') { s.excluded = s.excluded.filter(id => id !== params.id); if (params.excluded) s.excluded.push(params.id); }
  if (method === 'self.exclusions') return { sources: s.examples.filter(item => s.excluded.includes(item.id)) };
  if (method === 'self.reply') { s.replyPolls = 0; s.replyTurns = params.messages.length; return { id: 'reply-' + s.replyTurns, state: 'running', phase: 'reading' }; }
  if (method === 'self.replyStatus') return ++s.replyPolls < 2 ? { id: 'reply-' + s.replyTurns, state: 'running', phase: 'writing' } :
    { id: 'reply-' + s.replyTurns, state: 'done', result: { messages: s.replyTurns > 1 ? ['可以呀', '十二点老地方见'] : ['这周末有点安排', '下周可以吗～'] },
      sources: s.examples.filter(item => !s.excluded.includes(item.id)).map(item => ({ ...item, matchKinds: ['lexical'] })) };
  if (method === 'self.tags') return tagState();
  if (method === 'self.tagEvidence') return { name: params.name, total: 2, items: [
    { id: 'g/1', chat: '合成篮球群', isGroup: true, timeMs: 1791849600000, context: '群友甲：周六谁来打球？', text: '我来，' + params.name + '我每周都打', followup: '我：确实喜欢，打完挺开心', relation: 'interest', quote: '我每周都打', reason: '本人自述持续参与，并在后文表达喜欢', truncated: false, explicit: true },
    { id: 'p/2', chat: '合成好友甲', isGroup: false, timeMs: 1760000000000, context: '对方：最近在忙啥', text: '下班去打了会球<img src=x onerror="window.hacked=true">', followup: '', relation: 'occasional', quote: '下班去打了会球', reason: '只有单次参与，无法判断兴趣', truncated: false, explicit: false }] };
  if (method === 'self.tagEdit') { const key = params.name, tag = s.tagList.find(item => item.key === key);
    if (params.action === 'reset') delete s.tagEdits[key];
    else s.tagEdits[key] = params.action === 'add' ? { key, name: params.name, category: params.category, level: 'confirmed', manual: true, mentions: 0, chats: 0, days: 0, firstMs: null, lastMs: null, state: 'confirmed' }
      : { ...(tag || {}), state: params.action === 'delete' ? 'deleted' : 'confirmed' };
    return tagState(); }
  if (method === 'self.analysisSettings') return {outputs:['profile','tags','persona','values','library'],depth:s.analysisDepth};
  if (method === 'self.draftStatus') {
    if(!s.draftRun) return {state:'idle'};
    if(s.draftRun.state!=='running') return s.draftRun;
    const poll=++s.draftPolls, done=poll===1?8:18;
    s.draftRun={...s.draftRun,phase:poll===1?'reviewing':poll===2?'writing':'done',state:poll<3?'running':'done',done,total:18,batchesDone:poll===1?1:3,
      coverage:{conversations:13,readConversations:12,failedConversations:1,messagesRead:240,ownMessages:18,privateMessages:14,groupMessages:4,analyzedMessages:done,analyzedParts:poll===1?9:21,totalParts:21,catalogComplete:true,directoryPartial:false,unreadMessages:2,excludedMessages:8,gaps:[{kind:'unreadable-conversations',count:1},{kind:'unread-messages',count:2}]},
      ...(poll>=3?{result:{background:'我在合成城市工作，周末喜欢和朋友吃饭。',style:'回复偏短，常用“好呀”，拒绝时先说明安排再约下次。'}}:{})};
    if(poll>=3) s.analysis={...s.analysis,state:'done',steps:s.analysis.steps.map(step=>({...step,state:'done'}))};
    return s.draftRun;
  }
  if(['self.status','self.reset','self.exclude'].includes(method)) return status();
  throw new Error('Unexpected synthetic method: '+method);
}
const host = {
  'model.status'() { const s=window.smoke; s.hostCalls.push({method:'model.status',params:{}}); if(s.modelConfigured==='error') throw new Error('synthetic-private-model-error'); return {configured:s.modelConfigured}; },
  'self-profile.read'(params) { window.smoke.hostCalls.push({ method: 'self-profile.read', params }); return { ...window.smoke.profile }; },
  'self-profile.save'(params) {
    const s = window.smoke; s.hostCalls.push({ method: 'self-profile.save', params });
    if (s.conflict) { s.conflict = false; s.profile = { background: '另一窗口更新的合成背景', style: '新的合成偏好', version: s.profile.version + 1 };
      throw Object.assign(new Error('自身资料已更新'), { code: 'request-conflict' }); }
    if (params.account !== s.context.account || params.expectedVersion !== s.profile.version) throw new Error('合成版本或账号不匹配');
    s.profile = { background: params.background, style: params.style, version: s.profile.version + 1 }; return { ...s.profile };
  },
  'settings.open'(params) { window.smoke.hostCalls.push({ method: 'settings.open', params }); return { ok: true }; },
  'view.focusChat'(params) { window.smoke.hostCalls.push({ method: 'view.focusChat', params }); return { ok: true }; }
};
const frame = KyfFrames.create({ container: document.getElementById('page'), plugin: 'kyf.self-analysis', page: 'ui/index.html', context: window.smoke.context, host, rpc });
window.smoke.notify = () => frame.event({ type: 'shown' });
window.smoke.finishAnalysis = () => {window.smoke.analysis={...window.smoke.analysis,state:'done',steps:window.smoke.analysis.steps.map(step=>({...step,state:'done'}))};window.smoke.notify();};
window.smoke.setContext = next => { if(next.account!==undefined&&next.account!==window.smoke.context.account) {window.smoke.draftRun=null;window.smoke.analysis={state:'idle',steps:[]};} window.smoke.context = { ...window.smoke.context, ...next }; frame.post(window.smoke.context); };
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
const errors = [], captures = [], geometry = [];
let win;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const hostPage = code => win.webContents.executeJavaScript(code);
async function page(code) {
  for (let attempt = 0; attempt < 40; attempt++) {
    const frame = win.webContents.mainFrame.frames[0];
    if (frame) try { return await frame.executeJavaScript(code); } catch { }
    await wait(70);
  }
  throw new Error("Synthetic self-analysis frame did not load");
}
async function until(code) {
  for (let attempt = 0; attempt < 60; attempt++) { if (await page(code)) return; await wait(60); }
  throw new Error("Timed out: " + code + "\n" + await page("document.body.innerText"));
}
async function clickVisible(id) {
  const control = await page(`(()=>{const e=document.getElementById(${JSON.stringify(id)});if(!e)return null;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {width:r.width,height:r.height,disabled:e.disabled,display:s.display,visibility:s.visibility};})()`);
  assert.ok(control && control.width > 0 && control.height > 0 && !control.disabled && control.display !== 'none' && control.visibility !== 'hidden', id + ' must be visible and enabled');
  await page(`document.getElementById(${JSON.stringify(id)}).click()`);
}
async function untilHost(code) {
  for (let attempt = 0; attempt < 60; attempt++) { if (await hostPage(code)) return; await wait(60); }
  throw new Error("Timed out waiting for synthetic host: " + code);
}
async function capture(name, scrollId = null) {
  await page(scrollId ? `document.getElementById(${JSON.stringify(scrollId)}).scrollIntoView({block:'start'})` : "document.getElementById('tabContent').scrollTop=0");
  await wait(100);
  const size = await page("({width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth})");
  assert.ok(size.scrollWidth <= size.width, name + ": horizontal page overflow " + JSON.stringify(size));
  // The six tabs intentionally scroll on narrow screens; their scroller, rather
  // than each offscreen tab, must stay inside the page viewport.
  const tabStrip = await page("(()=>{const e=document.querySelector('.tabs'),r=e.getBoundingClientRect();return {left:r.left,right:r.right,overflow:getComputedStyle(e).overflowX};})()");
  assert.ok(tabStrip.left >= -1 && tabStrip.right <= size.width + 1 && tabStrip.overflow === 'auto', name + ': tab scroller escaped viewport');
  const controls = await page(`Array.from(document.querySelectorAll('button,input,textarea,select')).filter(e=>{const r=e.getBoundingClientRect();return !e.closest('.tabs')&&r.width&&r.height}).map(e=>{const r=e.getBoundingClientRect();return {id:e.id,left:r.left,right:r.right,width:r.width}})`);
  for (const item of controls) assert.ok(item.left >= -1 && item.right <= size.width + 1, name + ": control escaped viewport " + JSON.stringify(item));
  geometry.push({ name, ...size });
  const options = { stayHidden: true, stayAwake: true };
  await win.webContents.capturePage(undefined, options); await wait(100);
  const image = await win.webContents.capturePage(undefined, options), colors = new Set(), bitmap = image.toBitmap();
  for (let index = 0; index < bitmap.length; index += 8) colors.add(bitmap.readUInt32LE(index));
  assert.ok(colors.size > 60, name + ": image was not painted");
  const bars = await page(`Array.from(document.querySelectorAll('progress')).flatMap(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&r.top>=0&&r.bottom<=innerHeight&&e.position>0?[{id:e.id,x:Math.round(r.left+Math.max(3,r.width*e.position/2)),y:Math.round(r.top+r.height/2),position:e.position,accent:getComputedStyle(e).getPropertyValue('--accent').trim()}]:[]})`);
  const imageWidth = image.getSize().width;
  for (const bar of bars) {
    const offset = (bar.y * imageWidth + bar.x) * 4, rgb = [bitmap[offset + 2], bitmap[offset + 1], bitmap[offset]];
    const expected = bar.accent.match(/^#([a-f0-9]{6})$/i)?.[1];
    assert.ok(expected, name + ": progress has no accent color");
    const target = [0, 2, 4].map(index => parseInt(expected.slice(index, index + 2), 16));
    assert.ok(rgb.every((value,index)=>Math.abs(value-target[index])<8), name + ": progress fill was not painted " + JSON.stringify({bar,rgb,target}));
  }
  assert.equal(win.isVisible(), false);
  const filename = path.join(output, name + ".png"); writeFileSync(filename, image.toPNG()); captures.push(filename);
}
async function finish(error) {
  const result = { ok: !error, errors, geometry, captures, ...(error ? { error: error.stack || String(error) } : {}) };
  writeFileSync(path.join(output, "result.json"), JSON.stringify(result, null, 2));
  try { process.stdout.write(JSON.stringify({ ok: result.ok, captures: captures.length, output, ...(error ? { error: String(error) } : {}) }) + "\n"); } catch { }
  win?.destroy(); server.close(); app.exit(error ? 1 : 0);
}
app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  win = new BrowserWindow({ width: 1140, height: 900, show: false, useContentSize: true,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false } });
  win.webContents.on("console-message", details => { if (details.level === "error" && !details.message.includes("favicon")) errors.push(details.message); });
  await win.loadURL("http://127.0.0.1:" + server.address().port + "/");
  await until("!document.getElementById('background').disabled && !document.getElementById('analysisRun').disabled");
  assert.equal(await hostPage("window.smoke.calls.some(c=>c.method==='self.analyze')"), false);
  assert.equal(await page("['draftProfile','tagsRun','tagsStart'].every(id=>document.getElementById(id).getBoundingClientRect().height===0)"), true);
  assert.equal(await page("document.querySelectorAll('[role=tabpanel]:not([hidden])').length"), 1);
  assert.equal(await page("!!document.querySelector('.readiness,#sourceState,#libraryState,#modelState,#refresh')"), false, "readiness badges stay off the main page");
  await capture("light-profile-1140");
  // The depth lives in the plugin settings; the menu shows it and opens them.
  await until("document.getElementById('analysisDepthSettings').textContent==='标准'");
  assert.equal(await page("!!document.getElementById('draftDepth')"), false, "no depth selector on the page");
  await page("document.getElementById('analysisDepthSettings').click()");
  await untilHost("smoke.hostCalls.some(call=>call.method==='settings.open'&&call.params.section==='plugin')");
  await page("document.getElementById('background').value='切换栏目保留的合成草稿';document.getElementById('background').dispatchEvent(new Event('input'))");
  await page("document.getElementById('tab-profile').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))");
  await until("!document.getElementById('panel-tags').hidden");
  assert.equal(await page("document.activeElement.id"), "tab-tags");
  await page("document.getElementById('tab-tags').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}))");
  assert.equal(await page("document.getElementById('background').value"), "切换栏目保留的合成草稿");
  await page("document.getElementById('tab-tags').click()");
  await until("!document.getElementById('tagsEmpty').hidden && !document.getElementById('analysisRun').disabled");
  await capture("light-tags-empty-1140");
  await clickVisible('analysisRun');
  await until("document.getElementById('tagsMessage').textContent==='正在提取标签 40 / 120'");
  await capture("light-tags-running-1140");
  await hostPage("smoke.tagRun={state:'complete'};smoke.finishAnalysis()");
  await until("document.querySelectorAll('#tagList .chip').length===8");
  assert.equal(await page("[...document.querySelectorAll('#tagList .chip')].find(e=>e.firstChild.textContent==='篮球').querySelector('.chip-relation').textContent"), '明确兴趣');
  assert.equal(await page("[...document.querySelectorAll('#tagList .chip')].find(e=>e.firstChild.textContent==='跑步').querySelector('.chip-relation').textContent"), '偶尔参与');
  await page("[...document.querySelectorAll('#tagList .chip')].find(e=>e.firstChild.textContent==='篮球').click()");
  await until("document.querySelectorAll('#tagEvidence .sample').length===2");
  assert.equal(await page("!!window.hacked || document.querySelectorAll('#tagEvidence img').length>0"), false);
  await page("document.getElementById('confirmTag').click()");
  await until("document.getElementById('confirmTag').textContent==='取消确认'");
  await capture("light-tags-1140");
  await page("[...document.querySelectorAll('#tagList .chip')].find(e=>e.firstChild.textContent==='跑步').click()");
  await until("document.getElementById('tagTitle').textContent==='跑步'");
  await page("document.getElementById('deleteTag').click()");
  await until("!document.getElementById('tagUndo').hidden && document.querySelectorAll('#tagList .chip').length===7");
  await capture("light-tags-deleted-1140");
  await page("document.getElementById('undoTag').click()");
  await until("document.getElementById('tagTitle').textContent==='跑步' && document.querySelectorAll('#tagList .chip').length===8");
  await page("document.getElementById('addTag').click();document.getElementById('tagName').value='养猫';document.getElementById('tagCategory').value='身份生活';document.getElementById('saveTag').click()");
  await until("document.getElementById('tagTitle').textContent==='养猫' && document.getElementById('tagStats').textContent==='身份生活 · 已确认 · 手动添加'");
  await capture("light-tags-added-1140");
  await page("document.getElementById('tab-build').click()");
  await capture("light-build-empty-1140");
  await hostPage("smoke.nextStep='library'");
  await clickVisible('analysisRun');
  await until("document.getElementById('sampleCount').textContent==='18'");
  assert.equal(await page("document.getElementById('downloadProgress').value"), 33554432);
  assert.equal(await page("document.getElementById('downloadProgress').max"), 100663296);
  await capture("light-building-1140");
  await clickVisible('analysisPause');
  await until("document.getElementById('buildState').textContent==='已暂停'");
  await until("document.getElementById('analysisRun').textContent==='继续分析' && !document.getElementById('analysisRun').disabled");
  assert.match(await page("document.getElementById('buildMessage').textContent"), /进度已保留/);
  await capture("light-paused-build-1140");
  await clickVisible('analysisRun');
  await until("document.getElementById('buildState').textContent==='正在建立'");
  await clickVisible('analysisPause');
  await until("document.getElementById('analysisRun').textContent==='继续分析' && !document.getElementById('analysisRun').disabled");
  await clickVisible('analysisRun');
  await until("document.getElementById('buildState').textContent==='正在建立'");
  await hostPage("smoke.modelConfigured=true;smoke.build.state='complete';smoke.build.processedConversations=12;smoke.complete=true;smoke.notify()");
  await until("document.getElementById('buildState').textContent==='正在完善'");
  await capture("light-download-1140");
  await hostPage("smoke.embedding.state='ready';smoke.embedding.ready=true;smoke.embedding.indexState='indexing';smoke.indexedSamples=6;smoke.notify()");
  await until("document.getElementById('semanticState').textContent==='正在理解样本'");
  assert.equal(await page("document.getElementById('semanticProgress').value"), 6);
  await capture("light-indexing-1140");
  await hostPage("smoke.embedding.indexState='ready';smoke.indexedSamples=18;smoke.searchMode='hybrid';smoke.semanticPartial=true;smoke.finishAnalysis()");
  await until("document.getElementById('buildState').textContent==='全部处理完'");
  await capture("light-build-complete-1140");
  await page("document.getElementById('tab-trial').click();document.getElementById('query').value='最近有点累，这周想在家歇着，不想答应聚会';document.getElementById('query').dispatchEvent(new Event('input'));document.getElementById('search').click()");
  await until("document.querySelectorAll('#samples .sample').length===2");
  assert.equal(await page("!!window.hacked || document.querySelectorAll('.sample img').length>0"), false);
  assert.equal(await page("document.getElementById('modelHint').hidden"), true);
  await capture("light-trial-search-1140");
  await page("document.getElementById('trialInput').value='周末一起吃饭吗？';document.getElementById('trialSend').click()");
  await until("document.querySelector('#chatLog .turn.me .bubble.pending')");
  await until("document.querySelectorAll('#chatLog .turn.me .bubble:not(.pending)').length===2");
  assert.equal(await page("document.getElementById('searchMessage').textContent"), "这条回复参考了 2 条历史回复");
  assert.deepEqual(await hostPage("smoke.calls.find(c=>c.method==='self.reply').params"), { messages: [{ from: "friend", text: "周末一起吃饭吗？" }] });
  await page("document.getElementById('trialInput').value='那周六中午？';document.getElementById('trialInput').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
  await until("document.querySelectorAll('#chatLog .turn.me .bubble:not(.pending)').length===4");
  assert.equal(await page("document.querySelectorAll('#chatLog .turn').length"), 4);
  await capture("light-trial-chat-1140");
  await page("document.querySelector('#samples .sample button').click()");
  await until("document.querySelectorAll('#samples .sample').length===1");
  await page("document.getElementById('tab-build').click()");
  await until("document.querySelectorAll('#excludedSamples .sample').length===1");
  await capture("light-build-exclusions-1140");
  await page("document.querySelector('#excludedSamples .sample button').click()");
  await until("document.querySelectorAll('#excludedSamples .sample').length===0 && document.getElementById('exclusionsPanel').hidden");
  await page("document.getElementById('tab-profile').click();document.getElementById('background').value='我的合成草稿';document.getElementById('background').dispatchEvent(new Event('input'))");
  await hostPage("smoke.conflict=true");
  await page("document.getElementById('saveProfile').click()");
  await until("document.getElementById('profileError').textContent.includes('草稿已保留') && !document.getElementById('reloadProfile').hidden");
  await page("document.getElementById('reloadProfile').click()");
  await until("!document.getElementById('latestProfile').hidden");
  assert.equal(await page("document.getElementById('background').value"), "我的合成草稿");
  await capture("light-conflict-1140");
  await page("document.getElementById('saveProfile').click()");
  await until("document.getElementById('saveProfile').disabled && !document.getElementById('analysisRun').disabled && document.getElementById('reloadProfile').hidden");
  await hostPage("smoke.nextStep='profile'");
  await clickVisible('analysisRun');
  await until("!document.getElementById('analysisPause').hidden && !document.getElementById('draftCoverage').hidden");
  assert.equal(await hostPage("'depth' in smoke.calls.filter(c=>c.method==='self.analyze').at(-1).params"), false, "the backend reads the saved depth");
  await clickVisible('analysisPause');
  await untilHost("smoke.pauseRequested===true");
  await hostPage("smoke.embedding.stopping=false;smoke.notify()");
  await until("document.getElementById('analysisRun').textContent==='继续分析'");
  assert.equal(await page("document.getElementById('draftCoverage').hidden"), false);
  await capture("light-draft-paused-1140", "draftCoverage");
  await clickVisible('analysisRun');
  await until("document.getElementById('profileMessage').textContent.includes('正在整理初稿') && !document.getElementById('analysisProgress').hidden");
  await capture("light-draft-progress-1140");
  await until("!document.getElementById('draftPanel').hidden");
  assert.equal(await page("document.getElementById('background').value"), "我的合成草稿", "existing text stays until replaced");
  assert.match(await page("document.getElementById('draftCoverageTitle').textContent"), /已完成可读部分/);
  assert.match(await page("document.getElementById('draftModelCoverage').textContent"), /18 \/ 18.*21 \/ 21/);
  assert.match(await page("document.getElementById('draftReadCoverage').textContent"), /私聊 14，群聊 4/);
  assert.match(await page("document.getElementById('draftGaps').textContent"), /聊天未能读取：1.*原始消息未能读取：2/);
  await capture("light-draft-full-coverage-gaps-1140", "draftCoverage");
  await capture("light-draft-preview-1140", "draftPanel");
  await page("document.getElementById('useDraftStyle').click()");
  assert.ok(await page("document.getElementById('style').value.startsWith('回复偏短')"));
  await page("document.getElementById('closeDraft').click();document.getElementById('saveProfile').click()");
  await until("document.getElementById('saveProfile').disabled && document.getElementById('draftPanel').hidden");
  assert.equal(await hostPage("smoke.profile.style.startsWith('回复偏短')"), true);
  await hostPage("smoke.setContext({theme:'dark'})");
  await page("document.getElementById('tab-trial').click()");
  await capture("dark-trial-1140");
  win.setContentSize(375, 900); await wait(150);
  await page("document.getElementById('tab-profile').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}))");
  assert.equal(await page("document.activeElement.id"), 'tab-trial');
  assert.equal(await page("(()=>{const r=document.activeElement.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;})()"), true, 'keyboard navigation scrolls the selected narrow-screen tab into view');
  await page("document.getElementById('tab-trial').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}))");
  await page("document.getElementById('tab-profile').click()");
  await capture("dark-profile-375");
  await page("document.getElementById('tab-tags').click()");
  await until("document.querySelectorAll('#tagList .chip').length===9");
  await capture("dark-tags-375");
  await page("document.getElementById('tab-trial').click()");
  await capture("dark-trial-375");
  await page("document.getElementById('tab-build').click()");
  await hostPage("smoke.embedding.state='missing';smoke.embedding.ready=false;smoke.embedding.indexState='idle';smoke.indexedSamples=0;smoke.notify()");
  await until("document.getElementById('buildState').textContent==='历史已整理' && !document.getElementById('analysisRun').disabled");
  await capture("dark-existing-library-375");
  const historyBeforeSemantic = await hostPage("JSON.stringify(smoke.build)");
  await hostPage("smoke.nextStep='semantic'");
  await clickVisible('analysisRun');
  await until("document.getElementById('semanticState').textContent==='正在下载资源'");
  assert.equal(await hostPage("'depth' in smoke.calls.filter(c=>c.method==='self.analyze').at(-1).params"), false);
  assert.equal(await hostPage("JSON.stringify(smoke.build)"), historyBeforeSemantic);
  await capture("dark-download-375");
  await hostPage("smoke.build.state='complete';smoke.complete=true;smoke.embedding.state='error';smoke.embedding.ready=false;smoke.embedding.indexState='error';smoke.embedding.error='合成资源校验失败，请稍后重试';smoke.analysis={...smoke.analysis,state:'partial'};smoke.notify()");
  await until("document.getElementById('semanticState').textContent==='准备未完成'");
  assert.equal(await page("document.getElementById('search').disabled"), false);
  await capture("dark-index-error-375");
  await until("!document.getElementById('analysisRun').disabled");
  await clickVisible('analysisRun');
  await until("document.getElementById('semanticState').textContent==='正在下载资源'");
  await hostPage("smoke.build.state='complete';smoke.complete=true;smoke.embedding={...smoke.embedding,state:'ready',ready:true,indexState:'indexing',error:''};smoke.indexedSamples=8;smoke.notify()");
  await until("document.getElementById('semanticState').textContent==='正在理解样本'");
  await clickVisible('analysisPause');
  await until("document.getElementById('semanticState').textContent==='正在暂停'");
  assert.equal(await page("document.getElementById('analysisRun').disabled"), true);
  await hostPage("smoke.embedding.stopping=false;smoke.notify()");
  await until("!document.getElementById('analysisRun').disabled");
  await capture("dark-paused-375");
  await clickVisible('analysisRun');
  await until("document.getElementById('semanticState').textContent==='正在理解样本'");
  await hostPage("smoke.embedding.indexState='ready';smoke.indexedSamples=18;smoke.finishAnalysis()");
  await until("document.getElementById('semanticState').textContent==='已可用'");
  await page("document.getElementById('tab-build').click();document.getElementById('reset').click()");
  await until("!document.getElementById('resetConfirm').hidden");
  await capture("dark-reset-375", "resetConfirm");
  await page("document.getElementById('confirmReset').click()");
  await until("document.getElementById('sampleCount').textContent==='0'");
  await page("document.getElementById('tab-profile').click();document.getElementById('background').value='账号甲未保存草稿';document.getElementById('background').dispatchEvent(new Event('input'))");
  assert.equal(await page("document.getElementById('profileMessage').textContent"), "未保存");
  await hostPage("smoke.profile={background:'账号乙合成资料',style:'',version:1};smoke.setContext({account:'synthetic-account-b',user:''})");
  await until("document.getElementById('background').value==='账号乙合成资料'");
  assert.equal(await page("document.getElementById('panel-profile').hidden"), false);
  assert.equal(await page("document.getElementById('query').value"), "");
  assert.equal(await page("document.querySelectorAll('.sample').length"), 0);
  assert.equal(await page("document.querySelectorAll('#chatLog .turn').length"), 0, "the previous account's simulated chat is dropped");
  await hostPage("smoke.setContext({account:'',user:''})");
  await until("document.getElementById('background').disabled");
  await page("document.getElementById('tab-build').click()");
  await until("!document.getElementById('sourceHint').hidden");
  await page("document.getElementById('sourceSettings').click();document.getElementById('tab-trial').click()");
  await hostPage("smoke.modelConfigured=false;smoke.notify()");
  await until("!document.getElementById('modelHint').hidden");
  await page("document.getElementById('modelSettings').click()");
  await capture("dark-no-account-375");
  await hostPage("smoke.setContext({account:'synthetic-account-b',user:''});smoke.sourceReady=false;smoke.notify()");
  await page("document.getElementById('tab-build').click()");
  await until("!document.getElementById('sourceHint').hidden");
  await capture("dark-source-unavailable-375");
  const calls = await hostPage("smoke.calls");
  for (const method of ["self.analyze","self.analysisStatus","self.analysisPause","self.search","self.reply","self.replyStatus","self.reset","self.exclude","self.exclusions","self.draftStatus","self.analysisSettings","self.tags","self.tagEvidence","self.tagEdit"]) assert.ok(calls.some(call=>call.method===method), method);
  assert.equal(calls.some(call=>["self.build","self.pause","self.resume","self.draft","self.draftPause","self.tagsAnalyze"].includes(call.method)), false, 'visible controls only use the unified analysis lifecycle');
  assert.equal(calls.some(call=>/generate|subscription|compare/.test(call.method)), false);
  assert.deepEqual((await hostPage("smoke.hostCalls")).filter(call=>call.method==='settings.open').map(call=>call.params.section), ["plugin","source","model"]);
  assert.deepEqual(errors, []);
  await finish();
}).catch(error => { void finish(error); });
