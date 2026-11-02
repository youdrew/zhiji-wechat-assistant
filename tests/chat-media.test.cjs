// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../chatui/media.js"), "utf8");
function harness(options = {}) {
  const made = [], requests = [], observers = [], popups = [], openCalls = [];
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
      this.className = ""; this.style = {}; this.hidden = false; this.attributes = {};
      this.listeners = new Map(); this.pauseCount = 0; this.loadCount = 0; this.playCount = 0;
      this.classList = {
        add: name => { this.className = [...new Set([...this.className.split(" "), name])].join(" ").trim(); },
        remove: name => { this.className = this.className.split(" ").filter(value => value !== name).join(" "); },
        contains: name => this.className.split(" ").includes(name),
      };
      made.push(this);
    }
    get isConnected() { return this === body || Boolean(this.parent?.isConnected); }
    set src(value) { this.attributes.src = value; requests.push({ tag: this.tagName, url: value }); }
    get src() { return this.attributes.src || ""; }
    append(...items) { items.forEach(item => this.appendChild(item)); }
    appendChild(item) { this.children.push(item); item.parent = this; return item; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(callback); }
    emit(name) { for (const callback of this.listeners.get(name) || []) callback({ target: this, preventDefault() {} }); }
    play() { this.playCount++; this.emit("play"); return Promise.resolve(); }
    pause() { this.pauseCount++; }
    load() { this.loadCount++; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
      return this.children.flatMap(child => [child, ...child.querySelectorAll("*")]).filter(child => selector === "*" ||
        (selector.startsWith(".") ? child.classList.contains(selector.slice(1)) : child.tagName === selector.toUpperCase()));
    }
  }
  const body = new Element("body"), container = new Element("div");
  body.append(container);
  const state = { account: "synthetic-account", user: "synthetic-friend", generation: 1 };
  const context = vm.createContext({
    window: { desktopHost: options.desktopHost, open: (...args) => {
      openCalls.push(args);
      if (options.blockPopup) return null;
      const popup = { closed: false, focusCount: 0, closeCount: 0,
        close() { this.closed = true; this.closeCount++; }, focus() { this.focusCount++; } };
      popups.push(popup);
      return popup;
    } }, URLSearchParams,
    document: { body, createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag),
      getElementById: id => id === "chatMessages" ? container : null },
    MutationObserver: class { constructor(callback) { observers.push(callback); } observe() {} },
  });
  vm.runInContext(source, context);
  function render(media, id = "a".repeat(64)) {
    const result = context.window.ChatMedia.render({ id, media }, state,
      captured => captured.account === state.account && captured.user === state.user && captured.generation === state.generation);
    if (result) container.append(result);
    return result;
  }
  return { render, state, made, requests, body, container, popups, openCalls, reset: context.window.ChatMedia.reset,
    mutate: () => observers.forEach(callback => callback()) };
}

test("image click opens a scoped independent viewer without original requests or modal in chat", () => {
  const h = harness();
  h.state.user = "synthetic&friend?query";
  const image = h.render({ kind: "image", width: 800, height: 1200, url: "https://untrusted.invalid/image" });
  assert.equal(image.querySelector("img").loading, "lazy");
  assert.equal(image.querySelector("img").decoding, "async");
  assert.equal(h.requests.length, 1);
  let url = new URL(h.requests[0].url, "http://local.invalid");
  assert.equal(url.pathname, "/api/media");
  assert.equal(url.searchParams.get("account"), "synthetic-account");
  assert.equal(url.searchParams.get("id"), "a".repeat(64));
  assert.equal(url.searchParams.get("variant"), "thumbnail");
  image.emit("click");
  assert.equal(h.body.querySelector("dialog"), null);
  assert.equal(h.requests.length, 1);
  assert.equal(h.openCalls.length, 1);
  assert.equal(h.openCalls[0][1], "_blank");
  assert.equal(h.openCalls[0][2], "popup,width=960,height=760");
  url = new URL(h.openCalls[0][0], "http://local.invalid");
  assert.equal(url.pathname, "/image-viewer.html");
  assert.deepEqual(Object.fromEntries(new URLSearchParams(url.hash.slice(1))),
    { account: h.state.account, user: h.state.user, id: "a".repeat(64) });
  assert.equal(h.requests.some(item => item.url.includes("untrusted.invalid")), false);
});

test("stickers lazily request original animation through the scoped local endpoint and reuse the image viewer", () => {
  const h = harness();
  h.state.user = "synthetic&friend?query";
  const sticker = h.render({ kind: "sticker", width: 320, height: 240,
    url: "https://untrusted.invalid/sticker.gif", cdnurl: "https://untrusted.invalid/another.gif" });
  const image = sticker.querySelector("img");
  assert.equal(image.loading, "lazy");
  assert.equal(image.decoding, "async");
  assert.equal(image.referrerPolicy, "no-referrer");
  assert.equal(image.alt, "聊天表情");
  assert.equal(sticker.attributes["aria-label"], "查看表情");
  assert.equal(sticker.classList.contains("chat-media-sticker"), true);
  assert.equal(sticker.style.width, "144px");
  assert.equal(sticker.style.aspectRatio, String(4 / 3));
  assert.equal(sticker.querySelector(".chat-video-play"), null);
  const url = new URL(image.src, "http://local.invalid");
  assert.equal(url.pathname, "/api/media");
  assert.deepEqual(Object.fromEntries(url.searchParams),
    { account: h.state.account, user: h.state.user, id: "a".repeat(64), variant: "original" });
  assert.equal(h.requests.length, 1, "no thumbnail conversion or remote fallback may replace animation");
  sticker.emit("click"); sticker.emit("click");
  assert.equal(h.body.querySelector("dialog"), null);
  assert.equal(h.requests.length, 1);
  assert.equal(h.openCalls.length, 1);
  assert.equal(h.popups[0].focusCount, 1);
  assert.equal(new URL(h.openCalls[0][0], "http://local.invalid").pathname, "/image-viewer.html");
  h.reset();
  assert.equal(h.popups[0].closed, true);
});

test("stickers stay compact with missing, invalid, or extreme dimensions", () => {
  const h = harness();
  for (const dimensions of [{}, { width: Infinity, height: 100 }, { width: 100, height: NaN },
    { width: -1, height: 1 }, { width: 1, height: 0 }]) {
    const sticker = h.render({ kind: "sticker", ...dimensions });
    assert.equal(sticker.style.width, "144px");
    assert.equal(sticker.style.aspectRatio, "1");
  }
  for (const dimensions of [{ width: 1, height: 10000 }, { width: 10000, height: 1 }]) {
    const sticker = h.render({ kind: "sticker", ...dimensions });
    const width = Number.parseFloat(sticker.style.width), height = width / Number(sticker.style.aspectRatio);
    assert.ok(width > 0 && width <= 144);
    assert.ok(height > 0 && height <= 144);
  }
});

test("unavailable stickers show a compact fallback and never try message-provided remote addresses", async () => {
  const h = harness({ blockPopup: true });
  const sticker = h.render({ kind: "sticker", url: "https://untrusted.invalid/sticker.gif" });
  const image = sticker.querySelector("img"), error = sticker.querySelector(".chat-media-tile-status");
  image.emit("error");
  assert.equal(image.hidden, true);
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "表情暂不可用");
  assert.equal(error.attributes.role, "status");
  sticker.emit("click");
  await new Promise(setImmediate);
  assert.equal(error.hidden, false);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests.some(item => item.url.includes("untrusted.invalid")), false);
});

test("stale sticker errors and preview gestures cannot affect another account or conversation", async () => {
  for (const key of ["account", "user", "generation"]) {
    const native = [], h = harness({ desktopHost: { openImagePreview: async scope => { native.push(scope); return true; } } });
    const sticker = h.render({ kind: "sticker" });
    h.state[key] = key === "generation" ? 2 : "other";
    sticker.querySelector("img").emit("error");
    sticker.emit("click");
    await Promise.resolve();
    assert.equal(sticker.querySelector("img").hidden, false, key);
    assert.equal(sticker.querySelector(".chat-media-tile-status").hidden, true, key);
    assert.equal(native.length, 0, key);
    assert.equal(h.requests.length, 1, key);
  }
});

test("sticker native previews pass only local identity and ignore delayed failures after reset", async () => {
  const native = [];
  let settle;
  const h = harness({ desktopHost: { openImagePreview: scope => {
    native.push(scope);
    return new Promise(resolve => { settle = resolve; });
  } } });
  const sticker = h.render({ kind: "sticker", url: "https://untrusted.invalid/sticker.gif" });
  sticker.emit("click");
  assert.deepEqual(JSON.parse(JSON.stringify(native)),
    [{ account: h.state.account, user: h.state.user, id: "a".repeat(64) }]);
  h.reset(); settle(false);
  await new Promise(setImmediate);
  assert.equal(sticker.querySelector(".chat-media-tile-status").hidden, true);
  assert.equal(h.openCalls.length, 0);
});

test("video initially requests only a lazy frame and loads playback on click", () => {
  const h = harness(), video = h.render({ kind: "video", durationMs: 65000 });
  assert.equal(video.querySelector(".chat-video-duration").textContent, "1:05");
  assert.equal(h.made.some(item => item.tagName === "VIDEO"), false);
  assert.equal(new URL(h.requests[0].url, "http://local.invalid").searchParams.get("variant"), "frame");
  video.querySelector("img").emit("error");
  assert.equal(video.disabled, undefined, "missing frame must not prevent playback");
  video.emit("click");
  const player = h.body.querySelector("video");
  assert.equal(player.preload, "none");
  assert.equal(player.controls, true);
  assert.equal(player.playCount, 1);
  assert.equal(new URL(player.src, "http://local.invalid").searchParams.get("variant"), "playback");
});

test("voice waits for a gesture and presents only attributed WeChat transcripts as text", () => {
  const h = harness(), voice = h.render({ kind: "voice", durationMs: 12501,
    transcript: "<img src=untrusted> 合成转写", transcriptSource: "wechat" });
  assert.equal(h.requests.length, 0);
  assert.equal(h.made.some(item => item.tagName === "AUDIO"), false);
  assert.equal(voice.querySelector(".chat-voice-duration").textContent, "13″");
  assert.equal(voice.querySelector("p").textContent, "<img src=untrusted> 合成转写");
  assert.equal(voice.querySelector("img"), null);
  voice.querySelector("button").emit("click");
  const audio = h.made.find(item => item.tagName === "AUDIO");
  assert.equal(audio.playCount, 1);
  assert.equal(voice.querySelector("button").attributes["aria-pressed"], "true");
  assert.equal(h.render({ kind: "voice", transcript: "not attributed" }).querySelector("p"), null);
});

test("only one voice plays at a time; clicking it again releases its resource", () => {
  const h = harness(), first = h.render({ kind: "voice" }, "one"), second = h.render({ kind: "voice" }, "two");
  first.querySelector("button").emit("click");
  const firstAudio = h.made.find(item => item.tagName === "AUDIO");
  second.querySelector("button").emit("click");
  assert.equal(firstAudio.pauseCount, 1);
  assert.equal(firstAudio.src, "");
  assert.equal(first.querySelector("button").attributes["aria-pressed"], "false");
  const secondAudio = h.made.filter(item => item.tagName === "AUDIO")[1];
  second.querySelector("button").emit("click");
  assert.equal(secondAudio.pauseCount, 1);
  assert.equal(secondAudio.src, "");
});

test("audio failure produces a small retryable state without substituting a transcript for playback", () => {
  const h = harness(), voice = h.render({ kind: "voice", transcript: "合成转写", transcriptSource: "wechat" });
  voice.querySelector("button").emit("click");
  const audio = h.made.find(item => item.tagName === "AUDIO");
  audio.emit("error");
  assert.equal(voice.querySelector(".chat-media-unavailable").hidden, false);
  assert.equal(voice.querySelector("p").textContent, "合成转写");
  voice.querySelector("button").emit("click");
  assert.equal(h.made.filter(item => item.tagName === "AUDIO").length, 2);
  assert.equal(voice.querySelector(".chat-media-unavailable").hidden, true);
});

test("stale account or conversation gestures cannot fetch original or playable bytes", () => {
  for (const key of ["account", "user", "generation"]) {
    const h = harness(), image = h.render({ kind: "image" }), voice = h.render({ kind: "voice" });
    h.state[key] = key === "generation" ? 2 : "other";
    const before = h.requests.length;
    image.emit("click"); voice.querySelector("button").emit("click");
    assert.equal(h.requests.length, before, key);
    assert.equal(h.body.querySelector("dialog"), null);
    assert.equal(h.openCalls.length, 0);
  }
});

test("reset closes preview and stops media; late errors cannot recreate it", () => {
  const h = harness(), video = h.render({ kind: "video" });
  video.emit("click");
  const player = h.body.querySelector("video");
  h.reset();
  assert.equal(player.pauseCount, 1);
  assert.equal(player.src, "");
  assert.equal(h.body.querySelector("dialog"), null);
  player.emit("error");
  assert.equal(h.body.querySelector("dialog"), null);
});

test("replaced message window and changed scope stop existing playback", () => {
  for (const remove of [true, false]) {
    const h = harness(), voice = h.render({ kind: "voice" });
    voice.querySelector("button").emit("click");
    const audio = h.made.find(item => item.tagName === "AUDIO");
    if (remove) voice.remove(); else h.state.account = "another-account";
    h.mutate();
    assert.equal(audio.src, "");
    assert.equal(audio.pauseCount, 1);
  }
});

test("missing thumbnail does not prevent opening the separate original viewer", () => {
  const h = harness(), image = h.render({ kind: "image" });
  image.querySelector("img").emit("error");
  assert.equal(image.querySelector(".chat-media-tile-status").hidden, false);
  image.emit("click");
  assert.equal(h.openCalls.length, 1);
  assert.equal(h.body.querySelector("dialog"), null);
});

test("distinct images have independent browser windows and repeated images focus the existing one", () => {
  const h = harness(), one = h.render({ kind: "image" }), two = h.render({ kind: "image" }, "b".repeat(64));
  one.emit("click"); two.emit("click"); one.emit("click");
  assert.equal(h.popups.length, 2);
  assert.equal(h.popups[0].focusCount, 1);
  h.popups[0].close();
  one.emit("click");
  assert.equal(h.popups.length, 3);
  h.reset();
  assert.equal(h.popups.every(popup => popup.closed), true);
});

test("inline voice and video switching keeps independent image windows open", () => {
  const h = harness();
  h.render({ kind: "image" }).emit("click");
  h.render({ kind: "voice" }).querySelector("button").emit("click");
  h.render({ kind: "video" }).emit("click");
  assert.equal(h.popups[0].closed, false);
  h.reset();
  assert.equal(h.popups[0].closed, true);
});

test("native image opening passes only verified scope; only external reset closes its windows", async () => {
  const native = [], closes = [];
  const h = harness({ desktopHost: { openImagePreview: async scope => { native.push(scope); return true; },
    closeImagePreviews: () => closes.push(true) } });
  h.render({ kind: "image", url: "https://untrusted.invalid" }).emit("click");
  await Promise.resolve();
  assert.deepEqual(JSON.parse(JSON.stringify(native)), [{ account: h.state.account, user: h.state.user, id: "a".repeat(64) }]);
  assert.equal(h.openCalls.length, 0);
  assert.equal(h.body.querySelector("dialog"), null);
  h.render({ kind: "voice" }).querySelector("button").emit("click");
  h.render({ kind: "video" }).emit("click");
  assert.equal(closes.length, 0);
  h.reset();
  assert.equal(closes.length, 1);
});

test("blocked popups and failed native opens show a compact error without modal or external fallback", async () => {
  for (const options of [{ blockPopup: true }, { desktopHost: { openImagePreview: async () => false } },
    { desktopHost: { openImagePreview: async () => { throw new Error("synthetic"); } } }]) {
    const h = harness(options), image = h.render({ kind: "image" });
    image.emit("click");
    await new Promise(setImmediate);
    assert.equal(image.querySelector(".chat-media-tile-status").hidden, false);
    assert.equal(h.body.querySelector("dialog"), null);
    assert.equal(h.popups.length, 0);
    if (options.desktopHost) assert.equal(h.openCalls.length, 0);
  }
});

test("late native open failures after reset cannot change old UI or close newer windows", async () => {
  let settle, closes = 0;
  const h = harness({ desktopHost: { openImagePreview: () => new Promise(resolve => { settle = resolve; }),
    closeImagePreviews: () => { closes++; } } });
  const image = h.render({ kind: "image" });
  image.emit("click");
  h.reset(); settle(false);
  await new Promise(setImmediate);
  assert.equal(image.querySelector(".chat-media-tile-status").hidden, true);
  assert.equal(closes, 1);
});

test("invalid scope cannot open an image window", () => {
  for (const change of [{ id: "non-stable-id" }, { id: "A".repeat(64) }, { account: "bad\naccount" },
    { user: "" }, { user: "x".repeat(4097) }]) {
    const calls = [];
    const h = harness({ desktopHost: { openImagePreview: async scope => { calls.push(scope); return true; } } });
    Object.assign(h.state, change);
    h.render({ kind: "image" }, change.id).emit("click");
    assert.equal(calls.length, 0);
    assert.equal(h.openCalls.length, 0);
  }
});

test("unrecognized media leaves the existing text renderer responsible", () => {
  const h = harness();
  assert.equal(h.render({ kind: "link", url: "https://untrusted.invalid" }), null);
  assert.equal(h.requests.length, 0);
});
