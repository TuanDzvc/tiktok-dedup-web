"""Zero-TikHub reader: local Node/native module, or authenticated PC relay.

Never silently falls back to a paid provider. No reader cookies, license payloads
or signing keys are returned to Python, HTTP clients, logs or cloud snapshots.
"""
import copy
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_CACHE = {}
_CHANNELS = {}


class NativeReaderError(RuntimeError):
    pass


def setting(name, default=""):
    if name in os.environ:
        return os.environ[name].strip()
    try:
        for line in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() == name:
                return value.strip().strip('\"\'')
    except OSError:
        pass
    return default


def provider_mode():
    mode = setting('DEDUP_PROVIDER', 'native').lower()
    if mode not in ('native', 'tikhub'):
        raise NativeReaderError('DEDUP_PROVIDER phải là native hoặc tikhub.')
    return mode


def bridge_token():
    explicit = setting('DEDUP_NATIVE_BRIDGE_TOKEN')
    if explicit:
        return explicit
    secret = setting('VIP_AUTH_SECRET')
    if not secret:
        return ''
    return hmac.new(secret.encode(), b'tikflow-native-reader-v1', hashlib.sha256).hexdigest()


def _local_fetch(payload):
    if sys.platform != 'win32':
        raise NativeReaderError('Bộ đọc native cần PC Windows. Cấu hình DEDUP_NATIVE_BRIDGE_URL để dùng PC từ cloud.')
    node = setting('TIKTOK_NATIVE_NODE') or shutil.which('node')
    if not node or not Path(node).is_file():
        raise NativeReaderError('Chưa tìm thấy node.exe trên PC. Cấu hình TIKTOK_NATIVE_NODE.')
    env = os.environ.copy()
    for name in ('TIKTOK_MANAGER_DATA_DIR', 'TIKTOK_NATIVE_MODULE'):
        value = setting(name)
        if value:
            env[name] = value
    try:
        result = subprocess.run(
            [node, str(ROOT / 'native_reader' / 'entry.cjs')],
            input=json.dumps(payload), capture_output=True, encoding='utf-8',
            timeout=70, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise NativeReaderError('Bộ đọc TikTok hết thời gian chờ. Không gọi API trả phí dự phòng.') from exc
    except (OSError, ValueError) as exc:
        raise NativeReaderError('Không khởi chạy được bộ đọc native. Kiểm tra Node và module TikTok Manager.') from exc
    if not isinstance(data, dict) or not data.get('success'):
        raise NativeReaderError(str(data.get('error') if isinstance(data, dict) else '') or 'Bộ đọc native không trả kết quả.')
    return data


def _remote_fetch(payload):
    base = setting('DEDUP_NATIVE_BRIDGE_URL', setting('PRIMARY_SYNC_BASE', 'https://checktiktok.site')).rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NativeReaderError('DEDUP_NATIVE_BRIDGE_URL phải là URL HTTPS của PC, không chứa token hoặc query.')
    token = bridge_token()
    if not token:
        raise NativeReaderError('Chưa cấu hình khóa xác thực bridge native.')
    try:
        response = requests.post(base + '/api/internal/native-dedup', json=payload,
                                 headers={'x-native-reader-token': token}, timeout=90,
                                 allow_redirects=False)
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise NativeReaderError('Không kết nối được bộ đọc PC. Không dùng API trả phí thay thế.') from exc
    if response.status_code != 200 or not isinstance(data, dict) or not data.get('success'):
        raise NativeReaderError('Bộ đọc PC chưa sẵn sàng. Không dùng API trả phí thay thế.')
    return data


def fetch_native_page(channel, sec_uid='', max_cursor=0, count=18, *, local_only=False):
    count = max(1, min(50, int(count)))
    payload = {'channel': str(channel or '').lstrip('!').strip()[:500],
               'secUid': str(sec_uid or '').strip()[:500],
               'maxCursor': max(0, int(max_cursor)), 'count': count}
    if not payload['channel'] and not payload['secUid']:
        raise NativeReaderError('Cần username hoặc secUid TikTok.')
    key = (payload['channel'].lower(), payload['secUid'], payload['maxCursor'], count, local_only)
    # The original reader is one account: serialize calls and collapse identical
    # concurrent scans. Cache is process-local, short lived and never synced.
    if not _LOCK.acquire(timeout=15):
        raise NativeReaderError('Bộ đọc đang bận. Vui lòng thử lại sau.')
    try:
        now = time.monotonic()
        cached = _CACHE.get(key)
        if cached and now - cached[0] < 30:
            return copy.deepcopy(cached[1])
        if not local_only and sys.platform != 'win32':
            data = _remote_fetch(payload)
        else:
            data = _local_fetch(payload)
        if not isinstance(data.get('rawItems'), list) or not data.get('secUid'):
            raise NativeReaderError('Bộ đọc trả dữ liệu thiếu danh sách bài hoặc secUid.')
        channel_key = str(data['secUid'])
        if isinstance(data.get('channel'), dict):
            _CHANNELS[channel_key] = data['channel']
        elif channel_key in _CHANNELS:
            data['channel'] = copy.deepcopy(_CHANNELS[channel_key])
        if len(_CACHE) >= 128:
            _CACHE.pop(next(iter(_CACHE)))
        if len(_CHANNELS) > 256:
            _CHANNELS.pop(next(iter(_CHANNELS)))
        _CACHE[key] = (time.monotonic(), copy.deepcopy(data))
        return data
    finally:
        _LOCK.release()
