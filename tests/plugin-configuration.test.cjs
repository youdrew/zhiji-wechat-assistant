// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Declarative plugin settings: host rendering, drafts, validation and revision-safe saves.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createHash } = require('node:crypto');
const { makeDocument, settle, text, find, button } = require('./helpers/plugin-settings-dom.cjs');
const source = readFileSync(path.join(__dirname, '../chatui/plugin-configuration.js'), 'utf8');

const plugin = (id = 'dev.settings', extra = {}) => ({ id, name: id === 'dev.other' ? '另一插件' : '合成插件', version: '1.0.0', source: 'local',
  description: '合成描述', enabled: true, process: { state: 'idle' }, contributes: {}, ...extra });
const schema = { schema: 1, groups: [{ id: 'behavior', title: '运行行为', description: '仅用于合成验收', fields: [
  { key: 'active', title: '生成建议', type: 'boolean', default: true, description: '控制下次任务' },
  { key: 'style', title: '表达风格', type: 'select', default: 'brief', options: [{ value: 'brief', label: '简洁' }, { value: 'warm', label: '温和' }] },
  { key: 'count', title: '读取条数', type: 'integer', default: 30, minimum: 1, maximum: 100 },
  { key: 'note', title: '额外偏好', type: 'string', default: '', maxLength: 20 },
] }], links: [{ label: '助手仓库', nav: 'repository' }] };
const defaults = { active: true, style: 'brief', count: 30, note: '' };
const definition = createHash('sha256').update(JSON.stringify(schema)).digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
const ok = (body, status = 200) => ({ ok: status < 400, status, json: async () => clone(body) });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function fixture(options = {}) {
  const { document, ids, add } = makeDocument(), calls = [], nav = [], models = [];
  const panel = add('panelPlugins'); panel.className = 'settings-panel active';
  add('pluginOverview', panel); add('pluginDetail', panel).hidden = true; add('pluginLeaveConfirm', panel).hidden = true;
  for (const [id, label] of [['pluginLeaveStay', '继续编辑'], ['pluginLeaveDiscard', '放弃更改'], ['pluginLeaveSave', '保存并离开']]) {
    const control = add(id, ids.get('pluginLeaveConfirm'), 'button'); control.textContent = label;
  }
  const plugins = [plugin(), plugin('dev.other')];
  plugins.forEach(item => { item.contributes.settings = clone(schema); });
  const state = { reply: null, snapshots: new Map(plugins.map(item => [item.id, { plugin: item, schema: clone(schema), values: clone(defaults),
    revision: 1, definition, enabled: item.enabled, effective: 'next-task' }])) };
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, body, method: options.method || 'GET' });
    if (state.reply) { const reply = state.reply(url, body, options); if (reply) return reply; }
    if (!url.startsWith('/api/plugins/settings')) throw new Error(`Unexpected endpoint: ${url}`);
    const id = body?.id || new URL(url, 'http://localhost').searchParams.get('id');
    const current = state.snapshots.get(id);
    if (body) state.snapshots.set(id, { ...current, values: clone(body.values), revision: current.revision + 1 });
    return ok(state.snapshots.get(id));
  };
  const window = {};
  vm.runInNewContext(source, { window, document, fetch, JSON, URL, AbortController, setTimeout, clearTimeout, console });
  window.PluginConfiguration.init({ getPlugin: id => plugins.find(item => item.id === id), onModelSettings: () => models.push(true),
    onOpenNav: (item, target) => nav.push({ id: item.id, target }) });
  const ui = window.PluginConfiguration, detail = ids.get('pluginDetail');
  const field = key => find(detail, item => item.name === key && ['INPUT', 'SELECT', 'TEXTAREA'].includes(item.tagName));
  const edit = async (key, value) => { const input = field(key); assert.ok(input, key);
    if (input.type === 'checkbox') input.checked = value; else input.value = String(value);
    await input.emit('input'); await input.emit('change'); };
  const click = async label => { const target = button(detail, label); assert.ok(target, `Missing button ${label}`); await target.click(); };
  const confirm = async label => { const target = button(ids.get('pluginLeaveConfirm'), label); assert.ok(target, `Missing confirm ${label}`); await target.click(); };
  return { ui, ids, document, detail, calls, state, plugins, field, edit, click, confirm, nav, models };
}


test('opens the plugin subpage and renders grouped typed controls with accessible names', async () => {
  const f = fixture(); await f.ui.open('dev.settings');
  assert.equal(f.ids.get('pluginOverview').hidden, true); assert.equal(f.detail.hidden, false);
  assert.equal(f.detail.querySelector('.plugin-meta').textContent, 'v1.0.0 · 外部');
  assert.match(text(f.detail), /合成插件/); assert.match(text(f.detail), /运行行为/); assert.match(text(f.detail), /仅用于合成验收/);
  assert.equal(f.calls[0].url, '/api/plugins/settings?id=dev.settings'); assert.equal(f.calls[0].method, 'GET');
  assert.equal(f.field('active').type, 'checkbox'); assert.equal(f.field('active').checked, true);
  assert.equal(f.field('style').tagName, 'SELECT'); assert.equal(f.field('style').options.length, 2);
  assert.equal(f.field('count').type, 'number'); assert.equal(String(f.field('count').value), '30');
  assert.equal(f.field('note').value, ''); assert.equal(f.ui.isDirty(), false);
  assert.equal(f.ids.get('pluginConfigSave').disabled, true);
  assert.ok(f.field('count').id); assert.ok(find(f.detail, item => item.tagName === 'LABEL' && item.htmlFor === f.field('count').id));
});

test('saves one typed document with the loaded revision and can revert local edits', async () => {
  const f = fixture(); await f.ui.open('dev.settings');
  await f.edit('active', false); await f.edit('style', 'warm'); await f.edit('count', 45); await f.edit('note', '合成偏好');
  assert.equal(f.ui.isDirty(), true); assert.equal(f.ids.get('pluginConfigSave').disabled, false);
  await f.ids.get('pluginConfigSave').click();
  assert.deepEqual(f.calls.at(-1), { url: '/api/plugins/settings', method: 'POST', body: { id: 'dev.settings', revision: 1, definition,
    values: { active: false, style: 'warm', count: 45, note: '合成偏好' } } });
  assert.equal(f.ui.isDirty(), false);
  await f.edit('count', 70); await f.ids.get('pluginConfigRevert').click();
  assert.equal(String(f.field('count').value), '45'); assert.equal(f.ui.isDirty(), false);
});

test('invalid fields stop saving and show a linked field error', async () => {
  const f = fixture(); await f.ui.open('dev.settings');
  await f.edit('count', '3.5'); const before = f.calls.length; await f.ids.get('pluginConfigSave').click();
  assert.equal(f.calls.length, before); assert.equal(f.ui.isDirty(), true);
  assert.equal(f.field('count').getAttribute('aria-invalid'), 'true');
  const description = f.field('count').getAttribute('aria-describedby'); assert.ok(description);
  assert.ok(description.split(/\s+/).some(id => /整数|范围|100/.test(text(f.ids.get(id)))));
  await f.edit('count', 40); await f.ids.get('pluginConfigSave').click();
  assert.equal(f.calls.at(-1).body.values.count, 40); assert.equal(f.ui.isDirty(), false);
});

test('server field errors preserve the draft and allow correcting only that field', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('note', '合成草稿');
  f.state.reply = (_url, body) => body ? ok({ error: 'plugin-settings-invalid', message: '请检查设置', detail: { fields: { note: '这个偏好无效' } } }, 400) : null;
  await f.ids.get('pluginConfigSave').click();
  assert.equal(f.field('note').value, '合成草稿'); assert.equal(f.ui.isDirty(), true);
  assert.match(text(f.detail), /这个偏好无效/); assert.equal(f.field('note').getAttribute('aria-invalid'), 'true');
  f.state.reply = null; await f.edit('note', '已更正'); await f.ids.get('pluginConfigSave').click();
  assert.equal(f.ui.isDirty(), false);
});

test('a revision conflict preserves the draft until the user explicitly reloads', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('count', 55);
  f.state.reply = (_url, body) => body ? ok({ error: 'plugin-settings-conflict', message: '设置已在其他窗口更新，请重新读取', revision: 2 }, 409) : null;
  await f.ids.get('pluginConfigSave').click();
  assert.equal(String(f.field('count').value), '55'); assert.equal(f.ui.isDirty(), true);
  assert.match(text(f.detail), /其他窗口|重新读取/); assert.equal(f.ids.get('pluginConfigReload').hidden, false);
  f.state.reply = null; f.state.snapshots.get('dev.settings').values.count = 61;
  await f.ids.get('pluginConfigReload').click();
  assert.equal(String(f.field('count').value), '55'); assert.equal(f.ids.get('pluginLeaveConfirm').hidden, false);
  await f.confirm('放弃更改'); await settle();
  assert.equal(String(f.field('count').value), '61'); assert.equal(f.ui.isDirty(), false);
});

test('a successful retry clears the previous conflict reload action', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('count', 41);
  f.state.reply = (_url, body) => body ? ok({ error: 'plugin-settings-conflict', message: '请重新读取' }, 409) : null;
  await f.ids.get('pluginConfigSave').click(); assert.equal(f.ids.get('pluginConfigReload').hidden, false);
  f.state.reply = null; await f.ids.get('pluginConfigSave').click();
  assert.equal(f.ui.isDirty(), false); assert.equal(f.ids.get('pluginConfigReload').hidden, true);
});

test('dirty navigation can continue editing, discard or save before leaving', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('count', 39); let navigated = 0;
  assert.equal(f.ui.requestLeave(() => { navigated++; }), false); assert.equal(navigated, 0);
  await f.confirm('继续编辑'); assert.equal(navigated, 0); assert.equal(f.ui.isDirty(), true);
  f.ui.requestLeave(() => { navigated++; }); await f.confirm('放弃更改');
  assert.equal(navigated, 1); assert.equal(f.ui.isDirty(), false);
  await f.ui.open('dev.settings'); await f.edit('count', 47);
  f.ui.requestLeave(() => { navigated++; }); await f.confirm('保存并离开');
  assert.equal(navigated, 2); assert.equal(f.calls.at(-1).body.values.count, 47);
});

test('configuration remains usable while disabled and never enables or starts a plugin', async () => {
  const f = fixture(); f.plugins[0].enabled = false;
  f.state.snapshots.get('dev.settings').enabled = false; f.state.snapshots.get('dev.settings').plugin.enabled = false;
  await f.ui.open('dev.settings'); assert.match(text(f.detail), /已停用|启用后/);
  await f.edit('count', 40); await f.ids.get('pluginConfigSave').click();
  assert.equal(f.calls.length, 2); assert.ok(f.calls.every(call => call.url.startsWith('/api/plugins/settings')));
});

test('unmet dependencies leave settings editable while blocking related business pages', async () => {
  const f = fixture();
  f.plugins[0].dependencies = ['dev.provider', 'dev.other'];
  f.plugins[0].dependencyError = { code: 'plugin-dependency-disabled', message: '请先启用依赖插件 dev.provider' };
  await f.ui.open('dev.settings');
  assert.equal(f.ids.get('pluginConfigDependencies').hidden, false);
  assert.equal(f.ids.get('pluginConfigDependencies').textContent, '依赖插件：dev.provider、dev.other');
  assert.match(f.ids.get('pluginConfigState').textContent, /依赖未满足.*dev.provider/);
  assert.equal(f.field('count').disabled, false);
  const link = button(f.detail, '助手仓库');
  assert.equal(link.disabled, true); assert.match(link.title, /dev.provider/);
  await link.click(); assert.equal(f.nav.length, 0);
  await f.edit('count', 44); await f.ids.get('pluginConfigSave').click();
  assert.equal(f.calls.at(-1).body.values.count, 44); assert.match(f.ids.get('pluginConfigStatus').textContent, /依赖恢复后生效/);
  assert.ok(f.calls.every(call => call.url.startsWith('/api/plugins/settings')));
  assert.equal(f.plugins[0].enabled, true);
});

test('dependency recovery restores related pages without rebuilding controls or overwriting a draft', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('note', '依赖恢复前的草稿');
  const input = f.field('note'), link = button(f.detail, '助手仓库');
  f.plugins[0].dependencies = ['dev.provider'];
  f.plugins[0].dependencyError = { code: 'plugin-dependency-missing', message: '缺少依赖插件 dev.provider' };
  f.ui.onListing({ plugins: f.plugins });
  assert.equal(link.disabled, true); assert.match(f.ids.get('pluginConfigState').textContent, /缺少依赖插件/);
  delete f.plugins[0].dependencyError; f.ui.onListing({ plugins: f.plugins });
  assert.equal(link.disabled, false); assert.equal(link.title, '');
  assert.doesNotMatch(f.ids.get('pluginConfigState').textContent, /依赖未满足/);
  assert.equal(f.field('note'), input); assert.equal(input.value, '依赖恢复前的草稿'); assert.equal(f.ui.isDirty(), true);
});


test('dependency process failures are diagnosed without blocking configuration edits', async () => {
  const f = fixture(); f.plugins[0].process = { state: 'dependency-blocked', reason: 'dev.provider 启动失败' };
  await f.ui.open('dev.settings');
  assert.match(f.ids.get('pluginConfigState').textContent, /依赖不可用.*dev.provider 启动失败/);
  assert.equal(f.field('count').disabled, false);
});

test('polling updates state without rebuilding controls or overwriting drafts', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); const input = f.field('count'); await f.edit('count', 88);
  f.plugins.forEach(item => { item.enabled = false; }); f.ui.onListing({ plugins: f.plugins });
  assert.equal(f.field('count'), input); assert.equal(String(input.value), '88'); assert.equal(f.ui.isDirty(), true);
  assert.match(text(f.detail), /已停用|启用后/); assert.equal(f.calls.length, 1);
});

test('a removed plugin preserves its draft but cannot save stale settings', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('count', 72); f.ui.onListing({ plugins: [] });
  const before = f.calls.length; await f.ids.get('pluginConfigSave').click();
  assert.equal(f.calls.length, before); assert.equal(String(f.field('count').value), '72'); assert.equal(f.ui.isDirty(), true);
  assert.match(text(f.detail), /移除|不可用|找不到|返回/);
});

test('a late settings read cannot replace a newly opened plugin', async () => {
  const f = fixture(), pending = deferred();
  f.state.reply = url => url.endsWith('id=dev.settings') ? pending.promise : null;
  const opening = f.ui.open('dev.settings'); await settle(); await f.ui.open('dev.other');
  pending.resolve(ok(f.state.snapshots.get('dev.settings'))); await opening; await settle();
  assert.match(text(f.ids.get('pluginConfigTitle')), /另一插件/); assert.doesNotMatch(text(f.ids.get('pluginConfigTitle')), /合成插件/);
  assert.equal(f.ui.isDirty(), false);
});

test('saving blocks navigation until the write completes and leaves the current draft intact', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('note', '待保存');
  const pending = deferred(); f.state.reply = (_url, body) => body ? pending.promise : null;
  const saving = f.ids.get('pluginConfigSave').click(); await settle(); let navigated = false;
  assert.equal(f.ui.requestLeave(() => { navigated = true; }), false); assert.equal(navigated, false);
  assert.equal(f.field('note').value, '待保存');
  pending.resolve(ok({ ...f.state.snapshots.get('dev.settings'), values: { ...defaults, note: '待保存' }, revision: 2 }));
  await saving; await settle(); assert.equal(f.ui.isDirty(), false); assert.equal(navigated, false);
});

test('save-and-leave disables every confirmation action while its POST is pending', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('note', '保存后离开');
  const pending = deferred(); f.state.reply = (_url, body) => body ? pending.promise : null;
  let navigated = 0; f.ui.requestLeave(() => { navigated++; });
  const saving = f.ids.get('pluginLeaveSave').click(); await settle();
  for (const id of ['pluginLeaveSave', 'pluginLeaveDiscard', 'pluginLeaveStay']) assert.equal(f.ids.get(id).disabled, true, id);
  await f.ids.get('pluginLeaveDiscard').click(); await f.ids.get('pluginLeaveStay').click();
  assert.equal(navigated, 0); assert.equal(f.ui.isEditing(), true); assert.equal(f.ui.isDirty(), true);
  pending.resolve(ok({ ...f.state.snapshots.get('dev.settings'), values: { ...defaults, note: '保存后离开' }, revision: 2 }));
  await saving; await settle();
  assert.equal(navigated, 1); assert.equal(f.ui.isEditing(), false); assert.equal(f.ids.get('pluginLeaveConfirm').hidden, true);
});

test('return restores focus to the same plugin settings entry after the list rebuilt its opener', async () => {
  const f = fixture(), list = f.document.createElement('div'); list.id = 'pluginList'; f.document.body.appendChild(list);
  const opener = f.document.createElement('button'); opener.id = 'pluginSettings-dev.settings'; list.appendChild(opener);
  await f.ui.open('dev.settings', opener);
  const replacement = f.document.createElement('button'); replacement.id = 'pluginSettings-dev.settings'; list.replaceChildren(replacement);
  assert.equal(opener.isConnected, false); f.ui.back();
  assert.equal(f.document.activeElement, replacement); assert.equal(f.ui.isEditing(), false);
});

test('plugins without a schema explain why there are no editable settings', async () => {
  const f = fixture(); f.state.snapshots.get('dev.settings').schema = null; await f.ui.open('dev.settings');
  assert.match(text(f.detail), /设置|配置/); assert.equal(f.ui.isDirty(), false); assert.equal(f.ids.has('pluginConfigSave'), false);
  assert.match(text(f.detail), /这个插件暂无可调整的设置/);
});

test('an incompatible plugin can explain its settings state without values or editable controls', async () => {
  const f = fixture(); f.plugins[0].incompatible = '插件需要 Core 接口版本 2';
  f.state.snapshots.get('dev.settings').values = {};
  await f.ui.open('dev.settings');
  assert.match(text(f.detail), /与当前应用不兼容，不能修改设置/);
  assert.doesNotMatch(text(f.detail), /字段不正确/); assert.equal(f.field('count'), undefined);
  assert.equal(f.calls.length, 1); assert.equal(f.ui.isDirty(), false);
});

test('related navigation respects unsaved changes and model settings share the same leave guard', async () => {
  const f = fixture(); await f.ui.open('dev.settings');
  const model = button(f.detail, '模型设置'); assert.ok(model); await model.click(); assert.equal(f.models.length, 1);
  await f.ui.open('dev.settings'); await f.edit('note', '先保留这份草稿');
  await button(f.detail, '助手仓库').click(); assert.equal(f.nav.length, 0);
  await f.confirm('继续编辑'); assert.equal(f.ui.isDirty(), true); assert.equal(f.nav.length, 0);
  await button(f.detail, '助手仓库').click(); await f.confirm('放弃更改');
  assert.equal(f.nav.length, 1); assert.equal(f.nav[0].target, 'repository');
});

test('back cancels a pending read and ignores its eventual response', async () => {
  const f = fixture(), pending = deferred(); let signal;
  f.state.reply = (_url, _body, options) => { signal = options.signal; return pending.promise; };
  const opening = f.ui.open('dev.settings'); await settle(); assert.equal(f.ui.isEditing(), true);
  f.ui.back(); assert.equal(signal.aborted, true); assert.equal(f.ui.isEditing(), false);
  pending.resolve(ok(f.state.snapshots.get('dev.settings'))); await opening;
  assert.equal(f.detail.hidden, true); assert.equal(f.ids.get('pluginOverview').hidden, false); assert.equal(f.ui.isEditing(), false);
});

test('schema changes during editing preserve the draft but block a stale save', async () => {
  const f = fixture(); await f.ui.open('dev.settings'); await f.edit('note', '保留草稿');
  f.plugins[0].contributes.settings = { ...clone(schema), description: '新版本设置声明' };
  f.ui.onListing({ plugins: f.plugins });
  assert.equal(f.ids.get('pluginConfigSave').disabled, true); assert.equal(f.field('note').value, '保留草稿');
  assert.match(text(f.detail), /设置声明已变化/); assert.equal(f.ui.isDirty(), true);
});

test('message insights retains reply preferences while concurrency belongs to core model settings', async () => {
  const manifest = JSON.parse(readFileSync(path.join(__dirname, '../plugins/kyf.message-insights/plugin.json'), 'utf8'));
  const settings = manifest.contributes.settings, values = Object.fromEntries(settings.groups.flatMap(group => group.fields).map(field => [field.key, field.default]));
  const item = plugin(manifest.id, { name: manifest.name, version: manifest.version, contributes: manifest.contributes });
  const ownDefinition = createHash('sha256').update(JSON.stringify(settings)).digest('hex');
  const f = fixture(); f.plugins.push(item);
  f.state.snapshots.set(item.id, { plugin: item, schema: settings, values: { ...values, replyStyle: 'polite' },
    revision: 4, definition: ownDefinition, enabled: true, effective: 'next-task' });
  await f.ui.open(item.id);
  assert.equal(f.field('maxConcurrentTasks'), undefined);
  assert.equal(f.field('replyStyle').value, 'polite');
  assert.match(text(f.detail), /默认回复语气/);
  assert.doesNotMatch(text(f.detail), /并行数量|同时执行的 AI 请求数/);
  for (const replyStyle of ['natural', 'brief']) {
    await f.edit('replyStyle', replyStyle); await f.ids.get('pluginConfigSave').click();
    assert.deepEqual(f.calls.at(-1).body.values, { replyStyle });
    assert.equal(f.calls.at(-1).body.definition, ownDefinition);
    assert.equal(f.ui.isDirty(), false);
  }
  f.ui.back(); await f.ui.open(item.id);
  assert.equal(f.field('replyStyle').value, 'brief');
  assert.equal(f.field('maxConcurrentTasks'), undefined);
  assert.ok(f.calls.every(call => call.url.startsWith('/api/plugins/settings')), 'editing settings never starts a model task or restarts a plugin');
});
