// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/composer-resize.js"), "utf8");
const storageKey = "chat-input-height-v1";
const heightProperty = "--chat-input-height";
const nearly = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 0.001,
  `${message}: expected ${expected}, received ${actual}`);

function eventTarget(value = {}) {
  const listeners = new Map();
  value.addEventListener = (type, callback) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(callback);
  };
  value.emit = (type, details = {}) => {
    const event = { type, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; }, ...details };
    for (const listener of listeners.get(type) || []) listener(event);
    return event;
  };
  return value;
}

function harness({ zoom = 1, containerHeight = 800, headerHeight = 60, paneMargins = 0, onResize,
  storage = new Map(), readFails = false, writeFails = false, captureFails = false } = {}) {
  const writes = [], removes = [], captures = [], observers = [];
  let document, hidden = false;
  class Node {
    constructor(id) {
      eventTarget(this); this.id = id; this.attributes = {}; this.captureIds = new Set();
      const values = new Map();
      this.style = { setProperty: (name, value) => values.set(name, String(value)),
        getPropertyValue: name => values.get(name) || "" };
      const classes = new Set();
      this.classList = { contains: name => classes.has(name),
        add: name => classes.add(name), remove: name => classes.delete(name) };
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    setPointerCapture(id) {
      if (captureFails) throw new Error("Synthetic pointer capture failure");
      this.captureIds.add(id); captures.push(id);
    }
    releasePointerCapture(id) {
      this.captureIds.delete(id);
      this.emit("lostpointercapture", { pointerId: id });
    }
    hasPointerCapture(id) { return this.captureIds.has(id); }
    focus() { document.activeElement = this; }
    get offsetHeight() {
      if (hidden) return 0;
      if (this.id === "chatInputPane") return parseFloat(this.style.getPropertyValue(heightProperty)) || 180;
      if (this.id === "chatMainArea") return Math.max(0, containerHeight / rootZoom() - headerHeight - paneMargins - pane.offsetHeight);
      return containerHeight / rootZoom();
    }
    getBoundingClientRect() {
      const height = this.offsetHeight * rootZoom();
      const top = hidden ? 0 : this.id === "chatMainArea" ? headerHeight * rootZoom() :
        this.id === "chatInputPane" ? (headerHeight + mainArea.offsetHeight + paneMargins / 2) * rootZoom() : 0;
      return { left: 0, top, width: 1000, height, right: 1000, bottom: top + height };
    }
  }
  const root = new Node("html"); root.style.zoom = String(zoom);
  const rootZoom = () => Number(root.style.zoom) || 1;
  document = eventTarget({ documentElement: root, activeElement: null, body: new Node("body") });
  const container = new Node("chatView"), mainArea = new Node("chatMainArea");
  const pane = new Node("chatInputPane"), handle = new Node("chatInputResizeHandle");
  const localStorage = {
    getItem(key) { if (readFails) throw new Error("Synthetic storage read failure"); return storage.get(key) ?? null; },
    setItem(key, value) {
      if (writeFails) throw new Error("Synthetic storage write failure");
      writes.push([key, String(value)]); storage.set(key, String(value));
    },
    removeItem(key) {
      if (writeFails) throw new Error("Synthetic storage write failure");
      removes.push(key); storage.delete(key);
    },
  };
  class ResizeObserver {
    constructor(callback) { this.callback = callback; this.observed = []; observers.push(this); }
    observe(node) { this.observed.push(node); }
  }
  const window = eventTarget({ localStorage, ResizeObserver,
    getComputedStyle: () => ({ marginTop: `${paneMargins / 2}px`, marginBottom: `${paneMargins / 2}px` }) });
  vm.runInNewContext(source, { window, document, console });
  const ui = window.ComposerResize;
  const init = () => ui.init({ pane, handle, container, mainArea, onResize });
  init();
  const pointer = (type, clientY = 400, properties = {}) => handle.emit(type,
    { pointerId: 7, button: 0, isPrimary: true, clientY, ...properties });
  return { ui, pane, handle, container, mainArea, root, document, window, storage, writes, removes, captures, init,
    get height() { return pane.offsetHeight; },
    get messageHeight() { return mainArea.offsetHeight; },
    down: (y = 400, details) => pointer("pointerdown", y, details),
    move: (y, details) => pointer("pointermove", y, details),
    up: (y = 400, details) => pointer("pointerup", y, details),
    cancel: details => pointer("pointercancel", 400, details),
    key: (key, details = {}) => handle.emit("keydown", { key, ...details }),
    setContainerHeight(height) { containerHeight = height; },
    setHeaderHeight(height) { headerHeight = height; },
    setHidden(value) { hidden = value; },
    notifyResize(target = container) {
      for (const observer of observers) {
        if (observer.observed.includes(target)) observer.callback([{ target }]);
      }
    },
  };
}

test("dragging the top edge upward grows the composer and persists only on release", () => {
  const h = harness();
  assert.equal(h.down(400).defaultPrevented, true);
  assert.equal(h.handle.hasPointerCapture(7), true);
  assert.equal(h.document.body.classList.contains("composer-resizing"), true);
  h.move(320);
  nearly(h.height, 260, "An upward movement grows the draft area");
  assert.deepEqual(h.writes, []);
  h.up(320);
  assert.equal(h.handle.hasPointerCapture(7), false);
  assert.equal(h.document.body.classList.contains("composer-resizing"), false);
  assert.deepEqual(h.writes, [[storageKey, "260"]]);
});

test("a final pointer position is applied even when the last move event was not delivered", () => {
  const h = harness(); h.down(400); h.move(380); h.up(300);
  nearly(h.height, 280, "The released position controls the completed height");
  assert.equal(h.storage.get(storageKey), "280");
});

test("secondary buttons, nonprimary and unrelated pointers cannot own the resize", () => {
  for (const details of [{ button: 2 }, { button: 1 }, { isPrimary: false }]) {
    const h = harness(); h.down(400, details); h.move(200); h.up(200);
    nearly(h.height, 180, "An ignored pointer leaves the composer alone");
    assert.deepEqual(h.captures, []); assert.deepEqual(h.writes, []);
  }
  const h = harness(); h.down(400);
  h.move(200, { pointerId: 8 }); h.up(200, { pointerId: 8 }); h.cancel({ pointerId: 8 });
  nearly(h.height, 180, "Only the captured pointer may resize");
  assert.equal(h.handle.hasPointerCapture(7), true);
  h.move(350); h.up(350);
  nearly(h.height, 230, "The original pointer still owns the resize");
});

test("minimum and hard maximum constrain downward and upward dragging", () => {
  const h = harness(); h.down(400); h.move(2000);
  nearly(h.height, 140, "The draft area keeps its usable minimum");
  h.move(-2000); nearly(h.height, 480, "The draft area has a hard maximum");
  h.up(-2000); assert.equal(h.storage.get(storageKey), "480");
});

test("dynamic limits subtract the header and reserve at least 140 pixels for messages", () => {
  const h = harness({ containerHeight: 450, headerHeight: 70 });
  h.down(400); h.move(-400); h.up(-400);
  nearly(h.height, 240, "450 - 70 header - 140 messages controls the maximum");
  nearly(h.messageHeight, 140, "The message area retains its minimum");
  assert.equal(h.handle.getAttribute("aria-valuemax"), "240");
});

test("a tall remembered composer accounts for pane margins in one refresh after a short window initially overflows", () => {
  const h = harness({ paneMargins: 18, storage: new Map([[storageKey, "480"]]) });
  h.setContainerHeight(360); h.ui.refresh();
  nearly(h.height, 142, "A single refresh subtracts header and margins despite initial overflow");
  nearly(h.messageHeight, 140, "The message area gets its promised space immediately");
  assert.equal(h.storage.get(storageKey), "480", "Window constraints keep the preferred height");
  h.setContainerHeight(800); h.notifyResize();
  nearly(h.height, 480, "Growing the window restores the preference");
  assert.deepEqual(h.writes, []);
});

test("all supported zooms convert screen drag distance into logical saved height", () => {
  for (const zoom of [0.9, 1, 1.1, 1.25, 1.5]) {
    const h = harness({ zoom });
    const before = h.pane.getBoundingClientRect().height;
    h.down(400); h.move(340);
    nearly(h.pane.getBoundingClientRect().height - before, 60, `Zoom ${zoom} follows screen movement`);
    h.up(340);
    nearly(Number(h.storage.get(storageKey)), 180 + 60 / zoom, `Zoom ${zoom} saves logical pixels`);
  }
});

test("dynamic height limits account for zoom and a changed header", () => {
  const h = harness({ zoom: 1.5, containerHeight: 750, headerHeight: 60 });
  h.key("End"); nearly(h.height, 300, "750 / 1.5 - 60 header - 140 messages defines the limit");
  h.setHeaderHeight(80); h.ui.refresh();
  nearly(h.height, 280, "A refreshed taller header still reserves message space");
  nearly(h.messageHeight, 140, "Message space is logical CSS pixels under zoom");
});

test("main area resize notifications adapt to header changes and restore preferences without saving", () => {
  const h = harness({ containerHeight: 600, paneMargins: 18,
    storage: new Map([[storageKey, "350"]]) });
  nearly(h.height, 350, "The initial header leaves room for the remembered height");
  h.setHeaderHeight(120); h.notifyResize(h.mainArea);
  nearly(h.height, 322, "An expanded header clamps the pane even though container height stays fixed");
  nearly(h.messageHeight, 140, "The message area retains usable space after the header expands");
  assert.equal(h.storage.get(storageKey), "350", "Header constraints do not replace the preferred height");
  h.setHeaderHeight(60); h.notifyResize(h.mainArea);
  nearly(h.height, 350, "Removing header content restores the preferred height");
  h.notifyResize(h.mainArea);
  nearly(h.height, 350, "A repeated layout notification stabilizes at the same height");
  assert.deepEqual(h.writes, [], "Observed layout changes do not write a new preference");
});

test("Escape, blur, lost capture and pointer cancellation restore the starting preference", () => {
  for (const cancel of [h => h.cancel(),
    h => h.handle.emit("lostpointercapture", { pointerId: 7 }),
    h => h.window.emit("blur"), h => h.document.emit("keydown", { key: "Escape" })]) {
    const h = harness({ storage: new Map([[storageKey, "300"]]) });
    h.down(400); h.move(300); cancel(h);
    nearly(h.height, 300, "Cancellation rolls back the provisional height");
    assert.equal(h.storage.get(storageKey), "300");
    assert.deepEqual(h.writes, []);
    h.move(200); h.up(200);
    nearly(h.height, 300, "Later pointer events cannot revive a cancelled drag");
    assert.deepEqual(h.writes, []);
  }
  const h = harness(); h.down(400); h.move(300); h.cancel();
  nearly(h.height, 180, "Cancelling an unsaved drag restores the default");
  assert.equal(h.storage.has(storageKey), false);
});

test("arrow keys, Shift increments and Home and End use the same limits and update accessibility values", () => {
  const h = harness();
  assert.equal(h.key("ArrowUp").defaultPrevented, true);
  nearly(h.height, 190, "ArrowUp increases by ten logical pixels");
  h.key("ArrowDown", { shiftKey: true }); nearly(h.height, 140, "Shift decreases by fifty pixels");
  h.key("End"); nearly(h.height, 480, "End selects the available maximum");
  h.key("Home"); nearly(h.height, 140, "Home selects the minimum");
  assert.equal(h.handle.getAttribute("aria-valuemin"), "140");
  assert.equal(h.handle.getAttribute("aria-valuemax"), "480");
  assert.equal(h.handle.getAttribute("aria-valuenow"), "140");
  assert.equal(h.handle.getAttribute("aria-valuetext"), "140 像素");
  const before = h.writes.length;
  assert.equal(h.key("Enter").defaultPrevented, false);
  assert.equal(h.writes.length, before);
});

test("double click restores the default height and clears the remembered height", () => {
  const h = harness({ storage: new Map([[storageKey, "350"]]) });
  h.down(400); h.move(350); h.handle.emit("dblclick");
  nearly(h.height, 180, "Double click cancels any drag and restores the default");
  assert.equal(h.storage.has(storageKey), false);
  assert.deepEqual(h.removes, [storageKey]); assert.deepEqual(h.writes, []);
});

test("saved heights restore across pages and temporary window constraints do not overwrite them", () => {
  const h = harness(); h.down(400); h.up(220);
  const reopened = harness({ storage: h.storage });
  nearly(reopened.height, 360, "A new page restores the logical preferred height");
  reopened.setContainerHeight(400); reopened.window.emit("resize");
  nearly(reopened.height, 200, "A short window clamps the rendered height");
  reopened.setContainerHeight(800); reopened.notifyResize();
  nearly(reopened.height, 360, "A larger window restores the original preference");
  assert.equal(reopened.storage.get(storageKey), "360"); assert.deepEqual(reopened.writes, []);
  const zoomed = harness({ storage: h.storage, zoom: 1.5 });
  nearly(zoomed.height, 1000 / 3, "The zoomed page clamps to its logical available height");
  assert.equal(zoomed.storage.get(storageKey), "360");
});

test("hiding another tab preserves the composer preference until chat is visible again", () => {
  const h = harness({ storage: new Map([[storageKey, "350"]]) });
  h.setHidden(true); h.notifyResize();
  assert.equal(h.pane.style.getPropertyValue(heightProperty), "350px");
  assert.equal(h.storage.get(storageKey), "350");
  h.setContainerHeight(400); h.setHidden(false); h.notifyResize();
  nearly(h.height, 200, "Returning to chat applies the actual visible window limit");
  assert.deepEqual(h.writes, []);
});

test("resize callbacks only report actual layout changes including cancellation and window constraints", () => {
  const changed = [], h = harness({ onResize: height => changed.push(height) });
  assert.deepEqual(changed, [], "The unchanged CSS default does not notify");
  h.down(400); h.move(300); h.ui.refresh(); h.move(300);
  assert.deepEqual(changed, [280], "Repeated refreshes at one height do not repeat the callback");
  h.cancel(); assert.deepEqual(changed, [280, 180], "Cancelling reports the restored layout");
  h.key("End"); h.setContainerHeight(400); h.ui.refresh();
  assert.deepEqual(changed, [280, 180, 480, 200], "Window constraints also notify scroll-preservation consumers");
  h.handle.emit("dblclick");
  assert.deepEqual(changed, [280, 180, 480, 200, 180], "Reset reports its default layout");
});

test("invalid or unavailable preferences keep the default and storage failures do not block resizing", () => {
  for (const value of ["bad-height", "null", "NaN", "Infinity", "", "-1", "139", "481", "1000"]) {
    const h = harness({ storage: new Map([[storageKey, value]]) });
    nearly(h.height, 180, `Invalid preference ${value} keeps the default`);
    assert.deepEqual(h.writes, []);
  }
  const unreadable = harness({ readFails: true }); unreadable.down(400); unreadable.up(350);
  nearly(unreadable.height, 230, "Failed reading does not block a new resize");
  const unwritable = harness({ writeFails: true }); unwritable.down(400); unwritable.up(350);
  nearly(unwritable.height, 230, "Failed saving preserves the layout for this window");
  unwritable.key("ArrowUp"); nearly(unwritable.height, 240, "Keyboard resizing also works without storage");
});

test("capture failure rolls back safely and repeated initialization binds only one handler", () => {
  const failed = harness({ captureFails: true }); failed.down(400); failed.up(300);
  nearly(failed.height, 180, "An unavailable pointer capture cannot start a drag");
  assert.deepEqual(failed.writes, []);
  const h = harness(); h.init(); h.init(); h.down(400); h.up(350);
  nearly(h.height, 230, "Repeated initialization still applies one movement");
  assert.equal(h.captures.length, 1); assert.equal(h.writes.length, 1);
});
