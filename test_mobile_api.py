import requests
import json

sec_uid = "MS4wLjABAAAApErW5A8OnyLg4NDY4FLwHiSM-kSdoYBsQhgeUsImU09nAob15HedyMjJPo7W-jxE"

url = "https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/aweme/post/"
params = {
    "sec_user_id": sec_uid,
    "count": 20,
    "max_cursor": 0,
    "aid": "1233",
    "version_code": "300904",
    "app_name": "musical_ly",
    "channel": "googleplay",
    "device_platform": "android",
    "device_type": "Pixel 4",
    "os_version": "10"
}
headers = {
    "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 10; en_US; Pixel 4; Build/QQ3A.200805.001; Cronet/TTNetVersion:b4d74d15 2020-04-23 QuicVersion:0144d358 2020-03-24)",
    "Accept-Encoding": "gzip, deflate",
}

r = requests.get(url, params=params, headers=headers, timeout=10)
print("Status:", r.status_code, "Length:", len(r.content))
if r.status_code == 200 and r.content:
    try:
        d = r.json()
        print("Keys:", list(d.keys()))
        items = d.get("aweme_list", [])
        print("Aweme items count:", len(items))
        if items:
            print("Video 1 ID:", items[0].get("aweme_id"))
            print("Video 1 desc:", items[0].get("desc"))
    except Exception as e:
        print("JSON err:", e, r.text[:200])
