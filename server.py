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
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# Import engine trích xuất
from core_dedup import fetch_channel_info_and_posts, DEFAULT_UA

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
async def api_check_channel(req: Request):
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

    # Chạy phân tích trong thread riêng tránh xung đột Playwright sync với asyncio loop
    import asyncio
    res = await asyncio.to_thread(fetch_channel_info_and_posts, query, max_cursor=max_cursor, max_videos=max_videos)
    return JSONResponse(content=res)

@app.get("/api/proxy/image")
def proxy_image(url: str = Query(..., description="TikTok CDN image URL")):
    """
    Proxy tải ảnh đại diện và ảnh bìa từ TikTok CDN tránh lỗi CORS / 403 Forbidden của TikTok.
    """
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="URL ảnh không hợp lệ.")

    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.tiktok.com/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }

    try:
        r = requests.get(url, headers=headers, timeout=10, stream=True)
        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "image/jpeg")
            return Response(content=r.content, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})
        else:
            raise HTTPException(status_code=r.status_code, detail="Không thể tải ảnh từ máy chủ TikTok")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi kết nối ảnh: {e}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
