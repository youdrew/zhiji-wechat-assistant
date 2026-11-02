// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/image-viewer.js"), "utf8");
const scope = { account: "synthetic-account", user: "synthetic&friend?query", id: "a".repeat(64) };
const fragment = values => `#${new URLSearchParams(values)}`;
function harness(hash = fragment(scope)) {
  const requests = [], elements = {};
  class Element {
    constructor() { this.listeners = new Map(); this.attributes = {}; this.style = {}; this.hidden = false; this.disabled = true; }
    set src(value) { this.attributes.src = value; requests.push(value); }
    get src() { return this.attributes.src || ""; }
    getAttribute(name) { return this.attributes[name]; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, handler) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(handler); }
    emit(name, properties = {}) {
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...properties };
      for (const handler of this.listeners.get(name) || []) handler(event);
      return event;
    }
  }
  for (const id of ["previewImage", "imageViewport", "imageCanvas", "imageStatus", "zoomOut", "zoomFit", "zoomIn"]) elements[id] = new Element();
  const image = elements.previewImage, viewport = elements.imageViewport;
  image.hidden = true; image.naturalWidth = 1600; image.naturalHeight = 1200;
  viewport.clientWidth = 800; viewport.clientHeight = 600; viewport.scrollLeft = 0; viewport.scrollTop = 0;
  const window = new Element();
  window.location = { hash }; window.closeCount = 0; window.close = () => { window.closeCount++; }; window.opener = {};
  vm.runInNewContext(source, { document: { getElementById: id => elements[id] }, window, URLSearchParams });
  return { ...elements, image, viewport, window, requests };
}

test("viewer fetches only the exact scoped local original and severs its opener", () => {
  const h = harness();
  assert.equal(h.requests.length, 1);
  const url = new URL(h.requests[0], "http://local.invalid");
  assert.equal(url.origin, "http://local.invalid");
  assert.equal(url.pathname, "/api/media");
  assert.deepEqual(Object.fromEntries(url.searchParams), { ...scope, variant: "original" });
  assert.equal(h.window.opener, null);
  assert.equal(h.image.referrerPolicy, "no-referrer");
  assert.equal(h.image.decoding, "async");
  assert.equal(h.zoomIn.disabled, true);
});

test("invalid, repeated, unknown or oversized fragment fields never request media", () => {
  const invalid = ["", fragment({ ...scope, id: "bad-id" }), fragment({ ...scope, id: "A".repeat(64) }),
    fragment({ ...scope, account: "" }), fragment({ ...scope, user: "bad\nuser" }),
    fragment({ ...scope, account: "x".repeat(4097) }), fragment({ ...scope, url: "https://untrusted.invalid" }),
    fragment({ account: scope.account, id: scope.id }), `${fragment(scope)}&account=duplicate`];
  for (const hash of invalid) {
    const h = harness(hash);
    assert.equal(h.requests.length, 0);
    assert.equal(h.imageStatus.textContent, "图片信息无效");
    assert.equal(h.imageStatus.hidden, false);
    assert.equal(h.zoomIn.disabled, true);
    h.image.emit("load");
    assert.equal(h.image.hidden, true, "a late event cannot revive an invalid viewer");
  }
});

test("fit, zoom, wheel and double click provide bounded centered image controls", () => {
  const h = harness();
  h.image.emit("load");
  const fitted = parseFloat(h.image.style.width);
  assert.ok(fitted <= h.viewport.clientWidth - 32);
  assert.ok(parseFloat(h.image.style.height) <= h.viewport.clientHeight - 32);
  assert.equal(h.image.hidden, false);
  assert.equal(h.imageStatus.hidden, true);
  assert.equal(h.zoomIn.disabled, false);
  h.zoomIn.emit("click");
  assert.ok(parseFloat(h.image.style.width) > fitted);
  h.zoomOut.emit("click");
  assert.ok(Math.abs(parseFloat(h.image.style.width) - fitted) < 0.001);
  h.zoomFit.emit("click");
  h.image.emit("dblclick");
  assert.equal(parseFloat(h.image.style.width), 1600);
  assert.ok(h.viewport.scrollLeft > 0);
  assert.ok(h.viewport.scrollTop > 0);
  h.image.emit("dblclick");
  assert.equal(parseFloat(h.image.style.width), fitted);
  assert.equal(h.viewport.scrollLeft, 0);
  const wheel = h.viewport.emit("wheel", { deltaY: -1 });
  assert.equal(wheel.defaultPrevented, true);
  assert.ok(parseFloat(h.image.style.width) > fitted);
  for (let index = 0; index < 100; index++) h.zoomIn.emit("click");
  assert.equal(parseFloat(h.image.style.width), 1600 * 8);
  for (let index = 0; index < 100; index++) h.zoomOut.emit("click");
  assert.equal(parseFloat(h.image.style.width), 1600 * 0.02);
});

test("fit follows window resize while manual zoom preserves image size", () => {
  const h = harness();
  h.image.emit("load");
  const before = parseFloat(h.image.style.width);
  h.viewport.clientWidth = 400; h.viewport.clientHeight = 300;
  h.window.emit("resize");
  assert.ok(parseFloat(h.image.style.width) < before);
  h.zoomIn.emit("click");
  const manual = h.image.style.width;
  h.viewport.clientWidth = 1000; h.viewport.clientHeight = 800;
  h.window.emit("resize");
  assert.equal(h.image.style.width, manual);
});

test("Escape closes the independent window and page exit releases its image", () => {
  const h = harness();
  h.image.emit("load");
  assert.equal(h.window.emit("keydown", { key: "Escape" }).defaultPrevented, true);
  assert.equal(h.window.closeCount, 1);
  h.window.emit("pagehide");
  assert.equal(h.image.src, "");
  const previous = h.image.style.width;
  h.zoomIn.emit("click"); h.image.emit("load"); h.zoomIn.emit("click");
  assert.equal(h.image.style.width, previous);
});

test("failed images show only plain generic text and disable zoom", () => {
  const h = harness();
  h.image.emit("error");
  assert.equal(h.imageStatus.textContent, "图片暂不可用");
  assert.equal(h.imageStatus.hidden, false);
  assert.equal(h.image.hidden, true);
  assert.equal(h.image.src, "");
  assert.equal(h.zoomFit.disabled, true);
  h.image.emit("load");
  assert.equal(h.image.hidden, true);
});

test("viewer document restricts assets to local origin and contains no native preload or inline code", () => {
  const html = readFileSync(path.join(__dirname, "../chatui/image-viewer.html"), "utf8");
  assert.match(html, /default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'/);
  assert.match(html, /<script src="\/image-viewer\.js" defer><\/script>/);
  assert.equal(/https?:\/\/|preload|onload=|onerror=/i.test(html), false);
  assert.equal(/innerHTML|desktopHost|fetch\(/.test(source), false);
});
