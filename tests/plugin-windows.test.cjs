// Modified by Know Your Friends, 2026-10-09: shared portrait/assistant dock regression coverage.
// Moved from tests/advisor-companion.test.cjs to tests/plugin-panel.test.cjs on 2026-10-13: the docked view is the
// generic plugin panel (scripts/plugin-panel.cjs, its preload, chatui/plugin-panel.js) and the main-window preload.
// Moved to tests/plugin-windows.test.cjs on 2026-10-22: chat-toolbar pages open in one window per friend
// (scripts/plugin-windows.cjs) instead of the dock; the cases of tests/assistant-window.test.cjs (the single
// pinned assistant window it replaces) are kept here.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test, mock } = require('node:test');
const { createPluginWindows, validTarget, windowScope } = require('../scripts/plugin-windows.cjs');
const CLIENT = 'http://127.0.0.1:45678/';
const TARGET = { plugin: 'kyf.advisor', page: 'ui/panel.html' };
const HOST = CLIENT + 'plugin-panel.html?plugin=kyf.advisor&page=ui%2Fpanel.html&independent=1';
const plain = value => JSON.parse(JSON.stringify(value));
const friend = (user, extra = {}) => ({ ...TARGET, account: 'a', user, name: user.toUpperCase(), label: '聊天助手', ...extra });

function fixture(area = { x: 0, y: 0, width: 1800, height: 1000 }) {
  const windows = [], handlers = new Map(), ipcMain = new EventEmitter(), copied = [];
  ipcMain.handle = (name, fn) => handlers.set(name, fn); ipcMain.removeHandler = name => handlers.delete(name);
  function contents() {
    const wc = new EventEmitter(); wc.mainFrame = { url: CLIENT }; wc.sent = []; wc.focused = false; wc.loads = []; wc.session = {};
    wc.send = (name, ...values) => wc.sent.push({ name, value: values.length > 1 ? values : values[0] }); wc.setWindowOpenHandler = () => {};
    wc.loadURL = url => { wc.mainFrame.url = url; wc.loads.push(url); return Promise.resolve(); };
    wc.isFocused = () => wc.focused; wc.isDestroyed = () => false; return wc;
  }
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.webContents = contents(); this.title = options.title; this.background = options.backgroundColor;
      this.visible = false; this.minimized = false; this.destroyed = false; this.bounds = { x: options.x, y: options.y, width: options.width, height: options.height };
      windows.push(this);
    }
    loadURL(url) { return this.webContents.loadURL(url); }
    getBounds() { return { ...this.bounds }; } setTitle(value) { this.title = value; } setBackgroundColor(value) { this.background = value; }
    isDestroyed() { return this.destroyed; } isVisible() { return this.visible; } isMinimized() { return this.minimized; } isMaximized() { return false; }
    show() { this.visible = true; this.emit('show'); } hide() { this.visible = false; this.emit('hide'); }
    minimize() { this.minimized = true; this.emit('minimize'); } restore() { this.minimized = false; this.emit('restore'); }
    focus() { this.webContents.focused = true; }
    close() { let prevented = false; this.emit('close', { preventDefault() { prevented = true; } }); if (!prevented) this.destroy(); }
    destroy() { if (this.destroyed) return; this.destroyed = true; this.visible = false; this.emit('closed'); }
  }
  const main = new EventEmitter(); main.bounds = { x: 40, y: 20, width: 800, height: 700 }; main.destroyed = false; main.focused = 0;
  main.webContents = contents(); main.getBounds = () => ({ ...main.bounds }); main.isDestroyed = () => main.destroyed;
  main.isMinimized = () => false; main.restore = () => {}; main.show = () => {}; main.focus = () => { main.focused++; };
  const screen = { getDisplayMatching: () => ({ workArea: area }), getAllDisplays: () => [{ workArea: area }] };
  const event = own => ({ sender: own.webContents, senderFrame: own.webContents.mainFrame });
  const manager = createPluginWindows({ BrowserWindow: Window, ipcMain, screen, mainWindow: main, clientUrl: CLIENT,
    clipboard: { writeText: text => copied.push(text) }, trustedMainFrame: e => e.sender === main.webContents && e.senderFrame === main.webContents.mainFrame });
  const open = value => handlers.get('plugin-panel:open')(event(main), value);
  const ready = own => { own.emit('ready-to-show'); own.webContents.emit('did-finish-load'); };
  const toPage = own => own.webContents.sent.filter(item => item.name === 'plugin-panel:to-panel').map(item => item.value);
  const states = () => main.webContents.sent.filter(item => item.name === 'plugin-panel:state').map(item => item.value);
  const send = (channel, ...values) => ipcMain.emit(channel, event(main), ...values);
  return { main, windows, handlers, ipcMain, manager, copied, event, open, ready, toPage, states, send };
}

test('each friend gets an own window; the same friend comes back to the front', () => {
  const f = fixture();
  assert.deepEqual(f.open(friend('u1')), { opened: true }); const first = f.windows[0];
  assert.equal(first.title, 'U1 · 聊天助手'); assert.deepEqual(first.webContents.loads, [HOST]);
  const preferences = first.options.webPreferences;
  assert(preferences.preload.endsWith('plugin-panel-preload.cjs'));
  assert.equal(preferences.sandbox, true); assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.nodeIntegration, false); assert.equal(preferences.webSecurity, true);
  assert.equal(preferences.session, f.main.webContents.session);
  assert.equal(first.bounds.x, 840, 'beside the main window where the dock used to be');
  f.ready(first); assert.equal(first.visible, true);
  assert.deepEqual(f.open(friend('u2')), { opened: true }); const second = f.windows[1]; f.ready(second);
  assert.equal(f.windows.length, 2); assert.notDeepEqual(second.bounds, first.bounds, 'a new window cascades');
  assert.deepEqual(plain(f.states().at(-1)), { windows: [
    { plugin: 'kyf.advisor', account: 'a', user: 'u1', visible: true }, { plugin: 'kyf.advisor', account: 'a', user: 'u2', visible: true }] });
  first.webContents.focused = false; first.minimize();
  assert.deepEqual(f.open(friend('u1', { name: '改名' })), { opened: true });
  assert.equal(f.windows.length, 2); assert.equal(first.minimized, false); assert.equal(first.webContents.focused, true);
  assert.equal(first.title, '改名 · 聊天助手'); assert.deepEqual(plain(f.toPage(first).at(-1)), { event: { type: 'visibility', visible: true } });
  assert.equal(f.manager.count(), 2); f.manager.close();
});

test('targets name a plugin page by relative path only; a window keeps only its friend\'s identity', () => {
  assert.equal(validTarget(TARGET), true);
  for (const page of ['../x.html', '/abs.html', 'ui/../x.html', 'ui//x.html', 'C:/x.html', 'a b.html', ''])
    assert.equal(validTarget({ plugin: 'kyf.advisor', page }), false, page);
  assert.equal(validTarget({ plugin: '../x', page: 'ui/panel.html' }), false);
  // Ported from tests/assistant-window.test.cjs: identity is bounded and caller-provided capabilities are dropped.
  assert.deepEqual(windowScope({ ...TARGET, account: 'a', user: 'u', role: 'admin', surface: 'forged' }),
    { ...TARGET, account: 'a', user: 'u', name: 'u', label: '插件', theme: 'dark' });
  for (const value of [null, [], {}, { ...TARGET, account: '', user: 'u' }, { ...TARGET, account: 'a', user: ' ' },
    { ...TARGET, account: 'a\n', user: 'u' }, { ...TARGET, account: 'a', user: 'x'.repeat(257) },
    { ...TARGET, account: 'a', user: 'u', name: 'x'.repeat(129) }]) assert.equal(windowScope(value), null);
  const f = fixture();
  assert.deepEqual(f.open({ plugin: 'kyf.advisor', page: '../../x.html', account: 'a', user: 'u' }), { opened: false });
  assert.deepEqual(f.handlers.get('plugin-panel:open')({ sender: {}, senderFrame: {} }, friend('u1')), { opened: false });
  assert.equal(f.windows.length, 0); f.manager.close();
});

test('a window fits the current work area even on a smaller display', () => {
  const f = fixture({ x: -800, y: 80, width: 390, height: 450 }); f.open(friend('u1'));
  const { options } = f.windows[0];
  assert.deepEqual([options.x, options.y, options.width, options.height], [-800, 80, 390, 450]);
  assert.deepEqual([options.minWidth, options.minHeight], [360, 420]); f.manager.close();
});

test('the friend context and its queued events reach only that friend\'s window, after the page loads', () => {
  const f = fixture(); f.open(friend('u1')); f.open(friend('u2')); const [one, two] = f.windows;
  for (let index = 0; index < 20; index++) f.send('plugin-panel:event', { target: { plugin: 'kyf.advisor', account: 'a', user: 'u1' }, event: { type: 'quote', index } });
  f.ipcMain.emit('plugin-panel:event', f.event(one), { target: { plugin: 'kyf.advisor', account: 'a', user: 'u1' }, event: { type: 'forged' } });
  f.send('plugin-panel:event', { target: { plugin: 'kyf.advisor', account: 'a', user: 'u1' }, event: { type: 'quote', text: 'x'.repeat(4 * 1024 * 1024) } });
  assert.equal(f.toPage(one).length, 0, 'nothing is sent before the page has loaded');
  f.ready(one); f.ready(two);
  const sent = f.toPage(one);
  assert.deepEqual(plain(sent[0]), { context: { account: 'a', user: 'u1', name: 'U1', theme: 'dark' } });
  assert.equal(sent.length, 17); assert.equal(sent[1].event.index, 4); assert.equal(sent.at(-1).event.index, 19);
  assert.deepEqual(plain(f.toPage(two)), [{ context: { account: 'a', user: 'u2', name: 'U2', theme: 'dark' } }]);
  f.send('plugin-panel:event', { target: { plugin: 'kyf.other', account: 'a', user: 'u2' }, event: { type: 'quote' } });
  assert.equal(f.toPage(two).length, 1, 'another plugin\'s events stay out');
  f.manager.setTheme('light');
  assert.equal(one.background, '#ffffff'); assert.equal(f.toPage(two).at(-1).context.theme, 'light');
  f.manager.close();
});

test('closing hides the window and keeps its page for an instant reopen; only the latest few stay', () => {
  const f = fixture(); f.open(friend('u1')); const own = f.windows[0]; f.ready(own);
  own.minimize();
  assert.deepEqual(plain(f.toPage(own).at(-1)), { event: { type: 'visibility', visible: false } });
  assert.equal(f.states().at(-1).windows[0].visible, false);
  own.restore(); assert.deepEqual(plain(f.toPage(own).at(-1)), { event: { type: 'visibility', visible: true } });
  own.close();
  assert.equal(own.destroyed, false); assert.equal(own.visible, false);
  assert.deepEqual(plain(f.toPage(own).at(-1)), { event: { type: 'visibility', visible: false } }, 'the page saves its draft');
  assert.deepEqual(plain(f.states().at(-1)), { windows: [] }); assert.equal(f.manager.count(), 0);
  f.send('plugin-panel:event', { target: { plugin: 'kyf.advisor', account: 'a', user: 'u1' }, event: { type: 'quote' } });
  assert.equal(f.toPage(own).at(-1).event.type, 'visibility', 'a closed window receives no events');
  f.open(friend('u1'));
  assert.equal(f.windows.length, 1); assert.equal(own.visible, true); assert.equal(own.webContents.loads.length, 1, 'the same page comes back');
  for (const user of ['u2', 'u3', 'u4', 'u5']) { f.open(friend(user)); f.ready(f.windows.at(-1)); }
  for (const own of f.windows) own.close();
  assert.deepEqual(f.windows.map(own => own.destroyed), [true, true, false, false, false], 'the three latest closed stay');
  f.ipcMain.emit('plugin-panel:hide', { sender: {}, senderFrame: {} });
  f.open(friend('u5')); const last = f.windows[4]; assert.equal(last.visible, true);
  f.ipcMain.emit('plugin-panel:hide', f.event(last)); assert.equal(last.visible, false); assert.equal(last.destroyed, false);
  f.manager.close(); assert(f.windows.every(own => own.destroyed));
});

test('host calls are pinned to the window\'s friend; unknown, oversized and cross-contact calls are refused', () => {
  const f = fixture(); f.open(friend('u1')); f.open(friend('u2')); const [one, two] = f.windows; f.ready(one); f.ready(two);
  const calls = () => f.main.webContents.sent.filter(item => item.name === 'plugin-panel:host-call').map(item => item.value);
  const results = own => own.webContents.sent.filter(item => item.name === 'plugin-panel:host-result').map(item => item.value);
  f.ipcMain.emit('plugin-panel:host-call', { sender: {}, senderFrame: {} }, { id: 'p1', method: 'toast', params: {} });
  f.ipcMain.emit('plugin-panel:host-call', f.event(one), { id: 'p1', method: 'attention.seen', params: { user: 'u1', x: 1 }, scope: { user: 'u2' }, plugin: 'forged' });
  f.ipcMain.emit('plugin-panel:host-call', f.event(two), { id: 'p1', method: 'toast' });
  const [forwarded, other] = calls();
  assert.notEqual(forwarded.id, 'p1'); assert(forwarded.id.length <= 64); assert.notEqual(forwarded.id, other.id);
  assert.deepEqual(plain({ ...forwarded, id: undefined }), { plugin: 'kyf.advisor', method: 'attention.seen', params: { user: 'u1', x: 1 },
    independent: true, scope: { account: 'a', user: 'u1', name: 'U1' } });
  assert.deepEqual(plain(other.scope), { account: 'a', user: 'u2', name: 'U2' });
  for (const call of [{ id: 'p2', method: 'settings.open' }, { id: 'p2', method: 'toast', params: { user: 'u2' } },
    { id: 'p2', method: 'toast', params: { account: 'b' } }, { id: 'p2', method: 'toast', params: { text: 'x'.repeat(4 * 1024 * 1024) } }])
    f.ipcMain.emit('plugin-panel:host-call', f.event(one), call);
  assert.equal(calls().length, 2);
  f.ipcMain.emit('plugin-panel:host-result', f.event(one), forwarded.id, { result: { ok: true } });
  f.send('plugin-panel:host-result', 'p1', { result: { ok: true } });
  assert.equal(results(one).length, 0, 'only the main window answers, by the forwarded id');
  f.send('plugin-panel:host-result', forwarded.id, { result: { ok: true } });
  assert.deepEqual(plain(results(one)), [['p1', { result: { ok: true } }]]); assert.equal(results(two).length, 0);
  f.send('plugin-panel:host-result', forwarded.id, { result: false }); assert.equal(results(one).length, 1, 'an answer is delivered once');
  f.manager.close();
});

test('pending calls are bounded and fail with their window or a reloaded main window; focus follows a successful reply', () => {
  const f = fixture(); f.open(friend('u1')); const own = f.windows[0]; f.ready(own);
  const calls = () => f.main.webContents.sent.filter(item => item.name === 'plugin-panel:host-call').map(item => item.value);
  const results = () => own.webContents.sent.filter(item => item.name === 'plugin-panel:host-result').map(item => item.value);
  f.ipcMain.emit('plugin-panel:host-call', f.event(own), { id: 'f1', method: 'view.focusChat' });
  f.send('plugin-panel:host-result', calls().at(-1).id, { error: { code: 'refused' } }); assert.equal(f.main.focused, 0);
  f.ipcMain.emit('plugin-panel:host-call', f.event(own), { id: 'f2', method: 'view.focusChat' });
  assert.equal(f.main.focused, 0, 'not before the main window has switched');
  f.send('plugin-panel:host-result', calls().at(-1).id, { result: { ok: true } }); assert.equal(f.main.focused, 1);
  for (let index = 0; index < 17; index++) f.ipcMain.emit('plugin-panel:host-call', f.event(own), { id: 'p' + index, method: 'toast' });
  assert.equal(calls().length, 2 + 16, 'at most 16 calls wait for the main window');
  f.main.webContents.emit('did-start-navigation', {}, CLIENT, true, true); assert.equal(results().length, 2);
  f.main.webContents.emit('did-start-navigation', {}, CLIENT, false, true);
  assert.equal(results().length, 18); assert.equal(results().at(-1)[1].error.code, 'host-timeout');
  assert.equal(own.destroyed, false, 'a main reload keeps the friend\'s window');
  f.ipcMain.emit('plugin-panel:host-call', f.event(own), { id: 'late', method: 'toast' });
  own.close(); assert.equal(results().at(-1)[0], 'late'); assert.equal(results().at(-1)[1].error.code, 'context-changed');
  const asked = calls().length; f.ipcMain.emit('plugin-panel:host-call', f.event(own), { id: 'hidden', method: 'toast' });
  assert.equal(calls().length, asked, 'a closed window asks nothing');
  f.manager.close();
});

test('copy needs the window page, a gesture, focus and a visible window', () => {
  const f = fixture(); f.open(friend('u1')); const own = f.windows[0]; f.ready(own); const copy = f.handlers.get('plugin-panel:copy');
  const subframe = { sender: own.webContents, senderFrame: { url: own.webContents.mainFrame.url } };
  assert.equal(copy(f.event(f.main), 'x', true), false); assert.equal(copy(subframe, 'x', true), false);
  own.webContents.focused = false; assert.equal(copy(f.event(own), 'suggestion', true), false);
  own.webContents.focused = true; assert.equal(copy(f.event(own), 'suggestion', false), false);
  own.minimize(); assert.equal(copy(f.event(own), 'suggestion', true), false); own.restore();
  for (const value of ['', '  ', 'x'.repeat(1_000_001), {}, null]) assert.equal(copy(f.event(own), value, true), false);
  assert.equal(copy(f.event(own), 'suggestion', true), true); assert.deepEqual(f.copied, ['suggestion']);
  own.close(); assert.equal(copy(f.event(own), 'hidden', true), false);
  f.manager.close();
});

test('a window and its frame navigate only to the host page and its own plugin page', () => {
  const f = fixture(); f.open(friend('u1')); const own = f.windows[0], wc = own.webContents;
  const navigate = (name, payload) => { let prevented = false; wc.emit(name, { preventDefault() { prevented = true; }, ...payload }, payload?.url); return prevented; };
  assert.equal(navigate('will-navigate', { url: CLIENT + 'index.html' }), true);
  assert.equal(navigate('will-navigate', { url: CLIENT + 'plugin-panel.html?plugin=a&page=b.html' }), false);
  assert.equal(navigate('will-attach-webview', {}), true);
  assert.equal(navigate('will-frame-navigate', { isMainFrame: false, url: CLIENT + 'plugin-pages/kyf.advisor/ui/panel.html' }), false);
  assert.equal(navigate('will-frame-navigate', { isMainFrame: true, url: CLIENT + 'plugin-pages/kyf.advisor/ui/panel.html' }), true);
  assert.equal(navigate('will-frame-navigate', { isMainFrame: false, url: CLIENT + 'plugin-pages/kyf.advisor/ui/repository.html' }), true);
  assert.equal(navigate('will-frame-navigate', { isMainFrame: false, url: CLIENT + 'plugin-pages/kyf.other/ui/panel.html' }), true);
  assert.equal(navigate('will-frame-navigate', { isMainFrame: false, url: 'https://example.invalid/' }), true);
  let titled = false; own.emit('page-title-updated', { preventDefault() { titled = true; } }); assert.equal(titled, true, 'the window keeps the friend\'s title');
  f.manager.close();
});

test('removed contributions, another account and a cleared account close their windows', () => {
  const f = fixture();
  f.open(friend('u1')); f.open(friend('u2')); f.open({ ...friend('u3'), account: 'b' }); f.open({ ...friend('u1'), plugin: 'kyf.other' });
  f.ipcMain.emit('plugin-panel:retain', f.event(f.windows[0]), { targets: [], account: '' });
  assert.equal(f.manager.count(), 4, 'only the main window decides');
  f.send('plugin-panel:retain', { targets: [TARGET], account: '' });
  assert.equal(f.windows[3].destroyed, true); assert.equal(f.manager.count(), 3);
  f.send('plugin-panel:retain', { targets: [TARGET], account: 'a' });
  assert.equal(f.windows[2].destroyed, true); assert.equal(f.manager.count(), 2);
  f.windows[1].close(); f.send('plugin-panel:forget', 'a');
  assert.equal(f.manager.count(), 0); assert(f.windows.every(own => own.destroyed), 'closed windows of the account go too');
  assert.deepEqual(plain(f.states().at(-1)), { windows: [] });
  f.manager.close();
});

test('a reloaded main renderer learns the open windows', () => {
  const f = fixture(); f.open(friend('u1')); f.ready(f.windows[0]);
  const before = f.states().length; f.main.webContents.emit('did-finish-load');
  assert.equal(f.states().length, before + 1); assert.equal(f.states().at(-1).windows[0].user, 'u1');
  assert.equal(f.windows[0].destroyed, false); f.manager.close();
});

test('teardown closes every window and removes every handler, even after the main window is gone', () => {
  const f = fixture(); f.open(friend('u1')); f.open(friend('u2')); f.windows[1].close(); const wc = f.main.webContents; f.manager.close();
  assert(f.windows.every(own => own.destroyed));
  for (const name of ['plugin-panel:open', 'plugin-panel:copy']) assert.equal(f.handlers.has(name), false, name);
  for (const name of ['plugin-panel:event', 'plugin-panel:retain', 'plugin-panel:forget', 'plugin-panel:host-call', 'plugin-panel:host-result', 'plugin-panel:hide'])
    assert.equal(f.ipcMain.listenerCount(name), 0, name);
  assert.equal(wc.listenerCount('did-finish-load'), 0); assert.equal(wc.listenerCount('did-start-navigation'), 0);
  assert.deepEqual(f.manager.open(friend('u3')), { opened: false });
  const g = fixture(); g.open(friend('u1'));
  g.main.destroyed = true; Object.defineProperty(g.main, 'webContents', { get() { throw new Error('Object has been destroyed'); } });
  assert.doesNotThrow(() => g.manager.close()); assert.equal(g.windows[0].destroyed, true);
});

test('every desktop packaging allowlist carries the window module and none the retired dock modules', () => {
  // Ported from tests/assistant-window.test.cjs (2026-10-22): packaged builds must contain the module they load.
  const read = relative => fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');
  const builder = JSON.parse(read('electron-builder.real-client.json')).files;
  assert(builder.includes('scripts/plugin-windows.cjs'));
  for (const [file, constant, entry] of [['scripts/stage-real-client.py', 'SCRIPTS', 'plugin-windows.cjs'],
    ['scripts/build-windows-release.py', 'ASAR_SCRIPTS', 'scripts/plugin-windows.cjs']]) {
    const tuple = read(file).match(new RegExp('^' + constant + '\\s*=\\s*\\(([\\s\\S]*?)^\\)', 'm'));
    assert(tuple, `${file} defines ${constant}`);
    const entries = [...tuple[1].matchAll(/["']([^"']+)["']/g)].map(match => match[1]);
    assert(entries.includes(entry), `${file} carries ${entry}`);
    assert(!entries.some(item => /plugin-panel\.cjs$|assistant-window\.cjs$/.test(item)), `${file} drops the retired modules`);
  }
  assert(!builder.some(item => /plugin-panel\.cjs$|assistant-window\.cjs$/.test(item)));
});

function preloadFixture(file = '../scripts/real-client-preload.cjs', active = true) {
  const ipc = new EventEmitter(), listeners = new Map(), styles = new Map(), classes = new Set(), calls = [];
  let host, modal = false, observer;
  ipc.invoke = async (...args) => { calls.push(args); return { visible: true }; }; ipc.send = (...args) => calls.push(args);
  const root = { style: { setProperty: (name, value) => styles.set(name, value) }, classList: { toggle(name, value) { if (value) classes.add(name); else classes.delete(name); } } };
  const document = { documentElement: root, body: {}, addEventListener: (name, fn) => listeners.set(name, fn), querySelector: () => modal ? {} : null };
  const window = {}; window.top = window;
  const context = { require: name => { assert.equal(name, 'electron'); return { ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_name, value) => { host = value; } } }; },
    process: { env: {} }, window, document, navigator: { userActivation: { isActive: active } }, MutationObserver: class { constructor(fn) { observer = fn; } observe() {} },
    URL, Set, Map, Object, Number, Promise, setTimeout, clearTimeout };
  vm.runInNewContext(fs.readFileSync(require.resolve(file), 'utf8'), context);
  return { host, ipc, window, styles, classes, calls, loaded: () => listeners.get('DOMContentLoaded')(), setModal: value => { modal = value; observer?.(); } };
}


test('main-window preload exposes only the plugin window and file picker bridge', async () => {
  const f = preloadFixture();
  for (const name of ['toggleAdvisor', 'sendAdvisorEnvelope', 'onAdvisorEnvelope', 'togglePersona', 'onPersonaVisibility',
    'suspendAdvisorForTraces', 'chooseAssistantPackage', 'togglePluginPanel', 'setPluginPanelContext', 'suspendPluginPanelForTraces'])
    assert.equal(f.host[name], undefined, name);
  await f.host.openPluginWindow({ ...TARGET, account: 'a', user: 'u', name: 'U', label: '聊天助手', theme: 'light', extra: 1 });
  assert.deepEqual(plain(f.calls.at(-1)), ['plugin-panel:open', { ...TARGET, account: 'a', user: 'u', name: 'U', label: '聊天助手', theme: 'light' }]);
  assert.deepEqual(plain(await f.host.openPluginWindow(null)), { opened: false });
  f.host.sendPluginWindowEvent({ target: { plugin: 'kyf.advisor', account: 'a', user: 'u' }, event: { type: 'quote' } });
  assert.deepEqual(plain(f.calls.at(-1)), ['plugin-panel:event', { target: { plugin: 'kyf.advisor', account: 'a', user: 'u' }, event: { type: 'quote' } }]);
  f.host.retainPluginWindows({ targets: [TARGET], account: 'a' }); assert.deepEqual(plain(f.calls.at(-1)), ['plugin-panel:retain', { targets: [TARGET], account: 'a' }]);
  f.host.forgetPluginWindows('a'); assert.deepEqual(f.calls.at(-1), ['plugin-panel:forget', 'a']);
  f.host.replyPluginPanelHostCall('w1:p1', { result: null }); assert.deepEqual(f.calls.at(-1), ['plugin-panel:host-result', 'w1:p1', { result: null }]);
  await f.host.choosePluginFile('kyf.advisor', 'directory');
  assert.deepEqual(f.calls.at(-1), ['real-client:choose-plugin-file', 'kyf.advisor', 'directory', true]);
  assert.equal(await f.host.choosePluginFile('kyf.advisor', 'device'), null);
  const seen = [], states = [];
  const off = f.host.onPluginPanelHostCall(call => seen.push(call)); f.host.onPluginPanelState(state => states.push(state));
  f.ipc.emit('plugin-panel:host-call', {}, { id: 'p1', method: 'toast' }); f.ipc.emit('plugin-panel:host-call', {}, 'bad');
  f.ipc.emit('plugin-panel:state', {}, { windows: [{ plugin: 'kyf.advisor', account: 'a', user: 'u', visible: true }] });
  f.ipc.emit('plugin-panel:state', {}, { visible: false });
  off(); f.ipc.emit('plugin-panel:host-call', {}, { id: 'p2' });
  assert.deepEqual(plain(seen), [{ id: 'p1', method: 'toast' }]);
  assert.deepEqual(plain(states), [{ windows: [{ plugin: 'kyf.advisor', account: 'a', user: 'u', visible: true }] }]);
  const passive = preloadFixture('../scripts/real-client-preload.cjs', false);
  assert.equal(await passive.host.choosePluginFile('kyf.advisor', 'file'), null, 'the picker needs a user gesture');
  f.window.top = {}; assert.deepEqual(plain(await f.host.openPluginWindow({ ...TARGET, account: 'a', user: 'u' })), { opened: false });
});

test('panel preload answers host calls by id, copies with the gesture state and hides', async () => {
  const f = preloadFixture('../scripts/plugin-panel-preload.cjs');
  const call = f.host.hostCall('view.focusChat', { a: 1 });
  const [channel, sent] = f.calls.at(-1);
  assert.equal(channel, 'plugin-panel:host-call'); assert.deepEqual(sent.params, { a: 1 });
  f.ipc.emit('plugin-panel:host-result', {}, 'other', { result: 1 });
  f.ipc.emit('plugin-panel:host-result', {}, sent.id, { result: { ok: true } });
  assert.deepEqual(await call, { result: { ok: true } });
  await f.host.copy('文本'); assert.deepEqual(f.calls.at(-1), ['plugin-panel:copy', '文本', true]);
  f.host.hide(); assert.deepEqual(f.calls.at(-1), ['plugin-panel:hide']);
  const messages = []; f.host.onMessage(message => messages.push(message));
  f.ipc.emit('plugin-panel:to-panel', {}, { context: { account: 'a' } }); f.ipc.emit('plugin-panel:to-panel', {}, null);
  assert.deepEqual(messages, [{ context: { account: 'a' } }]);
});

function panelHost(search = '?plugin=kyf.advisor&page=ui%2Fpanel.html') {
  const nodes = new Map(), listeners = {}, requests = [], hidden = [], copies = [], hostCalls = [];
  let receive;
  const element = tag => ({ tag, children: [], attributes: {}, dataset: {}, listeners: {}, textContent: '', className: '',
    contentWindow: tag === 'iframe' ? { messages: [], postMessage(message) { this.messages.push(message); } } : undefined,
    setAttribute(name, value) { this.attributes[name] = String(value); }, addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
    replaceChildren(...items) { this.children = items; }, appendChild(item) { this.children.push(item); }, remove() {} });
  const byId = id => { if (!nodes.has(id)) nodes.set(id, element('div')); return nodes.get(id); };
  const window = { location: { search }, addEventListener: (name, fn) => (listeners[name] ||= []).push(fn), removeEventListener() {},
    pluginPanel: { onMessage(fn) { receive = fn; }, hide: () => hidden.push(true), copy: async text => { copies.push(text); return true; },
      hostCall: async (method, params) => { hostCalls.push([method, params]); return method === 'toast' ? { error: { code: 'x', message: '失败' } } : { result: { ok: true } }; } } };
  const context = vm.createContext({ window, document: { getElementById: byId, createElement: element, body: { dataset: {} } },
    URLSearchParams, setTimeout: () => 0, clearTimeout() {}, Promise, JSON, Object, Error,
    fetch: async (url, init) => { requests.push({ url, body: JSON.parse(init.body) }); return { ok: true, status: 200, json: async () => ({ result: { echoed: true } }) }; } });
  for (const file of ['../chatui/plugin-frame.js', '../chatui/plugin-panel.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, file), 'utf8'), context);
  const frame = () => byId('pluginPanelFrame').children[0];
  const settle = async () => { for (let index = 0; index < 6; index++) await new Promise(resolve => setImmediate(resolve)); };
  async function fromPage(data) {
    for (const fn of listeners.message || []) fn({ source: frame().contentWindow, data });
    await settle();
    return frame().contentWindow.messages.at(-1);
  }
  return { byId, frame, fromPage, requests, hidden, copies, hostCalls, receive: message => receive(message), body: context.document.body };
}

test('panel host builds the sandboxed page on the first context and calls Core for the current friend', async () => {
  const h = panelHost();
  h.receive({ event: { type: 'quote', account: 'a', user: 'u1', messageId: 'm', text: 't' } });
  assert.equal(h.frame(), undefined, 'no page before the first context');
  h.receive({ context: { account: 'a', user: 'u1', name: 'U1', theme: 'light' } });
  const frame = h.frame();
  assert.equal(frame.src, '/plugin-pages/kyf.advisor/ui/panel.html'); assert.equal(frame.attributes.sandbox, 'allow-scripts');
  assert.equal(h.body.dataset.theme, 'light');
  await h.fromPage({ kyf: 1, type: 'ready' });
  const ready = frame.contentWindow.messages.find(item => item.type === 'context');
  assert.deepEqual(plain(ready.context), { account: 'a', user: 'u1', name: 'U1', theme: 'light', surface: 'panel', plugin: 'kyf.advisor' });
  assert.deepEqual(frame.contentWindow.messages.filter(item => item.type === 'event').map(item => item.event.type), ['quote'],
    'events wait for the page');
  await h.fromPage({ kyf: 1, type: 'call', id: 'c1', target: 'plugin', method: 'thread.get', params: { agentId: 'x' } });
  assert.deepEqual(plain(h.requests.at(-1)), { url: '/api/plugins/rpc', body: { plugin: 'kyf.advisor', method: 'thread.get', params: { agentId: 'x' }, account: 'a', user: 'u1' } });
  h.receive({ context: { account: 'a', user: 'u2', name: 'U2', theme: 'dark' } });
  // A save made for the friend just switched away from still reaches that friend; any other friend is refused.
  await h.fromPage({ kyf: 1, type: 'call', id: 'c2', target: 'plugin', method: 'draft.save', params: {}, scope: { account: 'a', user: 'u1' } });
  assert.equal(h.requests.at(-1).body.user, 'u1');
  const refused = await h.fromPage({ kyf: 1, type: 'call', id: 'c3', target: 'plugin', method: 'draft.save', params: {}, scope: { account: 'a', user: 'u9' } });
  assert.equal(refused.error.code, 'context-changed'); assert.equal(h.requests.length, 2);
  h.receive({ event: { type: 'account-cleared', account: 'a' } });
  const cleared = await h.fromPage({ kyf: 1, type: 'call', id: 'c4', target: 'plugin', method: 'draft.save', params: {}, scope: { account: 'a', user: 'u1' } });
  assert.equal(cleared.error.code, 'context-changed', 'a cleared account is never served as the previous friend');
});

test('panel host copies and closes in the desktop host and forwards other host methods', async () => {
  const h = panelHost(); h.receive({ context: { account: 'a', user: 'u' } }); await h.fromPage({ kyf: 1, type: 'ready' });
  assert.deepEqual(plain((await h.fromPage({ kyf: 1, type: 'call', id: 'c1', target: 'host', method: 'clipboard.copy', params: { text: '回答' } })).result), { copied: true });
  assert.deepEqual(h.copies, ['回答']);
  await h.fromPage({ kyf: 1, type: 'call', id: 'c2', target: 'host', method: 'panel.close' }); assert.equal(h.hidden.length, 1);
  assert.deepEqual(plain((await h.fromPage({ kyf: 1, type: 'call', id: 'c3', target: 'host', method: 'view.focusChat' })).result), { ok: true });
  assert.equal((await h.fromPage({ kyf: 1, type: 'call', id: 'c4', target: 'host', method: 'toast', params: { message: 'x' } })).error.message, '失败');
  assert.equal((await h.fromPage({ kyf: 1, type: 'call', id: 'c5', target: 'host', method: 'file.pick' })).error.code, 'unknown-method');
  assert.deepEqual(h.hostCalls.map(item => item[0]), ['view.focusChat', 'toast']);
  await h.fromPage({ kyf: 1, type: 'call', id: 'c6', target: 'host', method: 'attention.seen', params: { taskIds: ['t1'] } });
  assert.equal(h.hostCalls.at(-1)[0], 'attention.seen');
  const bad = panelHost('?plugin=../x&page=a.html'); bad.receive({ context: { account: 'a', user: 'u' } });
  assert.equal(bad.frame(), undefined);
});

test("a friend's own window gives its page the assistant-window surface", async () => {
  const h = panelHost('?plugin=kyf.advisor&page=ui%2Fpanel.html&independent=1');
  assert.equal(h.body.dataset.independent, 'true');
  h.receive({ context: { account: 'a', user: 'u1', name: 'U1', theme: 'dark' } }); await h.fromPage({ kyf: 1, type: 'ready' });
  assert.equal(h.frame().contentWindow.messages.find(item => item.type === 'context').context.surface, 'assistant-window');
});

test('panel host refuses a cleared current account, before and after its reset context arrives', async () => {
  // Added 2026-10-13 (review): the main window sends account-cleared first and the empty friend context after it.
  const h = panelHost(); h.receive({ context: { account: 'a', user: 'u1' } }); await h.fromPage({ kyf: 1, type: 'ready' });
  const save = id => h.fromPage({ kyf: 1, type: 'call', id, target: 'plugin', method: 'draft.save', params: {}, scope: { account: 'a', user: 'u1' } });
  h.receive({ event: { type: 'account-cleared', account: 'a' } });
  assert.equal((await save('c1')).error.code, 'context-changed');
  h.receive({ context: { account: '', user: '' } });
  assert.equal((await save('c2')).error.code, 'context-changed');
  assert.equal(h.requests.length, 0);
});

// Added 2026-10-14 (review): kyf-host.js runs first in a page and announces it is ready; the host answers with the
// context and the queued events (继续聊这句 sends a quote) while the page may still be loading its later scripts.
function pageBridge() {
  const listeners = [], posted = [], timers = [];
  const parent = { postMessage: message => posted.push(message) };
  const window = { parent, addEventListener: (name, fn) => { if (name === 'message') listeners.push(fn); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../plugin-sdk/web/kyf-host.js'), 'utf8'),
    { window, document: { documentElement: { dataset: {} } }, setTimeout: fn => timers.push(fn), clearTimeout() {} });
  const fromHost = data => listeners.forEach(fn => fn({ source: parent, data }));
  const runTimers = () => { for (const fn of timers.splice(0)) fn(); };
  return { host: window.kyfHost, posted, fromHost, runTimers };
}
test('messages reaching a page before its listener exists are delivered to it in order, each once', () => {
  const page = pageBridge();
  assert.deepEqual(plain(page.posted), [{ kyf: 1, type: 'ready' }]);
  page.fromHost({ kyf: 1, type: 'context', context: { account: 'a', user: 'u1', theme: 'light' } });
  page.fromHost({ kyf: 1, type: 'event', event: { type: 'quote', account: 'a', user: 'u1', messageId: 'm1', text: '周末见' } });
  assert.equal(page.host.context().user, 'u1');
  const received = [];
  page.host.on(message => received.push(message.type === 'event' ? message.event.type : message.type));
  assert.deepEqual(received, [], 'nothing runs inside the registering script');
  page.runTimers();
  assert.deepEqual(received, ['context', 'quote']);
  page.fromHost({ kyf: 1, type: 'event', event: { type: 'shown' } });
  page.runTimers();
  assert.deepEqual(received, ['context', 'quote', 'shown']);

  const late = pageBridge(), order = [];
  late.fromHost({ kyf: 1, type: 'event', event: { type: 'quote' } });
  late.host.on(message => order.push(message.event.type));
  late.fromHost({ kyf: 1, type: 'event', event: { type: 'visibility' } });
  late.runTimers();
  assert.deepEqual(order, ['quote', 'visibility'], 'a live message never overtakes an earlier one');
});
