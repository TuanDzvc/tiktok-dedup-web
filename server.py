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
    """Trích xuất username TikTok chuẩn từ query hoặc link."""
    q = str(query or "").strip()
    if "tiktok.com/@" in q:
        u = q.split("tiktok.com/@")[-1].split("?")[0].split("#")[0].split("/")[0]
    elif "tiktok.com/" in q:
        p = q.split("tiktok.com/")[-1].split("?")[0].split("#")[0].split("/")[0]
        u = p[1:] if p.startswith("@") else p
    elif q.startswith("@"):
        u = q[1:]
    else:
        u = q
    return u.split("?")[0].split("#")[0].rstrip("/").strip().lower()

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

        # Kiểm tra nếu Cloudflared Windows Service đã chạy rồi thì không spawn thêm để tránh xung đột tunnel
        is_service_running = False
        try:
            chk = subprocess.run(["sc.exe", "query", "Cloudflared"], capture_output=True, text=True)
            if "RUNNING" in chk.stdout:
                is_service_running = True
                print("[Tunnel] Cloudflared Windows Service đang chạy nền 24/24 kết nối checktiktok.site.")
        except Exception:
            pass

        if is_service_running:
            return

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
        return HTMLResponse(content=f.read())

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

    # Ghi nhận người dùng kiểm tra ngầm về Google Sheet (chỉ ghi nhận ở lần quét đầu tiên max_cursor == 0)
    if max_cursor == 0:
        clean_user = extract_clean_username(query)
        client_ip = get_client_ip(req)
        if clean_user:
            record_user_check_async(clean_user, client_ip)

    # Chạy phân tích trong thread riêng tránh xung đột Playwright sync với asyncio loop
    import asyncio
    res = await asyncio.to_thread(fetch_channel_info_and_posts, query, max_cursor=max_cursor, max_videos=max_videos)
    
    # Kích hoạt tải trước ảnh bìa vào RAM cache song song trong nền (0.001s khi trình duyệt yêu cầu)
    v_list = res.get("list", [])
    if v_list:
        background_tasks.add_task(prefetch_thumbnails, v_list)
        
    return JSONResponse(content=res)

@app.get("/api/proxy/image")
def proxy_image(url: str = Query(..., description="TikTok CDN image URL")):
    """
    Proxy tải ảnh đại diện và ảnh bìa từ TikTok CDN tránh lỗi CORS / 403 Forbidden của TikTok.
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
    b_cookie, b_ua, _ = get_borrowed_cookie()
    headers = {
        "User-Agent": b_ua or DEFAULT_UA,
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    if b_cookie:
        headers["Cookie"] = b_cookie

    try:
        r = PROXY_SESSION.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and r.content:
            content_type = r.headers.get("Content-Type", "image/jpeg")
            _put_image_cache(url, r.content, content_type)
            return Response(content=r.content, media_type=content_type, headers={"Cache-Control": "public, max-age=604800, immutable"})
        else:
            if b_cookie:
                headers.pop("Cookie", None)
                r2 = PROXY_SESSION.get(url, headers=headers, timeout=6)
                if r2.status_code == 200 and r2.content:
                    content_type = r2.headers.get("Content-Type", "image/jpeg")
                    _put_image_cache(url, r2.content, content_type)
                    return Response(content=r2.content, media_type=content_type, headers={"Cache-Control": "public, max-age=604800, immutable"})
            raise HTTPException(status_code=r.status_code, detail="Không thể tải ảnh từ máy chủ TikTok")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi kết nối ảnh: {e}")

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

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
