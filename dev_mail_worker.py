"""Start a managed Windows Mail worker on the development PC without a BAT.

Render remains the queue owner. Credentials live only in the worker's DPAPI
config; metadata contains a public worker id. Never execute downloaded scripts.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests

_LOCK = threading.Lock()


def _metadata_path():
    return Path(os.environ['LOCALAPPDATA']) / 'TikFlow' / 'MailDevWorker' / 'state.json'


def current_dev_id():
    try:
        value = json.loads(_metadata_path().read_text(encoding='utf-8'))['id']
        return value if re.fullmatch(r'[a-f0-9]{24}', str(value)) else ''
    except (OSError, ValueError, KeyError):
        return ''


def _running(root):
    try:
        pid = int((root / 'worker.pid').read_text())
        proc = psutil.Process(pid)
        args = proc.cmdline()
        return proc.is_running() and any(str(root / 'mail_worker.ps1').lower() == a.lower() for a in args) and '-JobPath' not in args
    except (OSError, ValueError, psutil.Error):
        return False


def _snapshot(origin, authorization):
    response = requests.get(origin + '/api/outlook-oauth/state', headers={'Authorization': authorization}, timeout=20)
    response.raise_for_status()
    return response.json()


def ensure_dev_worker(repo, origin, authorization):
    if os.name != 'nt':
        raise ValueError('PC dev cần chạy Windows để mở trình duyệt cục bộ.')
    with _LOCK:
        metadata = _metadata_path()
        try:
            wid = json.loads(metadata.read_text(encoding='utf-8'))['id']
        except (OSError, ValueError, KeyError):
            wid = ''
        if not re.fullmatch(r'[a-f0-9]{24}', str(wid)):
            wid = ''
        state = _snapshot(origin, authorization)
        existing = next((w for w in state.get('workers', []) if w['id'] == wid and not w.get('revoked')), None)
        root = Path(os.environ['LOCALAPPDATA']) / 'TikFlow' / 'MailWorker' / (wid or 'unpaired')
        config = root / 'config.bin'
        if not existing or not config.is_file():
            response = requests.post(origin + '/api/outlook-oauth/worker/download', headers={'Authorization': authorization}, json={}, timeout=25)
            response.raise_for_status()
            # Read only the scoped pairing fields; never execute the BAT.
            wid_match = re.search(r'set "TF_MAIL_ID=([a-f0-9]{24})"', response.text)
            token_match = re.search(r'set "TF_MAIL_TOKEN=([A-Za-z0-9_-]{30,100})"', response.text)
            if not wid_match or not token_match:
                raise ValueError('Không tạo được phiên tự động cho PC dev.')
            wid = wid_match.group(1)
            root = Path(os.environ['LOCALAPPDATA']) / 'TikFlow' / 'MailWorker' / wid
            root.mkdir(parents=True, exist_ok=True)
            config = root / 'config.bin'
            protect = """$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Security;
$data=[Console]::In.ReadToEnd()|ConvertFrom-Json;
$bytes=[Text.Encoding]::UTF8.GetBytes(($data.config|ConvertTo-Json -Compress));
$sealed=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser);
[IO.File]::WriteAllBytes($data.path,$sealed)"""
            result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-Command', protect],
                input=json.dumps({'path': str(config), 'config': {'origin': origin, 'id': wid, 'token': token_match.group(1)}}),
                text=True, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise ValueError('Không lưu được cấu hình mã hóa của PC dev.')
            metadata.parent.mkdir(parents=True, exist_ok=True)
            temporary = metadata.with_suffix('.tmp')
            temporary.write_text(json.dumps({'id': wid}), encoding='utf-8')
            temporary.replace(metadata)
        # Each child loads the current source; leave any running job untouched.
        for name in ('mail_worker.ps1', 'mail_cdp_worker.cs'):
            target = root / name
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_bytes((Path(repo) / 'static' / name).read_bytes())
            temporary.replace(target)
        if not _running(root):
            (root / 'stop.flag').unlink(missing_ok=True)
            subprocess.Popen(['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(root / 'mail_worker.ps1'), '-ConfigPath', str(config)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = _snapshot(origin, authorization)
            if any(w['id'] == wid and w.get('online') and not w.get('revoked') for w in state.get('workers', [])):
                return {'success': True, 'worker_id': wid, 'name': 'PC dev (máy chủ)', 'online': True}
            time.sleep(1)
        raise ValueError('PC dev chưa kết nối được hàng đợi. Bấm Bắt Đầu để thử lại; không cần mở BAT.')
