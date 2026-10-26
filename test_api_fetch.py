from curl_cffi import requests
import urllib.parse
import time
import random
import string
import json
import re

username = "user7745294847762"
profile_url = f"https://www.tiktok.com/@{username}"
s = requests.Session(impersonate="chrome120")

# First get profile with headers
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/"
}
r1 = s.get(profile_url, headers=headers)
print("Profile status:", r1.status_code, "Length:", len(r1.text))
m = re.search(r'"secUid":"([^"]+)"', r1.text)
sec_uid = m.group(1) if m else ""
print("sec_uid:", sec_uid)

if sec_uid:
    vfp = "verify_" + "".join(random.choices(string.hexdigits, k=7))
    cursor = int(time.time() * 1000)
    query_params = {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": "15",
        "cursor": cursor,
        "device_id": str(random.randint(10**18, 10**19)),
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
        "type": "1",
        "tz_name": "UTC",
        "verifyFp": vfp,
        "webcast_language": "en",
    }
    api_url = f"https://www.tiktok.com/api/creator/item_list/?{urllib.parse.urlencode(query_params)}"
    r2 = s.get(api_url, headers={"Referer": profile_url}, timeout=15)
    print("Creator item_list status:", r2.status_code, "text length:", len(r2.text))
    if r2.text:
        try:
            d = r2.json()
            items = d.get("itemList") or []
            print("Items count:", len(items))
            if items:
                print("First video:", items[0].get("id"))
        except Exception as e:
            print("JSON error:", e, r2.text[:200])
    
    # Try api/post/item_list/ as well
    api_url2 = f"https://www.tiktok.com/api/post/item_list/?{urllib.parse.urlencode(query_params)}"
    r3 = s.get(api_url2, headers={"Referer": profile_url}, timeout=15)
    print("Post item_list status:", r3.status_code, "text length:", len(r3.text))
    if r3.text:
        try:
            d = r3.json()
            items = d.get("itemList") or []
            print("Post Items count:", len(items))
            if items:
                print("First video:", items[0].get("id"))
        except Exception as e:
            print("Post JSON error:", e, r3.text[:200])
