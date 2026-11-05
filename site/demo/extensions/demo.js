// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

// The shipped plugin renderers talk exclusively to this closed, in-memory adapter.
// There is deliberately no fallback to the browser's original fetch.
(() => {
  const plugins = structuredClone(window.ZhijiDemoPlugins);
  const settings = new Map(plugins.map(plugin => [plugin.id, {
    revision: 1,
    values: Object.fromEntries((plugin.contributes.settings?.groups || [])
      .flatMap(group => group.fields).map(field => [field.key, field.default])),
  }]));
  const copy = value => structuredClone(value);
  const response = (value, status = 200) => ({ok: status === 200, status, json: async () => copy(value)});
  window.fetch = async (path, options = {}) => {
    if (typeof path !== 'string' || !path.startsWith('/api/plugins')) return response({message: '此操作不在演示范围内'}, 404);
    const body = options.body ? JSON.parse(options.body) : null;
    if (path === '/api/plugins' || path === '/api/plugins/refresh') return response({plugins, problems: []});
    if (path === '/api/plugins/enable' && body && typeof body.enabled === 'boolean') {
      const plugin = plugins.find(item => item.id === body.id);
      if (!plugin) return response({message: '未找到演示插件'}, 404);
      plugin.enabled = body.enabled;
      return response({plugins, problems: []});
    }
    if (path === '/api/plugins/settings' || path.startsWith('/api/plugins/settings?id=')) {
      const id = body?.id || new URLSearchParams(path.split('?')[1]).get('id');
      const plugin = plugins.find(item => item.id === id);
      if (!plugin) return response({message: '未找到演示插件'}, 404);
      const state = settings.get(id);
      if (body) {
        if (body.revision !== state.revision) return response({message: '演示设置已更新，请重新读取'}, 409);
        state.values = copy(body.values);
        state.revision += 1;
      }
      return response({plugin, enabled: plugin.enabled, schema: plugin.contributes.settings || null,
        definition: 'public-v2.1.0-demo', revision: state.revision, values: state.values});
    }
    return response({message: '此操作不在演示范围内'}, 404);
  };

  const byId = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('[data-tab]')];
  function selectTab(key, focus = false) {
    for (const tab of tabs) {
      const active = tab.dataset.tab === key;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      const panel = byId(tab.getAttribute('aria-controls'));
      panel.classList.toggle('active', active); panel.hidden = !active;
      if (active && focus) tab.focus();
    }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => window.PluginSettings.requestLeave(() => selectTab(tab.dataset.tab)));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') next = (index + 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      window.PluginSettings.requestLeave(() => selectTab(tabs[next].dataset.tab, true));
    });
  });

  const models = [
    {id: 'demo-chat', protocol: 'anthropic', concurrency: 4, context: 32768, timeout: 60},
    {id: 'demo-quick', protocol: 'chat_completions', concurrency: 2, context: 16384, timeout: 10},
    {id: 'demo-vision', protocol: 'responses', concurrency: 2, context: 32768, timeout: 30},
  ];
  const roles = {selectRoleMain: 'demo-chat', selectRoleJudge: 'demo-quick', selectRoleImage: 'demo-vision'};
  const fieldIds = ['selectApiProtocol', 'inputApiModelId', 'inputApiConcurrency', 'inputApiContextTokens', 'inputApiTimeoutMinutes'];
  let editing = -1;
  function renderModels() {
    for (const [id, selected] of Object.entries(roles)) {
      const select = byId(id);
      select.replaceChildren(...models.map(model => new Option(model.id, model.id)));
      select.value = selected;
    }
    byId('modelListRows').replaceChildren(...models.map((model, index) => {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'model-list-row';
      const label = document.createElement('span'), action = document.createElement('span');
      label.textContent = model.id; action.textContent = '编辑 ›'; row.append(label, action);
      row.addEventListener('click', () => openModel(index)); return row;
    }));
  }
  function openModel(index) {
    editing = index;
    const model = models[index] || {id: 'demo-new', protocol: 'anthropic', concurrency: 4, context: 32768, timeout: 60};
    const values = [model.protocol, model.id, model.concurrency, model.context, model.timeout];
    fieldIds.forEach((id, i) => { byId(id).value = values[i]; });
    byId('modelDetailTitle').textContent = index < 0 ? '添加模型' : '编辑模型';
    byId('modelHomeView').hidden = true; byId('modelDetailView').hidden = false;
    byId('apiModelTestStatus').textContent = '';
    byId('apiModelCount').textContent = '';
    byId('selectApiModel').replaceChildren(new Option('获取列表后选择', ''));
    byId('selectApiModel').disabled = true;
    byId('btnBackModelHome').focus();
  }
  function modelHome() {
    byId('modelDetailView').hidden = true; byId('modelHomeView').hidden = false;
    renderModels();
  }
  Object.keys(roles).forEach(id => byId(id).addEventListener('change', () => {
    roles[id] = byId(id).value;
    byId('modelHomeStatus').textContent = '演示角色已更新 · 刷新还原';
  }));
  byId('btnAddModel').addEventListener('click', () => openModel(-1));
  byId('btnBackModelHome').addEventListener('click', modelHome);
  byId('btnFetchApiModels').addEventListener('click', () => {
    byId('selectApiModel').replaceChildren(...['demo-chat', 'demo-quick', 'demo-vision'].map(id => new Option(id, id)));
    byId('selectApiModel').disabled = false;
    byId('apiModelCount').textContent = '3 个预设演示模型';
  });
  byId('selectApiModel').addEventListener('change', () => { byId('inputApiModelId').value = byId('selectApiModel').value; });
  byId('btnTestApiModel').addEventListener('click', () => { byId('apiModelTestStatus').textContent = '演示结果：连接成功 · 未请求模型服务'; });
  function saveModel(event) {
    event.preventDefault();
    if (!byId('modelDetailView').reportValidity()) return;
    const values = fieldIds.map(id => byId(id).value);
    const model = {protocol: values[0], id: values[1].trim(), concurrency: Number(values[2]), context: Number(values[3]), timeout: Number(values[4])};
    if (!model.id || models.some((item, index) => index !== editing && item.id === model.id)) {
      byId('apiModelTestStatus').textContent = '请填写不重复的演示模型 ID'; return;
    }
    if (editing >= 0) {
      const previous = models[editing].id;
      for (const id of Object.keys(roles)) if (roles[id] === previous) roles[id] = model.id;
      models[editing] = model;
    } else models.push(model);
    modelHome(); byId('modelHomeStatus').textContent = '演示设置已保存 · 刷新还原';
    byId('btnAddModel').focus();
  }
  byId('btnSaveApiModel').addEventListener('click', saveModel);
  byId('modelDetailView').addEventListener('submit', event => event.preventDefault());
  byId('modelDetailView').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing && event.target.tagName === 'INPUT') saveModel(event);
  });
  byId('demoSearch').addEventListener('change', () => {
    byId('searchStatus').textContent = byId('demoSearch').value === 'searxng'
      ? '演示已启用搜索；实际使用需在客户端配置搜索服务。' : '演示搜索已关闭。';
  });

  const related = byId('relatedDemo');
  const relatedRows = {
    repository: ['日常交流助手：结合聊天来龙去脉准备回复', '相处复盘：看看共同经历和交流节奏', '轻松破冰：从对方近况找到共同话题'],
    'mcp-admin': ['Claude Code · 演示连接 · 按账号授权', '数据范围：演示账号 / 林沐、苏禾', '操作权限：读取资料、查看统计；分析需单独授权'],
  };
  function showRelated(pluginId, nav) {
    const plugin = plugins.find(item => item.id === pluginId);
    const title = plugin?.contributes.nav?.find(item => item.id === nav)?.label || plugin?.name || '相关管理';
    byId('relatedTitle').textContent = title;
    const rows = relatedRows[nav] || ['演示资料只保留在当前页面。', '在客户端中可导入、核对和管理自己的资料。'];
    byId('relatedBody').replaceChildren(...rows.map(text => {
      const row = document.createElement('p'); row.className = 'demo-related-card'; row.textContent = text; return row;
    }));
    related.showModal();
  }
  byId('relatedClose').addEventListener('click', () => related.close());
  window.PluginSettings.init({onModelSettings: () => selectTab('models'), onOpenNav: showRelated});
  renderModels();
})();
