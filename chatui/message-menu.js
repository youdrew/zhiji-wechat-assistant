/* Know Your Friends, 2026-10-15. Apache-2.0. Right-click menu of a chat message: copy it, or ask the chat panel about it. */
(function (host) {
  "use strict";

  const attached = new WeakSet();
  let config = null, menu = null, copyButton = null, askButton = null, target = null, initialized = false;

  // The selected part of this message's bubble, or the whole message.
  function quotedText(item, message) {
    const selection = host.getSelection?.();
    const selected = selection && !selection.isCollapsed ? String(selection.toString() || "").trim() : "";
    const bubble = item.querySelector(".msg-bubble");
    if (selected && bubble?.contains(selection.anchorNode) && bubble.contains(selection.focusNode)) return selected;
    return typeof message.text === "string" ? message.text : "";
  }
  function askLabel() {
    const label = host.PluginSlots?.state?.toolbar?.[0]?.label;
    return `问${typeof label === "string" && label ? label : "聊天助手"}`;
  }
  const canAsk = () => host.PluginSlots?.canContinue?.() === true && typeof host.desktopHost?.openPluginWindow === "function";
  function current(candidate = target) {
    const state = config?.state;
    return !!candidate && !!state && candidate.account === state.currentAccount && candidate.user === state.currentUser &&
      candidate.generation === state.generation && candidate.item.isConnected;
  }

  function close() {
    if (!menu) return;
    menu.hidden = true;
    target = null;
  }
  function open(item, message, x, y) {
    const state = config.state, text = quotedText(item, message);
    target = { item, message, text, account: state.currentAccount, user: state.currentUser, generation: state.generation };
    copyButton.hidden = !text.trim();
    askButton.hidden = !canAsk();
    askButton.textContent = askLabel();
    if (copyButton.hidden && askButton.hidden) { target = null; return false; }
    menu.hidden = false;
    const zoom = Number(host.getComputedStyle?.(document.documentElement)?.zoom) || 1;
    const bounds = menu.getBoundingClientRect(), margin = 8;
    const width = host.innerWidth, height = host.innerHeight;
    menu.style.left = `${Math.max(margin, Math.min(x, width - bounds.width - margin)) / zoom}px`;
    menu.style.top = `${Math.max(margin, Math.min(y, height - bounds.height - margin)) / zoom}px`;
    (copyButton.hidden ? askButton : copyButton).focus();
    return true;
  }

  function attach(item, message) {
    if (attached.has(item) || !menu) return;
    attached.add(item);
    item.addEventListener("contextmenu", event => {
      if (event.target?.closest?.("a, button, input, textarea, video, audio")) return;
      if (open(item, message, event.clientX, event.clientY)) { event.preventDefault(); event.stopPropagation(); }
    });
  }

  async function copy() {
    if (!current()) { close(); return; }
    const value = target.text;
    close();
    let copied = false;
    if (typeof host.desktopHost?.copyDraft === "function") {
      try { copied = await host.desktopHost.copyDraft(value) === true; } catch { }
    }
    if (!copied && typeof host.navigator?.clipboard?.writeText === "function") {
      try { await host.navigator.clipboard.writeText(value); copied = true; } catch { }
    }
    config.toast(copied ? "已复制" : "复制失败，请重试");
  }
  // The panel opens with a draft quoting this message; nothing is sent until the user asks.
  async function ask() {
    if (!current() || !canAsk()) { close(); return; }
    const { message, text, account, user } = target;
    close();
    const ok = await host.PluginSlots.continueFromMessage({ account, user, messageId: String(message.id),
      text, side: message.side === "self" ? "self" : "other", sender: message.side === "self" ? "" : String(message.senderName || "") });
    if (!ok) config.toast("暂无法带入聊天助手");
  }

  function init(options) {
    config = options;
    if (initialized) return;
    menu = config.byId("messageContextMenu");
    copyButton = config.byId("btnCopyMessage");
    askButton = config.byId("btnAskAboutMessage");
    if (!menu || !copyButton || !askButton) return;
    initialized = true;
    menu.hidden = true;
    copyButton.addEventListener("click", () => { void copy(); });
    askButton.addEventListener("click", () => { void ask(); });
    menu.addEventListener("contextmenu", event => event.preventDefault());
    menu.addEventListener("keydown", event => {
      if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const items = [copyButton, askButton].filter(button => !button.hidden);
      const index = items.indexOf(document.activeElement);
      items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
    });
    const outside = event => { if (target && !menu.contains(event.target)) close(); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", event => { if (target && ["Escape", "Tab"].includes(event.key)) close(); });
    config.byId("chatMessages")?.addEventListener("scroll", close);
    host.addEventListener("resize", close);
    host.addEventListener("blur", close);
  }

  host.MessageMenu = { init, attach, close };
})(window);
