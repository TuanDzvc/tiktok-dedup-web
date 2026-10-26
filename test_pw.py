import sys
from pathlib import Path
sys.path.insert(0, 'E:/Tool/tiktok_dedup_web')
from core_dedup import fetch_external_channel_videos_playwright

channel_url = "https://www.tiktok.com/@user7745294847762"
channel_info = {}
items = fetch_external_channel_videos_playwright(channel_url, channel_info=channel_info, max_videos=18)
print("Items count from playwright:", len(items))
if items:
    print("First item:", items[0].get("id"), items[0].get("desc"))
