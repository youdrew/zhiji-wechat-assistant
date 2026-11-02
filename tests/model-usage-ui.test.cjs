// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The global 模型调用 window: always-on usage for all friends, a friend filter, call records with
// in-flight attempts and captured request / response details, and clearing.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = readFileSync(path.join(__dirname, '../chatui/model-usage.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
const plain = value => JSON.parse(JSON.stringify(value));
const descendants = element => [element, ...(element.children || []).flatMap(descendants)];
const lastCall = (fixture, operation) => fixture.calls.filter(([name]) => name === operation).at(-1)?.[1];
const recordRows = fixture => descendants(fixture.ids.get('modelUsageRecords')).filter(element => String(element.className).split(' ').includes('model-usage-record'));
async function selectRange(fixture, anchor, focus = anchor, preview = false) {
  const chart = fixture.charts.at(-1);
  assert.ok(chart, 'a usage trend is mounted');
  chart.options.setSelection({ anchor, focus }, { preview });
  await settle();
  return chart;
}

function fixture({ account = 'account-a', scoped = false } = {}) {
  const ids = new Map(), calls = [], keydown = [], charts = [], windowEvents = new Map();
  function node(tag = 'div') {
    const value = { tagName: tag.toUpperCase(), children: [], listeners: {}, attributes: {}, style: {}, dataset: {},
      hidden: false, disabled: false, value: '', textContent: '', className: '', clientWidth: 640 };
    value.classList = { add() {}, remove() {}, toggle() {} };
    Object.defineProperty(value, 'id', { get: () => value._id, set(name) { value._id = name; ids.set(name, value); } });
    value.append = (...children) => { for (const child of children) { child.parentElement = value; value.children.push(child); } };
    value.appendChild = child => { value.append(child); return child; };
    value.replaceChildren = (...children) => { value.children = []; value.append(...children); };
    value.setAttribute = (key, data) => {
      value.attributes[key] = String(data);
      if (key === 'tabindex') value.tabIndex = Number(data);
      if (key === 'colspan') value.colSpan = Number(data);
    };
    value.getAttribute = key => value.attributes[key] ?? null;
    value.addEventListener = (key, handler) => (value.listeners[key] ||= []).push(handler);
    value.click = async () => { for (const handler of value.listeners.click || []) await handler({ target: value, stopPropagation() {} }); await settle(); };
    value.focus = () => { document.activeElement = value; };
    value.contains = target => descendants(value).includes(target);
    value.closest = selector => {
      for (let item = value; item; item = item.parentElement) {
        if (selector.split(',').some(part => part.trim().toUpperCase() === item.tagName || (part.trim().startsWith('.') && String(item.className).split(' ').includes(part.trim().slice(1))))) return item;
      }
      return null;
    };
    return value;
  }
  const document = { body: node('body'), createElement: node, getElementById: id => ids.get(id), hidden: false, addEventListener: (key, handler) => { if (key === 'keydown') keydown.push(handler); } };
  const records = [
    { attemptId: 'a1', at: Date.UTC(2026, 9, 13, 6), plugin: 'kyf.friend-analysis', task: 'task-1', scope: 'friend', user: 'wxid_a', step: 'friend-summary', status: 'ok', model: 'model-x', inputTokens: 1200, outputTokens: 300, totalInputTokens: 1200, totalOutputTokens: 300, totalTokens: 1500, totalTokensSource: 'provider', accountingVersion: 1, durationMs: 2100, firstByteMs: 800, firstTextMs: 1200, traceRecordId: 'trace-1' },
    { attemptId: 'a2', at: Date.UTC(2026, 9, 13, 5), plugin: 'kyf.message-insights', scope: 'none', user: null, status: 'timeout', model: 'model-x', inputTokens: null, outputTokens: null, totalInputTokens: null, totalOutputTokens: null, totalTokens: null, accountingVersion: 1, durationMs: 60000 },
  ];
  function aggregate(items) {
    const result = { requests: items.length, failures: items.filter(item => ['timeout', 'http-error', 'network-error', 'stream-error'].includes(item.status)).length,
      inputTokens: 0, outputTokens: 0, totalInputTokens: 0, totalOutputTokens: 0, totalTokens: 0, reasoningTokens: 0,
      cacheReadTokens: 0, cacheWriteTokens: 0, missingUsage: 0, missingInputUsage: 0, missingOutputUsage: 0, missingTotalUsage: 0,
      knownInputRequests: 0, knownOutputRequests: 0, knownTotalRequests: 0, knownReasoningRequests: 0,
      knownCacheReadRequests: 0, knownCacheWriteRequests: 0, firstAt: null, lastAt: null };
    for (const item of items) {
      for (const side of ['Input', 'Output', 'Total']) {
        const key = side === 'Total' ? 'totalTokens' : `total${side}Tokens`;
        if (Number.isSafeInteger(item[key]) && item[key] >= 0) { result[key] += item[key]; result[`known${side}Requests`]++; }
        else result[`missing${side}Usage`]++;
      }
      for (const key of ['inputTokens', 'outputTokens', 'reasoningTokens', 'cacheReadTokens', 'cacheWriteTokens'])
        if (Number.isSafeInteger(item[key]) && item[key] >= 0) result[key] += item[key];
      for (const [field, known] of [['reasoningTokens', 'knownReasoningRequests'], ['cacheReadTokens', 'knownCacheReadRequests'], ['cacheWriteTokens', 'knownCacheWriteRequests']])
        if (Number.isSafeInteger(item[field]) && item[field] >= 0) result[known]++;
      if (item.inputTokens == null || item.outputTokens == null) result.missingUsage++;
      result.firstAt = result.firstAt === null ? item.at : Math.min(result.firstAt, item.at);
      result.lastAt = result.lastAt === null ? item.at : Math.max(result.lastAt, item.at);
    }
    return result;
  }
  const analysis = aggregate([records[0]]), insights = aggregate([records[1]]);
  const controls = { summary: { requests: 2, inputTokens: 1200, outputTokens: 300, missingUsage: 1, failures: 1, droppedRecords: 0,
    ...aggregate(records), cutoffMs: Date.UTC(2026, 9, 14, 6),
    models: ['model-x', 'model-on-another-page'],
    plugins: [{ plugin: 'kyf.friend-analysis', name: '知己分析', ...analysis },
      { plugin: 'kyf.message-insights', name: '分析与建议', ...insights }] },
    modules: [{ plugin: 'kyf.friend-analysis', name: '知己分析', ...analysis, steps: [{ step: 'friend-summary', name: '近期概述', ...analysis }] },
      { plugin: 'kyf.message-insights', name: '分析与建议', ...insights, steps: [{ step: null, name: '未分类步骤', ...insights }] }],
    timelineRecords: records,
    friends: [{ scope: 'friend', user: 'wxid_a', name: '小明', ...analysis },
      { scope: 'none', user: null, name: null, ...insights }],
    records, next: null, clear: { ok: true } };
  const filtered = (items, args) => items.filter(item => (!args.friend || (['self', 'none', 'multi'].includes(args.friend) ? item.scope === args.friend : item.user === args.friend)) &&
    (!args.plugin || item.plugin === args.plugin) && (!args.step || (args.step === '__none__' ? !item.step : item.step === args.step)) &&
    (!args.model || item.model === args.model) && (!args.status || item.status === args.status) &&
    (!args.since || item.at >= Number(args.since)) && (!args.until || item.at < Number(args.until)));
  function groupedModules(items) {
    return [...new Set(items.map(item => item.plugin))].map(plugin => {
      const group = items.filter(item => item.plugin === plugin);
      return { plugin, name: controls.modules.find(item => item.plugin === plugin)?.name || plugin, ...aggregate(group),
        steps: [...new Set(group.map(item => item.step || null))].map(step => ({ step, ...aggregate(group.filter(item => (item.step || null) === step)) })) };
    });
  }
  const traces = { rendered: [], retained: [], refreshed: [],
    renderTrace(container, id, tab, options) { container.textContent = `trace:${id}:${tab}`; this.rendered.push([id, tab, options]); },
    retainTraces(ids) { this.retained.push([...ids]); }, refreshTrace(id) { this.refreshed.push(id); } };
  const window = { document, addEventListener(type, handler) { const handlers = windowEvents.get(type) || []; handlers.push(handler); windowEvents.set(type, handlers); }, modelTracesWindow: { close: async () => { calls.push(['close-window']); return true; } },
    desktopHost: { modelTracesRequest: async (operation, args) => {
      calls.push([operation, args]);
      if (operation === 'usage-summary') return { ok: true, ...controls.summary,
        ...(scoped ? { ...aggregate(filtered(controls.timelineRecords, args)), plugins: groupedModules(filtered(controls.timelineRecords, args)) } : {}) };
      if (operation === 'usage-friends') return { ok: true, friends: controls.friends };
      if (operation === 'usage-records') return { ok: true, records: scoped ? filtered(controls.records, args) : controls.records, next: controls.next };
      if (operation === 'usage-modules') return { ok: true, modules: scoped ? groupedModules(filtered(controls.timelineRecords, args)) : controls.modules };
      if (operation === 'usage-timeline') {
        const bounds = args.boundaries.split(',').map(Number);
        const itemsForTrend = filtered(controls.timelineRecords, args);
        const buckets = bounds.slice(0, -1).map((since, index) => {
          const items = itemsForTrend.filter(item => item.at >= since && item.at < bounds[index + 1]);
          const groups = new Map();
          for (const item of items) { const group = groups.get(item.plugin) || []; group.push(item); groups.set(item.plugin, group); }
          return { since, until: bounds[index + 1], ...aggregate(items), modules: [...groups].map(([plugin, values]) => ({ plugin, ...aggregate(values) })) };
        });
        return { ok: true, bounds, buckets, firstAt: controls.summary.firstAt, lastAt: controls.summary.lastAt,
          cutoffMs: controls.summary.cutoffMs, droppedRecords: 0 };
      }
      if (operation === 'usage-clear') return controls.clear;
      return { ok: false };
    } } };
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({ window, document, Date, Number,
    setTimeout: handler => { timers.set(++timerId, handler); return timerId; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(readFileSync(path.join(__dirname, '../plugin-sdk/web/time-trend.js'), 'utf8'), context);
  window.KyfTimeTrend.createChart = options => {
    const chart = { options, element: node(), draw() {}, reveal() {}, isSelecting: () => !!controls.selecting, destroy() { chart.destroyed = true; },
      select(index) { options.setSelection({ anchor: index, focus: index }, { preview: false }); } };
    charts.push(chart);
    return chart;
  };
  vm.runInContext(source, context);
  window.ModelUsage.init({ account, traces });
  const text = value => String(value.textContent || '') + (value.children || []).map(text).join('');
  const find = (element, label) => {
    if (element.tagName === 'BUTTON' && element.textContent === label) return element;
    for (const child of element.children || []) { const match = find(child, label); if (match) return match; }
  };
  async function change(id, value) { ids.get(id).value = value; for (const handler of ids.get(id).listeners.change || []) await handler(); await settle(); }
  const tick = async () => { const [id, handler] = [...timers].at(-1) || []; assert.ok(handler, 'a refresh is scheduled'); timers.delete(id); handler(); await settle(); };
  const windowEvent = async type => { for (const handler of windowEvents.get(type) || []) await handler({}); await settle(); };
  return { window, ids, calls, controls, traces, text, find, change, keydown, charts, aggregate, tick, windowEvent, usage: window.ModelUsage };
}

test('opens on all friends with backend totals, per-plugin and per-friend summaries', async () => {
  const f = fixture(); f.usage.open(); await settle();
  const summary = f.calls.find(([op]) => op === 'usage-summary')[1];
  assert.deepEqual(JSON.parse(JSON.stringify(summary)), { account: 'account-a' });
  assert.equal(f.calls.find(([op]) => op === 'usage-friends')[1].friend, undefined);
  const totals = f.text(f.ids.get('modelUsageSummary'));
  assert.match(totals, /2 次/); assert.match(totals, /1,500/); assert.match(totals, /1,200/);
  assert.match(f.text(f.ids.get('modelUsageAccounting')), /总量完整 1 \/ 2 次/);
  assert.match(f.text(f.ids.get('modelUsageAccounting')), /另有 1 次调用总量未知/);
  const modules = f.text(f.ids.get('modelUsageModules'));
  assert.match(modules, /知己分析/); assert.match(modules, /分析与建议/); assert.match(modules, /1 次总量未知/);
  const friends = f.text(f.ids.get('modelUsageFriends'));
  assert.match(friends, /小明/); assert.match(friends, /未关联好友/);
  const list = f.text(f.ids.get('modelUsageRecords'));
  assert.match(list, /知己分析/); assert.match(list, /超时/);
  await recordRows(f)[0].click();
  assert.match(f.text(f.ids.get('modelUsageRecords')), /输入/);
  assert.match(f.text(f.ids.get('modelUsageRecords')), /1,200/);
  assert.match(f.text(f.ids.get('modelUsageRecords')), /300/);
  await recordRows(f)[1].click();
  assert.match(f.text(f.ids.get('modelUsageRecords')), /服务未提供完整用量/);
  assert.equal(f.ids.get('modelUsageFriend').children.map(item => item.textContent).includes('小明'), true);
});

test('self-analysis calls show 本人 in records, details, summaries and the shared friend filter', async () => {
  const f = fixture({ scoped: true });
  const completed = { ...f.controls.records[0], attemptId: 'self-done', plugin: 'kyf.self-analysis',
    scope: 'self', user: null, step: 'profile-draft', traceRecordId: null };
  const running = { ...completed, attemptId: 'self-running', status: 'running', totalTokens: null };
  f.controls.records.push(completed, running);
  f.controls.modules.push({ plugin: 'kyf.self-analysis', name: '自我剖析' });
  f.controls.friends.push({ scope: 'self', user: null, name: null, ...f.aggregate([completed]) });
  f.usage.open('records'); await settle();
  const rows = recordRows(f);
  for (const row of rows.filter(row => row.attributes['data-attempt-id'].startsWith('self-')))
    assert.equal(f.text(row.children[1]), '本人');
  assert.equal(f.text(rows[1].children[1]), '未关联好友', 'unrelated calls retain their own scope');
  await rows.find(row => row.attributes['data-attempt-id'] === 'self-done').click();
  assert.match(f.text(f.ids.get('modelUsageRecords')), /好友本人/);
  const choice = f.ids.get('modelUsageFriend').children.find(item => item.value === 'self');
  assert.equal(choice.textContent, '本人');
  const selfSummary = f.ids.get('modelUsageFriends').children.find(row => row.children[0]?.textContent === '本人');
  assert.ok(selfSummary);
  await selfSummary.click();
  assert.equal(f.ids.get('modelUsageFriend').value, 'self');
  for (const operation of ['usage-summary', 'usage-modules', 'usage-records', 'usage-timeline'])
    assert.equal(lastCall(f, operation).friend, 'self');
  assert.deepEqual(recordRows(f).map(row => row.attributes['data-attempt-id']), ['self-done', 'self-running']);
  assert.equal(lastCall(f, 'usage-friends').friend, undefined);
  await f.change('modelUsageFriend', 'none');
  assert.deepEqual(recordRows(f).map(row => row.attributes['data-attempt-id']), ['a2']);
});

test('clicking a friend filters totals and records but keeps the full friend list', async () => {
  const f = fixture(); f.usage.open(); await settle();
  await f.ids.get('modelUsageFriends').children[0].click();
  assert.equal(f.calls.filter(([op]) => op === 'usage-summary').at(-1)[1].friend, 'wxid_a');
  assert.equal(f.calls.filter(([op]) => op === 'usage-records').at(-1)[1].friend, 'wxid_a');
  assert.equal(f.calls.filter(([op]) => op === 'usage-friends').at(-1)[1].friend, undefined);
  assert.equal(f.ids.get('modelUsageFriend').value, 'wxid_a');
  assert.equal(f.ids.get('modelUsageFriends').children[0].attributes['aria-pressed'], 'true');
  await f.change('modelUsageFriend', '');
  assert.equal(f.calls.filter(([op]) => op === 'usage-summary').at(-1)[1].friend, undefined);
  await f.change('modelUsageRange', 'today');
  assert.match(f.calls.filter(([op]) => op === 'usage-records').at(-1)[1].since, /^\d+$/);
});

test('switching the main chat keeps the filter; switching accounts resets it', async () => {
  const f = fixture(); f.usage.open(); await settle();
  await f.change('modelUsageFriend', 'wxid_a');
  f.usage.setScope('account-a'); await settle();
  assert.equal(f.ids.get('modelUsageFriend').value, 'wxid_a');
  f.usage.setScope('account-b'); await settle();
  const last = f.calls.filter(([op]) => op === 'usage-summary').at(-1)[1];
  assert.equal(last.account, 'account-b'); assert.equal(last.friend, undefined);
});

test('model filters include the whole ledger and scope every aggregate and page request', async () => {
  const f = fixture(); f.controls.next = [123, 'a2']; f.usage.open(); await settle();
  assert.deepEqual(f.ids.get('modelUsageModel').children.map(item => item.value), ['', 'model-on-another-page', 'model-x']);
  await f.change('modelUsageModel', 'model-on-another-page');
  for (const operation of ['usage-summary', 'usage-friends', 'usage-records'])
    assert.equal(f.calls.filter(([op]) => op === operation).at(-1)[1].model, 'model-on-another-page');
  await f.ids.get('modelUsageMore').click();
  assert.equal(f.calls.filter(([op]) => op === 'usage-records').at(-1)[1].model, 'model-on-another-page');
  f.controls.summary.models = ['model-b']; f.usage.setScope('account-b'); await settle();
  assert.deepEqual(f.ids.get('modelUsageModel').children.map(item => item.value), ['', 'model-b']);
  assert.equal(f.calls.filter(([op]) => op === 'usage-summary').at(-1)[1].model, undefined);
});

test('model choices follow the other filters and keep only the selected model besides them', async () => {
  // Added 2026-10-14 (review): docs §8, the ledger lists the models matching the other filters.
  const f = fixture(); f.usage.open(); await settle();
  f.controls.summary.models = ['model-x'];
  await f.change('modelUsagePlugin', 'kyf.friend-analysis');
  assert.deepEqual(f.ids.get('modelUsageModel').children.map(item => item.value), ['', 'model-x']);
  await f.change('modelUsageModel', 'model-x');
  f.controls.summary.models = ['model-y'];
  await f.change('modelUsageRange', 'today');
  assert.deepEqual(f.ids.get('modelUsageModel').children.map(item => item.value), ['', 'model-y', 'model-x']);
});

const detailOf = (f, index = 0) => { const row = recordRows(f)[index]; return row.parentElement.children[row.parentElement.children.indexOf(row) + 1]; };
const tabOf = (f, index, name) => f.ids.get(`modelUsageRecordTab${index}${name}`);

test('record details switch between overview, request, response and process, and explain capture settings', async () => {
  const f = fixture(); f.usage.open('records'); await settle();
  assert.match(f.text(f.ids.get('modelUsageAccounting')), /开发者模式开启后采集/);
  f.usage.setDeveloperMode({ enabled: true, recordCount: 3 });
  assert.doesNotMatch(f.text(f.ids.get('modelUsageAccounting')), /开发者模式开启后采集/);
  await recordRows(f)[0].click();
  assert.equal(tabOf(f, 0, 'overview').getAttribute('aria-selected'), 'true');
  assert.deepEqual(['overview', 'request', 'response', 'events'].map(name => tabOf(f, 0, name).textContent), ['概览', '请求', '响应', '过程与错误']);
  assert.match(f.text(detailOf(f)), /model-x/);
  assert.deepEqual(f.traces.rendered, [], 'the overview reads nothing from diagnostics');
  await tabOf(f, 0, 'request').click();
  assert.equal(tabOf(f, 0, 'request').getAttribute('aria-selected'), 'true');
  assert.deepEqual(plain(f.traces.rendered.at(-1)), ['trace-1', 'request', { failed: false }]);
  assert.match(f.text(detailOf(f)), /trace:trace-1:request/);
  assert.deepEqual(f.traces.retained.at(-1), ['trace-1']);
  let prevented = false;
  for (const handler of tabOf(f, 0, 'request').listeners.keydown) handler({ key: 'ArrowRight', preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(tabOf(f, 0, 'response').getAttribute('aria-selected'), 'true');
  assert.deepEqual(plain(f.traces.rendered.at(-1)), ['trace-1', 'response', { failed: false }]);
  await tabOf(f, 0, 'events').click();
  const process = f.text(detailOf(f));
  assert.match(process, /首个响应字节\+0\.8 秒/); assert.match(process, /首段可见正文\+1\.2 秒/); assert.match(process, /2\.1 秒后 · 成功/);
  assert.match(process, /trace:trace-1:events/);
  // A record without a captured trace still explains its process and failure from the ledger.
  await recordRows(f)[1].click(); await tabOf(f, 1, 'events').click();
  assert.match(f.text(detailOf(f, 1)), /错误超时：超过等待时限/);
  await tabOf(f, 1, 'request').click();
  assert.match(f.text(detailOf(f, 1)), /未采集请求与响应/);
  await f.ids.get('modelUsageRecordToggle0').click();
  assert.deepEqual(f.traces.retained.at(-1), [], 'collapsed records release their viewers');
});

test('in-flight attempts are listed first, refresh quickly and update totals once they finish', async () => {
  const f = fixture();
  const running = { attemptId: 'live', at: Date.now() - 5000, plugin: 'kyf.advisor', task: 'task-live', scope: 'friend', user: 'wxid_a', step: 'advisor', status: 'running', model: 'model-x', totalTokens: null, traceRecordId: 'trace-live' };
  f.controls.records = [running, ...f.controls.records];
  f.usage.open('records'); await settle();
  const row = recordRows(f)[0];
  assert.match(f.text(row.children.find(cell => cell.className.startsWith('usage-record-status'))), /进行中/);
  assert.equal(f.text(row.children.find(cell => cell.className === 'usage-record-total')), '—');
  assert.match(f.text(row.children.find(cell => cell.className === 'usage-record-duration')), /^5\.\d 秒$/);
  assert.match(f.text(f.ids.get('modelUsageCompactSummary')), /1 次 进行中/);
  await row.click(); await tabOf(f, 0, 'events').click();
  assert.match(f.text(detailOf(f)), /等待服务返回/);
  await tabOf(f, 0, 'overview').click();
  assert.match(f.text(detailOf(f)), /调用结束后统计/);
  await tabOf(f, 0, 'response').click();
  let summaries = f.calls.filter(([op]) => op === 'usage-summary').length, records = f.calls.filter(([op]) => op === 'usage-records').length;
  await f.tick();
  assert.equal(f.calls.filter(([op]) => op === 'usage-records').length, records + 1, 'only the first page is re-read');
  assert.equal(f.calls.filter(([op]) => op === 'usage-summary').length, summaries);
  assert.deepEqual(f.traces.refreshed, ['trace-live']);
  f.controls.records = [{ ...running, status: 'ok', totalTokens: 900, durationMs: 6000 }, ...f.controls.records.slice(1)];
  await f.tick();
  assert.ok(f.calls.filter(([op]) => op === 'usage-summary').length > summaries, 'a finished attempt refreshes totals');
  assert.equal(f.text(recordRows(f)[0].children.find(cell => cell.className === 'usage-record-total')), '900');
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'true', 'the open detail follows the finished attempt');
  assert.deepEqual(f.traces.refreshed, ['trace-live', 'trace-live']);
});

test('the status filter can show only in-flight attempts', async () => {
  const f = fixture(); f.usage.open('records'); await settle();
  const labels = f.ids.get('modelUsageStatus').children.map(option => option.textContent);
  assert.deepEqual(labels.slice(0, 2), ['全部状态', '进行中']);
  assert.equal(labels.includes('排队中'), false);
  await f.change('modelUsageStatus', 'active');
  assert.equal(lastCall(f, 'usage-records').status, 'active');
});

test('a record links to every step and retry of its task across the whole history', async () => {
  const f = fixture(); f.usage.open('records'); await settle();
  await f.change('modelUsageRange', '7'); await f.change('modelUsagePlugin', 'kyf.friend-analysis');
  await recordRows(f)[0].click();
  await f.find(f.ids.get('modelUsageRecords'), '查看同一任务的全部步骤与重试').click(); await settle();
  const query = lastCall(f, 'usage-records');
  assert.equal(query.task, 'task-1'); assert.equal(query.since, undefined); assert.equal(query.plugin, undefined);
  assert.equal(f.ids.get('modelUsageRange').value, 'all');
  assert.match(f.text(f.ids.get('modelUsageStep')), /同一任务的全部步骤与重试/);
  await f.find(f.ids.get('modelUsageStep'), '取消任务筛选').click(); await settle();
  assert.equal(lastCall(f, 'usage-records').task, undefined);
  assert.equal(f.ids.get('modelUsageStep').hidden, true);
});

test('records keep dense cells and disclose complete metadata through an accessible button or row click', async () => {
  const f = fixture(); f.usage.open('records'); await settle();
  let row = recordRows(f)[0];
  assert.equal(row.tagName, 'TR');
  assert.equal(row.children.length, 8);
  const friend = row.children.find(cell => cell.className === 'usage-record-friend');
  const total = row.children.find(cell => cell.className === 'usage-record-total');
  assert.match(f.text(friend), /小明/); assert.match(f.text(total), /1,500/);
  assert.equal(f.ids.get('modelUsageRecordToggle0').tagName, 'BUTTON');
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'false');
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-controls'), 'modelUsageRecordDetail0');
  await row.click();
  row = recordRows(f)[0];
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'true');
  const detail = row.parentElement.children[row.parentElement.children.indexOf(row) + 1];
  assert.equal(detail.className, 'model-usage-record-detail');
  assert.equal(detail.hidden, false);
  assert.equal(detail.children[0].colSpan, 8);
  assert.match(f.text(detail), /model-x/); assert.match(f.text(detail), /1,200/); assert.match(f.text(detail), /300/);
  await f.ids.get('modelUsageRecordToggle0').click();
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'false');
  await f.ids.get('modelUsageRecordToggle0').click();
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'true');
  const requests = f.calls.length;
  f.usage.setView('stats'); f.usage.setView('records'); await settle();
  assert.equal(f.ids.get('modelUsageRecordToggle0').getAttribute('aria-expanded'), 'true');
  assert.equal(f.calls.length, requests, 'disclosure and tab changes use already loaded records');
});

test('records show a compact summary and keep coverage and counting rules in the footer', async () => {
  const f = fixture(); f.usage.open(); await settle();
  assert.equal(f.ids.get('modelUsageSummary').hidden, false);
  assert.equal(f.ids.get('modelUsageCompactSummary').hidden, true);
  f.usage.setView('records'); await settle();
  assert.equal(f.ids.get('modelUsageSummary').hidden, true);
  assert.equal(f.ids.get('modelUsageCompactSummary').hidden, false);
  assert.match(f.text(f.ids.get('modelUsageCompactSummary')), /1,500/);
  assert.match(f.text(f.ids.get('modelUsageCompactSummary')), /2/);
  const footer = f.ids.get('modelUsageAccounting').closest('footer');
  assert.ok(footer);
  assert.match(f.text(footer), /总量完整 1 \/ 2 次/);
  assert.match(f.text(footer), /缓存属于输入/);
  for (const id of ['modelUsageClose', 'modelUsageStatsTab', 'modelUsageRecordsTab', 'modelUsageTraces']) assert.equal(f.ids.has(id), false);
});

test('clearing usage asks first and clears only the current account', async () => {
  const f = fixture(); f.usage.open(); await settle();
  await f.ids.get('modelUsageClear').click();
  assert.equal(f.ids.get('modelUsageClearConfirm').hidden, false);
  assert.equal(f.calls.some(([op]) => op === 'usage-clear'), false);
  await f.ids.get('modelUsageCancelClear').click();
  assert.equal(f.ids.get('modelUsageClearConfirm').hidden, true);
  await f.ids.get('modelUsageClear').click(); f.controls.clear = { ok: false };
  await f.ids.get('modelUsageConfirmClear').click();
  assert.match(f.ids.get('modelUsageFeedback').textContent, /清除失败/);
  f.controls.clear = { ok: true }; await f.ids.get('modelUsageConfirmClear').click();
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls.filter(([op]) => op === 'usage-clear').at(-1)[1])), { account: 'account-a', confirm: true });
  assert.equal(f.ids.get('modelUsageClearConfirm').hidden, true);
  assert.match(f.ids.get('modelUsageFeedback').textContent, /已清除/);
});

test('later pages append with the cursor and failures are shown instead of zeros', async () => {
  const f = fixture(); f.controls.next = [123, 'a2']; f.usage.open(); await settle();
  assert.equal(f.ids.get('modelUsageMore').hidden, false);
  f.controls.next = null; await f.ids.get('modelUsageMore').click();
  assert.equal(f.calls.filter(([op]) => op === 'usage-records').at(-1)[1].before, '123,a2');
  assert.equal(recordRows(f).length, 4); assert.equal(f.ids.get('modelUsageMore').hidden, true);
  f.window.desktopHost.modelTracesRequest = async () => ({ ok: false, statusCode: 503 });
  f.usage.reload(); await settle();
  assert.match(f.ids.get('modelUsageError').textContent, /用量读取失败/);
});

test('a delayed clear acknowledgement cannot change another account or its model filter', async () => {
  const f = fixture(); f.usage.open(); await settle();
  const original = f.window.desktopHost.modelTracesRequest;
  let finish;
  f.window.desktopHost.modelTracesRequest = (operation, args) => operation === 'usage-clear'
    ? new Promise(resolve => { finish = resolve; }) : original(operation, args);
  await f.ids.get('modelUsageClear').click();
  const clearing = f.ids.get('modelUsageConfirmClear').click();
  await settle();
  f.usage.setScope('account-b'); await settle();
  await f.change('modelUsageModel', 'model-x');
  finish({ ok: true }); await clearing;
  assert.equal(f.ids.get('modelUsageFeedback').textContent, '');
  assert.equal(f.ids.get('modelUsageModel').value, 'model-x');
  assert.equal(f.ids.get('modelUsageClear').disabled, false);
});

test('without an account nothing is requested; Escape closes the window', async () => {
  const f = fixture({ account: '' }); f.usage.open(); await settle();
  assert.equal(f.calls.some(([op]) => op.startsWith('usage-')), false);
  assert.match(f.ids.get('modelUsageError').textContent, /连接微信账号后/);
  let prevented = false;
  f.keydown[0]({ key: 'Escape', target: {}, preventDefault() { prevented = true; } }); await settle();
  assert.equal(prevented, true); assert.equal(f.calls.filter(([op]) => op === 'close-window').length, 1);
});

test('statistics default to all time and use the shared calendar for every granularity and metric', async () => {
  const f = fixture(); f.usage.open(); await settle();
  assert.equal(f.ids.get('modelUsageStatsView').hidden, false);
  assert.equal(f.ids.get('modelUsageRecordsView').hidden, true);
  assert.equal(f.ids.get('modelUsageRange').value, 'all');
  assert.equal(f.find(f.ids.get('modelUsageTrendControls'), '天').getAttribute('aria-pressed'), 'true');
  assert.equal(f.find(f.ids.get('modelUsageTrendControls'), '总量').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(plain(f.charts.at(-1).options.getSelection()), { anchor: 0, focus: f.charts.at(-1).options.view.count - 1 });
  for (const [unit, label] of f.window.KyfTimeTrend.UNITS) {
    await f.find(f.ids.get('modelUsageTrendControls'), label).click();
    const chart = f.charts.at(-1), bounds = chart.options.view.bounds;
    assert.equal(chart.options.unit, unit);
    assert.equal(bounds[0], f.window.KyfTimeTrend.calendar.start(f.controls.summary.firstAt, unit));
    assert.ok(bounds.at(-2) <= f.controls.summary.cutoffMs && bounds.at(-1) > f.controls.summary.cutoffMs);
    const requests = f.calls.length;
    for (const [field, metric] of [['totalTokens', '总量'], ['totalInputTokens', '输入'], ['totalOutputTokens', '输出'], ['requests', '调用次数']]) {
      await f.find(f.ids.get('modelUsageTrendControls'), metric).click();
      assert.equal(f.find(f.ids.get('modelUsageTrendControls'), metric).getAttribute('aria-pressed'), 'true');
      const sum = f.charts.at(-1).options.series.flatMap(item => item.values).reduce((total, value) => total + value, 0);
      assert.equal(sum, f.controls.summary[field]);
    }
    assert.equal(f.calls.length, requests, 'switching metrics reuses the complete timeline');
  }
});

test('unknown usage is distinct from a truly empty ledger and zero-valued reported counters', async () => {
  const f = fixture();
  const unknown = [f.controls.records[1]], missing = f.aggregate(unknown);
  f.controls.summary = { ...f.controls.summary, ...missing };
  f.controls.records = f.controls.timelineRecords = unknown;
  f.controls.modules = [{ plugin: 'kyf.message-insights', name: '分析与建议', ...missing, steps: [] }];
  f.usage.open(); await settle();
  let tiles = descendants(f.ids.get('modelUsageSummary')).filter(element => element.tagName === 'STRONG').map(element => element.textContent);
  assert.deepEqual(tiles, ['未知', '未知', '未知', '1 次']);
  assert.match(f.text(f.ids.get('modelUsageAccounting')), /总量完整 0 \/ 1 次/);
  assert.match(f.text(f.ids.get('modelUsageModules')), /未知 token/);
  assert.equal(f.charts.at(-1).options.unknown(0), true);
  f.controls.summary = { ...f.controls.summary, ...f.aggregate([]) };
  f.controls.records = f.controls.timelineRecords = f.controls.modules = [];
  f.usage.reload(); await settle();
  tiles = descendants(f.ids.get('modelUsageSummary')).filter(element => element.tagName === 'STRONG').map(element => element.textContent);
  assert.deepEqual(tiles, ['0', '0', '0', '0 次']);
  assert.equal(f.charts.at(-1).options.hasData, false);
  const reportedZero = { ...unknown[0], totalTokens: 0, totalInputTokens: 0, totalOutputTokens: 0 };
  f.controls.summary = { ...f.controls.summary, ...f.aggregate([reportedZero]) };
  f.controls.records = f.controls.timelineRecords = [reportedZero];
  f.usage.reload(); await settle();
  tiles = descendants(f.ids.get('modelUsageSummary')).filter(element => element.tagName === 'STRONG').map(element => element.textContent);
  assert.deepEqual(tiles, ['0', '0', '0', '1 次']);
  assert.match(f.text(f.ids.get('modelUsageAccounting')), /总量完整 1 \/ 1 次/);
});

test('summary, module rankings and chart use whole-ledger aggregates even when a record is on another page', async () => {
  const f = fixture();
  const unpaged = { ...f.controls.records[0], attemptId: 'unpaged', plugin: 'kyf.advisor', step: 'advisor',
    inputTokens: 90000, outputTokens: 10000, totalInputTokens: 90000, totalOutputTokens: 10000, totalTokens: 100000 };
  f.controls.timelineRecords = [...f.controls.records, unpaged];
  f.controls.summary = { ...f.controls.summary, ...f.aggregate(f.controls.timelineRecords) };
  f.controls.modules.unshift({ plugin: 'kyf.advisor', name: '聊天助手', ...f.aggregate([unpaged]), steps: [{ step: 'advisor', ...f.aggregate([unpaged]) }] });
  f.controls.next = [123, 'a2'];
  f.usage.open(); await settle();
  assert.match(f.text(f.ids.get('modelUsageSummary')), /101,500/);
  assert.match(f.text(f.ids.get('modelUsageModules')), /100,000 token/);
  assert.equal(f.charts.at(-1).options.series.flatMap(item => item.values).reduce((total, value) => total + value, 0), 101500);
  assert.equal(recordRows(f).length, 2);
  assert.doesNotMatch(f.text(f.ids.get('modelUsageRecords')), /100,000/);
});

test('date selection scopes aggregates and records, while preview and tab changes preserve the mounted chart', async () => {
  const f = fixture({ scoped: true }); f.usage.open(); await settle();
  const chart = f.charts.at(-1), bounds = chart.options.view.bounds, requestsBefore = f.calls.length;
  await selectRange(f, 0, 0, true);
  assert.equal(f.calls.length, requestsBefore, 'drag previews must not issue aggregate requests');
  await selectRange(f, 0);
  for (const operation of ['usage-summary', 'usage-modules', 'usage-friends', 'usage-records']) {
    assert.equal(lastCall(f, operation).since, String(bounds[0]));
    assert.equal(lastCall(f, operation).until, String(bounds[1]));
  }
  assert.equal(f.calls.filter(([operation]) => operation === 'usage-timeline').length, 1, 'selection reuses the full timeline');
  assert.equal(f.charts.at(-1), chart, 'selection keeps chart scroll and zoom state');
  const selectedCalls = f.calls.length;
  f.usage.setView('records'); await settle();
  assert.equal(f.ids.get('modelUsageStatsView').hidden, true);
  assert.equal(f.ids.get('modelUsageRecordsView').hidden, false);
  f.usage.setView('stats'); await settle();
  assert.equal(f.calls.length, selectedCalls, 'changing tabs retains filtered data');
  assert.deepEqual(plain(chart.options.getSelection()), { anchor: 0, focus: 0 });
  await selectRange(f, 1);
  assert.match(f.text(f.ids.get('modelUsageSummary')), /调用0 次/);
  assert.match(f.text(f.ids.get('modelUsageRecords')), /暂无匹配/);
  await f.find(f.ids.get('modelUsageSelection'), '查看全部范围').click();
  assert.equal(lastCall(f, 'usage-modules').since, undefined);
  assert.equal(lastCall(f, 'usage-records').until, undefined);
  assert.match(f.text(f.ids.get('modelUsageSummary')), /调用2 次/);
  assert.equal(f.charts.at(-1), chart);
});

test('module disclosure and step filtering retain the selected period across tabs and pagination', async () => {
  const f = fixture(); f.controls.next = [123, 'a2']; f.usage.open(); await settle();
  await selectRange(f, 0);
  const selected = { since: lastCall(f, 'usage-records').since, until: lastCall(f, 'usage-records').until };
  const chartsBefore = f.charts.length, requestsBefore = f.calls.length;
  await f.find(f.ids.get('modelUsageModules'), '▸ 知己分析').click();
  assert.equal(f.calls.length, requestsBefore);
  assert.equal(f.charts.length, chartsBefore);
  const step = descendants(f.ids.get('modelUsageModules')).find(element => element.className === 'model-usage-step');
  assert.match(f.text(step), /近期概述/);
  await step.click();
  for (const operation of ['usage-summary', 'usage-modules', 'usage-friends', 'usage-records', 'usage-timeline']) {
    const args = lastCall(f, operation);
    assert.equal(args.plugin, 'kyf.friend-analysis');
    assert.equal(args.step, 'friend-summary');
    if (operation !== 'usage-timeline') { assert.equal(args.since, selected.since); assert.equal(args.until, selected.until); }
    else { assert.equal(args.since, undefined); assert.equal(args.until, undefined); }
  }
  assert.equal(f.ids.get('modelUsageStep').hidden, false);
  assert.match(f.text(f.ids.get('modelUsageStep')), /知己分析 \/ 近期概述/);
  f.usage.setView('records'); await settle();
  await f.ids.get('modelUsageMore').click();
  assert.deepEqual({ ...lastCall(f, 'usage-records') }, { account: 'account-a', plugin: 'kyf.friend-analysis', step: 'friend-summary', ...selected, before: '123,a2', limit: '50' });
  f.usage.setView('stats'); await settle();
  await f.find(f.ids.get('modelUsageStep'), '取消步骤筛选').click();
  assert.equal(lastCall(f, 'usage-records').step, undefined);
  assert.equal(lastCall(f, 'usage-records').plugin, 'kyf.friend-analysis');
  assert.equal(lastCall(f, 'usage-records').since, selected.since);
  assert.equal(f.ids.get('modelUsageStep').hidden, true);
});

test('minute drill-down uses complete bounded calendar requests and returns to the chosen granularity', async () => {
  const f = fixture(); f.usage.open(); await settle();
  await selectRange(f, 0);
  const dayStart = Number(lastCall(f, 'usage-records').since);
  const before = f.calls.filter(([operation]) => operation === 'usage-timeline').length;
  await f.find(f.ids.get('modelUsageSelection'), '按分钟查看这一天').click();
  const chart = f.charts.at(-1), requests = f.calls.filter(([operation]) => operation === 'usage-timeline').slice(before);
  assert.equal(chart.options.unit, 'minute');
  assert.equal(chart.options.view.bounds[0], dayStart);
  assert.equal(chart.options.view.bounds.at(-1), f.window.KyfTimeTrend.calendar.next(dayStart, 'day'));
  assert.equal(chart.options.view.count, 1440);
  assert.equal(requests.length, 2);
  for (const [, args] of requests) assert.ok(args.boundaries.split(',').length <= 1001);
  assert.equal(requests[0][1].boundaries.split(',').at(-1), requests[1][1].boundaries.split(',')[0]);
  assert.equal(f.find(f.ids.get('modelUsageTrendControls'), '天'), undefined);
  await f.find(f.ids.get('modelUsageTrendControls'), '返回').click();
  assert.equal(f.charts.at(-1).options.unit, 'day');
  assert.equal(f.find(f.ids.get('modelUsageTrendControls'), '天').getAttribute('aria-pressed'), 'true');
});

test('all-time daily history is complete across multiple timeline chunks', async () => {
  const f = fixture();
  const oldest = { ...f.controls.records[0], attemptId: 'oldest-unpaged', at: Date.UTC(2021, 9, 13, 6),
    totalInputTokens: 800, totalOutputTokens: 200, totalTokens: 1000, inputTokens: 800, outputTokens: 200 };
  f.controls.timelineRecords = [...f.controls.records, oldest];
  f.controls.summary = { ...f.controls.summary, ...f.aggregate(f.controls.timelineRecords) };
  f.usage.open(); await settle();
  const chart = f.charts.at(-1), timelineRequests = f.calls.filter(([operation]) => operation === 'usage-timeline');
  assert.ok(chart.options.view.count > 1000);
  assert.equal(timelineRequests.length, 2);
  assert.equal(chart.options.view.bounds[0], f.window.KyfTimeTrend.calendar.start(oldest.at, 'day'));
  assert.equal(timelineRequests[0][1].boundaries.split(',').at(-1), timelineRequests[1][1].boundaries.split(',')[0]);
  const values = chart.options.series.find(item => item.plugin === 'kyf.friend-analysis').values;
  assert.equal(values[0], 1000);
  assert.equal(values.reduce((total, value) => total + value, 0), 2500);
  assert.equal(recordRows(f).length, 2, 'oldest usage is included before its record page is loaded');
});

test('unclassified steps use the explicit null-step filter and can be cancelled', async () => {
  const f = fixture(); f.usage.open(); await settle();
  await f.find(f.ids.get('modelUsageModules'), '▸ 分析与建议').click();
  const step = descendants(f.ids.get('modelUsageModules')).find(element => element.className === 'model-usage-step');
  assert.match(f.text(step), /未分类步骤/);
  await step.click();
  assert.equal(lastCall(f, 'usage-modules').plugin, 'kyf.message-insights');
  assert.equal(lastCall(f, 'usage-modules').step, '__none__');
  await f.find(f.ids.get('modelUsageStep'), '取消步骤筛选').click();
  assert.equal(lastCall(f, 'usage-records').step, undefined);
  assert.equal(lastCall(f, 'usage-records').plugin, 'kyf.message-insights');
});

test('late usage responses from another account cannot replace the current statistics, chart or filters', async () => {
  const f = fixture(), original = f.window.desktopHost.modelTracesRequest;
  const pending = [];
  f.window.desktopHost.modelTracesRequest = (operation, args) => args.account === 'account-a'
    ? new Promise(resolve => pending.push(() => resolve({ ok: true, ...f.controls.summary,
      modules: [{ plugin: 'old-private', name: '旧账号模块' }], records: [{ plugin: 'old-private' }], friends: [] })))
    : original(operation, args);
  f.usage.open(); await settle();
  assert.ok(pending.length);
  f.usage.setScope('account-b'); await settle();
  await f.change('modelUsageModel', 'model-x');
  const chart = f.charts.at(-1), callsBefore = f.calls.length;
  pending.forEach(finish => finish()); await settle();
  assert.equal(f.ids.get('modelUsageModel').value, 'model-x');
  assert.equal(f.charts.at(-1), chart);
  assert.equal(f.calls.length, callsBefore, 'discarding a stale extent does not request its timeline');
  assert.doesNotMatch(f.text(f.ids.get('modelUsagePanel')), /旧账号模块|old-private/);
  assert.equal(lastCall(f, 'usage-summary').account, 'account-b');
});

test('switching granularity keeps query bounds aligned with the period displayed by the chart', async () => {
  const f = fixture({ scoped: true });
  const anotherDay = { ...f.controls.records[0], attemptId: 'another-day', at: Date.UTC(2026, 9, 14, 5),
    totalInputTokens: 500, totalOutputTokens: 100, totalTokens: 600, inputTokens: 500, outputTokens: 100 };
  f.controls.timelineRecords.push(anotherDay); f.controls.records = [...f.controls.timelineRecords];
  f.usage.open(); await settle();
  await selectRange(f, 0);
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token1,500/);
  await f.find(f.ids.get('modelUsageTrendControls'), '月').click();
  const chart = f.charts.at(-1), selection = chart.options.getSelection(), query = lastCall(f, 'usage-records');
  assert.equal(chart.options.unit, 'month');
  if (query.until !== undefined) {
    assert.equal(query.since, String(chart.options.view.bounds[Math.min(selection.anchor, selection.focus)]));
    assert.equal(query.until, String(chart.options.view.bounds[Math.max(selection.anchor, selection.focus) + 1]));
  } else assert.match(f.text(f.ids.get('modelUsageSelection')), /当前时间范围全部调用/);
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token2,100/);
});

test('selecting a chart during a pending filter change cannot commit the previous filter timeline', async () => {
  const f = fixture({ scoped: true }); f.usage.open(); await settle();
  const original = f.window.desktopHost.modelTracesRequest, before = f.charts.at(-1);
  let finish, blocked = false;
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    if (!blocked && operation === 'usage-summary' && args.plugin === 'kyf.friend-analysis' && !args.until) {
      blocked = true;
      return new Promise(resolve => { finish = () => resolve(response); });
    }
    return response;
  };
  await f.change('modelUsagePlugin', 'kyf.friend-analysis');
  assert.ok(finish, 'the new filter extent is still pending');
  if (!before.destroyed) await selectRange(f, 0);
  finish(); await settle();
  const chart = f.charts.at(-1);
  assert.equal(chart.destroyed, undefined);
  assert.deepEqual(plain(chart.options.series.map(item => item.plugin)), ['kyf.friend-analysis']);
  assert.equal(lastCall(f, 'usage-modules').plugin, 'kyf.friend-analysis');
});

test('resetting a minute selection and returning keeps aggregates scoped to the opened day', async () => {
  const f = fixture({ scoped: true });
  const anotherDay = { ...f.controls.records[0], attemptId: 'another-day', at: Date.UTC(2026, 9, 14, 5),
    totalInputTokens: 500, totalOutputTokens: 100, totalTokens: 600, inputTokens: 500, outputTokens: 100 };
  f.controls.timelineRecords.push(anotherDay); f.controls.records = [...f.controls.timelineRecords];
  f.usage.open(); await settle();
  await selectRange(f, 0);
  const dayStart = Number(lastCall(f, 'usage-records').since), dayEnd = Number(lastCall(f, 'usage-records').until);
  await f.find(f.ids.get('modelUsageSelection'), '按分钟查看这一天').click();
  assert.equal(f.find(f.ids.get('modelUsageSelection'), '查看全部范围'), undefined, 'already viewing the whole opened day');
  const index = Math.floor((f.controls.records[0].at - dayStart) / 60000);
  await selectRange(f, index);
  await f.find(f.ids.get('modelUsageSelection'), '查看全部范围').click();
  assert.equal(f.charts.at(-1).options.unit, 'minute');
  for (const operation of ['usage-summary', 'usage-modules', 'usage-friends', 'usage-records']) {
    assert.equal(lastCall(f, operation).since, String(dayStart));
    assert.equal(lastCall(f, operation).until, String(dayEnd));
  }
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token1,500/);
  assert.equal(f.find(f.ids.get('modelUsageSelection'), '查看全部范围'), undefined);
  await selectRange(f, index);
  await f.find(f.ids.get('modelUsageTrendControls'), '返回').click();
  assert.equal(f.charts.at(-1).options.unit, 'day');
  assert.equal(lastCall(f, 'usage-records').since, String(dayStart));
  assert.equal(lastCall(f, 'usage-records').until, String(dayEnd));
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token1,500/);
});

test('live refresh preserves the selected period and pauses for extra pages, hidden documents and clearing', async () => {
  const f = fixture(); f.controls.next = [123, 'a2']; f.usage.open(); await settle();
  await selectRange(f, 0);
  const selected = { since: lastCall(f, 'usage-records').since, until: lastCall(f, 'usage-records').until };
  let calls = f.calls.length;
  await f.tick();
  assert.ok(f.calls.length > calls);
  assert.equal(lastCall(f, 'usage-records').since, selected.since);
  assert.equal(lastCall(f, 'usage-records').until, selected.until);
  await f.ids.get('modelUsageMore').click();
  calls = f.calls.length;
  await f.tick(); await f.windowEvent('focus');
  assert.equal(f.calls.length, calls, 'refresh must preserve the extra record pages already loaded');
  f.usage.reload(); await settle();
  await f.ids.get('modelUsageClear').click();
  calls = f.calls.length;
  await f.tick(); await f.windowEvent('focus');
  assert.equal(f.calls.length, calls, 'the clear confirmation is not interrupted by polling');
  await f.ids.get('modelUsageCancelClear').click();
  f.window.document.hidden = true;
  await f.tick();
  assert.equal(f.calls.length, calls);
  f.window.document.hidden = false;
  await f.tick();
  assert.ok(f.calls.length > calls);
});

test('polling pauses during selection, discards an in-flight refresh, and resumes after release', async () => {
  const f = fixture(); f.usage.open(); await settle();
  const chart = f.charts.at(-1), original = f.window.desktopHost.modelTracesRequest;
  f.controls.selecting = true;
  let calls = f.calls.length;
  await f.tick(); await f.windowEvent('focus');
  assert.equal(f.calls.length, calls, 'timer and focus refreshes do not read during a chart gesture');
  await selectRange(f, 0);
  assert.ok(f.calls.length > calls, 'an explicit selection commit still reads its aggregate details');
  assert.equal(f.charts.at(-1), chart);
  f.controls.selecting = false;
  let finish, blocked = false;
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    if (!blocked && operation === 'usage-summary' && args.until === undefined) {
      blocked = true;
      return new Promise(resolve => { finish = () => resolve(response); });
    }
    return response;
  };
  calls = f.calls.length;
  await f.tick();
  assert.ok(finish, 'a periodic refresh is waiting for its extent');
  assert.equal(f.calls.length, calls + 1);
  f.controls.selecting = true;
  const newCall = { ...f.controls.records[0], attemptId: 'arrived-during-selection', totalTokens: 100,
    totalInputTokens: 80, totalOutputTokens: 20, inputTokens: 80, outputTokens: 20 };
  f.controls.timelineRecords = [...f.controls.timelineRecords, newCall];
  f.controls.summary = { ...f.controls.summary, ...f.aggregate(f.controls.timelineRecords) };
  finish(); await settle();
  assert.equal(f.calls.length, calls + 1, 'a refresh that became stale during dragging does not start aggregate or timeline requests');
  assert.equal(f.charts.at(-1), chart);
  assert.equal(chart.destroyed, undefined, 'the active gesture keeps its mounted canvas');
  assert.equal(f.ids.get('modelUsageLoading').hidden, true);
  f.controls.selecting = false;
  await f.tick();
  assert.ok(f.calls.length > calls + 1);
  const refreshed = f.charts.at(-1);
  assert.notEqual(refreshed, chart, 'new usage is rendered after the gesture ends');
  assert.equal(refreshed.options.series.flatMap(item => item.values).reduce((total, value) => total + value, 0), 1600);
  assert.deepEqual(plain(refreshed.options.getSelection()), { anchor: 0, focus: 0 });
});

test('a slow friend response does not gate token statistics and survives loading another records page', async () => {
  const f = fixture(); f.controls.next = [123, 'a2'];
  const original = f.window.desktopHost.modelTracesRequest;
  let finish;
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    if (operation === 'usage-friends') return new Promise(resolve => { finish = () => resolve(response); });
    return response;
  };
  f.usage.open(); await settle();
  assert.ok(finish);
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token1,500/);
  assert.equal(f.charts.length, 1, 'the timeline is visible without waiting for friend names');
  assert.equal(f.ids.get('modelUsageLoading').hidden, true);
  assert.match(f.text(f.ids.get('modelUsageFriends')), /正在更新好友汇总/);
  await f.ids.get('modelUsageMore').click();
  finish(); await settle();
  assert.match(f.text(f.ids.get('modelUsageFriends')), /小明/);
  assert.doesNotMatch(f.text(f.ids.get('modelUsageFriends')), /正在更新/);
});

test('background refreshes keep the panel still when the usage is unchanged', async () => {
  // Added 2026-10-18: the 10-second refresh showed loading lines that pushed both tabs around.
  const f = fixture(); f.controls.next = [123, 'a2'];
  f.usage.open(); await settle();
  const table = f.ids.get('modelUsageRecords').children[0], friend = f.ids.get('modelUsageFriends').children[0], chart = f.charts.at(-1);
  const original = f.window.desktopHost.modelTracesRequest, pending = new Map();
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    if (operation !== 'usage-summary' && operation !== 'usage-friends') return response;
    return new Promise(resolve => { pending.set(operation, () => resolve(response)); });
  };
  f.controls.summary.cutoffMs += 10000;
  await f.tick();
  assert.ok(pending.has('usage-summary'), 'the refresh is waiting for its totals');
  assert.equal(f.ids.get('modelUsageLoading').hidden, true);
  assert.equal(f.ids.get('modelUsageMore').disabled, false);
  pending.get('usage-summary')(); await settle();
  assert.ok(pending.has('usage-friends'), 'friend totals are still on their way');
  assert.equal(f.ids.get('modelUsageFriends').children[0], friend, 'the previous friend totals stay until new ones arrive');
  pending.get('usage-friends')(); await settle();
  assert.equal(f.ids.get('modelUsageRecords').children[0], table, 'a new cutoff alone does not rebuild the records');
  assert.equal(f.ids.get('modelUsageFriends').children[0], friend);
  assert.equal(f.charts.at(-1), chart);
  f.window.desktopHost.modelTracesRequest = original;
  await f.change('modelUsageRange', 'today');
  assert.notEqual(f.ids.get('modelUsageRecords').children[0], table, 'a filter change still renders');
});

test('friend-only failure does not hide valid token totals or the timeline', async () => {
  const f = fixture(), original = f.window.desktopHost.modelTracesRequest;
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    return operation === 'usage-friends' ? { ok: false } : response;
  };
  f.usage.open(); await settle();
  assert.match(f.text(f.ids.get('modelUsageSummary')), /已知总 token1,500/);
  assert.equal(f.charts.length, 1);
  assert.equal(f.ids.get('modelUsageError').hidden, true);
  assert.match(f.text(f.ids.get('modelUsageFriends')), /好友汇总读取失败/);
});

test('a late friend response cannot replace names from a newer filter scope', async () => {
  const f = fixture(), original = f.window.desktopHost.modelTracesRequest;
  let finish;
  f.window.desktopHost.modelTracesRequest = async (operation, args) => {
    const response = await original(operation, args);
    if (operation === 'usage-friends' && !args.plugin) return new Promise(resolve => { finish = () => resolve(response); });
    return response;
  };
  f.usage.open(); await settle();
  f.controls.friends = [{ ...f.controls.friends[0], name: '新范围昵称' }];
  await f.change('modelUsagePlugin', 'kyf.friend-analysis');
  assert.match(f.text(f.ids.get('modelUsageFriends')), /新范围昵称/);
  finish(); await settle();
  assert.match(f.text(f.ids.get('modelUsageFriends')), /新范围昵称/);
  assert.doesNotMatch(f.text(f.ids.get('modelUsageFriends')), /小明/);
});
