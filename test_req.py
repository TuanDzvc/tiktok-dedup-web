import requests
import re

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9'
}
r = requests.get('https://www.tiktok.com/@user7745294847762', headers=headers, timeout=12)
print('requests status:', r.status_code, 'len:', len(r.text))
m = re.search(r'"secUid":"([^"]+)"', r.text)
print('secUid found:', bool(m))
if m:
    print('secUid:', m.group(1))
