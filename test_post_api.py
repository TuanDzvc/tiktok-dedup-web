from curl_cffi import requests
import json
import re
import time

s = requests.Session(impersonate="chrome120")
username = "user7745294847762"

# 1. Load user profile
r = s.get(f"https://www.tiktok.com/@{username}")
m = re.search(r'"secUid":"([^"]+)"', r.text)
sec_uid = m.group(1) if m else ""
print("sec_uid:", sec_uid)

# Let's inspect cookies
print("Cookies:", s.cookies.get_dict())

# Let's test /api/post/item_list/
headers = {
    "Referer": f"https://www.tiktok.com/@{username}",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

params = {
    "aid": "1988",
    "app_language": "en",
    "app_name": "tiktok_web",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_online": "true",
    "browser_platform": "Win32",
    "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "channel": "tiktok_web",
    "cookie_enabled": "true",
    "count": "18",
    "cursor": "0",
    "device_platform": "web_pc",
    "focus_state": "true",
    "from_page": "user",
    "history_len": "2",
    "is_fullscreen": "false",
    "is_page_visible": "true",
    "language": "en",
    "os": "windows",
    "priority_region": "",
    "referer": "",
    "region": "US",
    "screen_height": "1080",
    "screen_width": "1920",
    "secUid": sec_uid,
    "tz_name": "UTC",
    "webcast_language": "en"
}

r2 = s.get("https://www.tiktok.com/api/post/item_list/", params=params, headers=headers)
print("api/post/item_list status:", r2.status_code, "len:", len(r2.text))
if len(r2.text) > 0:
    print("Preview:", r2.text[:300])
    try:
        d = r2.json()
        print("Item count:", len(d.get("itemList", [])))
    except Exception as e:
        print("JSON err:", e)
