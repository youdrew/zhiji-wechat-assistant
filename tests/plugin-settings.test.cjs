// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 设置 → 插件: listing, enabled state, enable switch, recovery diagnostics, rescan, compatibility and slot refresh.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { makeDocument, text, find, settle } = require('./helpers/plugin-settings-dom.cjs');
const source = readFileSync(path.join(__dirname, '../chatui/plugin-settings.js'), 'utf8');

const plugin = (id, extra = {}) => ({ id, name: id === 'kyf.message-insights' ? '分析与建议' : id === 'kyf.friend-analysis' ? '知己分析' : '本地实验',
  version: '1.0.0', description: '', author: '', source: id.startsWith('kyf.') ? 'bundled' : 'local', events: [],
  contributes: { messageAnnotations: id === 'kyf.message-insights' }, enabled: true, process: { state: 'idle', reason: null, log: [] }, ...extra });

function fixture() {
  const { ids, document, add } = makeDocument(), calls = [], timers = [], opened = [];
  for (const id of ['pluginList', 'pluginStatus', 'btnRescanPlugins', 'settingsModal', 'panelPlugins', 'pluginOverview', 'pluginDetail']) add(id);
  ids.get('settingsModal').className = 'settings-modal-overlay show'; ids.get('panelPlugins').className = 'settings-panel active';
  const controls = { listing: { coreApi: 1, plugins: [plugin('kyf.friend-analysis'), plugin('kyf.message-insights', { process: { state: 'ready', reason: null, log: [] } })], problems: [] },
    reply: null };
  const fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push([url, body]);
    if (controls.reply) { const reply = controls.reply(url, body); if (reply) return reply; }
    if (url === '/api/plugins/enable') controls.listing.plugins = controls.listing.plugins.map(item => item.id === body.id ? { ...item, enabled: body.enabled, process: { state: 'idle', reason: null, log: [] } } : item);
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(controls.listing)) };
  };
  const window = { PluginConfiguration: { init() {}, open: (id, opener) => opened.push({ id, opener }), onListing() {}, isEditing: () => false } }, changes = [];
  vm.runInNewContext(source, { window, document, fetch, JSON, setTimeout: handler => { timers.push(handler); return timers.length; }, clearTimeout() {} });
  window.PluginSettings.init({ onChange: listing => changes.push(listing), onOpenSettings: (id, opener) => opened.push({ id, opener }) });
  const rows = () => ids.get('pluginList').children;
  const switchOf = row => find(row, item => item.attributes?.role === 'switch');
  const buttonOf = (row, label) => find(row, item => item.tagName === 'BUTTON' && item.textContent.includes(label));
  return { ids, calls, controls, changes, timers, text, rows, switchOf, buttonOf, opened, settings: window.PluginSettings };
}

test('lists plugin metadata with accessible switches and no normal status text', async () => {
  const f = fixture();
  f.controls.listing.plugins.push(plugin('dev.sample', { enabled: false }),
    plugin('dev.starting', { process: { state: 'starting', reason: null, log: [] } }),
    plugin('dev.stopped', { process: { state: 'stopped', reason: '插件已停用', log: [] } }),
    plugin('dev.external', { name: '合成外部插件', source: 'external', enabled: false }));
  await f.settings.refresh(); await settle();
  const [analysis, insights, local, starting, stopped, external] = f.rows();
  assert.equal(analysis.querySelector('strong').textContent, '知己分析');
  assert.equal(analysis.querySelector('.plugin-meta').textContent, 'v1.0.0');
  assert.equal(analysis.querySelector('.plugin-source').textContent, '内置');
  assert.equal(local.querySelector('.plugin-source').textContent, '外部');
  assert.equal(external.querySelector('.plugin-source').textContent, '外部');
  assert.equal(f.switchOf(external).attributes['aria-label'], '启用合成外部插件');
  assert.equal(f.switchOf(external).attributes['aria-checked'], 'false');
  for (const row of [analysis, insights, starting, stopped]) {
    assert.equal(f.switchOf(row).attributes['aria-checked'], 'true');
    assert.equal(f.switchOf(row).attributes['aria-label'], `停用${row.querySelector('strong').textContent}`);
    assert.equal(row.querySelector('.plugin-state'), null);
    assert.equal(row.querySelector('.plugin-toggle-state'), null);
  }
  assert.equal(local.querySelector('.plugin-toggle-state'), null);
  assert.equal(local.querySelector('.plugin-state'), null);
  assert.equal(f.switchOf(local).attributes['aria-label'], '启用本地实验');
  assert.equal(f.switchOf(local).attributes['aria-checked'], 'false'); assert.equal(f.buttonOf(local, '重启'), undefined);
  assert.equal(f.changes.length, 0);
});

test('every plugin has a settings entry, including disabled plugins, without enabling it', async () => {
  const f = fixture(); f.controls.listing.plugins.push(plugin('dev.sample', { enabled: false }));
  await f.settings.refresh(); await settle();
  for (const row of f.rows()) assert.ok(f.buttonOf(row, '设置'));
  const settings = f.buttonOf(f.rows()[2], '设置'), before = f.calls.length;
  await settings.click();
  assert.equal(f.opened.length, 1); assert.equal(f.opened[0].id, 'dev.sample');
  assert.equal(f.opened[0].opener, settings); assert.equal(f.calls.length, before);
});

test('a running plugin exposes only settings and the enable switch', async () => {
  const f = fixture(); await settle();
  const row = f.rows()[1], actions = row.querySelector('.plugin-actions');
  assert.deepEqual(actions.children, [f.buttonOf(row, '设置'), f.switchOf(row)]);
  assert.equal(row.querySelector('.plugin-more'), null);
  assert.equal(f.buttonOf(row, '重启'), undefined);
});

test('toggling posts the new state and refreshes contribution slots', async () => {
  const f = fixture(); await settle();
  await f.switchOf(f.rows()[1]).click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/enable', { id: 'kyf.message-insights', enabled: false }]);
  assert.equal(f.switchOf(f.rows()[1]).attributes['aria-checked'], 'false');
  assert.equal(f.switchOf(f.rows()[1]).attributes['aria-label'], '启用分析与建议');
  assert.equal(f.rows()[1].querySelector('.plugin-toggle-state'), null);
  assert.equal(f.rows()[1].querySelector('.plugin-state'), null);
  assert.match(f.ids.get('pluginStatus').textContent, /分析与建议 已停用/);
  assert.equal(f.changes.length, 1);
  assert.equal(f.changes[0].plugins.find(item => item.id === 'kyf.message-insights').enabled, false);
  await f.switchOf(f.rows()[1]).click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/enable', { id: 'kyf.message-insights', enabled: true }]);
  assert.equal(f.switchOf(f.rows()[1]).attributes['aria-checked'], 'true');
  assert.equal(f.switchOf(f.rows()[1]).attributes['aria-label'], '停用分析与建议');
  assert.equal(f.rows()[1].querySelector('.plugin-toggle-state'), null);
  assert.equal(f.rows()[1].querySelector('.plugin-state'), null);
  assert.equal(f.changes.length, 2);
});

test('incompatible plugins cannot be enabled and manifest problems are shown plainly', async () => {
  const f = fixture();
  f.controls.listing.plugins.push(plugin('dev.old', { enabled: false, incompatible: '插件需要 Core 接口版本 2，当前提供 1' }));
  f.controls.listing.problems = [{ directory: 'D:\\kyf\\.local\\plugins\\broken', source: 'local', error: 'plugin.json 无法读取' }];
  await f.settings.refresh(); await settle();
  const old = f.rows()[2];
  assert.equal(old.querySelector('.plugin-state'), null);
  assert.match(old.querySelector('.plugin-problem').textContent, /^不兼容 · .*当前提供 1；不能启用/);
  assert.equal(old.querySelector('.plugin-toggle-state'), null);
  assert.equal(f.switchOf(old).attributes['aria-checked'], 'false');
  assert.equal(f.switchOf(old).disabled, true);
  const text = f.text(f.ids.get('pluginList'));
  assert.match(text, /未加载/); assert.match(text, /broken外部plugin\.json 无法读取/);
});

test('enable errors from Core are reported and the switch is released', async () => {
  const f = fixture(); await settle();
  f.controls.reply = url => url === '/api/plugins/enable' ? { ok: false, status: 409, json: async () => ({ error: 'plugin-incompatible', message: '插件需要 Core 接口版本 2' }) } : null;
  await f.switchOf(f.rows()[0]).click();
  assert.match(f.ids.get('pluginStatus').textContent, /插件需要 Core 接口版本 2/);
  assert.equal(f.switchOf(f.rows()[0]).disabled, false); assert.equal(f.changes.length, 0);
});

test('unmet dependencies explain the block without preventing an enabled plugin from being disabled', async () => {
  const f = fixture(), error = { code: 'plugin-dependency-disabled', message: '请先启用依赖插件 dev.provider' };
  f.controls.listing.plugins = [plugin('dev.waiting', { enabled: false, dependencies: ['dev.provider', 'dev.other'], dependencyError: error }),
    plugin('dev.selected', { dependencies: ['dev.provider'], dependencyError: error })];
  await f.settings.refresh(); await settle();
  const [waiting, selected] = f.rows();
  assert.equal(waiting.querySelector('.plugin-dependencies').textContent, '依赖插件：dev.provider、dev.other');
  assert.match(waiting.querySelector('.plugin-dependency-error').textContent, /依赖未满足.*dev.provider/);
  assert.equal(f.switchOf(waiting).disabled, true); assert.match(f.switchOf(waiting).title, /先启用依赖插件 dev.provider/);
  assert.equal(f.switchOf(selected).disabled, false); assert.equal(f.switchOf(selected).attributes['aria-checked'], 'true');
  await f.switchOf(selected).click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/enable', { id: 'dev.selected', enabled: false }]);
  assert.equal(f.switchOf(f.rows()[1]).attributes['aria-checked'], 'false');
});

test('dependency changes refresh contribution slots while preserving the selected enable state', async () => {
  const f = fixture(); await settle();
  const item = f.controls.listing.plugins[0];
  item.dependencies = ['dev.provider'];
  item.dependencyError = { code: 'plugin-dependency-missing', message: '缺少依赖插件 dev.provider' };
  await f.settings.refresh(); await settle();
  assert.equal(f.changes.length, 1); assert.equal(f.switchOf(f.rows()[0]).attributes['aria-checked'], 'true');
  delete item.dependencyError;
  await f.settings.refresh(); await settle();
  assert.equal(f.changes.length, 2); assert.equal(f.rows()[0].querySelector('.plugin-dependency-error'), null);
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-checked'], 'true');
});

test('a dependency process failure remains visible while the plugin enable selection is retained', async () => {
  const f = fixture();
  f.controls.listing.plugins[0].process = { state: 'dependency-blocked', reason: '依赖插件 dev.provider 启动失败', log: [] };
  await f.settings.refresh(); await settle();
  assert.match(f.text(f.rows()[0].querySelector('.plugin-state')), /依赖不可用.*dev.provider 启动失败/);
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-checked'], 'true'); assert.equal(f.switchOf(f.rows()[0]).disabled, false);
});

test('a plugin blocked after crashes keeps diagnostics and recovers through the enable switch', async () => {
  const f = fixture();
  f.controls.listing.plugins[0].process = { state: 'blocked', reason: '插件在 10 分钟内连续异常退出，已暂停自动启动；可在设置中关闭后重新开启插件', log: ['Traceback: boom'] };
  await f.settings.refresh(); await settle();
  const row = f.rows()[0];
  assert.equal(f.switchOf(row).attributes['aria-checked'], 'true');
  assert.equal(row.querySelector('.plugin-toggle-state'), null);
  assert.match(f.text(row.querySelector('.plugin-state')), /已阻止 · 插件在 10 分钟内连续异常退出/);
  assert.match(f.text(row.querySelector('.plugin-state')), /可在设置中关闭后重新开启插件/);
  assert.equal(row.querySelector('.plugin-log').querySelector('summary').textContent, '最近输出');
  assert.equal(row.querySelector('.plugin-log').querySelector('pre').textContent, 'Traceback: boom');
  assert.deepEqual(row.querySelector('.plugin-actions').children, [f.buttonOf(row, '设置'), f.switchOf(row)]);
  assert.equal(f.buttonOf(row, '重新启动'), undefined);
  await f.switchOf(row).click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/enable', { id: 'kyf.friend-analysis', enabled: false }]);
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-checked'], 'false');
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-label'], '启用知己分析');
  assert.equal(f.rows()[0].querySelector('.plugin-toggle-state'), null);
  assert.equal(f.rows()[0].querySelector('.plugin-state'), null);
  await f.switchOf(f.rows()[0]).click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/enable', { id: 'kyf.friend-analysis', enabled: true }]);
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-checked'], 'true');
  assert.equal(f.switchOf(f.rows()[0]).attributes['aria-label'], '停用知己分析');
  assert.equal(f.rows()[0].querySelector('.plugin-toggle-state'), null);
  assert.equal(f.rows()[0].querySelector('.plugin-state'), null);
  assert.equal(f.calls.some(([url]) => url === '/api/plugins/restart'), false);
});

test('rescanning picks up new plugins and notifies slots only when something changed', async () => {
  const f = fixture(); await settle();
  await f.ids.get('btnRescanPlugins').click();
  assert.deepEqual(f.calls.at(-1), ['/api/plugins/refresh', {}]); assert.equal(f.changes.length, 0);
  f.controls.listing.plugins.push(plugin('kyf.advisor'));
  await f.ids.get('btnRescanPlugins').click();
  assert.equal(f.rows().length, 3); assert.equal(f.changes.length, 1);
  assert.ok(f.timers.length > 0);
});
