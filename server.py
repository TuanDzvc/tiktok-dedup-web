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
from typing import Optional, Dict, Any
from pathlib import Path

import uvicorn
import asyncio
from io import BytesIO
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# Import engine trích xuất
import core_dedup
from core_dedup import fetch_channel_info_and_posts, DEFAULT_UA, get_borrowed_cookie
import studio_engine

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
ENV_FILE = BASE_DIR / ".env"
_VIP_LOCK = threading.Lock()
_PEER_SYNC_TIMER_LOCK = threading.Lock()
_PEER_SYNC_TIMER: Optional[threading.Timer] = None
_PEER_SYNC_RUN_LOCK = threading.Lock()
_APPLYING_REMOTE_SYNC = False
RENDER_SYNC_BASE = (os.environ.get("RENDER_SYNC_BASE") or "https://tiktok-dedup-web.onrender.com").rstrip("/")
PRIMARY_SYNC_BASE = (os.environ.get("PRIMARY_SYNC_BASE") or "https://checktiktok.site").rstrip("/")
SUPABASE_SYNC_BUCKET = "tikflow-vip-vault"
SUPABASE_SYNC_OBJECT = "sync_snapshot.json"
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
        _schedule_peer_sync_push(1.0)
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


def _consume_free_quota(dev_id: str, username: str = "") -> Dict[str, Any]:
    day_k = _vn_date_key()
    dev_k = f"dev:{dev_id}"
    usr_k = f"usr:{username.strip().lower()}" if username else ""
    with _QUOTA_LOCK:
        qmap = _load_free_quota_unlocked()
        bucket = qmap.get(day_k)
        if not isinstance(bucket, dict):
            bucket = {}
            qmap[day_k] = bucket
        dev_used = int(bucket.get(dev_k) or 0)
        usr_used = int(bucket.get(usr_k) or 0) if usr_k else 0
        new_used = max(dev_used, usr_used) + 1
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
    if dev_id not in devs and max_devs > 0:
        if len(active_ids) >= max_devs:
            devs[dev_id] = {
                "id": dev_id,
                "label": dev_label,
                "ip": client_ip,
                "last_seen": now_ts,
                "blocked": True,
                "auto_limit": True,
            }
            u_rec["updated_at"] = now_ts
            if max_devs == 1:
                return "⛔ Tài khoản chỉ được dùng trên 1 mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"
            return f"⛔ Tài khoản chỉ được dùng tối đa {max_devs} mã máy để tránh share tài khoản. Nếu muốn đăng nhập thêm ở máy khác vui lòng liên hệ Admin!"

    devs[dev_id] = {
        "id": dev_id,
        "label": dev_label or prev.get("label") or ("PC Admin" if is_super_admin else ""),
        "ip": client_ip,
        "last_seen": now_ts,
        "blocked": False,
    }
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
    """Xác nhận thanh toán & nâng cấp tài khoản lên VIP idempotent dưới _VIP_LOCK."""
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
    days = max(1, int(ord_rec.get("duration_days") or 30))
    old_role = str(u_rec.get("role") or "member").lower()
    old_exp = int(u_rec.get("expires_at") or 0)

    if old_role != "admin":
        u_rec["role"] = "vip"
        u_rec["active"] = True
        if u_rec.get("max_devices") is None:
            u_rec["max_devices"] = _default_max_devices_for_user(uname)
        if old_role == "vip" and old_exp <= 0:
            u_rec["expires_at"] = 0
        else:
            base_ts = max(now_ts, old_exp) if (old_role == "vip" and old_exp > now_ts) else now_ts
            u_rec["expires_at"] = base_ts + days * 86400
        old_note = str(u_rec.get("note") or "").strip()
        if not old_note or old_note == "Tự đăng ký trên web":
            u_rec["note"] = f"VIP QR PayOS ({req_amount:,}đ/{days}N)"
        u_rec["updated_at"] = now_ts

    ord_rec["status"] = "PAID"
    ord_rec["amount_paid"] = int(amount_paid)
    ord_rec["paid_at"] = now_ts
    _save_vip_orders_unlocked(orders)
    _save_vip_users_unlocked(users)
    return {"ok": True, "already_paid": False, "newly_paid": True, "order": ord_rec, "user": u_rec}


def _public_order_view(ord_rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_code": int(ord_rec.get("order_code") or 0),
        "username": str(ord_rec.get("username") or ""),
        "amount": int(ord_rec.get("amount") or 0),
        "duration_days": int(ord_rec.get("duration_days") or 30),
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


def _load_sync_meta_unlocked() -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {"deleted_users": {}, "unblocked_devices": {}}
    if VIP_SYNC_META_FILE.exists():
        try:
            raw = json.loads(VIP_SYNC_META_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("deleted_users"), dict):
                    out["deleted_users"] = {str(k).lower(): int(v or 0) for k, v in raw["deleted_users"].items() if k}
                if isinstance(raw.get("unblocked_devices"), dict):
                    out["unblocked_devices"] = {str(k).upper(): int(v or 0) for k, v in raw["unblocked_devices"].items() if k}
        except Exception:
            pass
    return out


def _save_sync_meta_unlocked(meta: Dict[str, Any]) -> None:
    try:
        cleaned = {
            "deleted_users": dict(sorted((meta.get("deleted_users") or {}).items(), key=lambda kv: -int(kv[1] or 0))[:500]),
            "unblocked_devices": dict(sorted((meta.get("unblocked_devices") or {}).items(), key=lambda kv: -int(kv[1] or 0))[:500]),
        }
        tmp_f = VIP_SYNC_META_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_SYNC_META_FILE)
    except Exception as e:
        logger.error(f"[PeerSync] Lỗi lưu vip_sync_meta.json: {e}")


def _user_record_timestamp(u_rec: Dict[str, Any]) -> int:
    if not isinstance(u_rec, dict):
        return 0
    ts = int(u_rec.get("updated_at") or u_rec.get("created_at") or 0)
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
    return {
        "users": users,
        "blocked_devices": bmap,
        "orders": orders,
        "payos_config": payos_cfg,
        "free_quota": qmap,
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

    r_users = remote.get("users") if isinstance(remote.get("users"), dict) else {}
    r_bmap = remote.get("blocked_devices") if isinstance(remote.get("blocked_devices"), dict) else {}
    r_orders = remote.get("orders") if isinstance(remote.get("orders"), dict) else {}
    r_pcfg = remote.get("payos_config") if isinstance(remote.get("payos_config"), dict) else {}
    r_qmap = remote.get("free_quota") if isinstance(remote.get("free_quota"), dict) else {}
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

    m_meta = {"deleted_users": m_del, "unblocked_devices": m_unblk}

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
            f"{sb_cfg['url']}/storage/v1/object/{SUPABASE_SYNC_BUCKET}/{SUPABASE_SYNC_OBJECT}",
            headers=_supabase_headers(sb_cfg),
            timeout=10,
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
    }
    payload_bytes = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    obj_url = f"{sb_cfg['url']}/storage/v1/object/{SUPABASE_SYNC_BUCKET}/{SUPABASE_SYNC_OBJECT}"
    try:
        r = requests.post(obj_url, headers=hdrs, data=payload_bytes, timeout=12)
        if r.status_code in (400, 404):
            if _supabase_ensure_bucket_sync(sb_cfg):
                r = requests.post(obj_url, headers=hdrs, data=payload_bytes, timeout=12)
        return r.status_code in (200, 201)
    except Exception as exc:
        logger.warning(f"[SupabaseSync] Lỗi ghi snapshot: {exc}")
        return False


def _sync_two_way_with_peer_sync(push_after_pull: bool = True) -> Dict[str, Any]:
    """Đồng bộ 2 chiều qua Supabase Storage (bền vững) + HTTP Peer PC ⇄ Render."""
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
                if sb_remote is not None:
                    sb_pulled = True
                    with _VIP_LOCK:
                        _merge_sync_state_unlocked(sb_remote)
                if push_after_pull or sb_remote is None:
                    with _VIP_LOCK:
                        snap_sb = _export_sync_snapshot_unlocked()
                    if _supabase_push_snapshot_sync(snap_sb, sb_cfg):
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
                    _supabase_push_snapshot_sync(snap_after_peer, sb_cfg)

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
    """Đẩy ngay thay đổi tài khoản sang server đối diện sau 0.4s debounce."""
    global _PEER_SYNC_TIMER
    if _APPLYING_REMOTE_SYNC or os.getenv("PYTEST_CURRENT_TEST"):
        return

    def _run_bg():
        _sync_two_way_with_peer_sync(push_after_pull=True)

    with _PEER_SYNC_TIMER_LOCK:
        if _PEER_SYNC_TIMER is not None:
            try:
                _PEER_SYNC_TIMER.cancel()
            except Exception:
                pass
        t = threading.Timer(max(0.1, float(delay_sec)), _run_bg)
        t.daemon = True
        t.start()
        _PEER_SYNC_TIMER = t


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


def _has_vip_privilege(u: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(u, dict) or not u.get("active", True):
        return False
    role = str(u.get("role") or "member").lower()
    if role == "admin":
        return True
    if role != "vip":
        return False
    exp_at = int(u.get("expires_at") or 0)
    return exp_at <= 0 or exp_at >= int(time.time())


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
    out = {
        "username": u.get("username"),
        "role": u.get("role", "member"),
        "is_vip": _has_vip_privilege(u),
        "active": bool(u.get("active", True)),
        "expires_at": int(u.get("expires_at") or 0),
        "note": str(u.get("note") or ""),
        "scan_count": int(u.get("scan_count") or 0),
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

    # Chỉ bật cloudflare tunnel trên máy tính local, không bật khi chạy trên cloud (Render/VPS)
    if not _is_cloud_runtime():
        t = threading.Thread(target=start_cloudflare_tunnel, args=(int(os.getenv("PORT", 8000)),), daemon=True)
        t.start()

    def _peer_sync_loop():
        time.sleep(2.5)
        while True:
            _sync_two_way_with_peer_sync(push_after_pull=True)
            time.sleep(60)

    threading.Thread(target=_peer_sync_loop, daemon=True).start()

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
        if not has_vip:
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
        return JSONResponse(status_code=401, content={"success": False, "error": "Vui lòng đăng ký hoặc đăng nhập tài khoản trước khi lấy mã QR kích hoạt VIP."})

    cfg = _get_payos_config()
    if not cfg.get("configured"):
        return JSONResponse(status_code=503, content={"success": False, "error": "Hệ thống thanh toán PayOS chưa được cấu hình. Vui lòng liên hệ Admin qua Zalo/Facebook!"})

    uname = str(u_obj["username"]).strip().lower()
    if str(u_obj.get("role") or "").lower() == "admin" or (str(u_obj.get("role") or "").lower() == "vip" and int(u_obj.get("expires_at") or 0) <= 0):
        return JSONResponse(status_code=400, content={"success": False, "error": "Tài khoản của bạn đã có quyền VIP Vĩnh Viễn rồi!"})

    force_new = bool(body.get("force_new", False))
    amount = int(cfg["price_vnd"])
    days = int(cfg["duration_days"])
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
                    "message": "🎉 Đơn trước đó đã thanh toán xong! Tài khoản VIP đã được kích hoạt.",
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
    desc = f"VIP{order_code}"
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
        "items": [{"name": f"Goi VIP {days} ngay ({uname})"[:75], "quantity": 1, "price": amount}],
        "signature": _payos_sign_data(sign_payload, cfg["checksum_key"]),
    }

    try:
        pay_res = await asyncio.to_thread(_create_payos_payment_sync, payload, cfg)
    except Exception as exc:
        logger.error(f"[PayOS] Lỗi tạo đơn QR cho {uname}: {exc}")
        return JSONResponse(status_code=502, content={"success": False, "error": f"Không tạo được mã QR PayOS: {exc}"})

    qr_str = str(pay_res.get("qrCode") or "").strip()
    bank_bin = str(pay_res.get("bin") or "").strip()
    qr_uri = _build_qr_data_uri(qr_str)
    ord_rec = {
        "order_code": order_code,
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
_TIKHUB_BAL_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _get_tikhub_live_balance(force_refresh: bool = False) -> Dict[str, Any]:
    """Lấy số dư TikHub thực tế ($0.0000 miễn phí qua get_user_info) — chỉ phục vụ Admin."""
    now_t = time.time()
    cached = _TIKHUB_BAL_CACHE.get("data")
    if not force_refresh and isinstance(cached, dict) and (now_t - float(_TIKHUB_BAL_CACHE.get("ts") or 0.0)) < 15:
        return cached
    api_key = core_dedup._get_tikhub_api_key()
    if not api_key:
        return {"balance_usd": 0.0, "free_credit_usd": 0.0, "total_usd": 0.0, "total_vnd": 0, "est_scans_left": 0, "usd_rate": _TIKHUB_USD_TO_VND}
    try:
        r = requests.get(
            "https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            ud = (r.json() or {}).get("user_data") or {}
            bal = round(float(ud.get("balance") or 0.0), 4)
            free_c = round(float(ud.get("free_credit") or 0.0), 4)
            tot = round(bal + free_c, 4)
            info = {
                "balance_usd": bal,
                "free_credit_usd": free_c,
                "total_usd": tot,
                "total_vnd": int(round(tot * _TIKHUB_USD_TO_VND)),
                "est_scans_left": int(tot / 0.001),
                "usd_rate": _TIKHUB_USD_TO_VND,
            }
            _TIKHUB_BAL_CACHE["ts"] = now_t
            _TIKHUB_BAL_CACHE["data"] = info
            return info
    except Exception:
        pass
    if isinstance(cached, dict):
        return cached
    return {"balance_usd": 0.0, "free_credit_usd": 0.0, "total_usd": 0.0, "total_vnd": 0, "est_scans_left": 0, "usd_rate": _TIKHUB_USD_TO_VND}


@app.get("/api/vip/admin/api-balance")
async def api_vip_admin_balance(req: Request, refresh: int = Query(0)):
    u_obj = _extract_vip_user(req)
    if not _has_vip_privilege(u_obj):
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ tài khoản VIP hoặc Admin mới xem được số dư API."})
    import asyncio
    info = await asyncio.to_thread(_get_tikhub_live_balance, bool(refresh))
    return JSONResponse(content={"success": True, "billing": info})


@app.get("/api/vip/admin/users")
async def api_vip_admin_list_users(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj or u_obj.get("role") != "admin":
        return JSONResponse(status_code=403, content={"success": False, "error": "Chỉ Admin mới có quyền quản lý tài khoản VIP."})
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        out = [_public_vip_user(v, bmap, include_password=True) for v in users.values() if isinstance(v, dict)]
    out.sort(key=lambda x: (0 if x["role"] == "admin" else 1, -x["created_at"]))
    return JSONResponse(content={"success": True, "users": out})


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
    max_devs_in = body.get("max_devices")

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
        if eff_role == "member":
            exp_ts = 0
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
    {"id": "preset:us_la", "port": 11101, "name": "California (Los Angeles)", "country": "US", "host": "192.168.2.101", "default": True, "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_nyc", "port": 11104, "name": "New York (NYC)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_sv", "port": 11102, "name": "Silicon Valley", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_lv", "port": 11103, "name": "Las Vegas", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_dc", "port": 11105, "name": "Washington DC", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:us_la2", "port": 11110, "name": "Los Angeles (#2)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
    {"id": "preset:de_fra", "port": 11108, "name": "Frankfurt", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
    {"id": "preset:de_ber", "port": 11109, "name": "Berlin", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
    {"id": "preset:mx_mex", "port": 11107, "name": "Mexico City", "country": "MX", "host": "192.168.2.101", "group": "🇲🇽 Mexico"},
    {"id": "preset:id_jkt", "port": 11106, "name": "Jakarta", "country": "ID", "host": "192.168.2.101", "group": "🇮🇩 Indonesia"},
]
_ROUTER_PROXY_MAP = {item["id"]: item for item in _ROUTER_PROXY_TARGETS}


def _resolve_proxy_value(proxy_val: str) -> str:
    """Giải mã mã proxy nội bộ (preset:...) sang địa chỉ SOCKS5 thực chỉ khi chạy tại Backend Local."""
    s = str(proxy_val or "").strip()
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
                has_vip_access = _has_vip_privilege(u_rec)
                if has_vip_access and max_cursor == 0:
                    u_rec["scan_count"] = int(u_rec.get("scan_count") or 0) + 1
                _save_vip_users_unlocked(users_db)
        elif dev_id in bmap_db:
            return JSONResponse(
                status_code=403,
                content={"success": False, "device_blocked": True, "error": f"⛔ Mã máy {dev_id} đã bị Quản trị viên khóa truy cập!"},
            )

    allow_paid_dedup = True
    dedup_locked = False
    q_stat = None

    if not has_vip_access:
        if not re.match(r"^DEV-[0-9A-Z]{6,12}$", raw_explicit_dev):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Không xác thực được Mã máy thiết bị. Vui lòng tải lại trang web (Ctrl+F5) và thử lại!"},
            )
        q_stat = _get_free_quota_status(dev_id, checked_username)
        if q_stat["used"] >= FREE_DAILY_LIMIT:
            allow_paid_dedup = False
            dedup_locked = True

    force_ext = query.startswith("!")
    if force_ext:
        cookie = ""
    clean_user = extract_clean_username(query.lstrip("!"))

    # Nếu client chưa truyền cookie, tự động tra cứu trong ACTIVE_STUDIO_ACCOUNTS
    if not cookie and clean_user and not force_ext:
        acc_entry = studio_engine.ACTIVE_STUDIO_ACCOUNTS.get(clean_user.lower())
        if acc_entry:
            cookie = acc_entry.get("cookie", "")
            if not proxy and acc_entry.get("proxy"):
                proxy = acc_entry["proxy"]
            if not sec_uid and acc_entry.get("account", {}).get("sec_uid"):
                sec_uid = acc_entry["account"]["sec_uid"]

    # Ghi nhận người dùng kiểm tra ngầm về đúng trang tính Google Sheet theo tài khoản đã đăng ký (lần quét đầu max_cursor == 0)
    if max_cursor == 0:
        client_ip = get_client_ip(req)
        if clean_user:
            record_user_check_async(clean_user, client_ip, account=checked_username, device_id=dev_id)

    # Chạy phân tích trong thread riêng:
    # - 10 lượt đầu Free/ngày theo Mã máy: mở khóa 100% tính năng (kể cả Check Trùng & Video Gốc).
    # - Sau khi hết 10 lượt: vẫn cho check bình thường (0đ) thông tin User, Video, Shadowban; khóa cột Trùng lặp & Án phạt bằng nút VIP.
    import asyncio
    res = await asyncio.to_thread(
        fetch_channel_info_and_posts,
        query,
        sec_uid=sec_uid,
        max_cursor=max_cursor,
        cookie_str=cookie,
        proxy_str="",
        max_videos=max_videos,
        allow_paid_groupid=allow_paid_dedup,
    )

    internal_cost = res.pop("_internal_cost", None) if isinstance(res, dict) else None
    if isinstance(res, dict) and res.get("success"):
        if has_vip_access:
            res["vip_mode"] = True
            res["dedup_locked"] = False
            cost_obj = internal_cost if isinstance(internal_cost, dict) else {}
            c_usd = round(float(cost_obj.get("cost_usd") or 0.0), 4)
            c_calls = int(cost_obj.get("api_calls") or 0)
            c_cached = int(cost_obj.get("cached_hits") or 0)
            c_vnd = int(round(c_usd * _TIKHUB_USD_TO_VND))
            bal_info = await asyncio.to_thread(_get_tikhub_live_balance, c_calls > 0)
            res["admin_billing"] = {
                "scan_cost_usd": c_usd,
                "scan_cost_vnd": c_vnd,
                "api_calls": c_calls,
                "cached_hits": c_cached,
                "balance_usd": bal_info["total_usd"],
                "balance_vnd": bal_info["total_vnd"],
                "est_scans_left": bal_info["est_scans_left"],
                "usd_rate": _TIKHUB_USD_TO_VND,
            }
        elif not dedup_locked:
            res["vip_mode"] = False
            res["dedup_locked"] = False
            res["free_quota"] = _consume_free_quota(dev_id, checked_username)
        else:
            res["vip_mode"] = False
            res["dedup_locked"] = True
            res["free_quota"] = q_stat
            for v_item in (res.get("list") or []):
                if isinstance(v_item, dict):
                    v_item["_dedup_locked"] = True
                    v_item["is_dup"] = False
                    v_item["group_id"] = ""
                    v_item["groupId"] = ""
                    v_item["origin_url"] = ""
                    v_item["has_origin_link"] = False
                    v_item["is_crp_punished"] = False
                    v_item["has_penalty"] = False
                    v_item["is_fyp_restricted"] = False
                    v_item["penalty_details"] = ""
                    v_item["dup_reason"] = ""
            if isinstance(res.get("stats_summary"), dict):
                res["stats_summary"]["dup_count"] = 0
                res["stats_summary"]["clean_count"] = 0

    # Kích hoạt tải trước ảnh bìa vào RAM cache song song trong nền (0.001s khi trình duyệt yêu cầu)
    v_list = res.get("list", [])
    if v_list:
        background_tasks.add_task(prefetch_thumbnails, v_list)
        
    return JSONResponse(content=res)

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
