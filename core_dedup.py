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

def _build_requests_proxies(proxy: str = "") -> Optional[Dict[str, str]]:
    """Chuẩn hóa chuỗi proxy sang socks5h/http cho thư viện requests và bỏ qua proxy đã chết."""
    if not proxy:
        return None
    p_c = proxy.strip()
    if not p_c or p_c in _DEAD_PROXIES:
        return None
    if p_c.startswith("socks5://"):
        p_c = "socks5h://" + p_c[len("socks5://"):]
    elif not p_c.startswith(("http://", "https://", "socks5h://")):
        p_c = f"http://{p_c}"
    return {"http": p_c, "https": p_c}


def api_get_penalty_details_native(cookie_str: str, ua: str = "", proxy: str = "", vid: str = "", create_time: int = 0, region: str = "") -> dict:
    """Lấy chi tiết biên bản xử phạt kiểm duyệt video (getPenaltyDetails) từ TikTok Moderation API."""
    if not vid:
        return {"success": False}
    session = requests.Session()
    proxies = _build_requests_proxies(proxy)

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
            try:
                res = session.get(url, headers=headers, params=params, proxies=proxies, timeout=6 if proxies else 8)
            except Exception:
                if proxies:
                    res = session.get(url, headers=headers, params=params, proxies=None, timeout=8)
                else:
                    raise
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
                            top_det = "Mất đề xuất FYP: Nội dung không nguyên bản / Trùng lặp"
                        elif "11" in rc_list:
                            top_det = "Mất đề xuất FYP: Tương tác giả / Hành vi lừa dối"
                        elif "10" in rc_list:
                            top_det = "Mất đề xuất FYP: Nội dung nhạy cảm / Hành vi nguy hiểm / Giật gân"
                        elif "12" in rc_list:
                            top_det = "Mất đề xuất FYP: Spam / Thao túng hệ thống"
                        elif "9" in rc_list or "999" in rc_list:
                            top_det = "Mất đề xuất FYP: Không đủ điều kiện lên Bảng tin Xu hướng (Cho bạn)"
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
        vid = str(p.get("aweme_id") or p.get("item_id") or p.get("vid") or p.get("id") or "").strip()
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
                    p["shadowban"] = True
                    top_d = p_res.get("top_penalty_details")
                    if top_d:
                        p["penalty_details"] = top_d
                        p["shadowReason"] = top_d
                        if "trùng lặp" in top_d.lower() or "không nguyên bản" in top_d.lower():
                            p["is_dup"] = True
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
    if reg_val.upper() == "UK":
        reg_val = "GB"
    now_ts = int(time.time())
    start_ts = str(now_ts - 86400 * 365)
    end_ts = str(now_ts)

    proxies = _build_requests_proxies(proxy)

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
        req_params = {
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
        }
        req_hdrs = {
            "User-Agent": ua_val,
            "Cookie": ck_header,
            "Referer": f"https://www.tiktok.com/tiktokstudio/monetization/item/{vid}/",
            "Accept": "application/json, text/plain, */*",
        }
        try:
            try:
                r = _get_crp_sess().get(
                    "https://www.tiktok.com/tiktok/v1/creator/m10n_center/video_reward_analytics",
                    params=req_params,
                    headers=req_hdrs,
                    proxies=proxies,
                    timeout=5,
                )
            except Exception:
                if proxies:
                    r = _get_crp_sess().get(
                        "https://www.tiktok.com/tiktok/v1/creator/m10n_center/video_reward_analytics",
                        params=req_params,
                        headers=req_hdrs,
                        proxies=None,
                        timeout=6,
                    )
                else:
                    raise
            if r.status_code == 200 and r.content:
                j = r.json()
                crp_d = j.get("crp_analytics_data") or {}
                vid_info = j.get("video_analytics_video_info") or {}
                progs = vid_info.get("video_analytics_programs") or []
                has_explicit_reason = bool(
                    crp_d.get("disqualify_reason")
                    or crp_d.get("punish_reason")
                    or crp_d.get("ineligible_reason")
                    or any(
                        isinstance(pr, dict) and (
                            pr.get("disqualify_reason")
                            or pr.get("punish_reason")
                            or pr.get("ineligible_reason")
                        )
                        for pr in progs
                    )
                )
                is_pun = (
                    (crp_d.get("is_punished") is True)
                    or any(isinstance(pr, dict) and pr.get("is_punished") is True for pr in progs)
                    or has_explicit_reason
                )
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
    proxies = _build_requests_proxies(proxy)

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
            try:
                res = session.post(cur_url, headers=headers, json=payload, proxies=proxies, timeout=10)
            except Exception:
                if proxies:
                    res = session.post(cur_url, headers=headers, json=payload, proxies=None, timeout=10)
                else:
                    raise
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
    "VN": ("Việt Nam", "🇻🇳"),
    "BD": ("Bangladesh", "🇧🇩"),
    "IN": ("Ấn Độ", "🇮🇳"),
    "PK": ("Pakistan", "🇵🇰"),
    "LK": ("Sri Lanka", "🇱🇰"),
    "NP": ("Nepal", "🇳🇵"),
    "BT": ("Bhutan", "🇧🇹"),
    "MV": ("Maldives", "🇲🇻"),
    "AF": ("Afghanistan", "🇦🇫"),
    "CN": ("Trung Quốc", "🇨🇳"),
    "TW": ("Đài Loan", "🇹🇼"),
    "HK": ("Hồng Kông", "🇭🇰"),
    "MO": ("Ma Cao", "🇲🇴"),
    "JP": ("Nhật Bản", "🇯🇵"),
    "KR": ("Hàn Quốc", "🇰🇷"),
    "KP": ("Triều Tiên", "🇰🇵"),
    "MN": ("Mông Cổ", "🇲🇳"),
    "TH": ("Thái Lan", "🇹🇭"),
    "ID": ("Indonesia", "🇮🇩"),
    "PH": ("Philippines", "🇵🇭"),
    "MY": ("Malaysia", "🇲🇾"),
    "SG": ("Singapore", "🇸🇬"),
    "KH": ("Campuchia", "🇰🇭"),
    "LA": ("Lào", "🇱🇦"),
    "MM": ("Myanmar", "🇲🇲"),
    "BN": ("Brunei", "🇧🇳"),
    "TL": ("Đông Timor", "🇹🇱"),
    "DE": ("Đức", "🇩🇪"),
    "FR": ("Pháp", "🇫🇷"),
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
    "IE": ("Ireland", "🇮🇪"),
    "IS": ("Iceland", "🇮🇸"),
    "LU": ("Luxembourg", "🇱🇺"),
    "MC": ("Monaco", "🇲🇨"),
    "MT": ("Malta", "🇲🇹"),
    "CY": ("Síp", "🇨🇾"),
    "GR": ("Hy Lạp", "🇬🇷"),
    "CZ": ("Séc", "🇨🇿"),
    "SK": ("Slovakia", "🇸🇰"),
    "HU": ("Hungary", "🇭🇺"),
    "RO": ("Romania", "🇷🇴"),
    "BG": ("Bulgaria", "🇧🇬"),
    "HR": ("Croatia", "🇭🇷"),
    "RS": ("Serbia", "🇷🇸"),
    "SI": ("Slovenia", "🇸🇮"),
    "BA": ("Bosnia & Herzegovina", "🇧🇦"),
    "ME": ("Montenegro", "🇲🇪"),
    "MK": ("Bắc Macedonia", "🇲🇰"),
    "AL": ("Albania", "🇦🇱"),
    "MD": ("Moldova", "🇲🇩"),
    "EE": ("Estonia", "🇪🇪"),
    "LV": ("Latvia", "🇱🇻"),
    "LT": ("Litva", "🇱🇹"),
    "UA": ("Ukraine", "🇺🇦"),
    "BY": ("Belarus", "🇧🇾"),
    "RU": ("Nga", "🇷🇺"),
    "TR": ("Thổ Nhĩ Kỳ", "🇹🇷"),
    "GE": ("Gruzia", "🇬🇪"),
    "AM": ("Armenia", "🇦🇲"),
    "AZ": ("Azerbaijan", "🇦🇿"),
    "KZ": ("Kazakhstan", "🇰🇿"),
    "UZ": ("Uzbekistan", "🇺🇿"),
    "TM": ("Turkmenistan", "🇹🇲"),
    "KG": ("Kyrgyzstan", "🇰🇬"),
    "TJ": ("Tajikistan", "🇹🇯"),
    "AE": ("UAE", "🇦🇪"),
    "SA": ("Ả Rập Xê Út", "🇸🇦"),
    "QA": ("Qatar", "🇶🇦"),
    "KW": ("Kuwait", "🇰🇼"),
    "BH": ("Bahrain", "🇧🇭"),
    "OM": ("Oman", "🇴🇲"),
    "YE": ("Yemen", "🇾🇪"),
    "JO": ("Jordan", "🇯🇴"),
    "LB": ("Lebanon", "🇱🇧"),
    "SY": ("Syria", "🇸🇾"),
    "IQ": ("Iraq", "🇮🇶"),
    "IR": ("Iran", "🇮🇷"),
    "IL": ("Israel", "🇮🇱"),
    "PS": ("Palestine", "🇵🇸"),
    "EG": ("Ai Cập", "🇪🇬"),
    "MA": ("Ma-rốc", "🇲🇦"),
    "DZ": ("Algeria", "🇩🇿"),
    "TN": ("Tunisia", "🇹🇳"),
    "LY": ("Libya", "🇱🇾"),
    "SD": ("Sudan", "🇸🇩"),
    "ET": ("Ethiopia", "🇪🇹"),
    "KE": ("Kenya", "🇰🇪"),
    "TZ": ("Tanzania", "🇹🇿"),
    "UG": ("Uganda", "🇺🇬"),
    "RW": ("Rwanda", "🇷🇼"),
    "NG": ("Nigeria", "🇳🇬"),
    "GH": ("Ghana", "🇬🇭"),
    "CI": ("Bờ Biển Ngà", "🇨🇮"),
    "SN": ("Senegal", "🇸🇳"),
    "CM": ("Cameroon", "🇨🇲"),
    "CD": ("CHDC Congo", "🇨🇩"),
    "CG": ("Congo", "🇨🇬"),
    "AO": ("Angola", "🇦🇴"),
    "MZ": ("Mozambique", "🇲🇿"),
    "ZM": ("Zambia", "🇿🇲"),
    "ZW": ("Zimbabwe", "🇿🇼"),
    "NA": ("Namibia", "🇳🇦"),
    "BW": ("Botswana", "🇧🇼"),
    "MG": ("Madagascar", "🇲🇬"),
    "MU": ("Mauritius", "🇲🇺"),
    "ZA": ("Nam Phi", "🇿🇦"),
    "CA": ("Canada", "🇨🇦"),
    "MX": ("Mexico", "🇲🇽"),
    "BR": ("Brazil", "🇧🇷"),
    "AR": ("Argentina", "🇦🇷"),
    "CL": ("Chile", "🇨🇱"),
    "CO": ("Colombia", "🇨🇴"),
    "PE": ("Peru", "🇵🇪"),
    "VE": ("Venezuela", "🇻🇪"),
    "EC": ("Ecuador", "🇪🇨"),
    "BO": ("Bolivia", "🇧🇴"),
    "PY": ("Paraguay", "🇵🇾"),
    "UY": ("Uruguay", "🇺🇾"),
    "GY": ("Guyana", "🇬🇾"),
    "SR": ("Suriname", "🇸🇷"),
    "PA": ("Panama", "🇵🇦"),
    "CR": ("Costa Rica", "🇨🇷"),
    "NI": ("Nicaragua", "🇳🇮"),
    "HN": ("Honduras", "🇭🇳"),
    "SV": ("El Salvador", "🇸🇻"),
    "GT": ("Guatemala", "🇬🇹"),
    "BZ": ("Belize", "🇧🇿"),
    "CU": ("Cuba", "🇨🇺"),
    "DO": ("Cộng hòa Dominica", "🇩🇴"),
    "HT": ("Haiti", "🇭🇹"),
    "JM": ("Jamaica", "🇯🇲"),
    "PR": ("Puerto Rico", "🇵🇷"),
    "TT": ("Trinidad & Tobago", "🇹🇹"),
    "BS": ("Bahamas", "🇧🇸"),
    "BB": ("Barbados", "🇧🇧"),
    "AU": ("Úc", "🇦🇺"),
    "NZ": ("New Zealand", "🇳🇿"),
    "PG": ("Papua New Guinea", "🇵🇬"),
    "FJ": ("Fiji", "🇫🇯"),
    "WS": ("Samoa", "🇼🇸"),
    "TO": ("Tonga", "🇹🇴"),
    "EU": ("Châu Âu", "🇪🇺"),
}

def normalize_country_code(code: Any) -> str:
    c = str(code or "").strip().upper().replace("_TTP", "").replace("-TTP", "")
    return "UK" if c == "GB" else c


def format_full_country_display(code: str, loc_detail: str = "") -> str:
    raw = (code or "").strip()
    if not raw:
        return "Quốc tế"
    c = normalize_country_code(raw)
    base_c = normalize_country_code(c.split("-")[0].strip()) if "-" in c else c
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
_SCAN_COST_TLS = threading.local()


def _reset_scan_cost_ctx() -> Dict[str, Any]:
    ctx = {"cost_usd": 0.0, "api_calls": 0, "cached_hits": 0}
    _SCAN_COST_TLS.ctx = ctx
    return ctx


def _get_scan_cost_ctx() -> Optional[Dict[str, Any]]:
    return getattr(_SCAN_COST_TLS, "ctx", None)


def _extract_aweme_quality_label(vid_obj: Any) -> str:
    """Trích xuất nhãn độ phân giải thực (1080p/720p/540p/480p...) từ metadata video Mobile & Web."""
    if not isinstance(vid_obj, dict):
        return ""
    best_short = 0

    def _consider_wh(w_val: Any, h_val: Any) -> None:
        nonlocal best_short
        try:
            w = int(w_val or 0)
            h = int(h_val or 0)
            if w > 0 and h > 0:
                short_side = min(w, h)
            elif w > 0 or h > 0:
                short_side = max(w, h)
            else:
                return
            if short_side > best_short:
                best_short = short_side
        except Exception:
            pass

    _consider_wh(vid_obj.get("width"), vid_obj.get("height"))

    p_addr = vid_obj.get("play_addr") or vid_obj.get("playAddr")
    if isinstance(p_addr, dict):
        _consider_wh(p_addr.get("width") or p_addr.get("Width"), p_addr.get("height") or p_addr.get("Height"))

    for br_key in ("bit_rate", "bitrateInfo"):
        br_list = vid_obj.get(br_key)
        if not isinstance(br_list, list):
            continue
        for br in br_list:
            if not isinstance(br, dict):
                continue
            pa = br.get("play_addr") or br.get("PlayAddr")
            if isinstance(pa, dict):
                _consider_wh(pa.get("width") or pa.get("Width"), pa.get("height") or pa.get("Height"))
            gear = str(br.get("gear_name") or br.get("GearName") or "").lower()
            m_g = re.search(r"(?:^|[_-])(2160|1440|1080|720|540|480|360)(?:p|[_-]|$)", gear)
            if m_g and best_short == 0:
                best_short = int(m_g.group(1))

    if best_short >= 2160:
        return "4K"
    if best_short >= 1440:
        return "2K"
    if best_short >= 1000:
        return "1080p"
    if best_short >= 700:
        return "720p"
    if best_short >= 520:
        return "540p"
    if best_short >= 460:
        return "480p"
    if best_short > 0:
        return f"{best_short}p"

    ratio = str(vid_obj.get("definition") or vid_obj.get("ratio") or "").strip().lower()
    m_r = re.search(r"(2160|1440|1080|720|540|480|360)", ratio)
    if m_r:
        r_num = int(m_r.group(1))
        if r_num >= 2160:
            return "4K"
        if r_num >= 1440:
            return "2K"
        return f"{r_num}p"
    return ""


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
        reg = normalize_country_code(aw.get("region"))
        nff = bool(aw.get("is_nff_or_nr"))
        au_obj = aw.get("author") if isinstance(aw.get("author"), dict) else {}
        vid_obj = aw.get("video") if isinstance(aw.get("video"), dict) else {}
        au_uid = str(au_obj.get("uid") or "").strip()
        au_reg = normalize_country_code(au_obj.get("region") or au_obj.get("account_region") or "")
        if au_reg in ("NONE", "AUTO", "EU") or len(au_reg) != 2:
            au_reg = ""
        dur_ms = int(vid_obj.get("duration") or 0)
        d_size = int(((vid_obj.get("play_addr") or {}).get("data_size")) or 0)
        qual_lbl = _extract_aweme_quality_label(vid_obj)
        results[aid] = aw
        if gid:
            with _GROUPID_CACHE_LOCK:
                cache_ref[aid] = {
                    "group_id": gid,
                    "region": reg,
                    "author_region": au_reg,
                    "is_nff_or_nr": nff,
                    "author_uid": au_uid,
                    "dur_ms": dur_ms,
                    "data_size": d_size,
                    "quality": qual_lbl,
                    "ts": now_ts,
                }
                changed = True
    if changed:
        _save_persistent_groupid_cache()


_TIKHUB_CHANNEL_MOBILE_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_TIKHUB_CHANNEL_MOBILE_CACHE_LOCK = threading.Lock()


def _normalize_tikhub_aweme_to_raw_item(aw: Dict[str, Any], default_username: str = "") -> Dict[str, Any]:
    """Chuẩn hóa item aweme từ TikHub Mobile v3 sang định dạng raw_item của bộ phân tích."""
    aid = str(aw.get("aweme_id") or aw.get("id") or "").strip()
    aw_au = aw.get("author") if isinstance(aw.get("author"), dict) else {}
    aw_stat = aw.get("statistics") if isinstance(aw.get("statistics"), dict) else {}
    aw_vid = aw.get("video") if isinstance(aw.get("video"), dict) else {}
    aw_status = aw.get("status") if isinstance(aw.get("status"), dict) else {}
    aw_music = aw.get("music") if isinstance(aw.get("music"), dict) else {}

    cover_urls: List[str] = []
    for cov_key in ("origin_cover", "cover"):
        cov_obj = aw_vid.get(cov_key)
        if isinstance(cov_obj, dict):
            for u in (cov_obj.get("url_list") or []):
                if isinstance(u, str) and u.startswith("http") and u not in cover_urls:
                    cover_urls.append(u)
    cover_url = cover_urls[0] if cover_urls else ""

    play_addr = aw_vid.get("play_addr") if isinstance(aw_vid.get("play_addr"), dict) else {}
    vid_uri = str(aw_vid.get("vid") or play_addr.get("uri") or "").strip()

    au_avatar = ""
    for av_key in ("avatar_larger", "avatar_medium", "avatar_thumb"):
        av_obj = aw_au.get(av_key)
        if isinstance(av_obj, dict):
            for u in (av_obj.get("url_list") or []):
                if isinstance(u, str) and u.startswith("http"):
                    au_avatar = u
                    break
        if au_avatar:
            break

    au_reg = normalize_country_code(aw_au.get("region") or aw_au.get("account_region") or "")
    if au_reg in ("NONE", "AUTO", "EU") or len(au_reg) != 2:
        au_reg = ""

    uname = str(aw_au.get("unique_id") or default_username or "").lstrip("@").strip()
    ct = int(aw.get("create_time") or 0)
    is_top_val = int(aw.get("is_top") or 0)
    priv_status = aw_status.get("private_status")

    return {
        "id": aid,
        "aweme_id": aid,
        "group_id": str(aw.get("group_id") or aw.get("groupId") or "").strip(),
        "desc": aw.get("desc") or "",
        "createTime": ct,
        "create_time": ct,
        "is_top": is_top_val,
        "isPinnedItem": bool(is_top_val > 0),
        "duration": int(aw_vid.get("duration") or 0),
        "quality": _extract_aweme_quality_label(aw_vid),
        "region": normalize_country_code(aw.get("region")),
        "author_region": au_reg,
        "is_nff_or_nr": bool(aw.get("is_nff_or_nr")),
        "video": aw_vid,
        "video_id": vid_uri,
        "music": aw_music,
        "status": aw_status,
        "isProhibited": bool(aw_status.get("is_prohibited")),
        "privateItem": bool((priv_status not in (None, 0)) or aw_status.get("is_private")),
        "cover_url": cover_url,
        "cover_urls": cover_urls,
        "statistics": aw_stat,
        "play_count": int(aw_stat.get("play_count") or 0),
        "digg_count": int(aw_stat.get("digg_count") or 0),
        "comment_count": int(aw_stat.get("comment_count") or 0),
        "share_count": int(aw_stat.get("share_count") or 0),
        "collect_count": int(aw_stat.get("collect_count") or 0),
        "author": {
            "id": str(aw_au.get("uid") or ""),
            "uid": str(aw_au.get("uid") or ""),
            "uniqueId": uname,
            "nickname": str(aw_au.get("nickname") or uname),
            "avatarLarger": au_avatar,
        },
    }


def _fetch_tikhub_multi_awemes(
    video_ids: List[str],
    sec_uid: str = "",
    unique_id: str = "",
    start_cursor_ms: int = 0,
    force_channel_fetch: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """
    Lấy group_id, region & NFF chuẩn Mobile với khóa trần chi phí cứng (~26đ = $0.001 / lượt quét kênh 18 video):
    - Dùng fetch_user_post_videos_v3 (count=20): trả đủ cả 20 video (hỗ trợ cả kênh US Oracle lẫn Global/VN/UK) chỉ trong ĐÚNG 1 request ($0.001 ~ 26đ).
    - Tuyệt đối KHÔNG gọi fetch_multi_video ($0.01) hay bắn hàng loạt fetch_one_video gây tốn tiền oan.
    """
    api_key = _get_tikhub_api_key()
    clean_ids = [str(v).strip() for v in video_ids if str(v).strip().isdigit()]
    clean_uname = str(unique_id or "").lstrip("@").strip()
    is_channel_scan = bool(sec_uid or clean_uname)
    if not api_key or (not clean_ids and not (force_channel_fetch and is_channel_scan)):
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    cost_ctx = _get_scan_cost_ctx()
    cost_lock = threading.Lock()

    def _add_api_cost(usd_val: float) -> None:
        if cost_ctx is not None:
            with cost_lock:
                cost_ctx["cost_usd"] = round(float(cost_ctx.get("cost_usd") or 0.0) + usd_val, 6)
                cost_ctx["api_calls"] = int(cost_ctx.get("api_calls") or 0) + 1

    # 1. Quét kênh (>= 1 video): gọi fetch_user_post_videos_v3 ($0.001 / 20 video ~ 26 VNĐ cho cả kênh 18 video!)
    if is_channel_scan:
        clean_id_set = set(clean_ids)
        cur_val = int(start_cursor_ms or 0)
        # Lượt đầu cố định 1 trang (20 video = $0.001 ~ 26đ); chỉ cho tối đa 2 trang nếu lật trang sâu
        max_pages = 1 if cur_val == 0 else 2
        for _page_idx in range(max_pages):
            if clean_ids and all(vid in results for vid in clean_ids):
                break
            req_params: Dict[str, Any] = {"count": 20, "max_cursor": cur_val, "sort_type": 0}
            if sec_uid:
                req_params["sec_user_id"] = sec_uid
            else:
                req_params["unique_id"] = clean_uname
            try:
                r_post = requests.get(
                    "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_user_post_videos_v3",
                    params=req_params,
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                    timeout=14,
                )
                if r_post.status_code == 200:
                    _add_api_cost(0.001)
                    p_data = (r_post.json() or {}).get("data") or {}
                    aw_list = p_data.get("aweme_list") if isinstance(p_data, dict) else (p_data if isinstance(p_data, list) else [])
                    before_hits = sum(1 for vid in clean_id_set if vid in results)
                    if aw_list:
                        _cache_and_collect_awemes(aw_list, results)
                        if cur_val == 0:
                            c_key = sec_uid or clean_uname.lower()
                            with _TIKHUB_CHANNEL_MOBILE_CACHE_LOCK:
                                if len(_TIKHUB_CHANNEL_MOBILE_CACHE) >= 150:
                                    oldest_k = min(_TIKHUB_CHANNEL_MOBILE_CACHE.items(), key=lambda kv: kv[1][0])[0]
                                    _TIKHUB_CHANNEL_MOBILE_CACHE.pop(oldest_k, None)
                                _TIKHUB_CHANNEL_MOBILE_CACHE[c_key] = (time.time(), list(aw_list))
                    after_hits = sum(1 for vid in clean_id_set if vid in results)
                    has_more_p = bool(p_data.get("has_more")) if isinstance(p_data, dict) else False
                    next_cur_p = int(p_data.get("max_cursor") or 0) if isinstance(p_data, dict) else 0
                    if sec_uid and next_cur_p > 0:
                        _TIKHUB_SECUID_NEXT_CURSOR[sec_uid] = next_cur_p
                    if not aw_list or (after_hits == before_hits) or not has_more_p or next_cur_p <= 0 or next_cur_p == cur_val:
                        break
                    cur_val = next_cur_p
                else:
                    break
            except Exception:
                break
        # Khóa cứng chi phí: Khi quét kênh chỉ dùng đúng cổng v3 ($0.001 ~ 26đ), KHÔNG fallback bắn lẻ 18 video!
        return results

    # 2. Trường hợp khách dán lẻ đúng 1 link / ID video duy nhất (không phải quét kênh): 1 request = $0.001 (~26đ)
    vid_single = clean_ids[0]
    try:
        resp = requests.get(
            "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_one_video_v3",
            params={"aweme_id": vid_single, "region": "US"},
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=8,
        )
        if resp.status_code == 200:
            _add_api_cost(0.001)
            d = (resp.json() or {}).get("data") or {}
            if isinstance(d, dict):
                aw = d.get("aweme_detail") if isinstance(d.get("aweme_detail"), dict) else d
                if aw.get("aweme_id") or aw.get("id"):
                    _cache_and_collect_awemes([aw], results)
    except Exception:
        pass
    return results


def sync_items_tikhub_groupid(
    raw_items: List[Dict[str, Any]],
    tkm_cache: Optional[Dict[str, Any]] = None,
    sec_uid: str = "",
    unique_id: str = "",
    is_load_more: bool = False,
    *,
    merge_missing_from_mobile: bool = False,
    max_merged_items: int = 0,
    expected_total_videos: int = 0,
    web_total_fetched: int = 0,
    web_reached_end: bool = False,
    author_uid: str = "",
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
            c_ctx = _get_scan_cost_ctx()
            if c_ctx is not None:
                c_ctx["cached_hits"] = int(c_ctx.get("cached_hits") or 0) + 1
            c_reg = normalize_country_code(c_entry.get("region"))
            if len(c_reg) == 2 and c_reg not in ("EU", "XO"):
                it["region"] = c_reg
            c_au_reg = normalize_country_code(c_entry.get("author_region") or "")
            if len(c_au_reg) == 2 and c_au_reg not in ("NONE", "AUTO", "EU", "XO"):
                it["author_region"] = c_au_reg
            if c_entry.get("is_nff_or_nr") is True:
                it["is_nff_or_nr"] = True
            if c_entry.get("quality") and not it.get("quality"):
                it["quality"] = str(c_entry["quality"])
            if not it.get("duration") and int(c_entry.get("dur_ms") or 0) > 0:
                it["duration"] = int(c_entry["dur_ms"])
            continue

        existing_gid = str(it.get("group_id") or it.get("groupId") or (tkm_map.get(aid) or {}).get("group_id") or "").strip()
        if existing_gid and existing_gid not in ("0", "None", "null"):
            continue

        if aid not in seen_missing:
            seen_missing.add(aid)
            missing_ids.append(aid)

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

    fetched_awemes: Dict[str, Dict[str, Any]] = {}
    has_tikhub_key = bool(_get_tikhub_api_key())
    cached_mobile_map: Dict[str, Dict[str, Any]] = {}
    cached_min_id = 0
    if not is_load_more and (sec_uid or unique_id):
        c_key = sec_uid or str(unique_id or "").lstrip("@").strip().lower()
        with _TIKHUB_CHANNEL_MOBILE_CACHE_LOCK:
            cached_pair = _TIKHUB_CHANNEL_MOBILE_CACHE.get(c_key)
        if cached_pair and (time.time() - float(cached_pair[0])) < 600:
            for aw in cached_pair[1]:
                if isinstance(aw, dict):
                    aw_id = str(aw.get("aweme_id") or aw.get("id") or "").strip()
                    if aw_id:
                        cached_mobile_map[aw_id] = aw
            nums = [int(k) for k in cached_mobile_map if k.isdigit()]
            if nums:
                cached_min_id = min(nums)

    need_live_for_missing = bool(missing_ids) and (
        not cached_mobile_map
        or any(
            m_id not in cached_mobile_map
            and (not m_id.isdigit() or cached_min_id == 0 or int(m_id) >= cached_min_id)
            for m_id in missing_ids
        )
    )
    if need_live_for_missing and has_tikhub_key:
        fetched_awemes = _fetch_tikhub_multi_awemes(missing_ids, sec_uid=sec_uid, unique_id=unique_id, start_cursor_ms=start_cur_ms)
    elif cached_mobile_map:
        fetched_awemes = cached_mobile_map
    elif merge_missing_from_mobile and not is_load_more and (sec_uid or unique_id) and has_tikhub_key:
        web_incomplete = bool(web_reached_end and expected_total_videos > 0 and web_total_fetched < expected_total_videos)
        cached_missing_for_uid = False
        clean_au_uid = str(author_uid or "").strip()
        if clean_au_uid and clean_au_uid != "---":
            existing_id_set = {
                str(x.get("id") or x.get("aweme_id") or x.get("item_id") or "").strip()
                for x in raw_items
                if isinstance(x, dict)
            }
            min_existing_id = min((int(x) for x in existing_id_set if x.isdigit()), default=0)
            if min_existing_id > 0:
                for k_cache, v_cache in p_cache.items():
                    if (
                        isinstance(v_cache, dict)
                        and str(v_cache.get("author_uid") or "") == clean_au_uid
                        and k_cache not in existing_id_set
                        and k_cache.isdigit()
                        and int(k_cache) >= min_existing_id
                    ):
                        cached_missing_for_uid = True
                        break
        if web_incomplete or cached_missing_for_uid:
            fetched_awemes = _fetch_tikhub_multi_awemes(
                [],
                sec_uid=sec_uid,
                unique_id=unique_id,
                start_cursor_ms=0,
                force_channel_fetch=True,
            )

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
        reg = normalize_country_code(aw.get("region"))
        if len(reg) == 2:
            it["region"] = reg
        aw_au = aw.get("author") if isinstance(aw.get("author"), dict) else {}
        au_reg = normalize_country_code(aw_au.get("region") or aw_au.get("account_region") or "")
        if len(au_reg) == 2 and au_reg not in ("NONE", "AUTO", "EU", "XO"):
            it["author_region"] = au_reg
        if aw.get("is_nff_or_nr") is True:
            it["is_nff_or_nr"] = True
        aw_vid = aw.get("video") if isinstance(aw.get("video"), dict) else {}
        q_lbl = _extract_aweme_quality_label(aw_vid)
        if q_lbl:
            it["quality"] = q_lbl
        if not it.get("duration") and int(aw_vid.get("duration") or 0) > 0:
            it["duration"] = int(aw_vid["duration"])

    if merge_missing_from_mobile and fetched_awemes:
        existing_ids = {
            str(x.get("id") or x.get("aweme_id") or x.get("item_id") or "").strip()
            for x in raw_items
            if isinstance(x, dict)
        }
        newest_existing_ct = max(
            (int(x.get("createTime") or x.get("create_time") or x.get("post_time") or 0) for x in raw_items if isinstance(x, dict)),
            default=0,
        )
        added_count = 0
        for aid, aw in fetched_awemes.items():
            if not aid or aid in existing_ids or not isinstance(aw, dict):
                continue
            aw_ct = int(aw.get("create_time") or 0)
            if is_load_more and newest_existing_ct > 0 and aw_ct > newest_existing_ct:
                continue
            raw_items.append(_normalize_tikhub_aweme_to_raw_item(aw, default_username=unique_id))
            existing_ids.add(aid)
            added_count += 1
        if added_count > 0:
            def _sort_key_raw(x: Dict[str, Any]) -> Tuple[int, int, int]:
                pinned = 0 if (x.get("isPinnedItem") or int(x.get("is_top") or 0) > 0) else 1
                ct_v = int(x.get("createTime") or x.get("create_time") or x.get("post_time") or 0)
                id_s = str(x.get("id") or x.get("aweme_id") or x.get("item_id") or "").strip()
                id_n = int(id_s) if id_s.isdigit() else 0
                return (pinned, -ct_v, -id_n)

            raw_items.sort(key=_sort_key_raw)
            if max_merged_items > 0 and len(raw_items) > max_merged_items:
                del raw_items[max_merged_items:]

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
                    if result_meta is not None:
                        result_meta["reached_end"] = True
                    break
                for it in items:
                    iid = str(it.get("id") or "")
                    if iid and not any(str(x.get("id")) == iid for x in all_items):
                        all_items.append(it)
                if result_meta is not None and (not data.get("hasMore") or len(items) < 15):
                    result_meta["reached_end"] = True
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

def _filter_pool_tuples(raw_pool: List[Tuple[str, str, str, str]], exclude_username: str = "") -> List[Tuple[str, str, str]]:
    ex_u = str(exclude_username or "").lstrip("@").strip().lower()
    out = [
        (ck, ua, px)
        for (ck, ua, px, own) in raw_pool
        if not (ex_u and own and own == ex_u)
    ]
    random.shuffle(out)
    return out


def get_borrowed_cookies_pool(exclude_username: str = "") -> List[Tuple[str, str, str]]:
    """
    Thu thập danh sách các cookie hợp lệ từ Studio Web, ConfigStore và cookie_pool.json (đã xáo trộn)
    để xoay vòng khi quét kênh ngoài, tránh chết 1 cookie hoặc bị TikTok giới hạn tốc độ.
    Có cache RAM 120s để không phải giải mã DPAPI / đọc đĩa lại mỗi lần quét.
    Loại trừ cookie thuộc chính exclude_username nếu được chỉ định.
    """
    now_t = time.time()
    cached_list = _BORROWED_POOL_CACHE.get("pool") or []
    if cached_list and (now_t - float(_BORROWED_POOL_CACHE.get("ts") or 0)) < 120:
        return _filter_pool_tuples(cached_list, exclude_username=exclude_username)

    pool_out: List[Tuple[str, str, str, str]] = []
    seen_sids = set()

    def _add_cand(ck_raw: str, ua_val: str = "", px_val: str = "", owner_uname: str = ""):
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
        clean_own = str(owner_uname or "").lstrip("@").strip().lower()
        pool_out.append((ck_str, ua_val or DEFAULT_UA, px_val or "", clean_own))

    # 1. Các nick đã đăng nhập trên Web (ACTIVE_STUDIO_ACCOUNTS)
    try:
        import studio_engine
        for k_s, s_entry in list((studio_engine.ACTIVE_STUDIO_ACCOUNTS or {}).items()):
            if isinstance(s_entry, dict) and s_entry.get("cookie"):
                acc_obj = s_entry.get("account") or {}
                own_s = acc_obj.get("uniqueId") or acc_obj.get("username") or (k_s if not str(k_s).isdigit() else "")
                _add_cand(s_entry["cookie"], DEFAULT_UA, "", owner_uname=str(own_s))
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
                    own_a = getattr(a, "username", "") or getattr(a, "name", "") or ""
                    _add_cand(c, getattr(a, "user_agent", "") or DEFAULT_UA, "", owner_uname=str(own_a))
    except Exception as e:
        print(f"[BorrowCookie] Lỗi mượn cookie từ tool: {e}")

    # 3. Kho cookie_pool.json
    try:
        pool_file = Path(__file__).resolve().parent / "cookie_pool.json"
        if pool_file.exists():
            data = json.loads(pool_file.read_text(encoding="utf-8"))
            if data.get("cookie"):
                _add_cand(data["cookie"], data.get("user_agent", DEFAULT_UA), "", owner_uname="")
            for acc_k, a_info in (data.get("accounts") or {}).items():
                if isinstance(a_info, dict) and a_info.get("cookie"):
                    own_p = a_info.get("username") or acc_k or ""
                    _add_cand(a_info["cookie"], a_info.get("user_agent", DEFAULT_UA), "", owner_uname=str(own_p))
    except Exception as e:
        print(f"[BorrowCookie] Lỗi đọc cookie_pool.json: {e}")

    env_c = os.getenv("TIKTOK_COOKIE", "")
    if env_c:
        _add_cand(env_c, DEFAULT_UA, "", owner_uname="")

    if pool_out:
        _BORROWED_POOL_CACHE["ts"] = now_t
        _BORROWED_POOL_CACHE["pool"] = list(pool_out)
    return _filter_pool_tuples(pool_out, exclude_username=exclude_username)

def get_borrowed_cookie(exclude_username: str = "") -> Tuple[str, str, str]:
    """Chọn ngẫu nhiên 1 cookie hợp lệ từ kho xoay vòng."""
    cands = get_borrowed_cookies_pool(exclude_username=exclude_username)
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
    allow_tool_self_cookies: bool = False,
) -> Dict[str, Any]:
    """
    Hàm kiểm tra trùng lặp & quét thông tin kênh TikTok.
    - Tự động nhận diện tài khoản trong ConfigStore (có cookie Studio) để quét án phạt & FYP chính xác 100% như tool.
    - Hỗ trợ lấy dữ liệu Check user (Tên, Handle, SĐT, Quốc gia, UID, Ngày tạo).
    - Hỗ trợ phân trang: mặc định quét 24 video, có thể quét thêm các đợt tiếp theo.
    """
    scan_cost_ctx = _reset_scan_cost_ctx()
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
    matched_acc_proxy = str(proxy_str or "").strip()
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

    if allow_tool_self_cookies and username and not matched_acc_cookie and not force_external:
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

    # Cơ chế mượn cookie từ kho tool: Chỉ dùng để lấy passport nếu có (tuyệt đối không đụng vào cookie của chính user đang quét nếu không phải tuandzvcc)
    if not is_self_account and not effective_cookie:
        b_c, b_ua, b_px = get_borrowed_cookie(exclude_username="" if allow_tool_self_cookies else username)
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
                                    detected_account_country = "UK"
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
        channel_info["region"] = normalize_country_code(detected_account_country)
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
    # Trường hợp A: Tài khoản có trong hệ thống Tool hoặc gắn Cookie Studio (quét 100% án phạt, Quỹ BKT & FYP cả trên PC lẫn Cloud)
    did_self_penalty_sync = False

    def _run_self_penalty_and_crp_sync(target_items: list):
        nonlocal did_self_penalty_sync
        if not target_items or not allow_paid_groupid or not effective_cookie:
            return
        did_self_penalty_sync = True
        for _it in target_items:
            if isinstance(_it, dict) and not _it.get("aweme_id"):
                _cand = str(_it.get("item_id") or _it.get("id") or "").strip()
                if _cand:
                    _it["aweme_id"] = _cand

        def _run_fyp_sync():
            if sync_items_penalty_data:
                try:
                    sync_items_penalty_data(target_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                except Exception as pen_err:
                    print(f"[CheckDupWeb] Lỗi sync_items_penalty_data: {pen_err}")

        def _run_crp_sync():
            try:
                sync_items_crp_punishment_data(target_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
            except Exception as crp_err:
                print(f"[CheckDupWeb] Lỗi sync_items_crp_punishment_data: {crp_err}")

        with ThreadPoolExecutor(max_workers=2) as sync_pool:
            f1 = sync_pool.submit(_run_fyp_sync)
            f2 = sync_pool.submit(_run_crp_sync)
            f1.result()
            f2.result()
        for _p in target_items:
            if _p.get("is_crp_punished"):
                _p["is_restored"] = False
                _p["isRestored"] = False
                _app_st = int(_p.get("appeal_status") or 0)
                if _app_st in (2, 3, 4):
                    _msg = "Bị đánh gậy Không nguyên tác · Loại khỏi Quỹ BKT (Kháng xịt / Đã kháng)"
                    _p["crp_penalty_details"] = _msg
                    if not _p.get("has_penalty"):
                        _p["penalty_details"] = _msg

    if is_self_account and effective_cookie:
        try:
            posts_res = (
                api_get_post_list(
                    effective_cookie, matched_acc_ua, matched_acc_proxy,
                    sec_uid=sec_uid, max_cursor=max_cursor, count=50
                ) or {}
            ) if api_get_post_list else {}
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

                if raw_items and allow_paid_groupid:
                    _run_self_penalty_and_crp_sync(raw_items)
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
        for p_c, p_ua, _ in get_borrowed_cookies_pool(exclude_username="" if allow_tool_self_cookies else username):
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
    # Nếu là nick chính chủ nhưng Branch A rỗng (rơi xuống Creator API / TikHub) thì vẫn phải đồng bộ gậy BKT & FYP
    if is_self_account and effective_cookie and raw_items and allow_paid_groupid and not did_self_penalty_sync:
        _run_self_penalty_and_crp_sync(raw_items)

    # Chỉ tài khoản đã đăng nhập (VIP / Admin) mới kích hoạt quét trùng lặp Group ID & API trả phí
    video_list = []
    if allow_paid_groupid:
        tkm_cache = _load_tiktokmanager_cache()
        sync_items_tikhub_groupid(
            raw_items,
            tkm_cache,
            sec_uid=sec_uid,
            unique_id=str(channel_info.get("uniqueId") or username or ""),
            is_load_more=(max_cursor > 0),
            merge_missing_from_mobile=(not is_self_account and not target_vid),
            max_merged_items=max_videos,
            expected_total_videos=int(channel_info.get("videoCount") or 0),
            web_total_fetched=len(all_fetched_api_items or raw_items),
            web_reached_end=bool(creator_meta.get("reached_end")),
            author_uid=str(check_user_info.get("uid") or "").strip(),
        )
    else:
        tkm_cache = {}

    # Bổ sung miễn phí (0đ) độ phân giải thực (720p/1080p/540p) cho video Studio hoặc video cache cũ chưa có quality
    p_gid_cache_ref = _load_persistent_groupid_cache()
    need_qual_items = [
        it for it in (raw_items or [])
        if isinstance(it, dict)
        and not str(it.get("quality") or "").strip()
        and not _extract_aweme_quality_label(it.get("video"))
    ]
    if need_qual_items and sec_uid:
        qual_src_map: Dict[str, Dict[str, Any]] = {}
        for x_src in (all_fetched_api_items or []):
            if isinstance(x_src, dict):
                x_aid = str(x_src.get("id") or x_src.get("aweme_id") or "").strip()
                if x_aid:
                    qual_src_map[x_aid] = x_src
        if not all(str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "") in qual_src_map for it in need_qual_items):
            q_cookies: List[Tuple[str, str, str]] = []
            if effective_cookie:
                q_cookies.append((effective_cookie, matched_acc_ua, matched_acc_proxy))
            for p_c, p_ua, _ in get_borrowed_cookies_pool(exclude_username="" if allow_tool_self_cookies else username):
                if p_c and not any(c[0] == p_c for c in q_cookies):
                    q_cookies.append((p_c, p_ua or matched_acc_ua, ""))
                if len(q_cookies) >= 2:
                    break
            q_limit = max(max_videos + max_cursor, 20)
            for q_ck, q_ua, q_px in q_cookies:
                try:
                    ext_q = fetch_external_channel_videos_creator_api(
                        sec_uid=sec_uid, cookie_str=q_ck, ua_str=q_ua, proxy_str=q_px,
                        max_videos=q_limit, max_cursor=max_cursor
                    )
                    if ext_q:
                        for x_src in ext_q:
                            if isinstance(x_src, dict):
                                x_aid = str(x_src.get("id") or x_src.get("aweme_id") or "").strip()
                                if x_aid:
                                    qual_src_map[x_aid] = x_src
                        break
                except Exception:
                    pass
        if qual_src_map:
            qual_cache_dirty = False
            for it in need_qual_items:
                aid = str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "").strip()
                src_it = qual_src_map.get(aid)
                if not isinstance(src_it, dict):
                    continue
                src_vid = src_it.get("video") if isinstance(src_it.get("video"), dict) else {}
                q_lbl = _extract_aweme_quality_label(src_vid)
                if q_lbl:
                    it["quality"] = q_lbl
                    if not isinstance(it.get("video"), dict) or not it.get("video"):
                        it["video"] = src_vid
                    if isinstance(p_gid_cache_ref.get(aid), dict):
                        with _GROUPID_CACHE_LOCK:
                            p_gid_cache_ref[aid]["quality"] = q_lbl
                        qual_cache_dirty = True
            if qual_cache_dirty:
                _save_persistent_groupid_cache()

    ch_followers = int(channel_info.get("followers") or 0)

    # Bản đồ toàn bộ video thuộc chính kênh đang quét (để loại trừ báo giả khi kênh cắt nhiều phần Part 1, Part 2... từ cùng 1 tập/bối cảnh)
    def _extract_item_dur_sec(x_item: Dict[str, Any]) -> float:
        v_sub = x_item.get("video") if isinstance(x_item.get("video"), dict) else {}
        raw_d = float(x_item.get("duration") or v_sub.get("duration") or 0)
        return (raw_d / 1000.0) if raw_d > 3600 else raw_d

    own_channel_map: Dict[str, Dict[str, Any]] = {}
    for x_it in (all_fetched_api_items or []) + (all_studio_items or []) + (raw_items or []):
        if isinstance(x_it, dict):
            x_id = str(x_it.get("id") or x_it.get("aweme_id") or x_it.get("item_id") or "").strip()
            if x_id:
                v_sub = x_it.get("video") if isinstance(x_it.get("video"), dict) else {}
                own_channel_map[x_id] = {
                    "dur_s": _extract_item_dur_sec(x_it),
                    "size": int(v_sub.get("size") or ((v_sub.get("playAddr") or {}).get("DataSize") if isinstance(v_sub.get("playAddr"), dict) else 0) or 0),
                    "ct": int(x_it.get("createTime") or x_it.get("create_time") or x_it.get("post_time") or 0),
                }
    cur_ch_uid = str(check_user_info.get("uid") or "").strip()

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
        shoot_tab = it.get("shoot_tab")
        vid_sub_obj = it.get("video") if isinstance(it.get("video"), dict) else {}
        vid_hash_id = str(vid_sub_obj.get("videoID") or vid_sub_obj.get("vid") or it.get("video_id") or "").strip().lower()
        play_addr_str = str(vid_sub_obj.get("playAddr") or "")

        # Trong kiến trúc lưu trữ VOD của ByteDance/TikTok:
        # Mã không gian "025" ở ký tự [3:6] (vd: v14025..., v12025..., v1c025..., v24025...) hoặc btag=e000b0000 là upload từ Web / Studio PC (aid=1988)
        # Mã không gian "044" ở ký tự [3:6] (vd: v12044..., v15044...) là upload từ App Mobile (aid=1233/1180)
        is_vod_web_space = bool(re.match(r"^v[0-9a-z]{2}025", vid_hash_id)) or ("btag=e000b0000" in play_addr_str)

        is_explicit_web = (
            device_plat in ("web_pc", "pc", "web")
            or source_raw in ("web", "web_pc")
            or is_vod_web_space
            or "web_upload" in str(it).lower()
        )

        # Phân loại đúng thiết bị tải lên (không để thẻ anchor quảng cáo CapCut của TikTok ghi đè nguồn Phone/Web)
        if is_explicit_web:
            source_lbl = "Web (PC)"
            source_icon = "fa-desktop"
            device = "web"
        elif shoot_tab and str(shoot_tab) not in ("0", "None"):
            source_lbl = "Mobile (Camera)"
            source_icon = "fa-camera"
            device = "phone"
        else:
            source_lbl = "Phone (Gallery)"
            source_icon = "fa-mobile-screen"
            device = "phone"


        music_obj = it.get("music") if isinstance(it.get("music"), dict) else {}


        is_muted = bool(it.get("is_muted") or music_obj.get("mute_share") or music_obj.get("mute") or music_obj.get("status") == 2)


        video_obj = it.get("video") if isinstance(it.get("video"), dict) else {}
        quality_str = str(it.get("quality") or "").strip() or _extract_aweme_quality_label(video_obj) or "Chuẩn"

        ch_reg = normalize_country_code(channel_info.get("region") or "US")
        cached_free_reg = ((p_gid_cache_ref.get(aweme_id) or {}).get("region") if isinstance(p_gid_cache_ref, dict) else "")
        region = normalize_country_code(it.get("region") or it.get("locationCreated") or it.get("country_code") or it.get("geo") or cached_free_reg or cached_meta.get("region") or ch_reg)

        group_id = str(it.get("group_id") or it.get("groupId") or cached_meta.get("group_id") or "") if allow_paid_groupid else ""
        is_same_channel_dup = False

        # Lọc bỏ báo trùng ảo (Same-Channel Series Cluster):
        # Khi cùng 1 kênh cắt nhiều phần khác nhau (Part 1, Part 2...) của cùng 1 show/bối cảnh & đăng cùng tiêu đề,
        # TikTok gom chung vào GroupdIdList trỏ về phần trước trên CHÍNH KÊNH ĐÓ dù 2 video dài ngắn / nội dung khác hẳn nhau.
        if group_id and group_id != aweme_id and group_id not in ("0", "None", "null"):
            this_dur_s = _extract_item_dur_sec(it)
            c_this = p_gid_cache_ref.get(aweme_id) if isinstance(p_gid_cache_ref, dict) else None
            c_orig = p_gid_cache_ref.get(group_id) if isinstance(p_gid_cache_ref, dict) else None
            is_same_channel_origin = (group_id in own_channel_map) or bool(
                cur_ch_uid and cur_ch_uid != "---" and isinstance(c_orig, dict) and c_orig.get("author_uid") == cur_ch_uid
            )
            if is_same_channel_origin:
                orig_info = own_channel_map.get(group_id) or {}
                orig_dur_s = float(orig_info.get("dur_s") or 0)
                if orig_dur_s <= 0 and isinstance(c_orig, dict) and int(c_orig.get("dur_ms") or 0) > 0:
                    orig_dur_s = float(c_orig["dur_ms"]) / 1000.0
                this_ms = int(c_this.get("dur_ms") or 0) if isinstance(c_this, dict) else 0
                orig_ms = int(c_orig.get("dur_ms") or 0) if isinstance(c_orig, dict) else 0
                this_sz = int((c_this or {}).get("data_size") or (own_channel_map.get(aweme_id) or {}).get("size") or 0)
                orig_sz = int((c_orig or {}).get("data_size") or orig_info.get("size") or 0)
                is_exact_self_repost = False
                if this_ms > 0 and orig_ms > 0:
                    is_exact_self_repost = abs(this_ms - orig_ms) <= 400 and (this_sz == 0 or orig_sz == 0 or abs(this_sz - orig_sz) / max(this_sz, orig_sz) < 0.03)
                elif this_dur_s > 0 and orig_dur_s > 0:
                    is_exact_self_repost = abs(this_dur_s - orig_dur_s) < 1.0 and (this_sz == 0 or orig_sz == 0 or abs(this_sz - orig_sz) / max(this_sz, orig_sz) < 0.03)
                if is_exact_self_repost:
                    is_same_channel_dup = True
                else:
                    # Là 2 phần video khác nhau trên chính kênh này (không phải đăng đúp cùng 1 video, không phải reup kênh khác)
                    group_id = aweme_id

        # Mã trạng thái TikTok:
        # 102 = Công khai | 140 = Chủ kênh để Riêng tư | 143 = Chủ kênh để Chỉ Bạn bè (KHÔNG phải vi phạm!)
        # 141 = Khóa Bản quyền | 142, 144 = Bị TikTok gỡ bỏ do vi phạm Tiêu chuẩn Cộng đồng
        st_val = it.get("status")
        st_code = 0
        if isinstance(st_val, (int, str)) and str(st_val).isdigit():
            st_code = int(st_val)
        elif isinstance(st_val, dict):
            st_code = int(st_val.get("video_status") or 0)

        # Dọn sạch cờ báo nhầm từ module cũ nếu video chỉ đang ở chế độ Riêng tư (140) hoặc Chỉ Bạn bè (143)
        if st_code in (140, 143):
            if "mã 140" in str(it.get("penalty_details") or "").lower() or "mã 143" in str(it.get("penalty_details") or "").lower():
                it["penalty_details"] = ""
                it["is_takedown"] = False
                it["is_fyp_restricted"] = False
                it["shadow"] = False
                it["has_penalty"] = False

        # Nhận diện khiếu nại bản quyền & vi phạm tiêu chuẩn thật sự
        is_copyright = bool(
            st_code == 141
            or it.get("is_copyright") is True
            or (isinstance(it.get("desc"), str) and "bản quyền" in it["desc"].lower())
            or (isinstance(it.get("penalty_details"), str) and "bản quyền" in it["penalty_details"].lower())
        )
        is_takedown = bool(st_code in (141, 142, 144) or (it.get("is_takedown") is True and st_code not in (140, 143)))

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
        elif pc_vid_status in (142, 144):
            is_takedown = True
            if not st_code:
                st_code = pc_vid_status

        is_restored = bool(it.get("is_restored", False))
        # Tín hiệu cấm đề xuất FYP từ hệ thống kiểm duyệt AI của TikTok (bỏ qua 140 Riêng tư & 143 Bạn bè)
        is_tns_fyp_ban = (not is_restored) and bool(
            (ec_verdict not in (0, 1))
            or (tns_status not in (0, 102, 140, 143))
            or (arb_status not in (0, 102, 140, 143))
            or (pc_status not in (0, 102, 140, 143))
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
            dup_reason = (
                "Trùng chính kênh (Đăng đúp cùng 1 video trên chính kênh này)"
                if is_same_channel_dup
                else "Trùng lặp nội dung với video gốc trên TikTok"
            )
            dup_origin_vid = group_id
        elif cached_meta.get("is_dup"):
            dup_reason = "Trùng lặp nội dung (Khớp cache TikTok Manager)"
            c_gid = str(cached_meta.get("group_id") or "").strip()
            if c_gid and c_gid != aweme_id and c_gid not in ("0", "None", "null"):
                dup_origin_vid = c_gid
                c_cache_orig = p_gid_cache_ref.get(dup_origin_vid) if isinstance(p_gid_cache_ref, dict) else None
                if (dup_origin_vid in own_channel_map) or (
                    cur_ch_uid and cur_ch_uid != "---" and isinstance(c_cache_orig, dict) and c_cache_orig.get("author_uid") == cur_ch_uid
                ):
                    is_same_channel_dup = True
                    dup_reason = "Trùng chính kênh (Đăng đúp cùng 1 video trên chính kênh này)"
        elif (display_penalty_type != 0 or display_policy != 0) and not is_restored:
            dup_reason = "Vi phạm chính sách trùng lặp / Bản quyền nội dung"

        age_hours = (time.time() - c_time) / 3600 if c_time > 0 else 0

        # Phân biệt rõ:
        # 1. Trùng lặp / Không nguyên tác (is_dup / is_crp_punished): trùng Group ID hoặc bị đánh gậy Không nguyên tác BKT
        # 2. Án phạt FYP (has_fyp_pen): vi phạm tiêu chuẩn cộng đồng, TNS penalty, tắt tiếng bản quyền
        # 3. Mất FYP (is_fyp): CHỈ bật khi TikTok thực sự cắm cờ hạn chế FYP (NFF/TNS/Bản quyền/Án phạt), KHÔNG tự bịa Mất FYP nếu chỉ mới Trùng!
        is_non_crp_dup = bool(is_dup and not is_crp_punished)
        is_fyp = bool(
            is_copyright
            or is_takedown
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
            shadow_reason = "Vi phạm bản quyền âm thanh / hình ảnh (TikTok tự khóa Riêng tư & ngừng đề xuất Xu hướng)"
            is_fyp = True
            is_shadow = True
        elif is_takedown:
            shadow_reason = "Video bị TikTok gỡ bỏ do vi phạm Tiêu chuẩn Cộng đồng"
            is_fyp = True
            is_shadow = True
        elif is_prohibited:
            shadow_reason = "Vi phạm Tiêu chuẩn Cộng đồng TikTok (Bị cấm phân phối)"
            is_fyp = True
            is_shadow = True
        elif is_tns_fyp_ban:
            if (ec_verdict not in (0, 1)) or it.get("is_nff_or_nr") or it.get("_oembed_restricted"):
                shadow_reason = shadow_reason or "Mất đề xuất Xu hướng (FYP): Nội dung bị AI đánh dấu nhạy cảm / hành vi nguy hiểm / chất lượng thấp / dính logo-QR"
            else:
                shadow_reason = shadow_reason or "Mất đề xuất Xu hướng (FYP): Bị bộ lọc an toàn nội dung của TikTok chặn phân phối"
            is_fyp = True
            is_shadow = True
        elif is_muted:
            shadow_reason = shadow_reason or "Vi phạm bản quyền âm thanh (Video bị tắt tiếng · Mất đề xuất Xu hướng)"
            is_fyp = True
            is_shadow = True
        elif can_repost is False or can_share is False:
            shadow_reason = shadow_reason or "Mất đề xuất Xu hướng (FYP): Video bị TikTok khóa chia sẻ & đăng lại"
            is_fyp = True
            is_shadow = True
        elif has_fyp_pen:
            shadow_reason = shadow_reason or "Mất đề xuất Xu hướng (FYP): Đang dính án phạt kiểm duyệt nội dung của TikTok"
            is_fyp = True
            is_shadow = True

        raw_pen_str = str(it.get("penalty_details") or "")
        if "kháng về thành công" in raw_pen_str.lower() and (has_pen or is_dup):
            raw_pen_str = ""
        pen_details_str = dup_reason if is_crp_punished and not has_fyp_pen else (raw_pen_str or dup_reason or shadow_reason or "")
        if is_copyright and not raw_pen_str:
            pen_details_str = "Vi phạm bản quyền âm thanh / hình ảnh (Bị khóa Riêng tư)"
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
            if is_same_channel_dup and clean_u:
                origin_url = f"https://www.tiktok.com/@{clean_u}/video/{target_orig_id}"
            else:
                origin_url = f"https://www.tiktok.com/@tiktok/video/{target_orig_id}"

        post_data = {
            "id": aweme_id,
            "awemeId": aweme_id,
            "aweme_id": aweme_id,
            "group_id": group_id,
            "groupId": group_id,
            "origin_url": origin_url,
            "has_origin_link": bool(origin_url),
            "is_same_channel_dup": bool(is_same_channel_dup and bool(target_orig_id)),
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
        final_acc_reg = ""

        # Ưu tiên 1: Lấy Quốc gia gốc của chính tài khoản (author.region từ TikTok Mobile API)
        au_regs = []
        for it in (raw_items or []):
            if not isinstance(it, dict):
                continue
            it_au = it.get("author") if isinstance(it.get("author"), dict) else {}
            r_cand = normalize_country_code(it.get("author_region") or it_au.get("region") or it_au.get("account_region") or "")
            if len(r_cand) == 2 and r_cand not in ("NONE", "AUTO", "EU", "XO"):
                au_regs.append(r_cand)
        if au_regs:
            final_acc_reg = Counter(au_regs).most_common(1)[0][0]

        # Ưu tiên 2: Nếu cache cũ chưa lưu author_region, kiểm tra cụm lưu trữ Project Texas độc quyền Mỹ (tos-useast8)
        if not final_acc_reg:
            has_useast8 = False
            for v_it in video_list:
                if "tos-useast8" in str(v_it.get("cover_url") or "").lower():
                    has_useast8 = True
                    break
            if not has_useast8:
                for it in (raw_items or []):
                    if isinstance(it, dict):
                        v_sub = it.get("video") if isinstance(it.get("video"), dict) else {}
                        if "tos-useast8" in f"{v_sub.get('cover', '')} {v_sub.get('playAddr', '')} {it.get('cover_url', '')}".lower():
                            has_useast8 = True
                            break
            if has_useast8:
                final_acc_reg = "US"
                cache_dirty_reg = False
                for v_it in video_list:
                    aid_k = str(v_it.get("aweme_id") or "").strip()
                    if aid_k and isinstance(p_gid_cache_ref.get(aid_k), dict) and not p_gid_cache_ref[aid_k].get("author_region"):
                        with _GROUPID_CACHE_LOCK:
                            p_gid_cache_ref[aid_k]["author_region"] = "US"
                        cache_dirty_reg = True
                if cache_dirty_reg:
                    _save_persistent_groupid_cache()

        # Ưu tiên 3: Dự phòng theo đa số Quốc gia IP của các video
        if not final_acc_reg:
            reg_counter = Counter(v.get("region") for v in video_list if v.get("region") and v.get("region") not in ("NONE", "AUTO", "EU"))
            if reg_counter:
                final_acc_reg, _ = reg_counter.most_common(1)[0]

        if final_acc_reg and final_acc_reg != channel_info.get("region"):
            channel_info["region"] = final_acc_reg
        reg_disp = format_full_country_display(channel_info.get("region") or "US")
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
        "video_notice": video_notice,
        "_internal_cost": {
            "cost_usd": round(float(scan_cost_ctx.get("cost_usd") or 0.0), 4),
            "api_calls": int(scan_cost_ctx.get("api_calls") or 0),
            "cached_hits": int(scan_cost_ctx.get("cached_hits") or 0),
        },
    }


# ==============================================================================
# BULK ACCOUNT STATUS & RECOMMENDATION CHECKER (TikHub handler_user_profile)
# ==============================================================================

ACCOUNT_COUNTRY_CACHE_FILE = Path(__file__).resolve().parent / "account_country_cache.json"
_ACCOUNT_COUNTRY_CACHE_LOCK = threading.Lock()
_ACCOUNT_COUNTRY_CACHE: Dict[str, Dict[str, str]] = {"by_handle": {}, "by_uid": {}}
_ACCOUNT_COUNTRY_CACHE_LOADED = False


def _load_account_country_cache_unlocked() -> Dict[str, Dict[str, str]]:
    """Tải bộ nhớ đệm quốc gia tài khoản từ account_country_cache.json."""
    global _ACCOUNT_COUNTRY_CACHE_LOADED
    if not _ACCOUNT_COUNTRY_CACHE_LOADED:
        _ACCOUNT_COUNTRY_CACHE_LOADED = True
        try:
            if ACCOUNT_COUNTRY_CACHE_FILE.exists():
                raw = json.loads(ACCOUNT_COUNTRY_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    bh = raw.get("by_handle")
                    bu = raw.get("by_uid")
                    if isinstance(bh, dict):
                        _ACCOUNT_COUNTRY_CACHE["by_handle"].update({str(k).lower(): str(v) for k, v in bh.items() if k and v})
                    if isinstance(bu, dict):
                        _ACCOUNT_COUNTRY_CACHE["by_uid"].update({str(k): str(v) for k, v in bu.items() if k and v})
        except Exception:
            pass
    return _ACCOUNT_COUNTRY_CACHE


def _save_account_country_cache_unlocked() -> None:
    """Lưu nguyên tử account_country_cache.json xuống đĩa."""
    try:
        tmp_f = ACCOUNT_COUNTRY_CACHE_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(_ACCOUNT_COUNTRY_CACHE, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_f, ACCOUNT_COUNTRY_CACHE_FILE)
    except Exception:
        pass


def _is_valid_country_code(code: Any) -> bool:
    c = normalize_country_code(code)
    return len(c) == 2 and c.isalpha() and c not in ("UN", "XX", "ZZ", "EU")


def _get_cached_account_country(username: str = "", uid: str = "") -> str:
    """Lấy quốc gia tài khoản từ cache đĩa hoặc danh sách nick trong Studio (0đ API)."""
    u_key = str(username or "").lstrip("@").strip().lower()
    uid_key = str(uid or "").strip()
    with _ACCOUNT_COUNTRY_CACHE_LOCK:
        cache = _load_account_country_cache_unlocked()
        if uid_key and uid_key in cache["by_uid"]:
            cand = normalize_country_code(cache["by_uid"][uid_key])
            if _is_valid_country_code(cand):
                return cand
        if u_key and u_key in cache["by_handle"]:
            cand = normalize_country_code(cache["by_handle"][u_key])
            if _is_valid_country_code(cand):
                return cand
    se_mod = sys.modules.get("studio_engine")
    if se_mod and u_key:
        try:
            entry = getattr(se_mod, "ACTIVE_STUDIO_ACCOUNTS", {}).get(u_key)
            if isinstance(entry, dict):
                acc_obj = entry.get("account") or {}
                c_raw = acc_obj.get("country") or acc_obj.get("region") or ""
                if _is_valid_country_code(c_raw):
                    return normalize_country_code(c_raw)
        except Exception:
            pass
    return ""


def _set_cached_account_country(username: str, uid: str, country_code: str) -> None:
    """Lưu quốc gia đã tra cứu vào cache đĩa để các lần check sau không tốn thêm API."""
    norm_c = normalize_country_code(country_code)
    if not _is_valid_country_code(norm_c):
        return
    u_key = str(username or "").lstrip("@").strip().lower()
    uid_key = str(uid or "").strip()
    with _ACCOUNT_COUNTRY_CACHE_LOCK:
        cache = _load_account_country_cache_unlocked()
        changed = False
        if u_key and cache["by_handle"].get(u_key) != norm_c:
            cache["by_handle"][u_key] = norm_c
            changed = True
        if uid_key and cache["by_uid"].get(uid_key) != norm_c:
            cache["by_uid"][uid_key] = norm_c
            changed = True
        if changed:
            _save_account_country_cache_unlocked()


def _extract_handle_from_url_or_text(raw: str) -> str:
    """Trích xuất handle sạch từ link tiktok.com/@user, @user hoặc username thuần."""
    s = str(raw or "").strip()
    if not s:
        return ""
    m_at = re.search(r"tiktok\.com/@([^/?#&\s]+)", s, re.I)
    if m_at:
        cand = m_at.group(1)
    elif "tiktok.com/" in s.lower():
        after = re.split(r"tiktok\.com/", s, maxsplit=1, flags=re.I)[-1]
        cand = after.split("?")[0].split("#")[0].split("/")[0]
        if cand.lower() in ("video", "photo", "v", "t", "explore", "foryou", "following", "live", "discover", "music", "tag"):
            return ""
    else:
        cand = s.split("?")[0].split("#")[0].split("/")[0]
    cand = urllib.parse.unquote(cand).strip().lstrip("@").lower()
    return cand if re.match(r"^[a-z0-9._-]{2,36}$", cand) else ""


def _resolve_bulk_account_handle(raw_input: str) -> str:
    """Chuẩn hóa mọi dạng @user, link hồ sơ, link video hoặc link rút gọn vt/vm về username sạch."""
    s = str(raw_input or "").strip()
    if not s:
        return ""
    if re.search(r"https?://(?:vt|vm)\.tiktok\.com/|tiktok\.com/t/", s, re.I):
        m_url = re.search(r"https?://\S+", s)
        target_url = m_url.group(0) if m_url else s
        try:
            r = requests.get(target_url, headers={"User-Agent": DEFAULT_UA}, allow_redirects=True, timeout=8)
            h_redir = _extract_handle_from_url_or_text(r.url or "")
            if h_redir:
                return h_redir
            m_uid_txt = re.search(r'"uniqueId":"([^"]+)"', r.text or "")
            if m_uid_txt:
                h_html = _extract_handle_from_url_or_text(m_uid_txt.group(1))
                if h_html:
                    return h_html
        except Exception:
            return ""
    return _extract_handle_from_url_or_text(s)


def _parse_account_labels(raw_labels: Any) -> Dict[str, Any]:
    """Chuẩn hóa trường account_labels từ TikTok Mobile API thành trạng thái đề xuất rõ ràng."""
    labels_list: List[Dict[str, Any]] = []
    if isinstance(raw_labels, str) and raw_labels.strip().startswith(("[", "{")):
        try:
            raw_labels = json.loads(raw_labels)
        except Exception:
            raw_labels = []
    if isinstance(raw_labels, dict):
        raw_labels = [raw_labels]
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, dict):
                labels_list.append({
                    "content": str(item.get("content") or "").strip(),
                    "label_type": item.get("label_type"),
                })
            elif isinstance(item, str) and item.strip():
                labels_list.append({"content": item.strip(), "label_type": None})

    for lb in labels_list:
        l_type = lb.get("label_type")
        c_low = lb["content"].lower()
        if str(l_type) == "2" or "not recommended" in c_low or "không được đề xuất" in c_low:
            return {
                "recommend_status": "not_recommended",
                "recommend_label": "Tài khoản không được đề xuất",
                "is_not_recommended": True,
                "raw_labels": labels_list,
            }

    first_warn = next((lb["content"] for lb in labels_list if lb.get("content")), "")
    if first_warn:
        label_vn = "Cảnh báo tài khoản" if "account warning" in first_warn.lower() else first_warn
        return {
            "recommend_status": "warning",
            "recommend_label": label_vn,
            "is_not_recommended": False,
            "raw_labels": labels_list,
        }

    return {
        "recommend_status": "ok",
        "recommend_label": "Đề xuất bình thường",
        "is_not_recommended": False,
        "raw_labels": [],
    }


def _first_avatar_url(usr: Dict[str, Any]) -> str:
    """Chọn URL ảnh đại diện đầu tiên khả dụng từ hồ sơ Mobile API."""
    for key in ("avatar_larger", "avatar_medium", "avatar_thumb", "avatar_300x300", "avatar_168x168"):
        av_obj = usr.get(key)
        if isinstance(av_obj, dict):
            urls = av_obj.get("url_list")
            if isinstance(urls, list):
                for u in urls:
                    if isinstance(u, str) and u.startswith("http"):
                        return u
        elif isinstance(av_obj, str) and av_obj.startswith("http"):
            return av_obj
    return ""


def _fetch_account_country_free_borrowed(
    unique_id: str,
    sec_uid: str,
    *,
    has_videos: bool = True,
    is_private: bool = False,
) -> str:
    """Lấy mã quốc gia 0đ bằng cookie mượn (Creator API video mới nhất -> locationCreated trên trang video)."""
    if is_private or not has_videos or not sec_uid or not unique_id:
        return ""
    pool = get_borrowed_cookies_pool(exclude_username=unique_id)
    if not pool:
        return ""
    for ck, ua, _ in pool[:2]:
        try:
            items = fetch_external_channel_videos_creator_api(
                sec_uid=sec_uid,
                cookie_str=ck,
                ua_str=ua or DEFAULT_UA,
                proxy_str="",
                max_videos=1,
            )
            if not items:
                continue
            vid = str(items[0].get("id") or "").strip()
            if not vid.isdigit():
                continue
            r_vid = requests.get(
                f"https://www.tiktok.com/@{unique_id}/video/{vid}",
                headers={
                    "User-Agent": ua or DEFAULT_UA,
                    "Cookie": ck,
                    "Referer": "https://www.tiktok.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=7,
            )
            if r_vid.status_code != 200 or not r_vid.text:
                continue
            raw_loc = ""
            m_uni = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>',
                r_vid.text,
            )
            if m_uni:
                try:
                    scope = json.loads(m_uni.group(1)).get("__DEFAULT_SCOPE__", {})
                    raw_loc = (
                        scope.get("webapp.video-detail", {})
                        .get("itemInfo", {})
                        .get("itemStruct", {})
                        .get("locationCreated")
                        or ""
                    )
                except Exception:
                    raw_loc = ""
            if not raw_loc:
                m_loc = re.search(r'"locationCreated"\s*:\s*"([A-Za-z]{2})"', r_vid.text)
                if m_loc:
                    raw_loc = m_loc.group(1)
            if _is_valid_country_code(raw_loc):
                return normalize_country_code(raw_loc)
        except Exception:
            continue
    return ""


def check_single_account_profile_tikhub(raw_input: str, api_key: str = "") -> Dict[str, Any]:
    """Kiểm tra trạng thái đề xuất (account_labels), ngày tạo & follow (đúng 1 request TikHub $0.001); quốc gia lấy 0đ từ cache/cookie mượn."""
    handle = _resolve_bulk_account_handle(raw_input)
    if not handle:
        return {
            "ok": False,
            "input": str(raw_input or "").strip(),
            "unique_id": "",
            "error": "Đường dẫn hoặc @username không hợp lệ.",
            "_api_calls": 0,
            "_cached_country_hit": False,
        }

    tik_key = (api_key or _get_tikhub_api_key()).strip()
    if not tik_key:
        return {
            "ok": False,
            "input": str(raw_input or "").strip(),
            "unique_id": handle,
            "error": "Chưa cấu hình TIKHUB_API_KEY trên máy chủ.",
            "_api_calls": 0,
            "_cached_country_hit": False,
        }

    hdrs = {"Authorization": f"Bearer {tik_key}", "Accept": "application/json"}
    api_calls = 0
    cached_hit = False

    try:
        r = requests.get(
            "https://api.tikhub.io/api/v1/tiktok/app/v3/handler_user_profile",
            headers=hdrs,
            params={"unique_id": handle},
            timeout=15,
        )
        body = r.json() if r.content else {}
    except Exception as exc:
        return {
            "ok": False,
            "input": str(raw_input or "").strip(),
            "unique_id": handle,
            "error": f"Lỗi kết nối API: {exc}",
            "_api_calls": 0,
            "_cached_country_hit": False,
        }

    if r.status_code != 200 or not isinstance(body, dict) or body.get("code") != 200:
        return {
            "ok": False,
            "input": str(raw_input or "").strip(),
            "unique_id": handle,
            "error": "Không tìm thấy tài khoản hoặc tài khoản đã bị khóa.",
            "_api_calls": 0,
            "_cached_country_hit": False,
        }

    api_calls += 1
    usr = (body.get("data") or {}).get("user")
    if not isinstance(usr, dict) or not str(usr.get("uid") or "").strip():
        return {
            "ok": False,
            "input": str(raw_input or "").strip(),
            "unique_id": handle,
            "error": "Không tìm thấy tài khoản hoặc tài khoản đã bị khóa.",
            "_api_calls": api_calls,
            "_cached_country_hit": False,
        }

    uid = str(usr.get("uid") or "").strip()
    sec_uid = str(usr.get("sec_uid") or "").strip()
    unique_id = str(usr.get("unique_id") or handle).lstrip("@").strip() or handle
    nickname = str(usr.get("nickname") or unique_id)
    avatar = _first_avatar_url(usr)

    followers = int(usr.get("follower_count") or 0)
    following = int(usr.get("following_count") or 0)
    videos = int(usr.get("aweme_count") or 0)
    likes = int(usr.get("total_favorited") or 0)
    is_private = bool(usr.get("secret"))

    create_date = decode_snowflake_timestamp(uid) or "—"
    if create_date == "—":
        try:
            ct = int(usr.get("create_time") or 0)
            if ct > 0:
                create_date = datetime.datetime.fromtimestamp(ct).strftime("%d/%m/%Y")
        except Exception:
            pass

    country_code = _get_cached_account_country(unique_id, uid)
    if country_code:
        cached_hit = True
    else:
        for raw_u_reg in (usr.get("region"), usr.get("country"), usr.get("account_region")):
            if _is_valid_country_code(str(raw_u_reg or "")):
                country_code = normalize_country_code(str(raw_u_reg))
                break
        if not country_code:
            country_code = _fetch_account_country_free_borrowed(
                unique_id,
                sec_uid,
                has_videos=(videos > 0),
                is_private=is_private,
            )
        if country_code:
            _set_cached_account_country(unique_id, uid, country_code)

    region_disp = format_full_country_display(country_code) if country_code else "Không rõ"
    label_info = _parse_account_labels(usr.get("account_labels"))

    return {
        "ok": True,
        "input": str(raw_input or "").strip(),
        "uid": uid,
        "sec_uid": sec_uid,
        "unique_id": unique_id,
        "nickname": nickname,
        "avatar": avatar,
        "create_date": create_date,
        "region": country_code,
        "region_display": region_disp,
        "followers": followers,
        "followers_fmt": format_count_compact(followers),
        "following": following,
        "videos": videos,
        "likes": likes,
        "is_private": is_private,
        **label_info,
        "_api_calls": api_calls,
        "_cached_country_hit": cached_hit,
    }


def check_accounts_batch_tikhub(raw_inputs: List[str], max_workers: int = 6) -> Dict[str, Any]:
    """Kiểm tra song song danh sách tài khoản TikTok (lọc trùng & giữ nguyên thứ tự)."""
    cleaned_raws = [str(x or "").strip() for x in (raw_inputs or []) if str(x or "").strip()]
    if not cleaned_raws:
        return {
            "success": False,
            "error": "Vui lòng nhập ít nhất 1 @username hoặc link TikTok.",
            "results": [],
            "summary": {"total": 0, "ok_count": 0, "not_recommended_count": 0, "warning_count": 0, "error_count": 0},
            "api_calls": 0,
            "cached_country_hits": 0,
            "cost_usd": 0.0,
            "duplicate_lines_skipped": 0,
        }

    # Giải link rút gọn & chuẩn hóa handle song song trước khi lọc trùng để tránh gọi API trùng
    resolve_workers = min(8, max(1, len(cleaned_raws)))
    with ThreadPoolExecutor(max_workers=resolve_workers) as r_ex:
        resolved_handles = list(r_ex.map(_resolve_bulk_account_handle, cleaned_raws))

    seen_keys = set()
    unique_items: List[Tuple[str, str]] = []
    dup_skipped = 0
    for raw_in, h_res in zip(cleaned_raws, resolved_handles):
        dedup_k = h_res if h_res else f"raw:{raw_in.lower()}"
        if dedup_k in seen_keys:
            dup_skipped += 1
            continue
        seen_keys.add(dedup_k)
        unique_items.append((raw_in, h_res or raw_in))

    api_key = _get_tikhub_api_key()
    workers = min(max(1, int(max_workers or 6)), max(1, len(unique_items)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        raw_results = list(ex.map(lambda pair: check_single_account_profile_tikhub(pair[1], api_key=api_key), unique_items))

    results: List[Dict[str, Any]] = []
    total_calls = 0
    cached_hits = 0
    ok_cnt = 0
    not_rec_cnt = 0
    warn_cnt = 0
    err_cnt = 0

    for (orig_raw, _), item in zip(unique_items, raw_results):
        item_clean = dict(item)
        item_clean["input"] = orig_raw
        total_calls += int(item_clean.pop("_api_calls", 0) or 0)
        if item_clean.pop("_cached_country_hit", False):
            cached_hits += 1

        if not item_clean.get("ok"):
            err_cnt += 1
        elif item_clean.get("recommend_status") == "not_recommended":
            not_rec_cnt += 1
        elif item_clean.get("recommend_status") == "warning":
            warn_cnt += 1
        else:
            ok_cnt += 1
        results.append(item_clean)

    return {
        "success": True,
        "results": results,
        "summary": {
            "total": len(results),
            "ok_count": ok_cnt,
            "not_recommended_count": not_rec_cnt,
            "warning_count": warn_cnt,
            "error_count": err_cnt,
        },
        "api_calls": total_calls,
        "cached_country_hits": cached_hits,
        "cost_usd": round(total_calls * 0.001, 4),
        "duplicate_lines_skipped": dup_skipped,
    }
