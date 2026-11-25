"""Independent signing/guest reader experiment. Never imports Manager code."""
import argparse
import gzip
import json
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent


def independence_guard(event, args):
    if event in ('open', 'ctypes.dlopen') and args and isinstance(args[0], (str, bytes)):
        value = str(args[0]).replace('\\', '/').lower()
        if any(marker in value for marker in ('/tiktokmanager/', 'hwid_native', '/checktrungstandalone/')):
            raise RuntimeError('Forbidden Manager dependency access: ' + event)


sys.addaudithook(independence_guard)
sys.path.insert(0, str(ROOT / 'vendor' / 'tiktok-signer'))
from tiktok_signer import DeviceProfile, TikTokSigner
from curl_cffi import requests

STATE = ROOT / '.private' / 'session.json'
PROFILE = 'android37'
CLIENTS = {
    'android37': {'version_name':'37.0.4','version_code':'370004','manifest':'2023700040',
        'model':'2203121C','brand':'Xiaomi','os':'9','os_api':'28','language':'vi',
        'sdk':'v05.01.02-alpha.7-ov-android','sdk_code':83952160,'lc_id':2142840551},
    'android35': {'version_name':'35.4.2','version_code':'350402','manifest':'2023504020',
        'model':'Pixel 4 XL','brand':'google','os':'10','os_api':'29','language':'en',
        'sdk':'v05.00.03-ov-android','sdk_code':167773760,'lc_id':1611921764},
}
CLIENTS['official47'] = {**CLIENTS['android35'], 'aid':1180,'app_name':'trill',
    'package':'com.ss.android.ugc.trill','version_name':'47.9.3','version_code':'470903',
    'manifest':'2024709030','model':'SM-A156E','brand':'Samsung','os':'15','os_api':'35'}
UA = ('com.zhiliaoapp.musically/2023700040 (Linux; U; Android 9; vi_VN; '
      '2203121C; Build/PQ3A.190705.09121607;tt-ok/3.12.13.4-tiktok)')


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text('utf-8'))
    spec = CLIENTS[PROFILE]
    profile = DeviceProfile(device_id='0', device_type=spec['model'], os_version=spec['os'],
        openudid=secrets.token_hex(8), cdid=str(uuid.uuid4()))
    return {'device': profile.to_dict(), 'device_id': '0', 'iid': '0', 'cookie': '',
            'created_at': int(time.time()),
            'client_profile': PROFILE,
            'provenance': 'fresh local experiment; no imported session'}


def client_spec(state):
    return {**CLIENTS[state.get('client_profile', 'android37')], **state.get('client_overrides',{})}


def user_agent(state):
    spec = client_spec(state)
    return (f"{spec.get('package','com.zhiliaoapp.musically')}/{spec['manifest']} (Linux; U; Android {spec['os']}; "
            f"{spec['language']}_VN; {spec['model']}; Build/QQ3A.200805.001;tt-ok/3.12.13.4-tiktok)")


def save_state(state):
    STATE.parent.mkdir(exist_ok=True)
    temporary=STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(STATE)


def base_params(state):
    profile = state['device']
    spec = client_spec(state)
    now = int(time.time())
    return {
        'device_platform': 'android', 'os': 'android', 'ssmix': 'a',
        '_rticket': str(now * 1000), 'cdid': profile['cdid'], 'channel': 'googleplay',
        'aid': str(spec.get('aid',1233)), 'app_name': spec.get('app_name','musical_ly'), 'version_code': spec['version_code'],
        'version_name': spec['version_name'], 'manifest_version_code': spec['manifest'],
        'update_version_code': spec['manifest'], 'ab_version': spec['version_name'],
        'resolution': '720*1280', 'dpi': '320', 'device_type': profile['device_type'],
        'device_brand': spec['brand'], 'language': spec['language'], 'os_api': spec['os_api'], 'os_version': profile['os_version'],
        'ac': 'wifi', 'is_pad': '0', 'current_region': 'VN', 'app_type': 'normal',
        'sys_region': 'VN', 'last_install_time': str(state.get('created_at',now - 86400)),
        'timezone_name': 'Asia/Ho_Chi_Minh', 'residence': 'VN', 'app_language': spec['language'],
        'timezone_offset': '25200', 'host_abi': 'arm64-v8a', 'locale': 'vi-VN',
        'ac2': 'wifi', 'uoo': '0', 'op_region': 'VN', 'build_number': spec['version_name'],
        'region': 'VN', 'ts': str(now), 'iid': state['iid'],
        'device_id': state['device_id'], 'openudid': profile['openudid'],
    }


def sign(state, query, body=None):
    profile = DeviceProfile.load(state['device'])
    spec = client_spec(state)
    return TikTokSigner.generate_headers(query, data=body,
        device_id=state['device_id'], device=profile, cookie=state.get('cookie') or None,
        aid=spec.get('aid',1233),lc_id=spec['lc_id'],sdk_ver=spec['sdk'],sdk_ver_code=spec['sdk_code'],
        version_name=spec['version_name'],version_code=spec['manifest'])


def summarize(response):
    try: data = response.json()
    except ValueError: data = None
    return {'http_status': response.status_code, 'body_bytes': len(response.content),
            'json_keys': list(data)[:15] if isinstance(data, dict) else None,
            'api_status': data.get('status_code') if isinstance(data, dict) else None,
            'captcha_header': bool(response.headers.get('bdturing-verify')),
            'server_time': data.get('server_time') if isinstance(data, dict) else None}


def register(state, host='log.tiktokv.com', wire='ttencrypt'):
    profile = state['device']; params = base_params(state)
    spec = client_spec(state)
    params.update({'app_id': str(spec.get('aid',1233)), 'device_id': '0', 'iid': '0'})
    header = {
        'display_name': 'TikTok', 'aid': spec.get('aid',1233), 'channel': 'googleplay',
        'package': spec.get('package','com.zhiliaoapp.musically'), 'version_name': spec['version_name'],
        'version_code': int(spec['version_code']), 'manifest_version_code': int(spec['manifest']),
        'update_version_code': int(spec['manifest']), 'os': 'Android', 'os_version': spec['os'],
        'os_api': int(spec['os_api']), 'device_model': profile['device_type'], 'device_brand': spec['brand'],
        'openudid': profile['openudid'], 'clientudid': profile['cdid'],
        'language': spec['language'], 'resolution': '1280x720', 'density_dpi': 320,
        'timezone': 7, 'tz_name': 'Asia/Ho_Chi_Minh', 'tz_offset': 25200,
        'carrier': '', 'access': 'wifi', 'not_request_sender': 0,
        'app_version': spec['version_name'], 'app_version_minor': '0',
        'sdk_version': '2.15.0', 'sdk_version_code': '2150', 'sdk_target_version': 29,
        'device_manufacturer': spec['brand'], 'cpu_abi': 'arm64-v8a', 'release_build': spec['version_name'],
        'display_density': 'xhdpi', 'cdid': profile['cdid'], 'device_id': '0', 'install_id': '0',
        'region': 'VN', 'sim_region': 'VN', 'app_language': spec['language'], 'sys_language': spec['language'],
        'new_user_mode': 1,
    }
    payload = {'magic_tag': 'ss_app_log', 'header': header, '_gen_time': int(time.time() * 1000), 'event_filter': 1}
    body = TikTokSigner.encrypt(payload) if wire == 'ttencrypt' else gzip.compress(json.dumps(payload, separators=(',', ':')).encode(), mtime=0)
    query = urlencode(params)
    headers = sign(state, query, body)
    headers.update({'user-agent': user_agent(state), 'accept-encoding': 'gzip',
                    'content-type': 'application/octet-stream;tt-data=a'})
    if wire == 'gzip':
        headers.update({'content-type': 'application/octet-stream;tt-data=b', 'content-encoding': 'gzip', 'x-bd-content-encoding': 'gzip'})
    response = requests.post(f'https://{host}/service/2/device_register/?{query}',
                             data=body, headers=headers, timeout=25)
    report = {'wire': wire, 'host': host,'client_profile':state.get('client_profile','android37'), **summarize(response)}
    try: data = response.json()
    except ValueError: data = {}
    did = str(data.get('device_id_str') or data.get('device_id') or '0')
    iid = str(data.get('install_id_str') or data.get('install_id') or '0')
    report['issued_device'] = did != '0' and iid != '0'
    report['device_id_is_zero'] = did == '0'
    report['install_id_is_zero'] = iid == '0'
    if report['issued_device']:
        state.update({'device_id': did, 'iid': iid})
        state['device']['device_id'] = did
        # Only cookies returned by this fresh registration belong to this state.
        state['cookie'] = '; '.join(k + '=' + v for k, v in response.cookies.items())
    save_state(state)
    return report


def scoped_state(state,host,path):
    """Select only cookies whose domain/path match this exact request."""
    if 'cookies' not in state:return state
    return {**state,'cookie':'; '.join(c['name']+'='+c['value'] for c in state['cookies']
        if (host==c['domain'] or host.endswith('.'+c['domain'])) and path.startswith(c.get('path','/')))}


def mobile(state, host, path, extra):
    if not any(host.endswith('.'+domain) for domain in ('tiktokv.us','tiktokv.eu','tiktokv.com')):
        raise ValueError('Only TikTok API hosts are allowed')
    state=scoped_state(state,host,path)
    params = {**base_params(state), **extra}; query = urlencode(params)
    headers = sign(state, query)
    headers.update({'user-agent': user_agent(state), 'accept-encoding': 'gzip, deflate, br',
                    'sdk-version': '2', 'passport-sdk-version': '6031490',
                    'x-tt-store-region': state.get('store_region','vn'),
                    'x-tt-store-region-src': 'uid' if 'sessionid=' in state.get('cookie','') else 'did',
                    'x-tt-dm-status': 'login=1;ct=1;rt=1' if 'sessionid=' in state.get('cookie','') else 'login=0;ct=1;rt=7',
                    'x-ss-dp':str(client_spec(state).get('aid',1233))})
    response = requests.get(f'https://{host}{path}?{query}', headers=headers, timeout=25,allow_redirects=False)
    return response


def resolve_public(username):
    username = username.strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9._]{1,64}', username):
        raise ValueError('Invalid username')
    response = requests.get('https://www.tiktok.com/@' + username,
                            impersonate='chrome', timeout=25)
    match = re.search(r'"secUid":"([^"]+)"', response.text)
    return (match.group(1) if match else ''), summarize(response)


def offline_check():
    # No network; all algorithms execute from the vendored readable Python source.
    profile = DeviceProfile(device_id='1234567890123456789', openudid='0123456789abcdef', cdid='00000000-0000-4000-8000-000000000001')
    query = 'aid=1233&device_id=1234567890123456789&version_name=37.0.4'
    headers = TikTokSigner.generate_headers(query, device=profile, unix=1795000000)
    assert headers['x-khronos'] == '1795000000'
    assert headers['x-ss-req-ticket'] == '1795000000000'
    assert re.fullmatch(r'[0-9a-f]{52}', headers['x-gorgon'])
    payload = {'magic_tag': 'ss_app_log', 'header': {'aid': 1233}}
    encrypted = TikTokSigner.encrypt(payload)
    assert json.loads(TikTokSigner.decrypt(encrypted)) == payload
    return {'signer_executes_from_source': True, 'ttencrypt_roundtrip': True,
            'signature_lengths': {k: len(v) for k, v in headers.items()},
            'manager_module_loaded': False, 'manager_session_imported': False,
            'live_acceptance_proven': False}


def main():
    global STATE, PROFILE
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['offline', 'register', 'probe'])
    parser.add_argument('--username', default='seitezhm99d')
    parser.add_argument('--sec-uid', default='')
    parser.add_argument('--host', default='api16-normal-c-alisg.tiktokv.com')
    parser.add_argument('--register-host', default='log.tiktokv.com')
    parser.add_argument('--wire', choices=['ttencrypt', 'gzip'], default='ttencrypt')
    parser.add_argument('--profile', choices=list(CLIENTS), default='android37')
    parser.add_argument('--state', type=Path, help='Explicit local state file; do not share credentials')
    args = parser.parse_args()
    PROFILE=args.profile
    if PROFILE != 'android37':
        STATE=ROOT/'.private'/f'{PROFILE}-session.json'
    if args.state:
        STATE=args.state.resolve()
    if args.action == 'offline': print(json.dumps(offline_check(), indent=2)); return
    state = load_state()
    if args.action == 'register':
        print(json.dumps(register(state, args.register_host, args.wire), indent=2)); return
    sec_uid = args.sec_uid
    if not sec_uid:
        sec_uid, report = resolve_public(args.username)
        print(json.dumps({'stage': 'public_profile', 'resolved': bool(sec_uid), **report}), flush=True)
    if not sec_uid:
        response = mobile(state, args.host.replace('api16-', 'search16-'), '/aweme/v1/discover/search/',
                          {'keyword': args.username, 'count': 10, 'cursor': 0, 'type': 1,
                           'search_source': 'normal_search', 'hot_search': 0, 'is_pull_refresh': 0, 'search_id': ''})
        print(json.dumps({'stage': 'guest_search', **summarize(response)}), flush=True)
        try: data = response.json()
        except ValueError: data = None
        def find(obj):
            if isinstance(obj, dict):
                if str(obj.get('unique_id', '')).lower() == args.username.lower() and obj.get('sec_uid'): return obj['sec_uid']
                for v in obj.values():
                    found = find(v)
                    if found: return found
            elif isinstance(obj, list):
                for v in obj:
                    found = find(v)
                    if found: return found
            return ''
        sec_uid = find(data)
    if not sec_uid:
        raise SystemExit(2)
    response = mobile(state, args.host, '/aweme/v1/aweme/post/',
                      {'sec_user_id': sec_uid, 'count': 18, 'max_cursor': 0, 'source': 0,
                       'sort_type': 0, 'user_avatar_shrink': '144_144', 'video_cover_shrink': '372_495'})
    report = {'stage': 'posts', **summarize(response)}
    usable = False
    try:
        data = response.json(); posts = data.get('aweme_list')
        report['posts'] = len(posts) if isinstance(posts, list) else None
        if isinstance(posts, list):
            report['has_group_id'] = sum('group_id' in p for p in posts)
            report['duplicates'] = sum(bool(p.get('group_id')) and str(p['group_id']) != str(p.get('aweme_id')) for p in posts)
            usable = bool(posts) and report['has_group_id'] == len(posts) and data.get('status_code') == 0
    except ValueError: pass
    report['check_trung_verified'] = usable
    print(json.dumps(report), flush=True)
    if not usable:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
