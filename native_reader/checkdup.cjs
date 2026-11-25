'use strict';
// Reconstructed from TikTok Manager 3.4.0: Jb, Qb, Zb, ek, tk,
// _anon_16392, _anon_16420, Ip, Xb. See README.md for provenance and native dependencies.
const https = require('node:https');
const zlib = require('node:zlib');
const crypto = require('node:crypto');

function parseUsername(input) {
  const clean = String(input || '').replace(/[\s\u00a0\u200b-\u200d\ufeff]/g, '');
  const url = clean.match(/tiktok\.com\/@([A-Za-z0-9._]+)/i);
  if (url) return url[1];
  return clean.replace(/^@+/, '').split(/[/?#]/)[0].match(/[A-Za-z0-9._]+/)?.[0] || '';
}
function detectDevice(post) {
  return post.content_original_type != null || post.shoot_tab_name ||
    (post.creation_info?.creation_used_functions || []).length > 0 ? 'phone' : 'web';
}
function detectShadow(post) {
  const status = post.status || {};
  const penalty = post.content_model?.standard_biz?.penalty_context || {};
  const reasons = [];
  if (status.is_prohibited) reasons.push('Cấm đăng');
  if (status.in_reviewing) reasons.push('Đang xét duyệt');
  if (status.self_see) reasons.push('Chỉ mình tôi');
  if (penalty.display_penalty_type) reasons.push('Án phạt');
  if (post.is_nff_or_nr === true) reasons.push('Không lên FYP');
  return { shadow: reasons.length > 0, reason: reasons.join(', ') };
}
function mapPosts(posts) {
  return (posts || []).map(post => {
    const stats = post.statistics || {};
    const groupId = post.group_id || '';
    const shadow = detectShadow(post);
    return {
      awemeId: String(post.aweme_id || ''), groupId: String(groupId),
      isDup: !!groupId && String(groupId) !== String(post.aweme_id),
      desc: post.desc || '', createTime: post.create_time || 0,
      play: stats.play_count ?? 0, like: stats.digg_count ?? 0,
      comment: stats.comment_count ?? 0, device: detectDevice(post),
      region: post.region || '', shadow: shadow.shadow, shadowReason: shadow.reason,
    };
  });
}
function findExactUser(root, username) {
  const target = username.toLowerCase();
  const stack = [root];
  while (stack.length) {
    const item = stack.pop();
    if (!item || typeof item !== 'object') continue;
    if (typeof item.unique_id === 'string' && item.unique_id.toLowerCase() === target &&
        (item.sec_uid || item.uid)) return item;
    for (const value of Object.values(item)) if (value && typeof value === 'object') stack.push(value);
  }
  return null;
}
function request(url, headers = {}, redirects = 0) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    if (target.protocol !== 'https:') return reject(new Error('Chỉ hỗ trợ HTTPS.'));
    const req = https.get(target, { headers }, res => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        if (redirects >= 4) return reject(new Error('Quá nhiều lần chuyển hướng.'));
        const next = new URL(res.headers.location, target);
        // Never forward reader credentials to a redirect host.
        if (next.host !== target.host && headers.cookie) return reject(new Error('API chuyển sang host khác.'));
        return request(next.href, headers, redirects + 1).then(resolve, reject);
      }
      const chunks = []; let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 24 * 1024 * 1024) req.destroy(new Error('Phản hồi quá lớn.'));
        else chunks.push(chunk);
      });
      res.on('error', reject);
      res.on('end', () => {
        try {
          let body = Buffer.concat(chunks);
          const encoding = res.headers['content-encoding'];
          if (encoding === 'gzip') body = zlib.gunzipSync(body);
          else if (encoding === 'br') body = zlib.brotliDecompressSync(body);
          else if (encoding === 'deflate') body = zlib.inflateSync(body);
          resolve({ status: res.statusCode, headers: res.headers, body: body.toString('utf8') });
        } catch (error) { reject(error); }
      });
    });
    req.on('error', reject);
    req.setTimeout(20000, () => req.destroy(new Error('Hết thời gian chờ TikTok (20 giây).')));
  });
}
function createReaderService(native, reader) {
  async function mobile(path, extra, search = false) {
    const stamp = Date.now();
    const params = { ...reader.params, ...extra, _rticket: String(stamp), ts: String(Math.floor(stamp / 1000)) };
    delete params.locate_item_id;
    const query = Object.entries(params).map(([key, value]) => key + '=' + encodeURIComponent(String(value))).join('&');
    const signature = JSON.parse(native.ttSign(JSON.stringify({
      params: query, payload_hash: '', cookie_hash: crypto.createHash('md5').update(reader.cookie || '', 'utf8').digest('hex'),
      device_id: reader.deviceId, aid: 1233, license_id: 1611921764,
      sdk_version_str: 'v05.00.03-ov-android', sdk_version: 167773760,
    })));
    if (!signature?.x_gorgon) throw new Error('Bộ ký request không trả về chữ ký.');
    let host = reader.host || 'api16-core-no1a.tiktokv.eu';
    if (search) host = host.replace(/^api\d+-core-/, 'search16-normal-');
    const headers = {
      host, 'accept-encoding': 'gzip, deflate, br', connection: 'keep-alive', cookie: reader.cookie,
      'passport-sdk-version': '6010290', 'sdk-version': '2', 'user-agent': reader.userAgent,
      'x-argus': signature.x_argus, 'x-bd-kmsv': '0', 'x-gorgon': signature.x_gorgon,
      'x-khronos': String(signature.x_khronos), 'x-ladon': signature.x_ladon,
      'x-ss-dp': '1233', 'x-ss-req-ticket': String(signature.x_ss_req_ticket),
      'x-ss-stub': signature.x_ss_stub, 'x-tt-store-region': 'gb', 'x-tt-store-region-src': 'uid',
      'x-vc-bdturing-sdk-version': '2.3.8.i18n',
    };
    if (!search) Object.assign(headers, { check_preload: 'true', 'rpc-persist-pyxis-policy-v-tnc': '1',
      'x-tt-dataflow-id': '134217728', 'x-tt-dm-status': 'login=1;ct=1;rt=1' });
    if (reader.xTtToken) headers['x-tt-token'] = reader.xTtToken;
    const response = await request(`https://${host}${path}?${query}`, headers);
    let data;
    try { data = JSON.parse(response.body); }
    catch { throw new Error(`Reader không trả JSON (HTTP ${response.status}${response.headers['bdturing-verify'] ? ', bdturing' : ''}). Phiên đọc có thể hết hạn.`); }
    if (response.status >= 400 || data.status_code) throw new Error(`TikTok: ${data.status_code || response.status} ${data.status_msg || ''}`.trim());
    return data;
  }
  async function resolveChannel(input) {
    const username = parseUsername(input);
    if (!username) throw new Error('Nhập @username hoặc link kênh TikTok.');
    let readerError;
    try {
      const data = await mobile('/aweme/v1/discover/search/', {
        aid: '1233', app_name: 'musical_ly', keyword: username, count: 10, cursor: 0,
        type: 1, search_source: 'normal_search', hot_search: 0, is_pull_refresh: 0, search_id: '',
      }, true);
      const user = findExactUser(data, username);
      if (user?.sec_uid) return { uid: String(user.uid || ''), secUid: user.sec_uid, uniqueId: user.unique_id || username,
        nickname: user.nickname || '', avatar: (user.avatar_thumb?.url_list || user.avatar_medium?.url_list || [])[0] || '',
        followers: user.follower_count || 0, videoCount: user.aweme_count || 0 };
    } catch (error) { readerError = error.message; }
    const response = await request(`https://www.tiktok.com/@${encodeURIComponent(username)}`, {
      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      'accept-language': 'en-US,en;q=0.9',
    });
    const secUid = response.status === 200 && response.body.match(/"secUid":"([^"]+)"/)?.[1];
    if (!secUid) throw new Error(`Không tìm được @${username}.${readerError ? ' ' + readerError : ' TikTok có thể đang chặn lượt đọc.'}`);
    const read = key => {
      const value = response.body.match(new RegExp('"' + key + '":"((?:\\\\.|[^"\\\\])*)"'))?.[1];
      if (!value) return '';
      try { return JSON.parse('"' + value + '"'); } catch { return value; }
    };
    const number = key => Number(response.body.match(new RegExp('"' + key + '":(\\d+)'))?.[1] || 0);
    return { secUid, uniqueId: read('uniqueId') || username, nickname: read('nickname'),
      avatar: read('avatarThumb'), followers: number('followerCount'), videoCount: number('videoCount') };
  }
  async function load({ channel = '', secUid = '', maxCursor = 0, count = 30 } = {}) {
    const info = secUid ? null : await resolveChannel(channel);
    const resolved = secUid || info.secUid;
    const raw = await mobile('/aweme/v1/aweme/post/', {
      source: 0, user_avatar_shrink: '144_144', video_cover_shrink: '372_495', sov_client_enable: 1,
      max_cursor: maxCursor, sec_user_id: resolved, count: Math.min(50, Math.max(1, Number(count) || 30)), sort_type: 0,
    });
    if (!Array.isArray(raw.aweme_list)) throw new Error('Reader không trả danh sách bài hợp lệ. Không thể kết luận kênh sạch.');
    return { success: true, channel: info, secUid: resolved, list: mapPosts(raw.aweme_list), rawItems: raw.aweme_list || [],
      hasMore: !!raw.has_more, maxCursor: raw.max_cursor || 0 };
  }
  return { load, resolveChannel };
}
module.exports = { parseUsername, detectDevice, detectShadow, mapPosts, findExactUser, createReaderService };
