"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 模拟对话: continue the latest real chat with a simulated friend. The session lives in this plugin's
// backend memory (page.simulation*); nothing here is sent to WeChat or saved anywhere.
// The chat follows the self-analysis 聊天试用 panel: one line per message, Enter sends the turn,
// and a running reply is polled once a second.
(function () {
  const A = window.FriendApp;
  const MAX_MESSAGES = 6;
  const S = {
    data: null, request: 0, mounted: false,
    // The request being polled, what it does ("send" | "reroll"), and the poll loop's serial.
    pending: null, kind: null, serial: 0, timer: null, sent: null, error: "", loadError: "",
  };
  const ui = {};

  const strings = value => Array.isArray(value) ? value.filter(item => typeof item === "string") : [];
  const friendName = () => String(A.scope.name || "").trim();
  const friendAvatar = () => Array.from(friendName())[0] || "友";

  function friendly(error) {
    if (error?.code === "api-not-configured") return "尚未配置生成模型，请在设置的模型设置中保存模型后重试。";
    if (error?.code === "unknown-method") return "当前软件暂不支持模拟对话，请更新后重试。";
    return String(error?.message || "").slice(0, 300) || "模拟回复未完成，请重试。";
  }
  // pending: null when idle, "" while a send or reroll is being started, then the polled requestId.
  const busy = () => S.pending !== null;
  function cancelTimer() { if (S.timer !== null) clearTimeout(S.timer); S.timer = null; }

  // -- skeleton: built once so the draft in the input survives re-renders -------------------------------
  function mount() {
    if (S.mounted) return;
    S.mounted = true;
    const root = A.byId("pageSimulation");
    // At most one thin grey line above the chat, hidden when there is nothing to say.
    ui.notice = A.el("p", "fa-sim-notice");
    ui.log = A.el("div", "fa-sim-log");
    ui.log.setAttribute("role", "log");
    ui.log.setAttribute("aria-live", "polite");
    const composer = A.el("div", "fa-sim-composer");
    ui.input = A.el("textarea");
    ui.input.rows = 3;
    ui.input.maxLength = 2000;
    ui.input.placeholder = "以你的身份发消息，换行分条，Enter 发送";
    ui.input.setAttribute("aria-label", "以你的身份发消息");
    ui.input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send(); }
    });
    const bar = A.el("div", "fa-sim-bar");
    ui.status = A.el("span", "fa-sim-status");
    ui.status.setAttribute("role", "status");
    ui.reset = A.button("重新开始", "fa-sim-link", () => { void reset(); });
    ui.send = A.button("发送", "fa-primary", () => { void (busy() ? cancel() : send()); });
    bar.append(ui.status, ui.reset, ui.send);
    composer.append(ui.input, bar);
    root.replaceChildren(ui.notice, ui.log, composer);
  }

  async function load() {
    mount();
    const request = ++S.request;
    try {
      const data = await A.rpc("page.simulation");
      if (request !== S.request || A.masked) return;
      apply(data);
    } catch (error) {
      if (request !== S.request || A.masked) return;
      S.loadError = friendly(error);
      render();
    }
  }
  function apply(data) {
    S.data = data || {};
    S.loadError = "";
    const turns = Array.isArray(S.data.turns) ? S.data.turns : [];
    const pending = typeof S.data.pending === "string" && S.data.pending ? S.data.pending : null;
    if (pending && pending !== S.pending) {
      // A reply started before this page opened: a last friend turn means it is being rerolled.
      S.kind = turns.at(-1)?.side === "friend" ? "reroll" : "send";
      void poll(pending);
    } else if (!pending && S.pending) {
      // Finished (or was reset elsewhere) before this page's poll noticed.
      S.serial++; cancelTimer(); S.pending = null; S.kind = null;
    }
    S.sent = null;
    render();
  }

  // -- rendering ------------------------------------------------------------------------------------------
  // The single most important notice: model not saved > model unavailable > history still loading > analysis stale.
  // Only explicit backend values count, so an older backend without these fields shows nothing.
  function notice(data) {
    if (data.model === "missing" || (data.model === undefined && data.configured === false))
      return "尚未配置生成模型，请先在设置的模型设置中保存模型。";
    if (data.model === "unavailable") return "生成模型暂时不可用，请稍后重试。";
    if (data.historyReady === false) return "聊天历史仍在读取，模拟暂时只参考已读到的部分。";
    return typeof data.warning === "string" ? data.warning : "";
  }
  // One side's consecutive bubbles beside its avatar, as in a WeChat chat window.
  function row(side, texts) {
    const node = A.el("div", `fa-sim-row ${side}`);
    const avatar = A.el("span", "fa-sim-avatar", side === "me" ? "我" : friendAvatar());
    avatar.setAttribute("aria-hidden", "true");
    const bubbles = A.el("div", "fa-sim-bubbles");
    for (const text of texts) bubbles.append(typeof text === "string" ? A.el("p", "fa-sim-bubble", text) : text);
    node.append(avatar, bubbles);
    return { node, bubbles };
  }
  function render() {
    if (A.masked || !S.mounted) return;
    const data = S.data || {}, context = Array.isArray(data.context) ? data.context : [];
    let turns = Array.isArray(data.turns) ? data.turns.slice() : [];
    if (busy() && S.kind === "reroll" && turns.at(-1)?.side === "friend") turns = turns.slice(0, -1);
    if (S.sent && !(turns.at(-1)?.side === "me" && sameTexts(turns.at(-1).messages, S.sent)))
      turns.push({ id: "", side: "me", messages: S.sent });
    ui.notice.textContent = ui.notice.title = notice(data);
    ui.notice.hidden = !ui.notice.textContent;

    const nodes = [];
    // The real chat this simulation continues, faded; consecutive messages of one side share a row.
    if (context.length) {
      const real = A.el("div", "fa-sim-real");
      let group = null;
      for (const message of context) {
        const side = message?.side === "me" ? "me" : "friend";
        if (group?.side !== side) { group = { side, ...row(side, []) }; real.append(group.node); }
        group.bubbles.append(A.el("p", "fa-sim-bubble", String(message?.text ?? "")));
      }
      nodes.push(real);
      const divider = A.el("div", "fa-sim-divider");
      divider.append(A.el("span", "", "以下为模拟"));
      nodes.push(divider);
    }
    const lastFriend = turns.reduce((found, turn, index) => turn.side === "friend" ? index : found, -1);
    turns.forEach((turn, index) => {
      if (turn.side === "friend" && turn.noReply) nodes.push(A.el("p", "fa-sim-system", "对方没有回复"));
      else {
        const texts = strings(turn.messages);
        if (texts.length) nodes.push(row(turn.side === "me" ? "me" : "friend", texts).node);
      }
      if (index === lastFriend && index === turns.length - 1 && !busy()) {
        const again = A.el("div", "fa-sim-again");
        again.append(A.button("换一个", "fa-sim-link", () => { void reroll(); }));
        nodes.push(again);
      }
    });
    if (busy()) nodes.push(row("friend", [A.el("p", "fa-sim-bubble typing", "正在输入…")]).node);
    for (const text of [S.error, S.loadError]) if (text) nodes.push(A.el("p", "fa-sim-system error", text));
    if (!nodes.length) nodes.push(A.el("p", "fa-sim-empty", "发条消息，看看 ta 会怎么回。"));
    ui.log.replaceChildren(...nodes);
    ui.log.scrollTop = ui.log.scrollHeight;

    ui.send.textContent = busy() ? "停止" : "发送";
    ui.send.disabled = S.pending === "";
    ui.reset.hidden = !turns.length && !busy() && !S.error;
  }
  const sameTexts = (left, right) => Array.isArray(left) && left.length === right.length && left.every((item, index) => item === right[index]);

  // -- actions --------------------------------------------------------------------------------------------
  async function send() {
    if (busy() || A.masked) return;
    // 换行分条：每一行是一条消息，整段是“我”的这一轮。
    const texts = ui.input.value.split("\n").map(item => item.trim()).filter(Boolean);
    if (!texts.length) { ui.input.focus?.(); return; }
    if (texts.length > MAX_MESSAGES) { ui.status.textContent = `一轮最多 ${MAX_MESSAGES} 条，请合并几行。`; return; }
    ui.status.textContent = "";
    S.error = "";
    S.sent = texts;
    ui.input.value = "";
    await start("send", () => A.rpc("page.simulationSend", { messages: texts }), texts);
  }
  async function reroll() {
    if (busy() || A.masked) return;
    S.error = "";
    await start("reroll", () => A.rpc("page.simulationReroll", {}));
  }
  async function start(kind, call, texts = null) {
    S.kind = kind;
    S.pending = "";
    const serial = ++S.serial;
    render();
    try {
      const result = await call();
      if (serial !== S.serial) return;
      if (typeof result?.requestId !== "string" || !result.requestId) throw new Error("模拟回复没有开始，请重试。");
      await poll(result.requestId, serial);
    } catch (error) {
      if (serial !== S.serial || A.masked) return;
      Object.assign(S, { pending: null, kind: null, error: friendly(error), sent: null });
      if (texts && !ui.input.value) ui.input.value = texts.join("\n");
      render();
    }
  }
  async function poll(requestId, serial = ++S.serial) {
    S.pending = requestId;
    cancelTimer();
    render();
    const live = () => serial === S.serial && !A.masked;
    let status = null;
    try {
      for (;;) {
        await new Promise(resolve => { S.timer = setTimeout(() => { S.timer = null; resolve(); }, 1000); });
        if (!live()) return;
        status = await A.rpc("page.simulationStatus", { requestId });
        if (!live()) return;
        if (!["queued", "running"].includes(status?.state)) break;
      }
    } catch (error) {
      if (!live()) return;
      status = { state: "failed", error: friendly(error) };
    }
    const failed = status?.state === "failed" ? String(status.error || "").slice(0, 300) || "模拟回复未完成，请重试。" : "";
    const sent = S.sent;
    Object.assign(S, { pending: null, kind: null, error: failed });
    // The session is the backend's: read it back so a done, failed or cancelled request shows as stored.
    await load();
    // A turn that did not stay in the session goes back into an empty input.
    const last = S.data?.turns?.at?.(-1);
    if (sent && status?.state !== "done" && !ui.input.value && !(last?.side === "me" && sameTexts(last.messages, sent)))
      ui.input.value = sent.join("\n");
  }
  async function cancel() {
    const requestId = S.pending;
    if (!requestId) return;
    try { await A.rpc("page.simulationCancel", { requestId }); }
    catch (error) { if (!A.masked) A.toast(friendly(error)); }
  }
  async function reset() {
    if (A.masked) return;
    S.serial++;
    cancelTimer();
    Object.assign(S, { pending: null, kind: null, error: "", sent: null });
    try { await A.rpc("page.simulationReset", {}); }
    catch (error) { if (!A.masked) A.toast(friendly(error)); }
    await load();
  }

  window.addEventListener?.("pagehide", cancelTimer);
  window.FriendSimulation = { load };
})();
