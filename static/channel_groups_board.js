/* TikTok Channel Groups Board — Per-account grouped TikTok channel manager & batch Dedup/FYP scanner */
(function () {
  'use strict';

  const ROW_MAX_CHANNELS = 8;

  const state = {
    loaded: false,
    loadedForUser: '',
    loading: false,
    saving: false,
    updatedAt: 0,
    topTabOrder: [],
    groups: [],
    selectedTopKey: '',
    selectedHandles: new Set(), // key: `${groupId}::${username}`
    addTargetGroupId: '__new__',
    scanning: false,
    stopRequested: false,
    scanningNowHandle: '',
    scanningGroupId: '',
    scanProgress: { done: 0, total: 0, bad: 0, clean: 0, err: 0, label: '' },
    sessionResults: new Map(), // handle -> { success, error, checkedAt, channel, user_info, videos, stats }
    resultFilter: 'all', // all | bad | clean | err
    dragGroupId: '',
    dragChannelInfo: null,
    dragTopKey: '',
    editGroupId: '',
    // Floating Channel Detail Modal state (Check-Post style)
    detailHandle: '',
    detailLoading: false,
    detailFilter: 'all', // all | dup | fyp | clean
    detailViewMode: 'table', // table | grid
  };

  function canUseChannelGroupsBoard() {
    if (typeof currentVipUser === 'undefined' || !currentVipUser) return false;
    return Boolean(String(currentVipUser.username || '').trim());
  }
  window.canUseChannelGroupsBoard = canUseChannelGroupsBoard;

  function esc(val) {
    if (typeof escapeHtml === 'function') return escapeHtml(String(val ?? ''));
    return String(val ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function notify(msg) {
    if (typeof showToast === 'function') showToast(msg);
  }

  function uid(prefix) {
    return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
  }

  function cleanLabel(v, maxLen = 80) {
    return String(v || '').replace(/\s+/g, ' ').trim().slice(0, maxLen);
  }

  function topKeyFromName(name) {
    const s = cleanLabel(name, 80)
      .replace(/[Đđ]/g, 'd')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
    return s ? `top-${s.slice(0, 64)}` : 'top-khac';
  }

  function inferTopGroupName(groupName) {
    const val = cleanLabel(groupName, 80);
    if (!val) return 'KHÁC';
    const base = val.replace(/\s+\d+$/u, '').trim();
    if (/^indo\s+web$/iu.test(base)) return 'INDO WEB';
    const m = base.match(/^(US|UK|INDO|ĐỨC|DUC|MEXICO|JP|KR|VN|BR|FR|ES|IT|TH|PH|MY|TW)(?:\s+(?:GAME|WEB))?$/iu);
    if (m) {
      const c = m[1].toUpperCase();
      return (c === 'DUC' || c === 'ĐỨC') ? 'ĐỨC' : c;
    }
    return base || 'KHÁC';
  }

  function extractTikTokHandle(raw) {
    let s = String(raw || '').trim();
    if (!s) return '';
    if (/^https?:\/\//i.test(s) || /tiktok\.com\//i.test(s)) {
      if (!/^https?:\/\//i.test(s)) s = 'https://' + s.replace(/^\/+/, '');
      try {
        const u = new URL(s);
        const host = u.hostname.toLowerCase();
        if (host !== 'tiktok.com' && !host.endsWith('.tiktok.com')) return '';
        const parts = u.pathname.split('/').filter(Boolean);
        if (parts.length && parts[0].startsWith('@')) s = parts[0].slice(1);
        else return '';
      } catch (_) {
        return '';
      }
    }
    s = s.replace(/^@+/, '').split(/[?#]/)[0].replace(/\/+$/, '').trim().toLowerCase();
    return /^[a-z0-9._]{2,64}$/.test(s) ? s : '';
  }

  function parseBulkHandlesMeta(rawText) {
    const tokens = String(rawText || '')
      .split(/[\r\n,\t ;]+/)
      .map(t => t.trim())
      .filter(Boolean);
    const out = [];
    const seen = new Set();
    let dupCount = 0;
    for (const tok of tokens) {
      const h = extractTikTokHandle(tok);
      if (!h) continue;
      if (seen.has(h)) {
        dupCount += 1;
        continue;
      }
      seen.add(h);
      out.push(h);
    }
    return { handles: out, dupCount };
  }

  function parseBulkHandles(rawText) {
    return parseBulkHandlesMeta(rawText).handles;
  }

  window.handleCgSmartPaste = function (e) {
    const el = e && e.target;
    const clip = (e && e.clipboardData) || window.clipboardData;
    if (!el || !clip) return;
    const raw = String(clip.getData('text') || '');
    const tokens = raw.split(/[\r\n,\t ;]+/).map(s => s.trim()).filter(Boolean);
    if (!tokens.length) return;
    e.preventDefault();

    const start = typeof el.selectionStart === 'number' ? el.selectionStart : el.value.length;
    const end = typeof el.selectionEnd === 'number' ? el.selectionEnd : start;
    const before = el.value.slice(0, start);
    const after = el.value.slice(end);

    const prefix = (before.length > 0 && !before.endsWith('\n')) ? '\n' : '';
    const body = tokens.join('\n');
    const suffix = (!after || !after.startsWith('\n')) ? '\n' : '';
    const inserted = prefix + body + suffix;

    el.value = before + inserted + after;
    const newPos = before.length + inserted.length;
    try { el.setSelectionRange(newPos, newPos); } catch (_) {}
    if (!after.trim()) {
      el.scrollTop = el.scrollHeight;
    }
    window.updateCgBulkInputCount();
  };

  window.updateCgBulkInputCount = function () {
    const el = document.getElementById('cgBulkChannelsInput');
    const raw = String(el ? el.value : '');
    const { handles, dupCount } = parseBulkHandlesMeta(raw);
    const cntEl = document.getElementById('cgBulkInputCount');
    const dupEl = document.getElementById('cgBulkDupHint');
    const clrBtn = document.getElementById('btnClearCgBulkInput');
    if (cntEl) cntEl.textContent = `${handles.length} kênh`;
    if (dupEl) {
      if (dupCount > 0) {
        dupEl.textContent = `Tự bỏ ${dupCount} dòng trùng`;
        dupEl.classList.remove('hidden');
      } else {
        dupEl.classList.add('hidden');
      }
    }
    if (clrBtn) clrBtn.classList.toggle('hidden', !raw.trim());
  };

  window.clearCgBulkInput = function () {
    const el = document.getElementById('cgBulkChannelsInput');
    if (el) {
      el.value = '';
      try { el.focus(); } catch (_) {}
    }
    window.updateCgBulkInputCount();
  };

  function deriveTopTabs() {
    const map = new Map();
    for (const grp of state.groups) {
      const tName = cleanLabel(grp.topGroupName, 80) || inferTopGroupName(grp.name);
      grp.topGroupName = tName;
      const tKey = topKeyFromName(tName);
      let entry = map.get(tKey);
      if (!entry) {
        entry = { key: tKey, name: tName, groupCount: 0, channelCount: 0, groupIds: [] };
        map.set(tKey, entry);
      }
      entry.groupCount += 1;
      entry.channelCount += (grp.channels || []).length;
      entry.groupIds.push(grp.id);
    }
    const orderIdx = new Map(state.topTabOrder.map((k, idx) => [k, idx]));
    const list = Array.from(map.values());
    list.sort((a, b) => {
      const ia = orderIdx.has(a.key) ? orderIdx.get(a.key) : 99999;
      const ib = orderIdx.has(b.key) ? orderIdx.get(b.key) : 99999;
      return ia - ib;
    });
    if (list.length && !list.some(t => t.key === state.selectedTopKey)) {
      state.selectedTopKey = list[0].key;
    } else if (!list.length) {
      state.selectedTopKey = '';
    }
    return list;
  }

  function totalChannelsCount() {
    return state.groups.reduce((acc, g) => acc + (g.channels ? g.channels.length : 0), 0);
  }

  function updateNavBadge() {
    const badge = document.getElementById('channelGroupsNavCount');
    const tabBtn = document.getElementById('tabMainChannelGroups');
    const loggedIn = canUseChannelGroupsBoard();
    if (tabBtn) tabBtn.classList.remove('hidden');
    if (!badge) return;
    const total = totalChannelsCount();
    badge.textContent = String(total);
    badge.classList.toggle('hidden', !loggedIn);
  }

  function computeStatsFromVideos(videos, forceDedupLocked = false) {
    const list = Array.isArray(videos) ? videos : [];
    const dedupLocked = Boolean(forceDedupLocked || (list.length > 0 && list.every(v => Boolean(v && v._dedup_locked))));
    let dup = 0;
    let fyp = 0;
    let clean = 0;
    for (const v of list) {
      const isDup = Boolean(!dedupLocked && v && (v.is_dup || v.is_crp_punished || v.is_copyright || Number(v.status_code || 0) === 141));
      const isFyp = Boolean(v && (v.is_fyp_restricted || v.shadow || (v.has_penalty && !v.is_crp_punished) || v.is_copyright || Number(v.status_code || 0) === 141));
      if (isDup) dup += 1;
      if (isFyp) fyp += 1;
      if (!dedupLocked && !isDup && !isFyp) clean += 1;
    }
    return { total: list.length, dup, fyp, clean, dedupLocked };
  }

  function findFullResultEntry(handle) {
    const h = String(handle || '').toLowerCase();
    if (state.sessionResults.has(h)) {
      const sr = state.sessionResults.get(h);
      if (sr && sr.success && Array.isArray(sr.videos)) return sr;
    }
    if (typeof checkHistoryItems !== 'undefined' && Array.isArray(checkHistoryItems)) {
      const hit = checkHistoryItems.find(it => String(it.username || '').toLowerCase() === h);
      if (hit && Array.isArray(hit.videos)) {
        return {
          ...hit,
          success: true,
          stats: hit.stats || computeStatsFromVideos(hit.videos),
        };
      }
    }
    return null;
  }

  function syncEntryIntoHistoryMemory(entry) {
    if (!entry || !entry.success || !entry.username) return;
    const h = String(entry.username).toLowerCase();
    if (typeof checkHistoryItems !== 'undefined' && Array.isArray(checkHistoryItems)) {
      const idx = checkHistoryItems.findIndex(it => String(it.username || '').toLowerCase() === h);
      if (idx >= 0) checkHistoryItems[idx] = entry;
      else checkHistoryItems.unshift(entry);
    }
  }

  function getChannelSummary(ch) {
    const h = String(ch.username || '').toLowerCase();
    if (state.sessionResults.has(h)) {
      const sr = state.sessionResults.get(h);
      if (!sr.success) return { state: 'err', error: sr.error || 'Lỗi quét' };
      return { state: 'ok', ...sr.stats };
    }
    const hist = findFullResultEntry(h);
    if (hist && hist.stats) {
      return {
        state: 'ok',
        total: Number(hist.stats.total || (hist.videos || []).length || 0),
        dup: Number(hist.stats.dup || 0),
        fyp: Number(hist.stats.fyp || 0),
        clean: Number(hist.stats.clean || 0),
      };
    }
    if (ch.lastCheck && Number(ch.lastCheck.checkedAt || 0) > 0) {
      return {
        state: 'ok',
        total: Number(ch.lastCheck.total || 0),
        dup: Number(ch.lastCheck.dup || 0),
        fyp: Number(ch.lastCheck.fyp || 0),
        clean: Number(ch.lastCheck.clean || 0),
      };
    }
    return { state: 'none' };
  }

  async function loadChannelGroupsBoard(force = false) {
    updateNavBadge();
    if (!canUseChannelGroupsBoard()) {
      state.groups = [];
      state.topTabOrder = [];
      state.selectedTopKey = '';
      state.selectedHandles.clear();
      state.sessionResults.clear();
      state.loaded = false;
      state.loadedForUser = '';
      updateNavBadge();
      renderChannelGroupsBoard();
      return;
    }
    const curUser = String(currentVipUser.username || '').trim().toLowerCase();
    if (state.loadedForUser && state.loadedForUser !== curUser) {
      state.groups = [];
      state.topTabOrder = [];
      state.selectedTopKey = '';
      state.selectedHandles.clear();
      state.sessionResults.clear();
      state.loaded = false;
    }
    if (!force && state.loaded && state.loadedForUser === curUser) {
      renderChannelGroupsBoard();
      return;
    }
    if (state.loading) return;
    state.loading = true;
    try {
      const headers = (typeof getDeviceHeaders === 'function') ? getDeviceHeaders(true) : {};
      const resp = await fetch('/api/channel-groups', { headers });
      const data = (typeof safeParseJson === 'function') ? await safeParseJson(resp) : await resp.json();
      if (resp.ok && data && data.success) {
        state.groups = Array.isArray(data.groups) ? data.groups : [];
        state.topTabOrder = Array.isArray(data.top_tab_order) ? data.top_tab_order : [];
        state.updatedAt = Number(data.updated_at || 0);
        state.loaded = true;
        state.loadedForUser = curUser;
      }
    } catch (err) {
      console.warn('[ChannelGroups] Lỗi tải nhóm kênh:', err);
    } finally {
      state.loading = false;
      updateNavBadge();
      renderChannelGroupsBoard();
    }
  }
  window.loadChannelGroupsBoard = loadChannelGroupsBoard;

  async function persistChannelGroups(allowEmpty = false) {
    if (!canUseChannelGroupsBoard()) return false;
    state.saving = true;
    updateNavBadge();
    try {
      const headers = {
        'Content-Type': 'application/json',
        ...((typeof getDeviceHeaders === 'function') ? getDeviceHeaders(true) : {}),
      };
      const resp = await fetch('/api/channel-groups', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          top_tab_order: state.topTabOrder,
          groups: state.groups,
          allow_empty: Boolean(allowEmpty),
          vip_token: (typeof getVipToken === 'function') ? getVipToken() : '',
        }),
      });
      const data = (typeof safeParseJson === 'function') ? await safeParseJson(resp) : await resp.json();
      if (resp.ok && data && data.success) {
        state.groups = Array.isArray(data.groups) ? data.groups : state.groups;
        state.topTabOrder = Array.isArray(data.top_tab_order) ? data.top_tab_order : state.topTabOrder;
        state.updatedAt = Number(data.updated_at || state.updatedAt);
        updateNavBadge();
        return true;
      }
      notify((data && data.error) || 'Không lưu được danh sách nhóm kênh.');
      return false;
    } catch (_) {
      notify('Lỗi kết nối khi lưu danh sách nhóm kênh.');
      return false;
    } finally {
      state.saving = false;
    }
  }

  function selectionKey(groupId, username) {
    return `${groupId}::${String(username || '').toLowerCase()}`;
  }

  function pruneSelections() {
    const valid = new Set();
    for (const g of state.groups) {
      for (const ch of (g.channels || [])) {
        valid.add(selectionKey(g.id, ch.username));
      }
    }
    for (const k of Array.from(state.selectedHandles)) {
      if (!valid.has(k)) state.selectedHandles.delete(k);
    }
  }

  function getVisibleGroups(topTabs) {
    const activeTop = topTabs.find(t => t.key === state.selectedTopKey);
    if (!activeTop) return [];
    const idSet = new Set(activeTop.groupIds);
    return state.groups.filter(g => idSet.has(g.id));
  }

  function getSelectedChannelObjects() {
    pruneSelections();
    const out = [];
    const seenHandles = new Set();
    for (const g of state.groups) {
      for (const ch of (g.channels || [])) {
        if (state.selectedHandles.has(selectionKey(g.id, ch.username)) && !seenHandles.has(ch.username)) {
          seenHandles.add(ch.username);
          out.push({ group: g, channel: ch });
        }
      }
    }
    return out;
  }

  // ==================== SETTINGS MODAL & CRUD ACTIONS ====================
  function syncCgSettingsModalUI() {
    const grpSel = document.getElementById('cgGroupSelect');
    const inlineFallback = document.getElementById('cgInlineNewGroupFallback');
    const topDatalist = document.getElementById('cgExistingTopTabsList');
    const topTabs = deriveTopTabs();
    if (topDatalist) {
      topDatalist.innerHTML = topTabs.map(t => `<option value="${esc(t.name)}"></option>`).join('');
    }
    if (!grpSel) return;
    if (!state.groups.length) {
      state.addTargetGroupId = '__new__';
    } else if (state.addTargetGroupId !== '__new__' && !state.groups.some(g => g.id === state.addTargetGroupId)) {
      const vis = getVisibleGroups(topTabs);
      state.addTargetGroupId = vis.length ? vis[0].id : state.groups[0].id;
    }
    const opts = [
      ...state.groups.map(g => `<option value="${esc(g.id)}" ${state.addTargetGroupId === g.id ? 'selected' : ''}>${esc(g.name)} (${(g.channels || []).length} kênh · Tab ${esc(g.topGroupName || inferTopGroupName(g.name))})</option>`),
      `<option value="__new__" ${(!state.groups.length || state.addTargetGroupId === '__new__') ? 'selected' : ''}>➕ Tạo nhóm mới</option>`,
    ];
    grpSel.innerHTML = opts.join('');
    if (inlineFallback) {
      inlineFallback.classList.toggle('hidden', Boolean(state.groups.length && state.addTargetGroupId !== '__new__'));
    }
  }

  window.openCgSettingsModal = function (prefillGroupId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    if (!canUseChannelGroupsBoard()) {
      notify('🔐 Vui lòng đăng nhập tài khoản để tạo & lưu nhóm kênh riêng!');
      if (typeof openVipModal === 'function') openVipModal(true, 'register');
      return;
    }
    if (prefillGroupId) {
      state.addTargetGroupId = prefillGroupId;
    } else if (state.groups.length) {
      const topTabs = deriveTopTabs();
      const vis = getVisibleGroups(topTabs);
      state.addTargetGroupId = vis.length ? vis[0].id : state.groups[0].id;
    } else {
      state.addTargetGroupId = '__new__';
    }
    syncCgSettingsModalUI();
    window.updateCgBulkInputCount();
    document.getElementById('cgSettingsModal')?.classList.remove('hidden');
    setTimeout(() => {
      const targetEl = state.addTargetGroupId === '__new__'
        ? document.getElementById('cgNewGroupNameInput')
        : document.getElementById('cgBulkChannelsInput');
      if (targetEl) targetEl.focus();
    }, 30);
  };
  window.toggleCgAddPanel = window.openCgSettingsModal;

  window.closeCgSettingsModal = function () {
    document.getElementById('cgSettingsModal')?.classList.add('hidden');
  };

  window.onCgGroupSelectChange = function (selEl) {
    state.addTargetGroupId = selEl.value;
    const inlineFallback = document.getElementById('cgInlineNewGroupFallback');
    if (inlineFallback) inlineFallback.classList.toggle('hidden', state.addTargetGroupId !== '__new__');
    if (state.addTargetGroupId === '__new__') {
      setTimeout(() => document.getElementById('cgNewGroupNameInput')?.focus(), 20);
    }
  };

  window.submitCgAddForm = async function (ev) {
    if (ev) ev.preventDefault();
    const grpSel = document.getElementById('cgGroupSelect');
    const newGrpIn = document.getElementById('cgNewGroupNameInput');
    const newTopIn = document.getElementById('cgNewTopTabInput');
    const bulkIn = document.getElementById('cgBulkChannelsInput');
    const customNameIn = document.getElementById('cgSingleCustomNameInput');

    const targetId = grpSel ? grpSel.value : state.addTargetGroupId;
    const handles = parseBulkHandles(bulkIn ? bulkIn.value : '');
    const customSingleName = cleanLabel(customNameIn ? customNameIn.value : '', 80);

    let targetGroup = null;
    if (targetId === '__new__' || !state.groups.length) {
      const gName = cleanLabel(newGrpIn ? newGrpIn.value : '', 80);
      if (!gName) {
        notify('⚠️ Vui lòng nhập tên nhóm mới (ví dụ: US 1, US Game, ĐỨC 1)!');
        if (newGrpIn) newGrpIn.focus();
        return;
      }
      const existingSameName = state.groups.find(g => g.name.toLowerCase() === gName.toLowerCase());
      if (existingSameName) {
        targetGroup = existingSameName;
        state.addTargetGroupId = existingSameName.id;
      } else {
        const explicitTop = cleanLabel(newTopIn ? newTopIn.value : '', 80);
        const topName = explicitTop || inferTopGroupName(gName);
        const gid = uid('grp');
        targetGroup = {
          id: gid,
          name: gName,
          topGroupName: topName,
          rowId: gid,
          channels: [],
        };
        state.groups.push(targetGroup);
        const tKey = topKeyFromName(topName);
        if (!state.topTabOrder.includes(tKey)) state.topTabOrder.push(tKey);
        state.selectedTopKey = tKey;
        state.addTargetGroupId = gid;
      }
    } else {
      targetGroup = state.groups.find(g => g.id === targetId);
      if (!targetGroup) {
        notify('⚠️ Không tìm thấy nhóm đích.');
        return;
      }
    }

    if (!handles.length && targetId !== '__new__') {
      notify('⚠️ Hãy dán ít nhất 1 @username hoặc link kênh TikTok!');
      if (bulkIn) bulkIn.focus();
      return;
    }

    const existingMap = new Map();
    for (const g of state.groups) {
      for (const ch of (g.channels || [])) {
        existingMap.set(String(ch.username).toLowerCase(), g.name);
      }
    }

    let added = 0;
    const skipped = [];
    for (const h of handles) {
      if (existingMap.has(h)) {
        skipped.push(`@${h} (${existingMap.get(h)})`);
        continue;
      }
      const hist = findFullResultEntry(h);
      const dispName = (handles.length === 1 && customSingleName)
        ? customSingleName
        : (hist && hist.nickname ? hist.nickname : h);
      const avUrl = (hist && hist.avatar) ? hist.avatar : '';
      const chObj = {
        id: uid('ch'),
        username: h,
        name: dispName,
        url: `https://www.tiktok.com/@${h}`,
        avatarUrl: avUrl,
        addedAt: Math.floor(Date.now() / 1000),
      };
      if (hist && hist.stats) {
        chObj.lastCheck = {
          checkedAt: Number(hist.checked_at || Math.floor(Date.now() / 1000)),
          total: Number(hist.stats.total || (hist.videos || []).length || 0),
          dup: Number(hist.stats.dup || 0),
          fyp: Number(hist.stats.fyp || 0),
          clean: Number(hist.stats.clean || 0),
        };
      }
      targetGroup.channels.push(chObj);
      existingMap.set(h, targetGroup.name);
      added += 1;
    }

    state.selectedTopKey = topKeyFromName(targetGroup.topGroupName || inferTopGroupName(targetGroup.name));
    await persistChannelGroups(false);
    if (bulkIn) bulkIn.value = '';
    if (customNameIn) customNameIn.value = '';
    if (newGrpIn) newGrpIn.value = '';
    if (newTopIn) newTopIn.value = '';
    window.updateCgBulkInputCount();
    renderChannelGroupsBoard();

    if (added > 0 && skipped.length > 0) {
      notify(`✅ Đã thêm ${added} kênh vào "${targetGroup.name}" · Bỏ qua ${skipped.length} kênh trùng: ${skipped.slice(0, 3).join(', ')}`);
    } else if (added > 0) {
      notify(`✅ Đã lưu ${added} kênh vào nhóm "${targetGroup.name}"!`);
    } else if (skipped.length > 0) {
      notify(`⚠️ Kênh đã tồn tại: ${skipped.slice(0, 3).join(', ')}`);
    } else {
      notify(`✅ Đã tạo nhóm "${targetGroup.name}"!`);
    }
  };

  window.removeCgChannel = async function (groupId, channelId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const grp = state.groups.find(g => g.id === groupId);
    if (!grp) return;
    const ch = (grp.channels || []).find(c => c.id === channelId);
    if (!ch) return;
    if (!window.confirm(`Xóa kênh "${ch.name || '@' + ch.username}" khỏi nhóm "${grp.name}"?`)) return;
    grp.channels = grp.channels.filter(c => c.id !== channelId);
    state.selectedHandles.delete(selectionKey(groupId, ch.username));
    await persistChannelGroups(state.groups.length === 0);
    renderChannelGroupsBoard();
    notify(`🗑️ Đã xóa @${ch.username} khỏi nhóm ${grp.name}`);
  };

  window.deleteCgGroup = async function (groupId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const grp = state.groups.find(g => g.id === groupId);
    if (!grp) return;
    const cnt = (grp.channels || []).length;
    const msg = cnt > 0
      ? `Xóa nhóm "${grp.name}" và ${cnt} kênh bên trong?`
      : `Xóa nhóm "${grp.name}"?`;
    if (!window.confirm(msg)) return;
    state.groups = state.groups.filter(g => g.id !== groupId);
    pruneSelections();
    await persistChannelGroups(state.groups.length === 0);
    renderChannelGroupsBoard();
    notify(`🗑️ Đã xóa nhóm "${grp.name}"`);
  };

  window.openCgEditGroupModal = function (groupId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const grp = state.groups.find(g => g.id === groupId);
    if (!grp) return;
    state.editGroupId = groupId;
    const modal = document.getElementById('cgEditGroupModal');
    const nameIn = document.getElementById('cgEditGroupNameInput');
    const sepChk = document.getElementById('cgEditSeparateTabChk');
    const topIn = document.getElementById('cgEditTopTabInput');
    const topDatalist = document.getElementById('cgExistingTopTabsList');
    if (!modal || !nameIn || !sepChk || !topIn) return;

    const topTabs = deriveTopTabs();
    if (topDatalist) {
      topDatalist.innerHTML = topTabs.map(t => `<option value="${esc(t.name)}"></option>`).join('');
    }
    nameIn.value = grp.name;
    const curTop = grp.topGroupName || inferTopGroupName(grp.name);
    const isSeparate = topKeyFromName(curTop) === topKeyFromName(grp.name);
    sepChk.checked = isSeparate;
    topIn.value = curTop;
    topIn.disabled = isSeparate;
    modal.classList.remove('hidden');
    setTimeout(() => nameIn.focus(), 30);
  };

  window.onCgEditSeparateChange = function (chk) {
    const nameIn = document.getElementById('cgEditGroupNameInput');
    const topIn = document.getElementById('cgEditTopTabInput');
    if (!topIn) return;
    topIn.disabled = chk.checked;
    if (chk.checked && nameIn) {
      topIn.value = cleanLabel(nameIn.value, 80);
    }
  };

  window.closeCgEditGroupModal = function () {
    state.editGroupId = '';
    document.getElementById('cgEditGroupModal')?.classList.add('hidden');
  };

  window.saveCgEditGroupModal = async function (ev) {
    if (ev) ev.preventDefault();
    const grp = state.groups.find(g => g.id === state.editGroupId);
    if (!grp) return window.closeCgEditGroupModal();
    const nameIn = document.getElementById('cgEditGroupNameInput');
    const sepChk = document.getElementById('cgEditSeparateTabChk');
    const topIn = document.getElementById('cgEditTopTabInput');
    const nextName = cleanLabel(nameIn ? nameIn.value : '', 80);
    if (!nextName) {
      notify('⚠️ Tên nhóm không được để trống.');
      return;
    }
    const nextTop = (sepChk && sepChk.checked)
      ? nextName
      : (cleanLabel(topIn ? topIn.value : '', 80) || inferTopGroupName(nextName));
    grp.name = nextName;
    grp.topGroupName = nextTop;
    const tKey = topKeyFromName(nextTop);
    if (!state.topTabOrder.includes(tKey)) state.topTabOrder.push(tKey);
    state.selectedTopKey = tKey;
    window.closeCgEditGroupModal();
    await persistChannelGroups(false);
    renderChannelGroupsBoard();
    notify(`✅ Đã cập nhật nhóm "${nextName}" (Tab: ${nextTop})`);
  };

  // ==================== SELECTION & CARD CLICK ====================
  window.toggleCgCardSelect = function (groupId, username, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const k = selectionKey(groupId, username);
    if (state.selectedHandles.has(k)) state.selectedHandles.delete(k);
    else state.selectedHandles.add(k);
    renderChannelGroupsBoard();
  };

  window.onCgCardClick = function (groupId, username, ev) {
    if (ev && ev.target && ev.target.closest('.tt-cg-select-chk, .tt-cg-remove, a')) return;
    if (ev && (ev.ctrlKey || ev.metaKey)) {
      window.toggleCgCardSelect(groupId, username, ev);
      return;
    }
    const forceRescan = Boolean(ev && ev.shiftKey);
    window.openCgChannelModal(username, forceRescan, ev);
  };

  window.toggleCgSelectGroup = function (groupId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const grp = state.groups.find(g => g.id === groupId);
    if (!grp || !(grp.channels || []).length) return;
    const keys = grp.channels.map(ch => selectionKey(grp.id, ch.username));
    const allSelected = keys.every(k => state.selectedHandles.has(k));
    for (const k of keys) {
      if (allSelected) state.selectedHandles.delete(k);
      else state.selectedHandles.add(k);
    }
    renderChannelGroupsBoard();
  };

  window.toggleCgSelectCurrentTab = function () {
    const topTabs = deriveTopTabs();
    const visGroups = getVisibleGroups(topTabs);
    const keys = [];
    for (const g of visGroups) {
      for (const ch of (g.channels || [])) keys.push(selectionKey(g.id, ch.username));
    }
    if (!keys.length) return;
    const allSelected = keys.every(k => state.selectedHandles.has(k));
    if (allSelected) {
      for (const k of keys) state.selectedHandles.delete(k);
    } else {
      for (const k of keys) state.selectedHandles.add(k);
    }
    renderChannelGroupsBoard();
  };

  window.clearCgSelection = function () {
    state.selectedHandles.clear();
    renderChannelGroupsBoard();
  };

  window.selectCgTopTab = function (topKey) {
    state.selectedTopKey = topKey;
    renderChannelGroupsBoard();
  };

  // ==================== EXPORT / IMPORT JSON ====================
  window.exportCgJson = function () {
    const stamp = new Date().toISOString().slice(0, 10);
    const payload = {
      format: 'tikflow-tiktok-channel-groups-v1',
      exportedAt: new Date().toISOString(),
      top_tab_order: state.topTabOrder,
      groups: state.groups,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `tiktok-nhom-kenh-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    notify('📤 Đã xuất file JSON danh sách nhóm kênh!');
  };

  window.triggerCgImportJson = function () {
    document.getElementById('cgImportJsonFileInput')?.click();
  };

  window.handleCgImportJsonFile = async function (inputEl) {
    const file = inputEl && inputEl.files && inputEl.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const rawGroups = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.groups) ? parsed.groups : null);
      if (!rawGroups) {
        notify('⚠️ File JSON không đúng định dạng danh sách nhóm kênh.');
        return;
      }
      if (state.groups.length > 0 && !window.confirm(`Nhập file JSON (${rawGroups.length} nhóm) sẽ thay thế danh sách hiện tại. Tiếp tục?`)) {
        return;
      }
      state.groups = rawGroups;
      if (parsed && Array.isArray(parsed.top_tab_order)) {
        state.topTabOrder = parsed.top_tab_order;
      }
      const ok = await persistChannelGroups(rawGroups.length === 0);
      renderChannelGroupsBoard();
      if (ok) notify(`📥 Đã nhập ${state.groups.length} nhóm kênh thành công!`);
    } catch (e) {
      notify('⚠️ Không đọc được file JSON: ' + (e.message || e));
    } finally {
      inputEl.value = '';
    }
  };

  // ==================== SCAN ENGINE (DEDUP & FYP) ====================
  async function scanSingleChannelHandle(handle) {
    const cleanH = String(handle || '').replace(/^@+/, '').trim().toLowerCase();
    const limitEl = document.getElementById('videoLimitSelect');
    const limit = Math.max(10, Math.min(35, Number(limitEl ? limitEl.value : 18) || 18));
    const vipTok = (typeof getVipToken === 'function') ? getVipToken() : '';
    const reqHeaders = {
      'Content-Type': 'application/json',
      ...((typeof getDeviceHeaders === 'function') ? getDeviceHeaders(true) : {}),
    };
    const resp = await fetch('/api/check', {
      method: 'POST',
      headers: reqHeaders,
      body: JSON.stringify({
        query: '@' + cleanH,
        max_cursor: 0,
        max_videos: limit,
        cookie: '',
        proxy: '',
        sec_uid: '',
        vip_token: vipTok,
        device_id: (typeof getHardwareDeviceId === 'function') ? getHardwareDeviceId() : '',
        device_label: (typeof getDeviceLabel === 'function') ? getDeviceLabel() : '',
      }),
    });
    const data = (typeof safeParseJson === 'function') ? await safeParseJson(resp) : await resp.json();
    if (typeof rememberDedupProvider === 'function' && data) {
      rememberDedupProvider(data);
    }
    if (data && data.free_quota && typeof updateFreeQuotaBannerUI === 'function') {
      updateFreeQuotaBannerUI(data.free_quota);
    }
    if (!resp.ok || !data || !data.success) {
      if (data && data.device_blocked) {
        try { localStorage.removeItem('tikflow_vip_token'); } catch (_) {}
        if (typeof currentVipUser !== 'undefined') currentVipUser = null;
        if (typeof renderVipNav === 'function') renderVipNav();
        state.stopRequested = true;
      }
      if (data && data.quota_exceeded) {
        state.stopRequested = true;
        notify(data.error || '⛔ Đã hết 10/10 lượt Miễn Phí hôm nay! Vui lòng nâng cấp VIP.');
        if (typeof openVipModal === 'function') openVipModal(true);
      }
      return {
        success: false,
        username: cleanH,
        error: (data && data.error) || 'Không đọc được kênh này.',
        checkedAt: Math.floor(Date.now() / 1000),
      };
    }
    const videos = Array.isArray(data.list) ? data.list : [];
    const isDedupLocked = Boolean(data.dedup_locked);
    const stats = computeStatsFromVideos(videos, isDedupLocked);
    const ch = data.channel || {};
    const nowTs = Math.floor(Date.now() / 1000);
    for (const grp of state.groups) {
      for (const item of (grp.channels || [])) {
        if (String(item.username).toLowerCase() === cleanH) {
          if (ch.avatar) item.avatarUrl = ch.avatar;
          if (ch.nickname && (!item.name || item.name.toLowerCase() === cleanH)) {
            item.name = ch.nickname;
          }
          if (!isDedupLocked) {
            item.lastCheck = { checkedAt: nowTs, ...stats };
          }
        }
      }
    }
    const entry = {
      success: true,
      username: cleanH,
      nickname: ch.nickname || cleanH,
      avatar: ch.avatar || '',
      region: ch.region || 'US',
      region_display: ch.region_display || '',
      followers: Number(ch.followers || 0),
      followers_fmt: ch.followers_fmt || '0',
      create_date: ch.createDate || '—',
      video_count_total: Number(ch.videoCount || videos.length),
      checked_at: nowTs,
      checkedAt: nowTs,
      channel: ch,
      user_info: data.user_info || {},
      videos,
      stats,
      dedup_locked: isDedupLocked,
    };
    syncEntryIntoHistoryMemory(entry);
    return entry;
  }

  // ==================== FLOATING CHANNEL DETAIL MODAL (CHECK-POST STYLE) ====================
  function getNavigationChannelList(currentHandle) {
    const topTabs = deriveTopTabs();
    const visGroups = getVisibleGroups(topTabs);
    const visList = [];
    for (const g of visGroups) {
      for (const c of (g.channels || [])) {
        visList.push({ handle: c.username.toLowerCase(), name: c.name || c.username, groupName: g.name, groupId: g.id });
      }
    }
    if (visList.some(x => x.handle === currentHandle)) return visList;
    const allList = [];
    for (const g of state.groups) {
      for (const c of (g.channels || [])) {
        allList.push({ handle: c.username.toLowerCase(), name: c.name || c.username, groupName: g.name, groupId: g.id });
      }
    }
    return allList;
  }

  function buildGridCardHtmlForModal(v, allVideos, handle) {
    const isThisDup = Boolean(v.is_dup || v.is_crp_punished || (v.group_id && String(v.group_id) !== String(v.aweme_id) && String(v.group_id) !== '0' && String(v.group_id) !== 'null'));
    const isCopyright = Boolean(v.is_copyright || v.status_code === 141 || (v.penalty_details && v.penalty_details.toLowerCase().includes('bản quyền')));
    const isFyp = Boolean(v.is_fyp_restricted || v.shadow || (v.has_penalty && !v.is_crp_punished) || isCopyright);
    const isSameChDup = Boolean(v.is_same_channel_dup || (isThisDup && v.group_id && String(v.group_id) !== String(v.aweme_id) && allVideos.some(x => String(x.aweme_id) === String(v.group_id))));

    let borderClass = 'border border-slate-700/80 bg-slate-900/70 hover:border-slate-500';
    if (isCopyright) borderClass = 'border-2 border-rose-600 ring-2 ring-rose-600/50 bg-rose-950/30';
    else if (isFyp) borderClass = 'border-2 border-red-500 ring-2 ring-red-500/50 bg-red-950/25';
    else if (isThisDup) borderClass = 'border-2 border-rose-500/80 bg-rose-950/15';

    const bigBadgeCls = 'px-4 py-2 rounded-2xl text-white font-black text-sm sm:text-base uppercase tracking-wide shadow-[0_8px_24px_rgba(0,0,0,0.75)] border-2 backdrop-blur-md text-center leading-tight';
    const comboBadgeCls = 'px-3.5 py-2 rounded-2xl text-white font-black text-xs sm:text-[13.5px] uppercase tracking-wide shadow-[0_8px_24px_rgba(0,0,0,0.75)] border-2 backdrop-blur-md text-center leading-tight';

    let centerBadge = `<span class="${bigBadgeCls} bg-emerald-600/95 border-emerald-200">🟢 SẠCH</span>`;
    if (isCopyright) {
      centerBadge = `<span class="${bigBadgeCls} bg-rose-700/95 border-rose-200">BẢN QUYỀN</span>`;
    } else if (v._dedup_locked) {
      centerBadge = isFyp
        ? `<span class="${bigBadgeCls} bg-amber-500/95 border-amber-200">🚨 MẤT FYP</span>`
        : `<button type="button" onclick="event.stopPropagation(); if(typeof openVipModal==='function') openVipModal(true, 'register');" class="pointer-events-auto cursor-pointer ${bigBadgeCls} bg-gradient-to-r from-amber-500/95 to-orange-500/95 hover:from-amber-400 hover:to-orange-400 border-amber-200 flex items-center gap-1.5"><i class="fa-solid fa-crown text-yellow-200"></i> VIP</button>`;
    } else if (isFyp && isThisDup) {
      centerBadge = `<span class="${comboBadgeCls} bg-rose-600/95 border-rose-200">${isSameChDup ? '🔴 TRÙNG KÊNH / MẤT FYP' : '🔴 TRÙNG / MẤT FYP'}</span>`;
    } else if (isFyp) {
      centerBadge = `<span class="${bigBadgeCls} bg-amber-500/95 border-amber-200">🚨 MẤT FYP</span>`;
    } else if (isThisDup) {
      centerBadge = `<span class="${isSameChDup ? comboBadgeCls : bigBadgeCls} bg-rose-600/95 border-rose-200">${isSameChDup ? '🔴 TRÙNG CHÍNH KÊNH' : '🔴 TRÙNG'}</span>`;
    }

    const coverProxied = (typeof getProxiedImage === 'function') ? getProxiedImage(v.cover_url, 'Video', '') : (v.cover_url || '');
    const flagHtml = (typeof getCountryFlag === 'function') ? getCountryFlag(v.region) : '';
    const regCode = (typeof formatCountryCode === 'function') ? formatCountryCode(v.region) : (v.region || '');
    const descTxt = (v.desc && v.desc.trim()) ? v.desc.trim() : '(không có mô tả)';
    const plays = Number(v.play_count || 0).toLocaleString('vi-VN');

    return `
      <div class="tt-cg-modal-card glass-card rounded-2xl overflow-hidden flex flex-col justify-between group transition-all duration-200 ${borderClass}">
        <div class="tt-cg-modal-thumb relative bg-black overflow-hidden cursor-pointer" onclick="openModal('${esc(v.aweme_id)}', '${esc(handle)}')">
          <img src="${coverProxied}" alt="Cover" loading="lazy" referrerpolicy="no-referrer" class="w-full h-full object-cover group-hover:scale-105 transition duration-300"
               onerror="this.src='https://placehold.co/300x400/111726/38bdf8?text=TikTok'" />
          <span class="absolute top-1.5 left-1.5 z-10 px-1.5 py-0.5 rounded bg-black/80 flex items-center gap-1 text-[10px] text-white font-bold border border-slate-700/60">
            ${flagHtml}<span>${esc(regCode)}</span>
          </span>
          <div class="absolute inset-0 flex items-center justify-center pointer-events-none p-2 z-10">${centerBadge}</div>
          ${v.duration_str ? `<span class="absolute bottom-1.5 right-1.5 px-1 py-0.5 rounded bg-black/80 text-[9px] text-white font-mono font-bold z-10">${esc(v.duration_str)}</span>` : ''}
          <div class="absolute bottom-1.5 left-1.5 flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/75 text-white text-[10px] font-mono font-bold z-10">
            <i class="fa-regular fa-eye text-cyan-400"></i><span>${plays}</span>
          </div>
        </div>
        <div class="px-2 py-1.5 space-y-1 bg-dark-900/95 flex-shrink-0">
          <div class="flex items-center justify-between gap-1">
            <p class="text-[11px] font-semibold truncate ${v.desc && v.desc.trim() ? 'text-white' : 'text-slate-500 italic'}" data-tip="${esc(descTxt)}">${esc(descTxt)}</p>
            <a href="${esc(v.share_url || '#')}" target="_blank" rel="noopener" class="text-cyan-400 hover:text-white flex-shrink-0 p-0.5" data-tip="Mở trên TikTok">
              <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
            </a>
          </div>
          <div class="grid grid-cols-4 gap-1 text-[10px] text-slate-300 font-mono pt-1 border-t border-slate-800 text-center">
            <span><i class="fa-solid fa-heart text-rose-500 mr-0.5"></i>${esc(v.digg_count_fmt || '0')}</span>
            <span><i class="fa-solid fa-comment text-sky-400 mr-0.5"></i>${esc(v.comment_count_fmt || '0')}</span>
            <span><i class="fa-solid fa-arrow-trend-up text-emerald-400 mr-0.5"></i>${esc(v.share_count_fmt || '0')}</span>
            <span><i class="fa-solid fa-bookmark text-amber-400 mr-0.5"></i>${esc(v.collect_count_fmt || '0')}</span>
          </div>
        </div>
      </div>`;
  }

  function renderCgChannelDetailModal() {
    const modal = document.getElementById('cgChannelDetailModal');
    const content = document.getElementById('cgChannelDetailContent');
    if (!modal || !content || !state.detailHandle) return;

    const h = state.detailHandle;
    const navList = getNavigationChannelList(h);
    const curNavIdx = navList.findIndex(x => x.handle === h);
    const prevHandle = curNavIdx > 0 ? navList[curNavIdx - 1].handle : (navList.length > 1 ? navList[navList.length - 1].handle : '');
    const nextHandle = (curNavIdx >= 0 && curNavIdx < navList.length - 1) ? navList[curNavIdx + 1].handle : (navList.length > 1 ? navList[0].handle : '');
    const grpInfo = navList[curNavIdx] || null;

    const entry = findFullResultEntry(h);
    const errEntry = state.sessionResults.get(h);
    const hasErr = !entry && errEntry && !errEntry.success;

    let savedCard = null;
    for (const g of state.groups) {
      const c = (g.channels || []).find(item => String(item.username).toLowerCase() === h);
      if (c) { savedCard = c; break; }
    }

    const dispName = (entry && entry.nickname) || (savedCard && savedCard.name) || h;
    const rawAv = (entry && entry.avatar) || (savedCard && savedCard.avatarUrl) || '';
    const avSrc = rawAv && typeof getProxiedImage === 'function'
      ? getProxiedImage(rawAv, dispName, '')
      : `https://ui-avatars.com/api/?name=${encodeURIComponent(dispName)}&background=0d111a&color=38bdf8&size=96`;
    const regCode = (typeof formatCountryCode === 'function') ? formatCountryCode((entry && entry.region) || 'US') : ((entry && entry.region) || 'US');
    const flagHtml = (typeof getCountryFlag === 'function') ? getCountryFlag(regCode) : '';

    const navigationGroups = new Map();
    for (const item of navList) {
      const key = item.groupId || item.groupName;
      if (!navigationGroups.has(key)) navigationGroups.set(key, { name: item.groupName, items: [] });
      navigationGroups.get(key).items.push(item);
    }
    const switcherOpts = Array.from(navigationGroups.values()).map(group => `
      <optgroup label="${esc(group.name)} (${group.items.length} kênh)">
        ${group.items.map((item, i) => `<option value="${esc(item.handle)}" ${item.handle === h ? 'selected' : ''}>${i + 1}. ${esc(item.name)} (@${esc(item.handle)})</option>`).join('')}
      </optgroup>`).join('');
    const currentGroupItems = navigationGroups.get(grpInfo?.groupId || grpInfo?.groupName)?.items || [];
    const localChannelIndex = currentGroupItems.findIndex(item => item.handle === h);
    const switcherLabel = grpInfo ? `${grpInfo.groupName} · ${localChannelIndex + 1}. ${grpInfo.name} (@${h})` : `@${h}`;

    const st = (entry && entry.stats) ? entry.stats : { total: 0, dup: 0, fyp: 0, clean: 0 };
    const fBtn = (mode, label, activeCls) => state.detailFilter === mode
      ? `px-3.5 py-1.5 rounded-xl ${activeCls} text-white text-xs sm:text-[13px] font-extrabold shadow transition whitespace-nowrap`
      : 'px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs sm:text-[13px] font-bold transition whitespace-nowrap';

    let bodyAreaHtml = '';
    if (entry) {
      syncEntryIntoHistoryMemory(entry);
      const allVids = entry.videos || [];
      const shownVids = allVids.filter(v => {
        const dFlag = Boolean(v.is_dup || v.is_crp_punished || v.is_copyright || Number(v.status_code || 0) === 141);
        const fFlag = Boolean(v.is_fyp_restricted || v.shadow || (v.has_penalty && !v.is_crp_punished) || v.is_copyright || Number(v.status_code || 0) === 141);
        if (state.detailFilter === 'dup') return dFlag;
        if (state.detailFilter === 'fyp') return fFlag;
        if (state.detailFilter === 'clean') return !dFlag && !fFlag;
        return true;
      });

      if (!shownVids.length) {
        bodyAreaHtml = `<div class="p-12 text-center text-slate-400 text-sm font-semibold">Không có video nào khớp bộ lọc đang chọn.</div>`;
      } else if (state.detailViewMode === 'grid') {
        bodyAreaHtml = `
          <div class="tt-cg-modal-grid">
            ${shownVids.map(v => buildGridCardHtmlForModal(v, allVids, h)).join('')}
          </div>`;
      } else {
        const tableRows = (typeof buildVideoTableRowHtml === 'function')
          ? shownVids.map((v, idx) => `<tr class="hover:bg-slate-800/45 transition ${v.is_dup ? 'bg-rose-950/15' : ''}">${buildVideoTableRowHtml(v, idx, allVids, '', h)}</tr>`).join('')
          : '';
        bodyAreaHtml = `
          <div class="overflow-x-auto">
            <table class="tt-cg-modal-table w-full text-left text-xs border-collapse">
              <thead class="bg-dark-950/95 text-slate-300 uppercase tracking-wider font-bold border-b border-slate-800 text-[11px] sticky top-0 z-20">
                <tr>
                  <th class="p-3 text-center w-11">#</th>
                  <th class="p-3 text-center w-[78px]">Ảnh bìa</th>
                  <th class="p-3 text-center">Mô tả &amp; Thời gian</th>
                  <th class="p-3 text-center w-24">Lượt xem</th>
                  <th class="p-3 text-center w-36">Tương tác</th>
                  <th class="p-3 text-center w-20">Quốc gia</th>
                  <th class="p-3 text-center px-4">Shadowban</th>
                  <th class="p-3 text-center px-3">TRÙNG LẶP &amp;<br>ÁN PHẠT</th>
                  <th class="p-3 text-center px-4">Aweme ID</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/80 font-medium">${tableRows}</tbody>
            </table>
          </div>`;
      }
    } else if (state.detailLoading) {
      // Kênh chưa từng quét: dựng khung nền mờ 7 hàng để giữ bố cục bảng đầy đặn bên dưới lớp blur
      const ghostRows = Array.from({ length: 7 }, (_, i) => `
        <tr class="border-b border-slate-800/70">
          <td class="p-3 text-center text-slate-600 font-mono font-bold">${i + 1}</td>
          <td class="p-2.5 text-center"><div class="inline-block w-12 h-16 rounded-lg bg-slate-800/80 border border-slate-700/60"></div></td>
          <td class="p-3"><div class="h-3.5 w-64 rounded bg-slate-800/80 mb-2"></div><div class="h-2.5 w-36 rounded bg-slate-800/60"></div></td>
          <td class="p-3 text-center"><div class="h-3.5 w-12 mx-auto rounded bg-slate-800/70"></div></td>
          <td class="p-3 text-center"><div class="h-3.5 w-20 mx-auto rounded bg-slate-800/70"></div></td>
          <td class="p-3 text-center"><div class="h-4 w-10 mx-auto rounded bg-slate-800/70"></div></td>
          <td class="p-3 text-center"><div class="h-7 w-16 mx-auto rounded-lg bg-emerald-950/50 border border-emerald-800/40"></div></td>
          <td class="p-3 text-center"><div class="h-7 w-16 mx-auto rounded-lg bg-slate-800/70"></div></td>
          <td class="p-3 text-center"><div class="h-3.5 w-28 mx-auto rounded bg-slate-800/60"></div></td>
        </tr>`).join('');
      bodyAreaHtml = `
        <div class="overflow-x-auto">
          <table class="tt-cg-modal-table w-full text-left text-xs border-collapse">
            <thead class="bg-dark-950/95 text-slate-300 uppercase tracking-wider font-bold border-b border-slate-800 text-[11px]">
              <tr>
                <th class="p-3 text-center w-11">#</th>
                <th class="p-3 text-center w-[78px]">Ảnh bìa</th>
                <th class="p-3 text-center">Mô tả &amp; Thời gian</th>
                <th class="p-3 text-center w-24">Lượt xem</th>
                <th class="p-3 text-center w-36">Tương tác</th>
                <th class="p-3 text-center w-20">Quốc gia</th>
                <th class="p-3 text-center px-4">Shadowban</th>
                <th class="p-3 text-center px-3">TRÙNG LẶP &amp;<br>ÁN PHẠT</th>
                <th class="p-3 text-center px-4">Aweme ID</th>
              </tr>
            </thead>
            <tbody>${ghostRows}</tbody>
          </table>
        </div>`;
    } else if (hasErr) {
      bodyAreaHtml = `
        <div class="flex-1 flex flex-col items-center justify-center py-16 gap-3 text-center px-4">
          <i class="fa-solid fa-triangle-exclamation text-4xl text-rose-400"></i>
          <div class="text-base font-extrabold text-rose-200">${esc(errEntry.error || 'Không đọc được dữ liệu kênh này.')}</div>
          <button type="button" onclick="openCgChannelModal('${esc(h)}', true)" class="tt-cg-btn tt-cg-btn-selected mt-1">
            <i class="fa-solid fa-rotate text-yellow-200"></i><span>Quét thử lại ngay</span>
          </button>
        </div>`;
    }

    const fullScanTargetLabel = (dispName && dispName.toLowerCase() !== h && dispName.toLowerCase() !== '@' + h)
      ? `${dispName} (@${h})`
      : `@${h}`;
    const scanningOverlayHtml = state.detailLoading ? `
      <div class="tt-cg-scan-overlay-card absolute inset-0 z-30 flex items-center justify-center p-6 bg-slate-950/35">
        <div class="tt-cg-scan-overlay-box px-9 py-6 rounded-3xl flex flex-col items-center text-center gap-3 max-w-3xl">
          <div class="relative w-14 h-14 flex items-center justify-center">
            <span class="absolute inset-0 rounded-full border-4 border-cyan-400/20"></span>
            <i class="fa-solid fa-circle-notch fa-spin text-4xl text-cyan-400 drop-shadow-[0_0_12px_rgba(34,211,238,0.65)]"></i>
          </div>
          <div class="text-lg sm:text-2xl font-extrabold text-white tracking-tight leading-snug">
            Đang quét trực tiếp kênh
            <span class="tt-cg-scan-overlay-handle text-[17px] sm:text-[23px] text-amber-300 font-extrabold break-words">${esc(fullScanTargetLabel)}</span>
          </div>
          <div class="text-sm sm:text-[15px] font-medium text-slate-200">
            Dữ liệu mới sẽ cập nhật ngay trên bảng nổi này, không chuyển khỏi <span class="text-emerald-300 font-extrabold">Check Nhóm Kênh</span>.
          </div>
        </div>
      </div>` : '';

    content.innerHTML = `
      <!-- Header: Hàng 1 gồm Avatar + Thông tin kênh (trái) và Bảng/Lưới + Quét lại + Đóng (phải); Hàng 2 dưới Avatar là bộ lọc -->
      <div class="px-4 py-2.5 border-b border-slate-800 bg-[#0b1329] flex flex-col gap-2 flex-shrink-0">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="tt-cg-detail-identity flex items-center gap-3 min-w-0">
            <img src="${avSrc}" alt="@${esc(h)}" class="w-11 h-11 rounded-2xl object-cover border-2 border-amber-400/75 flex-shrink-0"
                 onerror="this.src='https://ui-avatars.com/api/?name=${encodeURIComponent(h)}&background=0d111a&color=38bdf8&size=96'" />
            <div class="tt-cg-detail-info min-w-0 space-y-0.5">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-extrabold text-[15px] text-amber-300 break-words">${esc(dispName)}</span>
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-[10px] font-bold text-slate-200">${flagHtml}<span>${esc(regCode)}</span></span>
                ${grpInfo ? `<span class="px-2 py-0.5 rounded-md bg-indigo-950 text-indigo-200 border border-indigo-500/50 text-[10px] font-extrabold">${esc(grpInfo.groupName)}</span>` : ''}
                ${navList.length > 1 ? `
                  <div class="tt-cg-channel-nav">
                    <button type="button" onclick="openCgChannelModal('${esc(prevHandle)}', false)" class="px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 text-cyan-300 text-xs font-bold" data-tip="Kênh trước trong danh sách">◀</button>
                    <div class="tt-cg-channel-switcher">
                      <span class="tt-cg-channel-switcher-label" aria-hidden="true">${esc(switcherLabel)}</span>
                      <select onchange="openCgChannelModal(this.value, false)" aria-label="Chọn kênh trong danh sách" title="${esc(switcherLabel)}">${switcherOpts}</select>
                    </div>
                    <button type="button" onclick="openCgChannelModal('${esc(nextHandle)}', false)" class="px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 text-cyan-300 text-xs font-bold" data-tip="Kênh tiếp theo trong danh sách">▶</button>
                  </div>` : ''}
              </div>
              <div class="flex flex-wrap items-center gap-2 text-xs text-slate-300">
                <button type="button" onclick="event.stopPropagation(); copyText('@${esc(h)}')" title="Bấm để sao chép username" class="tt-cg-copy-handle text-cyan-400 hover:text-cyan-200 font-mono font-bold">@${esc(h)} <i class="fa-regular fa-copy" aria-hidden="true"></i></button>
                ${entry ? `<span>•</span><span>Follow: <b class="text-white">${esc(entry.followers_fmt || '0')}</b></span><span>•</span><span>Tạo: <b class="text-white">${esc(entry.create_date || '—')}</b></span>` : ''}
              </div>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            ${entry ? `
              <div class="inline-flex rounded-xl bg-slate-900 border border-slate-700 p-0.5">
                <button type="button" onclick="setCgDetailViewMode('table')" class="px-2.5 py-1 rounded-lg text-xs font-bold ${state.detailViewMode === 'table' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'}" data-tip="Xem dạng bảng chi tiết"><i class="fa-solid fa-table-list text-cyan-200 mr-1"></i>Bảng</button>
                <button type="button" onclick="setCgDetailViewMode('grid')" class="px-2.5 py-1 rounded-lg text-xs font-bold ${state.detailViewMode === 'grid' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'}" data-tip="Xem dạng lưới ảnh bìa như Check Post"><i class="fa-solid fa-grip text-amber-300 mr-1"></i>Lưới</button>
              </div>` : ''}
            <button type="button" onclick="openCgChannelModal('${esc(h)}', true)" ${state.detailLoading ? 'disabled' : ''} class="tt-cg-btn !h-8 !px-3 !text-xs" data-tip="Quét mới lại kênh @${esc(h)}">
              <i class="fa-solid fa-arrows-rotate text-emerald-300 ${state.detailLoading ? 'fa-spin' : ''}"></i><span>Quét lại</span>
            </button>
            <button type="button" onclick="closeCgChannelModal()" class="w-9 h-9 rounded-xl bg-rose-600 hover:bg-rose-500 text-white font-black text-xl leading-none flex items-center justify-center shadow" data-tip="Đóng bảng (ESC)">×</button>
          </div>
        </div>

        ${entry ? `
          <div class="pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-start gap-2">
            <button type="button" onclick="setCgDetailFilter('all')" class="${fBtn('all', 'Tất cả', 'bg-cyan-600')}">Tất cả (${st.total})</button>
            <button type="button" onclick="setCgDetailFilter('dup')" class="${fBtn('dup', 'Trùng', 'bg-rose-600')}">🔴 Trùng (${st.dup})</button>
            <button type="button" onclick="setCgDetailFilter('fyp')" class="${fBtn('fyp', 'Mất FYP', 'bg-amber-600')}">🚨 Mất FYP (${st.fyp})</button>
            <button type="button" onclick="setCgDetailFilter('clean')" class="${fBtn('clean', 'Sạch', 'bg-emerald-600')}">🟢 Sạch (${st.clean})</button>
          </div>` : ''}
      </div>

      <!-- Nội dung cuộn độc lập (giữ nguyên bảng bên dưới và làm mờ khi đang Quét lại) -->
      <div class="relative flex-1 min-h-0 flex flex-col bg-dark-950/60 overflow-hidden">
        <div class="tt-cg-modal-scroll flex-1 min-h-0 ${state.detailLoading ? 'overflow-hidden pointer-events-none select-none tt-cg-blur-underlay' : 'overflow-y-auto'} custom-scrollbar">
          ${bodyAreaHtml}
        </div>
        ${scanningOverlayHtml}
      </div>`;
  }

  window.openCgChannelModal = async function (username, forceRescan, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const h = String(username || '').replace(/^@+/, '').trim().toLowerCase();
    if (!h) return;
    state.detailHandle = h;
    state.detailFilter = 'all';
    const modal = document.getElementById('cgChannelDetailModal');
    if (modal) modal.classList.remove('hidden');

    const existing = findFullResultEntry(h);
    if (existing && !forceRescan) {
      state.detailLoading = false;
      renderCgChannelDetailModal();
      return;
    }

    state.detailLoading = true;
    renderCgChannelDetailModal();
    try {
      const res = await scanSingleChannelHandle(h);
      state.sessionResults.set(h, res);
    } catch (err) {
      state.sessionResults.set(h, {
        success: false,
        username: h,
        error: (err && err.message) || 'Lỗi kết nối khi quét kênh.',
        checkedAt: Math.floor(Date.now() / 1000),
      });
    } finally {
      if (state.detailHandle === h) {
        state.detailLoading = false;
        renderCgChannelDetailModal();
      }
      renderChannelGroupsBoard();
    }
  };
  window.onCgStatusPillClick = window.openCgChannelModal;

  window.closeCgChannelModal = function () {
    state.detailHandle = '';
    state.detailLoading = false;
    document.getElementById('cgChannelDetailModal')?.classList.add('hidden');
  };

  window.setCgDetailFilter = function (f) {
    state.detailFilter = f;
    renderCgChannelDetailModal();
  };

  window.setCgDetailViewMode = function (m) {
    state.detailViewMode = m;
    renderCgChannelDetailModal();
  };

  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') {
      const vidM = document.getElementById('videoModal');
      if (vidM && !vidM.classList.contains('hidden')) return; // let videoModal close first
      const detM = document.getElementById('cgChannelDetailModal');
      if (detM && !detM.classList.contains('hidden')) {
        window.closeCgChannelModal();
        return;
      }
      const setM = document.getElementById('cgSettingsModal');
      if (setM && !setM.classList.contains('hidden')) {
        window.closeCgSettingsModal();
      }
    }
  });

  // ==================== BATCH SCAN ====================
  function focusScanningChannel(handle, preferredGroupIds) {
    const matches = group => (group.channels || []).some(ch => String(ch.username || '').toLowerCase() === handle);
    const group = preferredGroupIds.map(id => state.groups.find(g => g.id === id)).find(g => g && matches(g))
      || state.groups.find(matches);
    state.scanningGroupId = group ? group.id : '';
    if (group) state.selectedTopKey = topKeyFromName(cleanLabel(group.topGroupName, 80) || inferTopGroupName(group.name));
  }

  function revealScanningChannel() {
    const handle = state.scanningNowHandle;
    requestAnimationFrame(() => {
      if (!state.scanning || state.scanningNowHandle !== handle || activeMainWorkspaceTab !== 'channel_groups') return;
      const card = document.querySelector('#channelGroupsBoardMount .tt-cg-item.is-scanning');
      if (!card) return;
      const rect = card.getBoundingClientRect();
      const headerBottom = document.querySelector('header')?.getBoundingClientRect().bottom || 0;
      if (rect.top < headerBottom + 8 || rect.bottom > innerHeight - 16 || rect.left < 8 || rect.right > innerWidth - 8) {
        card.scrollIntoView({ block: 'center', inline: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      }
    });
  }

  async function runBatchScanHandles(handlesList, labelText, preferredGroupIds = []) {
    if (state.scanning) {
      notify('⏳ Đang có lượt quét nhóm kênh chạy, vui lòng đợi hoặc bấm Dừng!');
      return;
    }
    const uniqueHandles = Array.from(new Set((handlesList || []).map(h => String(h || '').toLowerCase()).filter(Boolean)));
    if (!uniqueHandles.length) {
      notify('⚠️ Chưa có kênh nào để quét!');
      return;
    }
    state.scanning = true;
    state.stopRequested = false;
    state.scanProgress = { done: 0, total: uniqueHandles.length, bad: 0, clean: 0, err: 0, label: labelText || '' };
    renderChannelGroupsBoard();

    for (const h of uniqueHandles) {
      if (state.stopRequested) break;
      state.scanningNowHandle = h;
      focusScanningChannel(h, preferredGroupIds);
      renderChannelGroupsBoard();
      revealScanningChannel();
      try {
        const res = await scanSingleChannelHandle(h);
        state.sessionResults.set(h, res);
        state.scanProgress.done += 1;
        if (!res.success) state.scanProgress.err += 1;
        else if (res.stats.dup > 0 || res.stats.fyp > 0) state.scanProgress.bad += 1;
        else state.scanProgress.clean += 1;
      } catch (err) {
        state.sessionResults.set(h, {
          success: false,
          username: h,
          error: (err && err.message) || 'Lỗi kết nối khi quét kênh',
          checkedAt: Math.floor(Date.now() / 1000),
        });
        state.scanProgress.done += 1;
        state.scanProgress.err += 1;
      }
      if (state.detailHandle === h) renderCgChannelDetailModal();
      renderChannelGroupsBoard();
    }

    const stopped = state.stopRequested;
    state.scanning = false;
    state.stopRequested = false;
    state.scanningNowHandle = '';
    state.scanningGroupId = '';
    renderChannelGroupsBoard();

    if (typeof loadCheckHistory === 'function') {
      loadCheckHistory(false);
    }
    const p = state.scanProgress;
    notify(
      stopped
        ? `⏹ Đã dừng quét (${p.done}/${p.total} kênh · Có vấn đề: ${p.bad} · Sạch: ${p.clean})`
        : `✅ Quét xong ${p.done}/${p.total} kênh! (Có Trùng/Mất FYP: ${p.bad} · Sạch: ${p.clean}${p.err ? ' · Lỗi: ' + p.err : ''}) — Click vào ô kênh bất kỳ để mở bảng xem!`
    );
  }

  window.stopCgBatchScan = function () {
    if (!state.scanning) return;
    state.stopRequested = true;
    renderChannelGroupsBoard();
  };

  window.scanCgSelected = function () {
    const sel = getSelectedChannelObjects();
    if (!sel.length) {
      notify('⚠️ Bấm ô tick góc trái thẻ kênh (hoặc Chọn nhóm) rồi bấm Check đã chọn!');
      return;
    }
    runBatchScanHandles(sel.map(x => x.channel.username), `Đã chọn (${sel.length} kênh)`, sel.map(x => x.group.id));
  };

  window.scanCgGroup = function (groupId, ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    }
    const grp = state.groups.find(g => g.id === groupId);
    if (!grp || !(grp.channels || []).length) {
      notify('⚠️ Nhóm này chưa có kênh nào.');
      return;
    }
    runBatchScanHandles(grp.channels.map(c => c.username), `Nhóm ${grp.name}`, [grp.id]);
  };

  window.scanCgCurrentTab = function () {
    const topTabs = deriveTopTabs();
    const vis = getVisibleGroups(topTabs);
    const activeTab = topTabs.find(t => t.key === state.selectedTopKey);
    const handles = [];
    for (const g of vis) {
      for (const ch of (g.channels || [])) handles.push(ch.username);
    }
    runBatchScanHandles(handles, activeTab ? `Tab ${activeTab.name}` : 'Tab hiện tại', vis.map(g => g.id));
  };

  window.scanCgAllGroups = function () {
    const handles = [];
    for (const g of state.groups) {
      for (const ch of (g.channels || [])) handles.push(ch.username);
    }
    runBatchScanHandles(handles, 'Toàn bộ danh sách');
  };

  window.setCgResultFilter = function (f) {
    state.resultFilter = f;
    renderBatchResultsPanel();
  };

  // ==================== DRAG & DROP REORDERING ====================
  function setupDragHandlers(root) {
    root.querySelectorAll('.tt-cg-top-tab').forEach(tab => {
      tab.addEventListener('dragstart', ev => {
        state.dragTopKey = tab.dataset.topKey || '';
        if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move';
      });
      tab.addEventListener('dragend', () => {
        state.dragTopKey = '';
        root.querySelectorAll('.tt-cg-top-tab').forEach(el => el.classList.remove('drag-before', 'drag-after'));
      });
      tab.addEventListener('dragover', ev => {
        if (!state.dragTopKey || state.dragTopKey === tab.dataset.topKey) return;
        ev.preventDefault();
        const rect = tab.getBoundingClientRect();
        const after = ev.clientX >= rect.left + rect.width / 2;
        tab.classList.toggle('drag-before', !after);
        tab.classList.toggle('drag-after', after);
      });
      tab.addEventListener('dragleave', () => tab.classList.remove('drag-before', 'drag-after'));
      tab.addEventListener('drop', async ev => {
        if (!state.dragTopKey || state.dragTopKey === tab.dataset.topKey) return;
        ev.preventDefault();
        const rect = tab.getBoundingClientRect();
        const after = ev.clientX >= rect.left + rect.width / 2;
        const tops = deriveTopTabs().map(t => t.key);
        const fromIdx = tops.indexOf(state.dragTopKey);
        if (fromIdx < 0) return;
        tops.splice(fromIdx, 1);
        let toIdx = tops.indexOf(tab.dataset.topKey);
        if (toIdx < 0) return;
        if (after) toIdx += 1;
        tops.splice(toIdx, 0, state.dragTopKey);
        state.topTabOrder = tops;
        state.dragTopKey = '';
        await persistChannelGroups(false);
        renderChannelGroupsBoard();
      });
    });

    root.querySelectorAll('.tt-cg-group').forEach(sec => {
      const gid = sec.dataset.groupId;
      const handle = sec.querySelector('.tt-cg-drag-handle');
      if (handle) {
        handle.addEventListener('dragstart', ev => {
          state.dragGroupId = gid;
          if (ev.dataTransfer) {
            ev.dataTransfer.effectAllowed = 'move';
            ev.dataTransfer.setData('text/plain', gid);
          }
        });
        handle.addEventListener('dragend', () => {
          state.dragGroupId = '';
          root.querySelectorAll('.tt-cg-group').forEach(g => g.classList.remove('drag-over-top', 'drag-over-bottom', 'drag-over-left', 'drag-over-right'));
        });
      }
      sec.addEventListener('dragover', ev => {
        if (state.dragChannelInfo) {
          ev.preventDefault();
          return;
        }
        if (!state.dragGroupId || state.dragGroupId === gid) return;
        ev.preventDefault();
        const rect = sec.getBoundingClientRect();
        const relY = (ev.clientY - rect.top) / Math.max(1, rect.height);
        sec.classList.remove('drag-over-top', 'drag-over-bottom', 'drag-over-left', 'drag-over-right');
        if (relY < 0.5) sec.classList.add('drag-over-top');
        else sec.classList.add('drag-over-bottom');
      });
      sec.addEventListener('dragleave', ev => {
        if (!sec.contains(ev.relatedTarget)) {
          sec.classList.remove('drag-over-top', 'drag-over-bottom', 'drag-over-left', 'drag-over-right');
        }
      });
      sec.addEventListener('drop', async ev => {
        if (state.dragChannelInfo && !ev.defaultPrevented) {
          ev.preventDefault();
          const { groupId: srcGid, channelId: srcCid } = state.dragChannelInfo;
          state.dragChannelInfo = null;
          const srcG = state.groups.find(g => g.id === srcGid);
          const dstG = state.groups.find(g => g.id === gid);
          if (!srcG || !dstG) return;
          const cIdx = srcG.channels.findIndex(c => c.id === srcCid);
          if (cIdx < 0) return;
          const [moved] = srcG.channels.splice(cIdx, 1);
          dstG.channels.push(moved);
          await persistChannelGroups(false);
          renderChannelGroupsBoard();
          return;
        }
        if (!state.dragGroupId || state.dragGroupId === gid) return;
        ev.preventDefault();
        const srcId = state.dragGroupId;
        state.dragGroupId = '';
        const rect = sec.getBoundingClientRect();
        const relY = (ev.clientY - rect.top) / Math.max(1, rect.height);
        sec.classList.remove('drag-over-top', 'drag-over-bottom', 'drag-over-left', 'drag-over-right');

        const srcIdx = state.groups.findIndex(g => g.id === srcId);
        const dstIdx = state.groups.findIndex(g => g.id === gid);
        if (srcIdx < 0 || dstIdx < 0) return;
        const [srcGrp] = state.groups.splice(srcIdx, 1);
        let insertIdx = state.groups.findIndex(g => g.id === gid);
        if (relY >= 0.5) insertIdx += 1;
        state.groups.splice(insertIdx, 0, srcGrp);
        await persistChannelGroups(false);
        renderChannelGroupsBoard();
      });
    });

    root.querySelectorAll('.tt-cg-item').forEach(card => {
      const gid = card.dataset.groupId;
      const cid = card.dataset.channelId;
      card.addEventListener('dragstart', ev => {
        state.dragChannelInfo = { groupId: gid, channelId: cid };
        if (ev.dataTransfer) {
          ev.dataTransfer.effectAllowed = 'move';
          ev.dataTransfer.setData('text/plain', cid);
        }
      });
      card.addEventListener('dragend', () => {
        state.dragChannelInfo = null;
        root.querySelectorAll('.tt-cg-item').forEach(c => c.classList.remove('drag-before', 'drag-after'));
      });
      card.addEventListener('dragover', ev => {
        if (!state.dragChannelInfo) return;
        if (state.dragChannelInfo.groupId === gid && state.dragChannelInfo.channelId === cid) return;
        ev.preventDefault();
        ev.stopPropagation();
        const rect = card.getBoundingClientRect();
        const after = ev.clientX >= rect.left + rect.width / 2;
        card.classList.toggle('drag-before', !after);
        card.classList.toggle('drag-after', after);
      });
      card.addEventListener('dragleave', () => card.classList.remove('drag-before', 'drag-after'));
      card.addEventListener('drop', async ev => {
        if (!state.dragChannelInfo) return;
        ev.preventDefault();
        ev.stopPropagation();
        const { groupId: srcGid, channelId: srcCid } = state.dragChannelInfo;
        state.dragChannelInfo = null;
        if (srcGid === gid && srcCid === cid) return;
        const rect = card.getBoundingClientRect();
        const after = ev.clientX >= rect.left + rect.width / 2;
        const srcG = state.groups.find(g => g.id === srcGid);
        const dstG = state.groups.find(g => g.id === gid);
        if (!srcG || !dstG) return;
        const sIdx = srcG.channels.findIndex(c => c.id === srcCid);
        if (sIdx < 0) return;
        const [moved] = srcG.channels.splice(sIdx, 1);
        let dIdx = dstG.channels.findIndex(c => c.id === cid);
        if (dIdx < 0) dstG.channels.push(moved);
        else {
          if (after) dIdx += 1;
          dstG.channels.splice(dIdx, 0, moved);
        }
        await persistChannelGroups(false);
        renderChannelGroupsBoard();
      });
    });
  }

  // ==================== COMPACT BATCH SUMMARY REMOVED PER USER PREFERENCE ====================
  function renderBatchResultsPanel() {
    const host = document.getElementById('cgBatchResultsSection');
    if (host) {
      host.classList.add('hidden');
      host.innerHTML = '';
    }
  }

  // ==================== MAIN BOARD RENDER ====================
  function renderChannelGroupsBoard() {
    const root = document.getElementById('channelGroupsBoardMount');
    if (!root) return;
    if (!canUseChannelGroupsBoard()) {
      const host = document.getElementById('cgBatchResultsSection');
      if (host) {
        host.classList.add('hidden');
        host.innerHTML = '';
      }
      root.innerHTML = `
        <div class="tt-cg-board-card">
          <div class="tt-cg-header">
            <div class="tt-cg-title">Danh sách nhóm kênh</div>
            <div class="tt-cg-toolbar">
              <button type="button" onclick="if(typeof openVipModal==='function') openVipModal(true, 'register')" class="tt-cg-btn tt-cg-btn-add">
                <i class="fa-solid fa-right-to-bracket text-yellow-200"></i><span>Đăng nhập tài khoản</span>
              </button>
            </div>
          </div>
          <div class="tt-cg-empty py-12 space-y-3">
            <div class="text-base text-amber-300 font-extrabold">Đăng nhập tài khoản để tạo &amp; lưu nhóm kênh TikTok riêng của bạn</div>
            <div class="text-xs text-slate-300">Mỗi tài khoản có một bảng danh sách nhóm kênh riêng biệt để Check Trùng &amp; FYP hàng loạt nhanh chóng.</div>
            <div class="pt-1">
              <button type="button" onclick="if(typeof openVipModal==='function') openVipModal(true, 'register')" class="tt-cg-btn tt-cg-btn-add">
                <i class="fa-solid fa-user-check text-yellow-200"></i><span>Đăng nhập / Đăng ký ngay</span>
              </button>
            </div>
          </div>
        </div>`;
      return;
    }
    pruneSelections();
    const topTabs = deriveTopTabs();
    const visGroups = getVisibleGroups(topTabs);
    const totalGroups = state.groups.length;
    const totalCh = totalChannelsCount();
    const selList = getSelectedChannelObjects();
    const activeTabObj = topTabs.find(t => t.key === state.selectedTopKey);
    const tabChCount = activeTabObj ? activeTabObj.channelCount : 0;

    let tabAllSelected = false;
    if (visGroups.length) {
      const tabKeys = [];
      for (const g of visGroups) {
        for (const c of (g.channels || [])) tabKeys.push(selectionKey(g.id, c.username));
      }
      tabAllSelected = tabKeys.length > 0 && tabKeys.every(k => state.selectedHandles.has(k));
    }

    const p = state.scanProgress;
    const pct = p.total > 0 ? Math.round((p.done / p.total) * 100) : 0;
    const progressHtml = `<div class="tt-cg-progress-slot" aria-live="polite">${state.scanning ? `
      <div class="tt-cg-inline-progress" title="Đang quét ${esc(p.label)}: ${p.done}/${p.total} kênh${state.scanningNowHandle ? ' · @' + esc(state.scanningNowHandle) : ''}">
        <span class="tt-cg-progress-text"><i class="fa-solid fa-spinner fa-spin"></i> <b>${p.done}/${p.total}</b> kênh${state.scanningNowHandle ? ` · <span class="text-amber-300">@${esc(state.scanningNowHandle)}</span>` : ''}</span>
        <span class="tt-cg-progress-stats"><span class="text-rose-300">Có vấn đề: ${p.bad}</span> · <span class="text-emerald-300">Sạch: ${p.clean}</span>${p.err ? ` · <span class="text-amber-300">Lỗi: ${p.err}</span>` : ''}</span>
        <button type="button" onclick="stopCgBatchScan()" class="tt-cg-btn tt-cg-btn-stop"><i class="fa-solid fa-stop"></i><span>Dừng</span></button>
      </div>
      <div class="tt-cg-progress-track" role="progressbar" aria-label="Tiến trình quét nhóm kênh" aria-valuemin="0" aria-valuemax="${p.total}" aria-valuenow="${p.done}"><div style="width:${pct}%"></div></div>
    ` : ''}</div>`;

    const topStripHtml = topTabs.length ? `
      <div class="tt-cg-filter-strip" role="tablist">
        ${topTabs.map(tab => `
          <button type="button" draggable="true" data-top-key="${esc(tab.key)}"
                  onclick="selectCgTopTab('${esc(tab.key)}')"
                  data-tip="Tab ${esc(tab.name)}: ${tab.groupCount} nhóm · ${tab.channelCount} kênh (kéo trái/phải để đổi vị trí)"
                  class="tt-cg-top-tab ${tab.key === state.selectedTopKey ? 'is-active' : ''}">
            <span class="tt-cg-top-tab-name">${esc(tab.name)}</span>
            <span class="tt-cg-top-tab-count">${tab.groupCount}</span>
          </button>
        `).join('')}
      </div>` : '';

    const renderGroupSectionHtml = (grp, gIdx) => {
      const colNum = Math.floor(gIdx / 4) + 1;
      const rowNum = (gIdx % 4) + 1;
      const chs = grp.channels || [];
      const grpAllSel = chs.length > 0 && chs.every(c => state.selectedHandles.has(selectionKey(grp.id, c.username)));
      const cardsHtml = chs.length ? chs.map((ch, idx) => {
        const h = String(ch.username || '').toLowerCase();
        const isSel = state.selectedHandles.has(selectionKey(grp.id, h));
        const isScanningThis = state.scanning && state.scanningNowHandle === h && state.scanningGroupId === grp.id;
        const sum = getChannelSummary(ch);
        let pillCls = 'tt-cg-status-pill';
        let pillText = '⚡ Mở & Check';
        let pillTip = 'Click vào ô kênh để mở bảng nổi Check Trùng & FYP ngay tại đây';
        if (isScanningThis) {
          pillCls += ' is-scanning';
          pillText = '⏳ Đang quét...';
        } else if (sum.state === 'err') {
          pillCls += ' is-err';
          pillText = '⚠️ Lỗi · Thử lại';
          pillTip = sum.error || 'Quét lỗi — click để quét lại';
        } else if (sum.state === 'ok') {
          if (sum.dedupLocked) {
            pillCls += sum.fyp > 0 ? ' is-fyp' : '';
            pillText = sum.fyp > 0 ? `🚨 Mất FYP ${sum.fyp} · 👑 VIP` : `👑 Cần VIP xem Trùng`;
          } else if (sum.dup > 0 && sum.fyp > 0) {
            pillCls += ' is-bad';
            pillText = `🔴 Trùng ${sum.dup} · 🚨 ${sum.fyp}`;
          } else if (sum.dup > 0) {
            pillCls += ' is-bad';
            pillText = `🔴 Trùng ${sum.dup}/${sum.total}`;
          } else if (sum.fyp > 0) {
            pillCls += ' is-fyp';
            pillText = `🚨 Mất FYP ${sum.fyp}/${sum.total}`;
          } else {
            pillCls += ' is-clean';
            pillText = `✅ Sạch (${sum.total})`;
          }
          pillTip = 'Click vào ô để mở bảng nổi xem video (Shift + Click để quét mới lại)';
        }

        const avImg = ch.avatarUrl
          ? ((typeof getProxiedImage === 'function') ? getProxiedImage(ch.avatarUrl, ch.name || h, '') : ch.avatarUrl)
          : '';
        const initial = String(ch.name || h || '?').replace(/^@/, '').trim().charAt(0).toUpperCase() || '?';

        return `
          <div class="tt-cg-item ${isSel ? 'is-selected' : ''} ${isScanningThis ? 'is-scanning' : ''}"
               draggable="true"
               data-group-id="${esc(grp.id)}"
               data-channel-id="${esc(ch.id)}"
               onclick="onCgCardClick('${esc(grp.id)}', '${esc(h)}', event)"
               data-tip="Bấm mở bảng nổi xem video (@${esc(h)})&#10;💡 Ô vuông góc trái: chọn quét hàng loạt">
            <div class="tt-cg-item-top">
              <div class="tt-cg-select-wrap">
                <button type="button" class="tt-cg-select-chk"
                        onclick="toggleCgCardSelect('${esc(grp.id)}', '${esc(h)}', event)"
                        data-tip="${isSel ? 'Bỏ chọn kênh này' : 'Tick chọn kênh này để Check hàng loạt'}">
                  <i class="fa-solid fa-check"></i>
                </button>
                <span class="tt-cg-order">${idx + 1}</span>
              </div>
              <div class="tt-cg-avatar">
                ${avImg ? `<img src="${avImg}" alt="@${esc(h)}" onerror="this.remove();this.parentElement.textContent='${esc(initial)}'" />` : esc(initial)}
              </div>
              <button type="button" class="tt-cg-remove" data-tip="Xóa @${esc(h)} khỏi nhóm"
                      onclick="removeCgChannel('${esc(grp.id)}', '${esc(ch.id)}', event)">×</button>
            </div>
            <div class="tt-cg-item-footer">
              <strong class="tt-cg-name">${esc(ch.name || '@' + h)}</strong>
              <span class="${pillCls}" data-tip="${esc(pillTip)}">${esc(pillText)}</span>
            </div>
          </div>`;
      }).join('') : `<div class="tt-cg-empty col-span-full !py-4 !text-xs">Nhóm trống · <button type="button" onclick="openCgSettingsModal('${esc(grp.id)}', event)" class="text-emerald-300 underline font-extrabold">Bấm thêm kênh vào nhóm</button></div>`;

      return `
        <section class="tt-cg-group" data-group-id="${esc(grp.id)}" style="--cg-col:${colNum};--cg-row:${rowNum};">
          <div class="tt-cg-group-head">
            <span class="tt-cg-drag-handle" draggable="true" data-tip="Giữ &amp; kéo ☰ lên/xuống để đổi thứ tự nhóm (xếp dọc 4 hàng rồi sang cột tiếp theo)">☰</span>
            <span class="tt-cg-group-name">${esc(grp.name)} (${chs.length})</span>
            <div class="tt-cg-group-actions">
              <button type="button" class="tt-cg-grp-mini-btn select" onclick="toggleCgSelectGroup('${esc(grp.id)}', event)" data-tip="Chọn / Bỏ chọn toàn bộ kênh trong nhóm ${esc(grp.name)}">
                <i class="fa-solid ${grpAllSel ? 'fa-circle-xmark text-amber-300' : 'fa-list-check text-sky-300'}"></i>
                <span>${grpAllSel ? 'Bỏ chọn' : 'Chọn nhóm'}</span>
              </button>
              <button type="button" class="tt-cg-grp-mini-btn scan" onclick="scanCgGroup('${esc(grp.id)}', event)" ${state.scanning || !chs.length ? 'disabled' : ''} data-tip="Check Trùng &amp; FYP toàn bộ kênh trong nhóm ${esc(grp.name)}">
                <i class="fa-solid fa-bolt text-yellow-300"></i>
                <span>Check nhóm</span>
              </button>
              <button type="button" class="tt-cg-grp-icon-btn rename" onclick="openCgEditGroupModal('${esc(grp.id)}', event)" data-tip="Đổi tên nhóm / chọn Tab hiển thị"><i class="fa-solid fa-pen-to-square text-amber-200 text-[10px]"></i></button>
              <button type="button" class="tt-cg-grp-icon-btn delete" onclick="deleteCgGroup('${esc(grp.id)}', event)" data-tip="Xóa nhóm ${esc(grp.name)}">×</button>
            </div>
          </div>
          <div class="tt-cg-grid">${cardsHtml}</div>
        </section>`;
    };

    // Dùng chung CSS Grid (grid-auto-flow: column + grid-template-rows: repeat(nRows, auto)) để các hàng US1-US5-US9-US13 thẳng hàng ngang tuyệt đối
    let boardColsClass = 'cols-1';
    let nCols = 0;
    let bodyHtml = '';
    if (visGroups.length) {
      nCols = Math.ceil(visGroups.length / 4);
      const nRows = Math.min(4, visGroups.length);
      boardColsClass = nCols <= 4 ? `cols-${nCols}` : 'cols-many';
      const stripCls = nCols <= 4 ? `has-${nCols}-cols` : 'has-many-cols';
      bodyHtml = `
        <div class="tt-cg-columns-strip rows-${nRows} ${stripCls}">
          ${visGroups.map((grp, gIdx) => renderGroupSectionHtml(grp, gIdx)).join('')}
        </div>`;
    } else {
      bodyHtml = `
        <div class="tt-cg-empty py-10 space-y-3">
          <div class="text-base text-amber-300 font-extrabold">Chưa có nhóm kênh TikTok nào được lưu.</div>
          <div class="text-xs text-slate-300">Bấm nút <button type="button" onclick="openCgSettingsModal()" class="text-emerald-300 underline font-extrabold">⚙ Cài đặt</button> ở góc phải trên để tạo nhóm &amp; thêm kênh.</div>
        </div>`;
    }
    state.currentBoardCols = nCols;
    syncCgSupportWidgetVisibility();

    root.innerHTML = `
      <div class="tt-cg-board-card ${boardColsClass}">
        <div class="tt-cg-header">
          <div class="tt-cg-title">Danh sách nhóm kênh</div>
          <span class="tt-cg-count-pill">
            <span class="tt-cg-count-groups">${totalGroups} nhóm</span>
            <span class="tt-cg-count-sep">-</span>
            <span class="tt-cg-count-channels">${totalCh} kênh</span>
          </span>
          ${progressHtml}
          <div class="tt-cg-toolbar">
            ${tabChCount > 0 ? `
              <button type="button" onclick="toggleCgSelectCurrentTab()" class="tt-cg-btn" data-tip="${tabAllSelected ? 'Bỏ đánh dấu toàn bộ kênh trong tab này' : `Chọn toàn bộ ${tabChCount} kênh trong tab ${esc(activeTabObj ? activeTabObj.name : '')}`}">
                <i class="fa-solid ${tabAllSelected ? 'fa-circle-xmark text-amber-300' : 'fa-list-check text-cyan-300'}"></i>
                <span>${tabAllSelected ? 'Bỏ chọn tab' : `Chọn cả tab (${tabChCount})`}</span>
              </button>` : ''}
            ${selList.length > 0 ? `
              <button type="button" onclick="clearCgSelection()" class="tt-cg-btn" data-tip="Xóa toàn bộ đánh dấu đang chọn">
                <i class="fa-solid fa-rotate-left text-rose-300"></i>
                <span>Bỏ chọn (${selList.length})</span>
              </button>` : ''}
            <button type="button" onclick="scanCgSelected()" class="tt-cg-btn tt-cg-btn-selected" ${state.scanning || selList.length === 0 ? 'disabled' : ''} data-tip="Bấm ô tick góc trái các thẻ kênh rồi bấm nút này để quét">
              <i class="fa-solid fa-check-double text-emerald-300"></i><span>Check đã chọn (${selList.length})</span>
            </button>
            <button type="button" onclick="scanCgCurrentTab()" class="tt-cg-btn tt-cg-btn-tabscan" ${state.scanning || tabChCount === 0 ? 'disabled' : ''} data-tip="Quét toàn bộ kênh trong tab ${esc(activeTabObj ? activeTabObj.name : '')}">
              <i class="fa-solid fa-bolt text-yellow-300"></i><span>Check tab ${esc(activeTabObj ? activeTabObj.name : '')} (${tabChCount})</span>
            </button>
            <button type="button" onclick="scanCgAllGroups()" class="tt-cg-btn tt-cg-btn-allscan" ${state.scanning || totalCh === 0 ? 'disabled' : ''} data-tip="Quét toàn bộ ${totalCh} kênh trong tất cả các nhóm">
              <i class="fa-solid fa-rocket text-amber-200"></i><span>Check tất cả (${totalCh})</span>
            </button>
            <button type="button" onclick="openCgSettingsModal()" class="tt-cg-btn tt-cg-btn-add" data-tip="Mở Cài đặt nhóm, thêm kênh TikTok &amp; Xuất/Nhập JSON">
              <i class="fa-solid fa-sliders text-yellow-200"></i><span>Cài đặt</span>
            </button>
          </div>
        </div>
        ${topStripHtml}
        <div class="tt-cg-body">${bodyHtml}</div>
      </div>`;

    syncCgSettingsModalUI();
    setupDragHandlers(root);
    renderBatchResultsPanel();
    syncCgSupportWidgetVisibility();
  }
  window.renderChannelGroupsBoard = renderChannelGroupsBoard;

  function syncCgSupportWidgetVisibility() {
    const widget = document.getElementById('floatingSupportWidget');
    if (!widget) return;
    const onCgTab = (typeof activeMainWorkspaceTab !== 'undefined') && activeMainWorkspaceTab === 'channel_groups';
    const hideForCol4 = Boolean(onCgTab && canUseChannelGroupsBoard() && Number(state.currentBoardCols || 0) >= 4);
    widget.classList.toggle('hidden', hideForCol4);
  }
  window.syncCgSupportWidgetVisibility = syncCgSupportWidgetVisibility;
})();
