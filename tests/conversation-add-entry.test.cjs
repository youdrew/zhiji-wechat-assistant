"use strict";
/*
 * Regression test: "首次使用选择一个聊天，之后没法添加别人了".
 *
 * The sidebar's only in-app entry into the conversation manager used to live
 * inside the empty state and was guarded by `!chatState.selectedConversations.size`.
 * Selecting the first conversation therefore removed the entry permanently, so a
 * second person could not be added from the sidebar.
 *
 * This loads the real chatui/index.html + app.js over a loopback server (so the
 * sibling <script src> files run exactly as in the browser), stubs the bridge
 * API, drives the reported user flow and asserts a working add entry stays
 * reachable after the first conversation is selected.
 *
 * Requires jsdom; the test skips cleanly when it is not installed.
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const CHATUI = path.join(__dirname, "..", "chatui");

let JSDOM = null;
let VirtualConsole = null;
let startStaticServer = null;
let unavailable = null;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
  ({ startStaticServer } = require("./helpers/static-server.cjs"));
} catch (error) {
  unavailable = `jsdom is required for this DOM test (${error.message})`;
}

const SELF = { username: "wxid_me", name: "我", avatar: "" };
const SESSIONS = [
  { username: "wxid_alice", name: "小爱", preview: "在吗", time: 1700000000, isGroup: false, avatar: "" },
  { username: "wxid_bob", name: "阿波", preview: "收到", time: 1700000100, isGroup: false, avatar: "" },
  { username: "room_family", name: "家庭群", preview: "晚安", time: 1700000200, isGroup: true, avatar: "" },
];

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return body; },
    async text() { return JSON.stringify(body); },
  };
}

// Load the real UI and stub the loopback bridge it talks to.
async function loadClient() {
  const { server, origin } = await startStaticServer(CHATUI);
  const virtualConsole = new VirtualConsole();
  const jsdomErrors = [];
  virtualConsole.on("jsdomError", error => jsdomErrors.push(String((error && error.message) || error)));

  const dom = await JSDOM.fromURL(`${origin}/index.html`, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  const bridge = { selected: new Set(), selectionPosts: [] };

  window.fetch = async (url, options = {}) => {
    const pathname = String(url).replace(/^https?:\/\/[^/]+/, "");
    const method = (options.method || "GET").toUpperCase();
    const body = options.body ? JSON.parse(options.body) : null;
    if (pathname === "/api/sessions") {
      return jsonResponse({ account: "acct-1", self: SELF, sessions: SESSIONS, messagesReady: true });
    }
    if (pathname === "/api/conversation-selection") {
      if (method === "POST") {
        bridge.selectionPosts.push(body);
        if (body.selected) bridge.selected.add(body.session);
        else bridge.selected.delete(body.session);
        return jsonResponse({ account: "acct-1", initialized: true, selectedSessions: [...bridge.selected] });
      }
      return jsonResponse({
        account: "acct-1",
        initialized: bridge.selectionPosts.length > 0,
        selectedSessions: [...bridge.selected],
      });
    }
    if (pathname === "/api/messages/batch") {
      const users = Array.isArray(body && body.users) ? body.users : [];
      return jsonResponse({
        account: "acct-1",
        windows: users.map(user => ({
          user,
          messages: [{ id: `${user}-1`, type: "text", content: "你好", time: 1700000000, sender: user, isSelf: false }],
          hasMoreBefore: false,
        })),
      });
    }
    if (pathname === "/api/runtime") return jsonResponse({ provider: "cpu" });
    if (pathname === "/api/model-source") return jsonResponse({ kind: "local", active: "local" });
    if (pathname === "/api/local-model") return jsonResponse({ state: "ready", modelReady: true });
    if (pathname === "/api/analysis-cache") return jsonResponse({ account: "acct-1", sources: [] });
    if (pathname === "/api/accounts") return jsonResponse({ accounts: [], currentAccountId: null });
    return jsonResponse({});
  };
  if (!window.AbortController) {
    window.AbortController = class { constructor() { this.signal = {}; } abort() { } };
  }
  for (let i = 0; i < 200 && window.eval("typeof loadSessions") !== "function"; i++) {
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  return {
    window,
    bridge,
    appErrors: jsdomErrors.filter(message => !/Could not parse CSS|Not implemented/.test(message)),
    close: () => server.close(),
  };
}

async function flush(window, times = 40) {
  for (let i = 0; i < times; i++) await new Promise(resolve => window.setTimeout(resolve, 0));
}

// The three in-app routes back into the conversation manager.
function addEntryState(window) {
  const doc = window.document;
  const sidebar = doc.getElementById("btnAddConversation");
  return {
    sidebarUsable: Boolean(sidebar) && !sidebar.disabled,
    sidebarEmpty: doc.querySelector("#sessionList .session-empty .settings-action-btn"),
    chatEmpty: doc.querySelector("#chatMessages .chat-empty .settings-action-btn"),
  };
}

function addEntryReachable(window) {
  const state = addEntryState(window);
  return state.sidebarUsable || Boolean(state.sidebarEmpty) || Boolean(state.chatEmpty);
}

test("a second conversation stays addable after the first is selected",
  { skip: unavailable || false },
  async () => {
    const client = await loadClient();
    const { window, bridge, appErrors, close } = client;
    try {
      assert.equal(appErrors.length, 0, `app.js raised errors: ${appErrors[0]}`);
      assert.equal(window.eval("typeof loadSessions"), "function", "loadSessions must be global");

      // Startup: the session directory loads, nothing is selected yet.
      window.eval("void loadSessions()");
      await flush(window);
      assert.ok(addEntryState(window).sidebarUsable,
        "the sidebar add entry must be usable once the session directory is ready");

      // Select the FIRST conversation (the reported action).
      window.eval("void toggleConversationSelected('wxid_alice')");
      await flush(window);
      assert.ok(bridge.selected.has("wxid_alice"), "the first conversation must be persisted as selected");

      // The add entry must still be reachable.
      const afterOne = addEntryState(window);
      assert.ok(addEntryReachable(window),
        "after selecting one conversation a working add entry must remain " +
        `(sidebar=${afterOne.sidebarUsable}, sidebarEmpty=${Boolean(afterOne.sidebarEmpty)}, ` +
        `chatEmpty=${Boolean(afterOne.chatEmpty)})`);

      // Open the manager through that entry.
      if (afterOne.sidebarUsable) window.document.getElementById("btnAddConversation").click();
      else (afterOne.sidebarEmpty || afterOne.chatEmpty).click();
      await flush(window, 30);

      const manager = window.document.getElementById("conversationManager");
      assert.equal(manager.hidden, false, "the conversation manager must open");
      assert.ok(window.document.getElementById("settingsModal").classList.contains("show"),
        "the settings modal hosting the manager must be open");
      const offered = [...manager.querySelectorAll(".conversation-manager-row strong")].map(node => node.textContent);
      assert.ok(offered.includes("阿波"), "the manager must offer people who are not added yet");

      // Add the SECOND person.
      const postedBefore = bridge.selectionPosts.length;
      window.eval("void toggleConversationSelected('wxid_bob')");
      await flush(window);
      assert.ok(bridge.selected.has("wxid_bob"), "the second conversation must be added");
      assert.equal(bridge.selectionPosts.length, postedBefore + 1,
        "adding the second conversation must reach the backend");

      // Both conversations render in the sidebar.
      const rendered = [...window.document.querySelectorAll("#sessionList .session-item")]
        .map(node => node.querySelector(".session-name") && node.querySelector(".session-name").textContent);
      assert.ok(rendered.includes("小爱"), "the first conversation stays in the sidebar");
      assert.ok(rendered.includes("阿波"), "the second conversation appears in the sidebar");

      // The entry survives further growth, and the toggle collapses cleanly.
      assert.ok(addEntryReachable(window), "the add entry must still exist with two conversations selected");
      window.document.getElementById("btnAddConversation").click();
      await flush(window, 20);
      assert.equal(window.document.getElementById("conversationManager").hidden, true,
        "clicking the add entry again must collapse the manager");
      assert.ok(addEntryReachable(window), "the add entry stays reachable after collapsing");
    } finally {
      window.close();
      close();
    }
  });
