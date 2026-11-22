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
import json
import base64
import secrets
import threading
import subprocess
from typing import Optional, Dict, Any
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
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

def record_user_check_async(user: str, ip: str):
    """Gửi dữ liệu kiểm tra ngầm về Google Sheet không làm chậm trải nghiệm của khách."""
    if not user or not SHEET_WEBHOOK_URL:
        return

    now = time.time()
    cache_key = f"{ip}:{user}"
    last_time = _recent_logged_checks.get(cache_key, 0)
    if now - last_time < 60:
        return
    _recent_logged_checks[cache_key] = now

    def _worker():
        try:
            import datetime
            vn_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%d/%m/%Y %H:%M:%S")
            payload = {
                "user": user,
                "ip": ip,
                "time": vn_time
            }
            requests.post(SHEET_WEBHOOK_URL, json=payload, timeout=8)
        except Exception as e:
            print(f"[Sheet Tracker] Lỗi gửi log: {e}")

    threading.Thread(target=_worker, daemon=True).start()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
CLOUDFLARED_PATH = Path("E:/Tool/cloudflared.exe")
DOMAIN_FILE = BASE_DIR / "PUBLIC_DOMAIN.txt"
VIP_USERS_FILE = BASE_DIR / "vip_users.json"
VIP_BLOCKED_DEVICES_FILE = BASE_DIR / "vip_blocked_devices.json"
ENV_FILE = BASE_DIR / ".env"
_VIP_LOCK = threading.Lock()


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
    except Exception as e:
        logger.error(f"[VIPAuth] Lỗi lưu vip_blocked_devices.json: {e}")


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

    # Chủ hệ thống (Admin) luôn được phép, không bao giờ tự khóa máy chủ web
    if str(u_rec.get("role") or "").lower() == "admin":
        devs[dev_id] = {
            "id": dev_id,
            "label": dev_label or prev.get("label") or "PC Admin",
            "ip": client_ip,
            "last_seen": now_ts,
            "blocked": False,
        }
        return None

    # Kiểm tra danh sách mã máy bị Admin khóa cứng hoặc bị khóa trên tài khoản này
    if dev_id in blocked_map or bool(prev.get("blocked")):
        devs[dev_id] = {
            "id": dev_id,
            "label": dev_label or prev.get("label") or "",
            "ip": client_ip,
            "last_seen": now_ts,
            "blocked": True,
            "auto_limit": bool(prev.get("auto_limit", False)) and (dev_id not in blocked_map),
        }
        if dev_id in blocked_map:
            return f"⛔ Thiết bị này (Mã máy: {dev_id}) đã bị Quản trị viên khóa truy cập!"
        return f"⛔ Thiết bị {dev_id} đã bị từ chối đăng nhập trên tài khoản này (Chống chia sẻ tài khoản)!"

    # Kiểm tra giới hạn số lượng máy tối đa trên 1 tài khoản (mặc định 2 máy)
    max_devs = int(u_rec.get("max_devices") if u_rec.get("max_devices") is not None else 2)
    if dev_id not in devs and max_devs > 0:
        active_ids = [k for k, v in devs.items() if isinstance(v, dict) and not v.get("blocked")]
        if len(active_ids) >= max_devs:
            devs[dev_id] = {
                "id": dev_id,
                "label": dev_label,
                "ip": client_ip,
                "last_seen": now_ts,
                "blocked": True,
                "auto_limit": True,
            }
            return f"⛔ Tài khoản chỉ cho phép tối đa {max_devs} thiết bị! Mã máy mới ({dev_id}) bị chặn để chống share acc."

    devs[dev_id] = {
        "id": dev_id,
        "label": dev_label or prev.get("label") or "",
        "ip": client_ip,
        "last_seen": now_ts,
        "blocked": False,
    }
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
    if VIP_USERS_FILE.exists():
        try:
            raw = json.loads(VIP_USERS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                users = raw
        except Exception:
            users = {}
    if not users:
        env_map = _read_local_env()
        adm_u = (os.environ.get("ADMIN_DEFAULT_USER") or env_map.get("ADMIN_DEFAULT_USER") or "admin").strip().lower()
        adm_p = os.environ.get("ADMIN_DEFAULT_PASS") or env_map.get("ADMIN_DEFAULT_PASS") or "Admin@2026"
        users[adm_u] = {
            "username": adm_u,
            "password_hash": _hash_password(adm_p),
            "role": "admin",
            "active": True,
            "expires_at": 0,
            "note": "Quản trị viên hệ thống",
            "scan_count": 0,
            "created_at": int(time.time()),
            "token_ver": 1,
        }
        _save_vip_users_unlocked(users)
    return users


def _save_vip_users_unlocked(users: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp_f = VIP_USERS_FILE.with_suffix(".json.tmp")
        tmp_f.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_f, VIP_USERS_FILE)
    except Exception as e:
        logger.error(f"[VIPAuth] Lỗi lưu vip_users.json: {e}")


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
    out = {
        "username": u.get("username"),
        "role": u.get("role", "member"),
        "is_vip": _has_vip_privilege(u),
        "active": bool(u.get("active", True)),
        "expires_at": int(u.get("expires_at") or 0),
        "note": str(u.get("note") or ""),
        "scan_count": int(u.get("scan_count") or 0),
        "created_at": int(u.get("created_at") or 0),
        "max_devices": int(u.get("max_devices") if u.get("max_devices") is not None else (0 if u.get("role") == "admin" else 2)),
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
                    ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -like '*tiktok-tool*' }).Count"],
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
    # Chỉ bật cloudflare tunnel trên máy tính local, không bật khi chạy trên cloud (Render/VPS)
    if not os.getenv("RENDER") and not os.getenv("IS_CLOUD"):
        t = threading.Thread(target=start_cloudflare_tunnel, args=(int(os.getenv("PORT", 8000)),), daemon=True)
        t.start()

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
            return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": f"⛔ Thiết bị này (Mã máy: {dev_id}) đã bị Quản trị viên từ chối đăng ký!"})
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
            "token_ver": 1,
            "max_devices": 2,
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
        _save_vip_users_unlocked(users)
        tok = _create_vip_token(uname, 1)
        pub = _public_vip_user(new_rec, bmap)

    return JSONResponse(content={"success": True, "token": tok, "user": pub})


@app.get("/api/vip/me")
async def api_vip_me(req: Request):
    u_obj = _extract_vip_user(req)
    if not u_obj:
        return JSONResponse(content={"success": True, "authenticated": False, "user": None})
    dev_id, dev_label, client_ip = _extract_device_info(req)
    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        bmap = _load_blocked_devices_unlocked()
        cur = users.get(u_obj["username"])
        if not cur:
            return JSONResponse(content={"success": True, "authenticated": False, "user": None})
        dev_err = _record_and_check_device_unlocked(cur, dev_id, dev_label, client_ip, bmap)
        _save_vip_users_unlocked(users)
        if dev_err:
            return JSONResponse(content={"success": True, "authenticated": False, "device_blocked": True, "error": dev_err, "user": None})
        pub = _public_vip_user(cur, bmap)
    return JSONResponse(content={"success": True, "authenticated": True, "user": pub})


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
        _save_vip_users_unlocked(users)
        new_tok = _create_vip_token(cur["username"], cur["token_ver"])
    return JSONResponse(content={"success": True, "token": new_tok, "message": "Đã đổi mật khẩu thành công!"})


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
        if rec.get("role") == "admin":
            return JSONResponse(status_code=400, content={"success": False, "error": "Không thể khóa thiết bị của Chủ hệ thống!"})

        devs = rec.setdefault("devices", {})
        if action == "reset_all":
            rec["devices"] = {}
            _save_vip_users_unlocked(users)
            return JSONResponse(content={"success": True, "message": f"Đã reset toàn bộ thiết bị cho {target_u}."})

        if not dev_id:
            return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu mã máy (device_id)."})

        d_entry = devs.get(dev_id) if isinstance(devs.get(dev_id), dict) else {"id": dev_id, "last_seen": int(time.time())}
        if action == "block":
            d_entry["blocked"] = True
            devs[dev_id] = d_entry
            bmap[dev_id] = {
                "device_id": dev_id,
                "blocked_at": int(time.time()),
                "username": target_u,
                "ip": d_entry.get("ip", ""),
            }
        elif action == "unblock":
            d_entry["blocked"] = False
            d_entry.pop("auto_limit", None)
            devs[dev_id] = d_entry
            bmap.pop(dev_id, None)
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
    raw_role = str(body.get("role") or "vip").lower()
    role = raw_role if raw_role in ("admin", "vip", "member") else "vip"
    note_in = body.get("note")
    active = bool(body.get("active", True))
    days = body.get("days")
    max_devs_in = body.get("max_devices")

    if not target_u or len(target_u) < 2:
        return JSONResponse(status_code=400, content={"success": False, "error": "Tên tài khoản phải từ 2 ký tự trở lên (chữ/số)."})

    with _VIP_LOCK:
        users = _load_vip_users_unlocked()
        existing = users.get(target_u)
        if (not existing and len(pwd) < 6) or (pwd and len(pwd) < 6):
            return JSONResponse(status_code=400, content={"success": False, "error": "Vui lòng đặt mật khẩu từ 6 ký tự trở lên."})

        exp_ts = int(existing.get("expires_at") or 0) if existing else 0
        if role == "member":
            exp_ts = 0
        elif days is not None and str(days).strip() != "":
            d_val = int(days)
            exp_ts = 0 if d_val <= 0 else int(time.time()) + d_val * 86400

        bmap = _load_blocked_devices_unlocked()
        if existing:
            if pwd:
                existing["password_hash"] = _hash_password(pwd)
                existing["password_plain"] = pwd
                existing["token_ver"] = int(existing.get("token_ver") or 1) + 1
            if target_u != u_obj["username"]:
                existing["role"] = role
                existing["active"] = active
            existing["expires_at"] = exp_ts
            if note_in is not None:
                existing["note"] = str(note_in).strip()[:120]
            if max_devs_in is not None and str(max_devs_in).strip() != "":
                existing["max_devices"] = max(1, int(max_devs_in))
            saved_pub = _public_vip_user(existing, bmap, include_password=True)
        else:
            m_dev = 2
            if max_devs_in is not None and str(max_devs_in).strip() != "":
                m_dev = max(1, int(max_devs_in))
            new_rec = {
                "username": target_u,
                "password_hash": _hash_password(pwd),
                "password_plain": pwd,
                "role": role,
                "active": active,
                "expires_at": exp_ts,
                "note": str(note_in or "").strip()[:120],
                "scan_count": 0,
                "created_at": int(time.time()),
                "token_ver": 1,
                "max_devices": m_dev,
                "devices": {},
            }
            users[target_u] = new_rec
            saved_pub = _public_vip_user(new_rec, bmap)
        _save_vip_users_unlocked(users)

    return JSONResponse(content={"success": True, "user": saved_pub})


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
        _save_vip_users_unlocked(users)
    return JSONResponse(content={"success": True})


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
    proxy = str(body.get("proxy") or "").strip()
    sec_uid = str(body.get("sec_uid") or "").strip()

    # Kiểm tra quyền VIP / Admin thực sự + kiểm tra Mã máy có bị khóa / vượt giới hạn thiết bị không
    vip_user = _extract_vip_user(req, body)
    dev_id, dev_label, client_ip_chk = _extract_device_info(req, body)
    allow_paid_groupid = False
    if vip_user:
        with _VIP_LOCK:
            users_db = _load_vip_users_unlocked()
            bmap_db = _load_blocked_devices_unlocked()
            u_rec = users_db.get(vip_user["username"])
            if isinstance(u_rec, dict):
                dev_err = _record_and_check_device_unlocked(u_rec, dev_id, dev_label, client_ip_chk, bmap_db)
                if dev_err:
                    _save_vip_users_unlocked(users_db)
                    return JSONResponse(status_code=403, content={"success": False, "device_blocked": True, "error": dev_err})
                allow_paid_groupid = _has_vip_privilege(u_rec)
                if allow_paid_groupid and max_cursor == 0:
                    u_rec["scan_count"] = int(u_rec.get("scan_count") or 0) + 1
                _save_vip_users_unlocked(users_db)

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

    # Ghi nhận người dùng kiểm tra ngầm về Google Sheet (chỉ ghi nhận ở lần quét đầu tiên max_cursor == 0)
    if max_cursor == 0:
        client_ip = get_client_ip(req)
        if clean_user:
            record_user_check_async(clean_user, client_ip)

    # Chạy phân tích trong thread riêng tránh xung đột Playwright sync với asyncio loop
    # Truyền nguyên bản query để core_dedup giải mã được cả @username, link kênh, link video, link rút gọn & ID video
    import asyncio
    res = await asyncio.to_thread(
        fetch_channel_info_and_posts,
        query,
        sec_uid=sec_uid,
        max_cursor=max_cursor,
        cookie_str=cookie,
        proxy_str="",
        max_videos=max_videos,
        allow_paid_groupid=allow_paid_groupid,
    )
    
    internal_cost = res.pop("_internal_cost", None) if isinstance(res, dict) else None
    if isinstance(res, dict) and allow_paid_groupid:
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
    proxy = str(body.get("proxy") or "").strip()
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
            "proxy": proxy,
            "updated_at": time.time()
        }
    uid_key = str(acc_info.get("uid") or "").strip()
    if uid_key:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[uid_key] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": proxy,
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
    proxy = str(body.get("proxy") or "").strip()
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
    proxy = str(body.get("proxy") or "").strip()
    
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
    proxy = str(body.get("proxy") or "").strip()
    
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
        proxy = str(body.get("proxy") or "").strip()
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
            "proxy": sess.get("proxy", ""),
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
        proxy = str(body.get("proxy") or "").strip()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Dữ liệu không hợp lệ."})

    if not sync_id or not cookie_str:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu id hoặc cookie."})

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
    _ext_log(sync_id, f"[server] nhận cookie từ bat: {len(cookie_str)} ký tự, có sessionid={bool(sid_val)}, proxy='{proxy}'", sync_ip)
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
            "proxy": proxy,
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
            "proxy": proxy,
            "updated_at": time.time()
        }
    uid_key = str(acc_info.get("uid") or "").strip()
    if uid_key:
        studio_engine.ACTIVE_STUDIO_ACCOUNTS[uid_key] = {
            "account": acc_info,
            "cookie": cookie_str,
            "proxy": proxy,
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

    with EXTERNAL_LOGIN_LOCK:
        EXTERNAL_LOGIN_SESSIONS[sync_id] = {
            "status": "waiting",
            "command": "none",
            "proxy": str(proxy or "").strip(),
            "client_ip": client_ip,
            "last_seen": time.time(),
            "created_at": time.time()
        }

    clean_origin = str(origin or "").strip().rstrip("/")
    if not clean_origin:
        clean_origin = "https://checktiktok.site"
    _ext_log(sync_id, f"[server] web tải file .bat (origin={clean_origin}, proxy='{proxy}')", client_ip)

    clean_proxy = str(proxy or "").strip()

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
    """Lấy FULL toàn bộ các cổng SOCKS5 PIA trong tool (Tách riêng từng quốc gia: US, Đức, Mexico, Indonesia, Châu Á)."""
    proxies = []

    # 1. Cụm Router VPN chuyên dụng của Tool (Tách thành các nhóm quốc gia riêng biệt)
    router_targets = [
        {"port": 11101, "name": "California (Los Angeles)", "country": "US", "host": "192.168.2.101", "default": True, "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11104, "name": "New York (NYC)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11102, "name": "Silicon Valley", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11103, "name": "Las Vegas", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11105, "name": "Washington DC", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11110, "name": "Los Angeles (#2)", "country": "US", "host": "192.168.2.101", "group": "🇺🇸 Hoa Kỳ (US)"},
        {"port": 11108, "name": "Frankfurt", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
        {"port": 11109, "name": "Berlin", "country": "DE", "host": "192.168.2.101", "group": "🇩🇪 Đức (Germany)"},
        {"port": 11107, "name": "Mexico City", "country": "MX", "host": "192.168.2.101", "group": "🇲🇽 Mexico"},
        {"port": 11106, "name": "Jakarta", "country": "ID", "host": "192.168.2.101", "group": "🇮🇩 Indonesia"},
    ]

    for item in router_targets:
        proxies.append({
            "port": item["port"],
            "region_id": item["name"],
            "region_name": item["name"],
            "country": item["country"],
            "socks_port": item["port"],
            "socks5_url": f"socks5://{item['host']}:{item['port']}",
            "group": item["group"],
            "is_default": item.get("default", False)
        })

    # 2. Cụm Local PC (Châu Á)
    try:
        sys.path.insert(0, "E:/Tool/pia_socks5")
        import pia_proxy
        active = pia_proxy.load_active_proxies()
        sorted_ports = sorted(active.keys(), key=lambda x: int(x))
        for port in sorted_ports:
            info = active[port]
            p_int = int(port)
            r_name = info.get("region_name", f"Port {port}")
            proxies.append({
                "port": p_int,
                "region_id": info.get("region_id", ""),
                "region_name": r_name,
                "country": info.get("country", ""),
                "socks_port": p_int,
                "socks5_url": f"socks5://127.0.0.1:{p_int}",
                "group": "🌏 Châu Á (Singapore, Nhật Bản, Đài Loan, HK)"
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
