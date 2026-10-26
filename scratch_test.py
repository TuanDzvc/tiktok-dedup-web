from curl_cffi import requests
import re
import json

s = requests.Session(impersonate='chrome120')
r = s.get('https://www.tiktok.com/@user7745294847762')
m = re.search(r'"secUid":"([^"]+)"', r.text)
sec_uid = m.group(1) if m else ''
print('secUid:', sec_uid)

# Let's inspect the Universal Data on profile page
m_rehyd = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>', r.text)
if m_rehyd:
    data = json.loads(m_rehyd.group(1))
    scope = data.get('__DEFAULT_SCOPE__', {})
    print('Scope keys:', list(scope.keys()))
    ud = scope.get('webapp.user-detail', {})
    print('ud keys:', list(ud.keys()))
    post_data = scope.get('webapp.user-post-list', {})
    print('post_data keys:', list(post_data.keys()) if post_data else 'None')

params = {
    'aid': '1988',
    'app_language': 'en',
    'app_name': 'tiktok_web',
    'count': '18',
    'cursor': '0',
    'secUid': sec_uid,
    'type': '1'
}
r2 = s.get('https://www.tiktok.com/api/post/item_list/', params=params)
print('api/post/item_list status:', r2.status_code)
d2 = r2.json() if r2.status_code == 200 else {}
print('api/post/item_list keys:', list(d2.keys()))
print('itemList count:', len(d2.get('itemList', [])))
if d2.get('itemList'):
    print('First video ID:', d2['itemList'][0].get('id'))
