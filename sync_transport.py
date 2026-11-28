"""Conditional, compressed sync transport; leaves conflict resolution unchanged."""
import copy
import gzip
import hashlib
import json
import threading
import time
import zlib


def content_hash(snapshot):
    if not isinstance(snapshot, dict):
        return ''
    body = {key: value for key, value in snapshot.items() if key != 'exported_at'}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def wire_response(snapshot, role, request_headers, method='GET'):
    digest = content_hash(snapshot)
    headers = {'ETag':'W/"' + digest + '"', 'X-Snapshot-Hash':digest,
               'X-Sync-Protocol':'2', 'Cache-Control':'no-store', 'Vary':'Accept-Encoding'}
    candidates = request_headers.get('if-none-match', '').split(',')
    if any(value.strip() in ('*', digest, '"'+digest+'"', 'W/"'+digest+'"') for value in candidates):
        return 304, b'', headers
    if method == 'HEAD':
        return 200, b'', headers
    payload = json.dumps({'success':True, 'role':role, 'snapshot':snapshot},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if 'gzip' in request_headers.get('accept-encoding', '').lower() and len(payload) >= 1024:
        payload = gzip.compress(payload, compresslevel=5)
        headers['Content-Encoding'] = 'gzip'
    return 200, payload, headers


def decode_body(raw, encoding='', limit=32_000_000):
    if len(raw) > limit:
        raise ValueError('sync_body_too_large')
    if encoding.lower() == 'gzip':
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = decoder.decompress(raw, limit + 1)
        except zlib.error as exc:
            raise ValueError('invalid_compressed_sync_body') from exc
        if len(raw) > limit or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid_compressed_sync_body')
    elif encoding:
        raise ValueError('unsupported_sync_encoding')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('invalid_sync_object')
    return data


class ConditionalJSONSnapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.last_status = ''

    def read(self, http, url, headers):
        with self.lock:
            previous = self.cache.get(url)
            request_headers = dict(headers)
            if previous:
                request_headers['If-None-Match'] = previous[0]
            response = http.get(url, params={'t':time.time_ns()}, headers=request_headers, timeout=25)
            if response.status_code == 304 and previous:
                self.last_status = 'not_modified'
                return copy.deepcopy(previous[1])
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    etag = response.headers.get('ETag') or response.headers.get('etag')
                    if etag:
                        self.cache = {url:(etag, copy.deepcopy(data))}
                    else:
                        self.cache.pop(url, None)
                    self.last_status = 'downloaded'
                    return data
            self.last_status = 'unavailable'
            return None


def pull_peer(http, url, headers, local_snapshot, durable_ok):
    """Probe with HEAD. Old peers use shared durable storage, not repeated full GETs."""
    try:
        probe = http.head(url, headers=headers, timeout=8)
        modern = probe.status_code == 200 and probe.headers.get('X-Sync-Protocol') == '2'
        if modern and probe.headers.get('X-Sync-Self-Echo') == '1':
            return {'ok':True, 'self_echo':True, 'modern':True}
        if not modern and durable_ok:
            return {'ok':False, 'skipped':'shared_durable_store', 'modern':False}
        remote_hash = probe.headers.get('X-Snapshot-Hash', '') if modern else ''
        local_hash = content_hash(local_snapshot)
        if remote_hash and remote_hash == local_hash:
            return {'ok':True, 'unchanged':True, 'hash':remote_hash, 'modern':modern}
        get_headers = dict(headers, **{'If-None-Match':'W/"'+local_hash+'"'})
        response = http.get(url, headers=get_headers, timeout=20)
        if response.status_code == 304:
            return {'ok':True, 'unchanged':True, 'hash':local_hash, 'modern':modern}
        if response.status_code != 200:
            return {'ok':False, 'error':'GET HTTP '+str(response.status_code), 'modern':modern}
        data = response.json()
        if data.get('self_echo'):
            return {'ok':True, 'self_echo':True, 'modern':modern}
        remote = data.get('snapshot')
        if data.get('success') and isinstance(remote, dict):
            return {'ok':True, 'snapshot':remote, 'hash':content_hash(remote), 'modern':modern}
        return {'ok':False, 'error':'invalid_peer_snapshot', 'modern':modern}
    except Exception as exc:
        return {'ok':False, 'error':type(exc).__name__, 'modern':False}


def push_peer(http, url, headers, snapshot, peer):
    if not peer.get('ok') or peer.get('self_echo'):
        return {'ok':False, 'skipped':True}
    if peer.get('hash') == content_hash(snapshot):
        return {'ok':True, 'skipped':True, 'changed':False}
    if peer.get('modern'):
        raw = json.dumps({'snapshot':snapshot}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        payload = gzip.compress(raw, compresslevel=5)
        response = http.post(url, headers={**headers, 'Content-Type':'application/json', 'Content-Encoding':'gzip'},
                             data=payload, timeout=25)
    else:
        response = http.post(url, headers=headers, json={'snapshot':snapshot}, timeout=25)
    if response.status_code != 200:
        return {'ok':False, 'error':'POST HTTP '+str(response.status_code)}
    data = response.json()
    return {'ok':data.get('success') is True, 'changed':bool(data.get('changed'))}
