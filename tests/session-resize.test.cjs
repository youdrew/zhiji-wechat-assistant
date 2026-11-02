// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/session-resize.js"), "utf8");
const storageKey = "session-pane-width-v1";
const widthProperty = "--session-pane-width";
const nearly = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 0.001,
  `${message}: expected ${expected}, received ${actual}`);

function style() {
  const values = new Map();
  return {
    setProperty: (name, value) => values.set(name, String(value)),
    getPropertyValue: name => values.get(name) || "",
    removeProperty(name) { const previous = values.get(name) || ""; values.delete(name); return previous; },
  };
}

function eventTarget(value = {}) {
  value.listeners = new Map();
  value.addEventListener = (type, callback) => {
    if (!value.listeners.has(type)) value.listeners.set(type, []);
    value.listeners.get(type).push(callback);
  };
  value.removeEventListener = (type, callback) => {
    value.listeners.set(type, (value.listeners.get(type) || []).filter(listener => listener !== callback));
  };
  value.emit = (type, details = {}) => {
    const event = { type, target: value, currentTarget: value, bubbles: true, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; },
      stopImmediatePropagation() { this.stopped = this.immediateStopped = true; }, ...details };
    for (let target = value; target; target = event.bubbles && !event.stopped ? target.parentNode : null) {
      event.currentTarget = target;
      for (const listener of [...(target.listeners.get(type) || [])]) {
        listener(event);
        if (event.immediateStopped) break;
      }
    }
    return event;
  };
  return value;
}

function harness({ zoom = 1, containerWidth = 1280, railWidth = 64, defaultWidth = 240,
  storage = new Map(), readFails = false, writeFails = false } = {}) {
  const writes = [], removes = [], captures = [], observers = [];
  let document, window;
  class Node {
    constructor(id) {
      eventTarget(this); this.id = id; this.style = style(); this.attributes = {};
      this.className = ""; this.children = []; this.parentNode = null; this.captureIds = new Set();
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { if (!this.classList.contains(name)) this.className = `${this.className} ${name}`.trim(); },
        remove: name => { this.className = this.className.split(/\s+/).filter(value => value !== name).join(" "); },
        toggle: (name, active) => this.classList[active ? "add" : "remove"](name),
      };
    }
    append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    setPointerCapture(id) { this.captureIds.add(id); captures.push(id); }
    releasePointerCapture(id) { this.captureIds.delete(id); }
    hasPointerCapture(id) { return this.captureIds.has(id); }
    focus() { document.activeElement = this; }
    get isConnected() { return true; }
    get offsetWidth() {
      if (this.id === "sessionPane") return parseFloat(this.style.getPropertyValue(widthProperty)) || defaultWidth;
      if (this.id === "appWindow") return containerWidth / rootZoom();
      if (this.id === "navigationRail") return railWidth;
      return 6;
    }
    get clientWidth() { return this.offsetWidth; }
    getBoundingClientRect() {
      const width = this.id === "appWindow" ? containerWidth : this.offsetWidth * rootZoom();
      return { left: 0, top: 0, width, height: 600, right: width, bottom: 600 };
    }
  }
  const root = new Node("html"); root.style.zoom = String(zoom);
  const rootZoom = () => Number(root.style.zoom) || 1;
  document = eventTarget({ documentElement: root, activeElement: null });
  document.body = new Node("body"); document.body.parentNode = document;
  const container = new Node("appWindow"), rail = new Node("navigationRail");
  const pane = new Node("sessionPane"), handle = new Node("sessionResizeHandle");
  document.body.append(container); container.append(rail, pane); pane.append(handle);
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
    disconnect() { this.observed = []; }
  }
  const getComputedStyle = node => ({ zoom: root.style.zoom, width: `${node.offsetWidth}px`,
    getPropertyValue: name => node.style.getPropertyValue(name) });
  window = eventTarget({ localStorage, ResizeObserver, getComputedStyle,
    innerWidth: containerWidth, innerHeight: 600, requestAnimationFrame: callback => callback() });
  document.parentNode = window; document.defaultView = window;
  const context = vm.createContext({ window, document, localStorage, ResizeObserver, getComputedStyle, console,
    requestAnimationFrame: callback => callback(), cancelAnimationFrame() {}, setTimeout, clearTimeout });
  vm.runInContext(source, context);
  const ui = window.SessionResize;
  const init = () => ui.init({ pane, handle, container, rail });
  init();
  const pointer = (type, clientX = 100, properties = {}) => handle.emit(type,
    { pointerId: 7, pointerType: "mouse", button: 0, buttons: type === "pointerup" ? 0 : 1,
      isPrimary: true, clientX, ...properties });
  return { ui, pane, handle, container, rail, root, document, window, storage, writes, removes, captures, observers, init,
    get width() { return pane.offsetWidth; },
    get customWidth() { return pane.style.getPropertyValue(widthProperty); },
    down: (x = 100, details) => pointer("pointerdown", x, details),
    move: (x, details) => pointer("pointermove", x, details),
    up: (x = 100, details) => pointer("pointerup", x, details),
    cancel: () => pointer("pointercancel"),
    key: (key, details = {}) => handle.emit("keydown", { key, ...details }),
    setContainerWidth(width) { containerWidth = width; window.innerWidth = width; },
    notifyResize() { for (const observer of observers) observer.callback(observer.observed.map(target => ({ target }))); },
  };
}

test("left dragging resizes the pane with pointer capture and saves only when released", () => {
  const h = harness();
  const down = h.down(100);
  assert.equal(down.defaultPrevented, true);
  assert.equal(h.handle.hasPointerCapture(7), true);
  h.move(180);
  nearly(h.width, 320, "Dragging adds the horizontal distance to the starting width");
  assert.deepEqual(h.writes, []);
  h.up(180);
  assert.equal(h.handle.hasPointerCapture(7), false);
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0][0], storageKey);
  nearly(Number(h.storage.get(storageKey)), 320, "Released width is persisted in logical CSS pixels");
});

test("secondary buttons and nonprimary pointers do not start a resize", () => {
  for (const details of [{ button: 2 }, { button: 1 }, { isPrimary: false }]) {
    const h = harness(); h.down(100, details); h.move(200); h.up(200);
    nearly(h.width, 240, "An ignored pointer cannot alter the pane");
    assert.deepEqual(h.captures, []);
    assert.deepEqual(h.writes, []);
  }
});

test("other pointers cannot move or finish the active resize", () => {
  const h = harness(); h.down(100);
  h.move(200, { pointerId: 8 }); h.up(200, { pointerId: 8 });
  nearly(h.width, 240, "Only the captured pointer moves the pane");
  assert.equal(h.handle.hasPointerCapture(7), true);
  assert.deepEqual(h.writes, []);
  h.move(160); h.up(160);
  nearly(h.width, 300, "The original pointer still owns the drag");
  assert.equal(h.writes.length, 1);
});

test("dragging respects the minimum width and maximum width", () => {
  const h = harness(); h.down(100); h.move(-1000);
  nearly(h.width, 174, "The pane cannot collapse below its minimum");
  h.move(1000);
  nearly(h.width, 480, "The pane cannot grow beyond its configured maximum");
  h.up(1000);
  nearly(Number(h.storage.get(storageKey)), 480, "Only the clamped width is stored");
});

test("dynamic maximum reserves space for the chat pane and accounts for the navigation rail", () => {
  const h = harness({ containerWidth: 700 }); h.down(100); h.move(1000);
  nearly(h.width, 276, "700 - 64 rail - 360 chat determines the maximum");
  h.up(1000);
  h.setContainerWidth(500); h.ui.refresh();
  nearly(h.width, 174, "A very narrow window retains the minimum usable list width");
});

test("all supported page zooms preserve the screen drag distance and logical persisted width", () => {
  for (const zoom of [0.9, 1, 1.1, 1.25, 1.5]) {
    const h = harness({ zoom });
    const before = h.pane.getBoundingClientRect().width;
    h.down(100); h.move(160);
    nearly(h.pane.getBoundingClientRect().width - before, 60, `Zoom ${zoom} follows the screen pointer`);
    nearly(h.width, 240 + 60 / zoom, `Zoom ${zoom} converts screen movement to logical width`);
    h.up(160);
    nearly(Number(h.storage.get(storageKey)), 240 + 60 / zoom, `Zoom ${zoom} saves logical width`);
  }
});

test("zoomed dynamic limits use rendered container and rail bounds", () => {
  const zoom = 1.5, h = harness({ zoom, containerWidth: 1000 });
  h.down(100); h.move(1000);
  const expected = (1000 - 64 * zoom) / zoom - 360;
  nearly(h.width, expected, "Available rendered width is converted back to logical CSS width");
  nearly((h.container.getBoundingClientRect().width - h.rail.getBoundingClientRect().width -
    h.pane.getBoundingClientRect().width) / zoom, 360, "The chat pane retains its logical minimum");
});

test("pointer cancel, lost capture, blur and Escape restore the starting width without saving", () => {
  for (const cancel of [
    h => h.cancel(),
    h => h.handle.emit("lostpointercapture", { pointerId: 7 }),
    h => h.window.emit("blur"),
    h => h.document.emit("keydown", { key: "Escape" }),
  ]) {
    const storage = new Map([[storageKey, "300"]]), h = harness({ storage });
    h.down(100); h.move(190); cancel(h);
    nearly(h.width, 300, "A cancelled drag restores its starting width");
    assert.equal(h.storage.get(storageKey), "300");
    assert.deepEqual(h.writes, []);
    h.move(220); h.up(220);
    nearly(h.width, 300, "Events after cancellation cannot resume the drag");
    assert.deepEqual(h.writes, []);
  }
});

test("cancelling a drag from the responsive default removes the temporary width override", () => {
  const h = harness(); assert.equal(h.customWidth, "");
  h.down(100); h.move(200); h.cancel();
  assert.equal(h.customWidth, "");
  nearly(h.width, 240, "Responsive default is restored");
  assert.equal(h.storage.has(storageKey), false);
});

test("arrow keys, Shift increments and Home and End resize within the same limits", () => {
  const h = harness();
  assert.equal(h.key("ArrowRight").defaultPrevented, true);
  nearly(h.width, 250, "Arrow keys resize by ten logical pixels");
  h.key("ArrowLeft", { shiftKey: true });
  nearly(h.width, 200, "Shift changes the increment to fifty logical pixels");
  h.key("Home"); nearly(h.width, 174, "Home selects the minimum");
  h.key("End"); nearly(h.width, 480, "End selects the available maximum");
  nearly(Number(h.storage.get(storageKey)), 480, "Keyboard actions persist the chosen width");
  const before = h.writes.length;
  assert.equal(h.key("Enter").defaultPrevented, false);
  assert.equal(h.writes.length, before, "Unrelated keys do not change the layout");
});

test("double-click returns to the responsive default and clears the saved preference", () => {
  const h = harness({ storage: new Map([[storageKey, "350"]]) });
  nearly(h.width, 350, "The persisted width is applied initially");
  h.handle.emit("dblclick");
  assert.equal(h.customWidth, "");
  nearly(h.width, 240, "Double-click restores the CSS default");
  assert.equal(h.storage.has(storageKey), false);
  assert.deepEqual(h.removes, [storageKey]);
});

test("a saved width survives a new page and temporary window constraints preserve the preference", () => {
  const h = harness(); h.down(100); h.move(260); h.up(260);
  nearly(h.width, 400, "The drag chooses a wider list");
  const reopened = harness({ storage: h.storage });
  nearly(reopened.width, 400, "A new page restores the saved width");
  const zoomed = harness({ storage: h.storage, zoom: 1.5 });
  nearly(zoomed.width, 400, "A new zoom setting retains the logical width preference");
  nearly(zoomed.pane.getBoundingClientRect().width, 600, "The saved logical width renders at the new zoom");
  reopened.setContainerWidth(700); reopened.ui.refresh();
  nearly(reopened.width, 276, "A smaller window clamps the rendered width");
  assert.equal(Number(reopened.storage.get(storageKey)), 400, "Temporary constraints do not overwrite the preference");
  reopened.setContainerWidth(1280); reopened.ui.refresh();
  nearly(reopened.width, 400, "Expanding the window restores the user's preferred width");
  assert.deepEqual(reopened.writes, []);
});

test("resize notifications apply window constraints without writing a new preference", () => {
  const h = harness({ storage: new Map([[storageKey, "400"]]) });
  h.setContainerWidth(700); h.window.emit("resize");
  nearly(h.width, 276, "Window resize reclamps the list");
  h.setContainerWidth(1280); h.notifyResize();
  nearly(h.width, 400, "An observed container resize restores the preference");
  assert.deepEqual(h.writes, []);
});

test("unusable storage values and read failures retain a usable responsive default", () => {
  for (const value of ["not-a-width", "null", "NaN", "Infinity", "", "-1", "173", "481", "1000"]) {
    const h = harness({ storage: new Map([[storageKey, value]]) });
    nearly(h.width, 240, `Invalid preference ${value} does not alter the layout`);
    assert.deepEqual(h.writes, []);
  }
  const h = harness({ readFails: true });
  nearly(h.width, 240, "A storage read failure does not prevent using the pane");
  h.down(100); h.move(150); h.up(150);
  nearly(h.width, 290, "The pane remains draggable after an unavailable preference");
});

test("a failed save preserves the completed layout change for the current page", () => {
  const h = harness({ writeFails: true });
  h.down(100); h.move(180); h.up(180);
  nearly(h.width, 320, "A write failure does not roll back a completed drag");
  assert.equal(h.handle.hasPointerCapture(7), false);
  assert.equal(h.storage.has(storageKey), false);
  h.key("ArrowRight");
  nearly(h.width, 330, "Keyboard resizing also remains usable when storage is unavailable");
});

test("repeated initialization binds one resize action and one persistence write", () => {
  const h = harness(); h.init(); h.init();
  h.down(100); h.move(140); h.up(140);
  nearly(h.width, 280, "Repeated initialization keeps one drag handler");
  assert.equal(h.captures.length, 1);
  assert.equal(h.writes.length, 1);
});
