import sys
from pathlib import Path
sys.path.insert(0, 'E:/Tool/tiktok_uploader_desktop')
from main import ConfigStore
from curl_cffi import requests
import json
import re

store = ConfigStore()
_, accs = store.load()
cookie = None
for a in accs:
    c = getattr(a, 'tiktok_cookie', '')
    if c:
        cookie = c
        break

print('Got cookie:', bool(cookie))
s = requests.Session(impersonate='chrome120')
if cookie:
    s.headers.update({
        'Cookie': cookie,
        'Referer': 'https://www.tiktok.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    })

r1 = s.get('https://www.tiktok.com/@user7745294847762')
m = re.search(r'"secUid":"([^"]+)"', r1.text)
sec_uid = m.group(1) if m else ''
print('secUid:', sec_uid)

# Check Universal Data for post list
m_rehyd = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>', r1.text)
if m_rehyd:
    data = json.loads(m_rehyd.group(1))
    scope = data.get('__DEFAULT_SCOPE__', {})
    print('Universal data scope keys:', list(scope.keys()))
    ud = scope.get('webapp.user-detail', {})
    print('User detail keys:', list(ud.keys()))
    post_list_obj = scope.get('webapp.user-post-list', {})
    if post_list_obj:
        print('Post list in Universal Data! Keys:', list(post_list_obj.keys()))
        items = post_list_obj.get('itemList', [])
        print('Universal Data items count:', len(items))

if sec_uid:
    params = {
        'aid': '1988',
        'app_language': 'en',
        'app_name': 'tiktok_web',
        'count': '18',
        'cursor': '0',
        'secUid': sec_uid,
        'type': '1',
        'device_platform': 'web_pc'
    }
    r2 = s.get('https://www.tiktok.com/api/post/item_list/', params=params)
    print('post/item_list status:', r2.status_code)
    try:
        d = r2.json()
        items = d.get('itemList', [])
        print('Items in api/post/item_list:', len(items))
        if items:
            print('First video ID:', items[0].get('id'))
    except Exception as e:
        print('JSON error:', e, r2.text[:200])
