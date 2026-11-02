// Copyright 2026 Zhiji WeChat Assistant contributors. SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = readFileSync(path.join(__dirname, "../chatui/message-links.js"), "utf8");

function harness(desktopHost) {
  const made = [];
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.ownText = ""; this.listeners = {}; made.push(this); }
    set textContent(value) { this.ownText = String(value); this.children = []; }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    emit(type, button = 0) {
      const event = { type, button, prevented: false, preventDefault() { this.prevented = true; } };
      this.listeners[type]?.(event); return event;
    }
  }
  const window = { desktopHost }, document = { createElement: tag => new Element(tag),
    createTextNode: text => Object.assign(new Element("#text"), { textContent: text }) };
  vm.runInNewContext(source, { window, document, URL });
  return { render: message => window.MessageLinks.render(message), made };
}

test("share cards retain title, description and source as text and link to the real URL", () => {
  const h = harness(), card = h.render({ kind: "other", text: "old title", link: {
    title: "合成分享 <img src=x onerror=alert(1)>", description: "第一行\n第二行", source: "小红书",
    url: "https://example.com/note?x=1&y=2",
  } });
  assert.equal(card.tag, "a");
  assert.equal(card.href, "https://example.com/note?x=1&y=2");
  assert.equal(card.target, "_blank"); assert.equal(card.rel, "noopener noreferrer");
  assert.equal(card.referrerPolicy, "no-referrer");
  assert.equal(card.children[0].textContent, "合成分享 <img src=x onerror=alert(1)>");
  assert.equal(card.children[1].textContent, "第一行\n第二行");
  assert.equal(card.children[2].textContent, "↗ 小红书");
  assert.equal(h.made.some(node => ["img", "iframe", "script"].includes(node.tag)), false);
  assert.equal(card.emit("click").prevented, false, "browser anchors retain native navigation");
});

test("missing or unsafe share URLs remain recognizable cards without navigation", () => {
  const h = harness();
  for (const url of [null, "", "javascript:alert(1)", "file:///C:/secret", "weixin://open", "//example.com",
    "https://user:pass@example.com/", "https://example.com\\@evil.invalid/", "https://example.com/\nfoo", "https://example.com/a b"]) {
    const card = h.render({ text: "合成标题", link: { url } });
    assert.equal(card.tag, "div", String(url));
    assert.equal(card.children[0].textContent, "合成标题");
    assert.equal(card.children.at(-1).textContent, "链接地址不可用");
    assert.equal(card.href, undefined);
  }
});

test("plain text links keep all original text including whitespace and sentence punctuation", () => {
  const h = harness(), text = "查看（https://example.com/a_(b)）。\n还有 http://example.org/?a=1&b=2!\n<img src=x>";
  const bubble = h.render({ kind: "text", text });
  assert.equal(bubble.textContent, text);
  const links = bubble.children.filter(node => node.tag === "a");
  assert.deepEqual(links.map(node => node.href), ["https://example.com/a_(b)", "http://example.org/?a=1&b=2"]);
  assert.equal(links.every(node => node.className === "msg-text-link"), true);
  assert.equal(h.made.some(node => node.tag === "img"), false);
});

test("ordinary messages and other media stay with their existing renderer", () => {
  const h = harness();
  for (const message of [{ kind: "text", text: "好的\n明天见" }, { kind: "image", text: "https://example.com" },
    { kind: "other", text: "<msg><url>https://example.com</url></msg>" },
    { kind: "text", text: "javascript:alert(1) file:///secret https://u:p@example.com/" }]) {
    assert.equal(h.render(message), null);
  }
});

test("Chinese sentence punctuation separates the URL from the following words", () => {
  const h = harness(), text = "请看 https://example.com，周末再聊。还有 https://example.org/说明。";
  const bubble = h.render({ kind: "text", text });
  assert.equal(bubble.textContent, text);
  assert.deepEqual(bubble.children.filter(node => node.tag === "a").map(node => node.href),
    ["https://example.com/", "https://example.org/%E8%AF%B4%E6%98%8E"]);
});

test("desktop clicks use the controlled opener, and rendering alone never opens links", async () => {
  const calls = [], h = harness({ openChatLink: url => { calls.push(url); return Promise.resolve(true); } });
  const card = h.render({ link: { title: "合成分享", url: "https://example.com/" } });
  const bubble = h.render({ kind: "text", text: "http://example.org" });
  assert.deepEqual(calls, []);
  assert.equal(card.emit("click").prevented, true);
  assert.equal(bubble.children.find(node => node.tag === "a").emit("auxclick", 1).prevented, true);
  assert.equal(card.emit("auxclick", 2).prevented, false);
  await Promise.resolve();
  assert.deepEqual(calls, ["https://example.com/", "http://example.org/"]);
});

test("link renderer loads before app and is used inside messageNode", () => {
  const html = readFileSync(path.join(__dirname, "../chatui/index.html"), "utf8");
  const app = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
  assert.ok(html.indexOf('src="message-links.js"') < html.indexOf('src="app.js"'));
  const start = app.indexOf("function messageNode(");
  assert.match(app.slice(start, app.indexOf("function renderMessages(", start)), /window\.MessageLinks\?\.render\(message\)/);
});
