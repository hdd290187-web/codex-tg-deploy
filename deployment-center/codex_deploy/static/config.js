(() => {
  const prefix = document.documentElement.dataset.prefix;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const get = (id) => document.getElementById(id);
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf;
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(`${prefix}${path}`, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
    return data;
  }
  const state = { codex: [], claude: [] };
  function valid(engine, value) {
    try { if (engine === 'codex') { if (typeof value !== 'string') throw Error('TOML'); return value.trim().length > 0; } JSON.parse(value); return true; }
    catch (_) { return false; }
  }
  function renderHistory(engine) {
    const select = get(`${engine}-history`); select.replaceChildren();
    state[engine].forEach((item) => { const option = document.createElement('option'); option.value = item.version; option.textContent = item.version === 0 ? '默认配置 · v0' : `当前模板 v${item.version} · ${new Date(item.created_at * 1000).toLocaleString()}`; select.append(option); });
    select.addEventListener('change', () => showDiff(engine).catch((e) => get('config-error').textContent = e.message));
  }
  async function showDiff(engine) {
    const version = get(`${engine}-history`).value;
    if (!version) return;
    const item = await api(`/api/config/templates/${engine}/${version}`);
    const current = get(engine).value.split('\n'); const previous = item.content.split('\n');
    const lines = []; const length = Math.max(current.length, previous.length);
    for (let i = 0; i < length; i++) { if (current[i] === previous[i]) lines.push(`  ${current[i] || ''}`); else { if (previous[i] !== undefined) lines.push(`- ${previous[i]}`); if (current[i] !== undefined) lines.push(`+ ${current[i]}`); } }
    get(`${engine}-diff`).textContent = `v${version} 与编辑器当前内容的差异\n\n` + lines.join('\n');
  }
  async function load() {
    const data = await api('/api/config/templates');
    for (const engine of ['codex', 'claude']) {
      state[engine] = data[engine] || []; renderHistory(engine);
      if (state[engine][0]) get(engine).value = state[engine][0].content;
      get(`${engine}-version`).textContent = state[engine][0] ? `当前生效 v${state[engine][0].version}` : '默认 v0';
      get(`${engine}-valid`).textContent = valid(engine, get(engine).value) ? '格式有效' : '格式待修正';
      await showDiff(engine);
    }
  }
  async function save(engine) {
    const content = get(engine).value;
    if (!valid(engine, content)) { get(`${engine}-valid`).textContent = '格式无效'; get('config-error').textContent = `${engine} 配置格式无效，未保存。`; return; }
    await api('/api/config/templates', { method: 'POST', body: JSON.stringify({ engine, content }) });
    get('config-error').textContent = `✓ ${engine} 模板已保存为新版本。`;
    await load();
  }
  async function rollback(engine) {
    const version = get(`${engine}-history`).value;
    if (!version || !confirm(`确认把 ${engine} 回滚为 v${version} 并创建新版本？`)) return;
    await api(`/api/config/templates/${engine}/${version}/rollback`, { method: 'POST', body: '{}' });
    get('config-error').textContent = `✓ ${engine} 已创建回滚版本。`; await load();
  }
  for (const engine of ['codex', 'claude']) { get(`save-${engine}`).addEventListener('click', () => save(engine).catch((e) => get('config-error').textContent = e.message)); get(`rollback-${engine}`).addEventListener('click', () => rollback(engine).catch((e) => get('config-error').textContent = e.message)); }
  load().catch((e) => get('config-error').textContent = e.message);
})();
