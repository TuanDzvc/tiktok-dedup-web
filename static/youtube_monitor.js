/* Admin-only cloud YouTube monitoring. Secrets never enter browser storage. */
(() => {
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const byId = id => document.getElementById(id);
    let loaded = false, busy = false, state = null;
    const inputClass = 'w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none focus:border-cyan-400';
    const buttonClass = 'rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 px-3 py-2 text-xs font-bold text-white hover:brightness-110 transition disabled:opacity-40';
    const dialogStyle = document.createElement('style');
    dialogStyle.textContent = `
        #ytSettingsDialog {
            position: fixed;
            inset: 50% auto auto 50%;
            margin: 0;
            transform: translate(-50%, -50%);
            width: min(var(--yt-settings-width, 960px), calc(100vw - 32px));
            max-width: none;
        }
        #ytSettingsDialog::backdrop {
            background: rgba(0, 0, 0, .55);
            backdrop-filter: blur(7px);
            -webkit-backdrop-filter: blur(7px);
        }
    `;
    document.head.appendChild(dialogStyle);
    const tab = document.createElement('button');
    tab.id = 'tabMainYouTubeMonitor';
    tab.className = 'hidden';
    tab.type = 'button';
    tab.innerHTML = '<i class="fa-brands fa-youtube text-rose-400"></i><span>Theo Dõi YouTube</span>';
    tab.onclick = () => switchMainWorkspaceTab('youtube_monitor');
    byId('mainWorkspaceTabs').firstElementChild.appendChild(tab);
    const section = document.createElement('section');
    section.id = 'youtubeMonitorWorkspace';
    section.className = 'hidden space-y-5';
    section.innerHTML = `
        <div class="flex flex-wrap items-center justify-between gap-3">
            <div><h2 class="text-xl font-extrabold text-white"><i class="fa-brands fa-youtube text-rose-400 mr-2"></i>Theo Dõi Kênh YouTube</h2>
            <p class="text-xs text-slate-400 mt-2">Chạy trên Render · Tắt PC hoặc đóng web vẫn theo dõi khi dịch vụ Render đang hoạt động.</p>
            <p id="ytRuntime" class="text-xs text-slate-400 mt-1"></p></div>
            <div class="flex items-center gap-2"><button type="button" id="ytOpenSettings" aria-haspopup="dialog" class="${buttonClass}"><i class="fa-solid fa-gear mr-1"></i>Cài đặt</button>
            <button id="ytRefresh" class="${buttonClass}">↻ Làm mới</button></div>
        </div>
        <div id="ytMessage" role="status" class="text-sm text-cyan-200"></div>
        <dialog id="ytSettingsDialog" aria-labelledby="ytSettingsTitle" class="m-auto w-[calc(100vw-2rem)] max-w-4xl max-h-[90vh] overflow-hidden p-0 rounded-2xl border border-slate-700 bg-slate-950 text-slate-200 shadow-2xl backdrop:bg-black/70">
        <div class="max-h-[calc(90vh-2px)] overflow-y-auto custom-scrollbar">
        <div class="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-slate-700 bg-slate-950 px-4 py-3">
            <h3 id="ytSettingsTitle" class="font-bold text-cyan-200"><i class="fa-solid fa-gear mr-2"></i>Cài Đặt Theo Dõi YouTube</h3>
            <button type="button" id="ytCloseSettings" aria-label="Đóng cài đặt YouTube" class="w-8 h-8 rounded-lg border border-red-500/60 bg-red-950/80 hover:bg-red-900 text-xl text-red-300 hover:text-white transition">×</button>
        </div>
        <form id="ytSettings" class="bg-slate-900/70 p-4 space-y-4">
            <div class="flex flex-wrap items-center justify-between gap-3"><h4 class="font-bold text-cyan-200">Cấu hình thông báo</h4>
            <label class="text-sm text-emerald-300 flex items-center gap-2"><input id="ytEnabled" type="checkbox"> Bật theo dõi trên Render</label></div>
            <div id="ytRenderNotice" class="rounded-xl border border-amber-500/40 bg-amber-950/20 p-3 text-xs leading-relaxed text-amber-200 whitespace-nowrap overflow-x-auto custom-scrollbar">Render Free có thể ngủ khi thiếu truy cập. Muốn theo dõi liên tục 24/7, dùng gói Render luôn chạy. Lần quét đầu chỉ tạo mốc; không gửi lại video cũ.</div>
            <div class="grid md:grid-cols-2 gap-4">
                <label class="text-xs text-slate-300 space-y-2"><span>YouTube API key — mỗi dòng một key (có thể để trống để dùng RSS/web)</span>
                    <textarea id="ytKeys" rows="3" autocomplete="off" spellcheck="false" class="${inputClass}" placeholder="Để trống để giữ key đã lưu"></textarea>
                    <span id="ytKeyState" class="block text-cyan-300"></span></label>
                <div class="space-y-2"><label class="block text-xs text-slate-300" for="ytInterval">Chu kỳ kiểm tra mỗi kênh (giây, tối thiểu 3)</label>
                    <input id="ytInterval" type="number" min="3" max="86400" value="3" class="${inputClass}">
                    <p id="ytQuota" class="text-xs text-slate-400 leading-relaxed"></p>
                    <p class="text-xs text-slate-400 leading-relaxed">Theo dõi cả video thường và Shorts. Video mới hiển thị trong nhật ký; chỉ gửi ra nơi bạn đã cấu hình.</p></div>
                <div class="space-y-2"><label class="block text-xs text-slate-300" for="ytToken">Telegram Bot Token</label>
                    <input id="ytToken" type="password" autocomplete="new-password" class="${inputClass}" placeholder="Chưa cấu hình">
                    <label class="block text-xs text-slate-300" for="ytChat">Telegram Chat ID / @tên_kênh</label>
                    <input id="ytChat" class="${inputClass}" placeholder="Ví dụ: -100… hoặc Chat ID cá nhân">
                    <p class="text-xs text-slate-400">Nhắn /start cho bot; nếu gửi vào nhóm/kênh, thêm bot và cấp quyền gửi tin.</p>
                    <button type="button" id="ytTestTelegram" class="${buttonClass}">Gửi thử Telegram</button></div>
                <div class="space-y-2"><label class="block text-xs text-slate-300" for="ytDiscord">Discord Webhook URL (tùy chọn)</label>
                    <input id="ytDiscord" type="password" autocomplete="new-password" class="${inputClass}" placeholder="https://discord.com/api/webhooks/…">
                    <p class="text-xs text-slate-400">Có thể dùng Telegram, Discord hoặc cả hai. Lưu cấu hình trước khi gửi thử.</p>
                    <button type="button" id="ytTestDiscord" class="${buttonClass}">Gửi thử Discord</button></div>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-3"><p id="ytSettingsMessage" role="status" class="text-xs text-cyan-200"></p>
                <button id="ytSave" disabled class="rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 px-5 py-2 text-sm font-bold text-white disabled:opacity-40">Lưu cấu hình</button></div>
        </form>
        </div></dialog>
        <div class="rounded-2xl border border-slate-700 bg-slate-900/70 p-4 space-y-4">
            <form id="ytAddForm" class="flex flex-wrap gap-2"><input id="ytChannel" required class="${inputClass} flex-1 min-w-[220px]" placeholder="Dán @handle, Channel ID UC… hoặc link kênh YouTube">
                <button id="ytAdd" class="${buttonClass}">＋ Thêm kênh</button></form>
            <div id="ytChannels" class="space-y-3"></div>
        </div>
        <div class="rounded-2xl border border-slate-700 bg-slate-900/70 p-4"><h3 class="font-bold text-cyan-200 mb-3">Nhật ký video mới & thông báo</h3><div id="ytEvents" class="space-y-3 max-h-[500px] overflow-auto"></div></div>`;
    document.querySelector('main').appendChild(section);

    window.closeYoutubeMonitorSettings = () => {
        if (byId('ytSettingsDialog').open) byId('ytSettingsDialog').close();
    };
    function fitSettingsWidthToNotice() {
        const dialog = byId('ytSettingsDialog');
        if (!dialog.open) return;
        const notice = byId('ytRenderNotice');
        const text = document.createRange();
        text.selectNodeContents(notice);
        const horizontalSpace = element => {
            const style = getComputedStyle(element);
            return ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
                .reduce((sum, key) => sum + (parseFloat(style[key]) || 0), 0);
        };
        let width = Math.ceil(text.getBoundingClientRect().width + horizontalSpace(notice)
            + horizontalSpace(byId('ytSettings')) + horizontalSpace(dialog));
        dialog.style.setProperty('--yt-settings-width', width + 'px');
        const scroller = dialog.firstElementChild;
        width += scroller.offsetWidth - scroller.clientWidth;
        dialog.style.setProperty('--yt-settings-width', width + 'px');
    }
    byId('ytOpenSettings').onclick = () => {
        if (currentVipUser?.role !== 'admin') return;
        byId('ytSettingsDialog').showModal();
        fitSettingsWidthToNotice();
        document.fonts.ready.then(fitSettingsWidthToNotice);
    };
    window.addEventListener('resize', fitSettingsWidthToNotice);
    byId('ytCloseSettings').onclick = closeYoutubeMonitorSettings;
    byId('ytSettingsDialog').onclick = event => {
        if (event.target === byId('ytSettingsDialog')) closeYoutubeMonitorSettings();
    };

    function message(text, error = false) {
        for (const id of ['ytMessage', 'ytSettingsMessage']) {
            byId(id).textContent = text;
            byId(id).className = error ? 'text-sm text-rose-300' : 'text-sm text-cyan-200';
        }
    }
    async function api(action, body) {
        const response = await fetch('/api/youtube-monitor/' + action, {
            method: body ? 'POST' : 'GET', headers: {'Authorization':'Bearer '+getVipToken(), 'Content-Type':'application/json'},
            body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(75000), cache: 'no-store'
        });
        let data;
        try { data = await response.json(); } catch { throw Error('Render chưa trả dữ liệu hợp lệ. Thử lại sau.'); }
        if (!response.ok || !data.success) throw Error(data.error || 'Không thực hiện được yêu cầu.');
        return data;
    }
    function stamp(value) {
        return value ? new Date(value * 1000).toLocaleString('vi-VN', {timeZone:'Asia/Ho_Chi_Minh'}) : 'Chưa kiểm tra';
    }
    function render(data, fillSettings) {
        state = data;
        const s = data.settings;
        if (fillSettings) {
            byId('ytEnabled').checked = s.enabled;
            byId('ytInterval').value = s.interval_seconds;
            byId('ytChat').value = s.telegram_chat_id;
            byId('ytToken').placeholder = s.has_telegram_token ? 'Đã lưu token · để trống để giữ nguyên' : 'Chưa cấu hình';
            byId('ytDiscord').placeholder = s.has_discord_webhook ? 'Đã lưu webhook · để trống để giữ nguyên' : 'https://discord.com/api/webhooks/…';
        }
        byId('ytKeyState').textContent = `Đã lưu ${s.api_key_count} API key trên cloud.`;
        byId('ytRuntime').textContent = `Render: ${s.enabled ? 'Đã bật theo dõi' : 'Đang tạm dừng'} · Nhịp gần nhất: ${stamp(data.last_tick)}`;
        byId('ytQuota').textContent = `Nếu dùng API: khoảng ${(Math.ceil(86400/s.interval_seconds)*data.channels.filter(c=>c.enabled).length).toLocaleString('vi-VN')} đơn vị/ngày cho các lượt đọc danh sách video, chưa gồm lượt thêm kênh và thử lại. RSS/web có thể cập nhật chậm hơn.`;
        byId('ytSave').disabled = false;
        byId('ytChannels').innerHTML = data.channels.length ? data.channels.map(c => `
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-700 p-3">
                <div class="min-w-0 flex-1"><a class="text-sm font-bold text-cyan-200 break-words" target="_blank" rel="noopener noreferrer" href="https://www.youtube.com/channel/${esc(c.id)}">${esc(c.title)}</a>
                <div class="text-xs text-slate-400 break-all mt-1">${esc(c.id)} · ${esc(c.source || 'Chờ đọc dữ liệu')} · ${esc(stamp(c.last_checked))}</div>
                <div class="text-xs mt-1 ${c.error ? 'text-rose-300' : 'text-emerald-300'}">${esc(c.error || (!c.enabled ? 'Đã tạm dừng kênh' : c.baseline ? 'Đã tạo mốc · '+c.seen_count+' video đã ghi nhận' : 'Chờ lượt đầu để tạo mốc'))}</div>
                ${c.warning ? `<div class="text-xs text-amber-300 mt-1">${esc(c.warning)}</div>` : ''}</div>
                <div class="flex flex-wrap gap-2"><button data-action="check" data-id="${esc(c.id)}" class="${buttonClass}">Kiểm tra ngay</button>
                <button data-action="toggle" data-id="${esc(c.id)}" data-enabled="${!c.enabled}" class="${buttonClass}">${c.enabled ? 'Tạm dừng' : 'Bật kênh'}</button>
                <button data-action="delete" data-id="${esc(c.id)}" class="rounded-xl border border-rose-600/50 px-3 py-2 text-xs text-rose-300">Xóa</button></div>
            </div>`).join('') : '<p class="text-sm text-slate-400">Chưa có kênh theo dõi. Thêm kênh ở phía trên.</p>';
        byId('ytEvents').innerHTML = data.events.length ? data.events.map(e => `
            <div class="border-b border-slate-700/70 pb-3 text-xs"><div class="font-bold text-slate-200">${esc(e.channel_title)}</div>
            <a class="block text-cyan-300 break-words my-1" target="_blank" rel="noopener noreferrer" href="${esc(e.url)}">${esc(e.title)}</a>
            <div class="text-slate-400">${esc(stamp(e.detected_at))} · ${esc(e.sent.length ? 'Đã gửi: '+e.sent.join(', ') : 'Đã ghi nhận trên web')}${e.pending.length ? ' · Chờ gửi: '+esc(e.pending.join(', ')) : ''}</div>
            ${Object.values(e.errors).map(error=>`<div class="text-rose-300 mt-1">${esc(error)}</div>`).join('')}</div>`).join('') : '<p class="text-sm text-slate-400">Chưa phát hiện video mới sau mốc theo dõi.</p>';
        if (data.error) message(data.error, true);
    }
    window.loadYoutubeMonitor = async function () {
        if (busy || currentVipUser?.role !== 'admin') return;
        busy = true;
        try { render(await api('state'), !loaded); loaded = true; }
        catch (error) { message(error.message, true); }
        finally { busy = false; }
    };
    async function mutate(action, body) {
        if (busy) return;
        busy = true;
        try {
            const result = await api(action, body);
            if (result.settings) render(result, action === 'settings');
            message(result.message || 'Đã lưu trên Render.');
            return true;
        } catch (error) { message(error.message, true); return false; }
        finally { busy = false; }
    }
    byId('ytSettings').onsubmit = async event => {
        event.preventDefault();
        const body = {enabled:byId('ytEnabled').checked, interval_seconds:Number(byId('ytInterval').value), telegram_chat_id:byId('ytChat').value};
        for (const [field,key] of [['ytKeys','api_keys'],['ytToken','telegram_token'],['ytDiscord','discord_webhook']]) {
            const value = byId(field).value.trim();
            if (value) body[key] = key === 'api_keys' ? value.split(/\r?\n/).filter(Boolean) : value;
        }
        if (await mutate('settings', body)) {
            for (const id of ['ytKeys','ytToken','ytDiscord']) byId(id).value = '';
        }
    };
    byId('ytAddForm').onsubmit = async event => { event.preventDefault(); if (await mutate('add',{channel:byId('ytChannel').value})) byId('ytChannel').value = ''; };
    byId('ytChannels').onclick = async event => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        if (button.dataset.action === 'delete' && !confirm('Xóa kênh khỏi danh sách theo dõi?')) return;
        await mutate(button.dataset.action, {id:button.dataset.id,enabled:button.dataset.enabled === 'true'});
    };
    byId('ytTestTelegram').onclick = () => mutate('test-telegram',{});
    byId('ytTestDiscord').onclick = () => mutate('test-discord',{});
    byId('ytRefresh').onclick = () => loadYoutubeMonitor();
    setInterval(() => { if (activeMainWorkspaceTab === 'youtube_monitor' && !document.hidden) loadYoutubeMonitor(); }, 10000);
    // Auth may already have completed before this deferred script was loaded.
    switchMainWorkspaceTab(activeMainWorkspaceTab);
})();
