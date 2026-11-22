# -*- coding: utf-8 -*-
"""
Core Logic - Tích hợp phân tích trùng lặp, án phạt & FYP từ TikTok Tool & Studio API.
Tích hợp đồng bộ ConfigStore, TikTok Studio Cookie, Passport API & giải mã UID Snowflake.
"""

import os
import sys
import re
import time
import json
import random
import string
import urllib.parse
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Tuple, Union

import requests
from pathlib import Path

# Cố gắng import các hàm trích xuất chuyên sâu & bảo mật từ tool (khi chạy local)
ConfigStore = None
unprotect_secret = None
api_get_post_list = None
sync_items_penalty_data = None
get_profile_crp_status = None

try:
    tool_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    desktop_dir = os.path.join(tool_dir, "tiktok_uploader_desktop")
    if os.path.exists(desktop_dir) and desktop_dir not in sys.path:
        sys.path.insert(0, desktop_dir)

    try:
        from tiktok_uploader_desktop.main import ConfigStore, unprotect_secret
    except Exception:
        try:
            from main import ConfigStore, unprotect_secret
        except Exception:
            pass

    try:
        from tiktok_info_checker_gui import (
            api_get_post_list,
            sync_items_penalty_data,
            get_profile_crp_status
        )
    except Exception:
        pass
except Exception:
    pass

def _parse_cookie_dict(cookie_str: str) -> dict:
    """Parse cookie string thành dict - hỗ trợ cả format text và JSON array."""
    d = {}
    if not cookie_str:
        return d
    cookie_str = str(cookie_str).strip()
    if cookie_str.startswith("["):
        try:
            arr = json.loads(cookie_str)
            for item in arr:
                if isinstance(item, dict) and "name" in item:
                    d[item["name"]] = item.get("value", "")
            return d
        except Exception:
            pass
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d

def _cookie_to_header_str(cookie_str: str) -> str:
    """Chuyển cookie thành chuỗi header Cookie: key=value; ..."""
    if not cookie_str:
        return ""
    s = str(cookie_str).strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            parts = []
            for item in arr:
                if isinstance(item, dict) and "name" in item:
                    parts.append(f"{item['name']}={item.get('value', '')}")
            return "; ".join(parts)
        except Exception:
            pass
    return s

def _normalize_penalty_id(raw_id) -> str:
    if raw_id is None:
        return ""
    s = str(raw_id).strip()
    return "" if s in ("", "0", "None", "null") else s

def api_get_penalty_details_native(cookie_str: str, ua: str = "", proxy: str = "", vid: str = "", create_time: int = 0, region: str = "") -> dict:
    """Lấy chi tiết biên bản xử phạt kiểm duyệt video (getPenaltyDetails) từ TikTok Moderation API."""
    if not vid:
        return {"success": False}
    session = requests.Session()
    if proxy:
        p_c = proxy.strip()
        if not p_c.startswith(("http://", "https://", "socks5://")):
            p_c = f"http://{p_c}"
        session.proxies.update({"http": p_c, "https": p_c})

    hosts = [
        "https://www.tiktok.com",
        "https://api16-normal-useast8.tiktokv.us",
        "https://api16-normal-no1a.tiktokv.eu"
    ]
    headers = {
        "User-Agent": ua or DEFAULT_UA,
        "Cookie": _cookie_to_header_str(cookie_str),
        "Referer": "https://www.tiktok.com/tiktokstudio/content",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.tiktok.com"
    }

    vid_clean = str(vid).strip()
    ct_val = int(create_time or 0)
    if ct_val <= 0:
        if vid_clean.isdigit():
            try:
                ct_val = int(vid_clean) >> 32
            except Exception:
                ct_val = 0
    if ct_val <= 0:
        ct_val = int(time.time())

    params = {"vid": vid_clean, "createTime": ct_val}
    for host in hosts:
        url = f"{host}/mod/v1/getPenaltyDetails/"
        try:
            res = session.get(url, headers=headers, params=params, timeout=8)
            if res.status_code == 200 and res.content:
                d = res.json()
                sc = d.get("status_code", d.get("statusCode", 0))
                if sc == 8:
                    continue
                app_st = d.get("appeal_status", d.get("appealStatus", 0))
                try:
                    app_st_int = int(app_st) if app_st is not None else 0
                except Exception:
                    app_st_int = 0
                is_restored = (app_st_int == 1) or bool(d.get("isRestored") or d.get("is_restored"))
                penalties = d.get("penalties") or []
                has_pen = bool(penalties or d.get("hasPenalty") or d.get("has_penalty")) and not is_restored
                top_det = ""
                if penalties and not is_restored:
                    p0 = penalties[0] if isinstance(penalties[0], dict) else {}
                    top_det = p0.get("penalty_detail") or p0.get("description") or p0.get("penaltyDetail") or ""
                    rc_list = [str(x) for x in (p0.get("reason_codes") or [])]
                    if not top_det:
                        pr_list = p0.get("penalty_reason_info_list") or []
                        if pr_list and isinstance(pr_list[0], dict):
                            raw_c = str(pr_list[0].get("content") or "")
                            if "ineligible for the fyf" in raw_c.lower() or "for you" in raw_c.lower():
                                top_det = "Không đủ điều kiện xuất hiện trên Bảng tin Cho bạn (TNS · Hạn chế FYP)"
                            elif raw_c:
                                top_det = raw_c[:160]
                    if not top_det:
                        if "13" in rc_list:
                            top_det = "Nội dung không nguyên bản / Trùng lặp (Mã 13 · Mất FYP)"
                        elif "11" in rc_list:
                            top_det = "Tương tác giả / Hành vi lừa dối (Mã 11 · Mất FYP)"
                        elif "10" in rc_list:
                            top_det = "Nội dung nhạy cảm / Giật gân (Mã 10 · Mất FYP)"
                        elif "12" in rc_list:
                            top_det = "Spam / Thao túng hệ thống (Mã 12 · Mất FYP)"
                        elif "9" in rc_list or "999" in rc_list:
                            top_det = "Không đủ điều kiện xuất hiện trên Bảng tin Cho bạn (Hạn chế FYP)"
                    if app_st_int == 4 and top_det:
                        top_det += " (Kháng xịt)"
                return {
                    "success": True,
                    "hasPenalty": has_pen,
                    "isRestored": is_restored,
                    "penalties": penalties,
                    "top_penalty_details": top_det or ("Có án phạt vi phạm kiểm duyệt (Hạn chế FYP)" if has_pen else "")
                }
        except Exception:
            pass
    return {"success": False}

def sync_items_penalty_data_native(pen_items: list, cookie_str: str, ua: str = "", proxy: str = "", region: str = "US"):
    """Quét đồng bộ nhanh án phạt & hạn chế FYP cho danh sách video."""
    if not cookie_str or not pen_items:
        return
    def _sync_one(p):
        vid = str(p.get("aweme_id") or p.get("vid") or p.get("id") or "").strip()
        if not vid:
            return
        try:
            c_time = int(p.get("create_time", 0) or p.get("post_time", 0) or p.get("video_posted_date", 0) or 0)
            p_res = api_get_penalty_details_native(cookie_str, ua, proxy, vid, c_time, region)
            if p_res.get("success"):
                if p_res.get("isRestored"):
                    p["is_restored"] = True
                has_pen = bool(p_res.get("hasPenalty"))
                if has_pen:
                    p["has_penalty"] = True
                    p["penalty_flag"] = True
                    p["is_fyp_restricted"] = True
                    p["shadow"] = True
                    top_d = p_res.get("top_penalty_details")
                    if top_d:
                        p["penalty_details"] = top_d
                        p["shadowReason"] = top_d
        except Exception:
            pass

    from requests.adapters import HTTPAdapter
    sess_pen = requests.Session()
    sess_pen.mount("https://", HTTPAdapter(pool_connections=25, pool_maxsize=25))
    sess_pen.mount("http://", HTTPAdapter(pool_connections=25, pool_maxsize=25))
    with ThreadPoolExecutor(max_workers=min(24, len(pen_items))) as executor:
        list(executor.map(_sync_one, pen_items))

def sync_items_crp_punishment_data(pen_items: list, cookie_str: str, ua: str = "", proxy: str = "", region: str = "US"):
    """
    Quét đồng bộ gậy Không nguyên tác / Loại khỏi Quỹ BKT (Creator Rewards Program) cho các video >= 60s
    thông qua API chính thức của TikTok Studio Monetization (/tiktok/v1/creator/m10n_center/video_reward_analytics).
    """
    if not cookie_str or not pen_items:
        return

    targets = []
    for p in pen_items:
        dur_raw = p.get("duration_sec") or p.get("duration") or ((p.get("video") or {}).get("duration") if isinstance(p.get("video"), dict) else 0) or 0
        try:
            d_val = float(dur_raw)
            d_sec = d_val / 1000.0 if d_val > 1000 else d_val
        except Exception:
            d_sec = 0
        if d_sec >= 58 or d_sec == 0:
            targets.append(p)

    if not targets:
        return

    ck_header = _cookie_to_header_str(cookie_str)
    ua_val = ua or DEFAULT_UA
    reg_val = (region or "US").replace("_TTP", "").strip() or "US"
    now_ts = int(time.time())
    start_ts = str(now_ts - 86400 * 365)
    end_ts = str(now_ts)

    proxies = None
    if proxy and proxy.strip() not in _DEAD_PROXIES:
        p_c = proxy.strip()
        if not p_c.startswith(("http://", "https://", "socks5://")):
            p_c = f"http://{p_c}"
        proxies = {"http": p_c, "https": p_c}

    import threading
    _tls = threading.local()

    def _get_crp_sess() -> requests.Session:
        s = getattr(_tls, "sess", None)
        if s is None:
            s = requests.Session()
            _tls.sess = s
        return s

    def _check_crp_one(p):
        vid = str(p.get("aweme_id") or p.get("item_id") or p.get("vid") or p.get("id") or "").strip()
        if not vid:
            return
        try:
            r = _get_crp_sess().get(
                "https://www.tiktok.com/tiktok/v1/creator/m10n_center/video_reward_analytics",
                params={
                    "aid": "1988",
                    "app_name": "tiktok_creator_center",
                    "device_platform": "web_pc",
                    "channel": "tiktok_web",
                    "scene": "156",
                    "locale": "en",
                    "region": reg_val,
                    "start_date": start_ts,
                    "end_date": end_ts,
                    "video_id": vid,
                },
                headers={
                    "User-Agent": ua_val,
                    "Cookie": ck_header,
                    "Referer": f"https://www.tiktok.com/tiktokstudio/monetization/item/{vid}/",
                    "Accept": "application/json, text/plain, */*",
                },
                proxies=proxies,
                timeout=5,
            )
            if r.status_code == 200 and r.content:
                j = r.json()
                crp_d = j.get("crp_analytics_data") or {}
                vid_info = j.get("video_analytics_video_info") or {}
                progs = vid_info.get("video_analytics_programs") or []
                is_pun = (crp_d.get("is_punished") is True) or any(isinstance(pr, dict) and pr.get("is_punished") is True for pr in progs)
                if is_pun:
                    p["is_crp_punished"] = True
                    p["is_dup"] = True
                    p["is_restored"] = False
                    p["isRestored"] = False
                    app_st = int(p.get("appeal_status") or 0)
                    app_note = " · Đã kháng / Kháng xịt" if app_st in (2, 3, 4) else ""
                    crp_msg = f"Bị đánh gậy Không nguyên tác · Loại khỏi Quỹ BKT (Creator Rewards{app_note})"
                    p["crp_penalty_details"] = crp_msg
                    if not p.get("has_penalty"):
                        p["penalty_details"] = crp_msg
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(12, len(targets))) as executor:
        list(executor.map(_check_crp_one, targets))

def api_get_post_list_native(cookie_str: str, ua: str, proxy: str = "", sec_uid: str = "", max_cursor: int = 0, count: int = 50) -> dict:
    """Lấy danh sách video đã đăng từ TikTok Studio API (hoạt động độc lập 100% không cần GUI)."""
    session = requests.Session()
    if proxy:
        p_c = proxy.strip()
        if not p_c.startswith(("http://", "https://", "socks5://")):
            p_c = f"http://{p_c}"
        session.proxies.update({"http": p_c, "https": p_c})

    cookie_dict = _parse_cookie_dict(cookie_str)
    csrf_token = cookie_dict.get("tt_csrf_token", "")
    cookie_header = _cookie_to_header_str(cookie_str)

    url_studio = (
        "https://www.tiktok.com/tiktok/creator/manage/item_list/v1/?"
        "locale=en&aid=1988&priority_region=GB&region=US&"
        "app_name=tiktok_creator_center&channel=tiktok_web&device_platform=web_pc"
    )
    headers = {
        "User-Agent": ua or DEFAULT_UA,
        "Cookie": cookie_header,
        "Referer": "https://www.tiktok.com/tiktokstudio/content",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.tiktok.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if csrf_token:
        headers["X-Secsdk-Csrf-Token"] = csrf_token
        headers["csrf-token"] = csrf_token
        headers["X-Csrf-Token"] = csrf_token

    payload = {
        "cursor": max_cursor,
        "size": count if count >= 20 else 50,
        "query": {
            "sort_orders": [{"field_name": "post_time", "order": 2}],
            "conditions": [],
            "is_recent_posts": False
        }
    }

    raw_items = []
    has_more = False
    next_cursor = max_cursor

    studio_urls = [
        url_studio,
        "https://www.tiktok.com/tiktok/creator/manage/item_list/v1/?locale=en&aid=1988&region=US&app_name=tiktok_creator_center&channel=tiktok_web&device_platform=web_pc",
        url_studio + f"&_t={int(time.time() * 1000)}"
    ]

    for attempt in range(3):
        cur_url = studio_urls[attempt % len(studio_urls)]
        try:
            res = session.post(cur_url, headers=headers, json=payload, timeout=10)
            if res.status_code == 200 and res.content:
                data = res.json()
                sc = data.get("status_code", data.get("statusCode", -1))
                sm = data.get("status_msg", data.get("statusMsg", ""))
                if sc in (0, None) or not sm:
                    inner = data.get("data") or data
                    items_found = (
                        inner.get("item_list")
                        or inner.get("items")
                        or inner.get("aweme_list")
                        or []
                    )
                    if items_found:
                        raw_items = items_found
                        has_more = bool(inner.get("has_more") or inner.get("hasMore", False))
                        next_cursor = (
                            inner.get("cursor")
                            or inner.get("max_cursor")
                            or (max_cursor + len(raw_items))
                        )
                        break
        except Exception:
            pass
        if not raw_items and attempt < 2:
            time.sleep(0.4)

    return {
        "success": bool(raw_items),
        "items": raw_items,
        "has_more": has_more,
        "cursor": next_cursor
    }

if not api_get_post_list:
    api_get_post_list = api_get_post_list_native
if not sync_items_penalty_data:
    sync_items_penalty_data = sync_items_penalty_data_native

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

COUNTRY_NAMES = {
    "US": ("Mỹ", "🇺🇸"),
    "UK": ("Anh", "🇬🇧"),
    "GB": ("Anh", "🇬🇧"),
    "DE": ("Đức", "🇩🇪"),
    "FR": ("Pháp", "🇫🇷"),
    "JP": ("Nhật Bản", "🇯🇵"),
    "KR": ("Hàn Quốc", "🇰🇷"),
    "VN": ("Việt Nam", "🇻🇳"),
    "IT": ("Ý", "🇮🇹"),
    "ES": ("Tây Ban Nha", "🇪🇸"),
    "NL": ("Hà Lan", "🇳🇱"),
    "PL": ("Ba Lan", "🇵🇱"),
    "SE": ("Thụy Điển", "🇸🇪"),
    "NO": ("Na Uy", "🇳🇴"),
    "DK": ("Đan Mạch", "🇩🇰"),
    "FI": ("Phần Lan", "🇫🇮"),
    "CH": ("Thụy Sĩ", "🇨🇭"),
    "AT": ("Áo", "🇦🇹"),
    "BE": ("Bỉ", "🇧🇪"),
    "PT": ("Bồ Đào Nha", "🇵🇹"),
    "CZ": ("Séc", "🇨🇿"),
    "RO": ("Romania", "🇷🇴"),
    "GR": ("Hy Lạp", "🇬🇷"),
    "NZ": ("New Zealand", "🇳🇿"),
    "AR": ("Argentina", "🇦🇷"),
    "CL": ("Chile", "🇨🇱"),
    "CO": ("Colombia", "🇨🇴"),
    "ZA": ("Nam Phi", "🇿🇦"),
    "AE": ("UAE", "🇦🇪"),
    "SA": ("Ả Rập Xê Út", "🇸🇦"),
    "TH": ("Thái Lan", "🇹🇭"),
    "ID": ("Indonesia", "🇮🇩"),
    "PH": ("Philippines", "🇵🇭"),
    "MY": ("Malaysia", "🇲🇾"),
    "SG": ("Singapore", "🇸🇬"),
    "TW": ("Đài Loan", "🇹🇼"),
    "HK": ("Hồng Kông", "🇭🇰"),
    "AU": ("Úc", "🇦🇺"),
    "CA": ("Canada", "🇨🇦"),
    "BR": ("Brazil", "🇧🇷"),
    "MX": ("Mexico", "🇲🇽"),
}

def format_full_country_display(code: str, loc_detail: str = "") -> str:
    raw = (code or "").strip()
    if not raw:
        return "Quốc tế"
    c = raw.upper().replace("_TTP", "")
    base_c = c.split("-")[0].strip() if "-" in c else c
    if base_c in COUNTRY_NAMES:
        name_vn, _ = COUNTRY_NAMES[base_c]
        return f"{name_vn} ({base_c})"
    if base_c == "EU":
        return "Châu Âu (EU)"
    if base_c == "US":
        return "Mỹ (US)"
    return f"{c}"

def format_count_compact(num: Any) -> str:
    try:
        n = int(num or 0)
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")
        elif n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
        elif n >= 10_000:
            return f"{n / 1_000:.1f}K".replace(".0K", "K")
        return f"{n:,}"
    except Exception:
        return "0"

def clean_desc_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "(không mô tả)"
    clean = re.sub(r"#[^\s#]+", "", raw).strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if clean else raw

def decode_snowflake_timestamp(uid: Union[str, int]) -> Optional[str]:
    """Giải mã UID 64-bit TikTok để lấy ngày tạo chính xác (32 bit cao là timestamp)."""
    try:
        u = int(uid)
        if u > (1 << 32):
            ts = u >> 32
            if 1420070400 <= ts <= int(time.time()) + 86400:
                return datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y")
    except Exception:
        pass
    return None

def _load_tiktokmanager_cache() -> dict:
    """Tự động đồng bộ cache quét trùng (groupId, region, isDup) từ TikTok Manager"""
    info_map = {}
    try:
        db_path = os.path.expandvars(r"%APPDATA%\tiktokmanager\profiles.db")
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT aweme_id, group_id FROM dedup_log WHERE group_id IS NOT NULL AND group_id != aweme_id")
            for r in c.fetchall():
                if r[0] and r[1]:
                    info_map[str(r[0])] = {"group_id": str(r[1]), "is_dup": True}
            conn.close()
    except Exception:
        pass

    try:
        app_log = os.path.expandvars(r"%APPDATA%\tiktokmanager\logs\app.log")
        if os.path.exists(app_log):
            with open(app_log, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for m in re.finditer(r'"awemeId":\s*"(\d+)"[\s\S]*?"region":\s*"([A-Z]{2})"[\s\S]*?"groupId":\s*"(\d+)"', content):
                aid, reg, gid = m.group(1), m.group(2), m.group(3)
                info_map[str(aid)] = {
                    "group_id": str(gid),
                    "region": str(reg),
                    "is_dup": (str(aid) != str(gid))
                }
    except Exception:
        pass
    return info_map


_TIKHUB_ENV_CACHE: Dict[str, Any] = {"mtime": 0.0, "key": ""}
_GROUPID_CACHE_LOCK = threading.Lock()
_GROUPID_CACHE_LOADED = False
_GROUPID_CACHE: Dict[str, Dict[str, Any]] = {}
_GROUPID_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groupid_cache.json")


def _get_tikhub_api_key() -> str:
    """Đọc TIKHUB_API_KEY từ biến môi trường hoặc file .env (tự cập nhật khi file .env thay đổi)."""
    env_val = os.environ.get("TIKHUB_API_KEY", "").strip()
    if env_val:
        return env_val
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        if os.path.exists(env_path):
            mt = os.path.getmtime(env_path)
            if mt != _TIKHUB_ENV_CACHE.get("mtime"):
                found_key = ""
                with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("#") or "=" not in s:
                            continue
                        k, v = s.split("=", 1)
                        if k.strip() == "TIKHUB_API_KEY":
                            found_key = v.strip().strip('"').strip("'")
                            break
                _TIKHUB_ENV_CACHE["mtime"] = mt
                _TIKHUB_ENV_CACHE["key"] = found_key
            return str(_TIKHUB_ENV_CACHE.get("key") or "")
    except Exception:
        pass
    return ""


def _load_persistent_groupid_cache() -> Dict[str, Dict[str, Any]]:
    """Tải bộ nhớ đệm group_id vĩnh viễn trên ổ cứng để không tốn lượt gọi API lặp lại."""
    global _GROUPID_CACHE_LOADED
    with _GROUPID_CACHE_LOCK:
        if not _GROUPID_CACHE_LOADED:
            _GROUPID_CACHE_LOADED = True
            try:
                if os.path.exists(_GROUPID_CACHE_PATH):
                    with open(_GROUPID_CACHE_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        _GROUPID_CACHE.update(data)
            except Exception:
                pass
        return _GROUPID_CACHE


def _save_persistent_groupid_cache() -> None:
    """Lưu nguyên tử bộ nhớ đệm group_id xuống groupid_cache.json."""
    with _GROUPID_CACHE_LOCK:
        try:
            tmp_path = _GROUPID_CACHE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_GROUPID_CACHE, f, ensure_ascii=False)
            os.replace(tmp_path, _GROUPID_CACHE_PATH)
        except Exception:
            pass


_TIKHUB_PAID_ROUTE_COOLDOWN_UNTIL: float = 0.0
_TIKHUB_SECUID_NEXT_CURSOR: Dict[str, int] = {}


def _cache_and_collect_awemes(raw_awemes: List[Dict[str, Any]], results: Dict[str, Dict[str, Any]]) -> None:
    now_ts = int(time.time())
    cache_ref = _load_persistent_groupid_cache()
    changed = False
    for aw in raw_awemes:
        if not isinstance(aw, dict):
            continue
        aid = str(aw.get("aweme_id") or aw.get("id") or "").strip()
        if not aid:
            continue
        gid = str(aw.get("group_id") or aw.get("groupId") or "").strip()
        reg = str(aw.get("region") or "").strip().upper()
        nff = bool(aw.get("is_nff_or_nr"))
        results[aid] = aw
        if gid:
            with _GROUPID_CACHE_LOCK:
                cache_ref[aid] = {
                    "group_id": gid,
                    "region": reg,
                    "is_nff_or_nr": nff,
                    "ts": now_ts,
                }
                changed = True
    if changed:
        _save_persistent_groupid_cache()


def _fetch_tikhub_multi_awemes(
    video_ids: List[str],
    sec_uid: str = "",
    start_cursor_ms: int = 0,
) -> Dict[str, Dict[str, Any]]:
    """
    Lấy group_id, region & NFF chuẩn Mobile với cơ chế tự động chọn cổng rẻ nhất:
    1. Nếu tài khoản có số dư nạp (Balance): ưu tiên fetch_user_post_videos ($0.001 cho cả 20 video/lần ~ 25đ/kênh).
    2. Nếu đang dùng Free Credit (402 $0đ) hoặc còn sót video:
       - Ít video (<= 8 video): gọi fetch_one_video ($0.001/video, tránh mất $0.01 cả gói 10).
       - Nhiều video (>= 9 video): gom lô fetch_multi_video ($0.01/10 video).
    """
    global _TIKHUB_PAID_ROUTE_COOLDOWN_UNTIL
    api_key = _get_tikhub_api_key()
    clean_ids = [str(v).strip() for v in video_ids if str(v).strip().isdigit()]
    if not api_key or not clean_ids:
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 1. Cổng siêu rẻ $0.001 / trang (rẻ gấp 10 lần fetch_multi_video $0.01/10 video)
    clean_id_set = set(clean_ids)
    if sec_uid and len(clean_ids) >= 2 and time.time() >= _TIKHUB_PAID_ROUTE_COOLDOWN_UNTIL:
        cur_val = int(start_cursor_ms or 0)
        max_pages = min(4, max(1, (len(clean_ids) + 8) // 9))
        for _page_idx in range(max_pages):
            missing_now = [vid for vid in clean_ids if vid not in results]
            # Nếu chỉ còn đúng 1 video lẻ (VD video ghim cũ), gọi thẳng fetch_one_video ($0.001) chính xác 100% thay vì lật trang mò
            if len(missing_now) <= 1:
                break
            try:
                r_post = requests.get(
                    "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_user_post_videos",
                    params={"sec_user_id": sec_uid, "count": 20, "max_cursor": cur_val, "sort_type": 0},
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                    timeout=8,
                )
                if r_post.status_code == 200:
                    p_data = (r_post.json() or {}).get("data") or {}
                    aw_list = p_data.get("aweme_list") if isinstance(p_data, dict) else (p_data if isinstance(p_data, list) else [])
                    before_hits = sum(1 for vid in clean_id_set if vid in results)
                    if aw_list:
                        _cache_and_collect_awemes(aw_list, results)
                    after_hits = sum(1 for vid in clean_id_set if vid in results)
                    has_more_p = bool(p_data.get("has_more")) if isinstance(p_data, dict) else False
                    next_cur_p = int(p_data.get("max_cursor") or 0) if isinstance(p_data, dict) else 0
                    if next_cur_p > 0:
                        _TIKHUB_SECUID_NEXT_CURSOR[sec_uid] = next_cur_p
                    # Dừng ngay nếu trang vừa lấy không khớp thêm video mục tiêu nào hoặc đã hết trang
                    if not aw_list or (after_hits == before_hits) or not has_more_p or next_cur_p <= 0 or next_cur_p == cur_val:
                        break
                    cur_val = next_cur_p
                elif r_post.status_code == 402:
                    # Tài khoản đang dùng Free Credit -> tạm nghỉ thử cổng này 5 phút (402 không bị trừ tiền)
                    _TIKHUB_PAID_ROUTE_COOLDOWN_UNTIL = time.time() + 300
                    break
                else:
                    break
            except Exception:
                break

    remaining_ids = [vid for vid in clean_ids if vid not in results]
    if not remaining_ids:
        return results

    def _call_one(vid: str) -> Optional[Dict[str, Any]]:
        try:
            resp = requests.get(
                "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_one_video",
                params={"aweme_id": vid},
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=8,
            )
            if resp.status_code == 200:
                d = (resp.json() or {}).get("data") or {}
                if isinstance(d, dict):
                    aw = d.get("aweme_detail") if isinstance(d.get("aweme_detail"), dict) else d
                    if aw.get("aweme_id") or aw.get("id"):
                        return aw
        except Exception:
            pass
        return None

    def _call_chunk(chunk_ids: List[str]) -> List[Dict[str, Any]]:
        if len(chunk_ids) <= 8:
            out_ones: List[Dict[str, Any]] = []
            if len(chunk_ids) == 1:
                one_res = _call_one(chunk_ids[0])
                if one_res:
                    out_ones.append(one_res)
            else:
                with ThreadPoolExecutor(max_workers=min(6, len(chunk_ids))) as one_pool:
                    for one_res in one_pool.map(_call_one, chunk_ids):
                        if one_res:
                            out_ones.append(one_res)
            return out_ones
        try:
            resp = requests.post(
                "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_multi_video",
                json=chunk_ids,
                headers=headers,
                timeout=8,
            )
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data")
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
                if isinstance(data, dict):
                    arr = data.get("aweme_details") or data.get("aweme_list") or []
                    return [x for x in arr if isinstance(x, dict)]
            else:
                print(f"[TikHubGroupId] HTTP {resp.status_code} khi lấy batch {len(chunk_ids)} video")
        except Exception as err:
            print(f"[TikHubGroupId] Lỗi kết nối batch: {err}")
        return []

    chunks = [remaining_ids[i:i + 10] for i in range(0, len(remaining_ids), 10)]
    raw_awemes: List[Dict[str, Any]] = []
    if len(chunks) == 1:
        raw_awemes = _call_chunk(chunks[0])
    else:
        with ThreadPoolExecutor(max_workers=min(3, len(chunks))) as pool:
            for res_list in pool.map(_call_chunk, chunks):
                raw_awemes.extend(res_list)

    _cache_and_collect_awemes(raw_awemes, results)
    return results


def sync_items_tikhub_groupid(
    raw_items: List[Dict[str, Any]],
    tkm_cache: Optional[Dict[str, Any]] = None,
    sec_uid: str = "",
    is_load_more: bool = False,
) -> None:
    """Đồng bộ trực tiếp group_id, region và cờ is_nff_or_nr cho danh sách video (ưu tiên cache 0đ trước)."""
    if not raw_items:
        return
    p_cache = _load_persistent_groupid_cache()
    tkm_map = tkm_cache or {}
    missing_ids: List[str] = []
    seen_missing = set()

    for it in raw_items:
        aid = str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "").strip()
        if not aid:
            continue

        # Bắt Duet / Stitch gốc từ Web nếu có
        duet_info = it.get("duetInfo") if isinstance(it.get("duetInfo"), dict) else {}
        duet_from = str(duet_info.get("duetFromId") or "").strip()
        if duet_from and duet_from != "0" and duet_from != aid and len(duet_from) >= 15:
            if not it.get("group_id"):
                it["group_id"] = duet_from

        # Kiểm tra cache groupid_cache.json trước
        c_entry = p_cache.get(aid)
        if isinstance(c_entry, dict) and c_entry.get("group_id"):
            it["group_id"] = str(c_entry["group_id"])
            c_reg = str(c_entry.get("region") or "").strip().upper()
            if len(c_reg) == 2 and c_reg not in ("EU", "XO"):
                it["region"] = c_reg
            if c_entry.get("is_nff_or_nr") is True:
                it["is_nff_or_nr"] = True
            continue

        existing_gid = str(it.get("group_id") or it.get("groupId") or (tkm_map.get(aid) or {}).get("group_id") or "").strip()
        if existing_gid and existing_gid not in ("0", "None", "null"):
            continue

        if aid not in seen_missing:
            seen_missing.add(aid)
            missing_ids.append(aid)

    if not missing_ids or not _get_tikhub_api_key():
        return

    start_cur_ms = 0
    if is_load_more and sec_uid:
        saved_cur = int(_TIKHUB_SECUID_NEXT_CURSOR.get(sec_uid) or 0)
        if saved_cur > 0:
            start_cur_ms = saved_cur
        else:
            newest_ct = max(
                (int(x.get("createTime") or x.get("create_time") or x.get("post_time") or 0) for x in raw_items),
                default=0,
            )
            if newest_ct > 1_000_000_000:
                start_cur_ms = (newest_ct + 1) * 1000

    fetched_awemes = _fetch_tikhub_multi_awemes(missing_ids, sec_uid=sec_uid, start_cursor_ms=start_cur_ms)
    if not fetched_awemes:
        return

    for it in raw_items:
        aid = str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "").strip()
        aw = fetched_awemes.get(aid)
        if not isinstance(aw, dict):
            continue
        gid = str(aw.get("group_id") or aw.get("groupId") or "").strip()
        if gid and gid not in ("0", "None", "null"):
            it["group_id"] = gid
        reg = str(aw.get("region") or "").strip().upper()
        if len(reg) == 2:
            it["region"] = reg
        if aw.get("is_nff_or_nr") is True:
            it["is_nff_or_nr"] = True

class TikTokPlaywrightWorker:
    """
    Browser Worker chạy nền duy nhất (Singleton) giữ Chromium luôn mở sẵn.
    Bỏ qua toàn bộ thời gian khởi động browser (tiết kiệm 2-3s mỗi lượt quét).
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.playwright = None
        self.browser = None
        self._is_ready = False
        self._init_error = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.executor.submit(self._init_browser)

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _init_browser(self):
        try:
            from playwright.sync_api import sync_playwright
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                ]
            )
            self._is_ready = True
            self._init_error = None
            print("[PlaywrightWorker] Khởi động Chromium browser worker siêu tốc thành công.")
        except Exception as e:
            self._is_ready = False
            self._init_error = f"{type(e).__name__}: {str(e)}"
            print(f"[PlaywrightWorker] Lỗi khởi tạo browser: {e}")

    def fetch_channel(
        self,
        url: str,
        cookie_str: str = "",
        proxy_str: str = "",
        max_videos: int = 18,
        channel_info: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        try:
            future = self.executor.submit(self._task_scrape, url, cookie_str, proxy_str, max_videos, channel_info)
            return future.result(timeout=25)
        except Exception as e:
            print(f"[PlaywrightWorker] Lỗi fetch_channel: {e}")
            try:
                self.executor.submit(self._init_browser)
            except Exception:
                pass
            return []

    def _task_scrape(
        self,
        url: str,
        cookie_str: str,
        proxy_str: str,
        max_videos: int,
        channel_info: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not self.browser or not self.browser.is_connected():
            self._init_browser()
        if not self.browser:
            return []

        captured_items = []
        captured_seen = set()
        ctx = None
        try:
            ctx_opts = {
                "user_agent": DEFAULT_UA,
                "viewport": {"width": 1280, "height": 720},
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
            if proxy_str:
                p_clean = proxy_str.strip()
                if not p_clean.startswith(("http://", "https://", "socks5://")):
                    p_clean = f"http://{p_clean}"
                ctx_opts["proxy"] = {"server": p_clean}

            ctx = self.browser.new_context(**ctx_opts)
            if cookie_str:
                cookie_list = []
                for part in cookie_str.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        cookie_list.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".tiktok.com",
                            "path": "/"
                        })
                if cookie_list:
                    try:
                        ctx.add_cookies(cookie_list)
                    except Exception:
                        pass

            page = ctx.new_page()

            try:
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                """)
            except Exception:
                pass

            def block_unneeded(route):
                try:
                    rt = route.request.resource_type
                    if rt in ["media"]:
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass

            page.route("**/*", block_unneeded)

            def on_resp(r):
                if "/api/post/item_list/" in str(r.url):
                    try:
                        d = r.json()
                        items = d.get("itemList") or d.get("items") or []
                        for it in items:
                            iid = str(it.get("id") or it.get("aweme_id") or "")
                            if iid and iid not in captured_seen:
                                captured_seen.add(iid)
                                captured_items.append(it)
                    except Exception:
                        pass

            page.on("response", on_resp)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

            # Chờ gói tin API đầu tiên (lên đến 6s)
            for _ in range(60):
                if captured_items:
                    page.wait_for_timeout(200)
                    break
                page.wait_for_timeout(100)

            # Nếu chưa có item, di chuyển chuột nhẹ để kích hoạt event scroll/lazy-load của TikTok
            if not captured_items:
                try:
                    page.mouse.move(500, 300)
                    page.mouse.wheel(0, 800)
                except Exception:
                    pass
                for _ in range(30):
                    if captured_items:
                        page.wait_for_timeout(200)
                        break
                    page.wait_for_timeout(100)

            # Cuộn thêm nếu cần thêm video và chưa đủ số lượng
            if len(captured_items) < max_videos:
                idle_rounds = 0
                last_len = len(captured_items)
                try:
                    page.mouse.move(600, 400)
                except Exception:
                    pass
                while len(captured_items) < max_videos and idle_rounds < 4:
                    try:
                        page.mouse.wheel(0, 3500)
                        page.keyboard.press("PageDown")
                        page.wait_for_timeout(650)
                        if len(captured_items) > last_len:
                            last_len = len(captured_items)
                            idle_rounds = 0
                        else:
                            idle_rounds += 1
                    except Exception:
                        break

            if channel_info is not None and (not channel_info.get("avatar") or not channel_info.get("followers")):
                try:
                    dom_data = page.evaluate("""() => {
                        const img = document.querySelector('img[class*="avatar"], [data-e2e="user-avatar"] img, img[alt*="avatar"]');
                        const followers = document.querySelector('[data-e2e="followers-count"]')?.textContent?.trim() || "";
                        const nick = document.querySelector('[data-e2e="user-subtitle"], [data-e2e="user-title"]')?.textContent?.trim() || "";
                        return {
                            avatar: img ? (img.currentSrc || img.src) : "",
                            followers: followers,
                            nickname: nick
                        };
                    }""")
                    if isinstance(dom_data, dict):
                        if dom_data.get("avatar") and not channel_info.get("avatar"):
                            channel_info["avatar"] = dom_data["avatar"]
                        if dom_data.get("nickname") and (not channel_info.get("nickname") or channel_info.get("nickname") == channel_info.get("uniqueId")):
                            channel_info["nickname"] = dom_data["nickname"]
                        if dom_data.get("followers") and not channel_info.get("followers"):
                            try:
                                f_str = dom_data["followers"].upper().replace(" ", "").replace(",", "")
                                mult = 1
                                if f_str.endswith("K"):
                                    mult = 1000
                                    f_str = f_str[:-1]
                                elif f_str.endswith("M"):
                                    mult = 1000000
                                    f_str = f_str[:-1]
                                elif f_str.endswith("B"):
                                    mult = 1000000000
                                    f_str = f_str[:-1]
                                channel_info["followers"] = int(float(f_str) * mult)
                            except Exception:
                                pass
                except Exception:
                    pass

        except Exception as e:
            print(f"[PlaywrightWorker] Lỗi scrape: {e}")
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

        return captured_items

def fetch_external_channel_videos_playwright(
    channel_url: str,
    cookie_str: str = "",
    proxy_str: str = "",
    max_videos: int = 18,
    channel_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Mở Playwright với Browser Worker siêu tốc (quét chỉ trong 2-3s)."""
    worker = TikTokPlaywrightWorker.get_instance()
    return worker.fetch_channel(
        channel_url,
        cookie_str=cookie_str,
        proxy_str=proxy_str,
        max_videos=max_videos,
        channel_info=channel_info,
    )


def fetch_external_channel_videos_creator_api(
    sec_uid: str,
    cookie_str: str,
    ua_str: str = "",
    proxy_str: str = "",
    max_videos: int = 18,
    max_cursor: int = 0,
    result_meta: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Quét video siêu tốc qua API creator/item_list (0.5s - 1.5s, không tốn RAM Playwright).
    result_meta["confirmed_empty"] = True khi TikTok trả statusCode=0 nhưng không có video công khai nào
    (kênh để toàn bộ video ở chế độ Bạn bè / Riêng tư)."""
    if not sec_uid or not cookie_str:
        return []
    
    all_items = []
    raw_mc = int(max_cursor or 0)
    cursor = str(raw_mc) if raw_mc >= 1_000_000_000 else "0"
    user_agent = ua_str or DEFAULT_UA
    
    proxies = {}
    if proxy_str:
        p_c = proxy_str.strip()
        if not p_c.startswith(("http://", "https://", "socks5://")):
            p_c = f"http://{p_c}"
        proxies = {"http": p_c, "https": p_c}

    s = requests.Session()
    if proxies:
        s.proxies.update(proxies)

    max_loops = max(1, (max_videos // 15) + 1)
    for _ in range(max_loops):
        vfp = "verify_" + "".join(random.choices(string.hexdigits, k=7))
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
            "cursor": str(cursor),
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
        headers = {
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Referer": "https://www.tiktok.com/"
        }
        try:
            r = s.get(api_url, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                items = data.get("itemList") or []
                if not items:
                    if (not all_items and str(cursor) == str(max_cursor or 0)
                            and str(data.get("statusCode", data.get("status_code", ""))) == "0"
                            and result_meta is not None):
                        result_meta["confirmed_empty"] = True
                    break
                for it in items:
                    iid = str(it.get("id") or "")
                    if iid and not any(str(x.get("id")) == iid for x in all_items):
                        all_items.append(it)
                if len(all_items) >= max_videos:
                    break
                last_ts = items[-1].get("createTime")
                if last_ts:
                    cursor = str(int(last_ts) * 1000)
                else:
                    break
            else:
                break
        except Exception:
            break

    return all_items

_BORROWED_POOL_CACHE: Dict[str, Any] = {"ts": 0.0, "pool": []}
_DEAD_PROXIES: set = set()

def get_borrowed_cookies_pool() -> List[Tuple[str, str, str]]:
    """
    Thu thập danh sách các cookie hợp lệ từ Studio Web, ConfigStore và cookie_pool.json (đã xáo trộn)
    để xoay vòng khi quét kênh ngoài, tránh chết 1 cookie hoặc bị TikTok giới hạn tốc độ.
    Có cache RAM 120s để không phải giải mã DPAPI / đọc đĩa lại mỗi lần quét.
    """
    now_t = time.time()
    cached_list = _BORROWED_POOL_CACHE.get("pool") or []
    if cached_list and (now_t - float(_BORROWED_POOL_CACHE.get("ts") or 0)) < 120:
        out_copy = list(cached_list)
        random.shuffle(out_copy)
        return out_copy

    pool_out: List[Tuple[str, str, str]] = []
    seen_sids = set()

    def _add_cand(ck_raw: str, ua_val: str = "", px_val: str = ""):
        if not ck_raw or len(ck_raw) < 30:
            return
        ck_str = _cookie_to_header_str(ck_raw)
        if "sessionid=" not in ck_str and "sessionid_ss=" not in ck_str:
            return
        m_s = re.search(r"sessionid(?:_ss)?=([0-9a-zA-Z_-]{8,})", ck_str)
        sid_key = m_s.group(1) if m_s else ck_str[:48]
        if sid_key in seen_sids:
            return
        seen_sids.add(sid_key)
        pool_out.append((ck_str, ua_val or DEFAULT_UA, px_val or ""))

    # 1. Các nick đã đăng nhập trên Web (ACTIVE_STUDIO_ACCOUNTS)
    try:
        import studio_engine
        for _, s_entry in list((studio_engine.ACTIVE_STUDIO_ACCOUNTS or {}).items()):
            if isinstance(s_entry, dict) and s_entry.get("cookie"):
                _add_cand(s_entry["cookie"], DEFAULT_UA, "")
    except Exception:
        pass

    # 2. Kho ConfigStore của tool desktop (nếu có)
    try:
        if ConfigStore:
            store = ConfigStore()
            _, accs = store.load()
            for a in accs:
                c = getattr(a, "tiktok_cookie", "")
                if c:
                    if c.startswith("dpapi:") and unprotect_secret:
                        try:
                            c = unprotect_secret(c)
                        except Exception:
                            continue
                    _add_cand(c, getattr(a, "user_agent", "") or DEFAULT_UA, "")
    except Exception as e:
        print(f"[BorrowCookie] Lỗi mượn cookie từ tool: {e}")

    # 3. Kho cookie_pool.json
    try:
        pool_file = Path(__file__).resolve().parent / "cookie_pool.json"
        if pool_file.exists():
            data = json.loads(pool_file.read_text(encoding="utf-8"))
            if data.get("cookie"):
                _add_cand(data["cookie"], data.get("user_agent", DEFAULT_UA), "")
            for _, a_info in (data.get("accounts") or {}).items():
                if isinstance(a_info, dict) and a_info.get("cookie"):
                    _add_cand(a_info["cookie"], a_info.get("user_agent", DEFAULT_UA), "")
    except Exception as e:
        print(f"[BorrowCookie] Lỗi đọc cookie_pool.json: {e}")

    env_c = os.getenv("TIKTOK_COOKIE", "")
    if env_c:
        _add_cand(env_c, DEFAULT_UA, "")

    if pool_out:
        _BORROWED_POOL_CACHE["ts"] = now_t
        _BORROWED_POOL_CACHE["pool"] = list(pool_out)
    random.shuffle(pool_out)
    return pool_out

def get_borrowed_cookie() -> Tuple[str, str, str]:
    """Chọn ngẫu nhiên 1 cookie hợp lệ từ kho xoay vòng."""
    cands = get_borrowed_cookies_pool()
    if cands:
        return cands[0]
    return "", "", ""


def _resolve_query_target(raw_query: str, ua_str: str = "") -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Hỗ trợ mọi định dạng đầu vào: @username, link kênh, link video đầy đủ,
    link rút gọn (vt.tiktok.com / vm.tiktok.com), hoặc ID video 17-21 chữ số.
    Trả về: (username, target_video_id, fallback_video_dict)
    """
    q = str(raw_query or "").strip()
    if not q:
        return "", "", None

    ua = ua_str or DEFAULT_UA
    target_vid = ""
    fallback_it = None

    # 1. Nếu là link rút gọn (vt.tiktok.com / vm.tiktok.com / tiktok.com/t/)
    if re.search(r"https?://(?:vt|vm)\.tiktok\.com/|tiktok\.com/t/", q, re.I):
        try:
            r_red = requests.get(q, headers={"User-Agent": ua}, allow_redirects=True, timeout=8)
            if r_red.url and "tiktok.com" in r_red.url:
                q = r_red.url
        except Exception:
            pass

    # 2. Trích xuất video ID nếu có trong URL hoặc chuỗi số thuần 17-21 chữ số
    m_url_vid = re.search(r"tiktok\.com/@([^/?#]+)/(?:video|photo)/(\d{15,22})", q)
    if m_url_vid:
        return m_url_vid.group(1).strip().lstrip("@"), m_url_vid.group(2), None

    m_any_vid = re.search(r"/(?:video|photo|v)/(\d{15,22})", q)
    if m_any_vid:
        target_vid = m_any_vid.group(1)
    elif re.fullmatch(r"\d{17,22}", q):
        target_vid = q

    # Nếu chỉ có ID video mà chưa biết username -> tra ngược tác giả siêu tốc
    if target_vid:
        uname_found = ""
        try:
            ck_b, _, _ = get_borrowed_cookie()
            h_vid = {"User-Agent": ua, "Referer": "https://www.tiktok.com/"}
            if ck_b:
                h_vid["Cookie"] = ck_b
            r_vp = requests.get(f"https://www.tiktok.com/@tiktok/video/{target_vid}", headers=h_vid, allow_redirects=True, timeout=8)
            m_u = re.search(r"tiktok\.com/@([^/?#]+)/(?:video|photo)/", r_vp.url or "")
            if m_u and m_u.group(1) != "tiktok":
                uname_found = m_u.group(1)
            m_rehyd = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>', r_vp.text or "")
            if m_rehyd:
                sc = json.loads(m_rehyd.group(1)).get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
                ist = (sc.get("itemInfo") or {}).get("itemStruct") or {}
                if ist and ist.get("id"):
                    fallback_it = ist
                    au = (ist.get("author") or {}).get("uniqueId") or ""
                    if au:
                        uname_found = au
        except Exception:
            pass

        if not uname_found:
            try:
                r_wm = requests.get("https://www.tikwm.com/api/", params={"url": target_vid}, timeout=8).json()
                wm_d = r_wm.get("data") or {}
                au = (wm_d.get("author") or {}).get("unique_id") or ""
                if au:
                    uname_found = au
                if wm_d.get("id"):
                    fallback_it = {
                        "id": str(wm_d.get("id")),
                        "desc": wm_d.get("title") or "",
                        "createTime": wm_d.get("create_time") or 0,
                        "duration": wm_d.get("duration") or 0,
                        "region": wm_d.get("region") or "",
                        "cover_url": wm_d.get("origin_cover") or wm_d.get("cover") or "",
                        "play_count": wm_d.get("play_count") or 0,
                        "digg_count": wm_d.get("digg_count") or 0,
                        "comment_count": wm_d.get("comment_count") or 0,
                        "share_count": wm_d.get("share_count") or 0,
                        "collect_count": wm_d.get("collect_count") or 0,
                        "is_nff_or_nr": bool(wm_d.get("is_nff_or_nr")),
                        "author": {
                            "uniqueId": au,
                            "nickname": (wm_d.get("author") or {}).get("nickname") or au,
                            "avatarLarger": (wm_d.get("author") or {}).get("avatar") or "",
                        },
                    }
            except Exception:
                pass
        return uname_found.strip().lstrip("@"), target_vid, fallback_it

    # 3. Link kênh hoặc @username thông thường
    username = q
    if "tiktok.com/@" in q:
        username = q.split("tiktok.com/@")[-1].split("?")[0].split("#")[0].split("/")[0]
    elif "tiktok.com/" in q:
        p = q.split("tiktok.com/")[-1].split("?")[0].split("#")[0].split("/")[0]
        username = p.lstrip("@")
    else:
        username = q.lstrip("@")
    username = username.split("?")[0].split("#")[0].rstrip("/").strip().lstrip("@")
    return username, "", None

def fetch_channel_info_and_posts(
    channel_query: str,
    sec_uid: str = "",
    max_cursor: int = 0,
    cookie_str: str = "",
    ua_str: str = "",
    proxy_str: str = "",
    max_videos: int = 18,
    allow_paid_groupid: bool = False,
) -> Dict[str, Any]:
    """
    Hàm kiểm tra trùng lặp & quét thông tin kênh TikTok.
    - Tự động nhận diện tài khoản trong ConfigStore (có cookie Studio) để quét án phạt & FYP chính xác 100% như tool.
    - Hỗ trợ lấy dữ liệu Check user (Tên, Handle, SĐT, Quốc gia, UID, Ngày tạo).
    - Hỗ trợ phân trang: mặc định quét 24 video, có thể quét thêm các đợt tiếp theo.
    """
    query = str(channel_query or "").strip()
    force_external = query.startswith("!")
    if force_external:
        query = query.lstrip("!").strip()
        cookie_str = ""
    username, target_vid, fallback_target_item = _resolve_query_target(query, ua_str or DEFAULT_UA)

    channel_info = {
        "uniqueId": username,
        "nickname": username,
        "avatar": "",
        "followers": 0,
        "videoCount": 0,
        "createDate": "—",
        "region": "US",
        "location_detail": "",
    }

    # Đối tượng thông tin Check User bên phải (SĐT mặc định là —)
    check_user_info = {
        "screen_name": username,
        "username": f"@{username}",
        "create_date": "—",
        "mobile": "—",
        "country": "United States",
        "uid": "---",
        "last_nickname_change": "—",
        "is_self": False,
    }

    matched_acc_cookie = cookie_str
    matched_acc_proxy = ""
    matched_acc_ua = ua_str or DEFAULT_UA
    is_self_account = bool(matched_acc_cookie)

    # 1. Tìm trong ConfigStore của tool hoặc cookie_pool.json
    cookie_pool_data = {}
    try:
        pool_file = Path(__file__).resolve().parent / "cookie_pool.json"
        if pool_file.exists():
            cookie_pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
    except Exception:
        pass

    if username and not matched_acc_cookie and not force_external:
        # A. Thử tìm trong ConfigStore (nếu chạy local)
        if ConfigStore:
            try:
                store = ConfigStore()
                _, accs = store.load()
                for a in accs:
                    un = str(getattr(a, "username", "") or getattr(a, "name", "")).lstrip("@").strip().lower()
                    if un and un == username.strip().lower():
                        is_self_account = True
                        matched_acc_cookie = getattr(a, "tiktok_cookie", "")
                        if getattr(a, "user_agent", ""):
                            matched_acc_ua = getattr(a, "user_agent", "")
                        break
            except Exception:
                pass

        # B. Thử tìm trong cookie_pool.json['accounts'] (chạy trên Render / Cloud)
        if not matched_acc_cookie and cookie_pool_data:
            accs_map = cookie_pool_data.get("accounts", {})
            u_clean = username.strip().lower()
            if u_clean in accs_map:
                a_info = accs_map[u_clean]
                is_self_account = True
                matched_acc_cookie = a_info.get("cookie", "")
                if a_info.get("user_agent"):
                    matched_acc_ua = a_info["user_agent"]
                if a_info.get("sec_uid") and not sec_uid:
                    sec_uid = a_info["sec_uid"]

        # C. Thử tìm trong phiên Studio Web đang hoạt động (studio_engine.ACTIVE_STUDIO_ACCOUNTS)
        if not matched_acc_cookie:
            try:
                import studio_engine
                u_clean = username.strip().lower()
                for s_info in list(getattr(studio_engine, "ACTIVE_STUDIO_ACCOUNTS", {}).values()):
                    if not isinstance(s_info, dict) or not s_info.get("cookie"):
                        continue
                    acc_obj = s_info.get("account") or {}
                    a_un = str(acc_obj.get("uniqueId") or acc_obj.get("username") or "").lstrip("@").strip().lower()
                    if a_un and a_un == u_clean:
                        is_self_account = True
                        matched_acc_cookie = s_info.get("cookie", "")
                        if acc_obj.get("sec_uid") and not sec_uid:
                            sec_uid = acc_obj["sec_uid"]
                        break
            except Exception:
                pass

    if matched_acc_cookie and isinstance(matched_acc_cookie, str) and matched_acc_cookie.startswith("dpapi:") and unprotect_secret:
        try:
            matched_acc_cookie = unprotect_secret(matched_acc_cookie)
        except Exception:
            pass

    effective_cookie = matched_acc_cookie or cookie_str or ""
    detected_account_country = ""

    # Cơ chế mượn cookie từ kho tool: Chỉ dùng để lấy passport nếu có
    if not is_self_account and not effective_cookie:
        b_c, b_ua, b_px = get_borrowed_cookie()
        if b_c:
            effective_cookie = b_c
            matched_acc_ua = b_ua or matched_acc_ua
            if b_px and not matched_acc_proxy:
                matched_acc_proxy = b_px
            print(f"[CheckUser] Đã kích hoạt cơ chế mượn cookie từ kho tool để quét kênh ngoài @{username}")

    # 2. Nếu là nick trong tool, gọi Passport API lấy thông tin Check user chi tiết
    if is_self_account and effective_cookie:
        try:
            p_headers = {
                "User-Agent": matched_acc_ua,
                "Cookie": effective_cookie,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.tiktok.com/",
            }
            px_dict = {}
            if matched_acc_proxy and matched_acc_proxy.strip() in _DEAD_PROXIES:
                matched_acc_proxy = ""
            if matched_acc_proxy:
                p_c = matched_acc_proxy.strip()
                if not p_c.startswith(("http://", "https://", "socks5://")):
                    p_c = f"http://{p_c}"
                px_dict = {"http": p_c, "https": p_c}

            r_pass = None
            if px_dict:
                try:
                    r_pass = requests.get(
                        "https://www.tiktok.com/passport/web/account/info/",
                        headers=p_headers,
                        proxies=px_dict,
                        timeout=2.5
                    )
                except Exception:
                    _DEAD_PROXIES.add(matched_acc_proxy.strip())
                    matched_acc_proxy = ""
                    px_dict = {}
            if r_pass is None:
                r_pass = requests.get(
                    "https://www.tiktok.com/passport/web/account/info/",
                    headers=p_headers,
                    proxies=None,
                    timeout=6
                )
            if r_pass.status_code == 200:
                p_data = r_pass.json().get("data", {})
                if p_data:
                    check_user_info["is_self"] = True
                    p_name = p_data.get("screen_name") or p_data.get("nickname") or username
                    check_user_info["screen_name"] = p_name
                    channel_info["nickname"] = p_name

                    p_uname = str(p_data.get("username") or username).lstrip("@").strip()
                    check_user_info["username"] = f"@{p_uname}"
                    channel_info["uniqueId"] = p_uname

                    p_av = (
                        p_data.get("avatar_url")
                        or p_data.get("avatar_larger")
                        or p_data.get("avatar_medium")
                        or p_data.get("avatar_thumb")
                        or p_data.get("avatar_300x300")
                        or p_data.get("avatar_168x168")
                        or p_data.get("avatar")
                    )
                    if p_av and not channel_info.get("avatar"):
                        channel_info["avatar"] = p_av

                    if p_data.get("mobile"):
                        check_user_info["mobile"] = p_data["mobile"]
                    elif p_data.get("phone_collected"):
                        check_user_info["mobile"] = "Đã gắn"

                    p_c = (p_data.get("store_country") or p_data.get("country") or p_data.get("region") or "").strip().upper()
                    if p_c:
                        detected_account_country = p_c
                        check_user_info["country"] = format_full_country_display(p_c)

                    u_id_val = str(p_data.get("user_id") or "")
                    if u_id_val:
                        check_user_info["uid"] = u_id_val

                    c_ts = int(p_data.get("create_time") or 0)
                    if c_ts > 0:
                        c_date = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y")
                        check_user_info["create_date"] = c_date
                        channel_info["createDate"] = c_date
                    elif u_id_val:
                        sf = decode_snowflake_timestamp(u_id_val)
                        if sf:
                            check_user_info["create_date"] = sf
                            channel_info["createDate"] = sf

                    p_nm_ts = int(p_data.get("nickname_modify_time") or p_data.get("modify_time") or p_data.get("nickNameModifyTime") or 0)
                    if p_nm_ts > 0:
                        check_user_info["last_nickname_change"] = datetime.datetime.fromtimestamp(p_nm_ts).strftime("%d/%m/%Y")
        except Exception:
            pass

    # Nếu là nick trong tool mà chưa có Avatar, tự động lấy qua SSR Profile
    if is_self_account and not channel_info.get("avatar"):
        try:
            import studio_engine
            target_uname = (channel_info.get("uniqueId") or username).lstrip("@").strip()
            av_res = studio_engine.fetch_user_avatar(target_uname, cookie_str=effective_cookie, proxy=matched_acc_proxy)
            if av_res:
                channel_info["avatar"] = av_res
        except Exception:
            pass

    # 3. Kênh ngoài: Dùng requests lấy nhanh HTML trang chủ để lấy secUid & nickname
    ssr_status_code = -1
    ssr_video_count_known = False
    is_private_account = False
    if username and (not channel_info.get("avatar") or not channel_info.get("followers") or not sec_uid):
        try:
            s = requests.Session()
            s.headers.update({
                "User-Agent": matched_acc_ua,
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
            })
            if matched_acc_proxy:
                p_c = matched_acc_proxy.strip()
                if not p_c.startswith(("http://", "https://", "socks5://")):
                    p_c = f"http://{p_c}"
                s.proxies.update({"http": p_c, "https": p_c})

            cookie_header = effective_cookie if effective_cookie else ""

            req_headers = {}
            if cookie_header:
                req_headers["Cookie"] = cookie_header

            r = s.get(f"https://www.tiktok.com/@{username}", headers=req_headers, timeout=8)
            html = r.text
            if html:
                m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>', html)
                if m:
                    try:
                        u_json = json.loads(m.group(1))
                        scope = u_json.get("__DEFAULT_SCOPE__", {})
                        u_detail = scope.get("webapp.user-detail", {})
                        if u_detail.get("statusCode") is not None:
                            try:
                                ssr_status_code = int(u_detail.get("statusCode"))
                            except Exception:
                                pass
                        u_info = u_detail.get("userInfo", {})
                        u = u_info.get("user", {})
                        st = u_info.get("stats", {})

                        if u.get("privateAccount") is True or u.get("secret") is True or ssr_status_code == 10222:
                            is_private_account = True
                            check_user_info["is_private_account"] = True

                        if u.get("uniqueId"):
                            raw_u = str(u["uniqueId"]).lstrip("@").strip()
                            channel_info["uniqueId"] = raw_u
                            check_user_info["username"] = f"@{raw_u}"
                        if u.get("nickname"):
                            channel_info["nickname"] = u["nickname"]
                            check_user_info["screen_name"] = u["nickname"]
                        if u.get("avatarLarger") or u.get("avatarMedium"):
                            channel_info["avatar"] = u.get("avatarLarger") or u.get("avatarMedium") or ""
                        if st.get("followerCount") is not None:
                            channel_info["followers"] = int(st.get("followerCount") or 0)
                        if st.get("videoCount") is not None:
                            channel_info["videoCount"] = int(st.get("videoCount") or 0)
                            ssr_video_count_known = True
                        c_sec = u.get("secUid", "")
                        if c_sec:
                            sec_uid = c_sec

                        # Trích xuất UID ngay tại SSR
                        u_num = u.get("id") or u.get("uid") or ""
                        if u_num and check_user_info["uid"] in ("---", ""):
                            check_user_info["uid"] = str(u_num)

                        create_ts = int(u.get("createTime") or 0)
                        if create_ts > 0:
                            channel_info["createDate"] = datetime.datetime.fromtimestamp(create_ts).strftime("%d/%m/%Y")
                            check_user_info["create_date"] = channel_info["createDate"]
                        elif u_num:
                            sf_date = decode_snowflake_timestamp(u_num)
                            if sf_date:
                                channel_info["createDate"] = sf_date
                                check_user_info["create_date"] = sf_date

                        # Trích xuất thời điểm đổi nickname lần cuối (nếu có)
                        nick_mod = int(u.get("nickNameModifyTime") or u.get("nickNameModifyTs") or 0)
                        if nick_mod > 0:
                            check_user_info["last_nickname_change"] = datetime.datetime.fromtimestamp(nick_mod).strftime("%d/%m/%Y")
                        else:
                            check_user_info["last_nickname_change"] = "Chưa đổi lần nào"

                        # Trích xuất quốc gia kênh (CHỈ lấy từ chính user / biz context của kênh, KHÔNG lấy từ app_context của viewer mượn)
                        if not detected_account_country:
                            c_u = (u.get("region") or u.get("country") or "").strip().upper()
                            if c_u and c_u not in ("NONE", "AUTO", "EU") and not c_u.endswith("_TTP"):
                                detected_account_country = c_u
                            else:
                                biz_ctx = scope.get("webapp.biz-context", {})
                                d_kind = str(biz_ctx.get("domains", {}).get("kind") or "").lower()
                                v_reg = str(biz_ctx.get("vregion") or "").upper()
                                v_geo = str(biz_ctx.get("vgeo") or "").upper()
                                av_url = str(channel_info.get("avatar") or "")
                                if d_kind == "us" or "US" in v_reg or "US" in v_geo or "tiktokcdn-us.com" in av_url or "useast" in av_url:
                                    detected_account_country = "US"
                                elif "GB" in v_reg or "UK" in v_reg:
                                    detected_account_country = "GB"
                                elif "VN" in v_reg or "VN" in v_geo:
                                    detected_account_country = "VN"
                    except Exception:
                        pass

                if not sec_uid and not m and ssr_status_code not in (10202, 10203, 10204, 10221, 10223):
                    m_sec = re.search(r'"secUid":"([^"]+)"', html)
                    if m_sec:
                        sec_uid = m_sec.group(1)
                    m_nick = re.search(r'"nickname":"([^"]+)"', html)
                    if m_nick:
                        channel_info["nickname"] = m_nick.group(1).encode("utf-8").decode("unicode_escape", errors="ignore")
                        check_user_info["screen_name"] = channel_info["nickname"]
                    m_avatar = re.search(r'"avatarLarger":"([^"]+)"', html) or re.search(r'"avatarThumb":"([^"]+)"', html)
                    if m_avatar:
                        channel_info["avatar"] = m_avatar.group(1).replace("\\u0026", "&")
                    m_fans = re.search(r'"followerCount":([0-9]+)', html)
                    if m_fans:
                        channel_info["followers"] = int(m_fans.group(1))
                    m_vids = re.search(r'"videoCount":([0-9]+)', html)
                    if m_vids:
                        channel_info["videoCount"] = int(m_vids.group(1))
                        ssr_video_count_known = True
                    if re.search(r'"privateAccount"\s*:\s*true', html):
                        is_private_account = True
                    m_uid = re.search(r'"userId":"([0-9]+)"', html) or re.search(r'"authorId":"([0-9]+)"', html)
                    if m_uid:
                        check_user_info["uid"] = m_uid.group(1)
                        if channel_info.get("createDate") in ("", "—"):
                            sf_date = decode_snowflake_timestamp(m_uid.group(1))
                            if sf_date:
                                channel_info["createDate"] = sf_date
                                check_user_info["create_date"] = sf_date
                    m_nick_mod = re.search(r'"nickNameModifyTime":([0-9]+)', html)
                    if m_nick_mod and check_user_info.get("last_nickname_change") in ("—", ""):
                        nm_ts = int(m_nick_mod.group(1))
                        if nm_ts > 0:
                            check_user_info["last_nickname_change"] = datetime.datetime.fromtimestamp(nm_ts).strftime("%d/%m/%Y")
                        else:
                            check_user_info["last_nickname_change"] = "Chưa đổi lần nào"
        except Exception:
            pass

    # Nếu TikTok SSR đã xác nhận rõ tài khoản không tồn tại hoặc đã bị đình chỉ (10202, 10203, 10204, 10221, 10223) -> trả kết quả ngay ~0.8s, không chạy Playwright
    if not is_self_account and not sec_uid and not target_vid and ssr_status_code in (10202, 10203, 10204, 10221, 10223):
        print(f"[CheckDupWeb] @{username}: TikTok SSR trả mã {ssr_status_code} (không tồn tại / bị cấm) -> dừng ngay, bỏ qua Playwright")
        return {
            "success": False,
            "error": f"Không tìm thấy kênh hoặc tài khoản TikTok @{username} không tồn tại / đã bị đình chỉ!"
        }

    if detected_account_country and detected_account_country not in ("NONE", "AUTO", "EU") and not detected_account_country.endswith("_TTP"):
        channel_info["region"] = detected_account_country
    else:
        channel_info["region"] = "US"

    reg_disp = format_full_country_display(channel_info["region"])
    channel_info["region_display"] = reg_disp
    check_user_info["country"] = reg_disp

    raw_items = []
    all_studio_items = []
    all_fetched_api_items = []
    has_more = False
    next_cursor = 0

    # 4. LẤY VIDEO & QUÉT ÁN PHẠT:
    # Trường hợp A: Tài khoản có trong hệ thống Tool (Dùng Cookie Studio quét 100% án phạt & FYP)
    if is_self_account and api_get_post_list and effective_cookie:
        try:
            posts_res = api_get_post_list(
                effective_cookie, matched_acc_ua, matched_acc_proxy,
                sec_uid=sec_uid, max_cursor=max_cursor, count=50
            ) or {}
            all_studio_items = posts_res.get("items") or []
            if not all_studio_items:
                try:
                    import studio_engine
                    st_res = studio_engine.get_studio_post_list(
                        effective_cookie, proxy=matched_acc_proxy, count=50, max_cursor=max_cursor, sec_uid=sec_uid
                    )
                    if st_res.get("items"):
                        all_studio_items = st_res["items"]
                        if st_res.get("author_avatar") and not channel_info.get("avatar"):
                            channel_info["avatar"] = st_res["author_avatar"]
                except Exception:
                    pass

            if all_studio_items:
                offset = int(max_cursor) if max_cursor > 0 else 0
                raw_items = all_studio_items[offset:offset + max_videos]
                next_cursor = offset + len(raw_items)
                total_vids = int(channel_info.get("videoCount") or 0)
                if total_vids <= 0:
                    total_vids = len(all_studio_items)
                    channel_info["videoCount"] = total_vids
                has_more = next_cursor < total_vids
                if posts_res.get("author_avatar") and not channel_info.get("avatar"):
                    channel_info["avatar"] = posts_res["author_avatar"]

                if raw_items:
                    def _run_fyp_sync():
                        if sync_items_penalty_data:
                            try:
                                sync_items_penalty_data(raw_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                            except Exception as pen_err:
                                print(f"[CheckDupWeb] Lỗi sync_items_penalty_data: {pen_err}")

                    def _run_crp_sync():
                        try:
                            sync_items_crp_punishment_data(raw_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                        except Exception as crp_err:
                            print(f"[CheckDupWeb] Lỗi sync_items_crp_punishment_data: {crp_err}")

                    with ThreadPoolExecutor(max_workers=2) as sync_pool:
                        f1 = sync_pool.submit(_run_fyp_sync)
                        f2 = sync_pool.submit(_run_crp_sync)
                        f1.result()
                        f2.result()
                    for _p in raw_items:
                        if _p.get("is_crp_punished"):
                            _p["is_restored"] = False
                            _p["isRestored"] = False
                            _app_st = int(_p.get("appeal_status") or 0)
                            if _app_st in (2, 3, 4):
                                _msg = "Bị đánh gậy Không nguyên tác · Loại khỏi Quỹ BKT (Kháng xịt / Đã kháng)"
                                _p["crp_penalty_details"] = _msg
                                if not _p.get("has_penalty"):
                                    _p["penalty_details"] = _msg
        except Exception as e_studio:
            print(f"[CheckDupWeb] Lỗi api_get_post_list: {e_studio}")

    # Kênh ngoài đã xác nhận chắc chắn từ SSR là chưa đăng video nào (videoCount == 0) hoặc là Tài khoản Riêng tư (privateAccount == True)
    # -> không gọi Creator API và tuyệt đối không chạy Playwright (~30s vô ích), trả kết quả tức thì (~0.8s)
    is_external_private_acc = bool(is_private_account and not is_self_account and not target_vid)
    is_zero_videos_confirmed = bool(
        ssr_video_count_known and int(channel_info.get("videoCount") or 0) == 0
        and not is_private_account and not is_self_account and not target_vid
    )
    if is_external_private_acc:
        print(f"[CheckDupWeb] @{username}: Tài khoản Riêng tư (Private Account, mã {ssr_status_code}) -> bỏ qua Creator API & Playwright")
    elif is_zero_videos_confirmed:
        print(f"[CheckDupWeb] @{username}: Kênh có 0 video trên SSR -> bỏ qua Creator API & Playwright")

    # Trường hợp B1: Thử quét siêu tốc qua Creator API (xoay vòng tối đa 3-4 cookie mượn nếu cookie đầu bị giới hạn)
    creator_meta: Dict[str, Any] = {}
    if not raw_items and sec_uid and not is_external_private_acc and not is_zero_videos_confirmed:
        cookie_candidates: List[Tuple[str, str, str]] = []
        seen_ck = set()
        if effective_cookie:
            cookie_candidates.append((effective_cookie, matched_acc_ua, ""))
            seen_ck.add(effective_cookie)
        for p_c, p_ua, _ in get_borrowed_cookies_pool():
            if p_c and p_c not in seen_ck:
                cookie_candidates.append((p_c, p_ua or matched_acc_ua, ""))
                seen_ck.add(p_c)
            if len(cookie_candidates) >= 4:
                break

        api_items = []
        fetch_limit = max(max_videos + max_cursor, 35 if target_vid else (max_videos + max_cursor))
        for cand_ck, cand_ua, cand_px in cookie_candidates:
            creator_meta = {}
            api_items = fetch_external_channel_videos_creator_api(
                sec_uid=sec_uid,
                cookie_str=cand_ck,
                ua_str=cand_ua,
                proxy_str=cand_px,
                max_videos=fetch_limit,
                max_cursor=max_cursor,
                result_meta=creator_meta
            )
            if api_items or creator_meta.get("confirmed_empty"):
                break

        if api_items:
            all_fetched_api_items = api_items
            offset = int(max_cursor) if max_cursor > 0 else 0
            raw_items = api_items[offset:offset + max_videos]
            next_cursor = offset + len(raw_items)
            total_vids = int(channel_info.get("videoCount") or 0)
            if total_vids > 0:
                has_more = next_cursor < total_vids
            else:
                has_more = len(api_items) > next_cursor or len(raw_items) >= max_videos
            print(f"[CheckDupWeb] Quét siêu tốc qua Creator API thành công: {len(raw_items)} video cho @{username}")

    # Kênh có video nhưng TikTok xác nhận không có video công khai nào (toàn bộ để Bạn bè / Riêng tư)
    no_public_videos = bool(
        not raw_items and creator_meta.get("confirmed_empty")
        and int(channel_info.get("videoCount") or 0) > 0 and max_cursor == 0 and not target_vid
    )
    # Bỏ qua hoàn toàn Playwright (~30s) nếu kênh đã xác nhận: 0 video, toàn bộ ẩn Bạn bè/Riêng tư, hoặc Tài khoản Riêng tư
    skip_playwright = bool(
        not target_vid and (
            no_public_videos
            or creator_meta.get("confirmed_empty")
            or is_external_private_acc
            or is_zero_videos_confirmed
            or (ssr_video_count_known and int(channel_info.get("videoCount") or 0) == 0)
        )
    )
    if no_public_videos:
        print(f"[CheckDupWeb] @{username}: TikTok xác nhận 0 video công khai / {channel_info.get('videoCount')} video -> bỏ qua Playwright")
    elif skip_playwright and not raw_items:
        print(f"[CheckDupWeb] @{username}: Xác nhận kênh trống / riêng tư -> bỏ qua Playwright")

    # Trường hợp B2: Kênh ngoài hoặc chưa lấy được -> Quét dự phòng qua Playwright Worker siêu tốc
    if not raw_items and username and not skip_playwright:
        channel_url = f"https://www.tiktok.com/@{username}"
        pw_cookie = effective_cookie or ""
        pw_items = fetch_external_channel_videos_playwright(
            channel_url,
            cookie_str=pw_cookie,
            proxy_str="",
            max_videos=max_videos + max_cursor,
            channel_info=channel_info,
        )
        if not pw_items and username:
            print(f"[CheckDupWeb] Quét lần 1 kênh @{username} trả về 0 video, đang thử lại lần 2...")
            time.sleep(0.5)
            pw_items = fetch_external_channel_videos_playwright(
                channel_url,
                cookie_str=effective_cookie or "",
                proxy_str="",
                max_videos=max_videos + max_cursor,
                channel_info=channel_info,
            )

        if pw_items:
            offset = int(max_cursor)
            raw_items = pw_items[offset:offset + max_videos]
            next_cursor = offset + len(raw_items)
            total_vids = int(channel_info.get("videoCount") or 0)
            if total_vids > 0:
                has_more = next_cursor < total_vids
            else:
                has_more = len(pw_items) > next_cursor or len(raw_items) >= max_videos

    # Nếu người dùng dán trực tiếp Link Video hoặc ID Video: ưu tiên đưa video đó lên đầu & kiểm tra thêm oEmbed / Mobile API
    if target_vid and max_cursor == 0:
        oembed_restricted = False
        try:
            oe_u = f"https://www.tiktok.com/@{username or 'tiktok'}/video/{target_vid}"
            r_oe = requests.get(
                "https://www.tiktok.com/oembed",
                params={"url": oe_u},
                headers={"User-Agent": matched_acc_ua},
                timeout=5,
            )
            if r_oe.status_code in (401, 403):
                oembed_restricted = True
        except Exception:
            pass

        def _vid_of(item_dict: dict) -> str:
            return str(item_dict.get("id") or item_dict.get("aweme_id") or item_dict.get("item_id") or "")

        found_idx = next((idx for idx, x in enumerate(raw_items) if _vid_of(x) == target_vid), -1)
        if found_idx >= 0:
            target_obj = raw_items.pop(found_idx)
            if oembed_restricted:
                target_obj["_oembed_restricted"] = True
            raw_items.insert(0, target_obj)
        else:
            extra_pool = list(all_studio_items or []) + list(all_fetched_api_items or [])
            found_extra = next((x for x in extra_pool if _vid_of(x) == target_vid), None)
            if found_extra is not None:
                if oembed_restricted:
                    found_extra["_oembed_restricted"] = True
                if is_self_account and effective_cookie:
                    try:
                        if sync_items_penalty_data:
                            sync_items_penalty_data([found_extra], effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                        sync_items_crp_punishment_data([found_extra], effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                    except Exception:
                        pass
                raw_items.insert(0, found_extra)
                if len(raw_items) > max_videos:
                    raw_items = raw_items[:max_videos]
            else:
                if not fallback_target_item:
                    try:
                        r_wm = requests.get("https://www.tikwm.com/api/", params={"url": target_vid}, timeout=6).json()
                        wm_d = r_wm.get("data") or {}
                        if wm_d.get("id"):
                            au = (wm_d.get("author") or {}).get("unique_id") or username
                            fallback_target_item = {
                                "id": str(wm_d.get("id")),
                                "desc": wm_d.get("title") or "",
                                "createTime": wm_d.get("create_time") or 0,
                                "duration": wm_d.get("duration") or 0,
                                "region": wm_d.get("region") or channel_info.get("region") or "US",
                                "cover_url": wm_d.get("origin_cover") or wm_d.get("cover") or "",
                                "play_count": wm_d.get("play_count") or 0,
                                "digg_count": wm_d.get("digg_count") or 0,
                                "comment_count": wm_d.get("comment_count") or 0,
                                "share_count": wm_d.get("share_count") or 0,
                                "collect_count": wm_d.get("collect_count") or 0,
                                "is_nff_or_nr": bool(wm_d.get("is_nff_or_nr")),
                                "author": {
                                    "uniqueId": au,
                                    "nickname": (wm_d.get("author") or {}).get("nickname") or au,
                                    "avatarLarger": (wm_d.get("author") or {}).get("avatar") or "",
                                },
                            }
                    except Exception:
                        pass
                if not fallback_target_item and allow_paid_groupid and _get_tikhub_api_key():
                    try:
                        th_map = _fetch_tikhub_multi_awemes([target_vid])
                        th_aw = th_map.get(str(target_vid))
                        if isinstance(th_aw, dict) and (th_aw.get("aweme_id") or th_aw.get("id")):
                            th_auth = th_aw.get("author") if isinstance(th_aw.get("author"), dict) else {}
                            th_stat = th_aw.get("statistics") if isinstance(th_aw.get("statistics"), dict) else {}
                            th_vid = th_aw.get("video") if isinstance(th_aw.get("video"), dict) else {}
                            th_cov = ((th_vid.get("origin_cover") or th_vid.get("cover") or {}).get("url_list") or [""])[0]
                            th_av = ((th_auth.get("avatar_larger") or th_auth.get("avatar_medium") or th_auth.get("avatar_thumb") or {}).get("url_list") or [""])[0]
                            au = str(th_auth.get("unique_id") or username or "").strip()
                            if au and not username:
                                username = au
                                channel_info["uniqueId"] = au
                            fallback_target_item = {
                                "id": str(th_aw.get("aweme_id") or th_aw.get("id")),
                                "group_id": str(th_aw.get("group_id") or ""),
                                "desc": th_aw.get("desc") or "",
                                "createTime": th_aw.get("create_time") or 0,
                                "duration": th_vid.get("duration") or 0,
                                "region": str(th_aw.get("region") or channel_info.get("region") or "US").upper(),
                                "cover_url": th_cov,
                                "play_count": th_stat.get("play_count") or 0,
                                "digg_count": th_stat.get("digg_count") or 0,
                                "comment_count": th_stat.get("comment_count") or 0,
                                "share_count": th_stat.get("share_count") or 0,
                                "collect_count": th_stat.get("collect_count") or 0,
                                "is_nff_or_nr": bool(th_aw.get("is_nff_or_nr")),
                                "author": {
                                    "id": str(th_auth.get("uid") or ""),
                                    "uniqueId": au,
                                    "nickname": th_auth.get("nickname") or au,
                                    "avatarLarger": th_av,
                                },
                            }
                    except Exception:
                        pass
                if fallback_target_item:
                    if oembed_restricted:
                        fallback_target_item["_oembed_restricted"] = True
                    raw_items.insert(0, fallback_target_item)
                    if len(raw_items) > max_videos:
                        raw_items = raw_items[:max_videos]

    # Trích xuất Avatar & Nickname từ author nếu chưa có
    if not channel_info.get("avatar") and raw_items:
        for it in raw_items:
            auth = it.get("author") if isinstance(it.get("author"), dict) else {}
            av = auth.get("avatarLarger") or auth.get("avatarMedium") or auth.get("avatarThumb") or ""
            if av:
                channel_info["avatar"] = av
                if auth.get("nickname") and (not channel_info.get("nickname") or channel_info.get("nickname") == username):
                    channel_info["nickname"] = auth["nickname"]
                    check_user_info["screen_name"] = auth["nickname"]
                if auth.get("uniqueId") and not channel_info.get("uniqueId"):
                    raw_auth_u = str(auth["uniqueId"]).lstrip("@").strip()
                    channel_info["uniqueId"] = raw_auth_u
                    check_user_info["username"] = f"@{raw_auth_u}"
                break

    if not channel_info.get("videoCount") and raw_items:
        channel_info["videoCount"] = len(raw_items)

    # Trích xuất UID & Ngày tạo từ UID Snowflake
    if raw_items:
        for it in raw_items:
            auth = it.get("author") if isinstance(it.get("author"), dict) else {}
            u_id = auth.get("id") or auth.get("uid") or it.get("author_user_id") or it.get("authorUserId") or ""
            if u_id:
                if check_user_info["uid"] in ("---", ""):
                    check_user_info["uid"] = str(u_id)
                if channel_info.get("createDate") in ("", "—", "---"):
                    sf_date = decode_snowflake_timestamp(u_id)
                    if sf_date:
                        channel_info["createDate"] = sf_date
                        check_user_info["create_date"] = sf_date
                break

        if channel_info.get("createDate") in ("", "—"):
            earliest_ts = 0
            for it in raw_items:
                pts = int(it.get("createTime") or it.get("create_time") or it.get("post_time") or 0)
                if pts > 0 and (earliest_ts == 0 or pts < earliest_ts):
                    earliest_ts = pts
            if earliest_ts > 0:
                dt_str = datetime.datetime.fromtimestamp(earliest_ts).strftime("%d/%m/%Y")
                channel_info["createDate"] = dt_str
                check_user_info["create_date"] = dt_str

    # 5. Định dạng và kiểm tra trùng lặp cho từng video
    # Chỉ tài khoản đã đăng nhập (VIP / Admin) mới kích hoạt quét trùng lặp Group ID & API trả phí
    video_list = []
    if allow_paid_groupid:
        tkm_cache = _load_tiktokmanager_cache()
        sync_items_tikhub_groupid(raw_items, tkm_cache, sec_uid=sec_uid, is_load_more=(max_cursor > 0))
    else:
        tkm_cache = {}
    ch_followers = int(channel_info.get("followers") or 0)

    for it in raw_items:
        aweme_id = str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "")
        if not aweme_id:
            continue

        cached_meta = tkm_cache.get(aweme_id, {})
        desc = it.get("desc") or it.get("title") or ""
        stats = it.get("stats") or it.get("statistics") or {}
        play_cnt = int(it.get("play_count") or stats.get("playCount") or stats.get("play_count") or 0)
        c_time = int(it.get("createTime") or it.get("create_time") or it.get("post_time") or 0)

        device_plat = str(it.get("device_platform") or "").lower()


        source_raw = str(it.get("source") or "").lower()


        c2pa = it.get("c2pa_info") if isinstance(it.get("c2pa_info"), dict) else {}


        anchors = str(it.get("anchors") or "").lower()


        is_capcut = bool(it.get("is_capcut") or c2pa.get("is_capcut") or "capcut" in anchors)


        shoot_tab = it.get("shoot_tab")


        is_explicit_web = (


            device_plat in ("web_pc", "pc", "web")


            or source_raw in ("web", "web_pc")


            or "web_upload" in str(it).lower()


        )


        if is_capcut:


            source_lbl = "CapCut"


            source_icon = "fa-scissors"


            device = "phone"


        elif is_explicit_web:


            source_lbl = "Web (PC)"


            source_icon = "fa-desktop"


            device = "web"


        else:


            if shoot_tab and str(shoot_tab) not in ("0", "None"):


                source_lbl = "Mobile (Camera)"


                source_icon = "fa-camera"


            else:


                source_lbl = "Phone (Gallery)"


                source_icon = "fa-mobile-screen"


            device = "phone"


        music_obj = it.get("music") if isinstance(it.get("music"), dict) else {}


        is_muted = bool(it.get("is_muted") or music_obj.get("mute_share") or music_obj.get("mute") or music_obj.get("status") == 2)


        video_obj = it.get("video") if isinstance(it.get("video"), dict) else {}


        v_h = int(video_obj.get("height") or 0)


        v_w = int(video_obj.get("width") or 0)


        quality_str = "1080p" if v_h >= 1080 else ("720p" if v_h >= 720 else ("480p" if v_h >= 480 else (f"{v_w}x{v_h}" if v_h > 0 else "Chuẩn")))

        ch_reg = str(channel_info.get("region") or "US").upper()
        region = str(it.get("region") or it.get("locationCreated") or it.get("country_code") or it.get("geo") or cached_meta.get("region") or ch_reg).upper()

        group_id = str(it.get("group_id") or it.get("groupId") or cached_meta.get("group_id") or "") if allow_paid_groupid else ""

        # Mã trạng thái kiểm duyệt TikTok (Mã 141 = Khiếu nại bản quyền, 140-149 = Bị gỡ bỏ / Vi phạm)
        st_val = it.get("status")
        st_code = 0
        if isinstance(st_val, (int, str)) and str(st_val).isdigit():
            st_code = int(st_val)
        elif isinstance(st_val, dict):
            st_code = int(st_val.get("video_status") or 0)

        # Nhận diện khiếu nại bản quyền & vi phạm tiêu chuẩn
        is_copyright = bool(
            st_code == 141
            or it.get("is_copyright") is True
            or (isinstance(it.get("desc"), str) and "bản quyền" in it["desc"].lower())
            or (isinstance(it.get("penalty_details"), str) and "bản quyền" in it["penalty_details"].lower())
        )
        is_takedown = bool(st_code in (140, 141, 142, 143, 144) or it.get("is_takedown") is True)

        # Cờ kiểm duyệt chính sách (penaltyContext - lộ cả ở kênh ngoài qua Creator API mà không cần cookie chính chủ)
        penalty_ctx = it.get("penaltyContext") if isinstance(it.get("penaltyContext"), dict) else {}
        display_penalty_type = int(penalty_ctx.get("display_penalty_type") or 0)
        display_policy = int(penalty_ctx.get("display_policy") or 0)
        ec_verdict = int(penalty_ctx.get("ec_global_verdict") or 0)
        tns_status = int(penalty_ctx.get("tns_status") or 0)
        arb_status = int(penalty_ctx.get("arb_content_safety_status") or 0)
        pc_status = int(penalty_ctx.get("status") or 0)
        pc_vid_status = int(penalty_ctx.get("video_status") or 0)

        if pc_vid_status == 141:
            is_copyright = True
            is_takedown = True
            if not st_code:
                st_code = 141
        elif pc_vid_status in (140, 142, 143, 144):
            is_takedown = True
            if not st_code:
                st_code = pc_vid_status

        is_restored = bool(it.get("is_restored", False))
        # Tín hiệu cấm đề xuất FYP từ hệ thống kiểm duyệt TNS / Mobile NFF / oEmbed 403 (0% báo nhầm trên video sạch)
        is_tns_fyp_ban = (not is_restored) and bool(
            (ec_verdict not in (0, 1))
            or (tns_status not in (0, 102))
            or (arb_status not in (0, 102))
            or (pc_status not in (0, 102))
            or (it.get("is_nff_or_nr") is True)
            or (it.get("_oembed_restricted") is True)
        )

        item_ctrl = it.get("item_control") if isinstance(it.get("item_control"), dict) else {}
        can_repost = item_ctrl.get("can_repost", True)
        can_share = item_ctrl.get("can_share", True)
        is_prohibited = bool(it.get("isProhibited", False))

        status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
        is_priv = bool(status_dict.get("is_private") or it.get("is_private") or it.get("privateItem") or is_copyright or (it.get("visibility") == 2))

        dur = it.get("duration") or (it.get("video", {}).get("duration") if isinstance(it.get("video"), dict) else 0) or 0
        try:
            dur_sec = int(dur) // 1000 if int(dur) > 1000 else int(dur)
            duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else ""
        except Exception:
            dur_sec = 0
            duration_str = ""

        digg_cnt = int(it.get("digg_count") or it.get("like_count") or stats.get("diggCount") or stats.get("digg_count") or 0)
        comment_cnt = int(it.get("comment_count") or stats.get("commentCount") or stats.get("comment_count") or 0)
        share_cnt = int(it.get("share_count") or stats.get("shareCount") or stats.get("share_count") or 0)
        collect_cnt = int(it.get("collect_count") or it.get("favorite_count") or stats.get("collectCount") or stats.get("collect_count") or 0)

        is_crp_punished = bool(it.get("is_crp_punished", False))
        has_fyp_pen = bool(
            is_copyright
            or is_takedown
            or it.get("has_penalty", False)
            or ((display_penalty_type != 0 or display_policy != 0) and not is_restored)
            or is_tns_fyp_ban
            or is_prohibited
            or is_muted
        )
        has_pen = bool(has_fyp_pen or is_crp_punished)

        has_real_group_dup = bool(group_id and group_id != aweme_id and group_id not in ("0", "None", "null"))
        is_dup = bool(
            has_real_group_dup
            or is_crp_punished
            or cached_meta.get("is_dup", False)
            or it.get("is_dup", False)
            or it.get("isDup", False)
            or ((display_penalty_type != 0 or display_policy != 0) and not is_restored)
        )

        dup_reason = ""
        dup_origin_vid = ""
        if is_copyright:
            dup_reason = "Khiếu nại bản quyền (Âm thanh/Hình ảnh vi phạm - Mã 141)"
        elif is_crp_punished:
            dup_reason = str(it.get("crp_penalty_details") or "Bị đánh gậy Không nguyên tác · Loại khỏi Quỹ BKT (Creator Rewards)")
            if has_real_group_dup:
                dup_origin_vid = group_id
        elif has_real_group_dup:
            dup_reason = f"Trùng lặp Group ID (Group: {group_id})"
            dup_origin_vid = group_id
        elif cached_meta.get("is_dup"):
            dup_reason = "Trùng lặp nội dung (Khớp cache TikTok Manager)"
            c_gid = str(cached_meta.get("group_id") or "").strip()
            if c_gid and c_gid != aweme_id and c_gid not in ("0", "None", "null"):
                dup_origin_vid = c_gid
        elif (display_penalty_type != 0 or display_policy != 0) and not is_restored:
            dup_reason = "Vi phạm chính sách trùng lặp / Bản quyền nội dung"

        age_hours = (time.time() - c_time) / 3600 if c_time > 0 else 0

        # Phân biệt rõ:
        # 1. Trùng lặp / Không nguyên tác (is_dup / is_crp_punished): trùng Group ID hoặc bị đánh gậy Không nguyên tác BKT
        # 2. Án phạt FYP (has_fyp_pen): vi phạm tiêu chuẩn cộng đồng, TNS penalty, tắt tiếng bản quyền
        # 3. Mất FYP (is_fyp): video không đủ điều kiện vào For You feed
        # 4. Shadowban (is_shadow): hạn chế phân phối đề xuất FYP
        is_non_crp_dup = bool(is_dup and not is_crp_punished)
        is_fyp = bool(
            is_copyright
            or is_takedown
            or is_non_crp_dup
            or has_real_group_dup
            or is_tns_fyp_ban
            or is_muted
            or (can_repost is False)
            or (can_share is False)
            or is_prohibited
            or has_fyp_pen
            or it.get("is_fyp_restricted", False)
            or cached_meta.get("is_fyp_restricted", False)
        )

        is_shadow = bool(it.get("shadow", False) or it.get("shadowban", False))
        shadow_reason = str(it.get("shadowReason") or "")
        # Shadowban & FYP restriction: Xác định từ tín hiệu thực của TikTok (status 141, penaltyContext TNS/EC, NFF, oEmbed 403, item_control, isProhibited, mute)
        if is_copyright:
            shadow_reason = "Khiếu nại bản quyền (TikTok tự động gỡ khỏi FYP & chuyển Riêng tư)"
            is_fyp = True
            is_shadow = True
        elif is_takedown:
            shadow_reason = f"Video bị gỡ bỏ / Vi phạm tiêu chuẩn TikTok (Mã trạng thái {st_code})"
            is_fyp = True
            is_shadow = True
        elif is_prohibited:
            shadow_reason = "Nội dung vi phạm tiêu chuẩn cộng đồng TikTok (Bị cấm phân phối)"
            is_fyp = True
            is_shadow = True
        elif is_tns_fyp_ban:
            if (ec_verdict not in (0, 1)) or it.get("is_nff_or_nr") or it.get("_oembed_restricted"):
                shadow_reason = shadow_reason or "Không đủ điều kiện xuất hiện trên Bảng tin Cho bạn (Hạn chế đề xuất FYP · TNS)"
            else:
                tns_code = tns_status if tns_status not in (0, 102) else (arb_status if arb_status not in (0, 102) else pc_status)
                shadow_reason = shadow_reason or f"Hạn chế kiểm duyệt an toàn nội dung TikTok (Mã TNS {tns_code})"
            is_fyp = True
            is_shadow = True
        elif is_muted:
            shadow_reason = shadow_reason or "Vi phạm bản quyền âm thanh (Video bị tắt tiếng · Hạn chế FYP)"
            is_fyp = True
            is_shadow = True
        elif can_repost is False or can_share is False:
            shadow_reason = shadow_reason or "Hạn chế phân phối Bảng tin Cho bạn (Chặn chia sẻ / đăng lại)"
            is_fyp = True
            is_shadow = True
        elif has_fyp_pen:
            shadow_reason = shadow_reason or "Án phạt kiểm duyệt nội dung TikTok (Hạn chế FYP)"
            is_fyp = True
            is_shadow = True
        elif is_non_crp_dup or has_real_group_dup:
            shadow_reason = shadow_reason or dup_reason or "Nội dung trùng lặp (Không đủ điều kiện FYP)"
            is_fyp = True

        raw_pen_str = str(it.get("penalty_details") or "")
        if "kháng về thành công" in raw_pen_str.lower() and (has_pen or is_dup):
            raw_pen_str = ""
        pen_details_str = dup_reason if is_crp_punished and not has_fyp_pen else (raw_pen_str or dup_reason or shadow_reason or "")
        if is_copyright and not raw_pen_str:
            pen_details_str = "Khiếu nại bản quyền (Mã 141)"
        if not shadow_reason and is_shadow:
            shadow_reason = pen_details_str or "Video bị hạn chế xuất hiện trên FYP"

        cover_url = ""
        all_cover_urls = []
        if isinstance(it.get("cover_urls"), list):
            for u in it["cover_urls"]:
                if u and str(u).startswith("http") and u not in all_cover_urls:
                    all_cover_urls.append(str(u))
        cover_val = it.get("cover_url")
        if isinstance(cover_val, list) and cover_val:
            for item_c in cover_val:
                if isinstance(item_c, str) and item_c.strip().startswith("http") and item_c.strip() not in all_cover_urls:
                    all_cover_urls.append(item_c.strip())
            if all_cover_urls:
                cover_url = all_cover_urls[0]
        elif isinstance(cover_val, str) and cover_val.strip().startswith("http"):
            u_str = cover_val.strip()
            if u_str not in all_cover_urls:
                all_cover_urls.append(u_str)
            cover_url = u_str
        elif it.get("video") and isinstance(it["video"], dict):
            u_list = it["video"].get("cover", {}).get("url_list", []) if isinstance(it["video"].get("cover"), dict) else []
            for u in u_list:
                if u and str(u).startswith("http") and u not in all_cover_urls:
                    all_cover_urls.append(str(u))
            cover_url = u_list[0] if u_list else (str(it["video"].get("cover") or ""))
        elif it.get("cover") and isinstance(it["cover"], dict):
            u_list = it["cover"].get("url_list") or []
            for u in u_list:
                if u and str(u).startswith("http") and u not in all_cover_urls:
                    all_cover_urls.append(str(u))
            cover_url = u_list[0] if u_list else ""
        if not cover_url and all_cover_urls:
            cover_url = all_cover_urls[0]

        clean_u = (username or "").lstrip("@").strip()
        share_url = f"https://www.tiktok.com/@{clean_u}/video/{aweme_id}" if clean_u else f"https://www.tiktok.com/video/{aweme_id}"

        date_local = ""
        date_utc = ""
        if c_time > 0:
            try:
                date_utc = datetime.datetime.utcfromtimestamp(c_time).strftime("%d/%m/%Y %H:%M")
                date_local = datetime.datetime.fromtimestamp(c_time).strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass

        origin_url = ""
        target_orig_id = dup_origin_vid or (group_id if has_real_group_dup else "")
        if target_orig_id and target_orig_id != aweme_id and target_orig_id not in ("0", "None", "null"):
            origin_url = f"https://www.tiktok.com/@tiktok/video/{target_orig_id}"

        post_data = {
            "id": aweme_id,
            "awemeId": aweme_id,
            "aweme_id": aweme_id,
            "group_id": group_id,
            "groupId": group_id,
            "origin_url": origin_url,
            "has_origin_link": bool(origin_url),
            "desc": desc,
            "desc_clean": clean_desc_text(desc),
            "play_count": play_cnt,
            "play_count_fmt": format_count_compact(play_cnt),
            "create_time": c_time,
            "date_local": date_local,
            "date_utc": date_utc,
            "device": device,
            "source": source_lbl,
            "source_icon": source_icon,
            "is_muted": is_muted,
            "is_prohibited": is_prohibited,
            "quality": quality_str,
            "region": region,
            "region_display": format_full_country_display(region),
            "shadow": is_shadow,
            "shadowReason": shadow_reason,
            "is_dup": is_dup,
            "is_crp_punished": is_crp_punished,
            "is_fyp_restricted": is_fyp,
            "has_penalty": has_pen,
            "is_copyright": is_copyright,
            "is_takedown": is_takedown,
            "status_code": st_code,
            "penalty_details": pen_details_str,
            "cover_url": cover_url,
            "duration_str": duration_str,
            "share_url": share_url,
            "digg_count": digg_cnt,
            "digg_count_fmt": format_count_compact(digg_cnt),
            "comment_count": comment_cnt,
            "comment_count_fmt": format_count_compact(comment_cnt),
            "share_count": share_cnt,
            "share_count_fmt": format_count_compact(share_cnt),
            "collect_count": collect_cnt,
            "collect_count_fmt": format_count_compact(collect_cnt),
            "is_private": is_priv,
        }
        video_list.append(post_data)

    # Chỉ báo lỗi nếu hoàn toàn không tìm thấy thông tin kênh (không có avatar, followers, videoCount, UID)
    if not video_list and not channel_info.get("avatar") and not channel_info.get("followers") and not channel_info.get("videoCount") and not sec_uid and not is_self_account and check_user_info.get("uid") in ("---", ""):
        return {
            "success": False,
            "error": f"Không thể kết nối hoặc không tìm thấy kênh TikTok @{username}. Vui lòng kiểm tra lại đường dẫn / ID!"
        }

    total_loaded = len(video_list)
    dup_count = sum(1 for v in video_list if v.get("is_dup"))
    copyright_count = sum(1 for v in video_list if v.get("is_copyright"))
    fyp_pen_count = sum(1 for v in video_list if v.get("is_fyp_restricted") or v.get("shadow") or v.get("is_copyright"))
    bad_count = sum(1 for v in video_list if v.get("is_dup") or v.get("is_fyp_restricted") or v.get("shadow") or v.get("has_penalty") or v.get("is_copyright"))
    clean_count = max(0, total_loaded - bad_count)

    stats_summary = {
        "total_videos": total_loaded,
        "duplicate_count": dup_count,
        "penalty_count": fyp_pen_count,
        "copyright_count": copyright_count,
        "clean_count": clean_count,
        "dup_percentage": round((bad_count / total_loaded * 100), 1) if total_loaded > 0 else 0,
    }

    channel_info["followers_fmt"] = format_count_compact(channel_info.get("followers", 0))

    if video_list and not is_self_account:
        from collections import Counter
        reg_counter = Counter(v.get("region") for v in video_list if v.get("region") and v.get("region") not in ("NONE", "AUTO", "EU"))
        if reg_counter:
            top_reg, _ = reg_counter.most_common(1)[0]
            if top_reg and top_reg != channel_info.get("region"):
                channel_info["region"] = top_reg
                reg_disp = format_full_country_display(top_reg)
                channel_info["region_display"] = reg_disp
                check_user_info["country"] = reg_disp

    ch_vids = int(channel_info.get("videoCount") or 0)
    video_notice = ""
    if total_loaded == 0:
        if is_private_account and not is_self_account:
            video_notice = (f"Kênh @{username} đang để chế độ Tài khoản Riêng tư (Private Account) "
                            f"hoặc chưa có video công khai nào hiển thị với người ngoài.")
        elif ch_vids > 0 and no_public_videos:
            video_notice = (f"Kênh @{username} có {ch_vids} video nhưng chủ kênh đã ẩn toàn bộ sang chế độ "
                            f"Riêng tư / Bạn bè (0 video công khai hiển thị trên trang cá nhân).")
        elif ch_vids > 0:
            video_notice = f"Kênh @{username} có {ch_vids} video trên TikTok nhưng các video này có thể đang ở chế độ Riêng tư (Private), hoặc TikTok tạm thời giới hạn tải danh sách công khai."
        else:
            video_notice = f"Kênh @{username} hiện chưa đăng tải video nào."

    return {
        "success": True,
        "vip_mode": bool(allow_paid_groupid),
        "channel": channel_info,
        "secUid": sec_uid,
        "list": video_list,
        "stats_summary": stats_summary,
        "user_info": check_user_info,
        "hasMore": has_more,
        "maxCursor": next_cursor,
        "video_notice": video_notice
    }
