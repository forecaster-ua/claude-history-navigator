// Detect base path: works at /claude-history/ and at localhost:8055/
const BASE = (() => {
  const p = window.location.pathname;
  if (p === '/' || p === '') return '';
  const clean = p.endsWith('/') ? p.slice(0, -1) : p.replace(/\/[^/]+$/, '');
  return clean;
})();

let currentSessionId = null;
let searchMode = 'text';
let searchTimer = null;

// ── Bootstrap ────────────────────────────────────────────
async function init() {
  await Promise.all([loadStats(), loadSessions(), loadProviders()]);
  setupSearch();
  setupSelectionSave();
}

async function loadStats() {
  try {
    const d = await api('/api/stats');
    document.getElementById('stats').textContent =
      `${d.total_sessions} sessions · ${d.total_projects} projects`;
  } catch {}
}

// ── Sessions sidebar ─────────────────────────────────────
async function loadSessions() {
  try {
    const d = await api('/api/sessions');
    renderSidebar(d.projects || []);
  } catch (e) {
    document.getElementById('sessions-list').innerHTML =
      `<div class="empty-state">Failed to load<br><small>${e.message}</small></div>`;
  }
}

function renderSidebar(projects) {
  const list = document.getElementById('sessions-list');
  list.innerHTML = '';
  if (!projects.length) {
    list.innerHTML = '<div class="empty-state">No sessions found</div>';
    return;
  }
  projects.forEach(proj => {
    const group = el('div', 'project-group');

    const hdr = el('div', 'project-header');
    hdr.innerHTML = `
      <span class="ph-arrow">▶</span>
      <span class="ph-name" title="${esc(proj.project_path)}">${esc(proj.project_path)}</span>
      <span class="ph-count">${proj.sessions.length}</span>`;
    hdr.onclick = () => {
      hdr.classList.toggle('collapsed');
      group.querySelectorAll('.session-item').forEach(i =>
        i.classList.toggle('hidden', hdr.classList.contains('collapsed')));
    };
    group.appendChild(hdr);

    proj.sessions.forEach(s => {
      const item = el('div', 'session-item');
      item.dataset.id = s.id;
      item.innerHTML = `
        <div class="si-meta">
          <span class="si-date">${s.date}</span>
          <span class="si-stats">${fmtSize(s.size_bytes)} · ${s.message_count}msg</span>
        </div>
        <div class="si-preview">${esc(s.first_message || '(empty)')}</div>`;
      item.onclick = () => openSession(s.id, item);
      group.appendChild(item);
    });

    list.appendChild(group);
  });
}

// ── Session viewer ───────────────────────────────────────
async function openSession(id, itemEl) {
  currentSessionId = id;

  // Sidebar active state
  document.querySelectorAll('.session-item.active').forEach(i => i.classList.remove('active'));
  itemEl?.classList.add('active');

  // UI state: show messages, hide search
  showMessages();
  document.getElementById('conv-title').textContent = id;
  document.getElementById('brain-save-btn').style.display = '';
  document.getElementById('brain-search-btn').style.display = '';
  document.getElementById('context-btn').style.display = '';
  document.getElementById('download-btn').style.display = '';
  document.getElementById('delete-btn').style.display = '';

  // Reset context cache info for new session
  ctxCacheInfo = null;
  document.getElementById('context-view-btn').style.display = 'none';

  // Load context cache state for default lang/mode
  const lang = localStorage.getItem('ctx_lang') || 'en';
  const mode = localStorage.getItem('ctx_mode') || 'full';
  api(`/api/sessions/${id}/context?lang=${lang}&mode=${mode}`)
    .then(d => { ctxCacheInfo = d; updateContextBtns(); })
    .catch(() => {});

  const msg = document.getElementById('messages');
  msg.innerHTML = '<div class="loading-row"><span class="spinner"></span> Loading…</div>';

  try {
    const d = await api(`/api/sessions/${id}`);
    renderMessages(d.messages || []);
  } catch (e) {
    msg.innerHTML = `<div class="empty-state">Error: ${esc(e.message)}</div>`;
  }
}

function renderMessages(messages) {
  const container = document.getElementById('messages');
  container.innerHTML = '';

  if (!messages.length) {
    container.innerHTML = '<div class="empty-state">No messages in this session</div>';
    return;
  }

  messages.forEach(m => {
    const isUser = m.role === 'user';
    const wrap = el('div', `msg ${isUser ? 'user' : 'assistant'}`);

    const avatar = el('div', 'msg-avatar');
    avatar.textContent = isUser ? '👤' : '🤖';

    const body = el('div', 'msg-body');
    const meta = el('div', 'msg-meta');
    const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : '';
    meta.innerHTML = `<span class="msg-role">${isUser ? 'User' : 'Claude'}</span>${ts ? `<span>${ts}</span>` : ''}`;
    body.appendChild(meta);

    const content = m.content;
    if (typeof content === 'string' && content.trim()) {
      const bubble = el('div', 'msg-bubble');
      bubble.textContent = content;
      body.appendChild(bubble);
    } else if (Array.isArray(content)) {
      const textParts = [];
      const toolBlocks = [];

      content.forEach(block => {
        if (!block || typeof block !== 'object') return;
        const t = block.type;
        if (t === 'text' && block.text?.trim()) {
          textParts.push(block.text);
        } else if (t === 'thinking' && block.thinking?.trim()) {
          toolBlocks.push(makeToolBlock('thinking', '💭 Thinking', block.thinking));
        } else if (t === 'tool_use') {
          const input = JSON.stringify(block.input || {}, null, 2);
          const label = `🔧 ${block.name || 'Tool'}`;
          const kind = (block.name || '').toLowerCase().includes('bash') ? 'bash' : 'generic';
          toolBlocks.push(makeToolBlock(kind, label, input));
        } else if (t === 'tool_result') {
          const inner = typeof block.content === 'string' ? block.content : JSON.stringify(block.content);
          toolBlocks.push(makeToolBlock('result', '📤 Result', inner));
        }
      });

      if (textParts.length) {
        const bubble = el('div', 'msg-bubble');
        bubble.textContent = textParts.join('\n\n');
        body.appendChild(bubble);
      }
      toolBlocks.forEach(tb => body.appendChild(tb));
    }

    // Tool stdout from toolUseResult
    if (m.toolUseResult?.stdout?.trim()) {
      body.appendChild(makeToolBlock('result', '📟 Output', m.toolUseResult.stdout));
    }

    if (isUser) {
      wrap.appendChild(body);
      wrap.appendChild(avatar);
    } else {
      wrap.appendChild(avatar);
      wrap.appendChild(body);
    }
    container.appendChild(wrap);
  });
}

function makeToolBlock(kind, label, content) {
  const wrap = el('div', 'tool-block');

  const hdr = el('div', 'tool-header');
  hdr.innerHTML = `<span class="tool-chevron">▶</span><span class="tool-label ${kind}">${esc(label)}</span>`;

  const body = el('div', 'tool-body hidden');
  body.textContent = content;

  hdr.onclick = () => {
    const isOpen = !body.classList.contains('hidden');
    body.classList.toggle('hidden', isOpen);
    hdr.classList.toggle('open', !isOpen);
  };

  wrap.appendChild(hdr);
  wrap.appendChild(body);
  return wrap;
}

// ── Search ───────────────────────────────────────────────
function setupSearch() {
  const input = document.getElementById('search-input');

  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length >= 2) {
      searchTimer = setTimeout(() => doSearch(q), 380);
    } else if (!q) {
      clearSearch();
    }
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { input.value = ''; clearSearch(); }
  });

  document.querySelectorAll('.mode-pills button').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.mode-pills button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      searchMode = btn.dataset.mode;
      const q = input.value.trim();
      if (q.length >= 2) doSearch(q);
    };
  });
}

async function doSearch(q) {
  const sr = document.getElementById('search-results');
  sr.style.display = 'block';
  document.getElementById('messages').style.display = 'none';
  sr.innerHTML = '<div class="loading-row"><span class="spinner"></span> Searching…</div>';

  try {
    const d = await api(`/api/search?q=${encodeURIComponent(q)}&mode=${searchMode}&limit=30`);
    renderSearchResults(d, q);
  } catch (e) {
    sr.innerHTML = `<div class="empty-state">Search error: ${esc(e.message)}</div>`;
  }
}

function renderSearchResults(data, q) {
  const sr = document.getElementById('search-results');
  const results = data.results || [];

  if (!results.length) {
    sr.innerHTML = `<div class="empty-state">No results for "${esc(q)}"</div>`;
    return;
  }

  let html = `<div class="search-header">${results.length} result(s) · <strong>${data.mode}</strong> search</div>`;
  sr.innerHTML = html;

  results.forEach(r => {
    const card = el('div', 'search-result');

    if (data.mode === 'text') {
      card.innerHTML = `
        <div class="sr-meta">
          <span class="sr-path">${esc(r.project_path || '')}</span>
          <span class="sr-date">${esc(r.date || '')}</span>
        </div>
        <div class="sr-snippet">${r.snippet || ''}</div>`;
      card.onclick = () => {
        clearSearch();
        // find sidebar item
        const item = document.querySelector(`.session-item[data-id="${r.id}"]`);
        openSession(r.id, item);
      };
    } else {
      const pct = r.similarity ? Math.round(r.similarity * 100) : null;
      card.innerHTML = `
        <div class="sr-meta">
          <span class="sr-path">${esc(r.title || 'Brain result')}</span>
          ${pct ? `<span class="sr-score">${pct}%</span>` : ''}
        </div>
        <div class="sr-snippet">${esc((r.content || '').slice(0, 300))}</div>`;
    }

    sr.appendChild(card);
  });
}

function clearSearch() {
  document.getElementById('search-results').style.display = 'none';
  document.getElementById('messages').style.display = 'flex';
}

function showMessages() {
  document.getElementById('search-results').style.display = 'none';
  document.getElementById('messages').style.display = 'flex';
}

// ── Brain buttons ────────────────────────────────────────
// ── Download ─────────────────────────────────────────────
document.getElementById('download-btn').onclick = () => {
  if (!currentSessionId) return;
  window.location.href = `${BASE}/api/sessions/${currentSessionId}/download`;
};

// ── Context popup ─────────────────────────────────────────
let ctxProviders = [];
let ctxCacheInfo = null; // cached context info for current session+lang+mode

async function loadProviders() {
  try {
    const d = await api('/api/providers');
    ctxProviders = d.providers || [];
  } catch {}
}

function getPillVal(groupId) {
  return document.querySelector(`#${groupId} .pill.active`)?.dataset.val || null;
}

function initPillGroup(groupId, lsKey) {
  const saved = localStorage.getItem(lsKey);
  const group = document.getElementById(groupId);
  group.querySelectorAll('.pill').forEach(p => {
    if (saved && p.dataset.val === saved) {
      group.querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
      p.classList.add('active');
    }
    p.onclick = () => {
      group.querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
      p.classList.add('active');
      localStorage.setItem(lsKey, p.dataset.val);
      refreshCtxPopupState();
    };
  });
}

function populateProviderSelect(selectId, lsKey) {
  const select = document.getElementById(selectId);
  select.innerHTML = '';
  const saved = localStorage.getItem(lsKey);
  ctxProviders.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.label}${p.has_key ? '' : ' (no key)'}`;
    if (saved ? p.id === saved : p.has_key) opt.selected = true;
    select.appendChild(opt);
  });
  select.onchange = () => {
    localStorage.setItem(lsKey, select.value);
    const p = ctxProviders.find(x => x.id === select.value);
    if (p) {
      const modelInput = document.getElementById('set-model');
      if (modelInput && !modelInput.value) modelInput.value = p.default_model;
    }
  };
}

async function refreshCtxPopupState() {
  if (!currentSessionId) return;
  const lang = getPillVal('ctx-lang-pills') || 'en';
  const mode = getPillVal('ctx-mode-pills') || 'full';

  try {
    ctxCacheInfo = await api(`/api/sessions/${currentSessionId}/context?lang=${lang}&mode=${mode}`);
  } catch {
    ctxCacheInfo = null;
  }

  const badge = document.getElementById('ctx-popup-cached-badge');
  const genBtn = document.getElementById('ctx-generate-btn');
  const updateBtn = document.getElementById('ctx-update-btn');
  const viewBtn = document.getElementById('ctx-view-cached-btn');

  if (ctxCacheInfo?.cached) {
    const info = ctxCacheInfo;
    badge.style.display = 'block';
    badge.innerHTML = `◈ Cached · ${info.generated_ago} · ${info.provider}/${info.model}` +
      (info.is_stale ? ` · <span style="color:var(--orange)">⚠ ${info.session_message_count - info.message_count} new messages</span>` : '');
    genBtn.textContent = '↺ Regenerate';
    viewBtn.style.display = '';
    updateBtn.style.display = info.is_stale ? '' : 'none';
  } else {
    badge.style.display = 'none';
    genBtn.textContent = '◈ Generate';
    viewBtn.style.display = 'none';
    updateBtn.style.display = 'none';
  }
}

function openCtxPopup() {
  if (!currentSessionId) return;
  initPillGroup('ctx-lang-pills', 'ctx_lang');
  initPillGroup('ctx-mode-pills', 'ctx_mode');
  populateProviderSelect('ctx-provider-select', 'ctx_provider');
  refreshCtxPopupState();
  document.getElementById('ctx-popup-overlay').style.display = 'flex';
}

document.getElementById('context-btn').onclick = openCtxPopup;
document.getElementById('context-view-btn').onclick = () => showAllContexts();

document.getElementById('ctx-popup-close').onclick = () => {
  document.getElementById('ctx-popup-overlay').style.display = 'none';
};
document.getElementById('ctx-popup-overlay').onclick = (e) => {
  if (e.target === document.getElementById('ctx-popup-overlay'))
    document.getElementById('ctx-popup-overlay').style.display = 'none';
};

async function runContextGeneration(endpoint) {
  const lang = getPillVal('ctx-lang-pills') || 'en';
  const mode = getPillVal('ctx-mode-pills') || 'full';
  const provider = document.getElementById('ctx-provider-select').value;
  const providerObj = ctxProviders.find(p => p.id === provider);

  document.getElementById('ctx-popup-overlay').style.display = 'none';

  const btn = document.getElementById('context-btn');
  const viewBtn = document.getElementById('context-view-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Generating…';
  viewBtn.style.display = 'none';

  try {
    const d = await api(`/api/sessions/${currentSessionId}/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang, mode, provider, model: providerObj?.default_model }),
    });
    ctxCacheInfo = { cached: true, ...d, generated_ago: 'just now', is_stale: false };
    updateContextBtns();
    showModal(d.context_text, `${d.provider} / ${d.model} · ${lang.toUpperCase()} / ${mode}`);
  } catch (e) {
    toast('Context error: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '◈ Context';
    updateContextBtns();
  }
}

document.getElementById('ctx-generate-btn').onclick = () => runContextGeneration('context');
document.getElementById('ctx-update-btn').onclick = () => runContextGeneration('context/update');
document.getElementById('ctx-view-cached-btn').onclick = () => {
  document.getElementById('ctx-popup-overlay').style.display = 'none';
  if (ctxCacheInfo?.cached)
    showModal(ctxCacheInfo.context_text, `${ctxCacheInfo.provider} / ${ctxCacheInfo.model} · ${ctxCacheInfo.language?.toUpperCase()} / ${ctxCacheInfo.mode}`);
};

async function showAllContexts() {
  if (!currentSessionId) return;
  try {
    const d = await api(`/api/sessions/${currentSessionId}/contexts`);
    const contexts = d.contexts || [];
    if (!contexts.length) { openCtxPopup(); return; }
    showModalWithTabs(contexts);
  } catch (e) {
    toast('Error loading contexts: ' + e.message, true);
  }
}

function showModalWithTabs(contexts) {
  const tabsEl = document.getElementById('modal-tabs');
  tabsEl.innerHTML = '';

  let active = contexts[0];

  function renderTab(ctx, idx) {
    const tab = el('div', `modal-tab${ctx.is_stale ? ' stale' : ''}${idx === 0 ? ' active' : ''}`);
    tab.textContent = `${ctx.language.toUpperCase()}/${ctx.mode} · ${ctx.generated_ago}`;
    tab.title = `${ctx.provider}/${ctx.model}${ctx.is_stale ? ' · has new messages' : ''}`;
    tab.onclick = () => {
      tabsEl.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      active = ctx;
      renderModalBody(ctx);
      document.getElementById('modal-provider').textContent =
        `${ctx.provider} / ${ctx.model}`;
    };
    tabsEl.appendChild(tab);
  }

  contexts.forEach((ctx, i) => renderTab(ctx, i));
  renderModalBody(active);
  document.getElementById('modal-provider').textContent =
    `${active.provider} / ${active.model}`;
  document.getElementById('modal-overlay').style.display = 'flex';
}

function renderModalBody(ctx) {
  const body = document.getElementById('modal-body');
  body.innerHTML = markdownToHtml(ctx.context_text || '');
}

function updateContextBtns() {
  const viewBtn = document.getElementById('context-view-btn');
  if (ctxCacheInfo?.cached) {
    viewBtn.style.display = '';
    viewBtn.textContent = ctxCacheInfo.is_stale ? '◈ Context ●' : '◈ View Context';
  } else {
    viewBtn.style.display = 'none';
  }
  // Update count badge async
  if (currentSessionId) {
    api(`/api/sessions/${currentSessionId}/contexts`)
      .then(d => {
        const n = d.contexts?.length || 0;
        if (n > 0) {
          viewBtn.style.display = '';
          viewBtn.textContent = `◈ View Context${n > 1 ? ` (${n})` : ''}`;
        }
      }).catch(() => {});
  }
}

function markdownToHtml(md) {
  return md
    .replace(/^# (.+)$/gm, '<h1 style="font-size:15px;color:var(--text);margin-bottom:8px">$1</h1>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^### (.+)$/gm, '<h3 style="color:var(--text2);font-size:13px;margin:10px 0 4px">$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--bg3);padding:1px 4px;border-radius:3px;font-family:monospace;font-size:11px">$1</code>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/gs, m => `<ul style="padding-left:16px;margin:4px 0">${m}</ul>`)
    .replace(/\n/g, '<br>');
}

function showModal(markdown, providerLabel) {
  document.getElementById('modal-provider').textContent = providerLabel || '';
  document.getElementById('modal-tabs').innerHTML = '';
  document.getElementById('modal-body').innerHTML = markdownToHtml(markdown);
  document.getElementById('modal-overlay').style.display = 'flex';
}

document.getElementById('modal-close').onclick = () => {
  document.getElementById('modal-overlay').style.display = 'none';
};
document.getElementById('modal-overlay').onclick = (e) => {
  if (e.target === document.getElementById('modal-overlay'))
    document.getElementById('modal-overlay').style.display = 'none';
};
document.getElementById('modal-copy-btn').onclick = () => {
  const text = document.getElementById('modal-body').innerText;
  if (navigator.clipboard && location.protocol === 'https:') {
    navigator.clipboard.writeText(text)
      .then(() => toast('Copied to clipboard!'))
      .catch(() => copyFallback(text));
  } else {
    copyFallback(text);
  }
};

function copyFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(ta);
  toast(ok ? 'Copied to clipboard!' : 'Copy failed — select text manually', !ok);
}
document.getElementById('modal-brain-btn').onclick = async () => {
  const text = document.getElementById('modal-body').innerText;
  await saveToBrain(text);
};

document.getElementById('delete-btn').onclick = async () => {
  if (!currentSessionId) return;
  if (!confirm(`Delete session ${currentSessionId.slice(0, 8)}…?\nФайл будет удалён с диска.`)) return;
  try {
    await api(`/api/sessions/${currentSessionId}`, { method: 'DELETE' });
    toast('Session deleted');
    // Remove from sidebar
    document.querySelector(`.session-item[data-id="${currentSessionId}"]`)?.remove();
    // Reset viewer
    currentSessionId = null;
    document.getElementById('conv-title').textContent = 'Select a session';
    document.getElementById('messages').innerHTML = '<div class="welcome"><div class="welcome-icon">◈</div><h3>Claude History Browser</h3><p>Select a session from the sidebar or use search to find conversations</p></div>';
    ['delete-btn','brain-save-btn','brain-search-btn'].forEach(id => document.getElementById(id).style.display = 'none');
    loadStats();
  } catch (e) {
    toast('Delete failed: ' + e.message, true);
  }
};

document.getElementById('brain-save-btn').onclick = async () => {
  const sel = window.getSelection()?.toString().trim();
  const text = sel?.length > 10 ? sel : null;
  if (!text && !currentSessionId) return;

  if (!text) {
    toast('Select text first to save to Brain', true);
    return;
  }
  await saveToBrain(text);
};

document.getElementById('brain-search-btn').onclick = async () => {
  const q = document.getElementById('search-input').value.trim() ||
    prompt('Brain semantic search query:');
  if (!q) return;
  document.getElementById('search-input').value = q;
  document.querySelectorAll('.mode-pills button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === 'semantic');
  });
  searchMode = 'semantic';
  doSearch(q);
};

// Selection → floating save button
function setupSelectionSave() {
  const btn = document.getElementById('sel-save');

  document.addEventListener('mouseup', e => {
    if (btn.contains(e.target)) return;
    const sel = window.getSelection();
    const text = sel?.toString().trim();
    if (text?.length > 20 && currentSessionId) {
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      btn.style.left = `${rect.left + window.scrollX}px`;
      btn.style.top = `${rect.bottom + window.scrollY + 8}px`;
      btn.style.display = 'block';
      btn._text = text;
    } else {
      btn.style.display = 'none';
    }
  });

  btn.onclick = async () => {
    btn.style.display = 'none';
    await saveToBrain(btn._text);
  };
}

async function saveToBrain(text) {
  try {
    await api('/api/brain/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, session_id: currentSessionId, tags: [] })
    });
    toast('Saved to Brain ✓');
  } catch (e) {
    toast('Save failed: ' + e.message, true);
  }
}

// ── Settings panel ────────────────────────────────────────
document.getElementById('settings-btn').onclick = openSettings;
document.getElementById('settings-close').onclick = () => {
  document.getElementById('settings-overlay').style.display = 'none';
};
document.getElementById('settings-overlay').onclick = (e) => {
  if (e.target === document.getElementById('settings-overlay'))
    document.getElementById('settings-overlay').style.display = 'none';
};

async function openSettings() {
  const [cfg, provData] = await Promise.all([
    api('/api/llm/config'),
    api('/api/providers'),
  ]);

  const provSelect = document.getElementById('set-provider');
  provSelect.innerHTML = '';
  provData.providers.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    if (p.id === cfg.provider) opt.selected = true;
    provSelect.appendChild(opt);
  });

  document.getElementById('set-model').value = cfg.model || '';

  const keysList = document.getElementById('set-keys-list');
  keysList.innerHTML = '';
  provData.providers.forEach(p => {
    const row = el('div', 'provider-key-row');
    row.innerHTML = `
      <div class="provider-key-label">
        <span class="provider-key-name">${esc(p.label)}</span>
        <span class="provider-key-status ${p.has_key ? 'ok' : 'missing'}">${p.has_key ? '✓ Key set' : 'No key'}</span>
      </div>
      <div class="provider-key-input-row">
        <input type="password" placeholder="${p.has_key ? '••••••••••••' : 'Paste API key...'}" data-provider="${p.id}">
        <button class="btn-save-key" data-provider="${p.id}">Save</button>
      </div>`;
    keysList.appendChild(row);
  });

  keysList.querySelectorAll('.btn-save-key').forEach(btn => {
    btn.onclick = async () => {
      const pid = btn.dataset.provider;
      const input = keysList.querySelector(`input[data-provider="${pid}"]`);
      const key = input.value.trim();
      if (!key) { toast('Enter API key first', true); return; }
      try {
        await api('/api/providers/key', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: pid, api_key: key }),
        });
        input.value = '';
        input.placeholder = '••••••••••••';
        const badge = btn.closest('.provider-key-row').querySelector('.provider-key-status');
        badge.className = 'provider-key-status ok';
        badge.textContent = '✓ Key set';
        toast(`${pid} key saved`);
        await loadProviders();
        ctxProviders = ctxProviders.map(p => p.id === pid ? { ...p, has_key: true } : p);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
  });

  document.getElementById('set-save-btn').onclick = async () => {
    const provider = provSelect.value;
    const model = document.getElementById('set-model').value.trim();
    try {
      await api('/api/llm/config', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, model }),
      });
      toast('Settings saved ✓');
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };

  document.getElementById('settings-overlay').style.display = 'flex';
}

// ── Refresh ──────────────────────────────────────────────
document.getElementById('refresh-btn').onclick = async () => {
  document.getElementById('stats').textContent = '…';
  await api('/api/index/refresh', { method: 'POST' });
  await Promise.all([loadStats(), loadSessions()]);
  toast('Index refreshed ✓');
};

// ── Utils ────────────────────────────────────────────────
async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function el(tag, cls) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function fmtSize(b) {
  if (b < 1024) return b + 'B';
  if (b < 1048576) return (b / 1024).toFixed(0) + 'KB';
  return (b / 1048576).toFixed(1) + 'MB';
}

function toast(msg, err = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${err ? 'err' : 'ok'}`;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 3000);
}

// Start
init();
