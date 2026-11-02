"use strict";
// Know Your Friends, Apache-2.0: the desktop host owns scope and window lifetime.
(() => {
  let initialized = false, revision = -1, activeView = "stats";
  const tabs = [["stats", "modelCallsStatsTab"], ["records", "modelCallsRecordsTab"]];
  function setView(view) {
    if (!initialized || !tabs.some(([name]) => name === view)) return false;
    activeView = view;
    for (const [name, id] of tabs) {
      const tab = document.getElementById(id); tab.setAttribute("aria-selected", String(name === view)); tab.tabIndex = name === view ? 0 : -1;
    }
    document.getElementById("modelUsagePanel").setAttribute("aria-labelledby", tabs.find(([name]) => name === view)[1]);
    window.ModelUsage.open(view);
    return true;
  }
  function applyContext(context) {
    if (!context || typeof context !== "object") return;
    const next = Number.isSafeInteger(context.revision) ? context.revision : 0;
    // A context notification can arrive before the initial request resolves.
    if (next < revision) return;
    revision = next;
    document.body.classList.toggle("theme-light", context.theme === "light");
    document.getElementById("modelTracesStartup").hidden = true;
    if (!initialized) {
      initialized = true;
      const mount = document.getElementById("modelCallsContent");
      // Captured requests and responses open inside a call record's detail.
      window.ModelTraces.init({ ...context, standalone: true, onSettings: setting => window.ModelUsage.setDeveloperMode(setting) });
      window.ModelUsage.init({ account: context.account, traces: window.ModelTraces, mount });
      document.getElementById("modelUsagePanel").setAttribute("role", "tabpanel");
      document.getElementById("modelCallsNavigation").hidden = false; mount.hidden = false;
      setView(activeView);
    } else {
      window.ModelTraces.setScope(context.account, context.user, context.view);
      window.ModelUsage.setScope(context.account);
    }
  }
  async function readContext() {
    const retry = document.getElementById("modelTracesStartupRetry");
    retry.hidden = true;
    try {
      const context = await window.modelTracesWindow.getContext();
      if (!context || typeof context !== "object") throw new Error("context-unavailable");
      applyContext(context);
    } catch {
      if (initialized) return;
      document.getElementById("modelTracesStartupStatus").textContent = "暂时无法读取当前账号，请重新加载。";
      retry.hidden = false;
    }
  }
  const unsubscribe = window.modelTracesWindow?.onContext?.(applyContext);
  for (const [view, id] of tabs) {
    document.getElementById(id).addEventListener("click", () => setView(view));
    document.getElementById(id).addEventListener("keydown", event => {
      if (event.isComposing || event.keyCode === 229 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = tabs.findIndex(([name]) => name === view);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      setView(tabs[next][0]); document.getElementById(tabs[next][1]).focus();
    });
  }
  window.ModelCalls = { setView };
  window.addEventListener("beforeunload", () => unsubscribe?.());
  document.getElementById("modelTracesStartupRetry").addEventListener("click", readContext);
  void readContext();
})();
