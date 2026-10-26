import sys
sys.path.insert(0, 'E:/Tool/tiktok_dedup_web')
import core_dedup

def debug_fetch():
    res = core_dedup.fetch_channel_info_and_posts("user7745294847762", max_videos=18)
    print("Res keys:", list(res.keys()))
    print("Videos count:", len(res.get("videos", [])))
    print("Total items:", res.get("total_items"))

debug_fetch()
