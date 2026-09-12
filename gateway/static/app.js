/* ZCode Universal Model Gateway - admin UI
 * Plain JavaScript, no build step, no external dependencies.
 * All dynamic content is inserted through DOM nodes / textContent to avoid XSS.
 * NOTE: identifiers and comments are English for reuse; user-facing
 * strings are English as well.
 */
'use strict';

(function () {
  // ---------------------------------------------------------------- state
  const state = {
    token: sessionStorage.getItem('zumg_token') || '',
    providers: [],
    models: [],
    status: null,
    meta: null,
    controller: null,
    streaming: false,
  };

  // ---------------------------------------------------------------- utils
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'value') node.value = value;
        else if (key === 'checked') node.checked = true;
        else if (key === 'disabled') node.disabled = true;
        else if (key.startsWith('on') && typeof value === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (key === 'dataset') {
          for (const dk of Object.keys(value)) node.dataset[dk] = value[dk];
        } else node.setAttribute(key, value);
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function badge(text, kind) {
    return el('span', { class: 'badge ' + (kind || 'muted'), text: text });
  }

  function notify(message, kind) {
    const box = el('div', { class: 'toast ' + (kind || ''), text: message });
    $('#toast').append(box);
    setTimeout(() => box.remove(), 4200);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      notify('Copied', 'ok');
    } catch (err) {
      notify('Copy failed: ' + err.message, 'err');
    }
  }

  function fmtTime(value) {
    if (!value) return '—';
    const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
  }

  function extractError(data) {
    if (!data) return null;
    const detail = data.detail !== undefined ? data.detail : data;
    if (typeof detail === 'string') return detail;
    if (detail && detail.error && detail.error.message) return detail.error.message;
    if (detail && detail.message) return detail.message;
    try {
      return JSON.stringify(detail);
    } catch (e) {
      return String(detail);
    }
  }

  // ------------------------------------------------------------------ API
  function askToken() {
    const value = window.prompt(
      'This gateway has an admin token enabled. Enter GATEWAY_ADMIN_TOKEN:'
    );
    if (value) {
      state.token = value;
      sessionStorage.setItem('zumg_token', value);
    }
  }

  async function api(path, options) {
    options = options || {};
    const headers = { accept: 'application/json' };
    if (state.token) headers['x-admin-token'] = state.token;
    let payload;
    if (options.body !== undefined) {
      headers['content-type'] = 'application/json';
      payload = JSON.stringify(options.body);
    }
    const response = await fetch(path, { method: options.method || 'GET', headers: headers, body: payload });
    if (response.status === 401) {
      askToken();
      throw new Error('A valid admin token is required.');
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        data = { detail: text };
      }
    }
    if (!response.ok) throw new Error(extractError(data) || 'HTTP ' + response.status);
    return data;
  }

  // ------------------------------------------------------------- navigation
  const loaders = {
    dashboard: loadStatus,
    providers: loadProviders,
    models: loadModels,
    console: loadConsoleOptions,
    configuration: loadConfig,
    logs: loadLogs,
    about: loadAbout,
  };

  function showView(name) {
    document.querySelectorAll('#sidebar a').forEach((a) => {
      a.classList.toggle('active', a.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach((v) => {
      v.classList.toggle('active', v.id === 'view-' + name);
    });
    $('#sidebar').classList.remove('open');
    const loader = loaders[name];
    if (loader) loader().catch((err) => notify(err.message, 'err'));
  }

  function initNav() {
    document.querySelectorAll('#sidebar a').forEach((a) => {
      a.addEventListener('click', () => showView(a.dataset.view));
    });
    $('#nav-toggle').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  }

  // ------------------------------------------------------------- dashboard
  async function loadStatus() {
    const data = await api('/api/admin/status');
    state.status = data;
    renderDashboard(data);
  }

  function statCard(label, value, small) {
    return el('div', { class: 'card' }, [
      el('div', { class: 'label', text: label }),
      el('div', { class: 'value' + (small ? ' small' : ''), text: String(value) }),
    ]);
  }

  function renderDashboard(data) {
    const baseUrl = (state.meta && state.meta.default_base_url) || 'http://127.0.0.1:8787/v1';
    $('#zcode-base-url').value = baseUrl;
    const cards = $('#dash-cards');
    clear(cards);
    cards.append(
      statCard('Gateway Status', 'OK', true),
      statCard('Gateway Version', data.version, true),
      statCard('Listen Address', '127.0.0.1', true),
      statCard('Config Status', data.config_valid ? 'Valid' : 'Invalid', true),
      statCard('Providers', data.providers),
      statCard('Models', data.models),
      statCard('Virtual Models', data.virtual_models),
      statCard('Providers with Keys', data.providers_with_keys)
    );

    const statusBadge = $('#config-status');
    statusBadge.textContent = data.config_valid ? 'Config: OK' : 'Config: Invalid';
    statusBadge.className = 'badge ' + (data.config_valid ? 'ok' : 'err');

    renderRecent($('#dash-recent'), data.recent_requests, false);
    renderRecent($('#dash-errors'), data.recent_errors, true);
  }

  function renderRecent(container, items, showError) {
    clear(container);
    if (!items || !items.length) {
      container.append(el('p', { class: 'muted', text: 'No records yet.' }));
      return;
    }
    const table = el('table', null, [
      el('thead', null, el('tr', null, [
        el('th', { text: 'Time' }),
        el('th', { text: 'Model' }),
        el('th', { text: 'Tier' }),
        el('th', { text: showError ? 'Error' : 'Status' }),
        el('th', { text: 'Latency' }),
      ])),
      el('tbody', null, items.map((item) =>
        el('tr', null, [
          el('td', { text: fmtTime(item.time_iso || item.time) }),
          el('td', { class: 'mono', text: item.logical_model || '—' }),
          el('td', { text: item.reasoning_level || '—' }),
          el('td', null, showError
            ? el('span', { class: 'badge err', text: item.error_type || 'Error' })
            : badge(String(item.status || '—'), item.ok ? 'ok' : 'err')),
          el('td', { text: item.latency_ms != null ? item.latency_ms + ' ms' : '—' }),
        ])
      )),
    ]);
    container.append(el('div', { class: 'table-wrap' }, table));
  }

  // ------------------------------------------------------------- providers
  async function loadProviders() {
    const data = await api('/api/admin/providers');
    state.providers = data.providers || [];
    renderProviders();
  }

  function keyBadge(provider) {
    const ks = provider.key_status || {};
    if (ks.source === 'local') return badge('Saved locally', 'ok');
    if (ks.source === 'temporary') return badge('Temporary key', 'warn');
    if (ks.source === 'environment') return badge('Environment variable', 'ok');
    return badge('Missing', 'err');
  }

  function renderProviders() {
    const container = $('#providers-table');
    clear(container);
    if (!state.providers.length) {
      container.append(el('p', { class: 'muted', text: 'No providers configured.' }));
      return;
    }
    const rows = state.providers.map((p) =>
      el('tr', null, [
        el('td', null, [
          el('div', { text: p.display_name || p.id }),
          el('div', { class: 'mono muted', text: p.id }),
        ]),
        el('td', null, badge(p.protocol, 'accent')),
        el('td', { class: 'mono', text: p.base_url }),
        el('td', { class: 'mono', text: p.api_key_env || '—' }),
        el('td', null, keyBadge(p)),
        el('td', null, p.enabled ? badge('Enabled', 'ok') : badge('Disabled', 'muted')),
        el('td', null, el('div', { class: 'row-actions' }, [
          actionBtn('Edit', () => openProviderForm(p)),
          actionBtn('Test', () => testProvider(p)),
          actionBtn('Duplicate', () => duplicateProvider(p)),
          actionBtn(p.enabled ? 'Disable' : 'Enable', () => toggleProvider(p, !p.enabled)),
          actionBtn('Delete', () => deleteProvider(p), 'danger'),
        ])),
      ])
    );
    container.append(el('div', { class: 'table-wrap' }, el('table', null, [
      el('thead', null, el('tr', null, [
        el('th', { text: 'Provider' }), el('th', { text: 'Protocol' }),
        el('th', { text: 'Base URL' }), el('th', { text: 'Key Env Var' }),
        el('th', { text: 'Key Status' }), el('th', { text: 'Enabled' }),
        el('th', { text: 'Actions' }),
      ])),
      el('tbody', null, rows),
    ])));
  }

  function actionBtn(label, onClick, kind) {
    return el('button', {
      class: 'btn sm' + (kind === 'danger' ? ' danger' : ''),
      type: 'button', text: label, onClick: onClick,
    });
  }

  function jsonTextarea(value) {
    return el('textarea', { spellcheck: 'false', text: value ? JSON.stringify(value, null, 2) : '' });
  }

  function parseJsonField(textarea, label, fallback) {
    const raw = textarea.value.trim();
    if (!raw) return fallback;
    try {
      return JSON.parse(raw);
    } catch (err) {
      throw new Error(label + ' is not valid JSON: ' + err.message);
    }
  }

  function openProviderForm(existing) {
    const isEdit = !!existing;
    const p = existing || { protocol: 'openai_responses', enabled: true, timeout: 600 };
    const idInput = el('input', { type: 'text', value: p.id || '', disabled: isEdit, placeholder: 'my_provider' });
    const nameInput = el('input', { type: 'text', value: p.display_name || '', placeholder: 'My provider' });
    const protocolSelect = el('select', null, ['openai_responses', 'openai_chat', 'anthropic_messages'].map((proto) =>
      el('option', { value: proto, text: proto, selected: (p.protocol || 'openai_responses') === proto })
    ));
    const baseInput = el('input', { type: 'text', value: p.base_url || '', placeholder: 'https://api.example.com/v1' });
    const keyEnvInput = el('input', { type: 'text', value: p.api_key_env || '', placeholder: 'MY_PROVIDER_API_KEY' });
    const timeoutInput = el('input', { type: 'number', value: p.timeout || 600 });
    const headersInput = jsonTextarea(p.headers || {});
    const headersEnvInput = jsonTextarea(p.headers_from_env || {});
    const enabledInput = el('input', { type: 'checkbox', checked: p.enabled !== false });
    const tempKeyInput = el('input', { type: 'password', placeholder: 'sk-… (memory only)' });

    const body = el('div', null, [
      field('Provider ID', idInput, 'Stable identifier referenced by models; must not contain "@".'),
      field('Display Name', nameInput, 'Name shown in the UI.'),
      field('Protocol', protocolSelect, 'Protocol the gateway uses to talk to this upstream.'),
      field('Base URL', baseInput, 'e.g. https://api.openai.com/v1'),
      field('API Key Env Var Name', keyEnvInput, 'Optional. The gateway also reads the key from this environment variable; a locally saved key takes precedence.'),
      field('Timeout (seconds)', timeoutInput, 'Timeout for a single upstream request.'),
      el('div', { class: 'grid-2' }, [
        field('Custom Headers (JSON)', headersInput, 'Fixed headers sent with every request.'),
        field('Headers from Environment (JSON)', headersEnvInput, 'Format: { "Header-Name": "ENV_VAR_NAME" }.'),
      ]),
      field('Enabled', el('label', { class: 'check' }, [enabledInput, 'This provider is enabled'])),
    ]);
    if (isEdit) {
      const keyStatusBadge = el('span', { class: 'badge muted' });
      const keyStatusRow = el('p', { class: 'sub' }, ['Current status: ', keyStatusBadge]);

      // Reflect the key state in the dialog without closing it.
      function renderKeyStatus(ks) {
        ks = ks || {};
        let statusText;
        if (ks.source === 'local') statusText = 'Saved to the local key file · survives restarts';
        else if (ks.source === 'temporary') statusText = 'Temporary key active for this session · lost on restart';
        else if (ks.source === 'environment') statusText = 'From an environment variable · no need to set it here';
        else statusText = 'No key set';
        keyStatusBadge.textContent = statusText;
        keyStatusBadge.className = 'badge ' + (ks.available ? 'ok' : 'err');
      }
      renderKeyStatus(p.key_status);

      async function applyKey(persist) {
        const result = await api('/api/admin/providers/' + encodeURIComponent(p.id) + '/key',
          { method: 'PUT', body: { api_key: tempKeyInput.value, persist: persist } });
        tempKeyInput.value = '';
        renderKeyStatus(result.key_status);
        notify(persist ? 'Key saved locally; survives restarts' : 'Temporary key set (lost on restart)', 'ok');
        loadProviders();
      }

      body.append(el('div', { class: 'panel', style: 'margin-bottom:0' }, [
        el('h2', { text: 'API Key' }),
        el('p', { class: 'sub', text: 'Enter the key here and choose how to keep it — no need to configure environment variables. The key is written to secrets.local.json in the gateway directory (ignored by .gitignore and never committed to Git); it is not written to config.yaml, not logged, and never returned to the browser.' }),
        keyStatusRow,
        el('div', { class: 'inline' }, [tempKeyInput, el('button', {
          class: 'btn primary', type: 'button', text: 'Save Locally', onClick: async () => {
            try { await applyKey(true); } catch (err) { notify(err.message, 'err'); }
          },
        }), el('button', {
          class: 'btn', type: 'button', text: 'This Session Only', onClick: async () => {
            try { await applyKey(false); } catch (err) { notify(err.message, 'err'); }
          },
        }), el('button', {
          class: 'btn danger', type: 'button', text: 'Clear Key', onClick: async () => {
            if (!window.confirm('Clear the saved key for this provider?')) return;
            try {
              const result = await api('/api/admin/providers/' + encodeURIComponent(p.id) + '/key', { method: 'DELETE' });
              renderKeyStatus(result.key_status);
              notify('Cleared', 'ok');
              loadProviders();
            } catch (err) { notify(err.message, 'err'); }
          },
        })]),
      ]));
    }

    const modal = openModal({
      title: isEdit ? 'Edit Provider' : 'Add Provider',
      wide: true,
      body: body,
      footer: [
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onClick: () => modal.close() }),
        el('button', {
          class: 'btn primary', type: 'button', text: 'Save', onClick: async () => {
            try {
              const payload = {
                display_name: nameInput.value.trim(),
                protocol: protocolSelect.value,
                base_url: baseInput.value.trim(),
                api_key_env: keyEnvInput.value.trim() || null,
                timeout: Number(timeoutInput.value) || 600,
                headers: parseJsonField(headersInput, 'Custom Headers', {}),
                headers_from_env: parseJsonField(headersEnvInput, 'Headers from Environment', {}),
                enabled: enabledInput.checked,
              };
              if (!baseInput.value.trim()) throw new Error('Base URL must not be empty.');
              if (isEdit) {
                await api('/api/admin/providers/' + encodeURIComponent(p.id), { method: 'PUT', body: payload });
              } else {
                payload.id = idInput.value.trim();
                if (!payload.id) throw new Error('Provider ID must not be empty.');
                await api('/api/admin/providers', { method: 'POST', body: payload });
              }
              modal.close();
              notify('Saved', 'ok');
              loadProviders();
            } catch (err) { notify(err.message, 'err'); }
          },
        }),
      ],
    });
  }

  function field(label, input, hint) {
    return el('div', { class: 'field' }, [
      el('label', { text: label }), input,
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ]);
  }

  async function testProvider(provider) {
    notify('Testing ' + provider.id + '…');
    try {
      const result = await api('/api/admin/providers/' + encodeURIComponent(provider.id) + '/test', { method: 'POST' });
      if (result.ok) {
        notify('OK — HTTP ' + result.status + ', took ' + result.latency_ms + ' ms' +
          (result.model_count != null ? ' (' + result.model_count + ' models)' : ''), 'ok');
      } else {
        notify('Failed — ' + (result.error || ('HTTP ' + result.status)), 'err');
      }
    } catch (err) { notify(err.message, 'err'); }
  }

  async function toggleProvider(provider, enabled) {
    try {
      await api('/api/admin/providers/' + encodeURIComponent(provider.id), { method: 'PUT', body: { enabled: enabled } });
      notify('Saved', 'ok');
      loadProviders();
    } catch (err) { notify(err.message, 'err'); }
  }

  async function duplicateProvider(provider) {
    const newId = window.prompt('New provider ID:', provider.id + '-copy');
    if (!newId) return;
    try {
      await api('/api/admin/providers/' + encodeURIComponent(provider.id) + '/duplicate', { method: 'POST', body: { id: newId } });
      notify('Saved', 'ok');
      loadProviders();
    } catch (err) { notify(err.message, 'err'); }
  }

  async function deleteProvider(provider) {
    // Find models that reference this provider so we can offer cascade delete.
    let usedBy = [];
    try {
      const data = await api('/api/admin/models');
      usedBy = (data.models || []).filter((m) => m.provider === provider.id).map((m) => m.id);
    } catch (err) { /* fall through to the plain delete below */ }

    let cascade = false;
    if (usedBy.length) {
      cascade = window.confirm(
        'Provider "' + provider.id + '" is referenced by these models: ' + usedBy.join(', ') +
        '.\n\nClick OK to delete it together with these models; click Cancel to delete nothing.'
      );
      if (!cascade) return;
    } else if (!window.confirm('Delete provider "' + provider.id + '"? This cannot be undone.')) {
      return;
    }

    try {
      const path = '/api/admin/providers/' + encodeURIComponent(provider.id) +
        (cascade ? '?cascade=true' : '');
      const result = await api(path, { method: 'DELETE' });
      const removed = (result && result.deleted_models) || [];
      notify(removed.length ? 'Deleted provider and ' + removed.length + ' models' : 'Deleted', 'ok');
      loadProviders();
    } catch (err) { notify(err.message, 'err'); }
  }

  // ---------------------------------------------------------------- models
  async function loadModels() {
    // Load providers too: the model form needs them for its dropdown, and the
    // "add model" guard must not misfire when the providers page was never
    // visited in this session.
    const [data, providers] = await Promise.all([
      api('/api/admin/models'),
      api('/api/admin/providers'),
    ]);
    state.models = data.models || [];
    state.providers = providers.providers || [];
    renderModels();
  }

  // Ensure providers are known; used before opening the model form.
  async function ensureProviders() {
    if (state.providers.length) return state.providers;
    const data = await api('/api/admin/providers');
    state.providers = data.providers || [];
    return state.providers;
  }

  function renderModels() {
    const container = $('#models-table');
    clear(container);
    if (!state.models.length) {
      container.append(el('p', { class: 'muted', text: 'No models configured.' }));
      return;
    }
    const rows = state.models.map((m) => {
      const reasoning = m.reasoning || {};
      return el('tr', null, [
        el('td', null, [
          el('div', { text: m.display_name || m.id }),
          el('div', { class: 'mono muted', text: m.id }),
        ]),
        el('td', null, badge(m.provider, 'accent')),
        el('td', { class: 'mono', text: m.upstream_model }),
        el('td', null, reasoning.default ? badge('Default: ' + reasoning.default, 'muted') : el('span', { class: 'muted', text: '—' })),
        el('td', null, (reasoning.supported && reasoning.supported.length)
          ? el('div', { class: 'virtual-list' }, reasoning.supported.map((lvl) => el('span', { class: 'chip', text: lvl })))
          : el('span', { class: 'muted', text: 'None' })),
        el('td', null, m.enabled ? badge('Enabled', 'ok') : badge('Disabled', 'muted')),
        el('td', null, el('div', { class: 'virtual-list' }, (m.virtual_models || []).map((v) =>
          el('span', { class: 'chip' }, [v, el('button', { type: 'button', text: 'Copy', onClick: () => copyText(v) })])))),
        el('td', null, el('div', { class: 'row-actions' }, [
          actionBtn('Edit', () => openModelForm(m)),
          actionBtn('Test', () => testModel(m)),
          actionBtn('Duplicate', () => duplicateModel(m)),
          actionBtn(m.enabled ? 'Disable' : 'Enable', () => toggleModel(m, !m.enabled)),
          actionBtn('Delete', () => deleteModel(m), 'danger'),
        ])),
      ]);
    });
    container.append(el('div', { class: 'table-wrap' }, el('table', null, [
      el('thead', null, el('tr', null, [
        el('th', { text: 'Model' }), el('th', { text: 'Provider' }),
        el('th', { text: 'Upstream Model' }), el('th', { text: 'Default Tier' }),
        el('th', { text: 'Supported Tiers' }), el('th', { text: 'Enabled' }),
        el('th', { text: 'Virtual Models' }), el('th', { text: 'Actions' }),
      ])),
      el('tbody', null, rows),
    ])));
  }

  // A reasoning level is stored as a JSON mapping object. The UI lets the user
  // pick a well-known destination field plus a strength value, and builds that
  // object; an "advanced" preset keeps raw JSON editing for anything unusual.
  const REASONING_FIELDS = [
    {
      id: 'reasoning.effort',
      label: 'reasoning.effort',
      hint: 'OpenAI Responses style',
      valueHint: 'e.g. high',
      build: (v) => ({ reasoning: { effort: v } }),
    },
    {
      id: 'reasoning_effort',
      label: 'reasoning_effort',
      hint: 'Flat field (some proxy services)',
      valueHint: 'e.g. high',
      build: (v) => ({ reasoning_effort: v }),
    },
    {
      id: 'thinking.budget_tokens',
      label: 'thinking.budget_tokens',
      hint: 'Anthropic thinking budget (number)',
      valueHint: 'e.g. 16000',
      build: (v) => ({ thinking: { type: 'enabled', budget_tokens: v } }),
    },
    {
      id: 'thinking.type',
      label: 'thinking.type',
      hint: 'Anthropic thinking toggle',
      valueHint: 'enabled or disabled',
      build: (v) => ({ thinking: { type: v } }),
    },
    {
      id: 'enable_thinking',
      label: 'enable_thinking',
      hint: 'Qwen thinking toggle (false disables thinking)',
      valueHint: 'true or false',
      build: (v) => ({ enable_thinking: coerceBool(v) }),
    },
    {
      id: 'custom',
      label: 'Custom JSON (advanced)',
      hint: 'Write any mapping object directly',
      valueHint: '',
      build: null,
    },
  ];

  function reasoningField(id) {
    return REASONING_FIELDS.find((f) => f.id === id);
  }

  function toNumberIfNumeric(value) {
    const text = String(value).trim();
    if (text === '') return value;
    const n = Number(text);
    return Number.isFinite(n) && String(n) === text ? n : value;
  }

  // Turn a typed value into a real boolean for fields that expect one.
  function coerceBool(value) {
    if (typeof value === 'boolean') return value;
    const text = String(value).trim().toLowerCase();
    if (['true', '1', 'yes', 'on', 'enabled'].includes(text)) return true;
    if (['false', '0', 'no', 'off', 'disabled'].includes(text)) return false;
    return value;
  }

  // Reverse the preset builders so an existing config opens in simple mode.
  function parseReasoningMapping(mapping) {
    const m = mapping || {};
    const keys = Object.keys(m);

    if (keys.length === 1) {
      const key = keys[0];
      const inner = m[key];
      if (key === 'reasoning_effort' && typeof inner !== 'object') {
        return { field: 'reasoning_effort', value: String(inner) };
      }
      if (key === 'enable_thinking' && typeof inner !== 'object') {
        return { field: 'enable_thinking', value: String(inner) };
      }
      if (key === 'reasoning' && inner && typeof inner === 'object' && Object.keys(inner).length === 1 && 'effort' in inner) {
        return { field: 'reasoning.effort', value: String(inner.effort) };
      }
      if (key === 'thinking' && inner && typeof inner === 'object') {
        const tk = Object.keys(inner).sort();
        if (tk.length === 2 && tk[0] === 'budget_tokens' && tk[1] === 'type' && inner.type === 'enabled') {
          return { field: 'thinking.budget_tokens', value: String(inner.budget_tokens) };
        }
        if (tk.length === 1 && tk[0] === 'type') {
          return { field: 'thinking.type', value: String(inner.type) };
        }
      }
    }
    if (keys.length === 0) {
      return { field: 'reasoning.effort', value: '' };
    }
    return { field: 'custom', json: JSON.stringify(m, null, 2) };
  }

  function openModelForm(existing) {
    const isEdit = !!existing;
    const m = existing || { enabled: true, reasoning: { supported: [], mapping: {} } };
    const idInput = el('input', { type: 'text', value: m.id || '', disabled: isEdit, placeholder: 'my-model' });
    const nameInput = el('input', { type: 'text', value: m.display_name || '', placeholder: 'My model' });
    const providerSelect = el('select', null, state.providers.map((p) =>
      el('option', { value: p.id, text: p.display_name + ' (' + p.id + ')', selected: m.provider === p.id })
    ));
    const upstreamInput = el('input', { type: 'text', value: m.upstream_model || '', placeholder: 'Actual upstream model ID' });
    const enabledInput = el('input', { type: 'checkbox', checked: m.enabled !== false });
    const ignoreClientReasoningInput = el('input', { type: 'checkbox', checked: m.ignore_client_reasoning === true });
    const overridesInput = jsonTextarea(m.request_overrides || {});
    const removeInput = el('textarea', { spellcheck: 'false', text: (m.remove_fields || []).join('\n') });

    const levelsWrap = el('div');
    const previewWrap = el('div', { class: 'virtual-list' });
    const reasoning = m.reasoning || {};
    const levels = (reasoning.supported || []).map((lvl) => {
      const parsed = parseReasoningMapping((reasoning.mapping || {})[lvl]);
      return {
        level: lvl,
        isDefault: reasoning.default === lvl,
        field: parsed.field,
        value: parsed.value || '',
        json: parsed.json || '{}',
      };
    });

    function refreshPreview() {
      clear(previewWrap);
      const base = idInput.value.trim() || '<model>';
      const virtual = [base].concat(levels.map((l) => l.level ? base + '@' + l.level : ''));
      virtual.filter(Boolean).forEach((v) => previewWrap.append(
        el('span', { class: 'chip' }, [v, el('button', { type: 'button', text: 'Copy', onClick: () => copyText(v) })])));
    }

    function levelRow(entry) {
      const nameInput = el('input', { type: 'text', value: entry.level, placeholder: 'high' });
      const defaultInput = el('input', { type: 'radio', name: 'default-level', checked: entry.isDefault });
      const fieldSelect = el('select', null, REASONING_FIELDS.map((f) =>
        el('option', { value: f.id, text: f.label, selected: entry.field === f.id })));
      const valueInput = el('input', {
        type: 'text', value: entry.value || '',
        placeholder: (reasoningField(entry.field) || {}).valueHint || '',
      });
      const jsonInput = el('textarea', { spellcheck: 'false', style: 'min-height:70px', text: entry.json || '{}' });
      const jsonWrap = el('div', { class: 'level-json' }, [
        el('div', { class: 'hint', text: 'Custom mapping JSON, deep-merged directly into the upstream request body.' }),
        jsonInput,
      ]);
      const fieldHint = el('div', { class: 'hint' });

      function syncFields() {
        const preset = reasoningField(fieldSelect.value) || {};
        const isCustom = fieldSelect.value === 'custom';
        valueInput.style.display = isCustom ? 'none' : '';
        jsonWrap.style.display = isCustom ? '' : 'none';
        valueInput.placeholder = preset.valueHint || '';
        if (isCustom) {
          fieldHint.textContent = 'Write the JSON below directly (deep-merged into the upstream request body).';
        } else {
          const sample = valueInput.value.trim() || (preset.valueHint || '').replace('e.g. ', '') || 'value';
          const preview = preset.build ? JSON.stringify(preset.build(toNumberIfNumeric(sample))) : '';
          fieldHint.textContent = (preset.hint || '') + (preview ? ' → will send ' + preview : '');
        }
      }
      fieldSelect.addEventListener('change', syncFields);
      valueInput.addEventListener('input', syncFields);

      const block = el('div', { class: 'level-block' }, [
        el('div', { class: 'level-row' }, [
          nameInput,
          fieldSelect,
          valueInput,
          el('label', { class: 'check' }, [defaultInput, 'Default']),
          actionBtn('Remove', () => {
            const i = levels.indexOf(entry);
            if (i >= 0) levels.splice(i, 1);
            block.remove();
            refreshPreview();
          }),
        ]),
        fieldHint,
        jsonWrap,
      ]);

      entry.inputs = {
        nameInput: nameInput,
        defaultInput: defaultInput,
        fieldSelect: fieldSelect,
        valueInput: valueInput,
        jsonInput: jsonInput,
      };
      syncFields();
      return block;
    }

    const headRow = el('div', { class: 'level-head' }, [
      el('div', { text: 'Tier Name' }),
      el('div', { text: 'Target Field' }),
      el('div', { text: 'Value' }),
      el('div', { text: 'Default' }),
      el('div', { text: '' }),
    ]);
    const rowsWrap = el('div', null, [headRow, ...levels.map(levelRow)]);
    // The header only makes sense once there is at least one level row.
    headRow.style.display = levels.length ? '' : 'none';
    levelsWrap.append(rowsWrap, el('button', {
      class: 'btn sm mt', type: 'button', text: '+ Add Tier', onClick: () => {
        headRow.style.display = '';
        const entry = { level: '', isDefault: false, field: 'reasoning.effort', value: '', json: '{}' };
        levels.push(entry);
        rowsWrap.append(levelRow(entry));
      },
    }));

    idInput.addEventListener('input', refreshPreview);

    const body = el('div', null, [
      el('div', { class: 'grid-2' }, [
        field('Logical Model ID', idInput, 'Model identifier exposed to ZCode; must not contain "@".'),
        field('Display Name', nameInput),
      ]),
      el('div', { class: 'grid-2' }, [
        field('Provider', providerSelect, 'Requests are forwarded to this provider.'),
        field('Upstream Model', upstreamInput, 'The real model ID on the provider side.'),
      ]),
      field('Enabled', el('label', { class: 'check' }, [enabledInput, 'This model is enabled'])),
      el('div', { class: 'panel' }, [
        field('Ignore Client Reasoning Settings', el('label', { class: 'check' }, [
          ignoreClientReasoningInput,
          'Use only the selected tier and drop reasoning parameters sent by the client (ZCode)',
        ]), 'When checked, the gateway strips client-sent reasoning / reasoning_effort / thinking / enable_thinking fields before applying the tier mapping, so the ZCode thinking toggle cannot affect the result. Recommended for models that use different field names (e.g. Qwen\'s reasoning_effort).'),
      ]),
      el('div', { class: 'panel' }, [
        el('h2', { text: 'Reasoning Tiers' }),
        el('p', { class: 'sub', text: 'Tier names can be any string (off, low, high, max, xhigh…). Pick a target field and enter a value; the gateway builds the mapping object for that field and deep-merges it into the upstream request body. Choose "Custom JSON" for special structures. An empty value means the tier sends nothing.' }),
        levelsWrap,
      ]),
      el('div', { class: 'panel' }, [
        el('h2', { text: 'Generated ZCode Models' }),
        previewWrap,
      ]),
      el('div', { class: 'grid-2' }, [
        field('Request Overrides (JSON)', overridesInput, 'Deep-merged into every request for this model.'),
        field('Remove Fields', removeInput, 'One field path per line, e.g. reasoning.summary'),
      ]),
    ]);

    refreshPreview();

    const modal = openModal({
      title: isEdit ? 'Edit Model' : 'Add Model',
      wide: true,
      body: body,
      footer: [
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onClick: () => modal.close() }),
        el('button', {
          class: 'btn primary', type: 'button', text: 'Save', onClick: async () => {
            try {
              const supported = [];
              const mapping = {};
              let defaultLevel = null;
              levels.forEach((entry) => {
                const lvl = (entry.inputs && entry.inputs.nameInput.value || entry.level || '').trim();
                if (!lvl) return;
                supported.push(lvl);
                if (entry.inputs && entry.inputs.defaultInput.checked) defaultLevel = lvl;

                const fieldId = entry.inputs ? entry.inputs.fieldSelect.value : (entry.field || 'custom');
                if (fieldId === 'custom') {
                  const raw = (entry.inputs ? entry.inputs.jsonInput.value : entry.json || '').trim();
                  if (raw && raw !== '{}') {
                    try {
                      mapping[lvl] = JSON.parse(raw);
                    } catch (err) {
                      throw new Error('Custom JSON for tier "' + lvl + '" is not valid JSON: ' + err.message);
                    }
                  }
                } else {
                  const preset = reasoningField(fieldId);
                  const value = (entry.inputs ? entry.inputs.valueInput.value : entry.value || '').trim();
                  if (value !== '' && preset && preset.build) {
                    mapping[lvl] = preset.build(toNumberIfNumeric(value));
                  }
                }
              });
              const payload = {
                display_name: nameInput.value.trim(),
                provider: providerSelect.value,
                upstream_model: upstreamInput.value.trim(),
                enabled: enabledInput.checked,
                ignore_client_reasoning: ignoreClientReasoningInput.checked,
                request_overrides: parseJsonField(overridesInput, 'Request Overrides', {}),
                remove_fields: removeInput.value.split('\n').map((s) => s.trim()).filter(Boolean),
              };
              if (!payload.upstream_model) throw new Error('Upstream model must not be empty.');
              if (!payload.provider) throw new Error('A provider must be selected. Add one on the Providers page first.');
              if (supported.length) {
                payload.reasoning = { supported: supported, default: defaultLevel || supported[0], mapping: mapping };
              } else {
                payload.reasoning = null;
              }
              if (isEdit) {
                await api('/api/admin/models/' + encodeURIComponent(m.id), { method: 'PUT', body: payload });
              } else {
                payload.id = idInput.value.trim();
                if (!payload.id) throw new Error('Logical model ID must not be empty.');
                await api('/api/admin/models', { method: 'POST', body: payload });
              }
              modal.close();
              notify('Saved', 'ok');
              loadModels();
            } catch (err) { notify(err.message, 'err'); }
          },
        }),
      ],
    });
  }

  async function testModel(model) {
    notify('Testing ' + model.id + '…');
    try {
      const result = await api('/api/admin/models/' + encodeURIComponent(model.id) + '/test', { method: 'POST' });
      if (result.ok) notify('OK — HTTP ' + result.status + ', took ' + result.latency_ms + ' ms', 'ok');
      else notify('Failed — ' + (result.error || ('HTTP ' + result.status)), 'err');
    } catch (err) { notify(err.message, 'err'); }
  }

  async function toggleModel(model, enabled) {
    try {
      await api('/api/admin/models/' + encodeURIComponent(model.id), { method: 'PUT', body: { enabled: enabled } });
      notify('Saved', 'ok');
      loadModels();
    } catch (err) { notify(err.message, 'err'); }
  }

  async function duplicateModel(model) {
    const newId = window.prompt('New model ID:', model.id + '-copy');
    if (!newId) return;
    try {
      await api('/api/admin/models/' + encodeURIComponent(model.id) + '/duplicate', { method: 'POST', body: { id: newId } });
      notify('Saved', 'ok');
      loadModels();
    } catch (err) { notify(err.message, 'err'); }
  }

  async function deleteModel(model) {
    if (!window.confirm('Delete model "' + model.id + '"? This cannot be undone.')) return;
    try {
      await api('/api/admin/models/' + encodeURIComponent(model.id), { method: 'DELETE' });
      notify('Deleted', 'ok');
      loadModels();
    } catch (err) { notify(err.message, 'err'); }
  }

  // --------------------------------------------------------- test console
  async function loadConsoleOptions() {
    const data = await api('/api/admin/models');
    state.models = data.models || [];
    const virtual = data.virtual_models || [];
    const select = $('#console-model');
    const previous = select.value;
    clear(select);
    virtual.forEach((v) => select.append(el('option', { value: v, text: v })));
    if (previous) select.value = previous;
  }

  function consoleBody(model) {
    const body = { model: model };
    const instructions = $('#console-instructions').value;
    const input = $('#console-input').value;
    if (instructions.trim()) body.instructions = instructions;
    if (input.trim()) body.input = input;
    body.stream = $('#console-stream').checked;
    return body;
  }

  async function runConsole() {
    const model = $('#console-model').value;
    if (!model) { notify('No model selected. Add a model first.', 'err'); return; }
    const body = consoleBody(model);
    const output = $('#console-output');
    output.textContent = '';
    $('#console-send').disabled = true;
    $('#console-stop').disabled = false;

    if ($('#console-preview').checked) {
      try {
        const preview = await api('/api/admin/preview', { method: 'POST', body: { model: model, body: body } });
        $('#console-preview-panel').style.display = '';
        $('#console-preview-out').textContent = JSON.stringify(preview, null, 2);
      } catch (err) {
        $('#console-preview-panel').style.display = '';
        $('#console-preview-out').textContent = 'Preview failed: ' + err.message;
      }
    } else {
      $('#console-preview-panel').style.display = 'none';
    }

    state.controller = new AbortController();
    const headers = { 'content-type': 'application/json' };
    if (state.token) headers['x-admin-token'] = state.token;

    try {
      if (body.stream) {
        await streamConsole(body, headers, output);
      } else {
        const response = await fetch('/v1/responses', { method: 'POST', headers: headers, body: JSON.stringify(body), signal: state.controller.signal });
        const data = await response.json().catch(() => null);
        output.textContent = $('#console-raw').checked
          ? JSON.stringify(data, null, 2)
          : extractResponseText(data);
        if (!response.ok && data) output.textContent = extractError(data) || output.textContent;
      }
    } catch (err) {
      if (err.name === 'AbortError') output.textContent += '\n[Stopped]';
      else output.textContent += '\n[Error] ' + err.message;
    } finally {
      $('#console-send').disabled = false;
      $('#console-stop').disabled = true;
      state.controller = null;
    }
  }

  function extractResponseText(data) {
    if (!data) return '';
    if (data.output_text) return data.output_text;
    if (Array.isArray(data.output)) {
      return data.output.map((item) => {
        if (item.type === 'message' && Array.isArray(item.content)) {
          return item.content.map((c) => c.text || '').join('');
        }
        if (item.type === 'reasoning') return '[Reasoning] ' + (item.summary || []).map((s) => s.text || '').join('');
        if (item.type === 'function_call') return '[Tool call] ' + item.name + '(' + item.arguments + ')';
        return '';
      }).filter(Boolean).join('\n');
    }
    return JSON.stringify(data, null, 2);
  }

  async function streamConsole(body, headers, output) {
    const raw = $('#console-raw').checked;
    const response = await fetch('/v1/responses', { method: 'POST', headers: headers, body: JSON.stringify(body), signal: state.controller.signal });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      output.textContent = (data && extractError(data)) || 'HTTP ' + response.status;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      const blocks = buffer.split('\n\n');
      buffer = blocks.pop();
      for (const block of blocks) handleSseBlock(block, output, raw);
    }
    if (buffer.trim()) handleSseBlock(buffer, output, raw);
  }

  function handleSseBlock(block, output, raw) {
    const lines = block.split('\n');
    let event = '';
    const dataLines = [];
    for (const line of lines) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    const dataText = dataLines.join('\n');
    if (raw) {
      output.textContent += 'event: ' + event + '\ndata: ' + dataText + '\n\n';
      return;
    }
    let data = null;
    try { data = JSON.parse(dataText); } catch (e) { return; }
    if (event === 'response.output_text.delta' && data.delta) output.textContent += data.delta;
    else if (event.indexOf('reasoning_summary_text.delta') !== -1 && data.delta) output.textContent += data.delta;
    else if (event === 'response.function_call_arguments.delta' && data.delta) output.textContent += '\n[Tool args] ' + data.delta;
    else if (event === 'response.failed' && data.response && data.response.error) {
      output.textContent += '\n[Error] ' + data.response.error.message;
    }
  }

  // ---------------------------------------------------------- configuration
  let configMode = 'form';

  async function loadConfig() {
    const data = await api('/api/admin/config');
    $('#config-yaml').value = data.yaml || '';
    if (!data.valid) {
      notify('Current config is invalid: ' + data.error, 'err');
    }
    switchConfigTab(configMode);
  }

  function switchConfigTab(mode) {
    configMode = mode;
    $('#config-tab-form').classList.toggle('active', mode === 'form');
    $('#config-tab-raw').classList.toggle('active', mode === 'raw');
    $('#config-form-mode').style.display = mode === 'form' ? '' : 'none';
    $('#config-raw-mode').style.display = mode === 'raw' ? '' : 'none';
  }

  function renderDiff(diff) {
    const container = $('#config-diff');
    clear(container);
    if (!diff) return;
    const parts = [];
    const label = { providers: 'providers', models: 'models' };
    ['providers', 'models'].forEach((key) => {
      const d = diff[key] || {};
      if ((d.added || []).length) parts.push('Added ' + label[key] + ': ' + d.added.join(', '));
      if ((d.removed || []).length) parts.push('Removed ' + label[key] + ': ' + d.removed.join(', '));
      if ((d.changed || []).length) parts.push('Changed ' + label[key] + ': ' + d.changed.join(', '));
    });
    if (diff.settings_changed) parts.push('Global settings changed');
    container.append(el('div', { class: 'panel', style: 'margin-bottom:0' }, [
      el('strong', { text: 'Diff summary: ' }),
      el('ul', { class: 'diff-list' }, parts.length
        ? parts.map((p) => el('li', { text: p }))
        : [el('li', { text: 'No changes detected.' })]),
    ]));
  }

  async function saveConfig() {
    try {
      await api('/api/admin/config', { method: 'PUT', body: { yaml: $('#config-yaml').value } });
      notify('Saved', 'ok');
      loadConfig();
      loadStatus().catch(() => {});
    } catch (err) { notify(err.message, 'err'); }
  }

  async function validateConfig() {
    try {
      const result = await api('/api/admin/config/validate', { method: 'POST', body: { yaml: $('#config-yaml').value } });
      if (result.valid) {
        notify('Validation passed', 'ok');
        renderDiff(result.diff);
      } else {
        notify(result.error, 'err');
        clear($('#config-diff'));
      }
    } catch (err) { notify(err.message, 'err'); }
  }

  async function reloadConfig() {
    try {
      await api('/api/admin/config/reload', { method: 'POST' });
      notify('Reloaded', 'ok');
      loadConfig();
    } catch (err) { notify(err.message, 'err'); }
  }

  function downloadConfig() {
    api('/api/admin/config').then((data) => {
      const blob = new Blob([data.yaml || ''], { type: 'text/yaml' });
      const url = URL.createObjectURL(blob);
      const a = el('a', { href: url, download: 'config.yaml' });
      document.body.append(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }).catch((err) => notify(err.message, 'err'));
  }

  function uploadConfig() {
    $('#config-file-input').click();
  }

  async function handleConfigFile(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const text = await file.text();
    try {
      const result = await api('/api/admin/config/upload', { method: 'POST', body: { yaml: text } });
      $('#config-yaml').value = result.yaml || text;
      switchConfigTab('raw');
      renderDiff(result.diff);
      if (window.confirm('The uploaded config is valid. Save it as the active configuration now?')) {
        await saveConfig();
      }
    } catch (err) { notify(err.message, 'err'); }
  }

  async function resetConfig() {
    if (!window.confirm('Reset the configuration to config.example.yaml? This overwrites the current config.')) return;
    try {
      await api('/api/admin/config/reset-example', { method: 'POST' });
      notify('Reset to sample config', 'ok');
      loadConfig();
    } catch (err) { notify(err.message, 'err'); }
  }

  // ------------------------------------------------- full backup (bundle)
  function exportBundle(includeKeys) {
    const path = '/api/admin/config/bundle' + (includeKeys ? '' : '?include_keys=false');
    fetch(path, { headers: state.token ? { 'x-admin-token': state.token } : {} })
      .then((response) => {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      })
      .then((text) => {
        const stamp = new Date().toISOString().slice(0, 10);
        const name = includeKeys ? `zumg-backup-${stamp}.json` : `zumg-config-${stamp}.json`;
        const blob = new Blob([text], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = el('a', { href: url, download: name });
        document.body.append(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        notify(includeKeys ? 'Full backup exported (with keys)' : 'Configuration exported (no keys)', 'ok');
      })
      .catch((err) => notify('Export failed: ' + err.message, 'err'));
  }

  function importBundle() {
    $('#bundle-file-input').click();
  }

  async function handleBundleFile(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch (err) {
      notify('Backup file is not valid JSON: ' + err.message, 'err');
      return;
    }
    const keys = payload.keys || {};
    const keyCount = Object.keys(keys).length;
    const providerCount = payload.config && payload.config.providers
      ? Object.keys(payload.config.providers).length : 0;
    const modelCount = payload.config && payload.config.models
      ? Object.keys(payload.config.models).length : 0;

    const message = 'This backup will import:\n\n'
      + '· Providers: ' + providerCount + '\n'
      + '· Models: ' + modelCount + '\n'
      + '· API keys: ' + keyCount + '\n\n'
      + 'The current configuration will be replaced (keys that exist locally but are not in the backup are kept). Continue?';
    if (!window.confirm(message)) return;

    try {
      const result = await api('/api/admin/config/bundle', { method: 'POST', body: payload });
      notify('Imported: ' + result.providers + ' providers / ' + result.models + ' models / '
        + (result.keys_restored || []).length + ' keys', 'ok');
      await loadConfig();
      loadStatus().catch(() => {});
    } catch (err) { notify(err.message, 'err'); }
  }

  // ------------------------------------------------------------------ logs
  async function loadLogs() {
    const data = await api('/api/admin/logs?limit=100');
    const container = $('#logs-table');
    clear(container);
    const logs = data.logs || [];
    if (!logs.length) {
      container.append(el('p', { class: 'muted', text: 'No requests logged.' }));
      return;
    }
    container.append(el('div', { class: 'table-wrap' }, el('table', null, [
      el('thead', null, el('tr', null, [
        el('th', { text: 'Time' }), el('th', { text: 'Logical Model' }), el('th', { text: 'Tier' }),
        el('th', { text: 'Provider' }), el('th', { text: 'Upstream Model' }), el('th', { text: 'Protocol' }),
        el('th', { text: 'Status' }), el('th', { text: 'Latency' }), el('th', { text: 'Error' }),
      ])),
      el('tbody', null, logs.map((row) => el('tr', null, [
        el('td', { text: fmtTime(row.time_iso || row.time) }),
        el('td', { class: 'mono', text: row.logical_model || '—' }),
        el('td', { text: row.reasoning_level || '—' }),
        el('td', { text: row.provider || '—' }),
        el('td', { class: 'mono', text: row.upstream_model || '—' }),
        el('td', { text: row.protocol || '—' }),
        el('td', null, badge(String(row.status || '—'), row.ok ? 'ok' : 'err')),
        el('td', { text: row.latency_ms != null ? row.latency_ms + ' ms' : '—' }),
        el('td', { text: row.error_type || '—' }),
      ]))),
    ])));
  }

  // ----------------------------------------------------------------- about
  async function loadAbout() {
    const container = $('#about-details');
    clear(container);
    try {
      const meta = state.meta || await api('/api/admin/meta');
      state.meta = meta;
      container.append(
        el('p', null, [el('strong', { text: 'Version: ' }), meta.version]),
        el('p', null, [el('strong', { text: 'Supported protocols: ' }), (meta.protocols || []).join(', ')]),
        el('p', null, [el('strong', { text: 'Default base URL: ' }), meta.default_base_url])
      );
    } catch (err) {
      container.append(el('p', { class: 'muted', text: err.message }));
    }
  }

  // ------------------------------------------------------------------ modal
  //
  // A dialog must only close on an explicit action (×, Cancel, Save, Escape).
  // Clicking the dimmed area does NOT close it, because that is far too easy
  // to do by accident while reaching for a field. We also move focus into the
  // dialog and trap Tab inside it, so keystrokes cannot reach the button that
  // opened the dialog (which would otherwise re-trigger it or dismiss it).
  const FOCUSABLE_SELECTOR = [
    'a[href]', 'button:not([disabled])', 'input:not([disabled])',
    'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  function openModal(options) {
    const root = $('#modal-root');
    const backdrop = el('div', { class: 'modal-backdrop' });
    let closed = false;

    const close = () => {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      clear(root);
    };

    const modal = el('div', {
      class: 'modal' + (options.wide ? ' wide' : ''),
      role: 'dialog',
      'aria-modal': 'true',
    }, [
      el('div', { class: 'modal-head' }, [
        el('h2', { text: options.title }),
        el('button', {
          class: 'close-x', type: 'button', text: '\u00d7',
          'aria-label': 'Close', onClick: close,
        }),
      ]),
      el('div', { class: 'modal-body' }, options.body),
      options.footer ? el('div', { class: 'modal-foot' }, options.footer) : null,
    ]);

    backdrop.append(modal);

    function focusable() {
      return Array.from(modal.querySelectorAll(FOCUSABLE_SELECTOR))
        .filter((n) => n.offsetParent !== null || n === document.activeElement);
    }

    function onKeyDown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    // Clicking anywhere on the backdrop is intentionally a no-op.
    backdrop.addEventListener('click', (event) => { event.stopPropagation(); });

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', onKeyDown, true);
    root.append(backdrop);

    // Put the caret in the first editable field so typing goes to the dialog.
    const firstField = focusable().find((n) => /^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName));
    if (firstField) {
      try { firstField.focus({ preventScroll: true }); } catch (e) { firstField.focus(); }
    } else {
      modal.setAttribute('tabindex', '-1');
      modal.focus({ preventScroll: true });
    }

    return { close: close, modal: modal };
  }

  // ------------------------------------------------------------------ init
  function init() {
    initNav();
    $('#copy-base-url').addEventListener('click', () => copyText($('#zcode-base-url').value));
    $('#add-provider').addEventListener('click', () => openProviderForm(null));
    $('#add-model').addEventListener('click', async () => {
      try {
        await ensureProviders();
      } catch (err) {
        notify(err.message, 'err');
        return;
      }
      if (!state.providers.length) {
        notify('Add a provider first.', 'err');
        showView('providers');
        return;
      }
      openModelForm(null);
    });

    $('#console-send').addEventListener('click', () => runConsole().catch((err) => notify(err.message, 'err')));
    $('#console-stop').addEventListener('click', () => { if (state.controller) state.controller.abort(); });
    $('#console-clear').addEventListener('click', () => {
      $('#console-output').textContent = '';
      $('#console-instructions').value = '';
      $('#console-input').value = '';
      $('#console-preview-out').textContent = '';
      $('#console-preview-panel').style.display = 'none';
    });

    $('#config-tab-form').addEventListener('click', () => switchConfigTab('form'));
    $('#config-tab-raw').addEventListener('click', () => switchConfigTab('raw'));
    $('#config-save').addEventListener('click', () => saveConfig());
    $('#config-validate').addEventListener('click', () => validateConfig());
    $('#config-reload').addEventListener('click', () => reloadConfig());
    $('#config-download').addEventListener('click', () => downloadConfig());
    $('#config-upload').addEventListener('click', () => uploadConfig());
    $('#config-reset').addEventListener('click', () => resetConfig());
    $('#config-file-input').addEventListener('change', (event) => handleConfigFile(event).catch((err) => notify(err.message, 'err')));
    $('#bundle-export').addEventListener('click', () => exportBundle(true));
    $('#bundle-export-nokey').addEventListener('click', () => exportBundle(false));
    $('#bundle-import').addEventListener('click', () => importBundle());
    $('#bundle-file-input').addEventListener('change', (event) => handleBundleFile(event).catch((err) => notify(err.message, 'err')));

    $('#logs-refresh').addEventListener('click', () => loadLogs().catch((err) => notify(err.message, 'err')));

    api('/api/admin/meta').then((meta) => {
      state.meta = meta;
      $('#version-badge').textContent = 'v' + meta.version;
      $('#zcode-base-url').value = meta.default_base_url;
    }).catch(() => {});

    loadStatus().catch((err) => {
      const badgeEl = $('#config-status');
      badgeEl.textContent = err.message.indexOf('Token') !== -1 ? 'Token required' : 'Not connected';
      badgeEl.className = 'badge err';
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
