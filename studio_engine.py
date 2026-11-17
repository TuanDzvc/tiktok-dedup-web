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
import socks
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
    """Chuẩn hóa cookie thành chuỗi header HTTP."""
    if not cookie_val:
        return ""
    cdict = parse_cookie_dict(cookie_val) if not isinstance(cookie_val, dict) else cookie_val
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
    """
    clean_u = (username or "").lstrip("@").strip()
    if not clean_u:
        return ""
    
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }
    if cookie_str:
        headers["Cookie"] = cookie_to_header_str(cookie_str)
    else:
        try:
            pool_path = Path(__file__).resolve().parent / "cookie_pool.json"
            if pool_path.exists():
                pool_data = json.loads(pool_path.read_text(encoding="utf-8"))
                if pool_data.get("cookie"):
                    headers["Cookie"] = pool_data["cookie"]
                if pool_data.get("user_agent"):
                    headers["User-Agent"] = pool_data["user_agent"]
        except Exception:
            pass

    proxies = normalize_proxy_for_requests(proxy)

    try:
        r = requests.get(
            f"https://www.tiktok.com/@{clean_u}",
            headers=headers,
            proxies=proxies if proxies else None,
            timeout=8
        )
        if r.status_code == 200:
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
        pass
    return ""

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
                # Trích xuất quốc gia chính xác từ Cookie của tài khoản (store-country-code, v.v.)
                m_country = re.search(r'(?:store-country-code|country_code|region|country)\s*=\s*([a-zA-Z]{2})', cookie_str, re.IGNORECASE)
                cookie_country = m_country.group(1).upper() if m_country else ""

                country = (
                    cookie_country
                    or data.get("store_country")
                    or data.get("country")
                    or data.get("region")
                    or "VN"
                ).upper()
                c_ts = int(data.get("create_time") or 0)
                c_date = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y") if c_ts > 0 else "-"
                sec_uid = str(data.get("sec_user_id") or data.get("sec_uid") or "")

                # Lấy Avatar chất lượng cao từ SSR profile TikTok với Cookie vừa nạp
                if not avatar and u_name:
                    avatar = fetch_user_avatar(u_name, cookie_str=cookie_str, proxy=proxy)

                return {
                    "valid": True,
                    "uid": user_id,
                    "username": f"@{u_name}".replace("@@", "@"),
                    "uniqueId": u_name,
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
    m_country = re.search(r'store-country-code\s*=\s*([a-zA-Z]{2})', cookie_str, re.IGNORECASE)
    c_code = m_country.group(1).upper() if m_country else "VN"

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
            vis_val = it.get("visibility")
            if vis_val is None:
                vis_val = status_dict.get("video_visibility") or status_dict.get("private_status") or 1
            vis_type = int(vis_val or 1)

            is_priv = bool(vis_type == 2 or status_dict.get("is_private") or it.get("is_private"))
            is_frd = bool(vis_type in (3, 4, 5) or status_dict.get("is_friend") or it.get("is_friend"))

            if is_priv:
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
            is_dup = bool(group_id and group_id != aid and group_id not in ("0", "None", "null"))
            in_rev = bool(it.get("in_review"))
            penalty_ctx = it.get("penaltyContext") if isinstance(it.get("penaltyContext"), dict) else {}
            has_penalty = bool(
                int(penalty_ctx.get("display_penalty_type") or 0) != 0
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
                "group_id": group_id,
                "origin_url": f"https://www.tiktok.com/@tiktok/video/{group_id}" if is_dup else "",
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
    "account": None,
    "cookie": "",
    "proxy": "",
    "error": "",
    "updated_at": 0
}
_BROWSER_LOGIN_LOCK = threading.Lock()
_BROWSER_STOP_EVENT = threading.Event()


def start_interactive_browser_login(timeout_sec: int = 300, proxy: str = "") -> Dict[str, Any]:
    """Khởi động luồng chạy trình duyệt đăng nhập nền (hỗ trợ HTTP & SOCKS5) và phản hồi ngay lập tức để không bao giờ bị timeout."""
    global _BROWSER_LOGIN_STATE, _BROWSER_STOP_EVENT
    with _BROWSER_LOGIN_LOCK:
        current_status = _BROWSER_LOGIN_STATE.get("status", "idle")
        updated_at = _BROWSER_LOGIN_STATE.get("updated_at", 0)
        # Nếu đang mở sẵn và vừa cập nhật trong vòng 45 giây gần đây
        if current_status in ("starting", "waiting") and (time.time() - updated_at < 45):
            return {
                "success": True,
                "status": current_status,
                "message": "Trình duyệt đang mở sẵn trên màn hình, vui lòng quét mã QR hoặc đăng nhập!"
            }

        # Báo hiệu dừng phiên cũ nếu có
        _BROWSER_STOP_EVENT.set()
        _BROWSER_STOP_EVENT = threading.Event()

        _BROWSER_LOGIN_STATE = {
            "status": "starting",
            "account": None,
            "cookie": "",
            "proxy": proxy,
            "error": "",
            "updated_at": time.time()
        }

        t = threading.Thread(
            target=_worker_browser_login,
            args=(timeout_sec, _BROWSER_STOP_EVENT, proxy),
            daemon=True
        )
        t.start()

        return {
            "success": True,
            "status": "starting",
            "message": "Đang mở cửa sổ trình duyệt Chrome..."
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
        _BROWSER_LOGIN_STATE["error"] = "Đã hủy phiên đăng nhập."
        _BROWSER_LOGIN_STATE["updated_at"] = time.time()
    return {"success": True, "status": "cancelled"}


def _worker_browser_login(timeout_sec: int, stop_event: threading.Event, proxy: str = ""):
    """Luồng worker chạy Playwright Chromium/Chrome mở cửa sổ thực tế trên desktop (hỗ trợ HTTP & SOCKS5 chống leak IP)."""
    global _BROWSER_LOGIN_STATE
    browser = None
    bridge = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
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

            context = browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1050, "height": 750}
            )
            context.add_init_script("delete Object.getPrototypeOf(navigator).webdriver")
            page = context.new_page()

            with _BROWSER_LOGIN_LOCK:
                _BROWSER_LOGIN_STATE["status"] = "waiting"
                _BROWSER_LOGIN_STATE["updated_at"] = time.time()

            try:
                # Dùng wait_until="domcontentloaded" để cửa sổ bật lên ngay tức thì trong 1-2s
                page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass

            try:
                page.locator("text=/Use QR|Dùng mã QR|Quét mã QR/i").first.click(timeout=1500)
            except Exception:
                pass

            start_ts = time.time()
            while time.time() - start_ts < timeout_sec:
                if stop_event.is_set():
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                try:
                    cookies = context.cookies()
                except Exception:
                    # Người dùng đã tắt cửa sổ trình duyệt bằng nút X đỏ
                    with _BROWSER_LOGIN_LOCK:
                        _BROWSER_LOGIN_STATE["status"] = "cancelled"
                        _BROWSER_LOGIN_STATE["error"] = "Bạn đã đóng cửa sổ trình duyệt."
                        _BROWSER_LOGIN_STATE["updated_at"] = time.time()
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                sid = next((c['value'] for c in cookies if c.get('name') == 'sessionid'), None)
                if sid:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get('name') and c.get('value'))
                    acc_info = get_account_info(cookie_str, proxy=proxy)
                    # Trích xuất trực tiếp Avatar của kênh từ trình duyệt đang mở
                    try:
                        page.wait_for_timeout(1000)
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

                    with _BROWSER_LOGIN_LOCK:
                        _BROWSER_LOGIN_STATE["status"] = "success"
                        _BROWSER_LOGIN_STATE["account"] = acc_info
                        _BROWSER_LOGIN_STATE["cookie"] = cookie_str
                        _BROWSER_LOGIN_STATE["proxy"] = proxy
                        _BROWSER_LOGIN_STATE["updated_at"] = time.time()
                    return

                time.sleep(1)

            # Quá thời gian chờ (5 phút)
            try:
                browser.close()
            except Exception:
                pass
            with _BROWSER_LOGIN_LOCK:
                _BROWSER_LOGIN_STATE["status"] = "timeout"
                _BROWSER_LOGIN_STATE["error"] = "Đã hết thời gian chờ đăng nhập (5 phút)."
                _BROWSER_LOGIN_STATE["updated_at"] = time.time()

    except Exception as e:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        with _BROWSER_LOGIN_LOCK:
            _BROWSER_LOGIN_STATE["status"] = "error"
            _BROWSER_LOGIN_STATE["error"] = f"Lỗi mở trình duyệt: {str(e)}"
            _BROWSER_LOGIN_STATE["updated_at"] = time.time()
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


