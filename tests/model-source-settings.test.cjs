// Generation-model settings: several models with role assignment, edited on one detail page.
// Modified by Know Your Friends, 2026-11-01 (Apache-2.0): multi-model home replaces the single profile panel.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");

const root = path.join(__dirname, "..");
const script = readFileSync(path.join(root, "chatui/app.js"), "utf8");
const html = readFileSync(path.join(root, "chatui/index.html"), "utf8");
function section(start, end) {
  const first = script.indexOf(start);
  const last = script.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing section: ${start}`);
  return script.slice(first, last);
}
const settingsCode = section("async function api(", "function status(") +
  section("const MODEL_SOURCE_PROTOCOLS", "let managedAccounts = [];") +
  section("function closeSettingsModalNow()", "// Forget this window's") +
  section('document.querySelectorAll(".settings-tab-btn")', 'byId("btnEmoji")') +
  "globalThis.ui = { showModelSource, loadModelSource, fetchApiModels, testApiModel, " +
  "saveApiModel, deleteApiModel, applyRole, openModelDetail, showModelView, localJudgeAction, " +
  "invalidateModelDiscovery, invalidateModelTest, " +
  "syncSavedApiKeyHint, restoreModelList, toggleApiKeyVisibility, hideApiKey, " +
  "apiKeyInputChanged, apiConnectionChanged, closeSettingsModalNow, getSnapshot: () => settingsState.modelSourceSnapshot, " +
  "detailId: () => settingsState.modelDetailId, " +
  "markDirty: () => { settingsState.modelSourceDraftDirty = true; } };";

function makeNode() {
  const classes = new Set();
  return {
    value: "", textContent: "", hidden: false, disabled: false, options: [], dataset: {},
    attributes: {}, events: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    addEventListener(name, listener) { (this.events[name] ||= []).push(listener); },
    click() { for (const listener of this.events.click || []) listener({ target: this }); },
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name);
        else classes.delete(name);
      },
    },
    replaceChildren(...children) { this.options = children; },
    appendChild(child) { this.options.push(child); },
  };
}
function harness(fetchImpl, { confirms = [] } = {}) {
  const nodes = new Map();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  const tabs = Object.fromEntries(["general", "models", "plugins", "about"].map(tab => {
    const node = makeNode();
    node.dataset.tab = tab;
    return [tab, node];
  }));
  const context = vm.createContext({
    URL, AbortController, setTimeout, clearTimeout,
    window: { confirm: () => confirms.length ? confirms.shift() : false },
    document: {
      createElement: () => makeNode(),
      addEventListener() {},
      querySelectorAll: selector => selector === ".settings-tab-btn" ? Object.values(tabs) : [],
    },
    closeConversationManager() {}, renderManagedAccounts() {}, loadAboutVersion() {},
    byId,
    text: (id, value) => { byId(id).textContent = value == null ? "" : String(value); },
    fetch: fetchImpl,
    localStorage: { setItem() { throw new Error("provider settings must not use browser storage"); } },
  });
  seed(context, { currentAccount: null, currentUser: null, view: "chat", controller: null, generation: 0 });
  vm.runInContext(settingsCode, context);
  return { ui: context.ui, byId, context, tabs };
}
const response = body => ({ ok: true, json: async () => body });
const MAIN = "0".repeat(32), JUDGE = "1".repeat(32), IMAGE = "2".repeat(32);
const noneState = { mode: "none", api: null, sourceId: "local:laya", status: "active",
  models: [], roles: { main: null, judge: null, image: null }, localJudge: null };
const apiState = { mode: "api", api: { protocol: "responses", baseUrl: "https://example.test/v1", model: "model-b",
  contextTokens: 128000, hasKey: true }, sourceId: MAIN, status: "active",
  models: [{ id: MAIN, name: "", protocol: "responses", baseUrl: "https://example.test/v1", model: "model-b",
    contextTokens: 128000, timeoutMs: 3_600_000, concurrency: 4, hasKey: true }],
  roles: { main: MAIN, judge: null, image: null }, localJudge: null };
const savedModels = {
  protocol: "responses", baseUrl: "https://example.test/v1", supported: true,
  models: [{ id: "model-a", name: "Model A" }, { id: "model-b", contextTokens: 65536 }],
};
function showSaved({ ui }, state = apiState) {
  ui.showModelSource(state);
  // 详情页字段在打开模型时填充；浏览器语义下赋值即字符串。
  if (state.roles?.main) ui.openModelDetail(state.roles.main);
}

it("keeps the home and detail views with role pickers and per-model actions", () => {
  for (const id of ["apiModelSettings", "btnFetchApiModels", "apiModelCount", "selectApiModel", "inputApiModelId",
    "inputApiContextTokens", "inputApiTimeoutMinutes", "inputApiConcurrency", "btnTestApiModel", "btnSaveApiModel",
    "btnDeleteApiModel", "btnAddModel", "btnBackModelHome", "modelHomeView", "modelDetailView", "searchSettingsView",
    "selectRoleMain", "selectRoleJudge", "selectRoleImage", "modelListRows",
    "btnToggleApiKeyVisibility", "apiKeyStatus"])
    assert.match(html, new RegExp(`id="${id}"`));
  for (const id of ["btnActivateApi", "btnClearApiKey", "modelSourceActive", "inputModelMaxConcurrentTasks",
    "selectModelSource", "localModelSettings", "selectRuntimeProvider", "btnDownloadLocalModel"])
    assert.doesNotMatch(html, new RegExp(`id="${id}"`));
  assert.match(html, /id="inputApiKey" type="password"/);
  for (const protocol of ["anthropic", "responses", "chat_completions", "gemini", "ollama"])
    assert.match(html, new RegExp(`value="${protocol}"`));
});

it("loads the saved profile into the home view without exposing its key", async () => {
  const { ui, byId } = harness(async () => response(apiState));
  await ui.loadModelSource();
  assert.deepEqual(byId("selectRoleMain").options.map(option => option.value), [MAIN]);
  assert.equal(byId("selectRoleMain").value, MAIN);
  assert.equal(byId("selectRoleJudge").value, "");
  assert.match(byId("selectRoleJudge").options[0].textContent, /跟随主模型/);
  assert.equal(byId("modelListRows").options.length, 1);
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(byId("apiKeySaved").hidden, true, "detail fields stay empty until a model is opened");
});

it("restores the saved model list and selection on the detail page with one local read", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, options });
    return response({ ...apiState, modelList: savedModels });
  });
  await ui.loadModelSource();
  ui.openModelDetail(MAIN);
  assert.deepEqual(requests.map(request => request.url), ["/api/model-source"]);
  assert.deepEqual(byId("selectApiModel").options.map(option => option.value), ["", "model-a", "model-b"]);
  assert.equal(byId("selectApiModel").value, "model-b");
  assert.equal(byId("selectApiModel").disabled, false);
  assert.equal(byId("inputApiModelId").value, "model-b");
  assert.equal(byId("inputApiContextTokens").value, "128000", "cached metadata must not overwrite a saved budget");
  assert.match(byId("apiModelCount").textContent, /2/);
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(byId("inputApiKey").type, "password");
  assert.match(byId("inputApiKey").placeholder, /^[•●*·]{4,}$/);
  assert.equal(ui.detailId(), MAIN);
  assert.equal(byId("btnDeleteApiModel").hidden, true, "主模型不可删除");
});

it("restores a fetched catalog and its connection before a model profile has been saved", async () => {
  const requests = [];
  const { ui, byId } = harness(async url => {
    requests.push(url);
    return response({ ...noneState, modelList: { ...savedModels, protocol: "chat_completions" } });
  });
  await ui.loadModelSource();
  assert.deepEqual(requests, ["/api/model-source"]);
  ui.openModelDetail(null);
  assert.equal(byId("selectApiProtocol").value, "chat_completions");
  assert.equal(byId("inputApiBaseUrl").value, savedModels.baseUrl);
  assert.deepEqual(byId("selectApiModel").options.map(option => option.value), ["", "model-a", "model-b"]);
  assert.equal(byId("inputApiModelId").value, "");
  assert.equal(ui.detailId(), null);
  assert.equal(byId("btnDeleteApiModel").hidden, true);
  assert.equal(byId("apiKeySaved").hidden, true);
});

it("saves edits to the main model through models/save and keeps its role", async () => {
  const requests = [];
  const saved = { ...apiState, api: { ...apiState.api, model: "model-a" }, modelList: savedModels };
  const state = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(saved);
  }, { confirms: [true] });
  showSaved(state, { ...apiState, modelList: savedModels });
  state.ui.openModelDetail(MAIN);
  state.byId("inputApiModelId").value = "model-a";
  await state.ui.saveApiModel();
  assert.equal(requests[0].url, "/api/model-source/models/save");
  assert.equal(requests[0].body.id, MAIN);
  assert.equal(requests[0].body.model, "model-a");
  assert.equal(state.ui.getSnapshot().roles.main, MAIN, "主模型身份变了，id 不变");
  assert.equal(state.byId("modelListRows").options.length, 1);
  await state.ui.loadModelSource();
  assert.equal(state.ui.getSnapshot().api.model, "model-a");
});

it("saves a new model without changing roles and lists it on the home view", async () => {
  const requests = [];
  const JUDGE_STATE = { ...apiState, models: [...apiState.models, {
    id: JUDGE, name: "判断", protocol: "anthropic", baseUrl: "https://judge.test/v1", model: "judge-model",
    contextTokens: 16384, timeoutMs: 3_600_000, concurrency: 2, hasKey: true }] };
  const state = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(JUDGE_STATE);
  });
  showSaved(state);
  state.ui.openModelDetail(null);
  state.byId("selectApiProtocol").value = "anthropic";
  state.byId("inputApiBaseUrl").value = "https://judge.test/v1";
  state.byId("inputApiModelId").value = "judge-model";
  state.byId("inputApiConcurrency").value = "2";
  state.byId("inputApiKey").value = "SYNTHETIC_JUDGE_KEY";
  await state.ui.saveApiModel();
  assert.equal(requests[0].url, "/api/model-source/models/save");
  assert.equal(requests[0].body.concurrency, 2);
  assert.equal(Object.hasOwn(requests[0].body, "id"), false);
  assert.equal(state.ui.getSnapshot().roles.main, MAIN, "添加模型不改变用途");
  assert.equal(state.byId("modelListRows").options.length, 2);
  assert.equal(state.byId("selectRoleJudge").options.length, 3, "跟随主模型 + 两个模型");
});

it("edits and probes an optional model using its own saved key without a main-model warning", async () => {
  const secondary = { ...apiState.models[0], id: JUDGE, model: "judge-model" };
  const both = { ...apiState, models: [...apiState.models, secondary] };
  const requests = [];
  const state = harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response(url.endsWith("/reveal-key") ? { apiKey: "SYNTHETIC_JUDGE_KEY" } :
      url.endsWith("/test") ? { ok: true, latencyMs: 1 } :
      url.endsWith("/list") ? savedModels : both);
  }); // An unexpected confirmation defaults to refusal in this harness.
  showSaved(state, both);
  state.ui.openModelDetail(JUDGE);
  assert.equal(state.byId("apiKeySaved").hidden, false);
  await state.ui.toggleApiKeyVisibility();
  assert.equal(state.byId("inputApiKey").value, "SYNTHETIC_JUDGE_KEY");
  state.ui.hideApiKey();
  await state.ui.testApiModel();
  await state.ui.fetchApiModels();
  state.byId("inputApiModelId").value = "judge-model-edited";
  await state.ui.saveApiModel();
  assert.equal(requests.length, 4);
  assert.ok(requests.every(request => request.body.id === JUDGE));
  assert.ok(requests.every(request => !Object.hasOwn(request.body, "apiKey")));
  assert.equal(requests[3].url, "/api/model-source/models/save");
});

it("invalidates a saved-key reveal before switching model detail pages", async () => {
  let finishReveal;
  const both = { ...apiState, models: [...apiState.models, { ...apiState.models[0], id: JUDGE }] };
  const state = harness(() => new Promise(resolve => { finishReveal = resolve; }));
  showSaved(state, both);
  const pending = state.ui.toggleApiKeyVisibility();
  state.ui.openModelDetail(JUDGE);
  finishReveal(response({ apiKey: "SYNTHETIC_OLD_MODEL_KEY" }));
  await pending;
  assert.equal(state.byId("inputApiKey").value, "");
  assert.equal(state.byId("inputApiKey").type, "password");
});

it("prepares, cancels and deletes the local judge through its own detail page", async () => {
  const requests = [];
  const local = {supported: true, state: "missing", ready: false, totalBytes: 168166496, downloadedBytes: 0};
  const state = harness(async (url, options) => {
    requests.push({url, body: JSON.parse(options.body)});
    return response({...apiState, localJudge: {...local,
      ...(url.endsWith("/prepare") ? {state: "downloading", downloadedBytes: 84083248} : {})}});
  });
  showSaved(state, {...apiState, localJudge: local});
  assert.equal(state.byId("selectRoleJudge").options.at(-1).value, "download-local");
  await state.ui.applyRole("judge", "download-local");
  assert.equal(state.byId("localJudgeView").hidden, false);
  assert.equal(state.byId("localJudgeProgress").value, 84083248);
  assert.match(state.byId("localJudgeStatus").textContent, /50%/);
  assert.equal(state.byId("btnCancelLocalJudge").hidden, false);
  await state.ui.localJudgeAction("cancel");
  await state.ui.localJudgeAction("delete");
  assert.deepEqual(requests.map(item => item.url.split("/").at(-1)), ["prepare", "cancel", "delete"]);
  assert.ok(requests.every(item => Object.keys(item.body).length === 0));
});

it("keeps the text-only builtin judge out of main and image role choices", () => {
  const builtin = {id: JUDGE, builtin: "julia-1", protocol: "systemone", model: "julia-1", concurrency: 1};
  const state = harness();
  showSaved(state, {...apiState, models: [...apiState.models, builtin],
    localJudge: {supported: true, state: "ready", ready: true, downloadedBytes: 168166496}});
  for (const id of ["selectRoleMain", "selectRoleImage"])
    assert.ok(state.byId(id).options.every(option => option.value !== JUDGE));
  assert.ok(state.byId("selectRoleJudge").options.some(option => option.value === JUDGE));
  state.ui.openModelDetail(JUDGE);
  assert.equal(state.byId("localJudgeView").hidden, false);
  assert.equal(state.byId("modelDetailView").hidden, true);
});

it("assigns and clears roles through one endpoint", async () => {
  const requests = [];
  const assigned = { ...apiState, roles: { main: MAIN, judge: JUDGE, image: null } };
  const cleared = { ...apiState, roles: { main: MAIN, judge: null, image: null } };
  const state = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(JSON.parse(options.body)?.judge ? assigned : cleared);
  });
  showSaved(state);
  await state.ui.applyRole("judge", JUDGE);
  assert.equal(requests[0].url, "/api/model-source/roles");
  assert.deepEqual(requests[0].body, { judge: JUDGE });
  assert.equal(state.byId("selectRoleJudge").value, JUDGE);
  await state.ui.applyRole("judge", "");
  assert.deepEqual(requests[1].body, { judge: null });
  assert.equal(state.byId("selectRoleJudge").value, "");
});

it("asks once before a main-model role change and reverts the picker on refusal", async () => {
  const requests = [];
  const state = harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response(apiState);
  }, { confirms: [false, true] });
  showSaved(state);
  await state.ui.applyRole("main", JUDGE);
  assert.equal(requests.length, 0, "拒绝后不发起保存");
  await state.ui.applyRole("main", JUDGE);
  assert.deepEqual(requests[0].body, { main: JUDGE });
});

it("deletes a non-main model and falls its roles back to the main model", async () => {
  const withJudge = { ...apiState, models: [...apiState.models, {
    id: JUDGE, name: "判断", protocol: "anthropic", baseUrl: "https://judge.test/v1", model: "judge-model",
    contextTokens: 16384, timeoutMs: 3_600_000, concurrency: 2, hasKey: true }],
    roles: { main: MAIN, judge: JUDGE, image: null } };
  const state = harness(async (url) => {
    if (url.endsWith("/models/delete")) return response({ ...apiState, roles: { main: MAIN, judge: null, image: null } });
    return response(withJudge);
  });
  showSaved(state, withJudge);
  state.ui.openModelDetail(JUDGE);
  assert.equal(state.byId("btnDeleteApiModel").hidden, false);
  await state.ui.deleteApiModel();
  assert.equal(state.ui.getSnapshot().roles.judge, null);
  assert.equal(state.byId("selectRoleJudge").value, "", "删除后退回跟随主模型");
});

it("reveals a saved key only on eye click and does not resubmit it as a new key", async () => {
  const requests = [];
  const state = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(url.endsWith("/reveal-key") ? { apiKey: "SYNTHETIC_SAVED_KEY" } : apiState);
  });
  const { ui, byId } = state;
  showSaved(state);
  ui.openModelDetail(MAIN);
  assert.equal(requests.length, 0);
  assert.equal(byId("inputApiKey").value, "");
  await ui.toggleApiKeyVisibility();
  assert.deepEqual(requests[0], { url: "/api/model-source/reveal-key", body: {
    id: MAIN, protocol: apiState.api.protocol, baseUrl: apiState.api.baseUrl,
  } });
  assert.equal(byId("inputApiKey").type, "text");
  assert.equal(byId("inputApiKey").value, "SYNTHETIC_SAVED_KEY");
  await ui.saveApiModel();
  assert.equal(Object.hasOwn(requests[1].body, "apiKey"), false, "留空沿用已存密文");
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(byId("inputApiKey").type, "password");
});

it("shows and hides an unsaved typed key without fetching or clearing the draft", async () => {
  const requests = [];
  const state = harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response(apiState);
  }, { confirms: [true] });
  showSaved(state);
  state.ui.openModelDetail(MAIN);
  state.byId("inputApiKey").value = "SYNTHETIC_NEW_KEY";
  state.ui.apiKeyInputChanged();
  await state.ui.toggleApiKeyVisibility();
  assert.equal(state.byId("inputApiKey").type, "text");
  await state.ui.toggleApiKeyVisibility();
  assert.equal(state.byId("inputApiKey").type, "password");
  assert.equal(state.byId("inputApiKey").value, "SYNTHETIC_NEW_KEY");
  assert.equal(requests.length, 0);
  await state.ui.saveApiModel();
  assert.equal(requests[0].body.apiKey, "SYNTHETIC_NEW_KEY");
});

it("saves edits to a revealed saved key as the user's replacement key", async () => {
  const requests = [];
  const state = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(url.endsWith("/reveal-key") ? { apiKey: "SYNTHETIC_SAVED_KEY" } : apiState);
  }, { confirms: [true] });
  showSaved(state);
  state.ui.openModelDetail(MAIN);
  await state.ui.toggleApiKeyVisibility();
  state.byId("inputApiKey").value = "SYNTHETIC_REPLACEMENT_KEY";
  state.ui.apiKeyInputChanged();
  assert.equal(state.byId("inputApiKey").type, "text", "editing must preserve the user's visible mode");
  state.ui.hideApiKey();
  assert.equal(state.byId("inputApiKey").value, "SYNTHETIC_REPLACEMENT_KEY");
  await state.ui.saveApiModel();
  assert.equal(requests[1].body.apiKey, "SYNTHETIC_REPLACEMENT_KEY");
});

it("a reveal error leaves the key masked and does not surface provider response text", async () => {
  const state = harness(async () => ({ ok: false, status: 409,
    json: async () => ({ error: "SYNTHETIC_SECRET_IN_ERROR" }),
  }));
  showSaved(state);
  state.ui.openModelDetail(MAIN);
  await state.ui.toggleApiKeyVisibility();
  assert.equal(state.byId("inputApiKey").type, "password");
  assert.equal(state.byId("inputApiKey").value, "");
  assert.match(state.byId("apiKeyStatus").textContent, /失败|无法读取/);
  assert.doesNotMatch(state.byId("apiKeyStatus").textContent, /SYNTHETIC_SECRET_IN_ERROR/);
});

it("only offers reuse of a saved key for its original protocol and Base URL", () => {
  const { ui, byId } = harness(async () => response(noneState));
  ui.showModelSource(apiState);
  ui.openModelDetail(MAIN);
  assert.equal(byId("apiKeySaved").hidden, false);
  byId("inputApiBaseUrl").value = "https://different.test/v1";
  ui.syncSavedApiKeyHint();
  assert.equal(byId("apiKeySaved").hidden, true);
  assert.equal(byId("inputApiKey").placeholder, "按服务要求填写 API Key");
});

it("uses a manual model when listing is unsupported, tests it, and saves only on success", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url.endsWith("/list")) return response({ supported: false, models: [] });
    if (url.endsWith("/test")) return response({ ok: true, latencyMs: 37.6 });
    return response(apiState);
  });
  ui.showModelSource(noneState);
  ui.openModelDetail(null);
  byId("selectApiProtocol").value = "responses";
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiModelId").value = "model-b";
  byId("inputApiContextTokens").value = "128000";
  byId("inputApiKey").value = "SYNTHETIC_KEY";
  await ui.fetchApiModels();
  assert.equal(byId("apiModelCount").textContent, "此接口不提供模型列表，可手动填写模型 ID");
  await ui.testApiModel();
  assert.equal(byId("apiModelTestStatus").textContent, "连接成功 · 38 ms");
  await ui.saveApiModel();
  assert.equal(requests[1].body.model, "model-b");
  assert.equal(requests[2].body.model, "model-b");
  assert.equal(requests[2].body.concurrency, 4);
  assert.equal(requests[2].body.contextTokens, 128000);
  assert.equal(requests[2].body.timeoutMs, 3_600_000);
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(ui.getSnapshot().mode, "api");
});

it("keeps the previous profile when saving fails", async () => {
  const { ui, byId } = harness(async () => ({ ok: false, status: 401 }));
  ui.showModelSource(noneState);
  ui.openModelDetail(null);
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiModelId").value = "model-b";
  byId("inputApiContextTokens").value = "4096";
  await ui.saveApiModel();
  assert.equal(ui.getSnapshot().mode, "none");
  assert.match(byId("modelSourceStatus").textContent, /保存失败（HTTP 401）/);
  assert.equal(byId("inputApiModelId").value, "model-b");
});

it("saves a longer generation deadline in milliseconds while retaining the saved profile", async () => {
  const requests = [];
  const updated = { ...apiState, api: { ...apiState.api, timeoutMs: 7_200_000 },
    models: [{ ...apiState.models[0], timeoutMs: 7_200_000 }] };
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return response(updated);
  });
  showSaved({ ui });
  byId("inputApiContextTokens").value = "128000";
  byId("inputApiTimeoutMinutes").value = "120";
  await ui.saveApiModel();
  assert.equal(requests[0].body.timeoutMs, 7_200_000);
  assert.equal(requests[0].body.contextTokens, 128000);
  assert.equal(requests[0].body.model, apiState.api.model);
  assert.equal(Object.hasOwn(requests[0].body, "apiKey"), false);
  assert.equal(ui.getSnapshot().sourceId, apiState.sourceId);
  assert.equal(byId("inputApiTimeoutMinutes").value, "120");
  assert.match(byId("modelSourceStatus").textContent, /已保存/);
});

it("rejects invalid generation deadlines and context sizes without saving", async () => {
  const requests = [];
  const { ui, byId } = harness(async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response(apiState);
  }, { confirms: [true] });
  ui.showModelSource(apiState);
  ui.openModelDetail(MAIN);
  byId("inputApiContextTokens").value = "128000";
  for (const value of ["0", "181", "1.5", "not-a-number"]) {
    byId("inputApiTimeoutMinutes").value = value;
    await ui.saveApiModel();
  }
  assert.equal(requests.length, 0);
  assert.match(byId("modelSourceStatus").textContent, /1～180 分钟/);
  for (const value of ["4095", "1000001", "8192.5", "invalid"]) {
    byId("inputApiTimeoutMinutes").value = "60";
    byId("inputApiContextTokens").value = value;
    await ui.saveApiModel();
  }
  assert.equal(requests.length, 0);
  byId("inputApiContextTokens").value = "4096";
  byId("inputApiConcurrency").value = "0";
  await ui.saveApiModel();
  assert.equal(requests.length, 0, "非法并发同样不发出保存");
  byId("inputApiConcurrency").value = "4";
  await ui.saveApiModel();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].timeoutMs, 3_600_000);
});
