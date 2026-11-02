"use strict";
// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// 助手仓库 nav page of kyf.advisor (added 2026-10-13 from the repository half of chatui/advisor.js): assistant
// templates and skills, created by hand or imported from GitHub or a host-picked local package. It runs in a
// sandboxed frame of the main window and reaches the plugin only through kyfHost; modals stay in this page.
(function () {
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value, limit) => typeof value === "string" && value.length <= limit;
  const id = value => text(value, 240) && value.length > 0;
  function nonce(prefix) {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return prefix + Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }
  function validCatalog(value) {
    return object(value) && Array.isArray(value.agents) && Array.isArray(value.skills) &&
      value.agents.every(agent => object(agent) && id(agent.id) && text(agent.name, 60) &&
        text(agent.description, 240) && text(agent.prompt, 16000) && typeof agent.enabled === "boolean" &&
        (agent.welcome === undefined || text(agent.welcome, 120)) &&
        Array.isArray(agent.skillIds) && agent.skillIds.every(id)) &&
      value.skills.every(skill => object(skill) && id(skill.id) && text(skill.name, 60)) &&
      ["available", "missing"].includes(value.engine?.state);
  }
  function errorText(value) {
    if (text(value?.message, 20000) && value.message) return value.message;
    if (text(value?.error, 20000) && value.error) return value.error;
    return "服务返回无效数据";
  }
  const Repository = {
    catalog: { agents: [], skills: [], engine: { state: "missing" } },
    catalogReady: false, catalogError: "", catalogBusy: false, catalogSerial: 0,
    // Plugin RPC through the host; errors become {error, code, status}.
    async call(method, params = {}) {
      try {
        const result = await window.kyfHost.rpc(method, params);
        return object(result) ? result : {};
      } catch (error) {
        return { error: error?.message || "请求失败", code: error?.code || "error", status: error?.status || 0 };
      }
    },
    async loadCatalog() {
      const serial = ++this.catalogSerial; this.catalogBusy = true; this.render();
      const data = await this.call("catalog.get");
      if (serial !== this.catalogSerial) return false;
      this.catalogBusy = false;
      if (!validCatalog(data)) { this.catalogError = errorText(data); this.catalogReady = false; this.render(); return false; }
      this.catalog = data; this.catalogReady = true; this.catalogError = ""; this.render(); return true;
    },
    async mutate(method, params, onError) {
      const serial = ++this.catalogSerial;
      const data = await this.call(method, params);
      if (serial !== this.catalogSerial) return false;
      this.catalogBusy = false;
      if (!validCatalog(data)) {
        const error = errorText(data);
        if (typeof onError === "function") onError(error);
        else { this.catalogError = error; this.render(); }
        return false;
      }
      this.catalog = data; this.catalogReady = true; this.catalogError = ""; this.render(); return true;
    },
    saveTemplate(agent, onError) {
      if (!this.catalogReady) return Promise.resolve(false);
      return this.mutate("catalog.saveAgent", { agent: {
        ...(agent.id ? { id: agent.id } : {}), name: String(agent.name || "").trim().slice(0, 60),
        description: String(agent.description || "").trim().slice(0, 240), prompt: String(agent.prompt || "").trim().slice(0, 16000),
        welcome: String(agent.welcome || "").trim().slice(0, 120),
        skillIds: (agent.skillIds || []).filter(value => this.catalog.skills.some(skill => skill.id === value)) } }, onError);
    },
    enableTemplate(agentId, enabled) { return this.mutate("catalog.enableAgent", { id: agentId, enabled: !!enabled }); },
    deleteTemplate(agentId) {
      return this.catalog.agents.some(item => item.id === agentId) ? this.mutate("catalog.deleteAgent", { id: agentId }) : Promise.resolve(false);
    },
    importSkill({ name, description, content }, onError) {
      if (!text(content, 24000) || /https?:\/\/|(?:file:\/\/)|(?:^|[^\w])[a-zA-Z]:[\\/]|<\s*script|javascript:/i.test(content)) {
        onError?.("技能内容不符合要求"); return Promise.resolve(false);
      }
      return this.mutate("catalog.importSkill", { name: String(name || "").trim().slice(0, 60),
        description: String(description || "").trim().slice(0, 240), content }, onError);
    },
    deleteSkill(skillId, onError) { return this.mutate("catalog.deleteSkill", { id: skillId }, onError); },
    render() {
      const container = document.getElementById("advisorRepoContainer"); if (!container) return; container.replaceChildren();
      const header = element("div", "advisor-repo-header"), titleWrap = element("div", "advisor-repo-title-wrap");
      titleWrap.appendChild(element("h2", "advisor-repo-title", "助手仓库"));
      header.appendChild(titleWrap); const actions = element("div", "advisor-repo-actions");
      actions.appendChild(button("新建助手", () => this.showImportAssistantModal(), "advisor-btn advisor-btn-primary", !this.catalogReady));
      actions.appendChild(button("刷新", () => this.loadCatalog(), "advisor-btn", this.catalogBusy));
      header.appendChild(actions); container.appendChild(header);
      if (this.catalogError) container.appendChild(element("div", "advisor-repo-error", this.catalogError));
      const grid = element("div", "advisor-card-grid");
      if (this.catalogReady) for (const agent of this.catalog.agents) {
        const card = element("div", `advisor-agent-card${agent.enabled ? " enabled" : ""}`), top = element("div", "advisor-card-top");
        const identity = element("div", "advisor-card-identity"); identity.appendChild(element("h3", "advisor-card-name", agent.name));
        top.appendChild(identity);
        const toggle = button("", () => this.enableTemplate(agent.id, !agent.enabled), `advisor-switch${agent.enabled ? " checked" : ""}`);
        toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-checked", String(agent.enabled));
        toggle.setAttribute("aria-label", `${agent.enabled ? "停用" : "启用"}${agent.name}`); top.appendChild(toggle); card.appendChild(top);
        if (agent.description) card.appendChild(element("p", "advisor-card-desc", agent.description));
        const tags = element("div", "advisor-card-skills");
        for (const skillId of agent.skillIds) tags.appendChild(element("span", "advisor-skill-tag", this.catalog.skills.find(item => item.id === skillId)?.name || skillId));
        card.appendChild(tags); const bottom = element("div", "advisor-card-bottom");
        bottom.appendChild(button("编辑", () => this.showEditAgentModal(agent)));
        bottom.appendChild(button("删除", () => this.confirmDelete(agent)));
        card.appendChild(bottom); grid.appendChild(card);
      }
      container.appendChild(grid);
      if (this.catalogReady && this.catalog.agents.length === 0)
        container.appendChild(element("div", "advisor-repo-empty", "暂无助手"));
    },
    // Sandboxed pages have no confirm() dialog; the question is a small modal of this page.
    confirmDelete(agent) {
      const modal = makeModal(`删除「${agent.name}」？`);
      finishModal(modal, "删除", () => this.deleteTemplate(agent.id));
      return modal;
    },
    showEditAgentModal(agent) {
      if (!agent) { this.showImportAssistantModal(); return; }
      const modal = makeModal("编辑助手");
      const name = field(modal.card, "名称", agent.name || "", 60), description = field(modal.card, "描述", agent.description || "", 240);
      const welcome = field(modal.card, "欢迎语", agent.welcome || "", 120);
      const prompt = field(modal.card, "提示词", agent.prompt || "", 16000, true);
      const group = element("div", "advisor-form-group"), skillHeader = element("div", "advisor-skill-header");
      skillHeader.appendChild(element("label", "advisor-form-label", "技能"));
      const selected = new Set(agent.skillIds || []), list = element("div", "advisor-skill-list");
      list.tabIndex = 0; list.setAttribute("aria-label", "技能");
      let deleting = false;
      const toggleDelete = button("移除技能", () => {
        deleting = !deleting; toggleDelete.classList.toggle("active", deleting);
        toggleDelete.setAttribute("aria-pressed", String(deleting)); renderSkills();
      }, "advisor-skill-delete-toggle");
      toggleDelete.setAttribute("aria-pressed", "false"); skillHeader.appendChild(toggleDelete);
      const renderSkills = () => {
        list.replaceChildren();
        for (const [index, skill] of this.catalog.skills.entries()) {
          const row = element("div", "advisor-skill-row");
          row.style.gridColumn = String(Math.floor(index % 10 / 5) + 1);
          row.style.gridRow = String(Math.floor(index / 10) * 5 + index % 5 + 1);
          const label = element("label", "advisor-skill-choice"), checkbox = document.createElement("input");
          checkbox.type = "checkbox"; checkbox.value = skill.id; checkbox.checked = selected.has(skill.id);
          checkbox.addEventListener("change", () => checkbox.checked ? selected.add(skill.id) : selected.delete(skill.id));
          label.title = skill.name;
          label.appendChild(checkbox); label.appendChild(element("span", "advisor-skill-name", skill.name)); row.appendChild(label);
          if (deleting) {
            const remove = button("×", () => { selected.delete(skill.id); renderSkills(); }, "advisor-skill-remove");
            remove.setAttribute("aria-label", "从当前助手移除" + skill.name); remove.title = "从当前助手移除" + skill.name;
            remove.hidden = !selected.has(skill.id); row.appendChild(remove);
          }
          list.appendChild(row);
        }
      };
      renderSkills(); group.appendChild(skillHeader); group.appendChild(list);
      modal.card.appendChild(group);
      finishModal(modal, "保存", async () => {
        if (!name.value.trim() || !prompt.value.trim()) { modal.reportError("请输入名称和提示词"); return false; }
        return this.saveTemplate({ id: agent.id, name: name.value, description: description.value, prompt: prompt.value, welcome: welcome.value,
          skillIds: [...selected].filter(skillId => this.catalog.skills.some(skill => skill.id === skillId)) }, modal.reportError);
      });
    },
    showImportSkillModal() {
      const modal = makeModal("导入技能"), name = field(modal.card, "名称", "", 60), description = field(modal.card, "描述", "", 240);
      const content = field(modal.card, "SKILL.md", "", 24000, true);
      finishModal(modal, "导入", () => this.importSkill({ name: name.value, description: description.value, content: content.value }, modal.reportError));
    },
    showImportAssistantModal() {
      const modal = makeModal("新建助手");
      modal.card.classList.add("advisor-import-modal");
      let preview = null, serial = 0, closed = false, busy = false, committing = false, requestId = nonce("req_");
      const cancelPreview = value => value && this.call("import.cancel", { previewId: value.id });
      const close = () => { if (committing) return; closed = true; serial++; void cancelPreview(preview); modal.overlay.remove(); };
      const url = field(modal.card, "GitHub 地址", "", 2048), sources = element("div", "advisor-import-sources");
      const summary = element("div", "advisor-import-summary"); summary.hidden = true;
      const name = field(modal.card, "名称", "", 60), description = field(modal.card, "描述", "", 240);
      const welcome = field(modal.card, "欢迎语", "", 120), prompt = field(modal.card, "提示词", "", 16000, true);
      const acknowledgement = element("label", "advisor-import-ack"), accept = document.createElement("input");
      accept.type = "checkbox"; acknowledgement.appendChild(accept); acknowledgement.appendChild(document.createTextNode("使用只读适配版本"));
      acknowledgement.hidden = true;
      const actions = element("div", "advisor-modal-actions"), cancel = button("取消", close);
      const create = button("导入并创建", async () => {
        if (busy || closed) return;
        if (!preview) await load({ kind: "github", url: url.value.trim() });
        if (closed || !preview || (preview.compatibility.state === "partial" && !accept.checked)) return;
        busy = true; committing = true; update(); modal.reportError("");
        const result = await this.call("import.commit", { previewId: preview.id, requestId, acceptPartial: accept.checked,
          agent: { name: name.value.trim(), description: description.value.trim(), prompt: prompt.value, welcome: welcome.value.trim() } });
        busy = false; committing = false;
        if (!closed && validCatalog(result) && id(result.importedAgentId)) {
          this.catalog = result; this.catalogReady = true; this.catalogError = ""; this.render(); close();
        } else if (!closed) { modal.reportError(errorText(result)); update(); }
      }, "advisor-btn advisor-btn-primary", true);
      const update = () => {
        create.disabled = busy || (!preview && !url.value.trim()) || (preview?.compatibility.state === "partial" && !accept.checked);
        cancel.disabled = committing; for (const item of sources.children) item.disabled = busy;
      };
      accept.addEventListener("change", update);
      url.addEventListener("input", () => {
        if (preview) { void cancelPreview(preview); preview = null; summary.hidden = true; acknowledgement.hidden = true; }
        update();
      });
      const load = async source => {
        if (closed || busy) return;
        const sequence = ++serial; busy = true; update(); modal.reportError("");
        void cancelPreview(preview); preview = null; requestId = nonce("req_");
        const result = await this.call("import.preview", { source });
        const value = result?.preview;
        if (closed || sequence !== serial) { void cancelPreview(value); return; }
        busy = false;
        if (!object(value) || !/^imp_[a-f0-9]{32}$/.test(value.id) || !text(value.name, 60) ||
          !text(value.description, 240) || !text(value.defaultPrompt, 16000) || !text(value.welcome, 120) ||
          !Number.isSafeInteger(value.resourceCount) || value.resourceCount < 0 || value.resourceCount > 128 ||
          !["readonly", "partial"].includes(value.compatibility?.state) || !Array.isArray(value.compatibility.warnings)) {
          modal.reportError(errorText(result)); summary.hidden = true; acknowledgement.hidden = true; update(); return;
        }
        preview = value; name.value = value.name; description.value = value.description;
        welcome.value = value.welcome; prompt.value = value.defaultPrompt; accept.checked = false;
        summary.replaceChildren();
        summary.appendChild(element("div", "advisor-import-state", value.compatibility.state === "readonly" ? "只读可用" : "只读适配"));
        summary.appendChild(element("div", "advisor-import-resource-count", `${value.resourceCount} 份参考资料`));
        for (const warning of value.compatibility.warnings) if (text(warning, 2000)) summary.appendChild(element("div", "advisor-import-warning", warning));
        summary.hidden = false; acknowledgement.hidden = value.compatibility.state !== "partial"; update();
      };
      sources.appendChild(button("读取", () => load({ kind: "github", url: url.value.trim() })));
      // The desktop host shows the picker and hands this page only a one-time grant, never the path.
      for (const [kind, label] of [["file", "文件 / ZIP"], ["directory", "文件夹"]]) sources.appendChild(button(label, async () => {
        if (busy) return;
        let selected;
        try { selected = await window.kyfHost.host("file.pick", { kind }); }
        catch (error) { modal.reportError(error?.code === "unknown-method" ? "请在桌面客户端选择文件" : error?.message || "文件选择失败，请重试"); return; }
        if (closed || !selected) return;
        if (!object(selected) || !/^fg_[a-f0-9]{32}$/.test(selected.token)) { modal.reportError(errorText(selected)); return; }
        void load({ kind: "local", grant: selected.token });
      }));
      modal.card.insertBefore(sources, name.parentNode); modal.card.insertBefore(summary, name.parentNode);
      modal.card.insertBefore(acknowledgement, name.parentNode);
      actions.appendChild(cancel); actions.appendChild(create); modal.card.appendChild(actions);
      return modal;
    },
    start() {
      const host = window.kyfHost;
      if (!host) return;
      host.on(message => {
        if (message.type === "event" && message.event?.type === "shown") void this.loadCatalog();
      });
      void host.ready().then(() => this.loadCatalog());
    },
  };
  function element(tag, className, content) {
    const result = document.createElement(tag); result.className = className; if (content !== undefined) result.textContent = content; return result;
  }
  function button(label, action, className = "advisor-btn", disabled = false) {
    const result = element("button", className, label); result.type = "button"; result.disabled = disabled; result.addEventListener("click", action); return result;
  }
  function makeModal(title) {
    const overlay = element("div", "advisor-modal-overlay"), card = element("div", "advisor-modal-card");
    card.appendChild(element("h3", "advisor-modal-title", title));
    const error = element("div", "advisor-modal-error"); error.setAttribute("role", "alert"); error.hidden = true;
    card.appendChild(error); overlay.appendChild(card); document.body.appendChild(overlay);
    return { overlay, card, reportError(value) { error.textContent = value; error.hidden = !value; } };
  }
  function field(card, label, value, maxLength, multiline = false) {
    const group = element("div", "advisor-form-group"); group.appendChild(element("label", "advisor-form-label", label));
    const input = element(multiline ? "textarea" : "input", multiline ? "advisor-textarea-form" : "advisor-input");
    input.value = value; input.maxLength = maxLength; if (multiline) input.rows = 6; group.appendChild(input); card.appendChild(group); return input;
  }
  function finishModal(modal, label, action) {
    const actions = element("div", "advisor-modal-actions"); actions.appendChild(button("取消", () => modal.overlay.remove()));
    const submit = button(label, async () => {
      submit.disabled = true; modal.reportError("");
      try { if (await action()) modal.overlay.remove(); }
      catch { modal.reportError("操作失败，请重试"); }
      finally { submit.disabled = false; }
    }, "advisor-btn advisor-btn-primary"); actions.appendChild(submit); modal.card.appendChild(actions);
  }
  window.AdvisorRepository = Repository;
  Repository.start();
})();
