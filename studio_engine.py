# -*- coding: utf-8 -*-
"""
studio_engine.py - Quản lý TikTok Studio & Check Post trên Web
Hỗ trợ:
1. Đăng nhập bằng mã QR (Passport Web QR Code API)
2. Xác thực và nạp Cookie tài khoản (Account Info API)
3. Lấy danh sách video Creator Studio (Check Post với đầy đủ video Công khai / Riêng tư / Bạn bè)
4. Thao tác video: Ẩn riêng tư, Chuyển bạn bè, Công khai, Xóa vĩnh viễn
"""

import os
from pathlib import Path
import re
import time
import json
import uuid
import base64
import datetime
import threading
import socket
from urllib.parse import urlparse
try:
    import socks
except ImportError:
    socks = None
from typing import Any, Dict, List, Optional, Tuple
import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Lưu trữ phiên đăng nhập QR & tài khoản trong bộ nhớ RAM máy chủ
QR_SESSIONS: Dict[str, Dict[str, Any]] = {}
ACTIVE_STUDIO_ACCOUNTS: Dict[str, Dict[str, Any]] = {}


def parse_cookie_dict(cookie_str: str) -> Dict[str, str]:
    """Chuyển đổi chuỗi cookie hoặc dict thành dictionary chuẩn."""
    if not cookie_str:
        return {}
    if isinstance(cookie_str, dict):
        return {str(k).strip(): str(v).strip() for k, v in cookie_str.items() if k}
    
    cdict = {}
    raw = str(cookie_str).strip()
    # Hỗ trợ dạng JSON
    if raw.startswith("{") and raw.endswith("}"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return {str(k).strip(): str(v).strip() for k, v in d.items()}
        except Exception:
            pass

    # Hỗ trợ chuỗi Netscape / standard header k1=v1; k2=v2
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"')
            if k:
                cdict[k] = v
    return cdict


def cookie_to_header_str(cookie_val: Any) -> str:
    """Chuẩn hóa cookie thành chuỗi header HTTP và đồng bộ sessionid/sessionid_ss."""
    if not cookie_val:
        return ""
    cdict = parse_cookie_dict(cookie_val) if not isinstance(cookie_val, dict) else dict(cookie_val)
    sid = cdict.get("sessionid") or cdict.get("sessionid_ss") or cdict.get("sid_tt")
    if sid:
        if "sessionid" not in cdict:
            cdict["sessionid"] = sid
        if "sessionid_ss" not in cdict:
            cdict["sessionid_ss"] = sid
    return "; ".join(f"{k}={v}" for k, v in cdict.items() if k and v)


def detect_proxy_type(host: str, port: int, timeout: float = 1.0) -> str:
    """Tự động kiểm tra port là SOCKS5 hay HTTP nếu người dùng không ghi scheme."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.sendall(b"\x05\x02\x00\x02")
        resp = s.recv(2)
        s.close()
        if len(resp) >= 2 and resp[0] == 5 and resp[1] in (0, 2):
            return "socks5"
    except Exception:
        pass
    return "http"


def parse_proxy_components(p_str: str) -> Tuple[bool, str, str, str, str]:
    """Tách chuỗi proxy thành (is_socks, ip, port, user, pwd)."""
    p = str(p_str).strip()
    is_socks = p.startswith(("socks5://", "socks5h://")) or "socks" in p.lower()
    clean = p.replace("socks5h://", "").replace("socks5://", "").replace("http://", "").replace("https://", "").strip()

    ip, port, user, pwd = "", "", "", ""
    if clean.count(":") == 3:
        ip, port, user, pwd = clean.split(":")
    elif "@" in clean:
        auth, host = clean.split("@", 1)
        if ":" in auth:
            user, pwd = auth.split(":", 1)
        if ":" in host:
            ip, port = host.split(":", 1)
        else:
            ip = host
    elif ":" in clean:
        ip, port = clean.split(":", 1)
    else:
        ip = clean

    # Nếu người dùng không nhập scheme (vd: ip:port:user:pass), tự động phát hiện port là SOCKS5 hay HTTP
    if not is_socks and ip and port and port.isdigit():
        detected = detect_proxy_type(ip, int(port))
        if detected == "socks5":
            is_socks = True

    return is_socks, ip, port, user, pwd


def normalize_proxy_for_requests(p_str: str) -> Dict[str, str]:
    """Chuẩn hóa proxy cho thư viện requests (hỗ trợ HTTP, HTTPS, SOCKS5, SOCKS5H remote DNS)."""
    if not p_str or not str(p_str).strip():
        return {}
    is_socks, ip, port, user, pwd = parse_proxy_components(p_str)
    if not ip or not port:
        return {}
    if is_socks:
        if user and pwd:
            px = f"socks5h://{user}:{pwd}@{ip}:{port}"
        else:
            px = f"socks5h://{ip}:{port}"
    else:
        if user and pwd:
            px = f"http://{user}:{pwd}@{ip}:{port}"
        else:
            px = f"http://{ip}:{port}"
    return {"http": px, "https": px}


class LocalSocks5Bridge:
    """
    Cầu nối HTTP-to-SOCKS5 cục bộ chạy ngầm trên 127.0.0.1.
    Giải quyết triệt để lỗi của Chromium/Chrome: 'Browser does not support socks5 proxy authentication'.
    Chuyển tiếp toàn bộ kết nối của Chrome qua SOCKS5 từ xa với Remote DNS (rdns=True),
    đảm bảo không bị leak DNS hoặc IP Việt Nam khi đăng nhập TikTok.
    """
    def __init__(self, remote_host: str, remote_port: int, username: str = "", password: str = ""):
        self.remote_host = remote_host
        self.remote_port = int(remote_port)
        self.username = username or None
        self.password = password or None
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(('127.0.0.1', 0))
        self.server_sock.listen(50)
        self.port = self.server_sock.getsockname()[1]
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                client_sock, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, client_sock):
        remote_sock = None
        try:
            req_data = b""
            while b"\r\n\r\n" not in req_data:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                req_data += chunk
            if not req_data:
                client_sock.close()
                return

            first_line = req_data.split(b"\r\n")[0].decode('latin1', 'ignore')
            parts = first_line.split()
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]
            if method.upper() == 'CONNECT':
                dst_host, dst_port = target.split(':', 1) if ':' in target else (target, 443)
                remote_sock = socks.socksocket()
                remote_sock.set_proxy(
                    socks.SOCKS5,
                    self.remote_host,
                    self.remote_port,
                    rdns=True,
                    username=self.username,
                    password=self.password
                )
                remote_sock.settimeout(20)
                remote_sock.connect((dst_host, int(dst_port)))
                remote_sock.settimeout(None)
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                parsed = urlparse(target)
                dst_host = parsed.hostname or "127.0.0.1"
                dst_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                remote_sock = socks.socksocket()
                remote_sock.set_proxy(
                    socks.SOCKS5,
                    self.remote_host,
                    self.remote_port,
                    rdns=True,
                    username=self.username,
                    password=self.password
                )
                remote_sock.settimeout(20)
                remote_sock.connect((dst_host, int(dst_port)))
                remote_sock.settimeout(None)
                remote_sock.sendall(req_data)

            def forward(src, dst):
                try:
                    while True:
                        data = src.recv(8192)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try: src.close()
                    except Exception: pass
                    try: dst.close()
                    except Exception: pass

            t1 = threading.Thread(target=forward, args=(client_sock, remote_sock), daemon=True)
            t2 = threading.Thread(target=forward, args=(remote_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            try: client_sock.close()
            except Exception: pass
            if remote_sock:
                try: remote_sock.close()
                except Exception: pass

    def stop(self):
        self.running = False
        try:
            self.server_sock.close()
        except Exception:
            pass


def setup_browser_proxy(p_str: str) -> Tuple[Optional[Dict[str, str]], Optional[LocalSocks5Bridge]]:
    """
    Thiết lập proxy cho Playwright Chromium / Chrome.
    - Với SOCKS5 có mật khẩu (user/pass): Dùng LocalSocks5Bridge để chuyển tiếp an toàn 100%.
    - Với SOCKS5 không mật khẩu: Dùng trực tiếp socks5://ip:port.
    - Với HTTP/HTTPS: Dùng trực tiếp Playwright proxy dict.
    """
    if not p_str or not str(p_str).strip():
        return None, None

    is_socks, ip, port, user, pwd = parse_proxy_components(p_str)
    if not ip or not port:
        return None, None

    if is_socks:
        if user and pwd:
            bridge = LocalSocks5Bridge(ip, int(port), username=user, password=pwd)
            return {"server": f"http://127.0.0.1:{bridge.port}"}, bridge
        else:
            return {"server": f"socks5://{ip}:{port}"}, None
    else:
        if user and pwd:
            return {"server": f"http://{ip}:{port}", "username": user, "password": pwd}, None
        else:
            return {"server": f"http://{ip}:{port}"}, None


def normalize_proxy_for_playwright(p_str: str) -> Optional[Dict[str, str]]:
    """Giữ tương thích ngược cho QR worker."""
    pw_dict, _ = setup_browser_proxy(p_str)
    return pw_dict


def _pw_qr_worker(session_id: str):
    sess = QR_SESSIONS.get(session_id)
    if not sess:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            context.add_init_script("delete Object.getPrototypeOf(navigator).webdriver")
            page = context.new_page()
            page.goto("https://www.tiktok.com/login", timeout=25000)
            page.wait_for_timeout(2000)

            # Bấm vào tùy chọn đăng nhập bằng mã QR (Use QR code)
            try:
                page.get_by_text("Use QR code").click(timeout=4000)
            except Exception:
                try:
                    page.locator("text=QR, a[href*='qrcode'], div:has-text('quét mã QR')").first.click(timeout=3000)
                except Exception:
                    pass

            # Trích xuất trực tiếp mã QR độ nét cao từ thẻ canvas
            qr_extracted = False
            for _ in range(25):
                canvas = page.locator("canvas").first
                if canvas.is_visible():
                    try:
                        b64_str = canvas.evaluate("c => c.toDataURL('image/png')")
                        if b64_str and len(b64_str) > 500:
                            sess["qr_image"] = b64_str
                            sess["ready"] = True
                            qr_extracted = True
                            break
                    except Exception:
                        pass
                el = page.locator("div[data-e2e='qr-code'], img[src*='qrcode']").first
                if el.is_visible():
                    try:
                        img_b = el.screenshot()
                        b64_str = f"data:image/png;base64,{base64.b64encode(img_b).decode('ascii')}"
                        if b64_str and len(b64_str) > 500:
                            sess["qr_image"] = b64_str
                            sess["ready"] = True
                            qr_extracted = True
                            break
                    except Exception:
                        pass
                page.wait_for_timeout(400)

            if not qr_extracted:
                sess["ready"] = True
                sess["status"] = "error"
                sess["error"] = "Không thể tải mã QR từ TikTok. Vui lòng bấm thử lại hoặc dùng nút Mở Trình Duyệt phía trên."
                browser.close()
                return

            # Chờ người dùng quét mã và bấm xác nhận trên điện thoại (tối đa 180s)
            start_ts = time.time()
            while time.time() - start_ts < 180:
                if sess.get("cancelled"):
                    break
                cookies = context.cookies()
                sid = next((c['value'] for c in cookies if c['name'] == 'sessionid'), None)
                if sid:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get('name') and c.get('value'))
                    acc_info = get_account_info(cookie_str)
                    sess["status"] = "confirmed"
                    sess["account"] = acc_info
                    sess["cookie_str"] = cookie_str
                    u_key = acc_info.get("username", "").lstrip("@").lower()
                    if u_key:
                        ACTIVE_STUDIO_ACCOUNTS[u_key] = {
                            "account": acc_info,
                            "cookie": cookie_str,
                            "updated_at": time.time()
                        }
                    break

                try:
                    scanned_el = page.locator("text=Scanned, text=Đã quét, div:has-text('Đã quét')").first
                    if scanned_el.is_visible():
                        sess["status"] = "scanned"
                except Exception:
                    pass

                time.sleep(1.2)

            if sess.get("status") != "confirmed":
                sess["status"] = "expired"

            browser.close()
    except Exception as e:
        sess["ready"] = True
        sess["status"] = "error"
        sess["error"] = str(e)


def create_qr_session() -> Dict[str, Any]:
    """Tạo phiên đăng nhập QR chính quy từ TikTok qua Playwright Headless Engine."""
    # Hủy các phiên QR cũ đang chờ để dọn dẹp tiến trình trình duyệt
    for sid, old_sess in list(QR_SESSIONS.items()):
        if old_sess.get("status") in ("waiting", "scanned"):
            old_sess["cancelled"] = True

    session_id = str(uuid.uuid4())
    QR_SESSIONS[session_id] = {
        "ready": False,
        "status": "waiting",
        "qr_image": "",
        "created_at": time.time(),
        "expire_time": time.time() + 180,
        "cookie_str": "",
        "account": None,
        "cancelled": False
    }

    t = threading.Thread(target=_pw_qr_worker, args=(session_id,), daemon=True)
    t.start()

    # Đợi tối đa 20s cho Playwright tải xong trang và trích xuất ảnh mã QR
    for _ in range(50):
        sess = QR_SESSIONS.get(session_id, {})
        if sess.get("ready"):
            break
        time.sleep(0.4)

    sess = QR_SESSIONS.get(session_id, {})
    if sess.get("qr_image"):
        return {
            "success": True,
            "session_id": session_id,
            "qr_image": sess["qr_image"],
            "expire_time": sess["expire_time"]
        }
    else:
        return {"success": False, "error": sess.get("error", "Không thể khởi tạo mã QR từ TikTok.")}


def check_qr_session_status(session_id: str) -> Dict[str, Any]:
    """Kiểm tra tiến trình quét mã QR của người dùng."""
    sess_info = QR_SESSIONS.get(session_id)
    if not sess_info:
        return {"success": False, "status": "expired", "message": "Phiên không tồn tại hoặc đã hết hạn."}

    st = sess_info.get("status", "waiting")
    if st == "confirmed" and sess_info.get("account"):
        return {
            "success": True,
            "status": "confirmed",
            "account": sess_info["account"],
            "cookie": sess_info["cookie_str"]
        }
    elif st == "scanned":
        return {"success": True, "status": "scanned", "message": "Đã quét mã! Vui lòng nhấn [Xác nhận] trên điện thoại."}
    elif st == "expired":
        return {"success": True, "status": "expired", "message": "Mã QR đã hết hạn."}
    elif st == "error":
        return {"success": False, "status": "error", "message": sess_info.get("error", "Lỗi phiên đăng nhập.")}
    else:
        return {"success": True, "status": "waiting", "message": "Đang chờ quét mã..."}


def fetch_user_avatar(username: str, cookie_str: str = "", proxy: str = None) -> str:
    """
    Lấy Avatar chất lượng cao của kênh TikTok dựa vào Username và Cookie đăng nhập.
    Sử dụng SSR __UNIVERSAL_DATA_FOR_REHYDRATION__ từ trang profile chuẩn như desktop tool.
    Tự động fallback sang cookie pool nếu cookie tài khoản bị WAF/hết hạn.
    """
    clean_u = (username or "").lstrip("@").strip()
    if not clean_u:
        return ""
    
    proxies = normalize_proxy_for_requests(proxy)

    # 1. Nếu có Cookie tài khoản, lấy trực tiếp Avatar chính chủ siêu nét từ trang chủ TikTok
    if cookie_str:
        try:
            h_home = {
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
                "Cookie": cookie_to_header_str(cookie_str)
            }
            r_home = requests.get(
                "https://www.tiktok.com/",
                headers=h_home,
                proxies=proxies if proxies else None,
                timeout=7
            )
            if r_home.status_code == 200 and r_home.text:
                m_h = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>', r_home.text)
                if m_h:
                    d_h = json.loads(m_h.group(1))
                    app_u = d_h.get("__DEFAULT_SCOPE__", {}).get("webapp.app-context", {}).get("user", {})
                    av_uris = app_u.get("avatarUri") or []
                    if isinstance(av_uris, list) and av_uris and av_uris[0]:
                        return av_uris[0]
                    av_u = app_u.get("avatarLarger") or app_u.get("avatarMedium") or app_u.get("avatarThumb")
                    if av_u:
                        return av_u
        except Exception:
            pass

    # 2. Thử tải trang Profile @clean_u. Thử lần lượt:
    # A) Với cookie tài khoản (nếu có)
    # B) Với cookie pool hệ thống (ổn định 100%, bypass WAF)
    # C) Không dùng cookie
    cookie_candidates = []
    if cookie_str:
        cookie_candidates.append(cookie_to_header_str(cookie_str))
    
    # Cookie pool
    pool_cookie = ""
    pool_ua = DEFAULT_UA
    try:
        pool_path = Path(__file__).resolve().parent / "cookie_pool.json"
        if pool_path.exists():
            pool_data = json.loads(pool_path.read_text(encoding="utf-8"))
            pool_cookie = pool_data.get("cookie", "")
            pool_ua = pool_data.get("user_agent", DEFAULT_UA)
    except Exception:
        pass

    if pool_cookie and pool_cookie not in cookie_candidates:
        cookie_candidates.append(pool_cookie)
    cookie_candidates.append("")  # fallback không cookie

    for candidate_ck in cookie_candidates:
        try:
            req_h = {
                "User-Agent": pool_ua or DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
            }
            if candidate_ck:
                req_h["Cookie"] = candidate_ck
            
            r = requests.get(
                f"https://www.tiktok.com/@{clean_u}",
                headers=req_h,
                proxies=proxies if proxies else None,
                timeout=8
            )
            if r.status_code == 200 and r.text and len(r.text) > 3000:
                html = r.text
                m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>', html)
                if m:
                    d = json.loads(m.group(1))
                    scope = d.get("__DEFAULT_SCOPE__", {})
                    user_detail = scope.get("webapp.user-detail", {})
                    u_info = user_detail.get("userInfo", {})
                    user = u_info.get("user", {})
                    av = user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb")
                    if av:
                        return av
                    app_ctx = scope.get("webapp.app-context", {})
                    app_user = app_ctx.get("user", {})
                    av = app_user.get("avatarLarger") or app_user.get("avatarMedium") or app_user.get("avatarThumb")
                    if av:
                        return av
                m_reg = re.search(r'"avatar(?:Larger|Medium|Thumb)":"([^"]+)"', html)
                if m_reg:
                    return m_reg.group(1).replace(r"\u0026", "&").replace(r"\/", "/")
        except Exception:
            continue

    return ""

def fetch_user_avatar_and_profile(username: str = "", cookie_str: str = "", proxy: str = None) -> dict:
    """Lấy avatar, uniqueId chuẩn và nickname từ Cookie hoặc SSR."""
    clean_u = (username or "").lstrip("@").strip()
    result = {"success": False, "avatar": "", "uniqueId": clean_u, "nickname": ""}
    
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    if cookie_str:
        headers["Cookie"] = cookie_to_header_str(cookie_str)
    
    proxies = normalize_proxy_for_requests(proxy)
    
    # 1. Nếu có Cookie, lấy trực tiếp từ trang chủ TikTok
    if cookie_str:
        try:
            r_home = requests.get("https://www.tiktok.com/", headers=headers, proxies=proxies if proxies else None, timeout=7)
            if r_home.status_code == 200 and r_home.text:
                m_h = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\s\S]*?)</script>', r_home.text)
                if m_h:
                    d_h = json.loads(m_h.group(1))
                    app_u = d_h.get("__DEFAULT_SCOPE__", {}).get("webapp.app-context", {}).get("user", {})
                    av_uris = app_u.get("avatarUri") or []
                    av = ""
                    if isinstance(av_uris, list) and av_uris and av_uris[0]:
                        av = av_uris[0]
                    elif app_u.get("avatarLarger") or app_u.get("avatarMedium") or app_u.get("avatarThumb"):
                        av = app_u.get("avatarLarger") or app_u.get("avatarMedium") or app_u.get("avatarThumb")
                    
                    real_u = app_u.get("uniqueId") or clean_u
                    real_nick = app_u.get("nickName") or ""
                    u_region = app_u.get("region") or app_u.get("countryCode") or ""
                    if av:
                        result["avatar"] = av
                    if real_u:
                        result["uniqueId"] = real_u
                    if real_nick:
                        result["nickname"] = real_nick
                    if u_region:
                        result["region"] = str(u_region).strip().upper()
                    if av:
                        result["success"] = True
                        return result
        except Exception:
            pass

    # 2. Nếu chưa lấy được, fetch từ profile @username (tự động fallback pool cookie nếu cần)
    target_uname = result.get("uniqueId") or clean_u
    if target_uname:
        av = fetch_user_avatar(target_uname, cookie_str=cookie_str, proxy=proxy)
        if not av:
            av = fetch_user_avatar(target_uname, cookie_str="", proxy="")
        if av:
            result["avatar"] = av
            result["success"] = True
            
    return result

def get_account_info(cookie_str: str, proxy: str = "") -> Dict[str, Any]:
    """Xác thực Cookie và lấy thông tin chi tiết tài khoản TikTok."""
    if not cookie_str:
        return {"valid": False, "error": "Chưa cung cấp Cookie"}

    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_to_header_str(cookie_str),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.tiktok.com/",
    }
    proxies = normalize_proxy_for_requests(proxy)

    try:
        r = requests.get(
            "https://www.tiktok.com/passport/web/account/info/",
            headers=headers,
            proxies=proxies if proxies else None,
            timeout=10
        )
        if r.status_code == 200:
            res_json = r.json()
            data = res_json.get("data") or {}
            user_id = str(data.get("user_id") or "")
            if user_id and user_id != "0":
                u_name = data.get("username") or data.get("screen_name") or "User"
                nick = data.get("nickname") or data.get("screen_name") or u_name
                avatar = (
                    data.get("avatar_url")
                    or data.get("avatar_larger")
                    or data.get("avatar_medium")
                    or data.get("avatar_thumb")
                    or data.get("avatar_300x300")
                    or data.get("avatar_168x168")
                    or data.get("avatar")
                    or ""
                )
                # Trích xuất quốc gia chính xác từ Cookie của tài khoản (store-country-code, store-country-code-ttp, region, v.v.)
                m_country = re.search(r'(?:store-country-code(?:-ttp)?|country_code|region|country)\s*=\s*([a-zA-Z]{2})', cookie_str, re.IGNORECASE)
                cookie_country = m_country.group(1).upper() if m_country else ""

                # Lấy Avatar chất lượng cao, đồng bộ Handle/Nickname và Region từ SSR TikTok
                clean_u = str(u_name or "").lstrip("@").strip()
                prof_data = {}
                if cookie_str:
                    prof_data = fetch_user_avatar_and_profile(clean_u, cookie_str=cookie_str, proxy=proxy)
                    if prof_data.get("avatar"):
                        avatar = prof_data["avatar"]
                    if prof_data.get("uniqueId") and not prof_data["uniqueId"].startswith("user"):
                        clean_u = prof_data["uniqueId"]
                    if prof_data.get("nickname"):
                        nick = prof_data["nickname"]
                elif not avatar and clean_u:
                    avatar = fetch_user_avatar(clean_u, cookie_str=cookie_str, proxy=proxy)

                country = (
                    cookie_country
                    or prof_data.get("region")
                    or data.get("store_country")
                    or data.get("country")
                    or data.get("country_code")
                    or "US"
                ).upper()
                c_ts = int(data.get("create_time") or 0)
                c_date = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y") if c_ts > 0 else "-"
                sec_uid = str(data.get("sec_user_id") or data.get("sec_uid") or "")

                return {
                    "valid": True,
                    "uid": user_id,
                    "username": f"@{clean_u}",
                    "uniqueId": clean_u,
                    "nickname": nick,
                    "avatar": avatar,
                    "country": country,
                    "create_date": c_date,
                    "sec_uid": sec_uid,
                    "email": data.get("email", ""),
                    "mobile": data.get("mobile", "") or ("Đã gán" if data.get("phone_collected") else "-")
                }
            else:
                msg = res_json.get("message") or ""
                if not msg or str(msg).lower() == "error":
                    msg = "Cookie không hợp lệ hoặc đã hết hạn (Login expired)."
                return {"valid": False, "error": msg}
        else:
            return {"valid": False, "error": f"HTTP {r.status_code} khi gọi Passport API."}
    except Exception as e:
        return {"valid": False, "error": f"Lỗi kết nối máy chủ TikTok: {str(e)}"}


def get_studio_post_list(cookie_str: str, proxy: str = "", count: int = 50, max_cursor: int = 0, sec_uid: str = "") -> Dict[str, Any]:
    """
    Lấy danh sách video Creator Studio đầy đủ 100%:
    Video Công khai, Video Riêng tư, Video Bạn bè và trạng thái vi phạm.
    """
    if not cookie_str:
        return {"success": False, "error": "Chưa có Cookie đăng nhập.", "items": []}

    cdict = parse_cookie_dict(cookie_str)
    csrf_token = cdict.get("tt_csrf_token", "")
    cookie_header = cookie_to_header_str(cdict)

    # Trích xuất quốc gia tài khoản để định tuyến đúng cụm máy chủ khu vực
    m_country = re.search(r'(?:store-country-code(?:-ttp)?|country_code|region|country)\s*=\s*([a-zA-Z]{2})', cookie_str, re.IGNORECASE)
    c_code = m_country.group(1).upper() if m_country else "US"

    # URL chuẩn của TikTok Creator Studio Item List
    url_studio = (
        f"https://www.tiktok.com/tiktok/creator/manage/item_list/v1/?"
        f"locale=vi&aid=1988&priority_region={c_code}&region={c_code}&"
        f"app_name=tiktok_creator_center&channel=tiktok_web&device_platform=web_pc"
    )
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_header,
        "Referer": "https://www.tiktok.com/tiktokstudio/content",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.tiktok.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty"
    }
    if csrf_token:
        headers["X-Secsdk-Csrf-Token"] = csrf_token
        headers["csrf-token"] = csrf_token
        headers["X-Csrf-Token"] = csrf_token

    payload = {
        "cursor": int(max_cursor),
        "size": count if count >= 20 else 50,
        "query": {
            "sort_orders": [{"field_name": "post_time", "order": 2}],
            "conditions": [],
            "is_recent_posts": False
        }
    }
    proxies = normalize_proxy_for_requests(proxy)

    raw_items = []
    has_more = False
    next_cursor = int(max_cursor)

    try:
        r = requests.post(url_studio, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=15)
        if r.status_code == 200:
            res_data = r.json()
            code = res_data.get("code", 0)
            status_code = res_data.get("status_code", 0)

            # Lấy danh sách item từ phản hồi TikTok Studio
            raw_items = res_data.get("item_list") or res_data.get("data", {}).get("item_list") or res_data.get("items") or []
            has_more = bool(res_data.get("has_more") or res_data.get("data", {}).get("has_more", False))
            next_cursor = int(res_data.get("cursor") or (max_cursor + len(raw_items)))

            # Nếu yêu cầu count > 50 (ví dụ mặc định 100 video) mà lượt 1 TikTok trả về tối đa 50:
            while len(raw_items) > 0 and len(raw_items) < count and has_more:
                remain = count - len(raw_items)
                payload_next = dict(payload)
                payload_next["cursor"] = next_cursor
                payload_next["size"] = min(remain, 50)
                try:
                    r2 = requests.post(url_studio, headers=headers, json=payload_next, proxies=proxies if proxies else None, timeout=12)
                    if r2.status_code == 200:
                        res2 = r2.json()
                        items2 = res2.get("item_list") or res2.get("data", {}).get("item_list") or res2.get("items") or []
                        if not items2:
                            break
                        raw_items.extend(items2)
                        has_more = bool(res2.get("has_more") or res2.get("data", {}).get("has_more", False))
                        next_cursor = int(res2.get("cursor") or (next_cursor + len(items2)))
                    else:
                        break
                except Exception:
                    break

            # Nếu item_list/v1 báo lỗi hoặc rỗng
            if not raw_items and (code == 8 or status_code == 8 or "login expired" in str(res_data).lower()):
                # Xác thực chéo lại cookie xem có thực sự hết hạn không hay do lỗi query
                acc_chk = get_account_info(cookie_str, proxy=proxy)
                if not acc_chk.get("valid"):
                    return {"success": False, "error": "Phiên đăng nhập đã hết hạn. Vui lòng nạp lại Cookie hoặc quét QR mới.", "items": []}

        # Fallback 1: Web API post/item_list nếu API Studio rỗng và có sec_uid
        if not raw_items and sec_uid:
            try:
                url_web = (
                    f"https://www.tiktok.com/api/post/item_list/?aid=1988&app_language=vi-VN&"
                    f"app_name=tiktok_web&channel=tiktok_web&device_platform=web_pc&"
                    f"secUid={sec_uid}&count={count}&cursor={max_cursor}"
                )
                rw = requests.get(url_web, headers={"User-Agent": DEFAULT_UA, "Cookie": cookie_header, "Referer": "https://www.tiktok.com/"}, proxies=proxies if proxies else None, timeout=12)
                if rw.status_code == 200:
                    dw = rw.json()
                    raw_items = dw.get("itemList") or dw.get("items") or []
                    has_more = bool(dw.get("hasMore", False))
                    next_cursor = int(dw.get("cursor") or (max_cursor + len(raw_items)))
            except Exception:
                pass

        # Fallback 2: Aweme Post API
        if not raw_items and sec_uid:
            aweme_hosts = ["api16-core-useast8.tiktokv.us", "api19-core-c-useast8.tiktokv.us"]
            for host in aweme_hosts:
                if raw_items:
                    break
                try:
                    url_aw = f"https://{host}/aweme/v1/aweme/post/?sec_user_id={sec_uid}&count={count}&max_cursor={max_cursor}"
                    ra = requests.get(url_aw, headers={"User-Agent": DEFAULT_UA, "Cookie": cookie_header}, proxies=proxies if proxies else None, timeout=10)
                    if ra.status_code == 200:
                        da = ra.json()
                        raw_items = da.get("aweme_list") or []
                        has_more = bool(da.get("has_more", False))
                        next_cursor = int(da.get("max_cursor") or (max_cursor + len(raw_items)))
                except Exception:
                    pass

        # Quét song song án phạt kiểm duyệt (FYP / Shadowban) & gậy Không nguyên bản Quỹ BKT (Creator Rewards >= 60s)
        if raw_items:
            try:
                import core_dedup as _cd
                from concurrent.futures import ThreadPoolExecutor as _TPE
                sync_targets = [it for it in raw_items[:50] if isinstance(it, dict)]
                for _it in sync_targets:
                    if not _it.get("aweme_id"):
                        _cand = str(_it.get("item_id") or _it.get("id") or "").strip()
                        if _cand:
                            _it["aweme_id"] = _cand
                if sync_targets:
                    def _sync_pen_fn():
                        fn = getattr(_cd, "sync_items_penalty_data", None)
                        if fn:
                            try:
                                fn(sync_targets, cookie_str, DEFAULT_UA, proxy, c_code)
                            except Exception:
                                pass

                    def _sync_crp_fn():
                        fn = getattr(_cd, "sync_items_crp_punishment_data", None)
                        if fn:
                            try:
                                fn(sync_targets, cookie_str, DEFAULT_UA, proxy, c_code)
                            except Exception:
                                pass

                    with _TPE(max_workers=2) as _pool:
                        _f1 = _pool.submit(_sync_pen_fn)
                        _f2 = _pool.submit(_sync_crp_fn)
                        _f1.result()
                        _f2.result()
            except Exception:
                pass

        # Parse chuẩn hóa toàn bộ dữ liệu video & trích xuất Avatar kênh từ item
        parsed_list = []
        author_avatar = ""

        for it in raw_items:
            # Tìm avatar kênh từ thông tin author nếu có
            if not author_avatar:
                auth = it.get("author") if isinstance(it.get("author"), dict) else {}
                av = auth.get("avatarLarger") or auth.get("avatarMedium") or auth.get("avatarThumb") or auth.get("avatar_url") or ""
                if isinstance(av, dict):
                    ul = av.get("url_list") or []
                    if ul:
                        av = ul[0]
                if isinstance(av, str) and av.startswith("http"):
                    author_avatar = av

            auth = it.get("author") if isinstance(it.get("author"), dict) else {}
            item_author = (
                auth.get("unique_id")
                or auth.get("uniqueId")
                or it.get("author_unique_id")
                or it.get("author_name")
                or ""
            )

            aid = str(it.get("item_id") or it.get("id") or it.get("aweme_id") or "")
            if not aid:
                continue

            desc = it.get("desc") or it.get("title") or "(không có mô tả)"
            stats = it.get("stats") or it.get("statistics") or {}
            play_cnt = int(it.get("play_count") or stats.get("play_count") or stats.get("playCount") or 0)
            digg_cnt = int(it.get("like_count") or it.get("digg_count") or stats.get("digg_count") or stats.get("diggCount") or 0)
            cmt_cnt = int(it.get("comment_count") or stats.get("comment_count") or stats.get("commentCount") or 0)
            share_cnt = int(it.get("share_count") or stats.get("share_count") or stats.get("shareCount") or 0)
            collect_cnt = int(it.get("collect_count") or stats.get("collect_count") or stats.get("collectCount") or 0)

            # Thumbnail an toàn hỗ trợ list/dict/string
            cover_url = ""
            cu = it.get("cover_url") or it.get("cover")
            if isinstance(cu, list) and cu:
                cover_url = cu[0]
            elif isinstance(cu, str):
                cover_url = cu
            elif isinstance(cu, dict):
                ul = cu.get("url_list") or []
                if ul:
                    cover_url = ul[0]
            if not cover_url:
                vid_obj = it.get("video") if isinstance(it.get("video"), dict) else {}
                c_sub = vid_obj.get("cover") if isinstance(vid_obj, dict) else {}
                if isinstance(c_sub, dict) and c_sub.get("url_list"):
                    cover_url = c_sub["url_list"][0]

            # Thời gian tạo
            c_ts = int(it.get("post_time") or it.get("create_time") or 0)
            date_str = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y %H:%M") if c_ts > 0 else "-"

            # Thời lượng
            dur_ms = int(it.get("duration") or (it.get("video", {}).get("duration") if isinstance(it.get("video"), dict) else 0) or 0)
            dur_sec = dur_ms // 1000 if dur_ms > 1000 else dur_ms
            dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else ""

            # Quyền riêng tư (Visibility)
            status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
            # Trạng thái kiểm duyệt TikTok (Mã 141 = Khiếu nại bản quyền, 140-149 = Vi phạm / Takedown)
            st_val = it.get("status")
            st_code = 0
            if isinstance(st_val, (int, str)) and str(st_val).isdigit():
                st_code = int(st_val)
            elif isinstance(st_val, dict):
                st_code = int(st_val.get("video_status") or 0)

            is_copyright = bool(
                st_code == 141
                or it.get("is_copyright") is True
                or (isinstance(it.get("desc"), str) and "bản quyền" in it["desc"].lower())
                or (isinstance(it.get("penalty_details"), str) and "bản quyền" in it["penalty_details"].lower())
            )
            # 140 = Riêng tư (Only Me) | 143 = Chỉ Bạn bè (Friends) -> KHÔNG phải bị gỡ bỏ!
            # 141 = Khóa Bản quyền | 142, 144 = Bị gỡ bỏ do vi phạm Tiêu chuẩn Cộng đồng
            is_takedown = bool(st_code in (141, 142, 144) or (it.get("is_takedown") is True and st_code not in (140, 143)))

            vis_val = it.get("visibility")
            if vis_val is None:
                vis_val = status_dict.get("video_visibility") or status_dict.get("private_status") or (2 if is_copyright else 1)
            vis_type = int(vis_val or 1)

            is_priv = bool(vis_type == 2 or status_dict.get("is_private") or it.get("is_private") or is_copyright)
            is_frd = bool(vis_type in (3, 4, 5) or status_dict.get("is_friend") or it.get("is_friend"))

            if is_copyright:
                visibility_code = 2
                visibility_label = "Khóa Bản quyền (Ẩn)"
                visibility_badge = "bg-rose-950 text-rose-300 border-rose-800"
            elif is_priv:
                visibility_code = 2
                visibility_label = "Riêng tư (Ẩn)"
                visibility_badge = "bg-rose-950 text-rose-300 border-rose-800"
            elif is_frd:
                visibility_code = 3
                visibility_label = "Bạn bè"
                visibility_badge = "bg-amber-950 text-amber-300 border-amber-800"
            else:
                visibility_code = 1
                visibility_label = "Công khai"
                visibility_badge = "bg-emerald-950 text-emerald-300 border-emerald-800"

            # Trùng lặp & Án phạt
            group_id = str(it.get("group_id") or it.get("groupId") or "")
            is_group_dup = bool(group_id and group_id != aid and group_id not in ("0", "None", "null"))
            is_crp_punished = bool(it.get("is_crp_punished", False))
            is_fyp_restricted = bool(it.get("is_fyp_restricted", False) or it.get("shadow", False) or it.get("shadowban", False))
            raw_pen_det = str(it.get("penalty_details") or "").strip()
            crp_pen_det = str(it.get("crp_penalty_details") or "").strip()
            if crp_pen_det and raw_pen_det and crp_pen_det != raw_pen_det:
                pen_details = f"{crp_pen_det} · {raw_pen_det}"
            else:
                pen_details = crp_pen_det or raw_pen_det
            is_dup = bool(is_group_dup or is_crp_punished)
            in_rev = bool(it.get("in_review"))
            penalty_ctx = it.get("penaltyContext") if isinstance(it.get("penaltyContext"), dict) else {}
            has_penalty = bool(
                is_copyright
                or is_takedown
                or is_crp_punished
                or is_fyp_restricted
                or bool(it.get("has_penalty", False))
                or int(penalty_ctx.get("display_penalty_type") or 0) != 0
                or int(penalty_ctx.get("display_policy") or 0) != 0
                or bool(it.get("isProhibited"))
                or in_rev
                or is_dup
            )

            parsed_list.append({
                "item_id": aid,
                "aweme_id": aid,
                "desc": desc,
                "cover_url": cover_url,
                "duration_str": dur_str,
                "date_str": date_str,
                "play_count": play_cnt,
                "digg_count": digg_cnt,
                "comment_count": cmt_cnt,
                "share_count": share_cnt,
                "collect_count": collect_cnt,
                "visibility_code": visibility_code,
                "visibility_label": visibility_label,
                "visibility_badge": visibility_badge,
                "is_dup": is_dup,
                "is_crp_punished": is_crp_punished,
                "is_fyp_restricted": is_fyp_restricted,
                "penalty_details": pen_details,
                "is_copyright": is_copyright,
                "is_takedown": is_takedown,
                "status_code": st_code,
                "group_id": group_id,
                "origin_url": f"https://www.tiktok.com/@tiktok/video/{group_id}" if is_group_dup else "",
                "has_penalty": has_penalty,
                "share_url": f"https://www.tiktok.com/@{item_author}/video/{aid}" if item_author else f"https://www.tiktok.com/video/{aid}"
            })

        return {
            "success": True,
            "items": parsed_list,
            "author_avatar": author_avatar,
            "has_more": has_more,
            "cursor": next_cursor,
            "total_count": len(parsed_list)
        }
    except Exception as e:
        return {"success": False, "error": f"Lỗi gọi Studio API: {str(e)}", "items": []}


def set_post_visibility(cookie_str: str, item_id: str, visibility_type: int, proxy: str = "") -> Dict[str, Any]:
    """
    Thay đổi chế độ hiển thị video trên TikTok Studio:
    1 = Công khai (Public)
    2 = Riêng tư / Ẩn (Private)
    3 = Bạn bè (Friends)
    """
    if not cookie_str or not item_id:
        return {"success": False, "error": "Thiếu Cookie hoặc Video ID."}

    cdict = parse_cookie_dict(cookie_str)
    csrf_token = cdict.get("tt_csrf_token", "")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_to_header_str(cdict),
        "Content-Type": "application/json; charset=utf-8",
        "Referer": "https://www.tiktok.com/tiktokstudio/content",
        "Origin": "https://www.tiktok.com",
        "Accept": "application/json, text/plain, */*"
    }
    if csrf_token:
        headers["X-Secsdk-Csrf-Token"] = csrf_token
        headers["csrf-token"] = csrf_token
        headers["X-Csrf-Token"] = csrf_token

    payload = {
        "itemId": str(item_id).strip(),
        "visibilityType": int(visibility_type)
    }
    proxies = normalize_proxy_for_requests(proxy)

    try:
        url = "https://www.tiktok.com/tiktokstudio/api/web/item/visibility"
        r = requests.post(url, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=12)
        # Nếu máy chủ TikTok bị quá tải trả về HTTP 500/502/503/504, tự động thử lại sau 800ms
        if r.status_code >= 500:
            time.sleep(0.8)
            r = requests.post(url, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=15)

        if r.status_code == 200:
            res_data = r.json()
            st = res_data.get("status_code", res_data.get("code", 0))
            if st == 0 or res_data.get("message") == "success":
                label = "Công khai" if visibility_type == 1 else ("Riêng tư (Ẩn)" if visibility_type == 2 else "Bạn bè")
                return {"success": True, "message": f"Đã chuyển video sang chế độ {label} thành công!"}
            else:
                msg = res_data.get("status_msg") or res_data.get("message") or f"Mã phản hồi: {st}"
                return {"success": False, "error": f"TikTok từ chối: {msg}"}
        else:
            return {"success": False, "error": f"Lỗi kết nối máy chủ TikTok: HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"Lỗi đổi trạng thái video: {str(e)}"}


def delete_studio_post(cookie_str: str, item_id: str, proxy: str = "") -> Dict[str, Any]:
    """Xóa vĩnh viễn video trên TikTok Studio."""
    if not cookie_str or not item_id:
        return {"success": False, "error": "Thiếu Cookie hoặc Video ID."}

    cdict = parse_cookie_dict(cookie_str)
    csrf_token = cdict.get("tt_csrf_token", "")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_to_header_str(cdict),
        "Content-Type": "application/json; charset=utf-8",
        "Referer": "https://www.tiktok.com/tiktokstudio/content",
        "Origin": "https://www.tiktok.com",
        "Accept": "application/json, text/plain, */*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }
    if csrf_token:
        headers["X-Secsdk-Csrf-Token"] = csrf_token
        headers["csrf-token"] = csrf_token
        headers["X-Csrf-Token"] = csrf_token

    payload = {
        "itemId": str(item_id).strip(),
        "item_id": str(item_id).strip()
    }
    proxies = normalize_proxy_for_requests(proxy)

    try:
        url = "https://www.tiktok.com/tiktokstudio/api/web/item/delete"
        r = requests.post(url, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=15)
        # Nếu máy chủ TikTok bị quá tải trả về HTTP 500/502/503/504, tự động thử lại sau 800ms
        if r.status_code >= 500:
            time.sleep(0.8)
            r = requests.post(url, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=15)

        if r.status_code == 200:
            res_data = r.json()
            st = res_data.get("status_code", res_data.get("code", 0))
            if st == 0 or res_data.get("message") == "success":
                return {"success": True, "message": "Đã xóa vĩnh viễn video thành công khỏi kênh!"}
            else:
                msg = res_data.get("status_msg") or res_data.get("message") or f"Mã phản hồi: {st}"
                return {"success": False, "error": f"TikTok từ chối xóa: {msg}"}
        else:
            return {"success": False, "error": f"Lỗi kết nối máy chủ TikTok: HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"Lỗi xóa video: {str(e)}"}


# Quản lý trạng thái đăng nhập trình duyệt tương tác bất đồng bộ (tránh timeout Cloudflare)
_BROWSER_LOGIN_STATE: Dict[str, Any] = {
    "status": "idle",
    "run_id": "",
    "account": None,
    "cookie": "",
    "proxy": "",
    "qr_image": "",
    "message": "",
    "error": "",
    "updated_at": 0
}
_BROWSER_LOGIN_LOCK = threading.Lock()
_BROWSER_STOP_EVENT = threading.Event()


def start_interactive_browser_login(timeout_sec: int = 300, proxy: str = "", force: bool = False, mode: str = "qr") -> Dict[str, Any]:
    """Khởi động luồng chạy Playwright Chromium (hỗ trợ chế độ stream QR hoặc mở cửa sổ desktop)."""
    global _BROWSER_LOGIN_STATE, _BROWSER_STOP_EVENT
    with _BROWSER_LOGIN_LOCK:
        current_status = _BROWSER_LOGIN_STATE.get("status", "idle")
        updated_at = _BROWSER_LOGIN_STATE.get("updated_at", 0)
        # Nếu đang mở sẵn và vừa cập nhật trong vòng 45 giây gần đây và không yêu cầu làm mới
        if not force and current_status in ("starting", "waiting", "qr_ready") and (time.time() - updated_at < 45):
            return {
                "success": True,
                "status": current_status,
                "run_id": _BROWSER_LOGIN_STATE.get("run_id", ""),
                "qr_image": _BROWSER_LOGIN_STATE.get("qr_image", ""),
                "message": _BROWSER_LOGIN_STATE.get("message", "Mã QR đã sẵn sàng, hãy dùng app TikTok để quét!")
            }

        # Báo hiệu dừng phiên cũ nếu có
        _BROWSER_STOP_EVENT.set()
        _BROWSER_STOP_EVENT = threading.Event()
        run_id = uuid.uuid4().hex

        _BROWSER_LOGIN_STATE = {
            "status": "starting",
            "run_id": run_id,
            "account": None,
            "cookie": "",
            "proxy": proxy,
            "qr_image": "",
            "message": "Đang kết nối TikTok & tạo mã QR..." if mode == "qr" else "Đang khởi động cửa sổ Chrome...",
            "error": "",
            "updated_at": time.time()
        }

        t = threading.Thread(
            target=_worker_browser_login,
            args=(timeout_sec, _BROWSER_STOP_EVENT, proxy, mode, run_id),
            daemon=True
        )
        t.start()

        return {
            "success": True,
            "status": "starting",
            "run_id": run_id,
            "message": "Đang kết nối TikTok và tạo mã QR đăng nhập..." if mode == "qr" else "Đang mở cửa sổ trình duyệt Chrome..."
        }


def get_interactive_login_status() -> Dict[str, Any]:
    """Lấy trạng thái phiên đăng nhập trình duyệt hiện tại."""
    global _BROWSER_LOGIN_STATE
    with _BROWSER_LOGIN_LOCK:
        return dict(_BROWSER_LOGIN_STATE)


def cancel_interactive_browser_login() -> Dict[str, Any]:
    """Hủy phiên đăng nhập trình duyệt và đóng cửa sổ."""
    global _BROWSER_LOGIN_STATE, _BROWSER_STOP_EVENT
    _BROWSER_STOP_EVENT.set()
    with _BROWSER_LOGIN_LOCK:
        _BROWSER_LOGIN_STATE["status"] = "cancelled"
        _BROWSER_LOGIN_STATE["qr_image"] = ""
        _BROWSER_LOGIN_STATE["error"] = "Đã hủy phiên đăng nhập."
        _BROWSER_LOGIN_STATE["updated_at"] = time.time()
    return {"success": True, "status": "cancelled"}


def _worker_browser_login(timeout_sec: int, stop_event: threading.Event, proxy: str = "", mode: str = "qr", run_id: str = ""):
    """Luồng worker chạy Playwright Chromium (hỗ trợ Headless QR stream hoặc mở cửa sổ Desktop)."""
    global _BROWSER_LOGIN_STATE
    browser = None
    bridge = None
    is_headless = (mode != "window")

    def _set_state(**fields: Any) -> bool:
        with _BROWSER_LOGIN_LOCK:
            if run_id and _BROWSER_LOGIN_STATE.get("run_id") != run_id:
                return False
            _BROWSER_LOGIN_STATE.update(fields)
            _BROWSER_LOGIN_STATE["updated_at"] = time.time()
            return True

    try:
        if stop_event.is_set():
            return
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": is_headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-webrtc-multiple-routes",
                    "--enforce-webrtc-ip-permission-check"
                ]
            }
            if proxy:
                pw_proxy, bridge = setup_browser_proxy(proxy)
                if pw_proxy:
                    launch_kwargs["proxy"] = pw_proxy

            # Ưu tiên mở bằng Google Chrome thật nếu có sẵn trên máy để mở siêu nhanh
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = p.chromium.launch(**launch_kwargs)

            # Tự động đồng bộ Múi giờ (Timezone) và Ngôn ngữ (Locale) theo quốc gia Proxy
            tz = "America/Los_Angeles"
            loc = "en-US"
            if proxy:
                p_lower = proxy.lower()
                if "11104" in proxy or "11105" in proxy or "nyc" in p_lower or "new_york" in p_lower:
                    tz = "America/New_York"
                elif "11103" in proxy or "las_vegas" in p_lower:
                    tz = "America/Los_Angeles"
                elif "11102" in proxy or "silicon" in p_lower:
                    tz = "America/Los_Angeles"
                elif "11108" in proxy or "11109" in proxy or ":de" in p_lower or "germany" in p_lower:
                    tz = "Europe/Berlin"
                    loc = "de-DE"
                elif "11106" in proxy or ":id" in p_lower or "jakarta" in p_lower or "indonesia" in p_lower:
                    tz = "Asia/Jakarta"
                    loc = "id-ID"
                elif "11107" in proxy or ":mx" in p_lower or "mexico" in p_lower:
                    tz = "America/Mexico_City"
                    loc = "es-MX"
                elif "1080" in proxy or "singapore" in p_lower:
                    tz = "Asia/Singapore"
                    loc = "en-SG"
                elif "1081" in proxy or "tokyo" in p_lower or "japan" in p_lower:
                    tz = "Asia/Tokyo"
                    loc = "ja-JP"
                elif "1082" in proxy or "taiwan" in p_lower:
                    tz = "Asia/Taipei"
                    loc = "zh-TW"
                elif "1083" in proxy or "hong_kong" in p_lower or "hk" in p_lower:
                    tz = "Asia/Hong_Kong"
                    loc = "zh-HK"

            context = browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1050, "height": 750},
                locale=loc,
                timezone_id=tz
            )
            context.add_init_script("delete Object.getPrototypeOf(navigator).webdriver")
            page = context.new_page()

            browser_closed = {"flag": False}
            try:
                browser.on("disconnected", lambda: browser_closed.__setitem__("flag", True))
                context.on("close", lambda: browser_closed.__setitem__("flag", True))
                page.on("close", lambda: browser_closed.__setitem__("flag", True) if len(context.pages) == 0 else None)
            except Exception:
                pass

            _set_state(status="waiting", message="Đang kết nối TikTok...")

            # Luôn truy cập https://www.tiktok.com/login (chỉ tự bấm Use QR ở chế độ stream headless)
            try:
                page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass

            if is_headless:
                try:
                    btn = page.locator("text=/Use QR|Dùng mã QR|Quét mã QR/i").first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                except Exception:
                    pass

            qr_captured = False
            last_qr_ts = 0

            # Vòng lặp chờ mã QR và theo dõi đăng nhập / phát hiện đóng cửa sổ tức thì
            start_ts = time.time()
            while time.time() - start_ts < timeout_sec:
                if stop_event.is_set():
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                try:
                    is_closed_now = browser_closed["flag"] or (not browser.is_connected()) or (len(context.pages) == 0) or all(pg.is_closed() for pg in context.pages)
                except Exception:
                    is_closed_now = True
                if is_closed_now:
                    _set_state(status="closed", message="Cửa sổ Chrome đã đóng.", error="")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                # Bắt mã QR ĐĂNG NHẬP THẬT (chỉ dùng cho luồng headless QR stream)
                if is_headless:
                    if not qr_captured:
                        try:
                            qr_btn = page.locator("text=/Use QR|Dùng mã QR|Quét mã QR/i").first
                            if qr_btn.is_visible(timeout=200):
                                qr_btn.click()
                        except Exception:
                            pass

                        try:
                            canvas = page.query_selector("canvas")
                            if canvas:
                                box = canvas.bounding_box()
                                if box and box["width"] > 40 and box["height"] > 40:
                                    qr_url = canvas.evaluate("el => el.toDataURL()")
                                    if qr_url and len(qr_url) > 2000:
                                        _set_state(
                                            status="qr_ready",
                                            qr_image=qr_url,
                                            message="Mã QR đã sẵn sàng! Mở app TikTok để quét.",
                                        )
                                        qr_captured = True
                                        last_qr_ts = time.time()
                        except Exception:
                            pass
                    elif time.time() - last_qr_ts > 15:
                        try:
                            reload_btn = page.locator("text=/Click to refresh|Nhấp để làm mới|Refresh|Tải lại/i").first
                            if reload_btn.is_visible(timeout=150):
                                try:
                                    reload_btn.click()
                                    time.sleep(0.6)
                                except Exception:
                                    pass
                            canvas = page.query_selector("canvas")
                            if canvas:
                                qr_url = canvas.evaluate("el => el.toDataURL()")
                                if qr_url and len(qr_url) > 2000 and qr_url != _BROWSER_LOGIN_STATE.get("qr_image"):
                                    _set_state(qr_image=qr_url)
                                    last_qr_ts = time.time()
                        except Exception:
                            pass

                # Kiểm tra cookie sessionid khi người dùng đăng nhập xong trên Chrome
                try:
                    cookies = context.cookies()
                except Exception:
                    if not stop_event.is_set():
                        _set_state(status="closed", message="Cửa sổ Chrome đã đóng.", error="")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                sid = next((c['value'] for c in cookies if c.get('name') in ('sessionid', 'sessionid_ss', 'sid_tt') and len(str(c.get('value') or '').strip()) > 8), None)
                if sid:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get('name') and c.get('value'))
                    acc_info = get_account_info(cookie_str, proxy=proxy)
                    try:
                        page.wait_for_timeout(800)
                        av_found = page.evaluate("""() => {
                            const img = document.querySelector('[data-e2e="profile-icon"] img, img[class*="avatar"], img[alt*="avatar"], header img[src*="tiktok"]');
                            if (img && (img.currentSrc || img.src)) return img.currentSrc || img.src;
                            try {
                                const u = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
                                if (u && u.__DEFAULT_SCOPE__) {
                                    const ud = u.__DEFAULT_SCOPE__["webapp.app-context"] || u.__DEFAULT_SCOPE__["webapp.user-detail"] || {};
                                    const user = ud.user || (ud.userInfo && ud.userInfo.user);
                                    if (user) return user.avatarLarger || user.avatarMedium || user.avatarThumb || "";
                                }
                            } catch(e) {}
                            return "";
                        }""")
                        if av_found and not acc_info.get("avatar"):
                            acc_info["avatar"] = av_found
                    except Exception:
                        pass

                    try:
                        browser.close()
                    except Exception:
                        pass

                    _set_state(
                        status="success",
                        account=acc_info,
                        cookie=cookie_str,
                        proxy=proxy,
                        message="Đăng nhập thành công!",
                        error="",
                    )

                    u_key = (acc_info.get("uniqueId") or acc_info.get("username", "")).lstrip("@").strip().lower()
                    if u_key:
                        ACTIVE_STUDIO_ACCOUNTS[u_key] = {
                            "account": acc_info,
                            "cookie": cookie_str,
                            "proxy": proxy,
                            "updated_at": time.time()
                        }
                    uid_key = str(acc_info.get("uid") or "").strip()
                    if uid_key:
                        ACTIVE_STUDIO_ACCOUNTS[uid_key] = {
                            "account": acc_info,
                            "cookie": cookie_str,
                            "proxy": proxy,
                            "updated_at": time.time()
                        }
                    return

                time.sleep(0.5)

            # Quá thời gian chờ (5 phút)
            try:
                browser.close()
            except Exception:
                pass
            if not stop_event.is_set():
                _set_state(status="timeout", error="Đã hết thời gian chờ đăng nhập (5 phút).")

    except Exception as e:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if not stop_event.is_set():
            _set_state(status="error", error=f"Lỗi mở trình duyệt: {str(e)}")
    finally:
        if bridge:
            try:
                bridge.stop()
            except Exception:
                pass


def run_interactive_browser_login(timeout_sec: int = 300) -> Dict[str, Any]:
    """Hàm đồng bộ tương thích ngược: bắt đầu và đợi kết quả."""
    start_interactive_browser_login(timeout_sec=timeout_sec)
    start_ts = time.time()
    while time.time() - start_ts < timeout_sec + 5:
        st = get_interactive_login_status()
        if st.get("status") == "success":
            return {"success": True, "account": st.get("account"), "cookie": st.get("cookie")}
        elif st.get("status") in ("cancelled", "error", "timeout"):
            return {"success": False, "error": st.get("error", "Đăng nhập không thành công.")}
        time.sleep(1)
    return {"success": False, "error": "Đã hết thời gian chờ đăng nhập (5 phút)."}


