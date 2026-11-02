"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// The window's run controls for the current analysis scheme: one line, one button and details.
// Modified 2026-10-13 (Apache-2.0): plugin architecture. Start, stop and continue are Core scheme
// tasks; the scheme plugin words the line and details (scheme.status / scheme.estimate).
(function () {
  const A = window.FriendApp;
  const OPEN = ["queued", "running", "stopping"];
  const R = { scheme: null, status: null, task: null, timer: null, busy: false, steps: new Map(), serial: 0 };

  const rpc = (method, params = {}) => A.api("/api/plugins/rpc", { method: "POST",
    body: { plugin: R.scheme.plugin, method, params } }).then(value => value.result);
  const taskActive = () => OPEN.includes(R.task?.state);

  async function refresh() {
    clearTimeout(R.timer);
    if (A.masked || !R.scheme) return;
    const serial = ++R.serial, scheme = R.scheme;
    try {
      const [status, tasks] = await Promise.all([rpc("scheme.status"),
        A.api("/api/tasks?" + A.query({ plugin: scheme.plugin }))]);
      if (serial !== R.serial) return;
      R.status = status;
      R.task = (tasks.tasks || []).find(task => task.kind === "scheme" && task.scheme === scheme.scheme) || null;
      announce();
      render();
    } catch (error) {
      // Keep a way out: the line refreshes by itself, and 重试 asks again at once (e.g. a recovered model gateway).
      if (serial === R.serial && !A.masked) {
        const action = A.byId("faRunButton");
        A.byId("faRunInfo").textContent = error.message || "分析方案暂时不可用";
        Object.assign(action, { hidden: false, disabled: false, className: "fa-secondary", textContent: "重试",
          onclick: () => { void refresh(); } });
      }
    }
    if (!A.masked && serial === R.serial)
      R.timer = setTimeout(() => { void refresh(); }, taskActive() || R.status?.active ? 1200 : 8000);
  }
  // Scheme pages reload only when the step that feeds them finishes.
  function announce() {
    const run = R.status?.run;
    if (!run) return;
    for (const step of run.steps || []) {
      const key = run.id + ":" + step.key, previous = R.steps.get(key);
      if (previous && previous !== step.state && ["done", "skipped", "failed"].includes(step.state))
        A.emit({ type: "analysis-step", key: step.key });
      R.steps.set(key, step.state);
    }
    const finished = run.id + ":run", previous = R.steps.get(finished);
    if (previous && ["queued", "running"].includes(previous) && !["queued", "running"].includes(run.state))
      A.emit({ type: "analysis-finished", state: run.state });
    R.steps.set(finished, run.state);
  }

  function render() {
    const info = A.byId("faRunInfo"), action = A.byId("faRunButton"), status = R.status || {};
    action.hidden = false;
    action.disabled = R.busy;
    action.className = "fa-primary";
    let label, handler;
    if (taskActive()) {
      label = R.task.state === "stopping" ? "正在停止" : "取消";
      action.className = "fa-secondary";
      action.disabled = R.busy || R.task.state === "stopping";
      handler = () => act("/api/tasks/stop", { id: R.task.id }, "取消失败");
    } else if (status.action?.continue && R.task && ["stopped", "interrupted", "failed", "partial"].includes(R.task.state)) {
      label = status.action.label;
      handler = () => resume(R.task);
    } else if (status.action) {
      label = status.action.label;
      handler = () => start(status.action.mode || "update");
    } else {
      label = "开始分析";
      handler = () => start("update");
    }
    if (status.disabled && !taskActive()) action.disabled = true;
    info.textContent = taskActive() && !status.active ? (R.task.progress?.label || "排队中") : (status.line || "");
    info.title = "查看分析详情";
    action.textContent = label;
    action.onclick = handler;
    if (A.sheetOwner === "run") showDetails(true);
  }

  async function act(path, body, failure) {
    R.busy = true;
    render();
    try { return await A.api(path, { method: "POST", body }); }
    catch (error) {
      if (error.status === 409 && error.code === "scheme-busy") return busy(error, path, body);
      A.toast(error.message || failure);
    } finally { R.busy = false; await refresh(); }
    return null;
  }
  const start = mode => act("/api/tasks", { plugin: R.scheme.plugin, scheme: R.scheme.scheme,
    title: R.scheme.name, params: { mode } }, "开始失败");
  const resume = task => act("/api/tasks/continue", { id: task.id }, "继续失败");

  // Another scheme of this friend is still running: stop it, or wait behind it.
  function busy(error, path, body) {
    const other = error.detail || {};
    const name = A.schemeName?.(other.plugin, other.scheme) || other.title || "另一个方案";
    const sheet = A.openSheet("这位联系人有方案正在分析", "scheme-busy");
    sheet.append(A.el("p", "", `「${name}」还在分析这位联系人。可以停止它后开始，或排在它后面自动开始。`));
    const actions = A.el("div", "fa-form-actions");
    const choose = conflict => { A.closeSheet(); void act(path, { ...body, conflict }, "开始失败"); };
    actions.append(A.button(`停止「${name}」并开始`, "fa-primary", () => choose("stop-other")),
      A.button("排在它后面", "fa-secondary", () => choose("queue")),
      A.button("取消", "fa-plain", () => A.closeSheet()));
    sheet.append(actions);
    return null;
  }

  function section(item) {
    if (item.kind === "note") return A.el("p", "fa-note", item.text);
    if (item.kind === "pairs") {
      const list = A.el("dl", "fa-kv");
      for (const [label, value] of item.rows || []) list.append(A.el("dt", "", label), A.el("dd", "", value));
      if (!item.title) return list;
      const details = A.el("details", "fa-details");
      details.open = !item.collapsed;
      details.append(A.el("summary", "", item.title), list);
      if (item.note) details.append(A.el("p", "fa-note", item.note));
      return details;
    }
    const box = A.el("div");
    if (item.title) box.append(A.el("div", "fa-group", item.title));
    const list = A.el("div", "fa-list");
    for (const step of item.items || []) {
      const row = A.el("div", "fa-item"), main = A.el("div", "fa-item-main"), body = A.el("div", "fa-item-text");
      const head = A.el("div", "", step.title);
      head.append(A.el("span", step.warn ? "fa-mark warn" : "fa-mark", " · " + step.state));
      body.append(head);
      if (step.detail) body.append(A.el("div", "fa-note", step.detail));
      if (step.error) body.append(A.el("div", "fa-note fa-danger-text", step.error));
      if (step.meta?.length) {
        const meta = A.el("div", "fa-item-meta");
        for (const text of step.meta) meta.append(A.el("span", "", text));
        body.append(meta);
      }
      main.append(body);
      row.append(main);
      list.append(row);
    }
    box.append(list);
    return box;
  }
  function showDetails(update = false) {
    const details = R.status?.details;
    const body = update ? A.byId("faSheetBody") : A.openSheet(details?.title || "分析详情", "run");
    body.replaceChildren();
    for (const item of details?.sections || []) body.append(section(item));
    if (R.task) body.append(A.el("p", "fa-note", `任务状态：${{ queued: "排队中", running: "进行中", stopping: "正在停止",
      done: "已完成", partial: "部分完成", failed: "未完成", stopped: "已停止", interrupted: "已中断" }[R.task.state] || R.task.state}`));
  }

  async function confirmRebuild() {
    const body = A.openSheet("重新分析全部", "rebuild");
    body.append(A.el("p", "", "按当前模型和分析规则重做全部可读历史。重做完成前各页继续显示原有结果；完成后才替换。"));
    try {
      const estimate = await rpc("scheme.estimate", { mode: "rebuild" });
      if (A.sheetOwner !== "rebuild") return;
      if (estimate?.line) body.append(A.el("p", "fa-note", estimate.line));
    } catch { }
    const actions = A.el("div", "fa-form-actions");
    actions.append(A.button("开始重新分析", "fa-primary", () => { A.closeSheet(); void start("rebuild"); }),
      A.button("取消", "fa-plain", () => A.closeSheet()));
    body.append(actions);
  }

  A.byId("faRunInfo").addEventListener("click", () => showDetails());
  window.FriendRun = {
    refresh, confirmRebuild,
    setScheme(scheme) {
      R.serial++;
      R.scheme = scheme; R.status = null; R.task = null; R.steps = new Map();
      clearTimeout(R.timer);
      A.byId("faRunInfo").textContent = "";
      A.byId("faRunButton").hidden = !scheme;
      void refresh();
    },
    setOverview() { },
    stop() { clearTimeout(R.timer); },
    invalidate() { clearTimeout(R.timer); R.timer = setTimeout(() => { void refresh(); }, 600); },
    get active() { return taskActive(); },
  };
})();
