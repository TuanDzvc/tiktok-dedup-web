# -*- coding: utf-8 -*-
"""
TikTok Dedup Web Server - FastAPI Edition
Web application độc lập cho tính năng kiểm tra trùng lặp kênh TikTok.
Tự động kích hoạt Cloudflare Tunnel để tạo tên miền công khai miễn phí (HTTPS).
"""

import os
import sys
from pathlib import Path

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
import time
import json
import threading
import subprocess
from typing import Optional
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# Import engine trích xuất
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
    max_videos = int(body.get("max_videos") or 18)
    cookie = str(body.get("cookie") or "").strip()
    proxy = str(body.get("proxy") or "").strip()
    sec_uid = str(body.get("sec_uid") or "").strip()

    clean_user = extract_clean_username(query)

    # Nếu client chưa truyền cookie, tự động tra cứu trong ACTIVE_STUDIO_ACCOUNTS
    if not cookie and clean_user:
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
    import asyncio
    res = await asyncio.to_thread(
        fetch_channel_info_and_posts,
        clean_user,
        sec_uid=sec_uid,
        max_cursor=max_cursor,
        cookie_str=cookie,
        proxy_str=proxy,
        max_videos=max_videos
    )
    
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
            
            # Trả về ảnh placeholder SVG màu tối nếu TikTok CDN từ chối (không lưu cache để cho phép thử lại)
            fallback_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="128" viewBox="0 0 96 128"><rect width="100%" height="100%" fill="#0f172a"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="28" fill="#38bdf8">🎬</text></svg>'
            return Response(content=fallback_svg.encode("utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
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
def api_studio_profile_avatar(username: str = Query(..., description="TikTok Username"), cookie: str = Query(None, description="Cookie (optional)")):
    """Lấy avatar trực tiếp cho kênh TikTok nếu session chưa có avatar."""
    clean_u = (username or "").lstrip("@").strip()
    if not clean_u:
        return JSONResponse(content={"success": False, "avatar": ""})
    av = studio_engine.fetch_user_avatar(clean_u, cookie_str=cookie)
    return JSONResponse(content={"success": bool(av), "avatar": av})

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
    
    acc_info = studio_engine.get_account_info(cookie_str, proxy=proxy)
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
    
    res = studio_engine.get_studio_post_list(cookie_str, proxy=proxy, count=count, max_cursor=max_cursor, sec_uid=sec_uid)
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
    
    res = studio_engine.set_post_visibility(cookie_str, item_id, visibility, proxy=proxy)
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
    
    res = studio_engine.delete_studio_post(cookie_str, item_id, proxy=proxy)
    return JSONResponse(content=res)

@app.post("/api/studio/browser/login/start")
async def api_studio_browser_login_start(req: Request):
    """Khởi chạy trình duyệt đăng nhập chạy ngầm (hỗ trợ HTTP & SOCKS5) và phản hồi ngay lập tức."""
    proxy = ""
    try:
        body = await req.json()
        proxy = str(body.get("proxy") or "").strip()
    except Exception:
        pass
    res = studio_engine.start_interactive_browser_login(proxy=proxy)
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
