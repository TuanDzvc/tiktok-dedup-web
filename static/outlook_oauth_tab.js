/* Strict tuandzvcc-only Outlook OAuth2 Web Manager on checktiktok.site */
(() => {
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const byId = (id) => document.getElementById(id);

    function isTuandzvcc() {
        const u = (typeof currentVipUser !== 'undefined' && currentVipUser) ? currentVipUser : window.currentVipUser;
        return Boolean(u && String(u.username || '').trim().toLowerCase() === 'tuandzvcc');
    }

    function authHeaders() {
        const tok = typeof getVipToken === 'function' ? getVipToken() : (localStorage.getItem('tikflow_vip_token') || '');
        return {
            'Authorization': 'Bearer ' + tok,
            'Content-Type': 'application/json',
        };
    }

    // Inject Navigation Tab Button (hidden by default; shown ONLY for tuandzvcc)
    const tabsWrap = byId('mainWorkspaceTabs')?.firstElementChild;
    if (tabsWrap && !byId('tabMainOutlookMail')) {
        const tabBtn = document.createElement('button');
        tabBtn.id = 'tabMainOutlookMail';
        tabBtn.type = 'button';
        tabBtn.className = 'hidden px-5 py-2.5 rounded-xl bg-transparent hover:bg-slate-800/90 text-slate-300 hover:text-white font-bold text-xs sm:text-sm flex items-center gap-2 transition whitespace-nowrap';
        tabBtn.setAttribute('data-tip', 'Đăng nhập Mail trên máy khách, lưu OAuth2 và đọc thư/code trên web (riêng tuandzvcc)');
        tabBtn.innerHTML = `
            <i class="fa-solid fa-envelope-open-text text-sky-400"></i>
            <span>Quản Lý Mail</span>
            <span id="outlookMailNavCount" class="hidden px-1.5 py-0.5 rounded-md bg-emerald-950 border border-emerald-500/40 text-emerald-300 font-mono text-[11px] font-extrabold tabular-nums">0</span>
        `;
        tabBtn.onclick = () => window.switchMainWorkspaceTab('outlook_mail');
        tabsWrap.appendChild(tabBtn);
    }

    // Inject Workspace Section
    const mainEl = document.querySelector('main');
    if (mainEl && !byId('outlookMailWorkspace')) {
        const sec = document.createElement('section');
        sec.id = 'outlookMailWorkspace';
        sec.className = 'hidden mt-5 pt-1';
        sec.style.cssText = 'width: min(98.8vw, 1900px); margin-left: 50%; transform: translateX(-50%);';
        sec.innerHTML = `
            <div class="rounded-2xl border border-slate-700/80 bg-[#16181d] p-4 sm:p-5 shadow-2xl text-slate-200">
                <div class="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-sky-700/50 bg-sky-950/20 p-3">
                    <button type="button" id="omDownloadWorker" class="rounded-lg bg-gradient-to-r from-cyan-500 to-blue-600 px-3 py-2 text-xs font-bold text-white">⬇ Tải worker máy khách (.bat)</button>
                    <select id="omWorkerSelect" aria-label="Máy chạy trình duyệt Mail" class="rounded-lg border border-slate-600 bg-slate-950 px-3 py-2 text-xs text-slate-200"><option value="">Chọn máy chạy</option></select>
                    <button type="button" id="omRevokeWorker" class="rounded-lg border border-rose-500/50 bg-rose-950 px-3 py-2 text-xs text-rose-200">Ngắt worker</button>
                    <span id="omWorkerStatus" class="text-xs text-slate-300">Mở .bat trong máy khách/máy ảo muốn chạy. Server không mở Chrome.</span>
                </div>
                <div class="grid grid-cols-1 xl:grid-cols-[330px_minmax(0,1fr)] gap-4 items-stretch">
                    <!-- LEFT SIDEBAR (stretches full height to match right workspace) -->
                    <div class="flex flex-col gap-3 h-full">
                        <div class="flex flex-col flex-1 min-h-0 gap-1.5">
                            <div class="flex items-center justify-between">
                                <label class="text-xs font-extrabold text-slate-100">Danh sách Mail (mail|pass hoặc mail|pass|kp)</label>
                                <span id="omInputMailCount" class="text-[11px] font-mono text-cyan-300 font-bold">0 dòng</span>
                            </div>
                            <textarea id="omMailInput" spellcheck="false" autocomplete="off"
                                class="w-full flex-1 min-h-[260px] rounded-lg border border-slate-700 bg-[#0e1014] px-3 py-2 font-mono text-xs text-slate-200 outline-none focus:border-blue-500 resize-none"
                                placeholder="mail@example.com|password&#10;mail@example.com|password|recovery@example.com&#10;(Hỗ trợ dán chuỗi có sẵn OAuth2Token)"></textarea>
                            <div class="flex items-center justify-end gap-2 pt-0.5">
                                <button type="button" id="omBtnClearMailInput" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 text-xs font-bold text-slate-300 transition">Xóa ô</button>
                                <button type="button" id="omBtnImportMails" class="px-3.5 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 border border-blue-400/50 text-xs font-extrabold text-white shadow transition">Nạp vào Bảng</button>
                            </div>
                        </div>

                        <div class="space-y-2 pt-2.5 border-t border-slate-800 flex-shrink-0">
                            <div class="flex items-center justify-between">
                                <label class="text-xs font-extrabold text-slate-100">Proxy (IP:PORT hoặc IP:PORT:USER:PASS)</label>
                                <span id="omInputProxyCount" class="text-[11px] font-mono text-amber-300 font-bold">0 proxy</span>
                            </div>
                            <div class="flex flex-wrap items-center gap-2.5 text-xs">
                                <label class="flex items-center gap-1.5 text-slate-300 font-semibold">
                                    <span>Số luồng:</span>
                                    <input id="omSpinThreads" type="number" min="1" max="12" value="3" class="w-14 rounded border border-slate-600 bg-[#1e2129] px-2 py-1 text-center font-mono font-bold text-white outline-none focus:border-blue-500">
                                </label>
                                <label class="flex items-center gap-1.5 text-slate-300 font-semibold">
                                    <span>Timeout(s):</span>
                                    <input id="omSpinTimeout" type="number" min="60" max="600" step="30" value="240" class="w-16 rounded border border-slate-600 bg-[#1e2129] px-2 py-1 text-center font-mono font-bold text-white outline-none focus:border-blue-500">
                                </label>
                            </div>
                            <p class="text-[11px] text-slate-300">Trình duyệt luôn hiện trên máy khách đã chọn. CMD chạy ẩn.</p>
                            <textarea id="omProxyInput" rows="6" spellcheck="false" autocomplete="off"
                                class="w-full rounded-lg border border-slate-700 bg-[#0e1014] px-3 py-2 font-mono text-xs text-slate-200 outline-none focus:border-blue-500 resize-y"
                                placeholder="103.11.22.33:8080:user:pass&#10;45.67.89.10:3128&#10;(Để trống nếu chạy mạng trực tiếp)"></textarea>
                            <div class="flex items-center justify-end">
                                <button type="button" id="omBtnApplyProxy" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 text-xs font-bold text-slate-200 transition">Gán Proxy vào Bảng</button>
                            </div>
                        </div>
                    </div>

                    <!-- RIGHT MAIN WORKSPACE -->
                    <div class="flex flex-col gap-2.5 min-w-0">
                        <!-- Top Toolbar -->
                        <div class="flex flex-wrap items-center justify-between gap-2">
                            <div class="flex flex-wrap items-center gap-1.5">
                                <span class="text-xs font-bold text-slate-300">Quản lý Thư mục:</span>
                                <select id="omFolderSelect" class="rounded-lg border border-slate-600 bg-[#22252e] px-2.5 py-1.5 text-xs font-bold text-white outline-none focus:border-blue-500 min-w-[130px]"></select>
                                <button type="button" id="omBtnFolderAdd" class="px-2.5 py-1.5 rounded-lg bg-teal-600 hover:bg-teal-500 text-white text-xs font-extrabold transition">Thêm</button>
                                <button type="button" id="omBtnFolderRename" class="px-2.5 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-xs font-extrabold transition">Sửa</button>
                                <button type="button" id="omBtnFolderDel" class="px-2.5 py-1.5 rounded-lg bg-rose-500 hover:bg-rose-400 text-white text-xs font-extrabold transition">Xóa</button>

                                <span class="mx-1 h-4 w-px bg-slate-700"></span>
                                <button type="button" id="omBtnCheckAll" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-slate-600 text-xs font-bold text-slate-200 transition">Chọn hết</button>
                                <button type="button" id="omBtnUncheckAll" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-slate-600 text-xs font-bold text-slate-200 transition">Bỏ chọn</button>
                                <button type="button" id="omBtnCopyTikFlow" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-cyan-500/40 text-xs font-bold text-cyan-300 transition" data-tip="Copy các dòng đã tích chọn theo định dạng mail|pass|oauth2_token|client_id để dán thẳng vào TikFlow">Copy chuẩn TikFlow</button>
                                <button type="button" id="omBtnExportTxt" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-slate-600 text-xs font-bold text-slate-200 transition" data-tip="Xuất các dòng đã tích chọn ra file .TXT">Xuất TXT</button>
                            </div>

                            <div class="flex flex-wrap items-center gap-1.5">
                                <button type="button" id="omBtnStart" disabled class="px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:bg-slate-800 disabled:text-slate-500 disabled:border-slate-700 disabled:shadow-none disabled:cursor-not-allowed border border-emerald-400/50 text-xs font-extrabold text-white shadow transition">▶ Bắt Đầu</button>
                                <button type="button" id="omBtnStop" disabled class="px-3.5 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 disabled:bg-slate-800 disabled:text-slate-500 disabled:border-slate-700 disabled:cursor-not-allowed border border-rose-400/50 text-xs font-extrabold text-white transition">⏹ Dừng Lại</button>
                                <button type="button" id="omBtnRetryFailed" disabled class="px-3 py-1.5 rounded-lg bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:text-slate-500 disabled:border-slate-700 disabled:cursor-not-allowed border border-slate-500 text-xs font-bold text-slate-100 transition">↻ Chạy lại Lỗi</button>
                            </div>
                        </div>

                        <!-- Search & Batch Code Actions Bar -->
                        <div class="flex flex-wrap items-center gap-2">
                            <div class="relative flex-1 min-w-[220px]">
                                <i class="fa-solid fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-xs"></i>
                                <input id="omSearchInput" type="text" autocomplete="off" spellcheck="false"
                                    placeholder="Tìm kiếm nhanh theo Email, Mail KP, IP, Code, Trạng thái..."
                                    class="w-full rounded-lg border border-slate-700 bg-[#0e1014] pl-8 pr-8 py-1.5 text-xs text-white outline-none focus:border-sky-500">
                                <button type="button" id="omBtnClearSearch" class="hidden absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-white font-bold text-sm leading-none">×</button>
                            </div>
                            <span id="omFilterCount" class="text-xs font-mono font-bold text-slate-400 px-1">Hiển thị: 0/0</span>
                            <button type="button" id="omBtnBatchGetCode" disabled class="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:bg-slate-800 disabled:text-slate-500 disabled:cursor-not-allowed text-white text-xs font-extrabold transition" data-tip="Đọc mã OTP mới nhất từ hộp thư cho các dòng đã tích chọn">📩 Lấy Code dòng chọn</button>
                            <button type="button" id="omBtnVerifyTokens" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-emerald-500/40 text-xs font-bold text-emerald-300 transition" data-tip="Kiểm tra OAuth2 Token của các dòng đã tích chọn còn đọc được Inbox không">🔍 Check Token</button>
                            <button type="button" id="omBtnDeleteSelected" disabled class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-rose-950 disabled:opacity-40 disabled:bg-slate-800/60 disabled:text-slate-500 disabled:border-slate-700/60 disabled:cursor-not-allowed border border-rose-500/40 text-xs font-bold text-rose-300 transition" data-tip="Xóa các dòng đang tích chọn khỏi bảng">🗑 Xóa chọn</button>
                        </div>

                        <!-- Main Table -->
                        <div class="rounded-xl border border-slate-700/90 bg-[#111317] overflow-auto max-h-[540px] min-h-[420px]">
                            <table class="w-full table-fixed border-collapse text-center text-xs">
                                <colgroup>
                                    <col style="width: 44px">
                                    <col style="width: 46px">
                                    <col style="width: 19%">
                                    <col style="width: 10%">
                                    <col style="width: 19%">
                                    <col style="width: 10%">
                                    <col style="width: 16%">
                                    <col style="width: 84px">
                                    <col style="width: 120px">
                                    <col style="width: 108px">
                                </colgroup>
                                <thead class="sticky top-0 z-10 bg-[#20232b] text-slate-200 border-b border-slate-700 select-none">
                                    <tr>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70"><input id="omMasterCheck" type="checkbox" class="rounded bg-slate-900"></th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Stt</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Hotmail/Outlook</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Password</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">RecoverEmail</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Ip</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Oauth2Token</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Code</th>
                                        <th class="px-2 py-2.5 text-center border-r border-slate-700/70">Lấy Code</th>
                                        <th class="px-2 py-2.5 text-center">Trạng thái</th>
                                    </tr>
                                </thead>
                                <tbody id="omTableBody" class="divide-y divide-slate-800/80 font-mono text-[11.5px]">
                                    <tr><td colspan="10" class="py-16 text-center text-slate-500 font-sans">Chưa có tài khoản nào trong thư mục này. Dán danh sách bên trái rồi bấm <b>Nạp vào Bảng</b>.</td></tr>
                                </tbody>
                            </table>
                        </div>

                        <!-- Progress Bar -->
                        <div class="flex items-center gap-3">
                            <div class="relative flex-1 h-5 rounded-md bg-[#0e1014] border border-slate-700 overflow-hidden">
                                <div id="omProgressBar" class="h-full bg-blue-600 transition-all duration-300" style="width: 0%"></div>
                                <span id="omProgressText" class="absolute inset-0 flex items-center justify-center text-[11px] font-extrabold text-white drop-shadow">0%</span>
                            </div>
                            <span id="omSummaryLabel" class="text-xs font-extrabold text-slate-200 whitespace-nowrap">Tổng: 0 — OK: 0 | Lỗi: 0</span>
                        </div>

                        <!-- Realtime Log Console -->
                        <div id="omLogConsole" class="h-36 rounded-xl border border-slate-700/90 bg-[#0d0f13] p-2.5 overflow-y-auto font-mono text-[11.5px] leading-relaxed space-y-0.5"></div>
                    </div>
                </div>
            </div>
        `;
        mainEl.appendChild(sec);
    }

    // Inject Mailbox Viewer Modal
    if (!byId('omMailboxModal')) {
        const modal = document.createElement('div');
        modal.id = 'omMailboxModal';
        modal.className = 'fixed inset-0 z-50 bg-black/75 backdrop-blur-sm hidden flex items-center justify-center p-4';
        modal.onclick = (ev) => { if (ev.target === modal) modal.classList.add('hidden'); };
        modal.innerHTML = `
            <div class="w-full max-w-4xl rounded-2xl border border-slate-600 bg-[#151821] shadow-2xl overflow-hidden text-slate-200 flex flex-col max-h-[85vh]">
                <div class="flex items-center justify-between px-4 py-3 border-b border-slate-700 bg-[#1c202b]">
                    <div class="flex items-center gap-2 min-w-0">
                        <i class="fa-solid fa-inbox text-sky-400"></i>
                        <h3 id="omModalTitle" class="text-sm font-extrabold text-white truncate">Hộp thư OAuth2</h3>
                    </div>
                    <button type="button" onclick="document.getElementById('omMailboxModal').classList.add('hidden')" class="w-7 h-7 rounded-lg bg-rose-600 hover:bg-rose-500 text-white font-black">×</button>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-[320px_minmax(0,1fr)] flex-1 min-h-[380px] overflow-hidden">
                    <div id="omModalMsgList" class="border-r border-slate-700/80 overflow-y-auto divide-y divide-slate-800 bg-[#10131a]"></div>
                    <pre id="omModalMsgBody" class="p-4 overflow-y-auto font-mono text-xs text-slate-200 whitespace-pre-wrap leading-relaxed bg-[#0c0e13] m-0">Chọn một thư bên trái để xem nội dung.</pre>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    let selectedWorkerId = '';
    try { selectedWorkerId = localStorage.getItem('tikflow_mail_worker_selected') || ''; } catch (_) {}
    function rememberWorker(id) {
        selectedWorkerId = id || '';
        try { localStorage.setItem('tikflow_mail_worker_selected', selectedWorkerId); } catch (_) {}
    }
    function requireOnlineWorker() {
        const selected = byId('omWorkerSelect');
        if ((omState.workers || []).some(w => w.id === selected?.value && w.online && !w.revoked)) return true;
        toast(selected?.value ? 'Máy đã chọn đang Offline. Mở BAT trên máy đó và chờ Online.' : 'Chọn máy chạy trong danh sách phía trên rồi bấm Bắt Đầu.');
        selected?.focus();
        return false;
    }
    let omState = {
        folders: ['Default'],
        current_folder: 'Default',
        settings: { threads: 3, timeout: 240, proxies_text: '', show_browser: true },
        rows: [],
        job: { running: false, done: 0, total: 0, ok_count: 0, err_count: 0 },
        logs: [],
    };
    let pollTimer = null;
    let loadingCodeIds = new Set();
    let lastLogSeq = '';
    let settingsInitialized = false;

    function toast(msg) {
        if (typeof window.showToast === 'function') window.showToast(msg);
    }

    function copyText(txt, label = 'dữ liệu') {
        if (!txt) return;
        navigator.clipboard.writeText(txt).then(() => toast(`📋 Đã copy ${label}!`)).catch(() => {});
    }

    async function omApi(path, body = null) {
        if (path === 'start' && body) body = {...body, worker_id: byId('omWorkerSelect')?.value || ''};
        const res = await fetch('/api/outlook-oauth/' + path, {
            method: body ? 'POST' : 'GET',
            headers: authHeaders(),
            body: body ? JSON.stringify(body) : undefined,
            cache: 'no-store',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.success === false) {
            throw new Error(data.error || `HTTP ${res.status}`);
        }
        return data;
    }

    function matchesSearch(row, q) {
        if (!q) return true;
        const hay = `${row.email || ''} ${row.password || ''} ${row.recovery_email || ''} ${row.ip_display || ''} ${row.last_code || ''} ${row.status || ''}`.toLowerCase();
        return q.split(/\s+/).every((part) => hay.includes(part));
    }

    function visibleRows() {
        const q = (byId('omSearchInput')?.value || '').trim().toLowerCase();
        return (omState.rows || []).filter((r) => matchesSearch(r, q));
    }

    function syncActionButtons() {
        const running = Boolean(omState.job?.running);
        const vis = visibleRows();
        const checkedRows = vis.filter((r) => Boolean(r.checked));
        const hasChecked = checkedRows.length > 0;
        const hasCheckedWithToken = checkedRows.some((r) => Boolean(r.oauth2_token));
        const allChecked = vis.length > 0 && checkedRows.length === vis.length;
        const hasFailed = (omState.rows || []).some((r) => !['OK', 'Chờ chạy', 'Đang chạy', ''].includes(String(r.status || '')));

        if (byId('omBtnCheckAll')) byId('omBtnCheckAll').disabled = vis.length === 0 || allChecked;
        if (byId('omBtnUncheckAll')) byId('omBtnUncheckAll').disabled = !hasChecked;
        if (byId('omBtnCopyTikFlow')) byId('omBtnCopyTikFlow').disabled = !hasCheckedWithToken;
        if (byId('omBtnExportTxt')) byId('omBtnExportTxt').disabled = !hasChecked;

        const workerOnline = (omState.workers || []).some(worker => worker.id === byId('omWorkerSelect')?.value && worker.online && !worker.revoked);
        if (byId('omBtnStart')) {
            const reason = running ? 'Lượt trước đang chạy. Bấm Dừng Lại để kết thúc.'
                : !hasChecked ? 'Tích chọn ít nhất một mail để chạy.'
                : !workerOnline ? 'Worker máy khách đang Offline. Mở lại file BAT trong máy khách/Sandbox và chờ Online.'
                : '';
            byId('omBtnStart').disabled = running;
            byId('omBtnStart').title = reason || 'Chạy các mail đã chọn trên máy khách';
        }
        if (byId('omBtnStop')) byId('omBtnStop').disabled = !running;
        if (byId('omBtnRetryFailed')) byId('omBtnRetryFailed').disabled = running || !hasFailed;

        if (byId('omBtnBatchGetCode')) byId('omBtnBatchGetCode').disabled = !hasCheckedWithToken;
        if (byId('omBtnVerifyTokens')) byId('omBtnVerifyTokens').disabled = !hasCheckedWithToken;
        if (byId('omBtnDeleteSelected')) byId('omBtnDeleteSelected').disabled = running || !hasChecked;

        if (byId('omMasterCheck')) {
            byId('omMasterCheck').disabled = vis.length === 0;
            byId('omMasterCheck').checked = allChecked;
        }
    }

    function applySnapshot(snap, syncInputs = false) {
        omState = snap;
        const workerSelect = byId('omWorkerSelect');
        if (workerSelect) {
            const previous = selectedWorkerId || workerSelect.value;
            const workers = (snap.workers || []).map((worker, order) => ({...worker, order})).filter(worker => !worker.revoked)
                .sort((a,b) => Number(b.created_at || 0) - Number(a.created_at || 0) || b.order - a.order);
            const latestOnlineByName = new Map();
            for (const worker of workers) if (worker.online && !latestOnlineByName.has(worker.name)) latestOnlineByName.set(worker.name, worker.id);
            workerSelect.innerHTML = '<option value="">Chọn phiên worker</option>' + workers.map(worker => `<option value="${esc(worker.id)}">${esc(worker.name)} · #${esc(worker.id.slice(0,6))} · ${worker.online ? (latestOnlineByName.get(worker.name) === worker.id ? '🟢 Online — Mới nhất' : '🟢 Online — Phiên cũ') : '⚪ Offline'}</option>`).join('');
            workerSelect.title = 'Mỗi dòng là một phiên BAT. Mở nhiều BAT có thể tạo nhiều phiên cùng tên máy. Chọn phiên cũ rồi bấm Ngắt worker để đóng phiên đó.';
            if (workers.some(worker => worker.id === previous)) workerSelect.value = previous;
            else if (!previous && workers.some(worker => worker.online)) workerSelect.value = workers.find(worker => worker.online).id;
            rememberWorker(workerSelect.value);
            byId('omWorkerStatus').textContent = workers.some(worker => worker.id === workerSelect.value && worker.online)
                ? `Trình duyệt mở trên ${workers.find(worker => worker.id === workerSelect.value).name} · #${workerSelect.value.slice(0,6)}. Phiên mới nhất được đánh dấu trong danh sách.`
                : 'Chưa kết nối máy chạy. Tải và mở .bat trong máy khách/máy ảo.';
        }
        const running = Boolean(snap.job?.running);
        const navBadge = byId('outlookMailNavCount');
        if (navBadge) {
            const total = (snap.rows || []).length;
            navBadge.textContent = String(total);
            navBadge.classList.toggle('hidden', !isTuandzvcc() || total === 0);
        }

        // Folder dropdown
        const sel = byId('omFolderSelect');
        if (sel) {
            sel.innerHTML = (snap.folders || ['Default'])
                .map((fn) => `<option value="${esc(fn)}" ${fn === snap.current_folder ? 'selected' : ''}>${esc(fn)}</option>`)
                .join('');
            sel.disabled = Boolean(snap.job?.running);
        }

        if (syncInputs || !settingsInitialized) {
            settingsInitialized = true;
            const st = snap.settings || {};
            if (byId('omSpinThreads')) byId('omSpinThreads').value = st.threads || 3;
            if (byId('omSpinTimeout')) byId('omSpinTimeout').value = st.timeout || 240;
            if (byId('omChkShowBrowser')) byId('omChkShowBrowser').checked = true;
            if (byId('omProxyInput')) {
                byId('omProxyInput').value = st.proxies_text || '';
                updateInputCounters();
            }
        }

        renderTable();
        renderProgressAndLogs();
        syncActionButtons();

        if (!pollTimer) {
            pollTimer = setInterval(() => {
                if (byId('outlookMailWorkspace')?.classList.contains('hidden') || !isTuandzvcc()) {
                    clearInterval(pollTimer); pollTimer = null; return;
                }
                refreshState(false);
            }, 2000);
        }
    }

    function renderTable() {
        const tbody = byId('omTableBody');
        if (!tbody) return;
        const q = (byId('omSearchInput')?.value || '').trim().toLowerCase();
        byId('omBtnClearSearch')?.classList.toggle('hidden', !q);

        const allRows = omState.rows || [];
        const filtered = allRows.filter((r) => matchesSearch(r, q));
        if (byId('omFilterCount')) {
            byId('omFilterCount').textContent = `Hiển thị: ${filtered.length}/${allRows.length}`;
        }

        if (!filtered.length) {
            tbody.innerHTML = `<tr><td colspan="10" class="py-14 text-center text-slate-500 font-sans">${
                allRows.length ? 'Không tìm thấy dòng nào khớp từ khóa lọc.' : 'Chưa có tài khoản nào trong thư mục này. Dán danh sách bên trái rồi bấm <b>Nạp vào Bảng</b>.'
            }</td></tr>`;
            syncActionButtons();
            return;
        }

        tbody.innerHTML = filtered.map((r, idx) => {
            const stText = String(r.status || 'Chờ chạy');
            const isOk = Boolean(r.oauth2_token) && stText === 'OK';
            const isRun = Boolean(r.active_running) || stText === 'Đang chạy';
            const isErr = !isOk && !isRun && stText !== 'Chờ chạy';
            let rowBg = idx % 2 === 0 ? 'bg-[#13151a]' : 'bg-[#171a20]';
            let statusCls = 'text-slate-400 font-semibold';
            if (isRun) {
                rowBg = 'bg-[#172b46]';
                statusCls = 'text-yellow-300 font-extrabold';
            } else if (isOk) {
                rowBg = 'bg-[#144524]';
                statusCls = 'text-emerald-400 font-extrabold';
            } else if (isErr) {
                rowBg = 'bg-[#3f1920]';
                statusCls = 'text-rose-400 font-extrabold';
            }

            const tokShort = r.oauth2_token ? `${r.oauth2_token.slice(0, 24)}...` : '—';
            const codeLoading = loadingCodeIds.has(r.row_id);
            const hasToken = Boolean(r.oauth2_token);

            return `
                <tr class="${rowBg} hover:brightness-110 transition" data-rid="${esc(r.row_id)}">
                    <td class="px-2 py-2 text-center border-r border-slate-800/60">
                        <input type="checkbox" class="om-row-chk rounded bg-slate-900 cursor-pointer" data-rid="${esc(r.row_id)}" ${r.checked ? 'checked' : ''}>
                    </td>
                    <td class="px-2 py-2 text-center text-slate-300 border-r border-slate-800/60">${idx + 1}</td>
                    <td class="px-2.5 py-2 text-center text-slate-100 border-r border-slate-800/60 cursor-pointer truncate" data-tip="Bấm để copy Email: ${esc(r.email)}" data-copy="${esc(r.email)}" data-label="Email">${esc(r.email)}</td>
                    <td class="px-2.5 py-2 text-center text-slate-200 border-r border-slate-800/60 cursor-pointer truncate" data-tip="Bấm để copy Password" data-copy="${esc(r.password)}" data-label="Password">${esc(r.password)}</td>
                    <td class="px-2.5 py-2 text-center text-slate-200 border-r border-slate-800/60 ${r.recovery_email ? 'cursor-pointer' : ''} truncate" ${r.recovery_email ? `data-tip="Bấm để copy RecoverEmail: ${esc(r.recovery_email)}" data-copy="${esc(r.recovery_email)}" data-label="RecoverEmail"` : ''}>${esc(r.recovery_email || '—')}</td>
                    <td class="px-2.5 py-2 text-center text-slate-300 border-r border-slate-800/60 ${r.ip_display ? 'cursor-pointer' : ''} truncate" ${r.ip_display ? `data-tip="Bấm để copy Proxy/IP: ${esc(r.ip_display)}" data-copy="${esc(r.proxy || r.ip_display)}" data-label="Proxy"` : ''}>${esc(r.ip_display || '—')}</td>
                    <td class="px-2.5 py-2 text-center text-cyan-200 border-r border-slate-800/60 ${hasToken ? 'cursor-pointer' : ''} truncate" ${hasToken ? `data-tip="Bấm để copy OAuth2Token" data-copy="${esc(r.oauth2_token)}" data-label="OAuth2Token"` : ''}>${esc(tokShort)}</td>
                    <td class="px-2 py-2 text-center border-r border-slate-800/60 ${r.last_code ? 'cursor-pointer text-yellow-300' : 'text-slate-500'} font-extrabold"
                        ${r.last_code ? `data-tip="${esc(r.last_code_info ? r.last_code_info + ' (Bấm để Copy)' : 'Bấm để Copy Code')}" data-copy="${esc(r.last_code)}" data-label="Code ${esc(r.last_code)}"` : ''}>${esc(r.last_code || '—')}</td>
                    <td class="px-2 py-1.5 text-center border-r border-slate-800/60 whitespace-nowrap">
                        <div class="inline-flex items-center justify-center gap-1">
                            <button type="button" class="om-btn-code px-2 py-1 rounded bg-sky-600 hover:bg-sky-500 disabled:bg-slate-800 disabled:text-slate-500 disabled:cursor-not-allowed text-white font-sans font-bold text-[11px] transition"
                                data-tip="Đọc nhanh mã OTP mới nhất từ hộp thư" data-rid="${esc(r.row_id)}" ${!hasToken || codeLoading ? 'disabled' : ''}>${codeLoading ? 'Đọc...' : 'Lấy Code'}</button>
                            <button type="button" class="om-btn-inbox w-7 h-[25px] inline-flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 border border-slate-600/80 disabled:opacity-35 disabled:cursor-not-allowed text-cyan-300 hover:text-white text-xs transition"
                                data-tip="Xem 15 thư gần nhất trong hộp thư" data-rid="${esc(r.row_id)}" ${!hasToken ? 'disabled' : ''}><i class="fa-solid fa-envelopes-bulk"></i></button>
                        </div>
                    </td>
                    <td class="px-2 py-2 text-center font-sans ${statusCls} truncate">${esc(stText)}</td>
                </tr>
            `;
        }).join('');
        syncActionButtons();
    }

    function renderProgressAndLogs() {
        const allRows = omState.rows || [];
        const job = omState.job || {};
        let pct = 0;
        if (job.running && job.total > 0) {
            pct = Math.round((job.done * 100) / job.total);
            byId('omSummaryLabel').textContent = `Đang chạy ${job.done}/${job.total} — OK: ${job.ok_count} | Lỗi: ${job.err_count}`;
        } else {
            const okN = allRows.filter((r) => r.oauth2_token && String(r.status || '') === 'OK').length;
            const errN = allRows.filter((r) => !['OK', 'Chờ chạy', 'Đang chạy', ''].includes(String(r.status || ''))).length;
            pct = allRows.length > 0 ? Math.round((okN * 100) / allRows.length) : 0;
            byId('omSummaryLabel').textContent = `Tổng: ${allRows.length} — OK: ${okN} | Lỗi: ${errN}`;
        }
        if (byId('omProgressBar')) byId('omProgressBar').style.width = `${pct}%`;
        if (byId('omProgressText')) byId('omProgressText').textContent = `${pct}%`;

        const logs = omState.logs || [];
        const newestSeq = logs.length ? logs[logs.length - 1].seq : '';
        const logBox = byId('omLogConsole');
        if (logBox && newestSeq !== lastLogSeq) {
            lastLogSeq = newestSeq;
            logBox.innerHTML = logs.map((l) => {
                let col = 'text-white';
                if (l.kind === 'ok') col = 'text-emerald-400 font-semibold';
                else if (l.kind === 'err') col = 'text-rose-400';
                else if (l.kind === 'summary') col = 'text-sky-400 font-semibold';
                else if (l.kind === 'code') col = 'text-yellow-300 font-semibold';
                const fmtTxt = String(l.text || '').replace(/^([•✓✗]\s*[^:]+:\s*)(\S)/u, (_, p1, ch) => p1 + ch.toUpperCase());
                return `<div><span class="text-white">[${esc(l.timestamp ? new Date(l.timestamp * 1000).toLocaleTimeString("vi-VN", {timeZone:"Asia/Ho_Chi_Minh",hour12:false}) : l.time)}]</span> <span class="${col}">${esc(fmtTxt)}</span></div>`;
            }).join('');
            logBox.scrollTop = logBox.scrollHeight;
        }
    }

    function updateInputCounters() {
        const mLines = (byId('omMailInput')?.value || '').split(/\r?\n/).filter((l) => l.trim() && l.includes('@'));
        const pLines = (byId('omProxyInput')?.value || '').split(/\r?\n/).filter((l) => l.trim());
        if (byId('omInputMailCount')) byId('omInputMailCount').textContent = `${mLines.length} dòng`;
        if (byId('omInputProxyCount')) byId('omInputProxyCount').textContent = `${pLines.length} proxy`;
    }

    function currentSettingsPayload() {
        return {
            threads: Number(byId('omSpinThreads')?.value || 3),
            timeout: Number(byId('omSpinTimeout')?.value || 240),
            proxies_text: byId('omProxyInput')?.value || '',
            show_browser: true,
        };
    }

    async function refreshState(syncInputs = false) {
        if (!isTuandzvcc()) return;
        try {
            const snap = await omApi('state');
            applySnapshot(snap, syncInputs);
        } catch (err) {
            toast('Lỗi tải Quản Lý Mail: ' + err.message);
        }
    }

    async function triggerGetCode(rowIds, autoCopySingle = false) {
        if (!rowIds.length) return;
        rowIds.forEach((id) => loadingCodeIds.add(id));
        renderTable();
        try {
            const snap = await omApi('get-code', { row_ids: rowIds });
            rowIds.forEach((id) => loadingCodeIds.delete(id));
            applySnapshot(snap, false);
            if (autoCopySingle && snap.single_code) {
                copyText(snap.single_code, `Code [${snap.single_code}]`);
            } else if (autoCopySingle && !snap.single_code) {
                toast('Chưa tìm thấy mã số mới trong hộp thư.');
            }
        } catch (err) {
            rowIds.forEach((id) => loadingCodeIds.delete(id));
            renderTable();
            toast('Lỗi đọc Code: ' + err.message);
        }
    }

    async function openInboxModal(rowId) {
        try {
            toast('⏳ Đang đọc 15 thư gần nhất qua OAuth2...');
            const data = await omApi('messages', { row_id: rowId });
            const modal = byId('omMailboxModal');
            const listEl = byId('omModalMsgList');
            const bodyEl = byId('omModalMsgBody');
            byId('omModalTitle').textContent = `Hộp thư OAuth2 — ${data.email} (${(data.messages || []).length} thư gần nhất)`;
            const msgs = data.messages || [];
            if (!msgs.length) {
                listEl.innerHTML = `<div class="p-6 text-xs text-slate-500 text-center">Hộp thư trống (0 thư).</div>`;
                bodyEl.textContent = 'Không có thư nào trong Inbox hoặc Junk.';
            } else {
                listEl.innerHTML = msgs.map((m, idx) => `
                    <button type="button" class="om-msg-item w-full text-left px-3.5 py-2.5 hover:bg-slate-800/80 transition ${idx === 0 ? 'bg-slate-800' : ''}" data-idx="${idx}">
                        <div class="flex items-center justify-between text-[11px] text-sky-400 font-bold">
                            <span>[${esc(m.folder)}]</span><span>${esc(m.age)}</span>
                        </div>
                        <div class="text-xs font-bold text-slate-100 truncate mt-0.5">${esc(m.sender)}</div>
                        <div class="text-[11px] text-slate-400 truncate">${esc(m.subject)}</div>
                    </button>
                `).join('');
                const showMsg = (i) => {
                    const m = msgs[i];
                    if (!m) return;
                    bodyEl.textContent = `Từ: ${m.sender}\nTiêu đề: ${m.subject}\nThời gian: ${m.received_at}\n${'-'.repeat(58)}\n\n${m.body}`;
                };
                showMsg(0);
                listEl.querySelectorAll('.om-msg-item').forEach((btn) => {
                    btn.onclick = () => {
                        listEl.querySelectorAll('.om-msg-item').forEach((b) => b.classList.remove('bg-slate-800'));
                        btn.classList.add('bg-slate-800');
                        showMsg(Number(btn.dataset.idx));
                    };
                });
            }
            modal.classList.remove('hidden');
        } catch (err) {
            toast('Lỗi mở hộp thư: ' + err.message);
        }
    }

    // Bind UI events
    byId('omMailInput')?.addEventListener('input', updateInputCounters);
    byId('omProxyInput')?.addEventListener('input', updateInputCounters);
    byId('omBtnClearMailInput')?.addEventListener('click', () => {
        byId('omMailInput').value = '';
        updateInputCounters();
    });
    byId('omSearchInput')?.addEventListener('input', renderTable);
    byId('omBtnClearSearch')?.addEventListener('click', () => {
        byId('omSearchInput').value = '';
        renderTable();
    });

    byId('omBtnImportMails')?.addEventListener('click', async () => {
        const raw = byId('omMailInput').value;
        if (!raw.trim()) return toast('Hãy dán danh sách mail|pass vào ô bên trái.');
        try {
            await omApi('settings', currentSettingsPayload());
            const snap = await omApi('rows', { action: 'import', raw_text: raw });
            byId('omMailInput').value = '';
            updateInputCounters();
            applySnapshot(snap, false);
            toast('✅ Đã nạp danh sách mail vào bảng!');
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnApplyProxy')?.addEventListener('click', async () => {
        try {
            const snap = await omApi('rows', { action: 'apply_proxies', proxies_text: byId('omProxyInput').value });
            applySnapshot(snap, false);
            toast('✅ Đã cập nhật Proxy cho bảng!');
        } catch (err) {
            toast(err.message);
        }
    });

    ['omSpinThreads', 'omSpinTimeout', 'omChkShowBrowser'].forEach((id) => {
        byId(id)?.addEventListener('change', () => {
            omApi('settings', currentSettingsPayload()).catch(() => {});
        });
    });

    byId('omFolderSelect')?.addEventListener('change', async (ev) => {
        try {
            const snap = await omApi('folder', { action: 'switch', name: ev.target.value });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnFolderAdd')?.addEventListener('click', async () => {
        const name = prompt('Nhập tên thư mục mới:');
        if (!name || !name.trim()) return;
        try {
            const snap = await omApi('folder', { action: 'add', name: name.trim() });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnFolderRename')?.addEventListener('click', async () => {
        const nextName = prompt('Đổi tên thư mục hiện tại thành:', omState.current_folder);
        if (!nextName || !nextName.trim() || nextName.trim() === omState.current_folder) return;
        try {
            const snap = await omApi('folder', { action: 'rename', name: omState.current_folder, new_name: nextName.trim() });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnFolderDel')?.addEventListener('click', async () => {
        if (!confirm(`Xóa thư mục "${omState.current_folder}" (${(omState.rows || []).length} mail)?`)) return;
        try {
            const snap = await omApi('folder', { action: 'delete', name: omState.current_folder });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnCheckAll')?.addEventListener('click', async () => {
        const ids = visibleRows().map((r) => r.row_id);
        if (!ids.length) return;
        const snap = await omApi('rows', { action: 'set_checked', row_ids: ids, checked: true });
        applySnapshot(snap, false);
    });

    byId('omBtnUncheckAll')?.addEventListener('click', async () => {
        const ids = visibleRows().map((r) => r.row_id);
        if (!ids.length) return;
        const snap = await omApi('rows', { action: 'set_checked', row_ids: ids, checked: false });
        applySnapshot(snap, false);
    });

    byId('omMasterCheck')?.addEventListener('change', async (ev) => {
        const ids = visibleRows().map((r) => r.row_id);
        if (!ids.length) return;
        const snap = await omApi('rows', { action: 'set_checked', row_ids: ids, checked: ev.target.checked });
        applySnapshot(snap, false);
    });

    byId('omBtnCopyTikFlow')?.addEventListener('click', () => {
        const okRows = visibleRows().filter((r) => r.checked && r.oauth2_token);
        if (!okRows.length) return toast('Hãy tích chọn dòng đã có OAuth2Token để copy.');
        const lines = okRows.map((r) => `${r.email}|${r.password}|${r.oauth2_token}|${r.client_id}`);
        copyText(lines.join('\n'), `${lines.length} dòng chuẩn TikFlow`);
    });

    byId('omBtnExportTxt')?.addEventListener('click', () => {
        const selRows = visibleRows().filter((r) => r.checked);
        if (!selRows.length) return toast('Hãy tích chọn ít nhất 1 dòng để xuất TXT.');
        const lines = selRows.map((r) => (
            r.oauth2_token
                ? `${r.email}|${r.password}|${r.recovery_email || ''}|${r.oauth2_token}|${r.client_id}`
                : `${r.email}|${r.password}${r.recovery_email ? '|' + r.recovery_email : ''}`
        ));
        const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${omState.current_folder}_outlook_oauth2.txt`;
        a.click();
        URL.revokeObjectURL(a.href);
    });

    byId('omBtnStart')?.addEventListener('click', async () => {
        const checkedIds = visibleRows().filter((r) => r.checked).map((r) => r.row_id);
        if (!checkedIds.length) { toast('Tích chọn ít nhất một mail trong bảng để chạy.'); return; }
        if (!requireOnlineWorker()) return;
        try {
            const snap = await omApi('start', { mode: 'ids', row_ids: checkedIds, settings: currentSettingsPayload() });
            applySnapshot(snap, false);
            toast('🚀 Đã gửi lệnh chạy đến worker máy khách đã chọn.');
        } catch (err) {
            toast(err.message);
        }
    });
    byId('omWorkerSelect')?.addEventListener('change', () => {
        rememberWorker(byId('omWorkerSelect').value);
        syncActionButtons();
    });
    byId('omDownloadWorker')?.addEventListener('click', async () => {
        const button = byId('omDownloadWorker');
        button.disabled = true;
        try {
            const response = await fetch('/api/outlook-oauth/worker/download', {method:'POST', headers:authHeaders(), body:'{}', cache:'no-store'});
            if (!response.ok) throw Error((await response.json()).error || 'Không tải được worker.');
            rememberWorker(response.headers.get('X-Mail-Worker-Id') || '');
            const url = URL.createObjectURL(await response.blob());
            const link = document.createElement('a');
            link.href = url; link.download = 'tikflow_mail_worker.bat'; link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            await refreshState(false);
            toast('Mở file .bat trong máy khách/máy ảo muốn chạy, chờ máy hiện Online rồi bấm Bắt Đầu.');
        } catch (error) { toast(error.message); }
        finally { button.disabled = false; }
    });
    byId('omRevokeWorker')?.addEventListener('click', async () => {
        const id = byId('omWorkerSelect').value;
        if (!id) return;
        try { applySnapshot(await omApi('worker/revoke', {worker_id:id}), false); }
        catch (error) { toast(error.message); }
    });

    byId('omBtnStop')?.addEventListener('click', async () => {
        try {
            const snap = await omApi('stop', {});
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnRetryFailed')?.addEventListener('click', async () => {
        if (!requireOnlineWorker()) return;
        try {
            const snap = await omApi('start', { mode: 'retry_failed', settings: currentSettingsPayload() });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnBatchGetCode')?.addEventListener('click', () => {
        const ids = visibleRows().filter((r) => r.checked && r.oauth2_token).map((r) => r.row_id);
        if (!ids.length) return toast('Hãy tích chọn các dòng đã có OAuth2Token để đọc Code.');
        triggerGetCode(ids, ids.length === 1);
    });

    byId('omBtnVerifyTokens')?.addEventListener('click', async () => {
        const targetIds = visibleRows().filter((r) => r.checked && r.oauth2_token).map((r) => r.row_id);
        if (!targetIds.length) return toast('Hãy tích chọn dòng có OAuth2Token để kiểm tra.');
        try {
            toast(`🔍 Đang kiểm tra ${targetIds.length} OAuth2 Token...`);
            const snap = await omApi('verify-tokens', { row_ids: targetIds });
            applySnapshot(snap, false);
            toast('✅ Đã kiểm tra xong trạng thái OAuth2 Token!');
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omBtnDeleteSelected')?.addEventListener('click', async () => {
        const ids = visibleRows().filter((r) => r.checked).map((r) => r.row_id);
        if (!ids.length) return toast('Hãy tích chọn các dòng muốn xóa.');
        if (!confirm(`Xóa ${ids.length} dòng đã chọn khỏi thư mục "${omState.current_folder}"?`)) return;
        try {
            const snap = await omApi('rows', { action: 'delete', row_ids: ids });
            applySnapshot(snap, false);
        } catch (err) {
            toast(err.message);
        }
    });

    byId('omTableBody')?.addEventListener('change', (ev) => {
        const chk = ev.target.closest('.om-row-chk');
        if (!chk) return;
        const rid = chk.dataset.rid;
        const row = (omState.rows || []).find((x) => x.row_id === rid);
        if (row) row.checked = chk.checked;
        syncActionButtons();
        omApi('rows', { action: 'set_checked', row_ids: [rid], checked: chk.checked }).catch(() => {});
    });

    byId('omTableBody')?.addEventListener('click', (ev) => {
        const codeBtn = ev.target.closest('.om-btn-code');
        if (codeBtn) {
            triggerGetCode([codeBtn.dataset.rid], true);
            return;
        }
        const inboxBtn = ev.target.closest('.om-btn-inbox');
        if (inboxBtn) {
            openInboxModal(inboxBtn.dataset.rid);
            return;
        }
        const copyCell = ev.target.closest('[data-copy]');
        if (copyCell && copyCell.dataset.copy) {
            copyText(copyCell.dataset.copy, copyCell.dataset.label || 'nội dung');
        }
    });

    // Expose loader & auth visibility sync to index.html
    window.loadOutlookMailWorkspace = () => {
        if (!isTuandzvcc()) return;
        refreshState(false);
    };

    window.syncOutlookMailTabVisibility = () => {
        const allowed = isTuandzvcc();
        const btn = byId('tabMainOutlookMail');
        const ws = byId('outlookMailWorkspace');
        if (btn) btn.classList.toggle('hidden', !allowed);
        if (!allowed) {
            if (ws) ws.classList.add('hidden');
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        } else {
            refreshState(true);
        }
    };

    // Initial sync after DOM/auth ready
    setTimeout(() => window.syncOutlookMailTabVisibility(), 120);
})();
