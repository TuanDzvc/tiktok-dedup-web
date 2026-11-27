"""Cloud-only YouTube discovery, adapted from YouTubePipelineWorker in main - Copy.py.

No desktop, download or upload dependencies. One Render process owns the private
Storage object; the PC forwards requests instead of running another scheduler.
"""
import copy
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests


class MonitorError(Exception):
    pass


def channel_reference(raw):
    value = str(raw or '').strip()
    if re.fullmatch(r'UC[\w-]{22}', value):
        return 'id', value
    if value.startswith('@') and re.fullmatch(r'@[\w.\-\u0080-\uffff]+', value):
        return 'forHandle', value
    parsed = urlparse(value if '://' in value else 'https://' + value)
    if parsed.scheme != 'https' or parsed.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        raise MonitorError('Dán Channel ID UC…, @handle hoặc link kênh YouTube hợp lệ.')
    parts = parsed.path.strip('/').split('/')
    if parts[0].startswith('@'):
        return channel_reference(parts[0])
    if len(parts) >= 2 and parts[0] == 'channel':
        return channel_reference(parts[1])
    if len(parts) >= 2 and parts[0] == 'user' and re.fullmatch(r'[\w.-]+', parts[1]):
        return 'forUsername', parts[1]
    raise MonitorError('Dùng link /channel/UC… hoặc @handle, không dùng link video.')


def discord_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or parsed.hostname not in ('discord.com', 'discordapp.com')
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[\w.-]+', parsed.path)
            or parsed.query or parsed.fragment):
        raise MonitorError('Webhook phải là URL Discord https://discord.com/api/webhooks/…')
    return value


class YouTubeSource:
    def __init__(self, session=None):
        self.http = session or requests.Session()
        self.cooldown = {}
        self.key_index = 0

    def api(self, resource, params, keys):
        available = [key for key in keys if self.cooldown.get(key, 0) <= time.time()]
        if not available:
            raise MonitorError('Chưa có API key khả dụng; thử RSS/web dự phòng.')
        start = self.key_index % len(available)
        self.key_index += 1
        for key in available[start:] + available[:start]:
            try:
                response = self.http.get('https://www.googleapis.com/youtube/v3/' + resource,
                                         params={**params, 'key': key}, timeout=12)
                if response.status_code in (400, 401, 403, 429):
                    self.cooldown[key] = time.time() + 300
                    continue
                if not response.ok:
                    continue
                return response.json()
            except (requests.RequestException, ValueError):
                continue
        raise MonitorError('YouTube API không trả dữ liệu. Kiểm tra key, quyền API hoặc quota.')

    def resolve(self, raw, keys):
        kind, value = channel_reference(raw)
        if keys:
            try:
                payload = self.api('channels', {'part': 'snippet,contentDetails', kind: value}, keys)
                items = payload.get('items') or []
                if not items:
                    raise MonitorError('Không tìm thấy kênh YouTube này.')
                item = items[0]
                return {'id': item['id'], 'title': item['snippet']['title'],
                        'playlist': item['contentDetails']['relatedPlaylists']['uploads']}
            except MonitorError:
                pass
        if kind == 'id':
            return {'id': value, 'title': value, 'playlist': 'UU' + value[2:]}
        path = value if kind == 'forHandle' else 'user/' + value
        try:
            response = self.http.get('https://www.youtube.com/' + path,
                                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            response.raise_for_status()
            match = re.search(r'"(?:externalId|channelId)"\s*:\s*"(UC[\w-]{22})"', response.text)
            if not match:
                match = re.search(r'youtube\.com/channel/(UC[\w-]{22})', response.text)
            if match:
                return {'id': match[1], 'title': value, 'playlist': 'UU' + match[1][2:]}
        except requests.RequestException:
            pass
        raise MonitorError('Không xác định được kênh. Thử Channel ID UC… hoặc thêm YouTube API key.')

    def fetch(self, channel, keys):
        api_error = ''
        if keys:
            try:
                data = self.api('playlistItems', {'part': 'snippet,contentDetails',
                                'playlistId': channel['playlist'], 'maxResults': 50}, keys)
                rows = []
                for item in data.get('items') or []:
                    sn, cd = item.get('snippet') or {}, item.get('contentDetails') or {}
                    vid = cd.get('videoId') or (sn.get('resourceId') or {}).get('videoId')
                    if vid and re.fullmatch(r'[\w-]{11}', vid):
                        rows.append({'id': vid, 'title': sn.get('title') or vid,
                                     'published_at': cd.get('videoPublishedAt') or sn.get('publishedAt') or ''})
                return rows, 'YouTube API', ''
            except MonitorError as exc:
                api_error = str(exc)
        try:
            response = self.http.get('https://www.youtube.com/feeds/videos.xml',
                                     params={'channel_id': channel['id']}, timeout=12)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            if root.tag != '{http://www.w3.org/2005/Atom}feed':
                raise ValueError('Not an Atom feed')
            ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
            rows = []
            for entry in root.findall('a:entry', ns):
                vid = entry.findtext('yt:videoId', '', ns)
                if re.fullmatch(r'[\w-]{11}', vid):
                    rows.append({'id': vid, 'title': entry.findtext('a:title', vid, ns),
                                 'published_at': entry.findtext('a:published', '', ns)})
            return rows, 'RSS', api_error
        except (requests.RequestException, ET.ParseError, ValueError):
            pass
        ids = []
        for tab in ('videos', 'shorts'):
            try:
                response = self.http.get('https://www.youtube.com/channel/' + channel['id'] + '/' + tab,
                                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
                response.raise_for_status()
                ids.extend(list(dict.fromkeys(re.findall(r'"videoId"\s*:\s*"([\w-]{11})"', response.text)))[:15])
            except requests.RequestException:
                pass
        if ids:
            return [{'id': vid, 'title': vid, 'published_at': ''} for vid in dict.fromkeys(ids)], 'Web dự phòng', api_error
        raise MonitorError('Không đọc được YouTube qua API, RSS và web; sẽ tự thử lại.')


class CloudStore:
    """Separate private object: never included in PC snapshots or public responses."""
    def __init__(self, cfg, bucket):
        if not cfg.get('configured'):
            raise MonitorError('Chưa có kho lưu cloud. Chưa thể bật theo dõi bền vững.')
        self.url = cfg['url'] + '/storage/v1/object/' + bucket + '/youtube_monitor_v1.json'
        self.headers = {'apikey': cfg['key'], 'Authorization': 'Bearer ' + cfg['key']}

    def load(self):
        try:
            response = requests.get(self.url, params={'t': time.time_ns()}, headers={**self.headers, 'Cache-Control': 'no-cache'}, timeout=20)
            if response.status_code in (400, 404):
                # Storage uses 400 for a missing object; do not treat auth/bucket errors as empty.
                error = response.json()
                if str(error.get('statusCode')) == '404' or error.get('error') == 'not_found':
                    return {'version': 1, 'settings': {}, 'channels': {}, 'events': []}
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get('channels'), dict) or not isinstance(data.get('settings'), dict):
                raise ValueError('Invalid monitor store')
            return data
        except (requests.RequestException, ValueError, AttributeError):
            raise MonitorError('Không đọc được kho theo dõi cloud; đang chặn ghi để bảo toàn dữ liệu.') from None

    def save(self, data):
        try:
            response = requests.post(self.url, headers={**self.headers, 'Content-Type': 'application/json',
                                     'x-upsert': 'true', 'cache-control': '0'},
                                     data=json.dumps(data, ensure_ascii=False).encode(), timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            raise MonitorError('Chưa lưu được vào cloud; hệ thống sẽ thử lại, không xác nhận đã lưu.') from None


class YouTubeMonitor:
    def __init__(self, store, source=None, deliver=None):
        self.store = store
        self.source = source or YouTubeSource()
        self.deliver = deliver or self._deliver
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.data = None
        self.error = ''
        self.last_tick = 0
        self.thread = None
        self.pending_ack = None

    def ensure_loaded(self):
        if self.data is None:
            self.data = self.store.load()

    def commit(self, data):
        self.store.save(data)
        self.data = data

    def view(self):
        with self.lock:
            self.ensure_loaded()
            data = copy.deepcopy(self.data)
        settings = data['settings']
        safe = {k: settings.get(k, default) for k, default in
                [('enabled', False), ('interval_seconds', 60), ('telegram_chat_id', '')]}
        safe.update(api_key_count=len(settings.get('api_keys') or []),
                    has_telegram_token=bool(settings.get('telegram_token')),
                    has_discord_webhook=bool(settings.get('discord_webhook')))
        channels = [{**{k: v for k, v in row.items() if k != 'seen'},
                     'seen_count': len(row.get('seen') or [])} for row in data['channels'].values()]
        return {'success': True, 'settings': safe, 'channels': channels,
                'events': data.get('events', [])[-150:][::-1], 'runtime': 'Render',
                'last_tick': self.last_tick, 'error': self.error}

    def action(self, action, body):
        with self.lock:
            self.ensure_loaded()
            if self.pending_ack is not None:
                self.commit(self.pending_ack)
                self.pending_ack = None
            data = copy.deepcopy(self.data)
            settings = data['settings']
            if action == 'settings':
                settings['enabled'] = bool(body.get('enabled'))
                settings['interval_seconds'] = max(30, min(86400, int(body.get('interval_seconds') or 60)))
                for key, limit in [('api_keys', 20), ('telegram_token', 256), ('discord_webhook', 512)]:
                    if key not in body:
                        continue
                    if key == 'api_keys':
                        values = body[key]
                        if not isinstance(values, list) or len(values) > limit:
                            raise MonitorError('Tối đa 20 API key.')
                        settings[key] = list(dict.fromkeys(str(x).strip() for x in values if str(x).strip()))
                        if any(not re.fullmatch(r'[\w-]{20,160}', k) for k in settings[key]):
                            raise MonitorError('YouTube API key không hợp lệ.')
                    else:
                        value = str(body[key] or '').strip()
                        if len(value) > limit:
                            raise MonitorError('Thông tin cấu hình quá dài.')
                        if key == 'discord_webhook':
                            value = discord_url(value)
                        if key == 'telegram_token' and value and not re.fullmatch(r'\d+:[\w-]{20,}', value):
                            raise MonitorError('Telegram Bot Token không hợp lệ.')
                        settings[key] = value
                chat = str(body.get('telegram_chat_id') or '').strip()
                if chat and not re.fullmatch(r'-?\d+|@[\w]{5,}', chat):
                    raise MonitorError('Telegram Chat ID phải là số hoặc @tên_kênh.')
                settings['telegram_chat_id'] = chat
                if settings['enabled'] and settings.get('telegram_token') and not chat:
                    raise MonitorError('Nhập Telegram Chat ID trước khi bật theo dõi.')
            elif action == 'add':
                if len(data['channels']) >= 100:
                    raise MonitorError('Tối đa 100 kênh theo dõi.')
                resolved = self.source.resolve(body.get('channel'), settings.get('api_keys') or [])
                if resolved['id'] in data['channels']:
                    raise MonitorError('Kênh đã có trong danh sách.')
                data['channels'][resolved['id']] = {**resolved, 'enabled': True, 'baseline': False,
                        'baseline_at': 0, 'seen': [], 'last_checked': 0, 'next_check': 0, 'error': '', 'source': ''}
            elif action in ('toggle', 'delete', 'check'):
                row = data['channels'].get(str(body.get('id') or ''))
                if not row:
                    raise MonitorError('Không tìm thấy kênh.')
                if action == 'delete':
                    del data['channels'][row['id']]
                    for event in data.get('events', []):
                        if event['channel_id'] == row['id']:
                            event['pending'] = []
                elif action == 'toggle':
                    row['enabled'] = bool(body.get('enabled'))
                    row['next_check'] = 0
                else:
                    row['next_check'] = 0
                    row['force_check'] = True
            else:
                raise MonitorError('Thao tác không hợp lệ.')
            self.commit(data)
        return self.view()

    def test(self, destination):
        if destination not in ('telegram', 'discord'):
            raise MonitorError('Chọn Telegram hoặc Discord.')
        with self.lock:
            self.ensure_loaded()
            settings = copy.deepcopy(self.data['settings'])
        self.deliver(destination, settings, '✅ TikFlow: kết nối thông báo YouTube thành công.')
        return {'success': True, 'message': 'Đã gửi thông báo thử.'}

    @staticmethod
    def _deliver(destination, settings, text):
        try:
            if destination == 'telegram':
                token, chat = settings.get('telegram_token'), settings.get('telegram_chat_id')
                if not token or not chat:
                    raise MonitorError('Chưa lưu Telegram Bot Token và Chat ID.')
                response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                                         json={'chat_id': chat, 'text': text[:4000]}, timeout=15, allow_redirects=False)
                if response.status_code != 200 or not response.json().get('ok'):
                    raise MonitorError('Telegram chưa nhận tin. Kiểm tra bot, Chat ID và quyền gửi tin.')
            else:
                url = discord_url(settings.get('discord_webhook'))
                if not url:
                    raise MonitorError('Chưa lưu Discord webhook.')
                response = requests.post(url, json={'content': text[:1900], 'allowed_mentions': {'parse': []}},
                                         timeout=15, allow_redirects=False)
                if response.status_code not in (200, 204):
                    raise MonitorError('Discord chưa nhận tin. Kiểm tra webhook và quyền gửi tin.')
        except (requests.RequestException, ValueError):
            raise MonitorError('Gửi thông báo chưa thành công; sẽ tự thử lại.') from None

    def scan(self, channel_id):
        with self.lock:
            self.ensure_loaded()
            row = copy.deepcopy(self.data['channels'].get(channel_id))
            settings = copy.deepcopy(self.data['settings'])
        if not row:
            return
        if not row.get('enabled') or not (settings.get('enabled') or row.get('force_check')):
            return
        try:
            videos, source, warning = self.source.fetch(row, settings.get('api_keys') or [])
            now = time.time()
            with self.lock:
                data = copy.deepcopy(self.data)
                current = data['channels'].get(channel_id)
                if not current:
                    return
                seen = list(current.get('seen') or [])
                if current.get('baseline'):
                    for video in reversed(videos):
                        if video['id'] in seen:
                            continue
                        # Do not alert for old uploads that enter a fallback feed later.
                        try:
                            published = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00')).timestamp()
                        except (ValueError, KeyError):
                            published = now
                        if published >= current.get('baseline_at', 0) - 120:
                            destinations = []
                            if settings.get('telegram_token') and settings.get('telegram_chat_id'):
                                destinations.append('telegram')
                            if settings.get('discord_webhook'):
                                destinations.append('discord')
                            data.setdefault('events', []).append({**video, 'channel_id': channel_id,
                                'channel_title': current['title'], 'detected_at': now,
                                'url': 'https://www.youtube.com/watch?v=' + video['id'],
                                'pending': destinations, 'sent': [], 'errors': {}, 'retry_at': 0})
                else:
                    current['baseline'] = True
                    current['baseline_at'] = now
                for video in videos:
                    if video['id'] not in seen:
                        seen.append(video['id'])
                current.update(seen=seen[-1000:], last_checked=now,
                    next_check=now + settings.get('interval_seconds', 60), source=source, error='', warning=warning)
                current.pop('force_check', None)
                # Retain all undelivered events, plus recent delivered history.
                events = data.get('events', [])
                data['events'] = [e for e in events[:-150] if e.get('pending')] + events[-150:]
                self.commit(data)
        except MonitorError as exc:
            with self.lock:
                data = copy.deepcopy(self.data)
                if channel_id in data['channels']:
                    data['channels'][channel_id].update(error=str(exc), last_checked=time.time(), next_check=time.time()+60)
                    data['channels'][channel_id].pop('force_check', None)
                    self.commit(data)

    def flush(self):
        with self.lock:
            settings = copy.deepcopy(self.data['settings'])
            if not settings.get('enabled'):
                return
            pending = [(e['channel_id'], e['id'], destination) for e in self.data.get('events', [])
                       if e.get('retry_at', 0) <= time.time()
                       and self.data['channels'].get(e['channel_id'], {}).get('enabled')
                       for destination in e.get('pending', [])]
        for channel_id, video_id, destination in pending[:20]:
            if self.stop_event.is_set():
                return
            with self.lock:
                event = next((e for e in self.data['events'] if e['channel_id'] == channel_id and e['id'] == video_id), None)
                if not event or destination not in event['pending']:
                    continue
                text = f"🆕 Video YouTube mới\nKênh: {event['channel_title']}\n{event['title']}\n{event['url']}"
            error = ''
            try:
                self.deliver(destination, settings, text)
            except MonitorError as exc:
                error = str(exc)
            with self.lock:
                data = copy.deepcopy(self.data)
                event = next(e for e in data['events'] if e['channel_id'] == channel_id and e['id'] == video_id)
                if error:
                    event['errors'][destination] = error
                    event['retry_at'] = time.time() + 60
                else:
                    event['pending'].remove(destination)
                    event['sent'].append(destination)
                    event['errors'].pop(destination, None)
                try:
                    self.commit(data)
                except MonitorError:
                    # Retain the acknowledgement in RAM and persist it before sending
                    # anything else. External sendMessage has no idempotency key.
                    self.pending_ack = data
                    raise

    def tick(self):
        if not self.check_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.ensure_loaded()
                if self.pending_ack is not None:
                    self.commit(self.pending_ack)
                    self.pending_ack = None
                settings = self.data['settings']
                due = [row['id'] for row in self.data['channels'].values()
                       if row.get('enabled') and (settings.get('enabled') or row.get('force_check'))
                       and row.get('next_check', 0) <= time.time()]
            for channel_id in due:
                if self.stop_event.is_set():
                    return
                self.scan(channel_id)
            if settings.get('enabled'):
                self.flush()
            self.error = ''
            self.last_tick = time.time()
        finally:
            self.check_lock.release()

    def start(self):
        def loop():
            # Let the previous Render process finish graceful shutdown during deploy.
            if self.stop_event.wait(20):
                return
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except MonitorError as exc:
                    self.error = str(exc)
                except Exception:
                    self.error = 'Theo dõi gặp lỗi; sẽ tự thử lại. Cấu hình vẫn được lưu.'
                self.stop_event.wait(5)
        self.thread = threading.Thread(target=loop, name='youtube-monitor', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=25)
