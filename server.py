# -*- coding: utf-8 -*-
"""
TikTok Dedup Web Server - FastAPI Edition
Web application độc lập cho tính năng kiểm tra trùng lặp kênh TikTok.
Tự động kích hoạt Cloudflare Tunnel để tạo tên miền công khai miễn phí (HTTPS).
"""

import os
import sys
import logging
from starlette.concurrency import run_in_threadpool
from pathlib import Path

logger = logging.getLogger("tikflow")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "server.log"

if sys.stdout is None or not hasattr(sys.stdout, "write"):
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stderr is None or not hasattr(sys.stderr, "write"):
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import re
import hmac
import hashlib
import time
import datetime
import json
import base64
import secrets
import threading
import subprocess
from typing import Optional, Dict, Any, List
from pathlib import Path

import uvicorn
import asyncio
from io import BytesIO
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# Import engine trích xuất
import core_dedup
from core_dedup import fetch_channel_info_and_posts, check_accounts_batch_tikhub, DEFAULT_UA, get_borrowed_cookie
import studio_engine
import dedup_provider
from vip_subscription import activate_paid_membership, restore_paid_memberships
from usage_counts import (ensure_usage_counts, record_dedup_success, record_account_results,
                          record_dedup_blocked, backfill_dedup_counts)
import channel_groups_store
import youtube_monitor

_YOUTUBE_MONITOR = None

# Cấu hình Google Sheet Webhook lưu vết người dùng
SHEET_WEBHOOK_URL = os.getenv(
    "SHEET_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbxiWmfPCblnMTi5PBl_Q39TjWb9Lj3Pv5UaJJ8SUTDCZvnk3yRfwhbIIaqvT6mMyLmmWQ/exec"
)

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

# Bộ nhớ đệm RAM siêu tốc cho ảnh (LRU Cache tối đa 1000 ảnh)
IMAGE_CACHE = OrderedDict()
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_CACHE_MAX = 1000

# Connection Pool dùng chung cho toàn bộ request tải ảnh (giảm 90% độ trễ SSL/TLS)
PROXY_SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1, pool_block=False)
PROXY_SESSION.mount("https://", _adapter)
PROXY_SESSION.mount("http://", _adapter)

def _get_image_cache(url: str):
    with IMAGE_CACHE_LOCK:
        if url in IMAGE_CACHE:
            val = IMAGE_CACHE.pop(url)
            IMAGE_CACHE[url] = val  # Move to end (MRU)
            return val
    return None

def _put_image_cache(url: str, content: bytes, media_type: str):
    with IMAGE_CACHE_LOCK:
        if url in IMAGE_CACHE:
            IMAGE_CACHE.pop(url)
        elif len(IMAGE_CACHE) >= IMAGE_CACHE_MAX:
            IMAGE_CACHE.popitem(last=False)
        IMAGE_CACHE[url] = (content, media_type)

def prefetch_thumbnails(video_list: list):
    """Tải trước toàn bộ ảnh bìa vào RAM cache trong background thread (tải song song 10 luồng)."""
    if not video_list:
        return
    needed = []
    for v in video_list[:30]:
        u = v.get("cover_url")
        if u and u.startswith("http") and not _get_image_cache(u):
            needed.append(u)
    if not needed:
        return

    b_cookie, b_ua, _ = get_borrowed_cookie()
    headers = {
        "User-Agent": b_ua or DEFAULT_UA,
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    if b_cookie:
        headers["Cookie"] = b_cookie

    def _load(u):
        try:
            r = PROXY_SESSION.get(u, headers=headers, timeout=6)
            if r.status_code == 200 and r.content:
                ctype = r.headers.get("Content-Type", "image/jpeg")
                _put_image_cache(u, r.content, ctype)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(10, len(needed))) as executor:
        list(executor.map(_load, needed))

# Bộ nhớ tạm để tránh spam webhook liên tục từ cùng 1 IP + user trong thời gian ngắn
_recent_logged_checks = {}

def get_client_ip(req: Request) -> str:
    """Lấy địa chỉ IP thật của khách truy cập (hỗ trợ qua Cloudflare / Reverse Proxy)."""
    cf_ip = req.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if req.client and req.client.host:
        return req.client.host
    return "Unknown"

def extract_clean_username(query: str) -> str:
    """Trích xuất username TikTok chuẩn từ query hoặc link (loại bỏ sạch sẽ mọi tiền tố @)."""
    q = str(query or "").strip()
    if "tiktok.com/@" in q:
        u = q.split("tiktok.com/@")[-1].split("?")[0].split("#")[0].split("/")[0]
    elif "tiktok.com/" in q:
        p = q.split("tiktok.com/")[-1].split("?")[0].split("#")[0].split("/")[0]
        u = p.lstrip("@")
    else:
        u = q.lstrip("@")
    return u.split("?")[0].split("#")[0].rstrip("/").strip().lstrip("@").lower()

SHEET_CONFIG_FILE = Path(__file__).resolve().parent / "sheet_config.json"
_SHEET_CFG_LOCK = threading.Lock()
_IP_GEO_CACHE: Dict[str, str] = {}
_COUNTRY_VN_TRANSLATE = {
    "Vietnam": "Việt Nam",
    "United States": "Mỹ",
    "United Kingdom": "Anh",
    "Bangladesh": "Bangladesh",
    "Singapore": "Singapore",
    "Japan": "Nhật Bản",
    "South Korea": "Hàn Quốc",
    "Germany": "Đức",
    "France": "Pháp",
    "Netherlands": "Hà Lan",
    "Thailand": "Thái Lan",
    "Indonesia": "Indonesia",
    "Malaysia": "Malaysia",
    "Philippines": "Philippines",
    "India": "Ấn Độ",
    "Taiwan": "Đài Loan",
    "Hong Kong": "Hồng Kông",
    "Australia": "Úc",
    "Canada": "Canada",
}


def _load_sheet_config() -> Dict[str, Any]:
    with _SHEET_CFG_LOCK:
        if SHEET_CONFIG_FILE.exists():
            try:
                raw = json.loads(SHEET_CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {"split_free_by_ip": bool(raw.get("split_free_by_ip", True))}
            except Exception:
                pass
    return {"split_free_by_ip": True}


def _save_sheet_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    clean = {"split_free_by_ip": bool(cfg.get("split_free_by_ip", True))}
    with _SHEET_CFG_LOCK:
        try:
            tmp_f = SHEET_CONFIG_FILE.with_suffix(".json.tmp")
            tmp_f.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_f, SHEET_CONFIG_FILE)
        except Exception as e:
            logger.error(f"[SheetCfg] Lỗi lưu sheet_config.json: {e}")
    return clean


def resolve_ip_location(ip: str) -> str:
    """Tra cứu nhanh vị trí địa lý + nhà mạng của IP khách truy cập (kèm RAM cache)."""
    s_ip = str(ip or "").strip()
    if not s_ip or s_ip == "Unknown":
        return "Không xác định"
    if s_ip in _IP_GEO_CACHE:
        return _IP_GEO_CACHE[s_ip]
    if s_ip in ("127.0.0.1", "::1", "localhost") or s_ip.startswith(("10.", "192.168.", "172.16.")):
        _IP_GEO_CACHE[s_ip] = "Máy chủ Local (PC)"
        return _IP_GEO_CACHE[s_ip]

    loc_str = ""
    try:
        r = requests.get(
            f"http://ip-api.com/json/{s_ip}?fields=status,country,countryCode,regionName,city,isp&lang=vi",
            timeout=3.5,
        )
        if r.status_code == 200:
            d = r.json() or {}
            if d.get("status") == "success":
                city = str(d.get("city") or d.get("regionName") or "").strip()
                city = {"Hanoi": "Hà Nội", "Ho Chi Minh City": "TP.HCM"}.get(city, city)
                country = str(d.get("country") or d.get("countryCode") or "").strip()
                country = _COUNTRY_VN_TRANSLATE.get(country, country)
                isp = str(d.get("isp") or "").strip()
                isp_short = re.sub(r"\s*(Corporation|Group|Company|Telecom|JSC|Ltd\.?)$", "", isp, flags=re.I).strip()[:28]
                parts = [p for p in (city, country) if p]
                base_loc = ", ".join(parts)
                loc_str = f"{base_loc} ({isp_short})" if (base_loc and isp_short) else (base_loc or isp_short)
    except Exception:
        pass

    if not loc_str:
        try:
            r2 = requests.get(f"https://ipwho.is/{s_ip}", timeout=3.5)
            if r2.status_code == 200:
                d2 = r2.json() or {}
                if d2.get("success"):
                    city2 = str(d2.get("city") or d2.get("region") or "").strip()
                    city2 = {"Hanoi": "Hà Nội", "Ho Chi Minh City": "TP.HCM"}.get(city2, city2)
                    country2 = str(d2.get("country") or d2.get("country_code") or "").strip()
                    country2 = _COUNTRY_VN_TRANSLATE.get(country2, country2)
                    isp2 = str(((d2.get("connection") or {}).get("isp")) or "").strip()[:28]
                    base2 = ", ".join(p for p in (city2, country2) if p)
                    loc_str = f"{base2} ({isp2})" if (base2 and isp2) else (base2 or isp2)
        except Exception:
            pass

    final_loc = loc_str or "Không rõ vị trí"
    if len(_IP_GEO_CACHE) > 2000:
        _IP_GEO_CACHE.clear()
    _IP_GEO_CACHE[s_ip] = final_loc
    return final_loc


def _get_sheet_webhook_url() -> str:
    env_map = _read_local_env()
    return (os.getenv("SHEET_WEBHOOK_URL") or env_map.get("SHEET_WEBHOOK_URL") or SHEET_WEBHOOK_URL or "").strip()


def _sanitize_sheet_tab(account: str) -> str:
    cleaned = re.sub(r'[\\/?*\[\]:]', '_', str(account or "").strip().lower())
    return cleaned[:95] if cleaned else "Khách_Free"


def init_user_sheet_async(account: str, role: str = "member", note: str = ""):
    """Gửi lệnh tạo trang tính (tab) riêng trên Google Sheet cho tài khoản vừa đăng ký."""
    hook_url = _get_sheet_webhook_url()
    acc_raw = str(account or "").strip().lower()
    if not acc_raw or not hook_url:
        return
    acc_sheet = _sanitize_sheet_tab(acc_raw)

    def _worker():
        try:
            vn_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%d/%m/%Y %H:%M:%S")
            payload = {
                "action": "init_user_sheet",
                "account": acc_sheet,
                "sheet_name": acc_sheet,
                "role": str(role or "member"),
                "note": str(note or "")[:120],
                "time": vn_time,
            }
            requests.post(hook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[Sheet Tracker] Lỗi khởi tạo trang tính cho {acc_sheet}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def record_user_check_async(user: str, ip: str, account: str = "", device_id: str = ""):
    """Gửi dữ liệu kiểm tra ngầm (kèm Vị trí IP) về đúng trang tính Google Sheet theo tài khoản hoặc IP."""
    hook_url = _get_sheet_webhook_url()
    s_ip = str(ip or "").strip()
    if not user or not hook_url or s_ip in ("127.0.0.1", "::1", "localhost"):
        return

    raw_acc = str(account or "").strip()
    if raw_acc:
        acc_sheet = _sanitize_sheet_tab(raw_acc)
    elif _load_sheet_config().get("split_free_by_ip"):
        short_dev = re.sub(r"[^A-Z0-9]", "", re.sub(r"^DEV-", "", str(device_id or "").strip().upper()))[:12]
        if short_dev:
            acc_sheet = f"Khách_{short_dev}"
        elif s_ip and s_ip != "Unknown":
            acc_sheet = f"Khách_{hashlib.sha256(s_ip.encode('utf-8')).hexdigest()[:8].upper()}"
        else:
            acc_sheet = "Khách_Free"
    else:
        acc_sheet = "Khách_Free"

    now = time.time()
    cache_key = f"{acc_sheet}:{ip}:{user}"
    last_time = _recent_logged_checks.get(cache_key, 0)
    if now - last_time < 60:
        return
    _recent_logged_checks[cache_key] = now

    def _worker():
        try:
            vn_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%d/%m/%Y %H:%M:%S")
            ip_loc = resolve_ip_location(ip)
            payload = {
                "action": "log_check",
                "account": acc_sheet,
                "sheet_name": acc_sheet,
                "user": user,
                "ip": ip,
                "ip_location": ip_loc,
                "device_id": str(device_id or "").strip(),
                "time": vn_time,
            }
            requests.post(hook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[Sheet Tracker] Lỗi gửi log ({acc_sheet}): {e}")

    threading.Thread(target=_worker, daemon=True).start()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
CLOUDFLARED_PATH = Path("E:/Tool/cloudflared.exe")
DOMAIN_FILE = BASE_DIR / "PUBLIC_DOMAIN.txt"
VIP_USERS_FILE = BASE_DIR / "vip_users.json"
VIP_BLOCKED_DEVICES_FILE = BASE_DIR / "vip_blocked_devices.json"
VIP_ORDERS_FILE = BASE_DIR / "vip_orders.json"
PAYOS_CONFIG_FILE = BASE_DIR / "payos_config.json"
VIP_SYNC_META_FILE = BASE_DIR / "vip_sync_meta.json"
CHECK_HISTORY_FILE = BASE_DIR / "check_history.json"
CHANNEL_GROUPS_FILE = BASE_DIR / "channel_groups.json"
CHECK_HISTORY_TTL_SECONDS = 7 * 86400
CHECK_HISTORY_MAX_CHANNELS = 60
CHECK_HISTORY_MAX_ACCOUNTS = 300
CHECK_HISTORY_MAX_VIDEOS = 100
ENV_FILE = BASE_DIR / ".env"
_VIP_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_CHANNEL_GROUPS_LOCK = threading.Lock()
_PEER_SYNC_TIMER_LOCK = threading.Lock()
_PEER_SYNC_TIMER: Optional[threading.Timer] = None
_PEER_SYNC_RUN_LOCK = threading.Lock()
_APPLYING_REMOTE_SYNC = False
RENDER_SYNC_BASE = (os.environ.get("RENDER_SYNC_BASE") or "https://tiktok-dedup-web.onrender.com").rstrip("/")
PRIMARY_SYNC_BASE = (os.environ.get("PRIMARY_SYNC_BASE") or "https://checktiktok.site").rstrip("/")
SUPABASE_SYNC_BUCKET = "tikflow-vip-vault"
SUPABASE_SYNC_OBJECT = "sync_snapshot.json"


def _env_number(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# Periodic safety-net sync (real changes are still pushed right away by
# _schedule_peer_sync_push). 5 min keeps Render awake while cutting Supabase load.
PEER_SYNC_INTERVAL_SEC = _env_number("PEER_SYNC_INTERVAL_SEC", 300, 60)
# Check history / free quota change on every check: batch them instead of 1s.
CHECK_HISTORY_SYNC_DELAY_SEC = _env_number("CHECK_HISTORY_SYNC_DELAY_SEC", 20, 1)
_PEER_SYNC_TIMER_DUE = 0.0
_SB_LAST_SNAPSHOT_HASH = ""
_PEER_SYNC_STATUS: Dict[str, Any] = {
    "last_ok_at": 0,
    "last_error": "",
    "last_direction": "",
    "user_count": 0,
    "supabase_configured": False,
    "supabase_ok_at": 0,
    "supabase_error": "",
}
PAYOS_API_BASE = "https://api-merchant.payos.vn"
PAYOS_BANK_NAMES = {
    "970422": "MB Bank",
    "970436": "Vietcombank",
    "970415": "VietinBank",
    "970418": "BIDV",
    "970416": "ACB",
    "970407": "Techcombank",
    "970423": "TPBank",
    "970432": "VPBank",
    "970448": "OCB",
    "970426": "MSB",
    "970452": "KienlongBank",
    "970403": "Sacombank",
    "970441": "VIB",
    "970437": "HDBank",
    "970405": "Agribank",
}


def _load_blocked_devices_unlocked() -> Dict[str, Dict[str, Any]]:
    if VIP_BLOCKED_DEVICES_FILE.exists():
        try:
            raw = json.loads(VIP_BLOCKED_DEVICES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_blocked_devices_unlocked(bmap: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp_f = VIP_BLOCKED_DEVICES_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(bmap, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_BLOCKED_DEVICES_FILE)
        _schedule_peer_sync_push()
    except Exception as e:
        logger.error(f"[VIPAuth] Lỗi lưu vip_blocked_devices.json: {e}")


FREE_QUOTA_FILE = BASE_DIR / "free_daily_quota.json"
FREE_DAILY_LIMIT = 10
_QUOTA_LOCK = threading.Lock()


def _vn_date_key() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%Y-%m-%d")


def _load_free_quota_unlocked() -> Dict[str, Dict[str, int]]:
    if FREE_QUOTA_FILE.exists():
        try:
            raw = json.loads(FREE_QUOTA_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_free_quota_unlocked(qmap: Dict[str, Dict[str, int]], keep_day: str) -> None:
    cleaned = {k: v for k, v in qmap.items() if k == keep_day and isinstance(v, dict)}
    try:
        tmp_f = FREE_QUOTA_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, FREE_QUOTA_FILE)
        _schedule_peer_sync_push(CHECK_HISTORY_SYNC_DELAY_SEC)
    except Exception as e:
        logger.error(f"[FreeQuota] Lỗi lưu free_daily_quota.json: {e}")


def _get_free_quota_status(dev_id: str, username: str = "") -> Dict[str, Any]:
    day_k = _vn_date_key()
    dev_k = f"dev:{dev_id}"
    usr_k = f"usr:{username.strip().lower()}" if username else ""
    with _QUOTA_LOCK:
        qmap = _load_free_quota_unlocked()
        bucket = qmap.get(day_k) if isinstance(qmap.get(day_k), dict) else {}
        dev_used = int(bucket.get(dev_k) or 0)
        usr_used = int(bucket.get(usr_k) or 0) if usr_k else 0
        used = max(dev_used, usr_used)
    return {
        "date": day_k,
        "used": used,
        "limit": FREE_DAILY_LIMIT,
        "remaining": max(0, FREE_DAILY_LIMIT - used),
        "device_id": dev_id,
    }


def _consume_free_quota(dev_id: str, username: str = "", amount: int = 1) -> Dict[str, Any]:
    day_k = _vn_date_key()
    dev_k = f"dev:{dev_id}"
    usr_k = f"usr:{username.strip().lower()}" if username else ""
    step = max(1, int(amount or 1))
    with _QUOTA_LOCK:
        qmap = _load_free_quota_unlocked()
        bucket = qmap.get(day_k)
        if not isinstance(bucket, dict):
            bucket = {}
            qmap[day_k] = bucket
        dev_used = int(bucket.get(dev_k) or 0)
        usr_used = int(bucket.get(usr_k) or 0) if usr_k else 0
        new_used = max(dev_used, usr_used) + step
        bucket[dev_k] = new_used
        if usr_k:
            bucket[usr_k] = new_used
        _save_free_quota_unlocked(qmap, day_k)
    return {
        "date": day_k,
        "used": new_used,
        "limit": FREE_DAILY_LIMIT,
        "remaining": max(0, FREE_DAILY_LIMIT - new_used),
        "device_id": dev_id,
    }


def _compute_history_video_stats(videos: List[Dict[str, Any]]) -> Dict[str, int]:
    dup_cnt = 0
    fyp_cnt = 0
    clean_cnt = 0
    for v in videos:
        if not isinstance(v, dict):
            continue
        aid = str(v.get("aweme_id") or v.get("id") or "").strip()
        gid = str(v.get("group_id") or "").strip()
        has_gid_dup = bool(gid and gid != aid and gid not in ("0", "None", "null"))
        is_dup = bool(v.get("is_dup") or v.get("is_crp_punished") or has_gid_dup)
        is_fyp = bool(
            v.get("is_fyp_restricted")
            or v.get("shadow")
            or (v.get("has_penalty") and not v.get("is_crp_punished"))
            or v.get("is_copyright")
            or int(v.get("status_code") or 0) == 141
        )
        if is_dup:
            dup_cnt += 1
        if is_fyp:
            fyp_cnt += 1
        if not is_dup and not is_fyp:
            clean_cnt += 1
    return {
        "total": len(videos),
        "dup": dup_cnt,
        "fyp": fyp_cnt,
        "clean": clean_cnt,
    }


def _compact_history_video(v: Dict[str, Any]) -> Dict[str, Any]:
    aid = str(v.get("aweme_id") or v.get("id") or "").strip()
    return {
        "id": aid,
        "aweme_id": aid,
        "group_id": str(v.get("group_id") or ""),
        "origin_url": str(v.get("origin_url") or ""),
        "is_same_channel_dup": bool(v.get("is_same_channel_dup", False)),
        "desc": str(v.get("desc") or "")[:600],
        "desc_clean": str(v.get("desc_clean") or "")[:600],
        "play_count": int(v.get("play_count") or 0),
        "digg_count": int(v.get("digg_count") or 0),
        "digg_count_fmt": str(v.get("digg_count_fmt") or "0"),
        "comment_count": int(v.get("comment_count") or 0),
        "comment_count_fmt": str(v.get("comment_count_fmt") or "0"),
        "share_count": int(v.get("share_count") or 0),
        "share_count_fmt": str(v.get("share_count_fmt") or "0"),
        "collect_count": int(v.get("collect_count") or 0),
        "collect_count_fmt": str(v.get("collect_count_fmt") or "0"),
        "create_time": int(v.get("create_time") or 0),
        "date_local": str(v.get("date_local") or ""),
        "date_utc": str(v.get("date_utc") or ""),
        "device": str(v.get("device") or "phone"),
        "source": str(v.get("source") or ""),
        "source_icon": str(v.get("source_icon") or ""),
        "is_muted": bool(v.get("is_muted", False)),
        "is_prohibited": bool(v.get("is_prohibited", False)),
        "quality": str(v.get("quality") or ""),
        "region": str(v.get("region") or ""),
        "region_display": str(v.get("region_display") or ""),
        "shadow": bool(v.get("shadow", False)),
        "shadowReason": str(v.get("shadowReason") or ""),
        "is_dup": bool(v.get("is_dup", False)),
        "is_crp_punished": bool(v.get("is_crp_punished", False)),
        "is_fyp_restricted": bool(v.get("is_fyp_restricted", False)),
        "has_penalty": bool(v.get("has_penalty", False)),
        "is_copyright": bool(v.get("is_copyright", False)),
        "is_takedown": bool(v.get("is_takedown", False)),
        "status_code": int(v.get("status_code") or 0),
        "penalty_details": str(v.get("penalty_details") or ""),
        "cover_url": str(v.get("cover_url") or ""),
        "duration_str": str(v.get("duration_str") or ""),
        "share_url": str(v.get("share_url") or ""),
    }


def _prune_history_bucket_unlocked(
    entries: Dict[str, Any],
    deleted: Dict[str, Any],
    cutoff: int,
    max_items: int,
) -> bool:
    """Dọn các mục hết hạn (>7 ngày) và giới hạn số lượng tối đa trong 1 ngăn lịch sử."""
    changed = False
    for h_key in list(entries.keys()):
        rec = entries.get(h_key)
        chk_at = int(rec.get("checked_at") or 0) if isinstance(rec, dict) else 0
        if not isinstance(rec, dict) or chk_at <= 0 or chk_at < cutoff:
            entries.pop(h_key, None)
            changed = True

    for d_key in list(deleted.keys()):
        d_ts = int(deleted.get(d_key) or 0)
        if d_ts <= 0 or d_ts < cutoff:
            deleted.pop(d_key, None)
            changed = True

    if len(entries) > max_items:
        keep_items = sorted(entries.items(), key=lambda kv: -int((kv[1] or {}).get("checked_at") or 0))[:max_items]
        entries.clear()
        entries.update(keep_items)
        changed = True
    return changed


def _merge_history_bucket(
    l_ent: Dict[str, Any],
    r_ent: Dict[str, Any],
    l_del: Dict[str, Any],
    r_del: Dict[str, Any],
    cutoff: int,
    tie_score_fn=None,
) -> tuple:
    """Trộn 2 chiều 1 ngăn lịch sử theo timestamp mới nhất và tôn trọng tombstone xóa."""
    merged_del: Dict[str, int] = {}
    for dk in set(l_del.keys()) | set(r_del.keys()):
        dk_c = str(dk or "").lstrip("@").strip().lower()
        if not dk_c:
            continue
        dt_val = max(int(l_del.get(dk) or 0), int(r_del.get(dk) or 0))
        if dt_val >= cutoff:
            merged_del[dk_c] = dt_val

    merged_ent: Dict[str, Any] = {}
    for hk in set(l_ent.keys()) | set(r_ent.keys()):
        hk_c = str(hk or "").lstrip("@").strip().lower()
        if not hk_c:
            continue
        le = l_ent.get(hk) if isinstance(l_ent.get(hk), dict) else None
        re_obj = r_ent.get(hk) if isinstance(r_ent.get(hk), dict) else None
        if not le and not re_obj:
            continue
        lt = int((le or {}).get("checked_at") or 0)
        rt = int((re_obj or {}).get("checked_at") or 0)
        if lt != rt:
            pick_e = le if lt > rt else re_obj
        elif tie_score_fn is not None:
            pick_e = le if tie_score_fn(le) >= tie_score_fn(re_obj) else re_obj
        else:
            pick_e = le or re_obj
        if not isinstance(pick_e, dict):
            continue
        chk_ts = int(pick_e.get("checked_at") or 0)
        if chk_ts < cutoff or merged_del.get(hk_c, 0) >= chk_ts:
            continue
        merged_ent[hk_c] = json.loads(json.dumps(pick_e))
    return merged_ent, merged_del


def _prune_check_history_unlocked(hmap: Dict[str, Any], now_ts: Optional[int] = None) -> bool:
    """Dọn lịch sử check quá 7 ngày (cả Check Trùng & Check Tài Khoản) cho từng user."""
    if not isinstance(hmap, dict):
        return False
    cur_ts = int(now_ts if now_ts is not None else time.time())
    cutoff = cur_ts - CHECK_HISTORY_TTL_SECONDS
    changed = False

    for acct_key in list(hmap.keys()):
        acct_box = hmap.get(acct_key)
        if not isinstance(acct_box, dict):
            hmap.pop(acct_key, None)
            changed = True
            continue
        entries = acct_box.get("entries")
        if not isinstance(entries, dict):
            entries = {}
            acct_box["entries"] = entries
            changed = True
        deleted = acct_box.get("deleted")
        if not isinstance(deleted, dict):
            deleted = {}
            acct_box["deleted"] = deleted
            changed = True
        acc_entries = acct_box.get("account_entries")
        if not isinstance(acc_entries, dict):
            acc_entries = {}
            acct_box["account_entries"] = acc_entries
            changed = True
        acc_deleted = acct_box.get("account_deleted")
        if not isinstance(acc_deleted, dict):
            acc_deleted = {}
            acct_box["account_deleted"] = acc_deleted
            changed = True

        if _prune_history_bucket_unlocked(entries, deleted, cutoff, CHECK_HISTORY_MAX_CHANNELS):
            changed = True
        if _prune_history_bucket_unlocked(acc_entries, acc_deleted, cutoff, CHECK_HISTORY_MAX_ACCOUNTS):
            changed = True

        if not entries and not deleted and not acc_entries and not acc_deleted:
            hmap.pop(acct_key, None)
            changed = True

    return changed


def _load_check_history_unlocked() -> Dict[str, Any]:
    if CHECK_HISTORY_FILE.exists():
        try:
            raw = json.loads(CHECK_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_check_history_unlocked(hmap: Dict[str, Any], schedule_sync: bool = True) -> None:
    try:
        tmp_f = CHECK_HISTORY_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(hmap, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_f, CHECK_HISTORY_FILE)
        if schedule_sync:
            _schedule_peer_sync_push(CHECK_HISTORY_SYNC_DELAY_SEC)
    except Exception as e:
        logger.error(f"[CheckHistory] Lỗi lưu check_history.json: {e}")


def _load_channel_groups_unlocked() -> Dict[str, Any]:
    return channel_groups_store.load_channel_groups_file(CHANNEL_GROUPS_FILE)


def _save_channel_groups_unlocked(state: Dict[str, Any], schedule_sync: bool = True) -> Dict[str, Any]:
    try:
        saved = channel_groups_store.save_channel_groups_file(CHANNEL_GROUPS_FILE, state)
        if schedule_sync:
            _schedule_peer_sync_push(CHECK_HISTORY_SYNC_DELAY_SEC)
        return saved
    except Exception as e:
        logger.error(f"[ChannelGroups] Lỗi lưu channel_groups.json: {e}")
        return channel_groups_store.normalize_store_payload(state)


def _record_check_history_for_user(
    account_username: str,
    res: Dict[str, Any],
    fallback_username: str = "",
    append_mode: bool = False,
) -> None:
    """Ghi lịch sử check riêng cho tài khoản đã đăng nhập (ghi đè lần check mới nhất của kênh, tự hết hạn 7 ngày)."""
    acct = str(account_username or "").strip().lower()
    if not acct or not isinstance(res, dict) or not res.get("success"):
        return
    ch_obj = res.get("channel") if isinstance(res.get("channel"), dict) else {}
    ui_obj = res.get("user_info") if isinstance(res.get("user_info"), dict) else {}
    raw_target = ch_obj.get("uniqueId") or ui_obj.get("username") or fallback_username or ""
    target_handle = re.sub(r"[^a-zA-Z0-9._-]", "", str(raw_target).lstrip("@").strip().lower())
    if not target_handle:
        return

    new_videos = [
        _compact_history_video(v)
        for v in (res.get("list") or [])
        if isinstance(v, dict) and str(v.get("aweme_id") or v.get("id") or "").strip()
    ]
    now_ts = int(time.time())

    compact_channel = {
        "uniqueId": target_handle,
        "nickname": str(ch_obj.get("nickname") or target_handle),
        "avatar": str(ch_obj.get("avatar") or ""),
        "createDate": str(ch_obj.get("createDate") or ui_obj.get("create_date") or "—"),
        "region": str(ch_obj.get("region") or "US"),
        "region_display": str(ch_obj.get("region_display") or ui_obj.get("country") or ""),
        "followers": int(ch_obj.get("followers") or 0),
        "followers_fmt": str(ch_obj.get("followers_fmt") or "0"),
        "videoCount": int(ch_obj.get("videoCount") or len(new_videos)),
    }
    compact_ui = {
        "uid": str(ui_obj.get("uid") or "---"),
        "username": f"@{target_handle}",
        "screen_name": str(ui_obj.get("screen_name") or compact_channel["nickname"]),
        "create_date": str(ui_obj.get("create_date") or compact_channel["createDate"]),
        "mobile": str(ui_obj.get("mobile") or "—"),
        "country": str(ui_obj.get("country") or compact_channel["region_display"] or "Hoa Kỳ (US)"),
        "last_nickname_change": str(ui_obj.get("last_nickname_change") or "—"),
        "is_self": bool(ui_obj.get("is_self", False)),
        "is_private_account": bool(ui_obj.get("is_private_account", False)),
    }

    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        _prune_check_history_unlocked(hmap, now_ts)
        acct_box = hmap.setdefault(acct, {"entries": {}, "deleted": {}, "account_entries": {}, "account_deleted": {}})
        entries = acct_box.setdefault("entries", {})
        deleted = acct_box.setdefault("deleted", {})

        existing = entries.get(target_handle) if append_mode and isinstance(entries.get(target_handle), dict) else None
        if existing:
            merged_vids = list(existing.get("videos") or [])
            seen_ids = {str(v.get("aweme_id") or v.get("id") or "") for v in merged_vids if isinstance(v, dict)}
            for nv in new_videos:
                naid = str(nv.get("aweme_id") or "")
                if naid and naid not in seen_ids:
                    seen_ids.add(naid)
                    merged_vids.append(nv)
            final_videos = merged_vids[:CHECK_HISTORY_MAX_VIDEOS]
            merged_ch = dict(existing.get("channel") or {})
            for ck, cv in compact_channel.items():
                if cv or ck not in merged_ch:
                    merged_ch[ck] = cv
            merged_ui = dict(existing.get("user_info") or {})
            for uk, uv in compact_ui.items():
                if (uv and uv not in ("—", "---")) or uk not in merged_ui:
                    merged_ui[uk] = uv
        else:
            final_videos = new_videos[:CHECK_HISTORY_MAX_VIDEOS]
            merged_ch = compact_channel
            merged_ui = compact_ui

        stats = _compute_history_video_stats(final_videos)
        entries[target_handle] = {
            "username": target_handle,
            "nickname": str(merged_ch.get("nickname") or target_handle),
            "avatar": str(merged_ch.get("avatar") or ""),
            "region": str(merged_ch.get("region") or "US"),
            "region_display": str(merged_ch.get("region_display") or ""),
            "followers": int(merged_ch.get("followers") or 0),
            "followers_fmt": str(merged_ch.get("followers_fmt") or "0"),
            "create_date": str(merged_ch.get("createDate") or "—"),
            "video_count_total": int(merged_ch.get("videoCount") or len(final_videos)),
            "checked_at": now_ts,
            "channel": merged_ch,
            "user_info": merged_ui,
            "videos": final_videos,
            "stats": stats,
        }
        deleted.pop(target_handle, None)
        _prune_check_history_unlocked(hmap, now_ts)
        _save_check_history_unlocked(hmap, schedule_sync=True)

    # Tự động cập nhật avatar / nickname / trạng thái check gần nhất vào bảng Check Nhóm Kênh của tài khoản đang đăng nhập
    if acct:
        try:
            with _CHANNEL_GROUPS_LOCK:
                cg_store = _load_channel_groups_unlocked()
                user_board = (cg_store.get("users") or {}).get(acct)
                cg_changed = False
                if isinstance(user_board, dict):
                    for grp in user_board.get("groups") or []:
                        for ch in grp.get("channels") or []:
                            if str(ch.get("username") or "").lower() == target_handle:
                                new_av = str(merged_ch.get("avatar") or "")
                                new_nick = str(merged_ch.get("nickname") or target_handle)
                                if new_av and ch.get("avatarUrl") != new_av:
                                    ch["avatarUrl"] = new_av
                                    cg_changed = True
                                if new_nick and (not ch.get("name") or ch.get("name") == target_handle) and ch.get("name") != new_nick:
                                    ch["name"] = new_nick
                                    cg_changed = True
                                ch["lastCheck"] = {
                                    "checkedAt": now_ts,
                                    "total": int(stats.get("total") or len(final_videos)),
                                    "dup": int(stats.get("dup") or 0),
                                    "fyp": int(stats.get("fyp") or 0),
                                    "clean": int(stats.get("clean") or 0),
                                }
                                cg_changed = True
                    if cg_changed:
                        user_board["updated_at"] = now_ts
                        _save_channel_groups_unlocked(cg_store, schedule_sync=True)
        except Exception as e_cg:
            logger.warning(f"[ChannelGroups] Bỏ qua cập nhật metadata kênh sau quét: {e_cg}")


def _record_account_check_history_for_user(
    account_username: str,
    results: List[Dict[str, Any]],
) -> None:
    """Ghi lịch sử Check Tài Khoản cho tài khoản đã đăng nhập (ghi đè lần mới nhất, tự hết hạn 7 ngày)."""
    acct = str(account_username or "").strip().lower()
    if not acct or not isinstance(results, list) or not results:
        return

    now_ts = int(time.time())
    compact_items: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        raw_h = item.get("unique_id") or item.get("username") or ""
        target_handle = re.sub(r"[^a-zA-Z0-9._-]", "", str(raw_h).lstrip("@").strip().lower())
        if not target_handle:
            continue
        compact_items.append({
            "username": target_handle,
            "unique_id": target_handle,
            "nickname": str(item.get("nickname") or target_handle),
            "uid": str(item.get("uid") or ""),
            "avatar": str(item.get("avatar") or ""),
            "create_date": str(item.get("create_date") or "—"),
            "region": str(item.get("region") or ""),
            "region_display": str(item.get("region_display") or "Không rõ"),
            "followers": int(item.get("followers") or 0),
            "followers_fmt": str(item.get("followers_fmt") or "0"),
            "is_private": bool(item.get("is_private", False)),
            "recommend_status": str(item.get("recommend_status") or "ok"),
            "recommend_label": str(item.get("recommend_label") or "Đề xuất bình thường"),
            "is_not_recommended": bool(item.get("is_not_recommended", False)),
            "raw_labels": item.get('raw_labels') if isinstance(item.get('raw_labels'), list) else [],
            "account_labels_version": int(item.get('account_labels_version') or 0),
            "recommend_note": str(item.get('recommend_note') or ''),
            "checked_at": now_ts,
        })

    if not compact_items:
        return

    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        _prune_check_history_unlocked(hmap, now_ts)
        acct_box = hmap.setdefault(acct, {"entries": {}, "deleted": {}, "account_entries": {}, "account_deleted": {}})
        acc_entries = acct_box.setdefault("account_entries", {})
        acc_deleted = acct_box.setdefault("account_deleted", {})
        for rec in compact_items:
            hk = rec["username"]
            acc_entries[hk] = rec
            acc_deleted.pop(hk, None)
        _prune_check_history_unlocked(hmap, now_ts)
        _save_check_history_unlocked(hmap, schedule_sync=True)


def _extract_device_info(req: Request, body: Optional[Dict[str, Any]] = None) -> tuple:
    raw_id = req.headers.get("x-device-id") or (body.get("device_id") if isinstance(body, dict) else "") or ""
    clean_id = re.sub(r"[^A-Z0-9-]", "", str(raw_id).strip().upper())[:20]
    client_ip = get_client_ip(req)
    if not clean_id or len(clean_id) < 6:
        ua = req.headers.get("user-agent") or ""
        h = hashlib.sha256(f"{client_ip}|{ua}".encode("utf-8")).hexdigest()[:8].upper()
        clean_id = f"DEV-{h}"
    raw_lbl = req.headers.get("x-device-label") or (body.get("device_label") if isinstance(body, dict) else "") or ""
    clean_lbl = str(raw_lbl).strip()[:36]
    return clean_id, clean_lbl, client_ip


UNLIMITED_DEVICE_USERS = {"tuandzvcc"}
THREE_DEVICE_USERS = {"admin"}


def _default_max_devices_for_user(username: str) -> int:
    u = str(username or "").strip().lower()
    if u in UNLIMITED_DEVICE_USERS:
        return 0
    if u in THREE_DEVICE_USERS:
        return 3
    return 1


def _record_and_check_device_unlocked(
    u_rec: Dict[str, Any],
    dev_id: str,
    dev_label: str,
    client_ip: str,
    blocked_map: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Kiểm tra mã máy có bị khóa / vượt giới hạn thiết bị không và ghi nhận lịch sử thiết bị + IP."""
    devs = u_rec.get("devices")
    if not isinstance(devs, dict):
        devs = {}
        u_rec["devices"] = devs

    now_ts = int(time.time())
    prev = devs.get(dev_id) if isinstance(devs.get(dev_id), dict) else {}
    uname = str(u_rec.get("username") or "").strip().lower()
    is_super_admin = str(u_rec.get("role") or "").lower() == "admin"
    is_unlimited = is_super_admin or (uname in UNLIMITED_DEVICE_USERS)
    raw_max_devs = int(u_rec.get("max_devices") if u_rec.get("max_devices") is not None else _default_max_devices_for_user(uname))
    max_devs = 0 if is_unlimited else raw_max_devs
    active_ids = [k for k, v in devs.items() if isinstance(v, dict) and not v.get("blocked")]
    enforce_limit = bool(_load_sync_meta_unlocked().get("enforce_device_limit", True))
    is_manual_block = (dev_id in blocked_map) or (bool(prev.get("blocked")) and not bool(prev.get("auto_limit")))

    if not enforce_limit:
        # Công tắc tổng Giới hạn đăng nhập đang TẮT: chỉ chặn thiết bị bị Admin khóa cứng thủ công,
        # còn lại cho phép đăng nhập thoải mái mã máy và luôn ghi nhận đầy đủ mọi Mã Máy + IP.
        if not is_unlimited and is_manual_block:
            devs[dev_id] = {
                "id": dev_id,
                "label": dev_label or prev.get("label") or "",
                "ip": client_ip,
                "last_seen": now_ts,
                "blocked": True,
                "auto_limit": False,
            }
            u_rec["updated_at"] = now_ts
            if dev_id in blocked_map:
                return f"⛔ Mã máy {dev_id} đã bị Quản trị viên khóa truy cập!"
            return f"⛔ Mã máy {dev_id} bị từ chối đăng nhập (Chống share acc)!"

        entry_unlim = {
            "id": dev_id,
            "label": dev_label or prev.get("label") or ("PC Admin" if is_super_admin else ""),
            "ip": client_ip,
            "last_seen": now_ts,
            "blocked": False,
        }
        if prev.get("admin_unblocked"):
            entry_unlim["admin_unblocked"] = True
        devs[dev_id] = entry_unlim
        u_rec["updated_at"] = now_ts
        return None

    # Kiểm tra danh sách mã máy bị Admin khóa cứng hoặc bị khóa trên tài khoản này (miễn trừ toàn bộ cho Chủ hệ thống / tài khoản vô cực)
    if not is_unlimited and ((dev_id in blocked_map) or bool(prev.get("blocked"))):
        devs[dev_id] = {
            "id": dev_id,
            "label": dev_label or prev.get("label") or "",
            "ip": client_ip,
            "last_seen": now_ts,
            "blocked": True,
            "auto_limit": bool(prev.get("auto_limit", False)) and (dev_id not in blocked_map),
        }
        u_rec["updated_at"] = now_ts
        if dev_id in blocked_map:
            return f"⛔ Mã máy {dev_id} đã bị Quản trị viên khóa truy cập!"
        if max_devs == 1:
            return "⛔ Tài khoản chỉ được dùng trên 1 mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"
        if max_devs > 1 and bool(prev.get("auto_limit")):
            return f"⛔ Tài khoản chỉ được dùng tối đa {max_devs} mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"
        return f"⛔ Mã máy {dev_id} bị từ chối đăng nhập (Chống share acc)!"

    # Giới hạn số lượng mã máy: tuandzvcc = vô cực (0), admin = 3 máy, các tài khoản khác mặc định = 1 mã máy cố định (đổi IP trên cùng mã máy vẫn hợp lệ)
    if not is_unlimited and max_devs > 0:
        is_new_over_limit = (dev_id not in devs) and (len(active_ids) >= max_devs)
        is_existing_excess = (
            (dev_id in devs)
            and (len(active_ids) > max_devs)
            and (not bool(prev.get("admin_unblocked")))
            and (dev_id not in active_ids[:max_devs])
        )
        if is_new_over_limit or is_existing_excess:
            devs[dev_id] = {
                "id": dev_id,
                "label": dev_label or prev.get("label") or "",
                "ip": client_ip,
                "last_seen": now_ts,
                "blocked": True,
                "auto_limit": True,
            }
            u_rec["updated_at"] = now_ts
            if max_devs == 1:
                return "⛔ Tài khoản chỉ được dùng trên 1 mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"
            return f"⛔ Tài khoản chỉ được dùng tối đa {max_devs} mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"

    entry_ok = {
        "id": dev_id,
        "label": dev_label or prev.get("label") or ("PC Admin" if is_super_admin else ""),
        "ip": client_ip,
        "last_seen": now_ts,
        "blocked": False,
    }
    if prev.get("admin_unblocked"):
        entry_ok["admin_unblocked"] = True
    devs[dev_id] = entry_ok
    u_rec["updated_at"] = now_ts
    return None


def _read_local_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _read_external_shop_payos_env() -> Dict[str, str]:
    """Đọc khóa PayOS dự phòng từ telegram_shop_bot/.env trên cùng PC nếu chưa khai báo ở .env riêng."""
    out: Dict[str, str] = {}
    try:
        ext_env = BASE_DIR.parent / "telegram_shop_bot" / ".env"
        if ext_env.exists():
            for line in ext_env.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                kk = k.strip()
                if kk.startswith("PAYOS_"):
                    out[kk] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _load_payos_file_config_unlocked() -> Dict[str, Any]:
    if PAYOS_CONFIG_FILE.exists():
        try:
            raw = json.loads(PAYOS_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_payos_file_config_unlocked(cfg: Dict[str, Any]) -> None:
    try:
        tmp_f = PAYOS_CONFIG_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, PAYOS_CONFIG_FILE)
        _schedule_peer_sync_push()
    except Exception as e:
        logger.error(f"[PayOS] Lỗi lưu payos_config.json: {e}")


ACCOUNT_CHECK_UNIT_PRICE_VND = 250
DEDUP_CHECK_UNIT_PRICE_VND = 26
_ACCOUNT_CHECK_USER_LOCKS: Dict[str, asyncio.Lock] = {}


def _calc_account_checks_left(balance_vnd: int) -> int:
    return max(0, int(balance_vnd or 0)) // ACCOUNT_CHECK_UNIT_PRICE_VND


def _calc_dedup_checks_left(balance_vnd: int) -> int:
    return max(0, int(balance_vnd or 0)) // DEDUP_CHECK_UNIT_PRICE_VND


def _get_user_account_check_lock(username: str) -> asyncio.Lock:
    k = str(username or "").strip().lower()
    lk = _ACCOUNT_CHECK_USER_LOCKS.get(k)
    if lk is None:
        lk = asyncio.Lock()
        _ACCOUNT_CHECK_USER_LOCKS[k] = lk
    return lk


def _get_payos_config() -> Dict[str, Any]:
    env_map = _read_local_env()
    ext_map = _read_external_shop_payos_env()
    f_cfg = _load_payos_file_config_unlocked()
    client_id = str(f_cfg.get("client_id") or os.environ.get("PAYOS_CLIENT_ID") or env_map.get("PAYOS_CLIENT_ID") or ext_map.get("PAYOS_CLIENT_ID") or "").strip()
    api_key = str(f_cfg.get("api_key") or os.environ.get("PAYOS_API_KEY") or env_map.get("PAYOS_API_KEY") or ext_map.get("PAYOS_API_KEY") or "").strip()
    checksum_key = str(f_cfg.get("checksum_key") or os.environ.get("PAYOS_CHECKSUM_KEY") or env_map.get("PAYOS_CHECKSUM_KEY") or ext_map.get("PAYOS_CHECKSUM_KEY") or "").strip()
    try:
        price_vnd = int(f_cfg.get("price_vnd") or os.environ.get("VIP_PRICE_VND") or env_map.get("VIP_PRICE_VND") or 50000)
    except Exception:
        price_vnd = 50000
    try:
        duration_days = int(f_cfg.get("duration_days") or os.environ.get("VIP_DURATION_DAYS") or env_map.get("VIP_DURATION_DAYS") or 30)
    except Exception:
        duration_days = 30
    return {
        "client_id": client_id,
        "api_key": api_key,
        "checksum_key": checksum_key,
        "price_vnd": max(2000, price_vnd),
        "duration_days": max(1, duration_days),
        "configured": bool(client_id and api_key and checksum_key),
    }


def _payos_sign_data(data: Dict[str, Any], checksum_key: str) -> str:
    parts = []
    for k in sorted(data.keys()):
        v = data[k]
        if v is None:
            v = ""
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{k}={v}")
    raw = "&".join(parts)
    return hmac.new(checksum_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def _payos_verify_webhook(data: Dict[str, Any], signature: str, checksum_key: str) -> bool:
    if not signature or not checksum_key or not isinstance(data, dict):
        return False
    expected = _payos_sign_data(data, checksum_key)
    return hmac.compare_digest(expected, str(signature).strip())


def _build_qr_data_uri(qr_payload: str) -> str:
    payload = str(qr_payload or "").strip()
    if not payload:
        return ""
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=3,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        try:
            import qrcode
            import qrcode.image.svg
            factory = qrcode.image.svg.SvgPathImage
            img = qrcode.make(payload, image_factory=factory, border=3)
            buf = BytesIO()
            img.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/svg+xml;base64,{b64}"
        except Exception as e:
            logger.error(f"[PayOS] Không tạo được ảnh QR: {e}")
            return ""


def _load_vip_orders_unlocked() -> Dict[str, Dict[str, Any]]:
    if VIP_ORDERS_FILE.exists():
        try:
            raw = json.loads(VIP_ORDERS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _save_vip_orders_unlocked(orders: Dict[str, Dict[str, Any]]) -> None:
    try:
        if len(orders) > 1000:
            items_sorted = sorted(orders.items(), key=lambda kv: int((kv[1] or {}).get("created_at") or 0), reverse=True)
            orders = dict(items_sorted[:1000])
        tmp_f = VIP_ORDERS_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_ORDERS_FILE)
        _schedule_peer_sync_push()
    except Exception as e:
        logger.error(f"[PayOS] Lỗi lưu vip_orders.json: {e}")


def _query_payos_order_sync(order_code: int, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not cfg.get("configured"):
        return None
    try:
        r = requests.get(
            f"{PAYOS_API_BASE}/v2/payment-requests/{int(order_code)}",
            headers={
                "x-client-id": cfg["client_id"],
                "x-api-key": cfg["api_key"],
            },
            timeout=12,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        if str(body.get("code")) not in ("00", "0"):
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"[PayOS] Lỗi kiểm tra đơn {order_code}: {e}")
        return None


def _create_payos_payment_sync(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        f"{PAYOS_API_BASE}/v2/payment-requests",
        json=payload,
        headers={
            "x-client-id": cfg["client_id"],
            "x-api-key": cfg["api_key"],
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    body = r.json()
    if str(body.get("code")) not in ("00", "0"):
        raise RuntimeError(str(body.get("desc") or body.get("message") or f"PayOS code {body.get('code')}"))
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("PayOS không trả về dữ liệu thanh toán.")
    return data


def _public_base_url_from_request(req: Request) -> str:
    xf_host = (req.headers.get("x-forwarded-host") or req.headers.get("host") or "").split(",")[0].strip()
    xf_proto = (req.headers.get("x-forwarded-proto") or req.url.scheme or "https").split(",")[0].strip().lower()
    if not xf_host:
        return "https://checktiktok.site"
    if "127.0.0.1" in xf_host or "localhost" in xf_host:
        return f"http://{xf_host}"
    return f"https://{xf_host}" if xf_proto not in ("http", "https") else f"{xf_proto}://{xf_host}"


def _fulfill_vip_order_unlocked(
    orders: Dict[str, Dict[str, Any]],
    users: Dict[str, Dict[str, Any]],
    order_code: int,
    amount_paid: int,
) -> Dict[str, Any]:
    """Xác nhận thanh toán PayOS (nâng cấp VIP hoặc cộng số dư Check Tài Khoản) idempotent dưới _VIP_LOCK."""
    key = str(int(order_code))
    ord_rec = orders.get(key)
    if not isinstance(ord_rec, dict):
        return {"ok": False, "reason": "order_not_found"}

    uname = str(ord_rec.get("username") or "").strip().lower()
    u_rec = users.get(uname)
    if not isinstance(u_rec, dict):
        return {"ok": False, "reason": "user_not_found"}

    if str(ord_rec.get("status") or "").upper() == "PAID":
        return {"ok": True, "already_paid": True, "newly_paid": False, "order": ord_rec, "user": u_rec}

    req_amount = int(ord_rec.get("amount") or 0)
    if int(amount_paid) < req_amount:
        return {"ok": False, "reason": "insufficient_amount"}

    now_ts = int(time.time())
    old_role = str(u_rec.get("role") or "member").lower()
    ord_type = str(ord_rec.get('order_type') or 'vip').lower()
    split = ord_rec.get('billing_policy') == 'split_v1'
    if split and ord_type not in ('vip', 'account'):
        return {'ok': False, 'reason': 'invalid_order_type'}

    if old_role != "admin":
        cfg = _get_payos_config()
        if (ord_type == "vip"
                and (split or req_amount >= int(cfg["price_vnd"]))):
            activate_paid_membership(u_rec, now_ts, int(ord_rec.get("duration_days") or cfg["duration_days"]), extend_active=split)
        credit_vnd = 0 if split and ord_type == 'vip' else max(0, int(amount_paid))
        if credit_vnd:
            u_rec["account_balance_vnd"] = max(0, int(u_rec.get("account_balance_vnd") or 0)) + credit_vnd
            u_rec["account_balance_updated_at"] = now_ts
        old_note = str(u_rec.get("note") or "").strip()
        if not old_note or old_note == "Tự đăng ký trên web":
            u_rec["note"] = f"Nạp QR PayOS ({req_amount:,}đ)"
        u_rec["updated_at"] = now_ts

    ord_rec["status"] = "PAID"
    ord_rec["amount_paid"] = int(amount_paid)
    ord_rec["paid_at"] = now_ts
    _save_vip_orders_unlocked(orders)
    _save_vip_users_unlocked(users)
    return {"ok": True, "already_paid": False, "newly_paid": True, "order": ord_rec, "user": u_rec}


def _public_order_view(ord_rec: Dict[str, Any]) -> Dict[str, Any]:
    ord_type = str(ord_rec.get("order_type") or "vip").strip().lower()
    amt_val = int(ord_rec.get("amount") or 0)
    wallet_credit = 0 if ord_rec.get('billing_policy') == 'split_v1' and ord_type == 'vip' else amt_val
    return {
        "order_code": int(ord_rec.get("order_code") or 0),
        "order_type": ord_type,
        "billing_policy": ord_rec.get('billing_policy') or 'legacy_combined',
        "wallet_credit_vnd": wallet_credit,
        "username": str(ord_rec.get("username") or ""),
        "amount": amt_val,
        "duration_days": int(ord_rec.get("duration_days") or _get_payos_config()["duration_days"]) if ord_type == 'vip' else 0,
        "dedup_checks_credit": 0,
        "dedup_unit_price_vnd": DEDUP_CHECK_UNIT_PRICE_VND,
        "checks_credit": _calc_account_checks_left(wallet_credit),
        "unit_price_vnd": ACCOUNT_CHECK_UNIT_PRICE_VND,
        "description": str(ord_rec.get("description") or ""),
        "status": str(ord_rec.get("status") or "PENDING"),
        "qr_code": str(ord_rec.get("qr_code") or ""),
        "qr_data_uri": str(ord_rec.get("qr_data_uri") or "") or _build_qr_data_uri(str(ord_rec.get("qr_code") or "")),
        "checkout_url": str(ord_rec.get("checkout_url") or ""),
        "bank_bin": str(ord_rec.get("bank_bin") or ""),
        "bank_name": str(ord_rec.get("bank_name") or ""),
        "account_number": str(ord_rec.get("account_number") or ""),
        "account_name": str(ord_rec.get("account_name") or ""),
        "created_at": int(ord_rec.get("created_at") or 0),
        "expired_at": int(ord_rec.get("expired_at") or 0),
        "paid_at": int(ord_rec.get("paid_at") or 0),
    }


def _get_vip_secret() -> bytes:
    env_map = _read_local_env()
    sec = os.environ.get("VIP_AUTH_SECRET") or env_map.get("VIP_AUTH_SECRET") or "tikflow-vip-secret-key-2026"
    return sec.encode("utf-8")


def _hash_password(password: str, salt_hex: Optional[str] = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{dk.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, dk_hex = stored_hash.split(":", 1)
        cand = _hash_password(password, salt_hex)
        return hmac.compare_digest(cand, f"{salt_hex}:{dk_hex}")
    except Exception:
        return False


def _load_vip_users_unlocked() -> Dict[str, Dict[str, Any]]:
    users: Dict[str, Dict[str, Any]] = {}
    changed = False
    if VIP_USERS_FILE.exists():
        try:
            raw = json.loads(VIP_USERS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                users = raw
        except Exception:
            users = {}

    env_map = _read_local_env()
    seed_b64 = (os.environ.get("VIP_USERS_SEED_B64") or env_map.get("VIP_USERS_SEED_B64") or "").strip()
    meta_deleted = (_load_sync_meta_unlocked().get("deleted_users") or {}) if seed_b64 else {}
    if seed_b64:
        try:
            seeded = json.loads(base64.b64decode(seed_b64).decode("utf-8"))
            if isinstance(seeded, dict):
                for k, v in seeded.items():
                    if not k or k in users or not isinstance(v, dict):
                        continue
                    del_ts = int(meta_deleted.get(k) or 0)
                    rec_ts = int(v.get("updated_at") or v.get("created_at") or 0)
                    if del_ts > 0 and del_ts >= rec_ts:
                        continue
                    users[k] = v
                    changed = True
        except Exception as e:
            logger.error(f"[VIPAuth] Lỗi giải mã VIP_USERS_SEED_B64: {e}")

    adm_u = (os.environ.get("ADMIN_DEFAULT_USER") or env_map.get("ADMIN_DEFAULT_USER") or ("admin" if not users else "")).strip().lower()
    adm_p = os.environ.get("ADMIN_DEFAULT_PASS") or env_map.get("ADMIN_DEFAULT_PASS") or "Admin@2026"
    if adm_u and adm_u not in users:
        users[adm_u] = {
            "username": adm_u,
            "password_hash": _hash_password(adm_p),
            "password_plain": adm_p,
            "role": "admin",
            "active": True,
            "expires_at": 0,
            "note": "Quản trị viên hệ thống",
            "scan_count": 0,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "token_ver": 1,
        }
        changed = True

    cfg = _get_payos_config()
    if restore_paid_memberships(users, _load_vip_orders_unlocked(), int(time.time()),
                                cfg["duration_days"], cfg["price_vnd"]):
        changed = True
    for user in users.values():
        if isinstance(user, dict) and ensure_usage_counts(user):
            user['updated_at'] = int(time.time())
            changed = True
    if changed:
        _save_vip_users_unlocked(users)
    return users


def _save_vip_users_unlocked(users: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp_f = VIP_USERS_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_USERS_FILE)
        _schedule_peer_sync_push()
    except Exception as e:
        logger.error(f"[VIPAuth] Lỗi lưu vip_users.json: {e}")


def _is_cloud_runtime() -> bool:
    return bool(os.getenv("IS_CLOUD") or os.getenv("RENDER"))


def _get_sync_token() -> str:
    return hmac.new(_get_vip_secret(), b"tikflow-pc-render-sync-v1", hashlib.sha256).hexdigest()


def _load_sync_meta_unlocked() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "deleted_users": {},
        "unblocked_devices": {},
        "enforce_device_limit": True,
        "enforce_device_limit_updated_at": 0,
    }
    if VIP_SYNC_META_FILE.exists():
        try:
            raw = json.loads(VIP_SYNC_META_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("deleted_users"), dict):
                    out["deleted_users"] = {str(k).lower(): int(v or 0) for k, v in raw["deleted_users"].items() if k}
                if isinstance(raw.get("unblocked_devices"), dict):
                    out["unblocked_devices"] = {str(k).upper(): int(v or 0) for k, v in raw["unblocked_devices"].items() if k}
                if "enforce_device_limit" in raw:
                    out["enforce_device_limit"] = bool(raw.get("enforce_device_limit"))
                out["enforce_device_limit_updated_at"] = int(raw.get("enforce_device_limit_updated_at") or 0)
        except Exception:
            pass
    return out


def _save_sync_meta_unlocked(meta: Dict[str, Any]) -> None:
    try:
        cleaned = {
            "deleted_users": dict(sorted((meta.get("deleted_users") or {}).items(), key=lambda kv: -int(kv[1] or 0))[:500]),
            "unblocked_devices": dict(sorted((meta.get("unblocked_devices") or {}).items(), key=lambda kv: -int(kv[1] or 0))[:500]),
            "enforce_device_limit": bool(meta.get("enforce_device_limit", True)),
            "enforce_device_limit_updated_at": int(meta.get("enforce_device_limit_updated_at") or 0),
        }
        tmp_f = VIP_SYNC_META_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_SYNC_META_FILE)
    except Exception as e:
        logger.error(f"[PeerSync] Lỗi lưu vip_sync_meta.json: {e}")


def _user_record_timestamp(u_rec: Dict[str, Any]) -> int:
    if not isinstance(u_rec, dict):
        return 0
    ts = max(
        int(u_rec.get("updated_at") or u_rec.get("created_at") or 0),
        int(u_rec.get("account_balance_updated_at") or 0),
    )
    devs = u_rec.get("devices") if isinstance(u_rec.get("devices"), dict) else {}
    for d in devs.values():
        if isinstance(d, dict):
            ts = max(ts, int(d.get("status_ts") or 0), int(d.get("last_seen") or 0))
    return ts


def _export_sync_snapshot_unlocked() -> Dict[str, Any]:
    users = _load_vip_users_unlocked()
    bmap = _load_blocked_devices_unlocked()
    orders = _load_vip_orders_unlocked()
    payos_cfg = _load_payos_file_config_unlocked()
    meta = _load_sync_meta_unlocked()
    with _QUOTA_LOCK:
        qmap = _load_free_quota_unlocked()
    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        if _prune_check_history_unlocked(hmap):
            _save_check_history_unlocked(hmap, schedule_sync=False)
    with _CHANNEL_GROUPS_LOCK:
        cg_state = _load_channel_groups_unlocked()
    return {
        "users": users,
        "blocked_devices": bmap,
        "orders": orders,
        "payos_config": payos_cfg,
        "free_quota": qmap,
        "check_history": hmap,
        "channel_groups": cg_state,
        "sync_meta": meta,
        "exported_at": int(time.time()),
    }


def _merge_sync_state_unlocked(remote: Dict[str, Any]) -> Dict[str, Any]:
    """Trộn 2 chiều không mất dữ liệu giữa Local và Peer, có chống vòng lặp push."""
    global _APPLYING_REMOTE_SYNC
    if not isinstance(remote, dict):
        return {"changed": False, "user_count": 0}

    l_users = _load_vip_users_unlocked()
    l_bmap = _load_blocked_devices_unlocked()
    l_orders = _load_vip_orders_unlocked()
    l_pcfg = _load_payos_file_config_unlocked()
    l_meta = _load_sync_meta_unlocked()
    with _QUOTA_LOCK:
        l_qmap = _load_free_quota_unlocked()
    with _HISTORY_LOCK:
        l_hist = _load_check_history_unlocked()
    with _CHANNEL_GROUPS_LOCK:
        l_cgroups = _load_channel_groups_unlocked()

    r_users = remote.get("users") if isinstance(remote.get("users"), dict) else {}
    r_bmap = remote.get("blocked_devices") if isinstance(remote.get("blocked_devices"), dict) else {}
    r_orders = remote.get("orders") if isinstance(remote.get("orders"), dict) else {}
    r_pcfg = remote.get("payos_config") if isinstance(remote.get("payos_config"), dict) else {}
    r_qmap = remote.get("free_quota") if isinstance(remote.get("free_quota"), dict) else {}
    r_hist = remote.get("check_history") if isinstance(remote.get("check_history"), dict) else {}
    r_cgroups = remote.get("channel_groups") if isinstance(remote.get("channel_groups"), dict) else {}
    r_meta = remote.get("sync_meta") if isinstance(remote.get("sync_meta"), dict) else {}

    # 1. Trộn sync_meta
    m_del: Dict[str, int] = dict(l_meta.get("deleted_users") or {})
    for uk, ts in (r_meta.get("deleted_users") or {}).items():
        uk_c = str(uk or "").strip().lower()
        if uk_c:
            m_del[uk_c] = max(m_del.get(uk_c, 0), int(ts or 0))

    m_unblk: Dict[str, int] = dict(l_meta.get("unblocked_devices") or {})
    for dk, ts in (r_meta.get("unblocked_devices") or {}).items():
        dk_c = str(dk or "").strip().upper()
        if dk_c:
            m_unblk[dk_c] = max(m_unblk.get(dk_c, 0), int(ts or 0))

    l_edl_val = bool(l_meta.get("enforce_device_limit", True))
    l_edl_ts = int(l_meta.get("enforce_device_limit_updated_at") or 0)
    r_edl_val = bool(r_meta.get("enforce_device_limit", True)) if "enforce_device_limit" in r_meta else l_edl_val
    r_edl_ts = int(r_meta.get("enforce_device_limit_updated_at") or 0)
    if r_edl_ts > l_edl_ts:
        m_edl_val, m_edl_ts = r_edl_val, r_edl_ts
    else:
        m_edl_val, m_edl_ts = l_edl_val, l_edl_ts

    # 2. Trộn users theo updated_at + devices_reset_at + deleted_users
    m_users: Dict[str, Dict[str, Any]] = {}
    all_unames = set(l_users.keys()) | set(r_users.keys())
    for raw_u in all_unames:
        uname = str(raw_u or "").strip().lower()
        if not uname:
            continue
        lu = l_users.get(raw_u) if isinstance(l_users.get(raw_u), dict) else None
        ru = r_users.get(raw_u) if isinstance(r_users.get(raw_u), dict) else None
        if not lu and not ru:
            continue

        lu_upd = int((lu or {}).get("updated_at") or 0)
        ru_upd = int((ru or {}).get("updated_at") or 0)
        newest_ts = max(_user_record_timestamp(lu or {}), _user_record_timestamp(ru or {}))

        del_ts = int(m_del.get(uname) or 0)
        if del_ts > 0 and del_ts >= newest_ts:
            continue
        if del_ts > 0 and newest_ts > del_ts:
            m_del.pop(uname, None)

        if lu and not ru:
            m_users[uname] = json.loads(json.dumps(lu))
            continue
        if ru and not lu:
            m_users[uname] = json.loads(json.dumps(ru))
            continue

        # Cả 2 bên đều có: ưu tiên bản có updated_at cao hơn; nếu chỉ 1 bên có explicit updated_at thì ưu tiên bên đó
        if lu_upd != ru_upd:
            primary = lu if lu_upd > ru_upd else ru
        else:
            primary = lu if _user_record_timestamp(lu) >= _user_record_timestamp(ru) else ru
        chosen = json.loads(json.dumps(primary))
        chosen["scan_count"] = max(int(lu.get("scan_count") or 0), int(ru.get("scan_count") or 0))
        chosen["account_check_count"] = max(int(lu.get("account_check_count") or 0), int(ru.get("account_check_count") or 0))
        if 'dedup_check_count' in lu or 'dedup_check_count' in ru:
            chosen['dedup_check_count'] = max(int(lu.get('dedup_check_count') or 0), int(ru.get('dedup_check_count') or 0))
            chosen['dedup_count_estimated'] = bool(lu.get('dedup_count_estimated') or ru.get('dedup_count_estimated'))
        if 'dedup_blocked_count' in lu or 'dedup_blocked_count' in ru:
            chosen['dedup_blocked_count'] = max(int(lu.get('dedup_blocked_count') or 0), int(ru.get('dedup_blocked_count') or 0))
        l_bal_ts = int(lu.get("account_balance_updated_at") or 0)
        r_bal_ts = int(ru.get("account_balance_updated_at") or 0)
        if l_bal_ts > r_bal_ts:
            chosen["account_balance_vnd"] = max(0, int(lu.get("account_balance_vnd") or 0))
            chosen["account_balance_updated_at"] = l_bal_ts
        elif r_bal_ts > l_bal_ts:
            chosen["account_balance_vnd"] = max(0, int(ru.get("account_balance_vnd") or 0))
            chosen["account_balance_updated_at"] = r_bal_ts
        else:
            chosen["account_balance_vnd"] = max(0, int(chosen.get("account_balance_vnd") or 0))
            chosen["account_balance_updated_at"] = l_bal_ts
        reset_at = max(int(lu.get("devices_reset_at") or 0), int(ru.get("devices_reset_at") or 0))
        if reset_at > 0:
            chosen["devices_reset_at"] = reset_at

        l_devs = lu.get("devices") if isinstance(lu.get("devices"), dict) else {}
        r_devs = ru.get("devices") if isinstance(ru.get("devices"), dict) else {}
        m_devs: Dict[str, Dict[str, Any]] = {}
        for did in set(l_devs.keys()) | set(r_devs.keys()):
            ld = l_devs.get(did) if isinstance(l_devs.get(did), dict) else None
            rd = r_devs.get(did) if isinstance(r_devs.get(did), dict) else None
            if not ld and not rd:
                continue
            l_dt = max(int((ld or {}).get("status_ts") or 0), int((ld or {}).get("last_seen") or 0))
            r_dt = max(int((rd or {}).get("status_ts") or 0), int((rd or {}).get("last_seen") or 0))
            d_pick = json.loads(json.dumps(ld if l_dt >= r_dt else rd))
            if reset_at > 0 and max(int(d_pick.get("last_seen") or 0), int(d_pick.get("status_ts") or 0)) <= reset_at:
                continue
            m_devs[did] = d_pick
        chosen["devices"] = m_devs
        if not chosen.get("updated_at"):
            chosen["updated_at"] = newest_ts or int(time.time())
        m_users[uname] = chosen

    # 3. Trộn blocked_devices
    m_bmap: Dict[str, Dict[str, Any]] = {}
    for did in set(l_bmap.keys()) | set(r_bmap.keys()):
        lb = l_bmap.get(did) if isinstance(l_bmap.get(did), dict) else None
        rb = r_bmap.get(did) if isinstance(r_bmap.get(did), dict) else None
        if not lb and not rb:
            continue
        lb_ts = int((lb or {}).get("blocked_at") or 0)
        rb_ts = int((rb or {}).get("blocked_at") or 0)
        pick_b = json.loads(json.dumps(lb if lb_ts >= rb_ts else rb))
        blk_ts = int(pick_b.get("blocked_at") or 0)
        unblk_ts = int(m_unblk.get(str(did).upper()) or 0)
        if unblk_ts > 0 and unblk_ts >= blk_ts:
            continue
        m_bmap[did] = pick_b

    # 4. Trộn orders (ưu tiên trạng thái PAID)
    m_orders: Dict[str, Dict[str, Any]] = {}
    for ok_key in set(l_orders.keys()) | set(r_orders.keys()):
        lo = l_orders.get(ok_key) if isinstance(l_orders.get(ok_key), dict) else None
        ro = r_orders.get(ok_key) if isinstance(r_orders.get(ok_key), dict) else None
        if not lo and not ro:
            continue
        if lo and not ro:
            m_orders[ok_key] = json.loads(json.dumps(lo))
        elif ro and not lo:
            m_orders[ok_key] = json.loads(json.dumps(ro))
        else:
            l_paid = str(lo.get("status") or "").upper() == "PAID"
            r_paid = str(ro.get("status") or "").upper() == "PAID"
            if l_paid != r_paid:
                pick_o = lo if l_paid else ro
            else:
                pick_o = lo if int(lo.get("paid_at") or lo.get("created_at") or 0) >= int(ro.get("paid_at") or ro.get("created_at") or 0) else ro
            m_orders[ok_key] = json.loads(json.dumps(pick_o))

    # 5. Trộn payos_config
    m_pcfg = json.loads(json.dumps(l_pcfg if int(l_pcfg.get("updated_at") or 0) >= int(r_pcfg.get("updated_at") or 0) else r_pcfg))

    # 6. Trộn free_daily_quota của ngày hôm nay
    day_k = _vn_date_key()
    l_day = l_qmap.get(day_k) if isinstance(l_qmap.get(day_k), dict) else {}
    r_day = r_qmap.get(day_k) if isinstance(r_qmap.get(day_k), dict) else {}
    m_day: Dict[str, int] = dict(l_day)
    for kq, vq in r_day.items():
        m_day[kq] = max(int(m_day.get(kq) or 0), int(vq or 0))
    m_qmap = {day_k: m_day} if m_day else {}

    # 7. Trộn check_history (Check Trùng + Check Tài Khoản) theo từng tài khoản
    now_hist_ts = int(time.time())
    hist_cutoff = now_hist_ts - CHECK_HISTORY_TTL_SECONDS
    m_hist: Dict[str, Any] = {}
    for raw_acct in set(l_hist.keys()) | set(r_hist.keys()):
        acct_k = str(raw_acct or "").strip().lower()
        if not acct_k:
            continue
        la = l_hist.get(raw_acct) if isinstance(l_hist.get(raw_acct), dict) else {}
        ra = r_hist.get(raw_acct) if isinstance(r_hist.get(raw_acct), dict) else {}

        merged_ent_h, merged_del_h = _merge_history_bucket(
            la.get("entries") if isinstance(la.get("entries"), dict) else {},
            ra.get("entries") if isinstance(ra.get("entries"), dict) else {},
            la.get("deleted") if isinstance(la.get("deleted"), dict) else {},
            ra.get("deleted") if isinstance(ra.get("deleted"), dict) else {},
            hist_cutoff,
            tie_score_fn=lambda e: len((e or {}).get("videos") or []),
        )
        merged_acc_ent, merged_acc_del = _merge_history_bucket(
            la.get("account_entries") if isinstance(la.get("account_entries"), dict) else {},
            ra.get("account_entries") if isinstance(ra.get("account_entries"), dict) else {},
            la.get("account_deleted") if isinstance(la.get("account_deleted"), dict) else {},
            ra.get("account_deleted") if isinstance(ra.get("account_deleted"), dict) else {},
            hist_cutoff,
            tie_score_fn=lambda e: (int((e or {}).get('account_labels_version') or 0), int((e or {}).get('labels_verified_at') or 0)),
        )

        if merged_ent_h or merged_del_h or merged_acc_ent or merged_acc_del:
            m_hist[acct_k] = {
                "entries": merged_ent_h,
                "deleted": merged_del_h,
                "account_entries": merged_acc_ent,
                "account_deleted": merged_acc_del,
            }
    _prune_check_history_unlocked(m_hist, now_hist_ts)

    m_meta = {
        "deleted_users": m_del,
        "unblocked_devices": m_unblk,
        "enforce_device_limit": m_edl_val,
        "enforce_device_limit_updated_at": m_edl_ts,
    }

    def _canon(obj: Any) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)

    any_changed = False
    _APPLYING_REMOTE_SYNC = True
    try:
        if _canon(m_users) != _canon(l_users):
            _save_vip_users_unlocked(m_users)
            any_changed = True
        if _canon(m_bmap) != _canon(l_bmap):
            _save_blocked_devices_unlocked(m_bmap)
            any_changed = True
        if _canon(m_orders) != _canon(l_orders):
            _save_vip_orders_unlocked(m_orders)
            any_changed = True
        if m_pcfg and _canon(m_pcfg) != _canon(l_pcfg):
            _save_payos_file_config_unlocked(m_pcfg)
            any_changed = True
        if _canon(m_meta) != _canon(l_meta):
            _save_sync_meta_unlocked(m_meta)
            any_changed = True
        with _QUOTA_LOCK:
            cur_q = _load_free_quota_unlocked()
            if _canon(m_qmap) != _canon(cur_q):
                _save_free_quota_unlocked(m_qmap, day_k)
                any_changed = True
        with _HISTORY_LOCK:
            cur_h = _load_check_history_unlocked()
            if _canon(m_hist) != _canon(cur_h):
                _save_check_history_unlocked(m_hist, schedule_sync=False)
                any_changed = True
        with _CHANNEL_GROUPS_LOCK:
            cur_cg = _load_channel_groups_unlocked()
            m_cgroups = channel_groups_store.merge_channel_groups_state(cur_cg, r_cgroups, deleted_users=m_del)
            if _canon(m_cgroups) != _canon(cur_cg):
                _save_channel_groups_unlocked(m_cgroups, schedule_sync=False)
                any_changed = True
    finally:
        _APPLYING_REMOTE_SYNC = False

    _PEER_SYNC_STATUS["user_count"] = len(m_users)
    return {"changed": any_changed, "user_count": len(m_users)}


def _get_supabase_sync_config() -> Dict[str, Any]:
    """Đọc cấu hình kho lưu trữ đám mây Supabase (từ env / .env / fallback local trên PC)."""
    env_map = _read_local_env()
    sb_url = (
        os.environ.get("SUPABASE_SYNC_URL")
        or env_map.get("SUPABASE_SYNC_URL")
        or os.environ.get("SUPABASE_URL")
        or env_map.get("SUPABASE_URL")
        or ""
    ).strip().rstrip("/")
    sb_key = (
        os.environ.get("SUPABASE_SYNC_KEY")
        or env_map.get("SUPABASE_SYNC_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or env_map.get("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    ).strip()
    if (not sb_url or not sb_key) and not _is_cloud_runtime():
        try:
            dev_vars_f = BASE_DIR.parent / "cloudflare_register_worker" / ".dev.vars"
            wrangler_f = BASE_DIR.parent / "cloudflare_register_worker" / "wrangler.toml"
            if dev_vars_f.exists() and not sb_key:
                for ln in dev_vars_f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = ln.strip()
                    if s and not s.startswith("#") and "=" in s:
                        k, v = s.split("=", 1)
                        if k.strip() == "SUPABASE_SERVICE_ROLE_KEY":
                            sb_key = v.strip().strip('"').strip("'")
            if wrangler_f.exists() and not sb_url:
                m_u = re.search(r'SUPABASE_URL\s*=\s*"([^"]+)"', wrangler_f.read_text(encoding="utf-8", errors="ignore"))
                if m_u:
                    sb_url = m_u.group(1).strip().rstrip("/")
        except Exception:
            pass
    configured = bool(sb_url.startswith("https://") and len(sb_key) > 20)
    _PEER_SYNC_STATUS["supabase_configured"] = configured
    return {"url": sb_url, "key": sb_key, "configured": configured}


def _supabase_headers(sb_cfg: Dict[str, Any]) -> Dict[str, str]:
    k = sb_cfg["key"]
    return {"apikey": k, "Authorization": f"Bearer {k}"}


def _supabase_ensure_bucket_sync(sb_cfg: Dict[str, Any]) -> bool:
    if not sb_cfg.get("configured"):
        return False
    hdrs = _supabase_headers(sb_cfg)
    try:
        r_get = requests.get(f"{sb_cfg['url']}/storage/v1/bucket/{SUPABASE_SYNC_BUCKET}", headers=hdrs, timeout=8)
        if r_get.status_code == 200:
            return True
        r_new = requests.post(
            f"{sb_cfg['url']}/storage/v1/bucket",
            headers={**hdrs, "Content-Type": "application/json"},
            json={"id": SUPABASE_SYNC_BUCKET, "name": SUPABASE_SYNC_BUCKET, "public": False},
            timeout=10,
        )
        return r_new.status_code in (200, 201, 409)
    except Exception as exc:
        logger.warning(f"[SupabaseSync] Lỗi kiểm tra/tạo bucket: {exc}")
        return False


def _supabase_pull_snapshot_sync(sb_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not sb_cfg.get("configured"):
        return None
    try:
        r = requests.get(
            f"{sb_cfg['url']}/storage/v1/object/{SUPABASE_SYNC_BUCKET}/{SUPABASE_SYNC_OBJECT}?t={int(time.time() * 1000)}",
            headers={**_supabase_headers(sb_cfg), "Cache-Control": "no-cache", "Pragma": "no-cache"},
            timeout=25,
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
        return None
    except Exception as exc:
        logger.warning(f"[SupabaseSync] Lỗi đọc snapshot: {exc}")
        return None


def _supabase_push_snapshot_sync(snap: Dict[str, Any], sb_cfg: Dict[str, Any]) -> bool:
    if not sb_cfg.get("configured"):
        return False
    hdrs = {
        **_supabase_headers(sb_cfg),
        "Content-Type": "application/json",
        "x-upsert": "true",
        "cache-control": "0",
    }
    payload_bytes = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    obj_url = f"{sb_cfg['url']}/storage/v1/object/{SUPABASE_SYNC_BUCKET}/{SUPABASE_SYNC_OBJECT}"
    try:
        r = requests.post(obj_url, headers=hdrs, data=payload_bytes, timeout=25)
        if r.status_code in (400, 404):
            if _supabase_ensure_bucket_sync(sb_cfg):
                r = requests.post(obj_url, headers=hdrs, data=payload_bytes, timeout=25)
        return r.status_code in (200, 201)
    except Exception as exc:
        logger.warning(f"[SupabaseSync] Lỗi ghi snapshot: {exc}")
        return False


def _snapshot_content_hash(snap: Any) -> str:
    """Hash of snapshot content, ignoring exported_at, to skip identical uploads."""
    if not isinstance(snap, dict):
        return ""
    try:
        body = {k: v for k, v in snap.items() if k != "exported_at"}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _sync_two_way_with_peer_sync(push_after_pull: bool = True) -> Dict[str, Any]:
    """Đồng bộ 2 chiều qua Supabase Storage (bền vững) + HTTP Peer PC ⇄ Render."""
    global _SB_LAST_SNAPSHOT_HASH
    if not _PEER_SYNC_RUN_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "busy"}
    try:
        is_cloud = _is_cloud_runtime()
        sb_cfg = _get_supabase_sync_config()
        sb_ok = False
        sb_pulled = False

        # 1. Kéo & trộn bản bền vững từ Supabase Storage (ưu tiên số 1 để không bao giờ mất data kể cả khi Render sleep)
        if sb_cfg.get("configured"):
            try:
                sb_remote = _supabase_pull_snapshot_sync(sb_cfg)
                remote_hash = ""
                if sb_remote is not None:
                    sb_pulled = True
                    remote_hash = _snapshot_content_hash(sb_remote)
                    with _VIP_LOCK:
                        _merge_sync_state_unlocked(sb_remote)
                if push_after_pull or sb_remote is None:
                    with _VIP_LOCK:
                        snap_sb = _export_sync_snapshot_unlocked()
                    local_hash = _snapshot_content_hash(snap_sb)
                    if local_hash and local_hash == remote_hash:
                        # Cloud copy already identical: skip the ~1 MB re-upload.
                        sb_push_ok = True
                        _PEER_SYNC_STATUS["supabase_push_skipped_at"] = int(time.time())
                    else:
                        sb_push_ok = _supabase_push_snapshot_sync(snap_sb, sb_cfg)
                    if sb_push_ok:
                        _SB_LAST_SNAPSHOT_HASH = local_hash
                        sb_ok = True
                        _PEER_SYNC_STATUS["supabase_ok_at"] = int(time.time())
                        _PEER_SYNC_STATUS["supabase_error"] = ""
                        _PEER_SYNC_STATUS["last_ok_at"] = int(time.time())
                        _PEER_SYNC_STATUS["last_error"] = ""
                        _PEER_SYNC_STATUS["user_count"] = len(snap_sb.get("users") or {})
                    else:
                        _PEER_SYNC_STATUS["supabase_error"] = "push_failed"
                elif sb_pulled:
                    sb_ok = True
                    _PEER_SYNC_STATUS["supabase_ok_at"] = int(time.time())
                    _PEER_SYNC_STATUS["supabase_error"] = ""
                    _PEER_SYNC_STATUS["last_ok_at"] = int(time.time())
                    _PEER_SYNC_STATUS["last_error"] = ""
            except Exception as e_sb:
                _PEER_SYNC_STATUS["supabase_error"] = str(e_sb)

        # 2. Đồng bộ trực tiếp PC ⇄ Render (bổ trợ thời gian thực)
        peer_base = PRIMARY_SYNC_BASE if is_cloud else RENDER_SYNC_BASE
        peer_ok = False
        peer_changed = False
        pulled_remote: Optional[Dict[str, Any]] = None
        pull_err = ""
        if peer_base:
            url = f"{peer_base}/api/internal/sync-state"
            hdrs = {
                "X-Sync-Token": _get_sync_token(),
                "X-Sync-Origin": "render" if is_cloud else "pc",
            }
            try:
                r_get = requests.get(url, headers=hdrs, timeout=20)
                if r_get.status_code == 200:
                    j_get = r_get.json()
                    if j_get.get("self_echo"):
                        pull_err = ""
                    elif j_get.get("success") and isinstance(j_get.get("snapshot"), dict):
                        pulled_remote = j_get["snapshot"]
                else:
                    pull_err = f"GET HTTP {r_get.status_code}"
            except Exception as e_get:
                pull_err = f"GET {e_get}"

            if pulled_remote is not None:
                with _VIP_LOCK:
                    _merge_sync_state_unlocked(pulled_remote)
                if sb_ok:
                    with _VIP_LOCK:
                        snap_after_peer = _export_sync_snapshot_unlocked()
                    after_hash = _snapshot_content_hash(snap_after_peer)
                    if not after_hash or after_hash != _SB_LAST_SNAPSHOT_HASH:
                        if _supabase_push_snapshot_sync(snap_after_peer, sb_cfg):
                            _SB_LAST_SNAPSHOT_HASH = after_hash

            if push_after_pull and (not is_cloud or pulled_remote is not None):
                try:
                    with _VIP_LOCK:
                        snap = _export_sync_snapshot_unlocked()
                    r_post = requests.post(url, headers=hdrs, json={"snapshot": snap}, timeout=25)
                    if r_post.status_code == 200:
                        j_post = r_post.json()
                        if not j_post.get("self_echo"):
                            peer_ok = True
                            peer_changed = bool(j_post.get("changed"))
                            _PEER_SYNC_STATUS["last_ok_at"] = int(time.time())
                            _PEER_SYNC_STATUS["last_error"] = ""
                            _PEER_SYNC_STATUS["last_direction"] = "supabase+render" if sb_ok else ("render->pc" if is_cloud else "pc<->render")
                            _PEER_SYNC_STATUS["user_count"] = len(snap.get("users") or {})
                    else:
                        pull_err = f"POST HTTP {r_post.status_code}"
                except Exception as e_post:
                    pull_err = str(e_post)

        if sb_ok or peer_ok or (not pull_err and pulled_remote is not None):
            if sb_ok and not peer_ok:
                _PEER_SYNC_STATUS["last_direction"] = "supabase_cloud"
            return {
                "ok": True,
                "supabase_ok": sb_ok,
                "peer_ok": peer_ok,
                "user_count": _PEER_SYNC_STATUS.get("user_count", 0),
                "pulled": bool(sb_pulled or pulled_remote is not None),
                "peer_changed": peer_changed,
            }

        err_final = pull_err or _PEER_SYNC_STATUS.get("supabase_error") or "sync_failed"
        _PEER_SYNC_STATUS["last_error"] = err_final
        return {"ok": False, "error": err_final}
    except Exception as exc:
        _PEER_SYNC_STATUS["last_error"] = str(exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _PEER_SYNC_RUN_LOCK.release()


def _schedule_peer_sync_push(delay_sec: float = 0.4) -> None:
    """Gom thay đổi rồi đồng bộ sang Supabase/server đối diện.

    Nếu đã có lịch đồng bộ đến hạn sớm hơn thì giữ nguyên (không dời mãi khi
    check liên tục); thay đổi gấp (delay nhỏ) vẫn được kéo lịch lên sớm hơn.
    """
    global _PEER_SYNC_TIMER, _PEER_SYNC_TIMER_DUE
    if _APPLYING_REMOTE_SYNC or os.getenv("PYTEST_CURRENT_TEST"):
        return

    def _run_bg():
        global _PEER_SYNC_TIMER
        with _PEER_SYNC_TIMER_LOCK:
            if _PEER_SYNC_TIMER is threading.current_thread():
                _PEER_SYNC_TIMER = None
        res = _sync_two_way_with_peer_sync(push_after_pull=True)
        if isinstance(res, dict) and res.get("skipped") == "busy":
            _schedule_peer_sync_push(3.0)

    delay = max(0.1, float(delay_sec))
    due = time.time() + delay
    with _PEER_SYNC_TIMER_LOCK:
        pending = _PEER_SYNC_TIMER
        if pending is not None and pending.is_alive() and _PEER_SYNC_TIMER_DUE <= due:
            return
        if pending is not None:
            try:
                pending.cancel()
            except Exception:
                pass
        t = threading.Timer(delay, _run_bg)
        t.daemon = True
        t.start()
        _PEER_SYNC_TIMER = t
        _PEER_SYNC_TIMER_DUE = due


def _create_vip_token(username: str, token_ver: int = 1, ttl_days: int = 30) -> str:
    payload = {
        "u": username.lower(),
        "v": int(token_ver),
        "exp": int(time.time()) + ttl_days * 86400,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_get_vip_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_vip_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.strip().split(".", 1)
        expected_sig = hmac.new(_get_vip_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        pad = "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8"))
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        uname = str(payload.get("u") or "").strip().lower()
        t_ver = int(payload.get("v") or 1)
        with _VIP_LOCK:
            users = _load_vip_users_unlocked()
            u_obj = users.get(uname)
            if not isinstance(u_obj, dict):
                return None
            if not u_obj.get("active", True):
                return None
            if int(u_obj.get("token_ver") or 1) != t_ver:
                return None
            return dict(u_obj)
    except Exception:
        return None


def _has_manual_vip_privilege(u: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(u, dict) or not u.get("active", True):
        return False
    role = str(u.get("role") or "member").lower()
    if role == "admin":
        return True
    if role != "vip":
        return False
    exp_at = int(u.get("expires_at") or 0)
    return exp_at <= 0 or exp_at > int(time.time())


def _has_vip_privilege(u: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(u, dict) or not u.get("active", True):
        return False
    if _has_manual_vip_privilege(u):
        return True
    return False


def _extract_vip_user(req: Request, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    tok = ""
    auth_hdr = req.headers.get("authorization") or req.headers.get("Authorization") or ""
    if auth_hdr.lower().startswith("bearer "):
        tok = auth_hdr[7:].strip()
    if not tok and isinstance(body, dict):
        tok = str(body.get("vip_token") or "").strip()
    return _verify_vip_token(tok)


def _public_vip_user(u: Dict[str, Any], blocked_map: Optional[Dict[str, Any]] = None, include_password: bool = False) -> Dict[str, Any]:
    raw_devs = u.get("devices") if isinstance(u.get("devices"), dict) else {}
    bmap = blocked_map or {}
    dev_list = []
    for did, dinfo in raw_devs.items():
        if not isinstance(dinfo, dict):
            continue
        is_blk = bool(dinfo.get("blocked")) or (did in bmap)
        dev_list.append({
            "id": str(dinfo.get("id") or did),
            "label": str(dinfo.get("label") or ""),
            "ip": str(dinfo.get("ip") or ""),
            "last_seen": int(dinfo.get("last_seen") or 0),
            "blocked": is_blk,
        })
    dev_list.sort(key=lambda x: -x["last_seen"])
    uname_pub = str(u.get("username") or "").strip().lower()
    raw_max_devs = int(u.get("max_devices") if u.get("max_devices") is not None else _default_max_devices_for_user(uname_pub))
    pub_max_devs = 0 if (uname_pub in UNLIMITED_DEVICE_USERS or str(u.get("role") or "").lower() == "admin") else raw_max_devs
    bal_vnd = max(0, int(u.get("account_balance_vnd") or 0))
    out = {
        "username": u.get("username"),
        "role": u.get("role", "member"),
        "is_vip": _has_vip_privilege(u),
        "has_manual_vip": _has_manual_vip_privilege(u),
        "active": bool(u.get("active", True)),
        "expires_at": int(u.get("expires_at") or 0),
        "note": str(u.get("note") or ""),
        "scan_count": int(u.get("scan_count") or 0),
        "dedup_check_count": int(u.get("dedup_check_count", max(0, int(u.get("scan_count") or 0) - int(u.get("account_check_count") or 0)))),
        "dedup_count_estimated": bool(u.get("dedup_count_estimated", int(u.get("scan_count") or 0) > 0)),
        "dedup_blocked_count": max(0, int(u.get("dedup_blocked_count") or 0)),
        "account_balance_vnd": bal_vnd,
        "dedup_checks_left": _calc_dedup_checks_left(bal_vnd),
        "dedup_check_unit_price_vnd": DEDUP_CHECK_UNIT_PRICE_VND,
        "account_checks_left": _calc_account_checks_left(bal_vnd),
        "account_check_unit_price_vnd": ACCOUNT_CHECK_UNIT_PRICE_VND,
        "account_check_count": int(u.get("account_check_count") or 0),
        "created_at": int(u.get("created_at") or 0),
        "max_devices": pub_max_devs,
        "devices": dev_list,
    }
    if include_password:
        out["password_plain"] = str(u.get("password_plain") or "—")
    return out

app = FastAPI(title="TikTok Dedup Studio Web", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_MAIL_CLIENT_INIT_LOCK = threading.Lock()
_MAIL_CLIENT_READY = False


def _prepare_client_mail_service():
    global _MAIL_CLIENT_READY
    with _MAIL_CLIENT_INIT_LOCK:
        from outlook_web_store import OUTLOOK_WEB_SERVICE
        if not _MAIL_CLIENT_READY:
            from outlook_client_worker import MailCloudStore, ClientMailWorker
            store = MailCloudStore(_get_supabase_sync_config(), SUPABASE_SYNC_BUCKET)
            restored = store.load()
            with OUTLOOK_WEB_SERVICE._lock:
                if restored is not None:
                    OUTLOOK_WEB_SERVICE._state = restored
                OUTLOOK_WEB_SERVICE._persist_callback = store.save
                ClientMailWorker(OUTLOOK_WEB_SERVICE)
            _MAIL_CLIENT_READY = True
        return OUTLOOK_WEB_SERVICE


@app.middleware('http')
async def client_mail_cloud_owner(req: Request, call_next):
    if not req.url.path.startswith('/api/outlook-oauth/'):
        return await call_next(req)
    if not _is_cloud_runtime():
        body = await req.body()
        if len(body) > 2_000_000:
            return JSONResponse(status_code=413, content={'success':False,'error':'Dữ liệu quá lớn.'})
        headers = {key:req.headers[key] for key in ('authorization','content-type','x-mail-worker-token','x-mail-worker-id') if key in req.headers}
        def forward():
            try:
                target=RENDER_SYNC_BASE + req.url.path + ('?'+req.url.query if req.url.query else '')
                response=requests.request(req.method,target,headers=headers,data=body,timeout=85,allow_redirects=False)
                safe_headers={key:response.headers[key] for key in ('content-type','content-disposition','x-mail-worker-id') if key in response.headers}
                return Response(response.content,status_code=response.status_code,headers={**safe_headers,'Cache-Control':'no-store'})
            except requests.RequestException:
                return JSONResponse(status_code=503,content={'success':False,'error':'Chưa kết nối được quản lý Mail trên Render. PC không chạy trình duyệt thay máy khách.'})
        return await run_in_threadpool(forward)
    # Check access before loading a private store. Worker credentials are verified
    # by their own bounded endpoints; admin endpoints require the web session.
    if '/worker/' not in req.url.path and not _is_strict_tuandzvcc(_extract_vip_user(req)):
        return JSONResponse(status_code=403,content={'success':False,'error':'Tính năng chỉ dành riêng cho tuandzvcc.'})
    try:
        await run_in_threadpool(_prepare_client_mail_service)
        return await call_next(req)
    except PermissionError:
        return JSONResponse(status_code=403,content={'success':False,'error':'Worker không hợp lệ hoặc đã thu hồi.'})
    except ValueError as exc:
        return JSONResponse(status_code=400,content={'success':False,'error':str(exc)[:220]})
    except Exception:
        logger.exception('[MailWorker] Request failed without returning private account data.')
        return JSONResponse(status_code=503,content={'success':False,'error':'Quản lý Mail chưa sẵn sàng hoặc chưa lưu được vào cloud. Thử lại sau.'})


@app.post('/api/outlook-oauth/worker/download')
def api_download_mail_worker(req: Request):
    if not _is_strict_tuandzvcc(_extract_vip_user(req)):
        return JSONResponse(status_code=403,content={'success':False,'error':'Không có quyền tải worker.'})
    from outlook_client_worker import launcher
    service=_prepare_client_mail_service()
    worker_id,token=service._client_worker.pair()
    return Response(launcher('https://checktiktok.site',worker_id,token,BASE_DIR,asset_origin=RENDER_SYNC_BASE).encode('ascii'),
        media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="tikflow_mail_worker.bat"','Cache-Control':'no-store','X-Mail-Worker-Id':worker_id})


@app.post('/api/outlook-oauth/worker/revoke')
async def api_revoke_mail_worker(req: Request):
    if not _is_strict_tuandzvcc(_extract_vip_user(req)):
        return JSONResponse(status_code=403,content={'success':False,'error':'Không có quyền thu hồi worker.'})
    body=await req.json()
    return JSONResponse(content=await run_in_threadpool(_prepare_client_mail_service()._client_worker.revoke,str(body.get('worker_id') or '')))


@app.post('/api/outlook-oauth/worker/{action}')
async def api_mail_worker_request(req: Request, action: str):
    if action not in ('poll','event'):
        return JSONResponse(status_code=404,content={'success':False})
    raw=await req.body()
    if len(raw)>100_000:
        return JSONResponse(status_code=413,content={'success':False})
    body=json.loads(raw)
    if not isinstance(body,dict):raise ValueError('Dữ liệu worker không hợp lệ.')
    controller=_prepare_client_mail_service()._client_worker
    method=controller.poll if action=='poll' else controller.event
    result=await run_in_threadpool(method,req.headers.get('x-mail-worker-id',''),req.headers.get('x-mail-worker-token',''),body)
    return JSONResponse(content=result,headers={'Cache-Control':'no-store'})


@app.post('/api/outlook-oauth/migrate-local')
async def api_mail_migrate_local(req: Request):
    if not _is_strict_tuandzvcc(_extract_vip_user(req)):
        return JSONResponse(status_code=403,content={'success':False})
    raw=await req.body()
    if len(raw)>2_000_000:return JSONResponse(status_code=413,content={'success':False})
    data=json.loads(raw)
    service=_prepare_client_mail_service()
    with service._lock:
        if any(service._state.get('folders',{}).values()):
            return JSONResponse(status_code=409,content={'success':False,'error':'Cloud đã có mail; không ghi đè bằng bản local.'})
        if not isinstance(data.get('folders'),dict):raise ValueError('Không có thư mục mail hợp lệ.')
        folders=data['folders']
        for name,rows in folders.items():
            if not isinstance(name,str) or not isinstance(rows,list) or any(not isinstance(row,dict) or not row.get('row_id') or not row.get('email') for row in rows):
                raise ValueError('Cấu trúc thư mục/mail không hợp lệ.')
        service._state.update(folders=folders,settings=data.get('settings') or {},current_folder=data.get('current_folder','Default'))
        service._save_unlocked()
    return JSONResponse(content={'success':True})

# Trạng thái Cloudflare Tunnel
tunnel_state = {
    "url": "",
    "status": "starting",
    "error": None,
    "started_at": 0,
    "proc": None
}

def start_cloudflare_tunnel(port: int = 8000):
    """Khởi động Cloudflare Tunnel và kết nối tên miền riêng https://checktiktok.site (hoặc trycloudflare dự phòng)."""
    if not CLOUDFLARED_PATH.exists():
        tunnel_state["status"] = "not_found"
        tunnel_state["error"] = f"Không tìm thấy file {CLOUDFLARED_PATH}"
        print(f"[Tunnel] {tunnel_state['error']}")
        return

    named_config = Path.home() / ".cloudflared" / "config.yml"
    if named_config.exists():
        custom_url = "https://checktiktok.site"
        tunnel_state["url"] = custom_url
        tunnel_state["status"] = "online"
        print("=" * 70, flush=True)
        print(f"🚀 TÊN MIỀN RIÊNG CHÍNH THỨC: {custom_url}", flush=True)
        print(f"   (Dự phòng: https://www.checktiktok.site)", flush=True)
        print(f"🔗 Local: http://localhost:{port}", flush=True)
        print("=" * 70, flush=True)
        try:
            with open(DOMAIN_FILE, "w", encoding="utf-8") as f:
                f.write(custom_url)
        except Exception:
            pass

        # Tránh sinh trùng hàng chục tiến trình cloudflared.exe của chính tunnel tiktok-tool (bỏ qua service hệ thống rỗng CommandLine)
        try:
            if os.name == "nt":
                ps_chk = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", "@(Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -like '*tiktok-tool*' }).Count"],
                    text=True,
                    errors="ignore",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).strip()
                if ps_chk.isdigit() and int(ps_chk) >= 1:
                    print(f"[Tunnel] Đã có {ps_chk} tiến trình cloudflared tiktok-tool đang chạy -> tái sử dụng ngay!", flush=True)
                    return
        except Exception:
            pass

        cmd = [
            str(CLOUDFLARED_PATH),
            "tunnel",
            "--config", str(named_config),
            "run", "tiktok-tool"
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            tunnel_state["proc"] = proc
            tunnel_state["started_at"] = time.time()
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
        except Exception as e:
            tunnel_state["status"] = "error"
            tunnel_state["error"] = str(e)
            print(f"[Tunnel] Lỗi chạy Cloudflare Named Tunnel: {e}")
        return

    cmd = [
        str(CLOUDFLARED_PATH),
        "tunnel",
        "--url", f"http://127.0.0.1:{port}",
        "--no-autoupdate",
    ]

    print(f"[Tunnel] Đang khởi động Cloudflare Tunnel kết nối tới port {port}...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            bufsize=1,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        tunnel_state["proc"] = proc
        tunnel_state["started_at"] = time.time()

        url_found = False
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            if not url_found:
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    found_url = m.group(0)
                    tunnel_state["url"] = found_url
                    tunnel_state["status"] = "online"
                    url_found = True
                    print("=" * 70, flush=True)
                    print(f"🎉 TÊN MIỀN WEB MIỄN PHÍ ĐÃ SẴN SÀNG: {found_url}", flush=True)
                    print(f"👉 Local: http://localhost:{port}", flush=True)
                    print("=" * 70, flush=True)
                    try:
                        with open(DOMAIN_FILE, "w", encoding="utf-8") as f:
                            f.write(found_url)
                    except Exception:
                        pass

        if not url_found:
            tunnel_state["status"] = "failed"
            print("[Tunnel] Không bắt được URL Cloudflare Tunnel.")
    except Exception as e:
        tunnel_state["status"] = "error"
        tunnel_state["error"] = str(e)
        print(f"[Tunnel] Lỗi chạy Cloudflare: {e}")

@app.on_event("startup")
def on_startup():
    dedup_provider.start_local_supervisor()
    # Khôi phục ngay bản dữ liệu bền vững mới nhất từ Supabase Cloud trước khi phục vụ (bảo toàn 100% dù Render sleep/restart)
    try:
        sb_cfg = _get_supabase_sync_config()
        if sb_cfg.get("configured"):
            sb_snap = _supabase_pull_snapshot_sync(sb_cfg)
            if sb_snap is not None:
                with _VIP_LOCK:
                    _merge_sync_state_unlocked(sb_snap)
                _PEER_SYNC_STATUS["supabase_ok_at"] = int(time.time())
                _PEER_SYNC_STATUS["last_ok_at"] = int(time.time())
                _PEER_SYNC_STATUS["last_direction"] = "supabase_boot_restore"
    except Exception as e_boot:
        logger.warning(f"[SupabaseSync] Bỏ qua khôi phục lúc khởi động: {e_boot}")

    # Cộng bù 1 lần lượt Check trùng bị đếm thiếu (không bao giờ làm giảm số)
    try:
        with _VIP_LOCK:
            bf_users = _load_vip_users_unlocked()
            with _QUOTA_LOCK:
                bf_quota = _load_free_quota_unlocked().get(_vn_date_key()) or {}
            bf_raised = backfill_dedup_counts(bf_users, _load_check_history_unlocked(), bf_quota)
            _save_vip_users_unlocked(bf_users)
        if bf_raised:
            logger.info(f"[UsageBackfill] Đã cộng bù lượt Check trùng cho: {', '.join(bf_raised)}")
    except Exception as e_bf:
        logger.warning(f"[UsageBackfill] Bỏ qua cộng bù: {e_bf}")

    # Only Render owns YouTube scheduling and its separate private cloud object.
    global _YOUTUBE_MONITOR
    if _is_cloud_runtime():
        try:
            _YOUTUBE_MONITOR = youtube_monitor.YouTubeMonitor(
                youtube_monitor.CloudStore(_get_supabase_sync_config(), SUPABASE_SYNC_BUCKET))
            _YOUTUBE_MONITOR.start()
        except youtube_monitor.MonitorError as exc:
            logger.warning('[YouTubeMonitor] %s', exc)

    # Chỉ bật cloudflare tunnel trên máy tính local, không bật khi chạy trên cloud (Render/VPS)
    if not _is_cloud_runtime():
        t = threading.Thread(target=start_cloudflare_tunnel, args=(int(os.getenv("PORT", 8000)),), daemon=True)
        t.start()

    def _peer_sync_loop():
        time.sleep(2.5)
        while True:
            _sync_two_way_with_peer_sync(push_after_pull=True)
            time.sleep(PEER_SYNC_INTERVAL_SEC)

    threading.Thread(target=_peer_sync_loop, daemon=True).start()


@app.on_event('shutdown')
def stop_youtube_monitor():
    if _YOUTUBE_MONITOR:
        _YOUTUBE_MONITOR.stop()


@app.api_route('/api/youtube-monitor/{action:path}', methods=['GET', 'POST'])
async def api_youtube_monitor(req: Request, action: str):
    user = _extract_vip_user(req)
    # PC validates the admin's current session, then delegates to Render.
    if not user and _is_cloud_runtime():
        trusted = req.headers.get('x-sync-token', '')
        if trusted and hmac.compare_digest(trusted, _get_sync_token()):
            username = req.headers.get('x-youtube-admin', '').strip().lower()
            with _VIP_LOCK:
                candidate = _load_vip_users_unlocked().get(username)
            if candidate and candidate.get('active', True):
                user = candidate
    if not user or user.get('role') != 'admin':
        return JSONResponse(status_code=403, content={'success': False, 'error': 'Chỉ admin được dùng Theo Dõi YouTube.'})
    if action not in ('state', 'settings', 'add', 'toggle', 'delete', 'check', 'test-telegram', 'test-discord'):
        return JSONResponse(status_code=404, content={'success': False, 'error': 'Không tìm thấy thao tác.'})
    if (action == 'state') != (req.method == 'GET'):
        return JSONResponse(status_code=405, content={'success': False, 'error': 'Phương thức không hợp lệ.'})
    body = {}
    if req.method == 'POST':
        raw = await req.body()
        if len(raw) > 20000:
            return JSONResponse(status_code=413, content={'success': False, 'error': 'Dữ liệu quá lớn.'})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse(status_code=400, content={'success': False, 'error': 'Dữ liệu không hợp lệ.'})
    if not _is_cloud_runtime():
        def forward():
            try:
                response = requests.request(req.method, RENDER_SYNC_BASE + '/api/youtube-monitor/' + action,
                    headers={'X-Sync-Token': _get_sync_token(), 'X-YouTube-Admin': user['username']},
                    json=body if req.method == 'POST' else None, timeout=65)
                return JSONResponse(status_code=response.status_code, content=response.json())
            except (requests.RequestException, ValueError):
                return JSONResponse(status_code=503, content={'success': False,
                    'error': 'Chưa kết nối được bộ theo dõi trên Render. Thử lại sau; PC không chạy thay để tránh báo trùng.'})
        return await run_in_threadpool(forward)
    if not _YOUTUBE_MONITOR:
        return JSONResponse(status_code=503, content={'success': False, 'error': 'Bộ theo dõi cloud chưa sẵn sàng.'})
    def execute():
        try:
            if action == 'state':
                result = _YOUTUBE_MONITOR.view()
            elif action.startswith('test-'):
                result = _YOUTUBE_MONITOR.test(action[5:])
            else:
                result = _YOUTUBE_MONITOR.action(action, body)
            return JSONResponse(content=result, headers={'Cache-Control': 'no-store'})
        except (youtube_monitor.MonitorError, ValueError, TypeError) as exc:
            message = str(exc) if isinstance(exc, youtube_monitor.MonitorError) else 'Giá trị cấu hình không hợp lệ.'
            return JSONResponse(status_code=400, content={'success': False, 'error': message})
    return await run_in_threadpool(execute)

@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon():
    fav = STATIC_DIR / "favicon.ico"
    if fav.exists():
        return FileResponse(fav, media_type="image/x-icon")
    return Response(status_code=404)

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>File templates/index.html chưa được tạo.</h1>", status_code=404)
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )

@app.get("/api/tunnel")
def get_tunnel_info():
    """Trả về tên miền công khai hiện tại."""
    return {
        "status": tunnel_state["status"],
        "url": tunnel_state["url"],
        "local_url": "http://localhost:8000",
        "error": tunnel_state["error"]
    }

@app.post("/api/vip/login")
async def api_vip_login(req: Request):
    """Đăng nhập tài khoản VIP hoặc Admin để mở khóa quét trùng lặp Group ID & tìm Video Gốc."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    uname = str(body.get("username") or "").strip().lower()
    pwd = str(body.get("password") or "")
    if not uname or not pwd:
        return JSONResponse(status_code=400, content={"success": False, "error": "Vui lòng nhập đầy đủ Tài khoản và Mật khẩu!"})

    dev_id, dev_label, client_ip = _extract_device_info(req, body)

    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        u_obj = users.get(uname)
        if not u_obj or not _verify_password(pwd, str(u_obj.get("password_hash") or "")):
            return JSONResponse(status_code=401, content={"success": False, "error": "Sai tên tài khoản hoặc mật khẩu!"})
        if not u_obj.get("active", True):
            return JSONResponse(status_code=403, content={"success": False, "error": "Tài khoản này đã bị tạm khóa."})
        u_obj["password_plain"] = pwd
        dev_err = _record_and_check_device_unlocked(u_obj, dev_id, dev_label, client_ip, bmap)
        _save_vip_users_unlocked(users)
        if dev_err:
            return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": dev_err})
        tok = _create_vip_token(uname, int(u_obj.get("token_ver") or 1))
        pub = _public_vip_user(u_obj, bmap)

    return JSONResponse(content={"success": True, "token": tok, "user": pub})


@app.post("/api/vip/register")
async def api_vip_register(req: Request):
    """Khách tự đăng ký tài khoản mới (mặc định hạng Thường Free 0đ, chờ Admin duyệt bật VIP)."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    raw_u = str(body.get("username") or "").strip().lower()
    uname = re.sub(r"[^a-z0-9_.-]", "", raw_u)
    pwd = str(body.get("password") or "")
    note = str(body.get("note") or "").strip()[:100]
    dev_id, dev_label, client_ip = _extract_device_info(req, body)

    if len(uname) < 3 or uname != raw_u:
        return JSONResponse(status_code=400, content={"success": False, "error": "Tên tài khoản từ 3 ký tự trở lên (chỉ dùng chữ thường, số, dấu chấm hoặc gạch dưới)."})
    if len(pwd) < 6:
        return JSONResponse(status_code=400, content={"success": False, "error": "Mật khẩu phải từ 6 ký tự trở lên."})

    with _VIP_LOCK:
        bmap = _load_blocked_devices_unlocked()
        if dev_id in bmap:
            return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": f"⛔ Mã máy {dev_id} đã bị Quản trị viên từ chối đăng ký!"})
        users = _load_vip_users_unlocked()
        if uname in users:
            return JSONResponse(status_code=400, content={"success": False, "error": "Tên tài khoản này đã có người đăng ký! Vui lòng chọn tên khác."})
        now_ts = int(time.time())
        new_rec = {
            "username": uname,
            "password_hash": _hash_password(pwd),
            "password_plain": pwd,
            "role": "member",
            "active": True,
            "expires_at": 0,
            "note": note or "Tự đăng ký trên web",
            "scan_count": 0,
            "account_balance_vnd": 0,
            "account_balance_updated_at": 0,
            "account_check_count": 0,
            "created_at": now_ts,
            "updated_at": now_ts,
            "token_ver": 1,
            "max_devices": _default_max_devices_for_user(uname),
            "devices": {
                dev_id: {
                    "id": dev_id,
                    "label": dev_label,
                    "ip": client_ip,
                    "last_seen": now_ts,
                    "blocked": False,
                }
            },
        }
        users[uname] = new_rec
        meta = _load_sync_meta_unlocked()
        if uname in meta["deleted_users"]:
            meta["deleted_users"].pop(uname, None)
            _save_sync_meta_unlocked(meta)
        _save_vip_users_unlocked(users)
        tok = _create_vip_token(uname, 1)
        pub = _public_vip_user(new_rec, bmap)

    init_user_sheet_async(uname, role="member", note=new_rec["note"])
    return JSONResponse(content={"success": True, "token": tok, "user": pub})


@app.get("/api/vip/me")
async def api_vip_me(req: Request):
    dev_id, dev_label, client_ip = _extract_device_info(req)
    u_obj = _extract_vip_user(req)
    if not u_obj:
        return JSONResponse(content={
            "success": True,
            "authenticated": False,
            "user": None,
            "free_quota": _get_free_quota_status(dev_id),
        })

    pending_code = 0
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        cur = users.get(u_obj["username"])
        if not cur:
            return JSONResponse(content={
                "success": True,
                "authenticated": False,
                "user": None,
                "free_quota": _get_free_quota_status(dev_id),
            })
        dev_err = _record_and_check_device_unlocked(cur, dev_id, dev_label, client_ip, bmap)
        _save_vip_users_unlocked(users)
        if dev_err:
            return JSONResponse(content={"success": True, "authenticated": False, "device_blocked": True, "error": dev_err, "user": None})
        pub = _public_vip_user(cur, bmap)
        has_vip = _has_vip_privilege(cur)
        is_adm = str(cur.get("role") or "").lower() == "admin"
        if not is_adm:
            now_ts = int(time.time())
            orders = _load_vip_orders_unlocked()
            cands = [
                o for o in orders.values()
                if isinstance(o, dict)
                and str(o.get("username") or "").lower() == cur["username"]
                and str(o.get("status") or "").upper() == "PENDING"
                and (now_ts - int(o.get("created_at") or 0)) < 86400
            ]
            if cands:
                cands.sort(key=lambda x: -int(x.get("created_at") or 0))
                pending_code = int(cands[0].get("order_code") or 0)

    if pending_code:
        cfg = _get_payos_config()
        pay_data = await asyncio.to_thread(_query_payos_order_sync, pending_code, cfg)
        if pay_data and str(pay_data.get("status") or "").upper() == "PAID":
            amt_paid = int(pay_data.get("amountPaid") or pay_data.get("amount") or 0)
            newly_paid = False
            sheet_note = ""
            with _VIP_LOCK:
                orders = _load_vip_orders_unlocked()
                users = _load_vip_users_unlocked()
                res_ful = _fulfill_vip_order_unlocked(orders, users, pending_code, amt_paid)
                if res_ful.get("ok") and res_ful.get("user"):
                    newly_paid = bool(res_ful.get("newly_paid"))
                    cur_u = res_ful["user"]
                    sheet_note = str(cur_u.get("note") or "")
                    pub = _public_vip_user(cur_u, _load_blocked_devices_unlocked())
                    has_vip = _has_vip_privilege(cur_u)
            if newly_paid:
                init_user_sheet_async(pub["username"], role=pub["role"], note=sheet_note)

    resp_payload: Dict[str, Any] = {"success": True, "authenticated": True, "user": pub}
    if not has_vip:
        resp_payload["free_quota"] = _get_free_quota_status(dev_id, pub.get("username") or "")
    return JSONResponse(content=resp_payload)


@app.post("/api/vip/change-password")
async def api_vip_change_password(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})
    u_obj = _extract_vip_user(req, body)
    if not u_obj:
        return JSONResponse(status_code=401, content={"success": False, "error": "Phiên đăng nhập không hợp lệ."})
    old_pwd = str(body.get("old_password") or "")
    new_pwd = str(body.get("new_password") or "")
    if len(new_pwd) < 6:
        return JSONResponse(status_code=400, content={"success": False, "error": "Mật khẩu mới phải từ 6 ký tự trở lên."})
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        cur = users.get(u_obj["username"])
        if not cur or not _verify_password(old_pwd, str(cur.get("password_hash") or "")):
            return JSONResponse(status_code=400, content={"success": False, "error": "Mật khẩu hiện tại không đúng!"})
        cur["password_hash"] = _hash_password(new_pwd)
        cur["password_plain"] = new_pwd
        cur["token_ver"] = int(cur.get("token_ver") or 1) + 1
        cur["updated_at"] = int(time.time())
        _save_vip_users_unlocked(users)
        new_tok = _create_vip_token(cur["username"], cur["token_ver"])
    return JSONResponse(content={"success": True, "token": new_tok, "message": "Đã đổi mật khẩu thành công!"})


@app.post("/api/vip/payos/create-order")
async def api_vip_payos_create_order(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    u_obj = _extract_vip_user(req, body)
    if not u_obj:
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng ký hoặc đăng nhập tài khoản trước khi lấy mã QR thanh toán."})

    cfg = _get_payos_config()
    if not cfg.get("configured"):
        return JSONResponse(status_code=503, content={"success": False, "error": "Hệ thống thanh toán PayOS chưa được cấu hình. Vui lòng liên hệ Admin qua Zalo/Facebook!"})

    uname = str(u_obj["username"]).strip().lower()
    if str(u_obj.get("role") or "").lower() == "admin":
        return JSONResponse(status_code=400, content={"success": False, "error": "Tài khoản Admin không cần thanh toán."})

    order_type = str(body.get('order_type') or '').strip().lower()
    if order_type not in ('vip', 'account'):
        return JSONResponse(status_code=400, content={'success': False, 'error': 'Chọn thanh toán VIP hoặc nạp ví Check tài khoản. Vui lòng tải lại trang nếu đang dùng giao diện cũ.'})
    if order_type == 'vip' and u_obj.get('role') == 'vip' and int(u_obj.get('expires_at') or 0) <= 0:
        return JSONResponse(status_code=400, content={'success': False, 'error': 'Tài khoản đã có VIP vĩnh viễn; không cần mua thêm VIP.'})
    base_price = max(2000, int(cfg["price_vnd"]))
    base_days = max(1, int(cfg["duration_days"]))
    minimum_amount = base_price if order_type == 'vip' else 50000
    raw_amt = body.get("amount")
    if raw_amt is None or str(raw_amt).strip() == "":
        amount = minimum_amount
    else:
        try:
            amount = int(raw_amt)
        except Exception:
            amount = 0
    if amount < minimum_amount or amount > 50_000_000:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"Số tiền nạp tối thiểu là {minimum_amount:,}đ (tối đa 50,000,000đ)!"},
        )
    if order_type == 'vip' and amount != base_price:
        return JSONResponse(status_code=400, content={'success': False, 'error': f'Gói VIP có giá {base_price:,}đ / {base_days} ngày. Nạp ví Check tài khoản là khoản riêng.'})
    days = base_days if order_type == 'vip' else 0

    force_new = bool(body.get("force_new", False))
    now_ts = int(time.time())

    # Tái sử dụng đơn PENDING còn hạn (< 10 phút) để tránh tạo nhiều link dư thừa trên PayOS
    reuse_ord: Optional[Dict[str, Any]] = None
    if not force_new:
        with _VIP_LOCK:
            orders = _load_vip_orders_unlocked()
            cands = [
                o for o in orders.values()
                if isinstance(o, dict)
                and str(o.get("username") or "").lower() == uname
                and str(o.get("order_type") or "vip").strip().lower() == order_type
                and o.get('billing_policy') == 'split_v1'
                and str(o.get("status") or "").upper() == "PENDING"
                and int(o.get("amount") or 0) == amount
                and int(o.get("duration_days") or 0) == days
                and int(o.get("expired_at") or 0) > now_ts + 90
                and str(o.get("qr_code") or "").strip()
            ]
            if cands:
                cands.sort(key=lambda x: -int(x.get("created_at") or 0))
                reuse_ord = dict(cands[0])

    if reuse_ord:
        ocode = int(reuse_ord["order_code"])
        pay_data = await asyncio.to_thread(_query_payos_order_sync, ocode, cfg)
        if pay_data:
            p_status = str(pay_data.get("status") or "").upper()
            if p_status == "PAID":
                amt_paid = int(pay_data.get("amountPaid") or pay_data.get("amount") or amount)
                newly_paid = False
                pub_user = None
                sheet_note = ""
                with _VIP_LOCK:
                    orders = _load_vip_orders_unlocked()
                    users = _load_vip_users_unlocked()
                    bmap = _load_blocked_devices_unlocked()
                    res_f = _fulfill_vip_order_unlocked(orders, users, ocode, amt_paid)
                    if res_f.get("ok") and res_f.get("user"):
                        newly_paid = bool(res_f.get("newly_paid"))
                        sheet_note = str(res_f["user"].get("note") or "")
                        pub_user = _public_vip_user(res_f["user"], bmap)
                        reuse_ord = dict(res_f["order"])
                if newly_paid and pub_user:
                    init_user_sheet_async(pub_user["username"], role=pub_user["role"], note=sheet_note)
                return JSONResponse(content={
                    "success": True,
                    "paid": True,
                    "order": _public_order_view(reuse_ord),
                    "user": pub_user,
                    "message": "🎉 Đơn trước đó đã thanh toán xong! Quyền lợi của đơn đã được cập nhật.",
                })
            if p_status == "PENDING":
                return JSONResponse(content={"success": True, "paid": False, "reused": True, "order": _public_order_view(reuse_ord)})
            # Nếu đã bị hủy / hết hạn trên PayOS thì đánh dấu và tạo đơn mới
            with _VIP_LOCK:
                orders = _load_vip_orders_unlocked()
                if str(ocode) in orders:
                    orders[str(ocode)]["status"] = p_status
                    _save_vip_orders_unlocked(orders)

    with _VIP_LOCK:
        orders = _load_vip_orders_unlocked()
        while True:
            cand_code = int(f"88{int(time.time()) % 100000000:08d}{secrets.randbelow(100):02d}")
            if str(cand_code) not in orders:
                order_code = cand_code
                break

    base_url = _public_base_url_from_request(req)
    return_url = f"{base_url}/payos/return?orderCode={order_code}"
    cancel_url = f"{base_url}/payos/cancel?orderCode={order_code}"
    desc = f"{'VIP' if order_type == 'vip' else 'ACC'}{order_code}"
    item_name = (f"VIP Check trung {days} ngay - {uname}" if order_type == 'vip' else f"Nap vi Check tai khoan {amount:,}d - {uname}")[:75]
    exp_ts = now_ts + 900
    sign_payload = {
        "amount": amount,
        "cancelUrl": cancel_url,
        "description": desc,
        "orderCode": order_code,
        "returnUrl": return_url,
    }
    payload = {
        "orderCode": order_code,
        "amount": amount,
        "description": desc,
        "returnUrl": return_url,
        "cancelUrl": cancel_url,
        "expiredAt": exp_ts,
        "items": [{"name": item_name, "quantity": 1, "price": amount}],
        "signature": _payos_sign_data(sign_payload, cfg["checksum_key"]),
    }

    try:
        pay_res = await asyncio.to_thread(_create_payos_payment_sync, payload, cfg)
    except Exception as exc:
        logger.error(f"[PayOS] Lỗi tạo đơn QR ({order_type}) cho {uname}: {exc}")
        return JSONResponse(status_code=502, content={"success": False, "error": f"Không tạo được mã QR PayOS: {exc}"})

    qr_str = str(pay_res.get("qrCode") or "").strip()
    bank_bin = str(pay_res.get("bin") or "").strip()
    qr_uri = _build_qr_data_uri(qr_str)
    ord_rec = {
        "order_code": order_code,
        "order_type": order_type,
        "billing_policy": "split_v1",
        "username": uname,
        "amount": amount,
        "duration_days": days,
        "description": str(pay_res.get("description") or desc),
        "status": "PENDING",
        "qr_code": qr_str,
        "qr_data_uri": qr_uri,
        "checkout_url": str(pay_res.get("checkoutUrl") or ""),
        "bank_bin": bank_bin,
        "bank_name": PAYOS_BANK_NAMES.get(bank_bin, f"Ngân hàng ({bank_bin})" if bank_bin else "Ngân hàng"),
        "account_number": str(pay_res.get("accountNumber") or ""),
        "account_name": str(pay_res.get("accountName") or ""),
        "created_at": now_ts,
        "expired_at": int(pay_res.get("expiredAt") or exp_ts),
        "paid_at": 0,
    }
    with _VIP_LOCK:
        orders = _load_vip_orders_unlocked()
        orders[str(order_code)] = ord_rec
        _save_vip_orders_unlocked(orders)

    return JSONResponse(content={"success": True, "paid": False, "order": _public_order_view(ord_rec)})


@app.post("/api/vip/payos/check-order")
async def api_vip_payos_check_order(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    u_obj = _extract_vip_user(req, body)
    if not u_obj:
        return JSONResponse(status_code=401, content={"success": False, "error": "Phiên đăng nhập không hợp lệ."})

    uname = str(u_obj["username"]).strip().lower()
    raw_code = body.get("order_code")

    with _VIP_LOCK:
        orders = _load_vip_orders_unlocked()
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        ord_rec = None
        if raw_code:
            cand = orders.get(str(int(raw_code)))
            if isinstance(cand, dict) and (str(cand.get("username") or "").lower() == uname or u_obj.get("role") == "admin"):
                ord_rec = dict(cand)
        else:
            cands = [o for o in orders.values() if isinstance(o, dict) and str(o.get("username") or "").lower() == uname]
            if cands:
                cands.sort(key=lambda x: -int(x.get("created_at") or 0))
                ord_rec = dict(cands[0])

        if not ord_rec:
            return JSONResponse(status_code=404, content={"success": False, "error": "Không tìm thấy đơn thanh toán VIP nào."})

        cur_user = users.get(str(ord_rec.get("username") or uname))
        if str(ord_rec.get("status") or "").upper() == "PAID":
            pub_u = _public_vip_user(cur_user, bmap) if isinstance(cur_user, dict) else None
            return JSONResponse(content={
                "success": True,
                "paid": True,
                "status": "PAID",
                "order": _public_order_view(ord_rec),
                "user": pub_u,
            })

    cfg = _get_payos_config()
    if not cfg.get("configured"):
        return JSONResponse(status_code=503, content={"success": False, "error": "PayOS chưa được cấu hình."})

    ocode = int(ord_rec["order_code"])
    pay_data = await asyncio.to_thread(_query_payos_order_sync, ocode, cfg)
    if not pay_data:
        return JSONResponse(content={"success": True, "paid": False, "status": str(ord_rec.get("status") or "PENDING"), "order": _public_order_view(ord_rec)})

    p_status = str(pay_data.get("status") or "PENDING").upper()
    amt_paid = int(pay_data.get("amountPaid") or 0)
    req_amt = int(ord_rec.get("amount") or 0)

    if p_status == "PAID" and amt_paid >= req_amt:
        newly_paid = False
        pub_u = None
        sheet_note = ""
        updated_ord = ord_rec
        with _VIP_LOCK:
            orders = _load_vip_orders_unlocked()
            users = _load_vip_users_unlocked()
            bmap = _load_blocked_devices_unlocked()
            res_f = _fulfill_vip_order_unlocked(orders, users, ocode, amt_paid)
            if res_f.get("ok") and res_f.get("user"):
                newly_paid = bool(res_f.get("newly_paid"))
                sheet_note = str(res_f["user"].get("note") or "")
                pub_u = _public_vip_user(res_f["user"], bmap)
                updated_ord = dict(res_f["order"])
        if newly_paid and pub_u:
            init_user_sheet_async(pub_u["username"], role=pub_u["role"], note=sheet_note)
        return JSONResponse(content={
            "success": True,
            "paid": True,
            "status": "PAID",
            "order": _public_order_view(updated_ord),
            "user": pub_u,
        })

    if p_status in ("CANCELLED", "EXPIRED") and str(ord_rec.get("status") or "").upper() != p_status:
        with _VIP_LOCK:
            orders = _load_vip_orders_unlocked()
            if str(ocode) in orders:
                orders[str(ocode)]["status"] = p_status
                _save_vip_orders_unlocked(orders)
                ord_rec = dict(orders[str(ocode)])

    return JSONResponse(content={
        "success": True,
        "paid": False,
        "status": p_status,
        "order": _public_order_view(ord_rec),
    })


@app.post("/api/vip/payos/webhook")
async def api_vip_payos_webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "invalid_json"})

    cfg = _get_payos_config()
    if not cfg.get("configured"):
        return JSONResponse(status_code=503, content={"success": False, "error": "payos_not_configured"})

    data = body.get("data") if isinstance(body, dict) else None
    signature = str(body.get("signature") or "") if isinstance(body, dict) else ""
    if not isinstance(data, dict) or not _payos_verify_webhook(data, signature, cfg["checksum_key"]):
        return JSONResponse(status_code=400, content={"success": False, "error": "invalid_signature"})

    raw_code = data.get("orderCode") or data.get("order_code") or 0
    if not raw_code:
        desc = str(data.get("description") or data.get("desc") or "")
        digits = "".join(ch for ch in desc if ch.isdigit())
        raw_code = int(digits) if digits else 0

    try:
        order_code = int(raw_code)
    except Exception:
        order_code = 0

    if not order_code:
        return JSONResponse(content={"success": True, "verified": True})

    amt_paid = 0
    for k_amt in ("amount", "transferAmount", "creditAmount"):
        if data.get(k_amt) is not None:
            try:
                amt_paid = int(float(data.get(k_amt)))
                break
            except Exception:
                pass

    newly_paid = False
    act_user = None
    sheet_note = ""
    with _VIP_LOCK:
        orders = _load_vip_orders_unlocked()
        if str(order_code) not in orders:
            return JSONResponse(content={"success": True, "verified": True, "note": "order_not_web_vip"})
        users = _load_vip_users_unlocked()
        res_f = _fulfill_vip_order_unlocked(orders, users, order_code, amt_paid)
        if res_f.get("ok") and res_f.get("user"):
            newly_paid = bool(res_f.get("newly_paid"))
            sheet_note = str(res_f["user"].get("note") or "")
            act_user = res_f["user"].get("username")
            act_role = res_f["user"].get("role", "vip")
        elif res_f.get("reason") == "insufficient_amount":
            return JSONResponse(status_code=400, content={"success": False, "error": "insufficient_amount"})

    if newly_paid and act_user:
        init_user_sheet_async(act_user, role=act_role, note=sheet_note)
    return JSONResponse(content={"success": True, "activated": bool(newly_paid), "order_code": order_code})


@app.get("/payos/return")
async def payos_return_redirect(orderCode: str = "", cancel: str = ""):
    code_clean = re.sub(r"[^0-9]", "", str(orderCode or ""))[:20]
    st = "cancel" if str(cancel).lower() == "true" else "return"
    return RedirectResponse(url=f"/?payos_order={code_clean}&payos_status={st}" if code_clean else "/")


@app.get("/payos/cancel")
async def payos_cancel_redirect(orderCode: str = ""):
    code_clean = re.sub(r"[^0-9]", "", str(orderCode or ""))[:20]
    return RedirectResponse(url=f"/?payos_order={code_clean}&payos_status=cancel" if code_clean else "/")


@app.get("/api/vip/admin/payos-config")
async def api_vip_admin_get_payos_config(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền xem cấu hình PayOS."})
    cfg = _get_payos_config()
    cid = cfg["client_id"]
    cid_masked = (cid[:4] + "..." + cid[-4:]) if len(cid) > 8 else ("Đã đặt" if cid else "")
    return JSONResponse(content={
        "success": True,
        "configured": cfg["configured"],
        "client_id_masked": cid_masked,
        "price_vnd": cfg["price_vnd"],
        "duration_days": cfg["duration_days"],
        "webhook_url": f"{_public_base_url_from_request(req)}/api/vip/payos/webhook",
    })


@app.post("/api/vip/admin/payos-config")
async def api_vip_admin_save_payos_config(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền lưu cấu hình PayOS."})
    try:
        body = await req.json()
    except Exception:
        body = {}

    with _VIP_LOCK:
        cur = _load_payos_file_config_unlocked()
        if body.get("price_vnd") is not None and str(body.get("price_vnd")).strip() != "":
            cur["price_vnd"] = max(2000, int(body.get("price_vnd")))
        if body.get("duration_days") is not None and str(body.get("duration_days")).strip() != "":
            cur["duration_days"] = max(1, int(body.get("duration_days")))
        for k_name in ("client_id", "api_key", "checksum_key"):
            val = str(body.get(k_name) or "").strip()
            if val:
                cur[k_name] = val
        cur["updated_at"] = int(time.time())
        _save_payos_file_config_unlocked(cur)

    if cur.get("price_vnd"):
        _upsert_local_env_key("VIP_PRICE_VND", str(cur["price_vnd"]))
    if cur.get("duration_days"):
        _upsert_local_env_key("VIP_DURATION_DAYS", str(cur["duration_days"]))
    if str(body.get("client_id") or "").strip():
        _upsert_local_env_key("PAYOS_CLIENT_ID", str(body.get("client_id")).strip())
    if str(body.get("api_key") or "").strip():
        _upsert_local_env_key("PAYOS_API_KEY", str(body.get("api_key")).strip())
    if str(body.get("checksum_key") or "").strip():
        _upsert_local_env_key("PAYOS_CHECKSUM_KEY", str(body.get("checksum_key")).strip())

    cfg = _get_payos_config()
    return JSONResponse(content={
        "success": True,
        "configured": cfg["configured"],
        "price_vnd": cfg["price_vnd"],
        "duration_days": cfg["duration_days"],
        "message": f"✅ Đã lưu cấu hình PayOS ({cfg['price_vnd']:,}đ / {cfg['duration_days']} ngày)!",
    })


_TIKHUB_USD_TO_VND = 25500
from api_balance_cache import ApiBalanceCache
_TIKHUB_BAL_READER = ApiBalanceCache(BASE_DIR / 'independent_reader/.private/api-balance-last-good.json', _TIKHUB_USD_TO_VND)


def _get_tikhub_live_balance(force_refresh: bool = False) -> Dict[str, Any]:
    return _TIKHUB_BAL_READER.get(core_dedup._get_tikhub_api_key(), force=force_refresh)


@app.get("/api/vip/admin/api-balance")
async def api_vip_admin_balance(req: Request, refresh: int = Query(0)):
    u_obj = _extract_vip_user(req)
    if not u_obj or str(u_obj.get("role") or "").lower() != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới xem được số dư ví API hệ thống."})
    import asyncio
    info = await asyncio.to_thread(_get_tikhub_live_balance, bool(refresh))
    return JSONResponse(status_code=200 if info["balance_known"] else 503, content={"success": info["balance_known"], "billing": info})


def _require_reader_admin(req: Request):
    user = _extract_vip_user(req)
    if not user or str(user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Chỉ Admin được quản lý phiên đọc TikTok.")


@app.get("/api/vip/admin/reader-status")
async def api_reader_status(req: Request):
    _require_reader_admin(req)
    return await asyncio.to_thread(dedup_provider.ROUTER.status)


@app.post("/api/vip/admin/reader-settings")
async def api_reader_settings(req: Request):
    _require_reader_admin(req)
    try:
        settings = dedup_provider.ROUTER.save_settings(await req.json())
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Cấu hình reader không hợp lệ.")
    return {"success": True, "settings": settings}


@app.post("/api/vip/admin/reader-refresh")
async def api_reader_refresh(req: Request):
    _require_reader_admin(req)
    body = await req.json()
    try:
        return await asyncio.to_thread(dedup_provider.ROUTER.refresh, bool(body.get("check_only")))
    except Exception:
        return JSONResponse(status_code=503, content={"success": False,
            "error": "Chưa kết nối được reader trên PC. Mở MuMu trên PC rồi thử lại. Cloud dùng API dự phòng."})


@app.get("/api/vip/admin/users")
async def api_vip_admin_list_users(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền quản lý tài khoản VIP."})
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        enforce_device_limit = bool(_load_sync_meta_unlocked().get("enforce_device_limit", True))
        out = [_public_vip_user(v, bmap, include_password=True) for v in users.values() if isinstance(v, dict)]
    out.sort(key=lambda x: (0 if x["role"] == "admin" else 1, -x["created_at"]))
    return JSONResponse(content={"success": True, "users": out, "enforce_device_limit": enforce_device_limit})


@app.post("/api/vip/admin/device-limit")
async def api_vip_admin_toggle_device_limit(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    u_obj = _extract_vip_user(req, body)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền chỉnh giới hạn đăng nhập mã máy."})

    with _VIP_LOCK:
        meta = _load_sync_meta_unlocked()
        if "enforce_device_limit" in body:
            new_val = bool(body.get("enforce_device_limit"))
        else:
            new_val = not bool(meta.get("enforce_device_limit", True))
        now_ts = int(time.time())
        meta["enforce_device_limit"] = new_val
        meta["enforce_device_limit_updated_at"] = now_ts
        _save_sync_meta_unlocked(meta)

        if not new_val:
            users = _load_vip_users_unlocked()
            bmap = _load_blocked_devices_unlocked()
            users_changed = False
            for u_rec in users.values():
                if not isinstance(u_rec, dict):
                    continue
                devs = u_rec.get("devices")
                if not isinstance(devs, dict):
                    continue
                rec_changed = False
                for did, d_entry in devs.items():
                    if (
                        isinstance(d_entry, dict)
                        and d_entry.get("blocked")
                        and d_entry.get("auto_limit")
                        and did not in bmap
                    ):
                        d_entry["blocked"] = False
                        d_entry.pop("auto_limit", None)
                        rec_changed = True
                if rec_changed:
                    u_rec["updated_at"] = now_ts
                    users_changed = True
            if users_changed:
                _save_vip_users_unlocked(users)
        _schedule_peer_sync_push()

    msg = (
        "🔒 Đã BẬT giới hạn đăng nhập: mỗi tài khoản giới hạn 1 mã máy!"
        if new_val
        else "🔓 Đã TẮT giới hạn đăng nhập: cho phép đăng nhập thoải mái mã máy (vẫn ghi lại đủ mọi mã máy & IP)."
    )
    return JSONResponse(content={
        "success": True,
        "enforce_device_limit": new_val,
        "message": msg,
    })


@app.get("/api/vip/admin/sheet-script")
async def api_vip_admin_sheet_script(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền xem script Google Sheet."})
    gs_file = BASE_DIR / "google_sheet_apps_script.gs"
    if not gs_file.exists():
        return JSONResponse(status_code=404, content={"success": False, "error": "Không tìm thấy file google_sheet_apps_script.gs"})
    return JSONResponse(content={
        "success": True,
        "script": gs_file.read_text(encoding="utf-8"),
        "sheet_id": "1HRGpokdDkzyHFCvJBDishlkR7HaVp6BgyBJPxNCW2A0",
        "split_free_by_ip": _load_sheet_config().get("split_free_by_ip", True),
    })


def _upsert_local_env_key(key: str, val: str) -> None:
    """Cập nhật hoặc thêm biến key=val vào file .env local mà không làm hỏng các dòng khác."""
    clean_val = str(val or "").replace("\r", "").replace("\n", "").strip()
    if not clean_val or os.getenv("IS_CLOUD") or os.getenv("RENDER"):
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines() if ENV_FILE.exists() else []
        out_lines = []
        replaced = False
        for ln in lines:
            stripped = ln.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped and stripped.split("=", 1)[0].strip() == key:
                out_lines.append(f"{key}={clean_val}")
                replaced = True
            else:
                out_lines.append(ln)
        if not replaced:
            out_lines.append(f"{key}={clean_val}")
        ENV_FILE.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.error(f"[RenderSync] Không lưu được {key} vào .env: {e}")


def _ensure_github_deploy_webhook(hook_url: str) -> bool:
    """Tự động gắn Render Deploy Hook vào GitHub Webhooks để mỗi lần push là Render tự cập nhật 24/7."""
    try:
        rem = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=str(BASE_DIR), text=True, timeout=5).strip()
        m = re.match(r"^https://([^@]+)@github\.com/([^/]+/[^/.]+?)(?:\.git)?$", rem)
        if not m:
            return False
        gh_tok, repo_slug = m.group(1), m.group(2)
        hdrs = {"Authorization": f"token {gh_tok}", "Accept": "application/vnd.github+json"}
        hook_base = hook_url.split("?")[0]
        r_list = requests.get(f"https://api.github.com/repos/{repo_slug}/hooks", headers=hdrs, timeout=10)
        if r_list.status_code != 200:
            return False
        hooks = r_list.json() or []
        for h in hooks:
            cfg = h.get("config") or {}
            cur_u = str(cfg.get("url") or "")
            if cur_u == hook_url:
                return True
            if cur_u.split("?")[0] == hook_base and h.get("id"):
                r_patch = requests.patch(
                    f"https://api.github.com/repos/{repo_slug}/hooks/{h['id']}",
                    headers=hdrs,
                    json={"active": True, "events": ["push"], "config": {"url": hook_url, "content_type": "json", "insecure_ssl": "0"}},
                    timeout=10,
                )
                return r_patch.status_code in (200, 201)
        r_add = requests.post(
            f"https://api.github.com/repos/{repo_slug}/hooks",
            headers=hdrs,
            json={"name": "web", "active": True, "events": ["push"], "config": {"url": hook_url, "content_type": "json", "insecure_ssl": "0"}},
            timeout=10,
        )
        return r_add.status_code in (200, 201)
    except Exception:
        return False


@app.get("/api/vip/admin/render-env")
async def api_vip_admin_render_env(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền xem cấu hình Render Cloud."})
    env_map = _read_local_env()
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        seed_b64 = base64.b64encode(json.dumps(users, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
        user_count = len(users)
    tikhub_key = (core_dedup._get_tikhub_api_key() or "").strip()
    adm_u = (os.environ.get("ADMIN_DEFAULT_USER") or env_map.get("ADMIN_DEFAULT_USER") or "admin").strip().lower()
    adm_p = os.environ.get("ADMIN_DEFAULT_PASS") or env_map.get("ADMIN_DEFAULT_PASS") or "Admin@2026"
    vip_sec = _get_vip_secret().decode("utf-8", errors="ignore")
    sheet_url = _get_sheet_webhook_url()
    hook_val = (os.environ.get("RENDER_DEPLOY_HOOK") or env_map.get("RENDER_DEPLOY_HOOK") or "").strip()
    payos_cfg = _get_payos_config()

    lines = [
        "IS_CLOUD=true",
        f"TIKHUB_API_KEY={tikhub_key}",
        f"ADMIN_DEFAULT_USER={adm_u}",
        f"ADMIN_DEFAULT_PASS={adm_p}",
        f"VIP_AUTH_SECRET={vip_sec}",
        f"SHEET_WEBHOOK_URL={sheet_url}",
        f"VIP_USERS_SEED_B64={seed_b64}",
        f"VIP_PRICE_VND={payos_cfg['price_vnd']}",
        f"VIP_DURATION_DAYS={payos_cfg['duration_days']}",
    ]
    if payos_cfg.get("configured"):
        lines.extend([
            f"PAYOS_CLIENT_ID={payos_cfg['client_id']}",
            f"PAYOS_API_KEY={payos_cfg['api_key']}",
            f"PAYOS_CHECKSUM_KEY={payos_cfg['checksum_key']}",
        ])
    sb_cfg = _get_supabase_sync_config()
    if sb_cfg.get("configured"):
        lines.extend([
            f"SUPABASE_SYNC_URL={sb_cfg['url']}",
            f"SUPABASE_SYNC_KEY={sb_cfg['key']}",
        ])
    return JSONResponse(content={
        "success": True,
        "env_text": "\n".join(lines),
        "has_deploy_hook": bool(hook_val),
        "user_count": user_count,
        "peer_sync": dict(_PEER_SYNC_STATUS),
    })


@app.get("/api/internal/sync-state")
async def api_internal_get_sync_state(req: Request):
    x_tok = (req.headers.get("x-sync-token") or "").strip()
    expected = _get_sync_token()
    if not x_tok or not hmac.compare_digest(x_tok, expected):
        return JSONResponse(status_code=403, content={"success": False, "error": "forbidden"})
    x_origin = (req.headers.get("x-sync-origin") or "").strip().lower()
    if _is_cloud_runtime() and x_origin == "render":
        return JSONResponse(content={"success": True, "self_echo": True})
    with _VIP_LOCK:
        snap = _export_sync_snapshot_unlocked()
    return JSONResponse(content={"success": True, "role": "cloud" if _is_cloud_runtime() else "pc", "snapshot": snap})


@app.post("/api/internal/sync-state")
async def api_internal_post_sync_state(req: Request):
    x_tok = (req.headers.get("x-sync-token") or "").strip()
    expected = _get_sync_token()
    if not x_tok or not hmac.compare_digest(x_tok, expected):
        return JSONResponse(status_code=403, content={"success": False, "error": "forbidden"})
    x_origin = (req.headers.get("x-sync-origin") or "").strip().lower()
    if _is_cloud_runtime() and x_origin == "render":
        return JSONResponse(content={"success": True, "self_echo": True})
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "invalid_json"})
    snap = body.get("snapshot") if isinstance(body, dict) and isinstance(body.get("snapshot"), dict) else body
    with _VIP_LOCK:
        res = _merge_sync_state_unlocked(snap if isinstance(snap, dict) else {})
    _PEER_SYNC_STATUS["last_ok_at"] = int(time.time())
    _PEER_SYNC_STATUS["last_error"] = ""
    _PEER_SYNC_STATUS["last_direction"] = "incoming"
    return JSONResponse(content={"success": True, "changed": res["changed"], "user_count": res["user_count"]})


@app.post("/api/vip/admin/sync-render-now")
async def api_vip_admin_sync_render_now(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền đồng bộ đám mây."})
    res = await asyncio.to_thread(_sync_two_way_with_peer_sync, True)
    if not res.get("ok"):
        return JSONResponse(status_code=502, content={"success": False, "error": f"Đồng bộ thất bại: {res.get('error')}", "peer_sync": dict(_PEER_SYNC_STATUS)})
    targets = []
    if res.get("supabase_ok"):
        targets.append("Supabase Cloud")
    if res.get("peer_ok"):
        targets.append("Render")
    target_txt = " & ".join(targets) if targets else "Cloud"
    return JSONResponse(content={
        "success": True,
        "user_count": res.get("user_count", 0),
        "peer_sync": dict(_PEER_SYNC_STATUS),
        "message": f"✅ Đã đồng bộ xong {res.get('user_count', 0)} tài khoản lên {target_txt}!",
    })


@app.post("/api/vip/admin/render-deploy")
async def api_vip_admin_render_deploy(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    u_obj = _extract_vip_user(req, body)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền kích hoạt deploy Render."})

    raw_hook = str(body.get("deploy_hook_url") or "").strip()
    env_map = _read_local_env()
    hook_url = raw_hook or (os.environ.get("RENDER_DEPLOY_HOOK") or env_map.get("RENDER_DEPLOY_HOOK") or "").strip()
    if not hook_url.startswith("https://api.render.com/deploy/srv-"):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Thiếu hoặc sai định dạng Render Deploy Hook! Vào Render -> Settings -> Deploy Hook, copy link https://api.render.com/deploy/srv-... dán vào ô rồi bấm lại.",
            },
        )

    if raw_hook:
        _upsert_local_env_key("RENDER_DEPLOY_HOOK", hook_url)

    def _run_deploy():
        gh_ok = _ensure_github_deploy_webhook(hook_url)
        try:
            resp = requests.post(hook_url, timeout=15)
            code = int(resp.status_code)
        except Exception as exc:
            return False, gh_ok, 0, str(exc)
        return code in (200, 201, 202), gh_ok, code, ""

    import asyncio
    triggered, gh_linked, http_code, req_err = await asyncio.to_thread(_run_deploy)
    if not triggered:
        err_msg = f"Không gọi được Render Deploy Hook ({req_err or f'HTTP {http_code}'}). Hãy kiểm tra lại link Deploy Hook trong Render -> Settings."
        return JSONResponse(status_code=400 if http_code else 502, content={"success": False, "triggered": False, "github_webhook_linked": gh_linked, "error": err_msg})

    msg = "🚀 Đã kích hoạt Render build phiên bản mới (mất ~2-4 phút là xong)!"
    if gh_linked:
        msg += " Đã gắn luôn GitHub Webhook: từ nay mỗi lần đẩy code mới là Render tự động cập nhật theo 24/7!"
    return JSONResponse(content={
        "success": True,
        "triggered": True,
        "github_webhook_linked": gh_linked,
        "message": msg,
    })


@app.post("/api/vip/admin/sheet-config")
async def api_vip_admin_sheet_config(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền chỉnh cấu hình Google Sheet."})
    try:
        body = await req.json()
    except Exception:
        body = {}
    cur = _load_sheet_config()
    if "split_free_by_ip" in body:
        new_val = bool(body.get("split_free_by_ip"))
    else:
        new_val = not bool(cur.get("split_free_by_ip", True))
    saved = _save_sheet_config({"split_free_by_ip": new_val})
    return JSONResponse(content={
        "success": True,
        "split_free_by_ip": saved["split_free_by_ip"],
        "message": (
            "✅ Đã BẬT tách riêng mỗi khách chưa đăng nhập thành 1 Trang tính theo Mã máy (Khách_<MãMáy>)!"
            if saved["split_free_by_ip"]
            else "✅ Đã chuyển về gom chung khách chưa đăng nhập vào 1 tab Khách_Free (vẫn tách dòng theo từng IP & Vị trí IP)."
        ),
    })


@app.post("/api/vip/admin/sync-sheets")
async def api_vip_admin_sync_sheets(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền đồng bộ trang tính."})
    hook_url = _get_sheet_webhook_url()
    if not hook_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "Chưa cấu hình SHEET_WEBHOOK_URL."})

    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        targets = [
            {"account": k, "role": str(v.get("role") or "member"), "note": str(v.get("note") or "")}
            for k, v in users.items()
            if isinstance(v, dict) and k
        ]
    targets.append({"account": "Khách_Free", "role": "guest", "note": "Khách chưa đăng nhập"})

    def _sync_all():
        vn_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%d/%m/%Y %H:%M:%S")
        synced = []
        script_supported = True
        for item in targets:
            acc_tab = "Khách_Free" if item["account"] == "Khách_Free" else _sanitize_sheet_tab(item["account"])
            payload = {
                "action": "init_user_sheet",
                "account": acc_tab,
                "sheet_name": acc_tab,
                "role": item["role"],
                "note": item["note"][:120],
                "time": vn_time,
            }
            try:
                r = requests.post(hook_url, json=payload, timeout=12)
                ok_json = False
                try:
                    j = r.json()
                    ok_json = bool(j.get("ok") and j.get("action") == "init_user_sheet")
                except Exception:
                    pass
                if not ok_json:
                    script_supported = False
                synced.append({"sheet": acc_tab, "http": r.status_code, "created": ok_json})
            except Exception as ex:
                script_supported = False
                synced.append({"sheet": acc_tab, "http": 0, "created": False, "error": str(ex)})
        return synced, script_supported

    import asyncio
    synced_list, script_ok = await asyncio.to_thread(_sync_all)
    return JSONResponse(content={
        "success": True,
        "script_supported": script_ok,
        "synced": synced_list,
        "message": (
            f"✅ Đã đồng bộ xong {len(synced_list)} trang tính theo từng tài khoản!"
            if script_ok
            else "⚠️ Webhook Google Sheet hiện tại vẫn đang chạy code Apps Script cũ! Hãy bấm [Copy Code Apps Script] rồi dán cập nhật lên Google Sheet (Triển khai phiên bản mới) để tự tách trang tính."
        ),
    })


@app.post("/api/vip/admin/device-action")
async def api_vip_admin_device_action(req: Request):
    """Admin khóa / mở khóa một Mã máy cụ thể hoặc reset danh sách thiết bị cho khách đổi máy."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})
    u_obj = _extract_vip_user(req, body)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền khóa/mở thiết bị."})

    target_u = str(body.get("username") or "").strip().lower()
    dev_id = re.sub(r"[^A-Z0-9-]", "", str(body.get("device_id") or "").strip().upper())
    action = str(body.get("action") or "").strip().lower()

    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        rec = users.get(target_u)
        if not isinstance(rec, dict):
            return JSONResponse(status_code=404, content={"success": False, "error": "Không tìm thấy tài khoản này."})
        if rec.get("role") == "admin" and action != "reset_all":
            return JSONResponse(status_code=400, content={"success": False, "error": "Không thể khóa thiết bị của Chủ hệ thống!"})

        devs = rec.setdefault("devices", {})
        now_ts = int(time.time())
        if action == "reset_all":
            rec["devices"] = {}
            rec["devices_reset_at"] = now_ts
            rec["updated_at"] = now_ts
            _save_vip_users_unlocked(users)
            return JSONResponse(content={"success": True, "message": f"Đã reset toàn bộ thiết bị cho {target_u}."})

        if not dev_id:
            return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu mã máy (device_id)."})

        d_entry = devs.get(dev_id) if isinstance(devs.get(dev_id), dict) else {"id": dev_id, "last_seen": now_ts}
        meta = _load_sync_meta_unlocked()
        if action == "block":
            d_entry["blocked"] = True
            d_entry["auto_limit"] = False
            d_entry.pop("admin_unblocked", None)
            d_entry["status_ts"] = now_ts
            devs[dev_id] = d_entry
            rec["updated_at"] = now_ts
            meta["unblocked_devices"].pop(dev_id, None)
            _save_sync_meta_unlocked(meta)
            bmap[dev_id] = {
                "device_id": dev_id,
                "blocked_at": now_ts,
                "username": target_u,
                "ip": d_entry.get("ip", ""),
            }
        elif action == "unblock":
            d_entry["blocked"] = False
            d_entry["admin_unblocked"] = True
            d_entry["status_ts"] = now_ts
            d_entry.pop("auto_limit", None)
            devs[dev_id] = d_entry
            rec["updated_at"] = now_ts
            bmap.pop(dev_id, None)
            meta["unblocked_devices"][dev_id] = now_ts
            _save_sync_meta_unlocked(meta)
        else:
            return JSONResponse(status_code=400, content={"success": False, "error": "Hành động không hợp lệ."})

        _save_vip_users_unlocked(users)
        _save_blocked_devices_unlocked(bmap)

    return JSONResponse(content={"success": True})


@app.post("/api/vip/admin/balance")
async def api_vip_admin_adjust_balance(req: Request):
    """Admin cộng / trừ / đặt số dư ví Check Tài Khoản cho user."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})
    u_obj = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền chỉnh số dư tài khoản."})

    target_u = re.sub(r"[^a-z0-9_.-]", "", str(body.get("username") or "").strip().lower())
    mode = str(body.get("mode") or "add").strip().lower()
    if mode not in ("add", "sub", "set"):
        return JSONResponse(status_code=400, content={"success": False, "error": "Chế độ chỉnh số dư không hợp lệ (add / sub / set)."})
    try:
        amount_vnd = int(body.get("amount_vnd"))
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Số tiền không hợp lệ."})

    if amount_vnd < 0 or (mode in ("add", "sub") and amount_vnd <= 0):
        return JSONResponse(status_code=400, content={"success": False, "error": "Vui lòng nhập số tiền hợp lệ (> 0đ khi cộng/trừ, >= 0đ khi đặt)."})
    if amount_vnd > 1_000_000_000:
        return JSONResponse(status_code=400, content={"success": False, "error": "Số tiền vượt quá giới hạn cho phép."})

    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        cur = users.get(target_u)
        if not isinstance(cur, dict):
            return JSONResponse(status_code=404, content={"success": False, "error": f"Không tìm thấy tài khoản '{target_u}'."})
        old_bal = max(0, int(cur.get("account_balance_vnd") or 0))
        if mode == "add":
            new_bal = old_bal + amount_vnd
        elif mode == "sub":
            new_bal = max(0, old_bal - amount_vnd)
        else:
            new_bal = amount_vnd
        now_ts = int(time.time())
        cur["account_balance_vnd"] = new_bal
        cur["account_balance_updated_at"] = now_ts
        cur["updated_at"] = now_ts
        _save_vip_users_unlocked(users)
        pub_u = _public_vip_user(cur, bmap, include_password=True)

    checks_left = _calc_account_checks_left(new_bal)
    dedup_left = _calc_dedup_checks_left(new_bal)
    return JSONResponse(content={
        "success": True,
        "user": pub_u,
        "message": f"✅ Đã cập nhật số dư @{target_u}: {new_bal:,}đ (~{dedup_left:,} lượt Check Trùng · ~{checks_left:,} lượt Check Tài Khoản)!",
    })


def _parse_admin_expires_date_vn(raw_date: str) -> Optional[int]:
    s = str(raw_date or "").strip()
    if not s:
        return None
    y = m = d = 0
    m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    m_vn = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
    elif m_vn:
        d, m, y = int(m_vn.group(1)), int(m_vn.group(2)), int(m_vn.group(3))
    else:
        return None
    if y < 2000 or y > 2100:
        return None
    try:
        tz_vn = datetime.timezone(datetime.timedelta(hours=7))
        dt = datetime.datetime(y, m, d, 23, 59, 59, tzinfo=tz_vn)
        return int(dt.timestamp())
    except ValueError:
        return None


@app.post("/api/vip/admin/users")
async def api_vip_admin_upsert_user(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})
    u_obj = _extract_vip_user(req, body)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền tạo/sửa tài khoản VIP."})

    target_u = re.sub(r"[^a-z0-9_.-]", "", str(body.get("username") or "").strip().lower())
    pwd = str(body.get("password") or "").strip()
    has_role_param = ("role" in body and body.get("role") is not None and str(body.get("role")).strip() != "")
    raw_role = str(body.get("role") or "vip").lower()
    role = raw_role if raw_role in ("admin", "vip", "member") else "vip"
    note_in = body.get("note")
    active = bool(body.get("active", True))
    days = body.get("days")
    expires_date = body.get("expires_date")
    expires_at_in = body.get("expires_at")
    max_devs_in = body.get("max_devices")

    parsed_exp_date: Optional[int] = None
    if expires_date is not None and str(expires_date).strip() != "":
        parsed_exp_date = _parse_admin_expires_date_vn(str(expires_date))
        if parsed_exp_date is None:
            return JSONResponse(status_code=400, content={"success": False, "error": "Ngày hết hạn không hợp lệ (định dạng YYYY-MM-DD hoặc DD/MM/YYYY)."})

    parsed_exp_ts_in: Optional[int] = None
    if expires_at_in is not None and str(expires_at_in).strip() != "":
        try:
            parsed_exp_ts_in = max(0, int(expires_at_in))
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error": "Timestamp hết hạn không hợp lệ."})

    if not target_u or len(target_u) < 2:
        return JSONResponse(status_code=400, content={"success": False, "error": "Tên tài khoản phải từ 2 ký tự trở lên (chữ/số)."})

    self_new_tok = ""
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        existing = users.get(target_u)
        if (not existing and len(pwd) < 6) or (pwd and len(pwd) < 6):
            return JSONResponse(status_code=400, content={"success": False, "error": "Vui lòng đặt mật khẩu từ 6 ký tự trở lên."})

        eff_role = (role if has_role_param else str(existing.get("role") or "vip").lower()) if existing else role
        exp_ts = int(existing.get("expires_at") or 0) if existing else 0
        if parsed_exp_date is not None and eff_role != "admin":
            eff_role = "vip"
            has_role_param = True
            exp_ts = parsed_exp_date
        elif eff_role == "member":
            exp_ts = 0
        elif parsed_exp_ts_in is not None:
            exp_ts = parsed_exp_ts_in
        elif days is not None and str(days).strip() != "":
            d_val = int(days)
            exp_ts = 0 if d_val <= 0 else int(time.time()) + d_val * 86400

        bmap = _load_blocked_devices_unlocked()
        now_ts = int(time.time())
        if existing:
            if pwd:
                existing["password_hash"] = _hash_password(pwd)
                existing["password_plain"] = pwd
                existing["token_ver"] = int(existing.get("token_ver") or 1) + 1
                if target_u == u_obj["username"]:
                    self_new_tok = _create_vip_token(target_u, existing["token_ver"])
            if target_u != u_obj["username"] and has_role_param:
                existing["role"] = eff_role
                existing["active"] = active
            elif target_u != u_obj["username"] and "active" in body:
                existing["active"] = active
            existing["expires_at"] = exp_ts
            if target_u != u_obj["username"]:
                if has_role_param and eff_role == "member":
                    existing["vip_revoked_at"] = now_ts
                    existing["vip_source"] = "admin_revoked"
                elif eff_role == "vip":
                    existing.pop("vip_revoked_at", None)
                    if parsed_exp_date is not None or parsed_exp_ts_in is not None or (days is not None and str(days).strip() != ""):
                        existing["vip_source"] = "admin"
            if note_in is not None:
                existing["note"] = str(note_in).strip()[:120]
            if target_u in UNLIMITED_DEVICE_USERS:
                existing["max_devices"] = 0
            elif max_devs_in is not None and str(max_devs_in).strip() != "":
                existing["max_devices"] = max(0, int(max_devs_in))
            existing["updated_at"] = now_ts
            saved_pub = _public_vip_user(existing, bmap, include_password=True)
        else:
            m_dev = _default_max_devices_for_user(target_u)
            if target_u in UNLIMITED_DEVICE_USERS:
                m_dev = 0
            elif max_devs_in is not None and str(max_devs_in).strip() != "":
                m_dev = max(0, int(max_devs_in))
            new_rec = {
                "username": target_u,
                "password_hash": _hash_password(pwd),
                "password_plain": pwd,
                "role": eff_role,
                "active": active,
                "expires_at": exp_ts,
                "note": str(note_in or "").strip()[:120],
                "scan_count": 0,
                "account_balance_vnd": 0,
                "account_balance_updated_at": 0,
                "account_check_count": 0,
                "created_at": now_ts,
                "updated_at": now_ts,
                "token_ver": 1,
                "max_devices": m_dev,
                "devices": {},
            }
            users[target_u] = new_rec
            saved_pub = _public_vip_user(new_rec, bmap)
        meta = _load_sync_meta_unlocked()
        if target_u in meta["deleted_users"]:
            meta["deleted_users"].pop(target_u, None)
            _save_sync_meta_unlocked(meta)
        _save_vip_users_unlocked(users)

    if not existing:
        init_user_sheet_async(target_u, role=eff_role, note=str(note_in or ""))
    resp_obj: Dict[str, Any] = {"success": True, "user": saved_pub}
    if self_new_tok:
        resp_obj["token"] = self_new_tok
    return JSONResponse(content=resp_obj)


@app.delete("/api/vip/admin/users/{target_user}")
async def api_vip_admin_delete_user(target_user: str, req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền xóa tài khoản VIP."})
    t_clean = str(target_user or "").strip().lower()
    if t_clean == u_obj["username"]:
        return JSONResponse(status_code=400, content={"success": False, "error": "Không thể tự xóa tài khoản Admin đang đăng nhập!"})
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        if t_clean not in users:
            return JSONResponse(status_code=404, content={"success": False, "error": "Không tìm thấy tài khoản này."})
        users.pop(t_clean, None)
        meta = _load_sync_meta_unlocked()
        meta["deleted_users"][t_clean] = int(time.time())
        _save_sync_meta_unlocked(meta)
        _save_vip_users_unlocked(users)
    return JSONResponse(content={"success": True})


_ROUTER_PROXY_TARGETS = [
    # 26/11/2026: cổng 11101 cũ (PIA 104.165.194.11 -> IP ra 104.165.194.174 Santa Clara) bị TikTok nhận là Hong Kong.
    # Đã đổi node_us_ca trên router sang PIA "US California" 172.98.213.203 -> IP ra 172.98.213.79 (Los Angeles), TikTok mở login bình thường.
    # Mặc định "California (Los Angeles)" dùng cổng 11110 (172.98.213.159, Los Angeles).
    {"id": "preset:us_la", "port": 11110, "name": "California (Los Angeles)", "country": "US", "host": "192.168.2.101", "default": True, "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_nyc", "port": 11104, "name": "New York (NYC)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_lv", "port": 11103, "name": "Las Vegas", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_dc", "port": 11105, "name": "Washington DC", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_la2", "port": 11101, "name": "Los Angeles (#2)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:de_fra", "port": 11108, "name": "Frankfurt", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
    {"id": "preset:de_ber", "port": 11109, "name": "Berlin", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
    {"id": "preset:mx_mex", "port": 11107, "name": "Mexico City", "country": "MX", "host": "192.168.2.101", "group": "🇲🇽 Mexico"},
    {"id": "preset:id_jkt", "port": 11106, "name": "Jakarta", "country": "ID", "host": "192.168.2.101", "group": "🇮🇩 Indonesia"},
]
_ROUTER_PROXY_MAP = {item["id"]: item for item in _ROUTER_PROXY_TARGETS}


def _resolve_proxy_value(proxy_val: str) -> str:
    """Giải mã mã proxy nội bộ (preset:...) sang địa chỉ SOCKS5 thực chỉ khi chạy tại Backend Local."""
    s = str(proxy_val or "").strip()
    if s == "preset:us_sv":
        s = "preset:us_la"
    if not s or s in ("direct", "custom"):
        return ""
    is_cloud = bool(os.getenv("IS_CLOUD") or os.getenv("RENDER"))
    if s in _ROUTER_PROXY_MAP:
        if is_cloud:
            return ""
        item = _ROUTER_PROXY_MAP[s]
        return f"socks5://{item['host']}:{item['port']}"
    m_pia = re.match(r"^preset:pia_(\d+)$", s)
    if m_pia:
        return "" if is_cloud else f"socks5://127.0.0.1:{m_pia.group(1)}"
    if ":" not in s or any(ch.isspace() for ch in s):
        return ""
    return s


def _mask_internal_proxy_for_client(proxy_val: str) -> str:
    """Ẩn hoàn toàn IP LAN nội bộ (192.168.x / 127.0.0.1) trước khi trả về trình duyệt hoặc file tải."""
    s = str(proxy_val or "").strip()
    if not s:
        return ""
    if "192.168." in s or "127.0.0.1" in s:
        for item in _ROUTER_PROXY_TARGETS:
            if f":{item['port']}" in s:
                return item["id"]
        return "preset:us_la"
    return s


@app.post("/api/check")
async def api_check_channel(req: Request, background_tasks: BackgroundTasks):
    """
    API kiểm tra kênh TikTok và phân tích trùng lặp.
    Nhận JSON: {"query": "jordsf2"} hoặc {"query": "https://www.tiktok.com/@jordsf2"}
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không hợp lệ.")

    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên người dùng hoặc link TikTok!")

    max_cursor = int(body.get("max_cursor") or 0)
    max_videos = 18
    cookie = str(body.get("cookie") or "").strip()
    proxy = _resolve_proxy_value(str(body.get("proxy") or ""))
    sec_uid = str(body.get("sec_uid") or "").strip()

    # Kiểm tra quyền VIP / Admin thực sự + kiểm tra Mã máy có bị khóa / vượt giới hạn thiết bị hoặc hết 10 lượt Free/ngày không
    vip_user = _extract_vip_user(req, body)
    raw_explicit_dev = re.sub(
        r"[^A-Z0-9-]",
        "",
        str(req.headers.get("x-device-id") or (body.get("device_id") if isinstance(body, dict) else "") or "").strip().upper(),
    )[:20]
    dev_id, dev_label, client_ip_chk = _extract_device_info(req, body)
    has_vip_access = False
    has_manual_vip = False
    is_admin_user = False
    charge_dedup_balance = False
    checked_username = ""

    with _VIP_LOCK:
        bmap_db = _load_blocked_devices_unlocked()
        if vip_user:
            users_db = _load_vip_users_unlocked()
            u_rec = users_db.get(vip_user["username"])
            if isinstance(u_rec, dict):
                checked_username = str(u_rec.get("username") or "")
                dev_err = _record_and_check_device_unlocked(u_rec, dev_id, dev_label, client_ip_chk, bmap_db)
                if dev_err:
                    _save_vip_users_unlocked(users_db)
                    return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": dev_err})
                is_admin_user = str(u_rec.get("role") or "").lower() == "admin"
                has_manual_vip = _has_manual_vip_privilege(u_rec)
                cur_bal = max(0, int(u_rec.get("account_balance_vnd") or 0))
                charge_dedup_balance = False
                has_vip_access = has_manual_vip
                _save_vip_users_unlocked(users_db)
        elif dev_id in bmap_db:
            return JSONResponse(
                status_code=403,
                content={"success": False, "device_blocked": True, "error": f"⛔ Mã máy {dev_id} đã bị Quản trị viên khóa truy cập!"},
            )

    is_tuandzvcc = bool(vip_user and str(vip_user.get("username") or "").strip().lower() == "tuandzvcc")
    if max_cursor > 0 and not is_tuandzvcc:
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "load_more_restricted": True,
                "error": "Tính năng quét thêm 18 video tiếp theo chỉ khả dụng cho tài khoản quản trị chính.",
            },
        )

    async def _execute_dedup_scan() -> Dict[str, Any]:
        allow_paid_dedup = True
        dedup_locked = False
        q_stat = None

        if not has_vip_access:
            if not re.match(r"^DEV-[0-9A-Z]{6,12}$", raw_explicit_dev):
                return {
                    "_early_error_status": 400,
                    "_early_error_body": {"success": False, "error": "Không xác thực được Mã máy thiết bị. Vui lòng tải lại trang web (Ctrl+F5) và thử lại!"},
                }
            q_stat = _get_free_quota_status(dev_id, checked_username)
            if q_stat["used"] >= FREE_DAILY_LIMIT:
                allow_paid_dedup = False
                dedup_locked = True

        force_ext = query.startswith("!")
        eff_cookie = "" if force_ext else cookie
        eff_proxy = proxy
        eff_sec_uid = sec_uid
        clean_user = extract_clean_username(query.lstrip("!"))

        # Chỉ tài khoản tuandzvcc mới được phép tự động tra cứu và dùng cookie nick trong tool / server
        if is_tuandzvcc and not eff_cookie and clean_user and not force_ext:
            acc_entry = studio_engine.ACTIVE_STUDIO_ACCOUNTS.get(clean_user.lower())
            if acc_entry:
                eff_cookie = acc_entry.get("cookie", "")
                if not eff_proxy and acc_entry.get("proxy"):
                    eff_proxy = acc_entry["proxy"]
                if not eff_sec_uid and acc_entry.get("account", {}).get("sec_uid"):
                    eff_sec_uid = acc_entry["account"]["sec_uid"]

        # Ghi nhận người dùng kiểm tra ngầm về đúng trang tính Google Sheet theo tài khoản đã đăng ký (lần quét đầu max_cursor == 0)
        if max_cursor == 0:
            client_ip = get_client_ip(req)
            if clean_user:
                record_user_check_async(clean_user, client_ip, account=checked_username, device_id=dev_id)

        res_scan = await asyncio.to_thread(
            dedup_provider.ROUTER.check,
            fetch_channel_info_and_posts,
            query,
            sec_uid=eff_sec_uid,
            max_cursor=max_cursor,
            cookie_str=eff_cookie,
            proxy_str=eff_proxy,
            max_videos=max_videos,
            allow_paid_groupid=allow_paid_dedup,
            allow_tool_self_cookies=is_tuandzvcc,
        )

        internal_cost = res_scan.pop("_internal_cost", None) if isinstance(res_scan, dict) else None
        if checked_username and max_cursor == 0 and dedup_locked:
            with _VIP_LOCK:
                usage_users = _load_vip_users_unlocked()
                usage_user = usage_users.get(checked_username)
                if isinstance(usage_user, dict):
                    record_dedup_blocked(usage_user)
                    _save_vip_users_unlocked(usage_users)
        if isinstance(res_scan, dict) and res_scan.get("success"):
            if checked_username and max_cursor == 0 and not dedup_locked:
                with _VIP_LOCK:
                    usage_users = _load_vip_users_unlocked()
                    usage_user = usage_users.get(checked_username)
                    if isinstance(usage_user, dict):
                        record_dedup_success(usage_user)
                        usage_user['updated_at'] = int(time.time())
                        _save_vip_users_unlocked(usage_users)
            if not is_tuandzvcc:
                res_scan["hasMore"] = False
            if has_vip_access:
                res_scan["vip_mode"] = True
                res_scan["dedup_locked"] = False
                if is_admin_user and bool((res_scan.get("reader_provider") or {}).get("fallback")):
                    cost_obj = internal_cost if isinstance(internal_cost, dict) else {}
                    c_usd = round(float(cost_obj.get("cost_usd") or 0.0), 4)
                    c_calls = int(cost_obj.get("api_calls") or 0)
                    c_cached = int(cost_obj.get("cached_hits") or 0)
                    c_vnd = int(round(c_usd * _TIKHUB_USD_TO_VND))
                    bal_info = await asyncio.to_thread(_get_tikhub_live_balance, c_calls > 0)
                    res_scan["admin_billing"] = {
                        "scan_cost_usd": c_usd,
                        "scan_cost_vnd": c_vnd,
                        "api_calls": c_calls,
                        "cached_hits": c_cached,
                        "balance_known": bal_info["balance_known"],
                        "balance_stale": bal_info["balance_stale"],
                        "balance_usd": bal_info["total_usd"],
                        "balance_vnd": bal_info["total_vnd"],
                        "est_scans_left": bal_info["est_scans_left"],
                        "usd_rate": _TIKHUB_USD_TO_VND,
                    }
            elif not dedup_locked:
                res_scan["vip_mode"] = False
                res_scan["dedup_locked"] = False
                res_scan["free_quota"] = _consume_free_quota(dev_id, checked_username)
            else:
                res_scan["vip_mode"] = False
                res_scan["dedup_locked"] = True
                res_scan["free_quota"] = q_stat
                for v_item in (res_scan.get("list") or []):
                    if isinstance(v_item, dict):
                        v_item["_dedup_locked"] = True
                        v_item["is_dup"] = False
                        v_item["is_same_channel_dup"] = False
                        v_item["group_id"] = ""
                        v_item["groupId"] = ""
                        v_item["origin_url"] = ""
                        v_item["has_origin_link"] = False
                        v_item["is_crp_punished"] = False
                        v_item["has_penalty"] = False
                        v_item["is_fyp_restricted"] = False
                        v_item["penalty_details"] = ""
                        v_item["dup_reason"] = ""
                if isinstance(res_scan.get("stats_summary"), dict):
                    res_scan["stats_summary"]["dup_count"] = 0
                    res_scan["stats_summary"]["clean_count"] = 0

            # Lưu lịch sử check riêng cho tài khoản đã đăng nhập (chỉ khi không bị khóa VIP; khách không đăng nhập sẽ không lưu)
            if vip_user and checked_username and not dedup_locked:
                try:
                    _record_check_history_for_user(
                        checked_username,
                        res_scan,
                        fallback_username=clean_user,
                        append_mode=(max_cursor > 0),
                    )
                except Exception as e_hist:
                    logger.warning(f"[CheckHistory] Lỗi ghi lịch sử check: {e_hist}")

        return res_scan

    if charge_dedup_balance and checked_username:
        async with _get_user_account_check_lock(checked_username):
            with _VIP_LOCK:
                users_db = _load_vip_users_unlocked()
                u_rec = users_db.get(checked_username)
                cur_bal_chk = max(0, int((u_rec or {}).get("account_balance_vnd") or 0))
                if not isinstance(u_rec, dict) or _has_manual_vip_privilege(u_rec):
                    charge_dedup_balance = False
                    has_vip_access = bool(isinstance(u_rec, dict) and _has_manual_vip_privilege(u_rec))
                elif cur_bal_chk < DEDUP_CHECK_UNIT_PRICE_VND:
                    charge_dedup_balance = False
                    has_vip_access = False

            res = await _execute_dedup_scan()
            if isinstance(res, dict) and "_early_error_status" in res:
                return JSONResponse(status_code=res["_early_error_status"], content=res["_early_error_body"])

            if charge_dedup_balance and isinstance(res, dict) and res.get("success"):
                with _VIP_LOCK:
                    users_db = _load_vip_users_unlocked()
                    u_rec = users_db.get(checked_username)
                    if isinstance(u_rec, dict):
                        cur_bal2 = max(0, int(u_rec.get("account_balance_vnd") or 0))
                        now_bal_ts = int(time.time())
                        charged_dedup = min(cur_bal2, DEDUP_CHECK_UNIT_PRICE_VND)
                        u_rec["account_balance_vnd"] = max(0, cur_bal2 - charged_dedup)
                        u_rec["account_balance_updated_at"] = now_bal_ts
                        u_rec["updated_at"] = now_bal_ts
                        u_rec["scan_count"] = int(u_rec.get("scan_count") or 0) + 1
                        _save_vip_users_unlocked(users_db)
                        new_bal = max(0, int(u_rec.get("account_balance_vnd") or 0))
                        res["user_billing"] = {
                            "is_admin_free": False,
                            "unit_price_vnd": DEDUP_CHECK_UNIT_PRICE_VND,
                            "charged_vnd": charged_dedup,
                            "balance_vnd": new_bal,
                            "dedup_checks_left": _calc_dedup_checks_left(new_bal),
                            "account_checks_left": _calc_account_checks_left(new_bal),
                            "is_vip": _has_vip_privilege(u_rec),
                        }
    else:
        res = await _execute_dedup_scan()
        if isinstance(res, dict) and "_early_error_status" in res:
            return JSONResponse(status_code=res["_early_error_status"], content=res["_early_error_body"])

    # Kích hoạt tải trước ảnh bìa vào RAM cache song song trong nền (0.001s khi trình duyệt yêu cầu)
    v_list = res.get("list", [])
    if v_list:
        background_tasks.add_task(prefetch_thumbnails, v_list)
        
    return JSONResponse(content=res)


_ACCOUNT_STREAM_TASKS = set()


async def _stream_account_checks(username, unique_inputs, pre_dup_skipped):
    """Keep the existing per-user wallet lock until all authorized work finishes.

    Browser disconnects do not cancel billing/history for work already submitted.
    Completed profiles are saved and billed before they are sent to the browser.
    """
    user_lock = _get_user_account_check_lock(username)
    await user_lock.acquire()
    ready = False
    try:
        with _VIP_LOCK:
            user = _load_vip_users_unlocked().get(username)
            if not isinstance(user, dict):
                return JSONResponse(status_code=401, content={'success': False, 'login_required': True, 'error': 'Tài khoản không tồn tại.'})
            admin = user.get('role') == 'admin'
            balance = max(0, int(user.get('account_balance_vnd') or 0))
            required = len(unique_inputs) * ACCOUNT_CHECK_UNIT_PRICE_VND
            if not admin and balance < required:
                return JSONResponse(status_code=402, content={'success': False, 'insufficient_balance': True,
                    'balance_vnd': balance, 'checks_left': _calc_account_checks_left(balance),
                    'required_vnd': required, 'requested_count': len(unique_inputs),
                    'error': 'Số dư Check Tài Khoản không đủ cho danh sách này. Vui lòng nạp thêm.'})
            ready = True
    finally:
        # The running job takes over ownership below; early preflight responses
        # must release the lock as well.
        if not ready:
            user_lock.release()

    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    totals = {'completed': 0, 'charged_count': 0, 'error_count': 0, 'calls': 0, 'cached': 0}

    def publish(event):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def completed(event):
        result = event['result']
        ok = result.get('ok') is True
        totals['completed'] += 1
        totals['charged_count'] += int(ok)
        totals['error_count'] += int(not ok)
        totals['calls'] += event['api_calls']
        totals['cached'] += event['cached_country_hits']
        with _VIP_LOCK:
            users = _load_vip_users_unlocked()
            user = users.get(username)
            if not isinstance(user, dict):
                raise RuntimeError('Tài khoản không còn tồn tại; đã dừng ghi kết quả.')
            if not admin and ok:
                user['account_balance_vnd'] = max(0, int(user.get('account_balance_vnd') or 0) - ACCOUNT_CHECK_UNIT_PRICE_VND)
                user['account_balance_updated_at'] = int(time.time())
            record_account_results(user, int(ok), 1)
            user['updated_at'] = int(time.time())
            _save_vip_users_unlocked(users)
            balance = max(0, int(user.get('account_balance_vnd') or 0))
            billing = {'is_admin_free': admin, 'unit_price_vnd': ACCOUNT_CHECK_UNIT_PRICE_VND,
                'charged_count': totals['charged_count'], 'error_skipped_count': totals['error_count'],
                'charged_vnd': 0 if admin else totals['charged_count'] * ACCOUNT_CHECK_UNIT_PRICE_VND,
                'balance_vnd': balance, 'checks_left': _calc_account_checks_left(balance),
                'dedup_checks_left': _calc_dedup_checks_left(balance), 'is_vip': _has_vip_privilege(user)}
        try:
            _record_account_check_history_for_user(username, [result])
        except Exception:
            logger.warning('[CheckHistory] Chưa lưu được một kết quả Check Tài Khoản.')
        publish({'type': 'result', 'success': True, 'index': event['index'], 'total': event['total'],
            'completed': totals['completed'], 'result': result, 'user_billing': billing,
            'scan_cost_usd': round(totals['calls'] * 0.001, 4) if admin else None,
            'scan_cost_vnd': int(round(totals['calls'] * 0.001 * _TIKHUB_USD_TO_VND)) if admin else None})

    async def run():
        try:
            result = await asyncio.to_thread(check_accounts_batch_tikhub, unique_inputs, 6, completed)
            result['duplicate_lines_skipped'] = int(result.get('duplicate_lines_skipped') or 0) + pre_dup_skipped
            if admin:
                info = await asyncio.to_thread(_get_tikhub_live_balance, totals['calls'] > 0)
                cost = round(float(result.get('cost_usd') or 0), 4)
                result['admin_billing'] = {'scan_cost_usd': cost, 'scan_cost_vnd': int(round(cost * _TIKHUB_USD_TO_VND)),
                    'api_calls': totals['calls'], 'cached_hits': totals['cached'],
                    'balance_known': info['balance_known'], 'balance_stale': info['balance_stale'],
                    'balance_usd': info['total_usd'], 'balance_vnd': info['total_vnd'],
                    'est_scans_left': info['est_scans_left'], 'usd_rate': _TIKHUB_USD_TO_VND}
            publish({'type': 'done', **result})
        except Exception:
            logger.exception('[AccountCheck] Lỗi khi xử lý danh sách đang truyền kết quả.')
            publish({'type': 'error', 'success': False,
                'error': 'Lượt kiểm tra bị gián đoạn. Các kết quả đã hiện vẫn được lưu trong lịch sử; kiểm tra lại lịch sử trước khi chạy lại.'})
        finally:
            user_lock.release()

    task = asyncio.create_task(run())
    _ACCOUNT_STREAM_TASKS.add(task)
    task.add_done_callback(_ACCOUNT_STREAM_TASKS.discard)

    async def events():
        yield json.dumps({'type': 'start', 'total': len(unique_inputs)}) + '\n'
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield '{"type":"heartbeat"}\n'
                continue
            yield json.dumps(event, ensure_ascii=False) + '\n'
            if event['type'] in ('done', 'error'):
                return

    return StreamingResponse(events(), media_type='application/x-ndjson',
        headers={'Cache-Control': 'no-cache, no-store, no-transform', 'X-Accel-Buffering': 'no'})


@app.post("/api/check-accounts")
async def api_check_accounts(req: Request, background_tasks: BackgroundTasks):
    """Kiểm tra hàng loạt trạng thái đề xuất (account_labels), ngày tạo, quốc gia và follow theo danh sách user/link."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu yêu cầu không hợp lệ."})

    raw_inputs: List[str] = []
    if isinstance(body.get("inputs"), list):
        raw_inputs = [str(x or "").strip() for x in body.get("inputs") or []]
    elif isinstance(body.get("raw_text"), str):
        raw_inputs = re.split(r"[\r\n,\t ]+", body.get("raw_text") or "")

    # Tách token lẫn dấu phẩy/khoảng trắng trong từng phần tử và lọc trùng giữ thứ tự
    tokens: List[str] = []
    for item in raw_inputs:
        for part in re.split(r"[\r\n,\t ]+", str(item or "").strip()):
            p = part.strip()
            if p:
                tokens.append(p)

    seen_handles = set()
    unique_inputs: List[str] = []
    pre_dup_skipped = 0
    for tok in tokens:
        ext_h = core_dedup._extract_handle_from_url_or_text(tok)
        dedup_k = ext_h if ext_h else f"raw:{tok.lower()}"
        if dedup_k in seen_handles:
            pre_dup_skipped += 1
            continue
        seen_handles.add(dedup_k)
        unique_inputs.append(tok)

    if not unique_inputs:
        return JSONResponse(status_code=400, content={"success": False, "error": "Vui lòng dán ít nhất 1 @username hoặc link TikTok!"})
    if len(unique_inputs) > 50:
        return JSONResponse(status_code=400, content={"success": False, "error": "Mỗi lần kiểm tra tối đa 50 tài khoản. Vui lòng chia nhỏ danh sách!"})

    vip_user = _extract_vip_user(req, body)
    dev_id, dev_label, client_ip_chk = _extract_device_info(req, body)
    checked_username = ""

    with _VIP_LOCK:
        bmap_db = _load_blocked_devices_unlocked()
        if vip_user:
            users_db = _load_vip_users_unlocked()
            u_rec = users_db.get(vip_user["username"])
            if not isinstance(u_rec, dict):
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "login_required": True,
                        "error": "Phiên đăng nhập không hợp lệ. Vui lòng đăng nhập lại!",
                    },
                )
            checked_username = str(u_rec.get("username") or "")
            dev_err = _record_and_check_device_unlocked(u_rec, dev_id, dev_label, client_ip_chk, bmap_db)
            _save_vip_users_unlocked(users_db)
            if dev_err:
                return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": dev_err})
        elif dev_id in bmap_db:
            return JSONResponse(
                status_code=403,
                content={"success": False, "device_blocked": True, "error": f"⛔ Mã máy {dev_id} đã bị Quản trị viên khóa truy cập!"},
            )
        else:
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "login_required": True,
                    "error": f"Vui lòng đăng nhập tài khoản để sử dụng Check Tài Khoản ({ACCOUNT_CHECK_UNIT_PRICE_VND}đ / tài khoản)!",
                },
            )

    req_count = len(unique_inputs)
    required_vnd = req_count * ACCOUNT_CHECK_UNIT_PRICE_VND

    if body.get('stream') is True:
        return await _stream_account_checks(checked_username, unique_inputs, pre_dup_skipped)

    async with _get_user_account_check_lock(checked_username):
        with _VIP_LOCK:
            users_db = _load_vip_users_unlocked()
            u_rec = users_db.get(checked_username)
            if not isinstance(u_rec, dict):
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "login_required": True, "error": "Tài khoản không tồn tại."},
                )
            is_admin_user = str(u_rec.get("role") or "").lower() == "admin"
            cur_bal = max(0, int(u_rec.get("account_balance_vnd") or 0))
            checks_left = _calc_account_checks_left(cur_bal)
            if not is_admin_user and cur_bal < required_vnd:
                return JSONResponse(
                    status_code=402,
                    content={
                        "success": False,
                        "insufficient_balance": True,
                        "balance_vnd": cur_bal,
                        "checks_left": checks_left,
                        "required_vnd": required_vnd,
                        "requested_count": req_count,
                        "unit_price_vnd": ACCOUNT_CHECK_UNIT_PRICE_VND,
                        "error": (
                            f"Số dư Check Tài Khoản không đủ! Cần {required_vnd:,}đ "
                            f"({req_count} tài khoản × {ACCOUNT_CHECK_UNIT_PRICE_VND}đ), "
                            f"số dư hiện có {cur_bal:,}đ (tối đa {checks_left} lượt). Vui lòng nạp thêm số dư qua VietQR (từ 50.000đ)!"
                        ),
                    },
                )

        batch_res = await asyncio.to_thread(check_accounts_batch_tikhub, unique_inputs, 6)
        batch_res["duplicate_lines_skipped"] = int(batch_res.get("duplicate_lines_skipped") or 0) + pre_dup_skipped
        if not batch_res.get("success"):
            return JSONResponse(status_code=400, content=batch_res)

        results_list = batch_res.get("results") or []
        ok_count = sum(1 for r in results_list if isinstance(r, dict) and r.get("ok") is True)
        error_count = max(0, len(results_list) - ok_count)
        charged_vnd = 0 if is_admin_user else (ok_count * ACCOUNT_CHECK_UNIT_PRICE_VND)

        with _VIP_LOCK:
            users_db = _load_vip_users_unlocked()
            u_rec = users_db.get(checked_username)
            if isinstance(u_rec, dict):
                cur_bal2 = max(0, int(u_rec.get("account_balance_vnd") or 0))
                if not is_admin_user and charged_vnd > 0:
                    now_bal_ts = int(time.time())
                    u_rec["account_balance_vnd"] = max(0, cur_bal2 - charged_vnd)
                    u_rec["account_balance_updated_at"] = now_bal_ts
                    u_rec["updated_at"] = now_bal_ts
                new_bal = max(0, int(u_rec.get("account_balance_vnd") or 0))
                record_account_results(u_rec, ok_count, len(results_list))
                u_rec['updated_at'] = int(time.time())
                _save_vip_users_unlocked(users_db)
                user_has_vip = _has_vip_privilege(u_rec)
            else:
                new_bal = 0
                user_has_vip = False

        batch_res["user_billing"] = {
            "is_admin_free": is_admin_user,
            "unit_price_vnd": ACCOUNT_CHECK_UNIT_PRICE_VND,
            "charged_count": ok_count,
            "error_skipped_count": error_count,
            "charged_vnd": charged_vnd,
            "balance_vnd": new_bal,
            "dedup_checks_left": _calc_dedup_checks_left(new_bal),
            "checks_left": _calc_account_checks_left(new_bal),
            "is_vip": user_has_vip,
        }

        if is_admin_user:
            c_usd = round(float(batch_res.get("cost_usd") or 0.0), 4)
            c_calls = int(batch_res.get("api_calls") or 0)
            c_cached = int(batch_res.get("cached_country_hits") or 0)
            c_vnd = int(round(c_usd * _TIKHUB_USD_TO_VND))
            bal_info = await asyncio.to_thread(_get_tikhub_live_balance, c_calls > 0)
            batch_res["vip_mode"] = True
            batch_res["admin_billing"] = {
                "scan_cost_usd": c_usd,
                "scan_cost_vnd": c_vnd,
                "api_calls": c_calls,
                "cached_hits": c_cached,
                "balance_known": bal_info["balance_known"],
                        "balance_stale": bal_info["balance_stale"],
                        "balance_usd": bal_info["total_usd"],
                "balance_vnd": bal_info["total_vnd"],
                "est_scans_left": bal_info["est_scans_left"],
                "usd_rate": _TIKHUB_USD_TO_VND,
            }

    if checked_username:
        try:
            _record_account_check_history_for_user(checked_username, batch_res.get("results") or [])
        except Exception as e_ahist:
            logger.warning(f"[CheckHistory] Lỗi ghi lịch sử Check Tài Khoản: {e_ahist}")

    av_items = [{"cover_url": it["avatar"]} for it in (batch_res.get("results") or []) if isinstance(it, dict) and it.get("avatar")]
    if av_items:
        background_tasks.add_task(prefetch_thumbnails, av_items)

    return JSONResponse(content=batch_res)


def _clear_history_bucket_response(req: Request, entries_key: str, deleted_key: str):
    vip_user = _extract_vip_user(req)
    if not vip_user:
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng nhập để sử dụng Lịch sử check."})
    acct = str(vip_user.get("username") or "").strip().lower()
    now_ts = int(time.time())
    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        acct_box = hmap.setdefault(acct, {"entries": {}, "deleted": {}, "account_entries": {}, "account_deleted": {}})
        entries = acct_box.get(entries_key) if isinstance(acct_box.get(entries_key), dict) else {}
        deleted = acct_box.setdefault(deleted_key, {})
        for k in list(entries.keys()):
            deleted[str(k).lstrip("@").strip().lower()] = now_ts
        acct_box[entries_key] = {}
        _prune_check_history_unlocked(hmap, now_ts)
        _save_check_history_unlocked(hmap, schedule_sync=True)
    return JSONResponse(content={"success": True})


def _delete_history_item_response(req: Request, target_username: str, entries_key: str, deleted_key: str, invalid_msg: str):
    vip_user = _extract_vip_user(req)
    if not vip_user:
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng nhập để sử dụng Lịch sử check."})
    acct = str(vip_user.get("username") or "").strip().lower()
    target = re.sub(r"[^a-zA-Z0-9._-]", "", str(target_username or "").lstrip("@").strip().lower())
    if not target:
        return JSONResponse(status_code=400, content={"success": False, "error": invalid_msg})
    now_ts = int(time.time())
    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        acct_box = hmap.setdefault(acct, {"entries": {}, "deleted": {}, "account_entries": {}, "account_deleted": {}})
        entries = acct_box.get(entries_key) if isinstance(acct_box.get(entries_key), dict) else {}
        deleted = acct_box.setdefault(deleted_key, {})
        entries.pop(target, None)
        deleted[target] = now_ts
        _prune_check_history_unlocked(hmap, now_ts)
        _save_check_history_unlocked(hmap, schedule_sync=True)
    return JSONResponse(content={"success": True, "deleted": target})


@app.get("/api/channel-groups")
def api_get_channel_groups(req: Request):
    """Lấy bảng Check Nhóm Kênh riêng của từng tài khoản đang đăng nhập."""
    vip_user = _extract_vip_user(req)
    if not channel_groups_store.is_authenticated_user(vip_user):
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng nhập tài khoản để sử dụng Check Nhóm Kênh."})
    acct = str(vip_user.get("username") or "").strip().lower()
    with _CHANNEL_GROUPS_LOCK:
        store = _load_channel_groups_unlocked()
        state = channel_groups_store.get_user_board_from_store(store, acct)
    # Bổ sung avatar / kết quả check gần nhất từ check_history của chính tài khoản đó
    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        acct_box = hmap.get(acct) if isinstance(hmap.get(acct), dict) else {}
        entries = acct_box.get("entries") if isinstance(acct_box.get("entries"), dict) else {}
    for g in state.get("groups") or []:
        for ch in g.get("channels") or []:
            h_ent = entries.get(str(ch.get("username") or "").lower())
            if isinstance(h_ent, dict):
                if not ch.get("avatarUrl") and h_ent.get("avatar"):
                    ch["avatarUrl"] = str(h_ent.get("avatar") or "")
                if (not ch.get("name") or ch.get("name") == ch.get("username")) and h_ent.get("nickname"):
                    ch["name"] = str(h_ent.get("nickname") or ch["username"])
                st_h = h_ent.get("stats") if isinstance(h_ent.get("stats"), dict) else {}
                h_ts = int(h_ent.get("checked_at") or 0)
                cur_lc = ch.get("lastCheck") if isinstance(ch.get("lastCheck"), dict) else {}
                if h_ts > int(cur_lc.get("checkedAt") or 0):
                    ch["lastCheck"] = {
                        "checkedAt": h_ts,
                        "total": int(st_h.get("total") or len(h_ent.get("videos") or [])),
                        "dup": int(st_h.get("dup") or 0),
                        "fyp": int(st_h.get("fyp") or 0),
                        "clean": int(st_h.get("clean") or 0),
                    }
    return JSONResponse(content={"success": True, **state})


@app.post("/api/channel-groups")
async def api_save_channel_groups(req: Request):
    """Lưu bảng Check Nhóm Kênh riêng của từng tài khoản đang đăng nhập."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not channel_groups_store.is_authenticated_user(vip_user):
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng nhập tài khoản để lưu Check Nhóm Kênh."})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"success": False, "error": "Payload không hợp lệ."})
    acct = str(vip_user.get("username") or "").strip().lower()
    now_ts = int(time.time())
    incoming = channel_groups_store.normalize_board_payload({
        "updated_at": now_ts,
        "top_tab_order": body.get("top_tab_order") if isinstance(body.get("top_tab_order"), list) else [],
        "groups": body.get("groups") if isinstance(body.get("groups"), list) else [],
    }, now_ts=now_ts)
    allow_empty = bool(body.get("allow_empty", False))
    with _CHANNEL_GROUPS_LOCK:
        store = _load_channel_groups_unlocked()
        existing = channel_groups_store.get_user_board_from_store(store, acct)
        if existing.get("groups") and not incoming.get("groups") and not allow_empty:
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": "Chặn ghi đè danh sách rỗng ngoài ý muốn.", **existing},
            )
        next_store = channel_groups_store.set_user_board_in_store(store, acct, incoming, now_ts=now_ts)
        saved_store = _save_channel_groups_unlocked(next_store, schedule_sync=True)
        saved_board = channel_groups_store.get_user_board_from_store(saved_store, acct)
    return JSONResponse(content={"success": True, **saved_board})


# ==============================================================================
# QUẢN LÝ MAIL OUTLOOK OAUTH2 TRỰC TIẾP TRÊN WEB (CHỈ RIÊNG TÀI KHOẢN tuandzvcc)
# ==============================================================================
def _is_strict_tuandzvcc(vip_user: dict | None) -> bool:
    return bool(vip_user and str(vip_user.get("username") or "").strip().lower() == "tuandzvcc")


@app.get("/api/outlook-oauth/state")
def api_outlook_oauth_state(req: Request, folder: str = ""):
    vip_user = _extract_vip_user(req)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    return JSONResponse(content=OUTLOOK_WEB_SERVICE.get_snapshot(folder=folder))


@app.post("/api/outlook-oauth/folder")
async def api_outlook_oauth_folder(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    res = OUTLOOK_WEB_SERVICE.manage_folder(
        action=str(body.get("action") or ""),
        name=str(body.get("name") or ""),
        new_name=str(body.get("new_name") or ""),
    )
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.post("/api/outlook-oauth/settings")
async def api_outlook_oauth_settings(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    return JSONResponse(content=OUTLOOK_WEB_SERVICE.update_settings(body if isinstance(body, dict) else {}))


@app.post("/api/outlook-oauth/rows")
async def api_outlook_oauth_rows(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    res = OUTLOOK_WEB_SERVICE.modify_rows(body if isinstance(body, dict) else {})
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.post("/api/outlook-oauth/start")
async def api_outlook_oauth_start(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    res = OUTLOOK_WEB_SERVICE.start_batch(body if isinstance(body, dict) else {})
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.post("/api/outlook-oauth/stop")
async def api_outlook_oauth_stop(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    return JSONResponse(content=OUTLOOK_WEB_SERVICE.stop_batch())


@app.post("/api/outlook-oauth/get-code")
async def api_outlook_oauth_get_code(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    row_ids = body.get("row_ids") if isinstance(body.get("row_ids"), list) else []
    res = await asyncio.to_thread(OUTLOOK_WEB_SERVICE.fetch_codes, [str(x) for x in row_ids])
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.post("/api/outlook-oauth/verify-tokens")
async def api_outlook_oauth_verify_tokens(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    row_ids = body.get("row_ids") if isinstance(body.get("row_ids"), list) else []
    res = await asyncio.to_thread(OUTLOOK_WEB_SERVICE.verify_tokens, [str(x) for x in row_ids])
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.post("/api/outlook-oauth/messages")
async def api_outlook_oauth_messages(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not _is_strict_tuandzvcc(vip_user):
        return JSONResponse(status_code=403, content={"success": False, "error": "Tính năng chỉ dành riêng cho tuandzvcc."})
    from outlook_web_store import OUTLOOK_WEB_SERVICE
    row_id = str(body.get("row_id") or "").strip()
    try:
        res = await asyncio.to_thread(OUTLOOK_WEB_SERVICE.get_mailbox_messages, row_id)
    except Exception as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    return JSONResponse(status_code=200 if res.get("success", True) else 400, content=res)


@app.get("/api/check-history")
def api_get_check_history(req: Request):
    """Lấy danh sách Lịch sử check (Check Trùng + Check Tài Khoản) của tài khoản đang đăng nhập (tự động xóa sau 7 ngày)."""
    vip_user = _extract_vip_user(req)
    if not vip_user:
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng nhập để sử dụng Lịch sử check."})
    acct = str(vip_user.get("username") or "").strip().lower()
    can_acc_hist = True
    now_ts = int(time.time())
    with _HISTORY_LOCK:
        hmap = _load_check_history_unlocked()
        if _prune_check_history_unlocked(hmap, now_ts):
            _save_check_history_unlocked(hmap, schedule_sync=False)
        acct_box = hmap.get(acct) if isinstance(hmap.get(acct), dict) else {}
        entries = acct_box.get("entries") if isinstance(acct_box.get("entries"), dict) else {}
        acc_entries = acct_box.get("account_entries") if isinstance(acct_box.get("account_entries"), dict) else {}
        items = sorted(
            (v for v in entries.values() if isinstance(v, dict)),
            key=lambda x: -int(x.get("checked_at") or 0),
        )
        account_items = sorted(
            (v for v in acc_entries.values() if isinstance(v, dict)),
            key=lambda x: -int(x.get("checked_at") or 0),
        ) if can_acc_hist else []
    # Fill missing countries from evidence already read by the channel checker.
    # This is a read-only enrichment: no paid recheck, quota change or new date.
    account_items = [dict(item) for item in account_items]
    for item in account_items:
        if int(item.get('account_labels_version') or 0) < core_dedup.ACCOUNT_LABELS_VERSION:
            if item.get('raw_labels'):
                item.update(core_dedup._parse_account_labels(item['raw_labels']))
            elif item.get('recommend_status') == 'not_recommended':
                item.update(recommend_status='warning', recommend_label='Kết quả cũ cần xác minh',
                    is_not_recommended=False,
                    recommend_note='Kết quả dùng bộ phân loại cũ, chưa lưu nhãn gốc. Không đủ bằng chứng để tiếp tục xếp vào nhóm không được đề xuất.')
        if not core_dedup._is_valid_country_code(item.get('region')):
            country = core_dedup._get_cached_account_country(item.get('unique_id') or item.get('username'), item.get('uid'))
            if country:
                item['region'] = country
                item['region_display'] = core_dedup.format_full_country_display(country)
    return JSONResponse(content={
        "success": True,
        "ttl_days": 7,
        "account": acct,
        "items": items,
        "account_items": account_items,
    })


@app.delete("/api/check-history")
def api_clear_all_check_history(req: Request):
    """Xóa toàn bộ Lịch sử Check Trùng của tài khoản đang đăng nhập."""
    return _clear_history_bucket_response(req, "entries", "deleted")


@app.delete("/api/check-history/{target_username}")
def api_delete_check_history_item(req: Request, target_username: str):
    """Xóa 1 kênh cụ thể khỏi Lịch sử Check Trùng của tài khoản đang đăng nhập."""
    return _delete_history_item_response(req, target_username, "entries", "deleted", "Tên kênh không hợp lệ.")


@app.delete("/api/account-check-history")
def api_clear_all_account_check_history(req: Request):
    """Xóa toàn bộ Lịch sử Check Tài Khoản của tài khoản đang đăng nhập."""
    return _clear_history_bucket_response(req, "account_entries", "account_deleted")


@app.delete("/api/account-check-history/{target_username}")
def api_delete_account_check_history_item(req: Request, target_username: str):
    """Xóa 1 tài khoản khỏi Lịch sử Check Tài Khoản của tài khoản đang đăng nhập."""
    return _delete_history_item_response(req, target_username, "account_entries", "account_deleted", "Tên tài khoản không hợp lệ.")


@app.post("/api/export-accounts-excel")
async def api_export_accounts_excel(req: Request):
    """Xuất danh sách Check Tài Khoản ra file Excel (.xlsx) 7 cột căn giữa, giãn rộng cột."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    vip_user = _extract_vip_user(req, body if isinstance(body, dict) else None)
    if not vip_user:
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "login_required": True,
                "error": "Vui lòng đăng nhập tài khoản để xuất Excel Check Tài Khoản!",
            },
        )

    raw_rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        return JSONResponse(status_code=400, content={"success": False, "error": "Không có dữ liệu để xuất Excel."})
    if len(raw_rows) > 500:
        return JSONResponse(status_code=400, content={"success": False, "error": "Tối đa 500 dòng mỗi lần xuất."})

    raw_fn = str((body.get("filename") if isinstance(body, dict) else "") or "TikTok_Accounts.xlsx")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", raw_fn.rsplit(".", 1)[0])[:80] or "TikTok_Accounts"
    safe_fn = f"{safe_stem}.xlsx"

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Check Tai Khoan"

    headers = ["STT", "User", "Ngày tạo", "Quốc gia", "Followers", "Link Tiktok", "Trạng thái"]
    center_align = Alignment(horizontal="center", vertical="center")
    hdr_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    hdr_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    hdr_border = Border(
        left=Side(style="thin", color="334155"),
        right=Side(style="thin", color="334155"),
        top=Side(style="thin", color="334155"),
        bottom=Side(style="thin", color="334155"),
    )
    cell_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    base_font = Font(name="Calibri", size=11, color="0F172A")
    link_font = Font(name="Calibri", size=11, color="1D4ED8", underline="single")
    ok_font = Font(name="Calibri", size=11, bold=True, color="047857")
    ok_fill = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")
    bad_font = Font(name="Calibri", size=11, bold=True, color="BE123C")
    bad_fill = PatternFill(start_color="FFF1F2", end_color="FFF1F2", fill_type="solid")

    ws.append(headers)
    ws.row_dimensions[1].height = 28
    max_lens = {idx: len(str(h)) for idx, h in enumerate(headers, start=1)}
    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = center_align
        c.border = hdr_border

    for i, item in enumerate(raw_rows, start=1):
        row_obj = item if isinstance(item, dict) else {}
        u_str = str(row_obj.get("user") or "").strip()
        c_date = str(row_obj.get("create_date") or "—").strip()
        country = str(row_obj.get("country") or "Không rõ").strip()
        try:
            fol_val = int(row_obj.get("followers") or 0)
        except Exception:
            fol_val = 0
        link_url = str(row_obj.get("link") or "").strip()
        status_str = str(row_obj.get("status") or "Bình thường").strip()
        if status_str not in ("Bình thường", "Không đc đề xuất"):
            status_str = "Không đc đề xuất" if "không" in status_str.lower() else "Bình thường"

        r_idx = i + 1
        vals = [i, u_str, c_date, country, fol_val, link_url, status_str]
        ws.append(vals)
        ws.row_dimensions[r_idx].height = 22

        for col_idx, val in enumerate(vals, start=1):
            c = ws.cell(row=r_idx, column=col_idx)
            c.alignment = center_align
            c.border = cell_border
            c.font = base_font
            if col_idx == 5:
                c.number_format = "#,##0"
                disp_len = len(f"{fol_val:,}")
            else:
                disp_len = len(str(val or ""))
            if disp_len > max_lens[col_idx]:
                max_lens[col_idx] = disp_len

            if col_idx == 6 and link_url.startswith("http"):
                c.hyperlink = link_url
                c.font = link_font
            elif col_idx == 7:
                is_ok = (status_str == "Bình thường")
                c.font = ok_font if is_ok else bad_font
                c.fill = ok_fill if is_ok else bad_fill

    min_widths = {1: 10, 2: 26, 3: 16, 4: 24, 5: 16, 6: 48, 7: 24}
    for col_idx, min_w in min_widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = max(min_w, min(65, max_lens[col_idx] + 6))

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:G{len(raw_rows) + 1}"

    buf = BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_fn}"'},
    )

@app.get("/api/proxy/image")
def proxy_image(
    url: str = Query(..., description="TikTok CDN image URL"),
    cookie: str = Query("", description="Chuỗi Cookie tài khoản (cần thiết cho video riêng tư / private CDN)")
):
    """
    Proxy tải ảnh đại diện và ảnh bìa từ TikTok CDN tránh lỗi CORS / 403 Forbidden của TikTok.
    Hỗ trợ Cookie của tài khoản để tải ảnh bìa video riêng tư (Private CDN).
    Sử dụng bộ nhớ đệm RAM (RAM Cache) + Connection Pool siêu tốc (0.001s).
    """
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="URL ảnh không hợp lệ.")

    # 1. Trả về ngay lập tức từ RAM Cache nếu đã tải trước (0.001s)
    cached = _get_image_cache(url)
    if cached:
        content, media_type = cached
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=604800, immutable"})

    # 2. Tải qua Connection Pool dùng chung
    target_cookie = cookie.strip() if cookie else ""
    if not target_cookie:
        # Thử trích xuất UID từ query parameter policy của TikTok Private CDN
        try:
            import urllib.parse, base64, json
            parsed_u = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed_u.query)
            policy_val = qs.get("policy", [""])[0]
            if policy_val:
                pad = len(policy_val) % 4
                if pad:
                    policy_val += "=" * (4 - pad)
                pj = json.loads(base64.b64decode(policy_val).decode("utf-8", "ignore"))
                target_uid = str(pj.get("uid") or "").strip()
                if target_uid and target_uid in studio_engine.ACTIVE_STUDIO_ACCOUNTS:
                    target_cookie = studio_engine.ACTIVE_STUDIO_ACCOUNTS[target_uid].get("cookie", "")
        except Exception:
            pass

    b_cookie, b_ua, _ = get_borrowed_cookie()
    if not target_cookie:
        target_cookie = b_cookie

    headers = {
        "User-Agent": b_ua or DEFAULT_UA,
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    if target_cookie:
        headers["Cookie"] = target_cookie

    try:
        r = PROXY_SESSION.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and r.content:
            content_type = r.headers.get("Content-Type", "image/jpeg")
            _put_image_cache(url, r.content, content_type)
            return Response(content=r.content, media_type=content_type, headers={"Cache-Control": "public, max-age=604800, immutable"})
        else:
            # Thử lần lượt các cookie tài khoản studio đã nạp trong bộ nhớ
            if studio_engine.ACTIVE_STUDIO_ACCOUNTS:
                for acc_k, acc_v in studio_engine.ACTIVE_STUDIO_ACCOUNTS.items():
                    cand_ck = acc_v.get("cookie", "")
                    if cand_ck and cand_ck != target_cookie:
                        headers["Cookie"] = cand_ck
                        r_cand = PROXY_SESSION.get(url, headers=headers, timeout=5)
                        if r_cand.status_code == 200 and r_cand.content:
                            content_type = r_cand.headers.get("Content-Type", "image/jpeg")
                            _put_image_cache(url, r_cand.content, content_type)
                            return Response(content=r_cand.content, media_type=content_type, headers={"Cache-Control": "public, max-age=604800, immutable"})

            # Thử lại không dùng Cookie (nếu CDN public)
            if target_cookie:
                headers.pop("Cookie", None)
                r2 = PROXY_SESSION.get(url, headers=headers, timeout=6)
                if r2.status_code == 200 and r2.content:
                    content_type = r2.headers.get("Content-Type", "image/jpeg")
                    _put_image_cache(url, r2.content, content_type)
                    return Response(content=r2.content, media_type=content_type, headers={"Cache-Control": "public, max-age=604800, immutable"})
            
            # Trả về ảnh placeholder SVG nếu TikTok CDN từ chối (không lưu cache để cho phép thử lại)
            if "avt-" in url or "avatar" in url or "cropcenter" in url:
                fallback_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><circle cx="48" cy="48" r="48" fill="#0284c7"/><path d="M48 24a14 14 0 100 28 14 14 0 000-28zm0 34c-16 0-26 10-26 18v2h52v-2c0-8-10-18-26-18z" fill="#ffffff"/></svg>'
            else:
                fallback_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="128" viewBox="0 0 96 128"><rect width="100%" height="100%" fill="#0f172a"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="28" fill="#38bdf8">🎬</text></svg>'
            return Response(content=fallback_svg.encode("utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
        if "avt-" in url or "avatar" in url or "cropcenter" in url:
            fallback_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><circle cx="48" cy="48" r="48" fill="#0284c7"/><path d="M48 24a14 14 0 100 28 14 14 0 000-28zm0 34c-16 0-26 10-26 18v2h52v-2c0-8-10-18-26-18z" fill="#ffffff"/></svg>'
        else:
            fallback_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="128" viewBox="0 0 96 128"><rect width="100%" height="100%" fill="#0f172a"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="28" fill="#38bdf8">🎬</text></svg>'
        return Response(content=fallback_svg.encode("utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/api/debug_worker")
def debug_worker():
    from core_dedup import TikTokPlaywrightWorker
    w = TikTokPlaywrightWorker.get_instance()
    return {
        "is_ready": w._is_ready,
        "has_browser": bool(w.browser),
        "is_connected": bool(w.browser and w.browser.is_connected()),
        "error": getattr(w, "_init_error", None)
    }


# ==============================================================================
# TIKTOK CREATOR STUDIO & CHECK POST API
# ==============================================================================
import studio_engine

@app.post("/api/studio/qr/start")
def api_studio_qr_start():
    """Khởi tạo phiên đăng nhập QR từ TikTok Passport."""
    res = studio_engine.create_qr_session()
    return JSONResponse(content=res)

@app.get("/api/studio/qr/status")
def api_studio_qr_status(session_id: str = Query(..., description="ID phiên QR")):
    """Kiểm tra trạng thái quét mã QR của người dùng."""
    res = studio_engine.check_qr_session_status(session_id)
    return JSONResponse(content=res)

@app.get("/api/studio/profile/avatar")
def api_studio_profile_avatar(username: str = Query("", description="TikTok Username"), cookie: str = Query(None, description="Cookie (optional)")):
    """Lấy avatar và thông tin hiển thị trực tiếp cho kênh TikTok."""
    clean_u = (username or "").lstrip("@").strip()
    if not clean_u and not cookie:
        return JSONResponse(content={"success": False, "avatar": ""})
    res = studio_engine.fetch_user_avatar_and_profile(clean_u, cookie_str=cookie)
    if not res.get("avatar") and clean_u:
        av = studio_engine.fetch_user_avatar(clean_u)
        if av:
            res["avatar"] = av
            res["success"] = True
    return JSONResponse(content=res)

@app.post("/api/studio/cookie/import")
async def api_studio_cookie_import(req: Request):
    """Xác thực và nạp chuỗi Cookie tài khoản TikTok."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không hợp lệ.")
    
    cookie_str = str(body.get("cookie") or "").strip()
    raw_proxy = _mask_internal_proxy_for_client(str(body.get("proxy") or ""))
    proxy = _resolve_proxy_value(raw_proxy)
    if not cookie_str:
        raise HTTPException(status_code=400, detail="Vui lòng dán chuỗi Cookie!")
    
    acc_info = await run_in_threadpool(studio_engine.get_account_info, cookie_str, proxy=proxy)
    if not acc_info.get("valid"):
        return JSONResponse(content={"success": False, "error": acc_info.get("error", "Cookie không hợp lệ hoặc đã hết hạn.")})

    u_name = str(acc_info.get("uniqueId") or acc_info.get("username") or "").lstrip("@").strip().lower()
    if u_name:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[u_name] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": raw_proxy,
            "updated_at": time.time()
        }
    uid_key = str(acc_info.get("uid") or "").strip()
    if uid_key:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[uid_key] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": raw_proxy,
            "updated_at": time.time()
        }

    return JSONResponse(content={
        "success": True,
        "account": acc_info,
        "cookie": cookie_str
    })

@app.post("/api/studio/posts")
async def api_studio_posts(req: Request):
    """Lấy danh sách video Creator Studio (Công khai / Riêng tư / Bạn bè)."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không hợp lệ.")
    
    cookie_str = str(body.get("cookie") or "").strip()
    if not cookie_str:
        raise HTTPException(status_code=400, detail="Chưa có Cookie đăng nhập.")
    
    count = int(body.get("count") or 50)
    max_cursor = int(body.get("max_cursor") or 0)
    proxy = _resolve_proxy_value(str(body.get("proxy") or ""))
    sec_uid = str(body.get("sec_uid") or "").strip()
    
    res = await run_in_threadpool(studio_engine.get_studio_post_list, cookie_str, proxy=proxy, count=count, max_cursor=max_cursor, sec_uid=sec_uid)
    return JSONResponse(content=res)

@app.post("/api/studio/item/visibility")
async def api_studio_item_visibility(req: Request):
    """Đổi chế độ hiển thị video: 1 (Công khai), 2 (Riêng tư), 3 (Bạn bè)."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không hợp lệ.")
    
    cookie_str = str(body.get("cookie") or "").strip()
    item_id = str(body.get("item_id") or "").strip()
    visibility = int(body.get("visibility") or 1)
    proxy = _resolve_proxy_value(str(body.get("proxy") or ""))
    
    if not cookie_str or not item_id:
        raise HTTPException(status_code=400, detail="Thiếu Cookie hoặc ID video.")
    
    res = await run_in_threadpool(studio_engine.set_post_visibility, cookie_str, item_id, visibility, proxy=proxy)
    return JSONResponse(content=res)

@app.post("/api/studio/item/delete")
async def api_studio_item_delete(req: Request):
    """Xóa vĩnh viễn video khỏi kênh TikTok."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Dữ liệu JSON không hợp lệ.")
    
    cookie_str = str(body.get("cookie") or "").strip()
    item_id = str(body.get("item_id") or "").strip()
    proxy = _resolve_proxy_value(str(body.get("proxy") or ""))
    
    if not cookie_str or not item_id:
        raise HTTPException(status_code=400, detail="Thiếu Cookie hoặc ID video.")
    
    res = await run_in_threadpool(studio_engine.delete_studio_post, cookie_str, item_id, proxy=proxy)
    return JSONResponse(content=res)

@app.post("/api/studio/browser/login/start")
async def api_studio_browser_login_start(req: Request):
    """Khởi chạy trình duyệt đăng nhập (hỗ trợ HTTP & SOCKS5, chế độ QR stream hoặc mở cửa sổ desktop)."""
    proxy = ""
    force = False
    mode = "qr"
    try:
        body = await req.json()
        proxy = _resolve_proxy_value(str(body.get("proxy") or ""))
        force = bool(body.get("force", False))
        mode = str(body.get("mode") or "qr").strip().lower()
    except Exception:
        pass
    res = studio_engine.start_interactive_browser_login(proxy=proxy, force=force, mode=mode)
    return JSONResponse(content=res)

@app.get("/api/studio/browser/login/status")
def api_studio_browser_login_status():
    """Lấy trạng thái phiên đăng nhập trình duyệt tương tác."""
    res = studio_engine.get_interactive_login_status()
    return JSONResponse(content=res)

@app.post("/api/studio/browser/login/cancel")
def api_studio_browser_login_cancel():
    """Hủy phiên đăng nhập trình duyệt và đóng cửa sổ."""
    res = studio_engine.cancel_interactive_browser_login()
    return JSONResponse(content=res)

# ==============================================================================
# HỖ TRỢ ĐĂNG NHẬP 1-CLICK TỰ ĐỘNG BẰNG FILE .BAT TRÊN MÁY KHÁCH
# ==============================================================================
EXTERNAL_LOGIN_SESSIONS: dict = {}
EXTERNAL_LOGIN_LOCK = threading.Lock()
EXTERNAL_LOGIN_EVENTS: dict = {}

def _ext_log(sync_id: str, line: str, client_ip: str = ""):
    """Lưu log từng phiên đăng nhập .bat (hiện realtime trên web) + ghi ra file debug."""
    ts = time.strftime("%H:%M:%S")
    lst = EXTERNAL_LOGIN_EVENTS.setdefault(sync_id, [])
    lst.append(f"{ts} {line}")
    if len(lst) > 300:
        del lst[:-300]
    try:
        with open(Path(__file__).resolve().parent / "external_login_debug.log", "a", encoding="utf-8") as lf:
            lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ID={sync_id} IP={client_ip} {line}\n")
    except Exception:
        pass

@app.get("/api/studio/external-login/debug")
def api_studio_external_login_debug(id: str = ""):
    sync_id = str(id or "").strip()
    sess = EXTERNAL_LOGIN_SESSIONS.get(sync_id) or {}
    return JSONResponse(content={
        "id": sync_id,
        "status": sess.get("status", "not_found"),
        "client_info": sess.get("client_info", ""),
        "lines": EXTERNAL_LOGIN_EVENTS.get(sync_id, [])
    })

def _cleanup_external_sessions():
    now = time.time()
    with EXTERNAL_LOGIN_LOCK:
        expired = [k for k, v in EXTERNAL_LOGIN_SESSIONS.items() if now - v.get("created_at", 0) > 1800]
        for k in expired:
            EXTERNAL_LOGIN_SESSIONS.pop(k, None)

@app.get("/api/studio/external-login/status")
def api_studio_external_login_status(req: Request, id: str = ""):
    """Kiểm tra trạng thái đăng nhập từ file .bat chạy trên máy khách."""
    _cleanup_external_sessions()
    sync_id = str(id or "").strip()
    client_ip = req.client.host if req.client else ""

    with EXTERNAL_LOGIN_LOCK:
        sess = EXTERNAL_LOGIN_SESSIONS.get(sync_id)

        # Fallback chỉ khi KHÔNG tìm thấy phiên (vd: server restart). Không bao giờ lấy
        # phiên đã hoàn tất/đã được web nhận trước đó để tránh nhận nhầm nick cũ.
        if not sess:
            now_ts = time.time()
            for sid, s in sorted(EXTERNAL_LOGIN_SESSIONS.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True):
                if (s.get("client_ip") == client_ip
                        and not s.get("consumed")
                        and now_ts - s.get("last_seen", 0) < 900
                        and s.get("status") in ("browser_opened", "browser_closed", "syncing", "success")):
                    sess = s
                    sync_id = sid
                    break

        if not sess:
            return JSONResponse(content={"success": False, "status": "not_found"})

        if sess.get("status") == "success":
            if not sess.get("consumed"):
                _ext_log(sync_id, "[web] trang web ĐÃ NHẬN kết quả success", client_ip)
            sess["consumed"] = True

        return JSONResponse(content={
            "success": True,
            "id": sync_id,
            "status": sess.get("status", "waiting"),
            "account": sess.get("account"),
            "cookie": sess.get("cookie"),
            "proxy": _mask_internal_proxy_for_client(sess.get("proxy", "")),
            "detail": sess.get("detail", ""),
            "last_seen": sess.get("last_seen", 0)
        })

@app.post("/api/studio/external-login/event")
async def api_studio_external_login_event(req: Request):
    """File .bat chạy ẩn trên máy khách báo sự kiện (browser_opened, browser_closed) và nhận lệnh điều khiển."""
    client_ip = req.client.host if req.client else ""
    try:
        body = await req.json()
        sync_id = str(body.get("id") or "").strip()
        event = str(body.get("event") or "").strip()
        detail = str(body.get("detail") or "").strip()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    if not sync_id:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu id."})

    with EXTERNAL_LOGIN_LOCK:
        sess = EXTERNAL_LOGIN_SESSIONS.get(sync_id)
        if not sess:
            sess = {
                "status": (event if event != "client_info" else "waiting") or "waiting",
                "detail": detail,
                "command": "none",
                "client_ip": client_ip,
                "created_at": time.time(),
                "last_seen": time.time()
            }
            EXTERNAL_LOGIN_SESSIONS[sync_id] = sess
        else:
            if event == "client_info":
                sess["client_info"] = detail
            elif event and sess.get("status") != "success":
                sess["status"] = event
            if detail and event != "client_info":
                sess["detail"] = detail
            sess["client_ip"] = client_ip
            sess["last_seen"] = time.time()

        cmd = sess.get("command", "none")
        if cmd != "none":
            sess["command"] = "none"

        # Ghi log chẩn đoán từ máy khách (chỉ khi nội dung thay đổi) để debug từ xa
        log_key = f"{event}|{detail}"
        if sess.get("_last_log") != log_key:
            sess["_last_log"] = log_key
            _ext_log(sync_id, f"[bat] {event}: {detail}", client_ip)

        logger.info(f"[ExternalLogin] ID={sync_id} IP={client_ip} Event={event} Detail={detail}")
        return JSONResponse(content={"success": True, "command": cmd, "status": sess.get("status")})

@app.post("/api/studio/external-login/command")
async def api_studio_external_login_command(req: Request):
    """Web gửi lệnh điều khiển xuống tiến trình .bat đang chạy ngầm trên máy khách (ví dụ: reopen_browser)."""
    try:
        body = await req.json()
        sync_id = str(body.get("id") or "").strip()
        command = str(body.get("command") or "").strip()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    if not sync_id or not command:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu id hoặc command."})

    with EXTERNAL_LOGIN_LOCK:
        sess = EXTERNAL_LOGIN_SESSIONS.get(sync_id)
        if not sess:
            return JSONResponse(status_code=404, content={"success": False, "error": "Không tìm thấy phiên đăng nhập."})

        sess["command"] = command
        _ext_log(sync_id, f"[web] gửi lệnh: {command}")
        if command == "reopen_browser":
            sess["status"] = "waiting"

        return JSONResponse(content={"success": True, "status": sess.get("status")})

@app.post("/api/studio/external-login/sync")
async def api_studio_external_login_sync(req: Request):
    """File .bat trên máy khách gửi cookie về sau khi đăng nhập thành công trên Chrome."""
    try:
        body = await req.json()
        sync_id = str(body.get("id") or "").strip()
        cookie_str = str(body.get("cookie") or "").strip()
        raw_proxy = _mask_internal_proxy_for_client(str(body.get("proxy") or ""))
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    if not sync_id or not cookie_str:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu id hoặc cookie."})

    with EXTERNAL_LOGIN_LOCK:
        prev_sess = EXTERNAL_LOGIN_SESSIONS.get(sync_id) or {}
        if not raw_proxy and prev_sess.get("proxy"):
            raw_proxy = _mask_internal_proxy_for_client(str(prev_sess.get("proxy") or ""))
    proxy = _resolve_proxy_value(raw_proxy)

    # Chuẩn hóa cookie có cả sessionid và sessionid_ss
    cdict = studio_engine.parse_cookie_dict(cookie_str)
    sid_val = cdict.get("sessionid") or cdict.get("sessionid_ss") or cdict.get("sid_tt") or cdict.get("sid_guard")
    if sid_val:
        if "sessionid" not in cdict:
            cdict["sessionid"] = sid_val
        if "sessionid_ss" not in cdict:
            cdict["sessionid_ss"] = sid_val
        cookie_str = studio_engine.cookie_to_header_str(cdict)

    sync_ip = req.client.host if req.client else ""
    _ext_log(sync_id, f"[server] nhận cookie từ bat: {len(cookie_str)} ký tự, có sessionid={bool(sid_val)}, proxy='{raw_proxy}'", sync_ip)
    t_sync = time.time()
    acc_info = await run_in_threadpool(studio_engine.get_account_info, cookie_str, proxy=proxy)
    if not acc_info or not (acc_info.get("uid") or acc_info.get("uniqueId") or acc_info.get("username")):
        _ext_log(sync_id, f"[server] xác thực qua proxy lỗi: {(acc_info or {}).get('error', '')} -> thử không proxy")
        # Thử lại không qua proxy phòng trường hợp proxy server bị nghẽn
        if proxy:
            acc_info = await run_in_threadpool(studio_engine.get_account_info, cookie_str, proxy="")

    # Nếu Passport API tạm thời bị rate-limit hoặc chặn từ IP server, vẫn chấp nhận cookie tươi từ máy khách với profile dự phòng
    if not acc_info or not (acc_info.get("uid") or acc_info.get("uniqueId") or acc_info.get("username")):
        _ext_log(sync_id, f"[server] Passport không xác thực được ({(acc_info or {}).get('error', '')}) -> dùng profile dự phòng")
        uid_val = str(cdict.get("uid_tt") or cdict.get("uid_tt_ss") or cdict.get("user_id") or "")
        if not uid_val and sid_val:
            uid_val = str(abs(hash(sid_val)))
        if not uid_val:
            uid_val = str(int(time.time()))
        u_fallback = f"user_{uid_val[-8:]}" if len(uid_val) >= 8 else f"user_{uid_val}"
        m_c = re.search(r'(?:store-country-code|country_code|region|country)\s*=\s*([a-zA-Z]{2})', cookie_str, re.IGNORECASE)
        c_val = m_c.group(1).upper() if m_c else "US"
        acc_info = {
            "valid": True,
            "uid": uid_val,
            "username": u_fallback,
            "uniqueId": u_fallback,
            "nickname": u_fallback,
            "avatar": "",
            "country": c_val,
            "createDate": "-",
            "sec_uid": ""
        }

    if acc_info and not acc_info.get("avatar"):
        u_cand = (acc_info.get("uniqueId") or acc_info.get("username") or "").lstrip("@").strip()
        if u_cand and not u_cand.startswith("user_"):
            try:
                cand_av = await run_in_threadpool(studio_engine.fetch_user_avatar, u_cand)
                if cand_av:
                    acc_info["avatar"] = cand_av
            except Exception:
                pass

    with EXTERNAL_LOGIN_LOCK:
        _ext_log(sync_id, f"[server] SUCCESS nick=@{acc_info.get('uniqueId')} uid={acc_info.get('uid')} avatar={'OK' if acc_info.get('avatar') else 'NONE'} ({time.time()-t_sync:.1f}s) -> chờ web nhận")
        EXTERNAL_LOGIN_SESSIONS[sync_id] = {
            "status": "success",
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": raw_proxy,
            "command": "none",
            "last_seen": time.time(),
            "created_at": time.time()
        }

    # Lưu vào bộ nhớ cache studio server
    u_key = (acc_info.get("uniqueId") or acc_info.get("username", "")).lstrip("@").strip().lower()
    if u_key:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[u_key] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": raw_proxy,
            "updated_at": time.time()
        }
    uid_key = str(acc_info.get("uid") or "").strip()
    if uid_key:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[uid_key] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": raw_proxy,
            "updated_at": time.time()
        }

    return JSONResponse(content={"success": True, "account": acc_info})


@app.get("/api/studio/download-login-bat")
def api_studio_download_login_bat(req: Request, id: str = "", proxy: str = "", origin: str = ""):
    """Tạo và tải về file .bat chạy ngầm 100% ẩn cmd, tự động mở Chrome và kết nối 2 chiều với web."""
    sync_id = str(id or "").strip()
    if not sync_id:
        sync_id = f"tikflow_{int(time.time())}_{secrets.token_hex(4)}"

    client_ip = req.client.host if req.client else ""
    safe_proxy_id = _mask_internal_proxy_for_client(str(proxy or ""))

    with EXTERNAL_LOGIN_LOCK:
        EXTERNAL_LOGIN_SESSIONS[sync_id] = {
            "status": "waiting",
            "command": "none",
            "proxy": safe_proxy_id,
            "client_ip": client_ip,
            "last_seen": time.time(),
            "created_at": time.time()
        }

    clean_origin = str(origin or "").strip().rstrip("/")
    if not clean_origin:
        clean_origin = "https://checktiktok.site"
    _ext_log(sync_id, f"[server] web tải file .bat (origin={clean_origin}, proxy='{safe_proxy_id}')", client_ip)

    # Tuyệt đối không chèn IP/Port mạng nội bộ (192.168.x / 127.0.0.1 / preset:...) vào file .bat tải về máy khách
    clean_proxy = "" if (not safe_proxy_id or safe_proxy_id.startswith("preset:") or "192.168." in safe_proxy_id or "127.0.0.1" in safe_proxy_id) else safe_proxy_id

    # Mã PowerShell chạy hoàn toàn ẩn ngầm trên máy khách
    ps_code = f'''[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls11 -bor [System.Net.SecurityProtocolType]::Tls
try {{
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {{$true}}
}} catch {{}}

# Ẩn ngay cửa sổ console
try {{
    $hideDef = '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow); [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();'
    Add-Type -Name Win32 -Namespace Native -MemberDefinition $hideDef -ErrorAction SilentlyContinue
    $h = [Native.Win32]::GetConsoleWindow()
    if ($h -ne [IntPtr]::Zero) {{ [Native.Win32]::ShowWindow($h, 0) }}
}} catch {{}}

function Log-Local($msg) {{
    try {{
        $l = "[" + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "] " + $msg
        Add-Content -Path (Join-Path $env:TEMP "tikflow_login.log") -Value $l -ErrorAction SilentlyContinue
    }} catch {{}}
}}

Log-Local "Bat script started for session: {sync_id}"

$chromePaths = @(
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ($env:LOCALAPPDATA + "\\Google\\Chrome\\Application\\chrome.exe"),
    ($env:ProgramFiles + "\\Google\\Chrome\\Application\\chrome.exe"),
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    ($env:LOCALAPPDATA + "\\Microsoft\\Edge\\Application\\msedge.exe")
)

$browser = $null
foreach ($p in $chromePaths) {{
    if ($p -and (Test-Path -LiteralPath $p)) {{ $browser = $p; break }}
}}
if (-not $browser) {{
    try {{
        $c = Get-Command chrome.exe -ErrorAction SilentlyContinue
        if ($c) {{ $browser = $c.Source }}
    }} catch {{}}
}}
if (-not $browser) {{
    try {{
        $e = Get-Command msedge.exe -ErrorAction SilentlyContinue
        if ($e) {{ $browser = $e.Source }}
    }} catch {{}}
}}
if (-not $browser) {{
    Log-Local "No browser found"
}}

$global:cdpPort = Get-Random -Minimum 9300 -Maximum 9800
$tmpDir = Join-Path $env:TEMP ("tikflow_prof_" + (Get-Random))

$proxyArg = ""
if ("{clean_proxy}" -ne "") {{
    $proxyVal = "{clean_proxy}".Trim()
    $pHost = ""
    $pPort = 0
    try {{
        $pUri = [System.Uri]$proxyVal
        $pHost = $pUri.Host
        $pPort = $pUri.Port
        if ($pPort -le 0) {{ $pPort = 1080 }}
    }} catch {{
        if ($proxyVal -match "://([^:/]+):([0-9]+)") {{
            $pHost = $matches[1]
            $pPort = [int]$matches[2]
        }}
    }}
    $proxyOk = $false
    if ($pHost -and $pPort -gt 0) {{
        try {{
            $tcp = New-Object System.Net.Sockets.TcpClient
            $ar = $tcp.BeginConnect($pHost, $pPort, $null, $null)
            $wait = $ar.AsyncWaitHandle.WaitOne(1500, $false)
            if ($wait -and $tcp.Connected) {{
                $tcp.EndConnect($ar)
                $tcp.Close()
                $proxyOk = $true
            }} else {{
                $tcp.Close()
            }}
        }} catch {{}}
    }}
    if ($proxyOk) {{
        $proxyArg = "--proxy-server=$proxyVal"
    }}
}}

function Start-LoginBrowser {{
    try {{
        if (-not (Test-Path -LiteralPath $tmpDir)) {{
            New-Item -ItemType Directory -Path $tmpDir -Force -ErrorAction SilentlyContinue | Out-Null
        }}
    }} catch {{}}

    $argList = @(
        ("--remote-debugging-port=" + $global:cdpPort),
        ("--user-data-dir=`"" + $tmpDir + "`""),
        "--no-first-run",
        "--no-default-browser-check"
    )
    if ($proxyArg) {{
        $argList += $proxyArg
    }}
    $argList += "https://www.tiktok.com/login"

    $p = Start-Process -FilePath $browser -ArgumentList $argList -PassThru
    Log-Local ("Started browser PID " + $p.Id + " on port " + $global:cdpPort)
    return $p
}}

function Send-State($evt, $detail = "") {{
    try {{
        $body = @{{ id = "{sync_id}"; event = $evt; detail = $detail }} | ConvertTo-Json
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        
        $req = [System.Net.WebRequest]::Create("{clean_origin}/api/studio/external-login/event")
        $req.Method = "POST"
        $req.ContentType = "application/json; charset=utf-8"
        $req.Timeout = 5000
        $req.ContentLength = $bytes.Length
        
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
        
        $resp = $req.GetResponse()
        $respStream = $resp.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($respStream, [System.Text.Encoding]::UTF8)
        $respText = $reader.ReadToEnd()
        $reader.Close()
        $resp.Close()
        
        $json = $respText | ConvertFrom-Json
        return $json.command
    }} catch {{
        Log-Local ("Send-State error: " + $_.Exception.Message)
        return "none"
    }}
}}

function Send-Sync($cookieStr, $proxyStr) {{
    try {{
        $body = @{{ id = "{sync_id}"; cookie = $cookieStr; proxy = $proxyStr }} | ConvertTo-Json
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        
        $req = [System.Net.WebRequest]::Create("{clean_origin}/api/studio/external-login/sync")
        $req.Method = "POST"
        $req.ContentType = "application/json; charset=utf-8"
        $req.Timeout = 35000
        $req.ContentLength = $bytes.Length
        
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
        
        $resp = $req.GetResponse()
        $respStream = $resp.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($respStream, [System.Text.Encoding]::UTF8)
        $respText = $reader.ReadToEnd()
        $reader.Close()
        $resp.Close()
        
        $json = $respText | ConvertFrom-Json
        if ($json.success -ne $true) {{ $global:syncErr = "server tra ve: " + $respText.Substring(0, [Math]::Min(200, $respText.Length)) }}
        return ($json.success -eq $true)
    }} catch {{
        $global:syncErr = $_.Exception.GetBaseException().Message
        Log-Local ("Send-Sync error: " + $global:syncErr)
        return $false
    }}
}}

function Get-BrowserCookiesViaCDP {{
    $cookies = $null
    $global:cdpErr = ""
    try {{
        $ver = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $global:cdpPort + "/json/version") -TimeoutSec 2
        $wsUrl = $ver.webSocketDebuggerUrl
        if ($wsUrl) {{
            $w = New-Object System.Net.WebSockets.ClientWebSocket
                    try {{ $w.Options.Proxy = $null }} catch {{}}
            $cts = New-Object System.Threading.CancellationTokenSource(9000)
            $w.ConnectAsync([System.Uri]$wsUrl, $cts.Token).Wait(6000)
            if ($w.State -eq [System.Net.WebSockets.WebSocketState]::Open) {{
                $msg = '{{"id":1,"method":"Storage.getCookies"}}'
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($msg)
                $seg = New-Object System.ArraySegment[byte] -ArgumentList @(,$bytes)
                $w.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).Wait(2000)

                $mem = New-Object System.IO.MemoryStream
                do {{
                    $buf = New-Object byte[] 65536
                    $rseg = New-Object System.ArraySegment[byte] -ArgumentList @(,$buf)
                    $rtask = $w.ReceiveAsync($rseg, $cts.Token)
                    if (-not $rtask.Wait(6000)) {{ $global:cdpErr = "ws receive timeout"; break }}
                    if ($rtask.Result.Count -gt 0) {{ $mem.Write($buf, 0, $rtask.Result.Count) }}
                }} while (-not $rtask.Result.EndOfMessage)

                $respStr = [System.Text.Encoding]::UTF8.GetString($mem.ToArray())
                $data = $respStr | ConvertFrom-Json
                try {{
                    $w.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", [System.Threading.CancellationToken]::None).Wait(500)
                    $w.Dispose()
                }} catch {{}}
                if ($data.result -and $data.result.cookies) {{
                    $cookies = $data.result.cookies
                }}
            }} else {{
                $global:cdpErr = "browser ws not open: " + $w.State
                try {{ $w.Dispose() }} catch {{}}
            }}
        }} else {{
            $global:cdpErr = "no webSocketDebuggerUrl"
        }}
    }} catch {{
        $global:cdpErr = "browser target: " + $_.Exception.GetBaseException().Message
    }}

    # Fallback: Nếu Browser target không trả về cookies, lấy qua Page target Network.getCookies
    if (-not $cookies) {{
        try {{
            $pages = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $global:cdpPort + "/json/list") -TimeoutSec 2
            foreach ($pg in $pages) {{
                if ($pg.type -eq "page" -and $pg.webSocketDebuggerUrl) {{
                    $w = New-Object System.Net.WebSockets.ClientWebSocket
                    try {{ $w.Options.Proxy = $null }} catch {{}}
                    $cts = New-Object System.Threading.CancellationTokenSource(9000)
                    $w.ConnectAsync([System.Uri]$pg.webSocketDebuggerUrl, $cts.Token).Wait(6000)
                    if ($w.State -eq [System.Net.WebSockets.WebSocketState]::Open) {{
                        $msg = '{{"id":2,"method":"Network.getCookies"}}'
                        $bytes = [System.Text.Encoding]::UTF8.GetBytes($msg)
                        $seg = New-Object System.ArraySegment[byte] -ArgumentList @(,$bytes)
                        $w.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).Wait(2000)

                        $mem = New-Object System.IO.MemoryStream
                        do {{
                            $buf = New-Object byte[] 65536
                            $rseg = New-Object System.ArraySegment[byte] -ArgumentList @(,$buf)
                            $rtask = $w.ReceiveAsync($rseg, $cts.Token)
                            if (-not $rtask.Wait(6000)) {{ $global:cdpErr = "ws receive timeout"; break }}
                            if ($rtask.Result.Count -gt 0) {{ $mem.Write($buf, 0, $rtask.Result.Count) }}
                        }} while (-not $rtask.Result.EndOfMessage)

                        $respStr = [System.Text.Encoding]::UTF8.GetString($mem.ToArray())
                        $data = $respStr | ConvertFrom-Json
                        try {{
                            $w.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", [System.Threading.CancellationToken]::None).Wait(500)
                            $w.Dispose()
                        }} catch {{}}
                        if ($data.result -and $data.result.cookies) {{
                            $cookies = $data.result.cookies
                            break
                        }}
                    }} else {{
                        $global:cdpErr += " | page ws not open: " + $w.State
                        try {{ $w.Dispose() }} catch {{}}
                    }}
                }}
            }}
        }} catch {{
            $global:cdpErr += " | page target: " + $_.Exception.GetBaseException().Message
        }}
    }}
    return $cookies
}}

if (-not $browser) {{
    Send-State "browser_not_found" "Không tìm thấy Chrome/Edge trên máy tính" | Out-Null
    exit 1
}}

$proc = Start-LoginBrowser
$global:diag = "Chrome đang mở, đang chờ đăng nhập..."
$global:lastDiag = ""
$osInfo = ""
try {{ $osInfo = [Environment]::OSVersion.VersionString + " PS" + $PSVersionTable.PSVersion.ToString() + " CLR" + [Environment]::Version.ToString() }} catch {{}}
$sysProxy = ""
try {{ $sysProxy = [System.Net.WebRequest]::DefaultWebProxy.GetProxy([Uri]"http://127.0.0.1:9222/").ToString() }} catch {{}}
Send-State "client_info" ("browser=" + $browser + " | " + $osInfo + " | proxy127=" + $sysProxy + " | chromeProxyArg=" + $proxyArg) | Out-Null
Send-State "browser_opened" "Đã khởi động Chrome trên cổng $global:cdpPort"
$startTs = [DateTime]::UtcNow
$synced = $false

while (([DateTime]::UtcNow - $startTs).TotalSeconds -lt 900) {{
    if ($synced) {{ break }}

    $isClosed = $false
    try {{
        if (-not $proc -or $proc.HasExited) {{
            $isClosed = $true
        }} else {{
            try {{
                $ping = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $global:cdpPort + "/json/version") -TimeoutSec 1 -ErrorAction Stop
                if (-not $ping.webSocketDebuggerUrl) {{ $isClosed = $true }}
            }} catch {{
                $isClosed = $true
            }}
        }}
    }} catch {{
        $isClosed = $true
    }}

    if ($isClosed) {{
        $cmd = Send-State "browser_closed" "Cửa sổ Chrome đã bị đóng"
        if ($cmd -eq "reopen_browser") {{
            $proc = Start-LoginBrowser
            Send-State "browser_opened" "Đã mở lại Chrome theo yêu cầu"
        }} elseif ($cmd -eq "exit") {{
            break
        }}
        Start-Sleep -Seconds 1
        continue
    }}

    # Kiểm tra lệnh điều khiển từ web (kèm dòng chẩn đoán đọc cookie lần trước)
    $cmd = Send-State "browser_opened" $global:diag
    if ($cmd -eq "exit") {{
        try {{ Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }} catch {{}}
        break
    }}

    # Đọc cookies thông qua CDP
    $cookies = Get-BrowserCookiesViaCDP
    if (-not $cookies) {{
        $global:diag = "CDP_FAIL: " + $global:cdpErr
    }} else {{
        $tkNames = @($cookies | Where-Object {{ $_.domain -like "*tiktok*" }} | ForEach-Object {{ $_.name }})
        $global:diag = "CDP_OK cookies=" + @($cookies).Count + " tiktok=" + $tkNames.Count + " names=" + (($tkNames | Select-Object -First 40) -join ",")
    }}
    if ($global:diag -ne $global:lastDiag) {{
        Log-Local $global:diag
        $global:lastDiag = $global:diag
    }}

    if ($cookies) {{
        $sidCookie = $cookies | Where-Object {{ 
            ($_.name -eq "sessionid" -or $_.name -eq "sessionid_ss" -or $_.name -eq "sid_tt" -or $_.name -eq "sid_guard") -and $_.value -and ($_.value.ToString().Trim().Length -gt 8)
        }} | Select-Object -First 1

        if ($sidCookie) {{
            $cookiePairs = @()
            $hasSessionId = $false
            $hasSessionIdSs = $false
            $sidVal = ""
            foreach ($c in $cookies) {{
                if ($c.name -and $c.value) {{
                    $cookiePairs += ($c.name + "=" + $c.value)
                    if ($c.name -eq "sessionid") {{ $hasSessionId = $true; $sidVal = $c.value }}
                    if ($c.name -eq "sessionid_ss") {{ $hasSessionIdSs = $true; if (-not $sidVal) {{ $sidVal = $c.value }} }}
                    if ($c.name -eq "sid_tt" -and -not $sidVal) {{ $sidVal = $c.value }}
                }}
            }}
            if (-not $hasSessionId -and $sidVal) {{
                $cookiePairs += ("sessionid=" + $sidVal)
            }}
            if (-not $hasSessionIdSs -and $sidVal) {{
                $cookiePairs += ("sessionid_ss=" + $sidVal)
            }}
            $fullCookieStr = $cookiePairs -join "; "

            Send-State "syncing" "Đã phát hiện Cookie đăng nhập. Đang đồng bộ nick..."
            Log-Local ("Found session cookie! Sending sync for session: " + $sidVal.Substring(0, [Math]::Min(10, $sidVal.Length)) + "...")

            $syncOk = Send-Sync $fullCookieStr "{clean_proxy}"
            if (-not $syncOk) {{
                $syncOk = Send-Sync $fullCookieStr ""
            }}

            if ($syncOk) {{
                $synced = $true
                Send-State "success" "Đồng bộ tài khoản thành công!"
                Log-Local "Sync success reported to web!"
                break
            }} else {{
                $global:diag = "SYNC_FAIL: " + $global:syncErr
                Log-Local $global:diag
            }}
        }}
    }}

    Start-Sleep -Seconds 1
}}

try {{
    if ($proc -and -not $proc.HasExited) {{
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }}
}} catch {{}}

try {{
    Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}} catch {{}}
exit 0
'''

    bat_header = '''<# :
@echo off
set "SCRIPT_FILE=%~f0"
REM Windows 11 24H2+ da go VBScript: chi dung VBS khi may con vbscript.dll, khong thi goi thang PowerShell an
if not exist "%windir%\\System32\\vbscript.dll" goto :NO_VBS
set "V=%TEMP%\\tf_hide_%RANDOM%.vbs"
echo Set W = CreateObject("WScript.Shell") > "%V%"
echo W.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ""$p = [Environment]::GetEnvironmentVariable('SCRIPT_FILE'); if (Test-Path -LiteralPath $p) { [ScriptBlock]::Create((Get-Content -LiteralPath $p -Encoding UTF8 -Raw)).Invoke() }""", 0, False >> "%V%"
wscript.exe //B "%V%"
if errorlevel 1 goto :NO_VBS
timeout /t 1 /nobreak >nul 2>&1
del /f /q "%V%" >nul 2>&1
exit
:NO_VBS
if defined V del /f /q "%V%" >nul 2>&1
start "" powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "$p = [Environment]::GetEnvironmentVariable('SCRIPT_FILE'); if (Test-Path -LiteralPath $p) { [ScriptBlock]::Create((Get-Content -LiteralPath $p -Encoding UTF8 -Raw)).Invoke() }"
exit
#>

'''
    # Bat bắt buộc CRLF (LF-only làm goto/label của cmd chạy sai)
    bat_content = (bat_header + ps_code).replace("\r\n", "\n").replace("\n", "\r\n")

    return Response(
        content=bat_content,
        media_type="application/x-bat",
        headers={
            "Content-Disposition": 'attachment; filename="tikflow_login.bat"',
            "Cache-Control": "no-cache, no-store, must-revalidate"
        }
    )
    bat_content = bat_header + ps_code

    return Response(
        content=bat_content,
        media_type="application/x-bat",
        headers={
            "Content-Disposition": 'attachment; filename="tikflow_login.bat"',
            "Cache-Control": "no-cache, no-store, must-revalidate"
        }
    )

@app.get("/api/studio/pia/proxies")
def api_studio_pia_proxies():
    """Lấy danh sách Proxy vùng cho UI mà tuyệt đối không lộ IP/Port mạng nội bộ."""
    proxies = []

    # 1. Cụm Router VPN chuyên dụng của Tool (ẩn hoàn toàn IP & Port LAN nội bộ, chỉ gửi preset ID)
    for item in _ROUTER_PROXY_TARGETS:
        proxies.append({
            "proxy_id": item["id"],
            "socks5_url": item["id"],
            "region_id": item["name"],
            "region_name": item["name"],
            "country": item["country"],
            "group": item["group"],
            "is_default": item.get("default", False),
        })

    # 2. Cụm Local PC (Châu Á)
    try:
        sys.path.insert(0, "E:/Tool/pia_socks5")
        import pia_proxy
        active = pia_proxy.load_active_proxies()
        sorted_ports = sorted(active.keys(), key=lambda x: int(x))
        for port in sorted_ports:
            info = active[port]
            if str(info.get("country", "")).strip().upper() == "HK":
                continue
            p_int = int(port)
            r_name = re.sub(r"\s*\(?\b" + re.escape(str(p_int)) + r"\b\)?", "", str(info.get("region_name", ""))).strip() or f"Châu Á #{len(proxies) + 1}"
            p_id = f"preset:pia_{p_int}"
            proxies.append({
                "proxy_id": p_id,
                "socks5_url": p_id,
                "region_id": info.get("region_id", ""),
                "region_name": r_name,
                "country": info.get("country", ""),
                "group": "🌏 Châu Á (Singapore, Nhật Bản, Đài Loan)",
            })
    except Exception:
        pass

    return JSONResponse(content={"success": True, "proxies": proxies})

@app.post("/api/studio/browser/login")
def api_studio_browser_login():
    """Tương thích ngược: bắt đầu phiên đăng nhập trình duyệt."""
    res = studio_engine.start_interactive_browser_login()
    return JSONResponse(content=res)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
