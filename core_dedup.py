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

# Cố gắng import các hàm trích xuất chuyên sâu & bảo mật từ tiktok_info_checker_gui.py của tool
ConfigStore = None
unprotect_secret = None
api_get_post_list = None
sync_items_penalty_data = None
get_profile_crp_status = None

try:
    tool_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    from tiktok_info_checker_gui import (
        ConfigStore,
        unprotect_secret,
        api_get_post_list,
        sync_items_penalty_data,
        get_profile_crp_status
    )
except Exception as e:
    pass

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
    clean = re.sub(r"#[^\s#]+", "", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "(không mô tả)"

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
    max_cursor: int = 0
) -> List[Dict[str, Any]]:
    """Quét video siêu tốc qua API creator/item_list (0.5s - 1.5s, không tốn RAM Playwright)."""
    if not sec_uid or not cookie_str:
        return []
    
    all_items = []
    cursor = str(max_cursor or 0)
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

def get_borrowed_cookie() -> Tuple[str, str, str]:
    """
    Tự động chọn 1 cookie hợp lệ trong kho ConfigStore hoặc cookie_pool.json để bypass WAF TikTok.
    Trả về (cookie_str, user_agent, proxy_str)
    """
    try:
        if ConfigStore:
            store = ConfigStore()
            _, accs = store.load()
            candidates = []
            for a in accs:
                c = getattr(a, "tiktok_cookie", "")
                if c:
                    if c.startswith("dpapi:") and unprotect_secret:
                        try:
                            c = unprotect_secret(c)
                        except Exception:
                            continue
                    if len(c) > 30 and ("sessionid=" in c or "sessionid_ss=" in c):
                        candidates.append((
                            c,
                            getattr(a, "user_agent", "") or DEFAULT_UA,
                            ""
                        ))
            if candidates:
                return random.choice(candidates)
    except Exception as e:
        print(f"[BorrowCookie] Lỗi mượn cookie từ tool: {e}")

    try:
        pool_file = Path(__file__).resolve().parent / "cookie_pool.json"
        if pool_file.exists():
            data = json.loads(pool_file.read_text(encoding="utf-8"))
            c = data.get("cookie", "")
            ua = data.get("user_agent", DEFAULT_UA)
            if c:
                return c, ua, ""
    except Exception as e:
        print(f"[BorrowCookie] Lỗi đọc cookie_pool.json: {e}")

    env_c = os.getenv("TIKTOK_COOKIE", "")
    if env_c:
        return env_c, DEFAULT_UA, ""

    return "", "", ""

def fetch_channel_info_and_posts(
    channel_query: str,
    sec_uid: str = "",
    max_cursor: int = 0,
    cookie_str: str = "",
    ua_str: str = "",
    proxy_str: str = "",
    max_videos: int = 18,
) -> Dict[str, Any]:
    """
    Hàm kiểm tra trùng lặp & quét thông tin kênh TikTok.
    - Tự động nhận diện tài khoản trong ConfigStore (có cookie Studio) để quét án phạt & FYP chính xác 100% như tool.
    - Hỗ trợ lấy dữ liệu Check user (Tên, Handle, SĐT, Quốc gia, UID, Ngày tạo).
    - Hỗ trợ phân trang: mặc định quét 24 video, có thể quét thêm các đợt tiếp theo.
    """
    query = str(channel_query or "").strip()
    username = query
    if "tiktok.com/@" in query:
        username = query.split("tiktok.com/@")[-1].split("?")[0].split("#")[0].split("/")[0]
    elif "tiktok.com/" in query:
        p = query.split("tiktok.com/")[-1].split("?")[0].split("#")[0].split("/")[0]
        username = p[1:] if p.startswith("@") else p
    elif query.startswith("@"):
        username = query[1:]
    username = username.split("?")[0].split("#")[0].rstrip("/").strip()

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
        "username": f"@{username}".replace("@@", "@"),
        "create_date": "—",
        "mobile": "—",
        "country": "United States",
        "uid": "---",
        "last_nickname_change": "—",
        "is_self": False,
    }

    matched_acc_cookie = cookie_str
    matched_acc_proxy = proxy_str
    matched_acc_ua = ua_str or DEFAULT_UA
    is_self_account = bool(matched_acc_cookie)

    # 1. Tìm trong ConfigStore của tool xem có nick này không
    if username and not matched_acc_cookie and ConfigStore:
        try:
            store = ConfigStore()
            _, accs = store.load()
            for a in accs:
                un = str(getattr(a, "username", "") or getattr(a, "name", "")).lstrip("@").strip().lower()
                if un and un == username.strip().lower():
                    is_self_account = True
                    matched_acc_cookie = getattr(a, "tiktok_cookie", "")
                    if getattr(a, "proxy", ""):
                        matched_acc_proxy = getattr(a, "proxy", "")
                    if getattr(a, "user_agent", ""):
                        matched_acc_ua = getattr(a, "user_agent", "")
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
            if matched_acc_proxy:
                p_c = matched_acc_proxy.strip()
                if not p_c.startswith(("http://", "https://", "socks5://")):
                    p_c = f"http://{p_c}"
                px_dict = {"http": p_c, "https": p_c}

            r_pass = requests.get(
                "https://www.tiktok.com/passport/web/account/info/",
                headers=p_headers,
                proxies=px_dict if px_dict else None,
                timeout=8
            )
            if r_pass.status_code == 200:
                p_data = r_pass.json().get("data", {})
                if p_data:
                    check_user_info["is_self"] = True
                    p_name = p_data.get("screen_name") or p_data.get("nickname") or username
                    check_user_info["screen_name"] = p_name
                    channel_info["nickname"] = p_name

                    p_uname = p_data.get("username") or username
                    check_user_info["username"] = f"@{p_uname}".replace("@@", "@")
                    channel_info["uniqueId"] = p_uname

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
        except Exception:
            pass

    # 3. Kênh ngoài: Dùng requests lấy nhanh HTML trang chủ để lấy secUid & nickname
    if not sec_uid and username:
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
                        u_info = u_detail.get("userInfo", {})
                        u = u_info.get("user", {})
                        st = u_info.get("stats", {})

                        if u.get("uniqueId"):
                            channel_info["uniqueId"] = u["uniqueId"]
                            check_user_info["username"] = f"@{u['uniqueId']}"
                        if u.get("nickname"):
                            channel_info["nickname"] = u["nickname"]
                            check_user_info["screen_name"] = u["nickname"]
                        if u.get("avatarLarger") or u.get("avatarMedium"):
                            channel_info["avatar"] = u.get("avatarLarger") or u.get("avatarMedium") or ""
                        if st.get("followerCount") is not None:
                            channel_info["followers"] = int(st.get("followerCount") or 0)
                        if st.get("videoCount") is not None:
                            channel_info["videoCount"] = int(st.get("videoCount") or 0)
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

                        if not detected_account_country:
                            c_u = (u.get("region") or u.get("country") or "").strip().upper()
                            if c_u and c_u not in ("NONE", "AUTO", "EU") and not c_u.endswith("_TTP"):
                                detected_account_country = c_u
                            else:
                                app_ctx = scope.get("webapp.app-context", {})
                                app_user = app_ctx.get("user", {})
                                c_ssr = (app_user.get("storeRegion") or app_user.get("ageGateRegion") or "").strip().upper()
                                if c_ssr and c_ssr not in ("NONE", "AUTO", "EU") and not c_ssr.endswith("_TTP"):
                                    detected_account_country = c_ssr
                    except Exception:
                        pass

                if not sec_uid:
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
                    m_uid = re.search(r'"userId":"([0-9]+)"', html) or re.search(r'"authorId":"([0-9]+)"', html)
                    if m_uid:
                        check_user_info["uid"] = m_uid.group(1)
                        if channel_info.get("createDate") in ("", "—"):
                            sf_date = decode_snowflake_timestamp(m_uid.group(1))
                            if sf_date:
                                channel_info["createDate"] = sf_date
                                check_user_info["create_date"] = sf_date
        except Exception:
            pass

    if detected_account_country and detected_account_country not in ("NONE", "AUTO", "EU") and not detected_account_country.endswith("_TTP"):
        channel_info["region"] = detected_account_country
    else:
        channel_info["region"] = "US"

    reg_disp = format_full_country_display(channel_info["region"])
    channel_info["region_display"] = reg_disp
    check_user_info["country"] = reg_disp

    raw_items = []
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
            if all_studio_items:
                offset = int(max_cursor) if max_cursor > 0 else 0
                raw_items = all_studio_items[offset:offset + max_videos]
                next_cursor = offset + len(raw_items)
                total_vids = int(channel_info.get("videoCount") or 0)
                if total_vids > 0:
                    has_more = next_cursor < total_vids
                else:
                    has_more = len(all_studio_items) > next_cursor or bool(posts_res.get("has_more"))

                if sync_items_penalty_data and raw_items:
                    try:
                        sync_items_penalty_data(raw_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                    except Exception as pen_err:
                        print(f"[CheckDupWeb] Lỗi sync_items_penalty_data: {pen_err}")
        except Exception as e_studio:
            print(f"[CheckDupWeb] Lỗi api_get_post_list: {e_studio}")

    # Trường hợp B1: Thử quét siêu tốc qua Creator API (chỉ mất ~1s, không tốn RAM)
    if not raw_items and sec_uid and effective_cookie:
        api_items = fetch_external_channel_videos_creator_api(
            sec_uid=sec_uid,
            cookie_str=effective_cookie,
            ua_str=matched_acc_ua,
            proxy_str=matched_acc_proxy or proxy_str,
            max_videos=max_videos + max_cursor,
            max_cursor=max_cursor
        )
        if api_items:
            offset = int(max_cursor) if max_cursor > 0 else 0
            raw_items = api_items[offset:offset + max_videos]
            next_cursor = offset + len(raw_items)
            total_vids = int(channel_info.get("videoCount") or 0)
            if total_vids > 0:
                has_more = next_cursor < total_vids
            else:
                has_more = len(api_items) > next_cursor or len(raw_items) >= max_videos
            print(f"[CheckDupWeb] Quét siêu tốc qua Creator API thành công: {len(raw_items)} video cho @{username}")

    # Trường hợp B2: Kênh ngoài hoặc chưa lấy được -> Quét dự phòng qua Playwright Worker siêu tốc
    if not raw_items and username:
        channel_url = f"https://www.tiktok.com/@{username}"
        pw_cookie = effective_cookie if is_self_account else ""
        pw_items = fetch_external_channel_videos_playwright(
            channel_url,
            cookie_str=pw_cookie,
            proxy_str=matched_acc_proxy or proxy_str,
            max_videos=max_videos + max_cursor,
            channel_info=channel_info,
        )
        if not pw_items and username:
            print(f"[CheckDupWeb] Quét lần 1 kênh @{username} trả về 0 video, đang thử lại lần 2...")
            time.sleep(0.3)
            pw_items = fetch_external_channel_videos_playwright(
                channel_url,
                cookie_str="",
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
                    channel_info["uniqueId"] = auth["uniqueId"]
                    check_user_info["username"] = f"@{auth['uniqueId']}"
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
    video_list = []
    seen_group_ids = {}
    seen_content_keys = {}
    tkm_cache = _load_tiktokmanager_cache()
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
        is_phone = device_plat in ("iphone", "android") or it.get("shoot_tab") or it.get("source") == "📱 App" or "App" in str(it.get("source", ""))
        device = "phone" if is_phone else "web"

        ch_reg = str(channel_info.get("region") or "US").upper()
        region = str(it.get("region") or it.get("locationCreated") or it.get("country_code") or it.get("geo") or cached_meta.get("region") or ch_reg).upper()

        group_id = str(it.get("group_id") or it.get("groupId") or cached_meta.get("group_id") or "")

        # Cờ kiểm duyệt chính sách (penaltyContext)
        penalty_ctx = it.get("penaltyContext") if isinstance(it.get("penaltyContext"), dict) else {}
        display_penalty_type = int(penalty_ctx.get("display_penalty_type") or 0)
        display_policy = int(penalty_ctx.get("display_policy") or 0)

        item_ctrl = it.get("item_control") if isinstance(it.get("item_control"), dict) else {}
        can_repost = item_ctrl.get("can_repost", True)
        is_prohibited = bool(it.get("isProhibited", False))

        status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
        is_priv = bool(status_dict.get("is_private") or it.get("is_private") or it.get("privateItem"))

        dur = it.get("duration") or (it.get("video", {}).get("duration") if isinstance(it.get("video"), dict) else 0) or 0
        try:
            dur_sec = int(dur) // 1000 if int(dur) > 1000 else int(dur)
            duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else ""
        except Exception:
            dur_sec = 0
            duration_str = ""

        digg_cnt = int(it.get("digg_count") or stats.get("diggCount") or stats.get("digg_count") or 0)
        comment_cnt = int(it.get("comment_count") or stats.get("commentCount") or stats.get("comment_count") or 0)
        share_cnt = int(it.get("share_count") or stats.get("shareCount") or stats.get("share_count") or 0)
        collect_cnt = int(it.get("collect_count") or stats.get("collectCount") or stats.get("collect_count") or 0)

        has_pen = bool(
            it.get("has_penalty", False)
            or (display_penalty_type != 0)
            or (display_policy != 0)
            or is_prohibited
        )

        is_dup = bool(
            (group_id and group_id != aweme_id)
            or cached_meta.get("is_dup", False)
            or it.get("is_dup", False)
            or it.get("isDup", False)
            or (display_penalty_type != 0)
            or (display_policy != 0)
        )

        dup_reason = ""
        dup_origin_vid = ""
        if group_id and group_id != aweme_id:
            dup_reason = f"Trùng lặp Group ID (Group: {group_id})"
        elif cached_meta.get("is_dup"):
            dup_reason = "Trùng lặp nội dung (Khớp cache TikTok Manager)"
        elif display_penalty_type != 0 or display_policy != 0:
            dup_reason = "Vi phạm chính sách trùng lặp / Bản quyền nội dung"

        if group_id:
            if group_id in seen_group_ids and seen_group_ids[group_id] != aweme_id:
                is_dup = True
                dup_origin_vid = seen_group_ids[group_id]
                dup_reason = f"Trùng Group ID với video {seen_group_ids[group_id]} trên kênh"
            seen_group_ids[group_id] = aweme_id

        desc_clean = clean_desc_text(desc).lower()
        if not is_dup and dur_sec > 0 and len(desc_clean) > 8 and desc_clean != "(không mô tả)":
            content_key = (dur_sec, desc_clean)
            if content_key in seen_content_keys:
                is_dup = True
                dup_origin_vid = seen_content_keys[content_key]
                dup_reason = f"Trùng thời lượng ({duration_str}) & mô tả với video {seen_content_keys[content_key]}"
            else:
                seen_content_keys[content_key] = aweme_id

        age_hours = (time.time() - c_time) / 3600 if c_time > 0 else 0

        is_fyp = bool(
            is_dup
            or (can_repost is False)
            or is_prohibited
            or has_pen
            or it.get("is_fyp_restricted", False)
            or cached_meta.get("is_fyp_restricted", False)
        )
        is_shadow = is_fyp

        shadow_reason = ""
        if can_repost is False:
            shadow_reason = "Hạn chế phân phối Bảng tin Cho bạn (Shadowban / Chặn chia sẻ lại)"
        elif is_prohibited:
            shadow_reason = "Nội dung vi phạm tiêu chuẩn cộng đồng TikTok (Bị cấm phân phối)"
        elif has_pen:
            shadow_reason = "Án phạt kiểm duyệt nội dung TikTok"
        elif is_dup:
            shadow_reason = dup_reason or "Nội dung trùng lặp (Không đủ điều kiện FYP)"

        if not is_fyp and ch_followers >= 500 and age_hours >= 12 and 0 < play_cnt <= 300 and share_cnt == 0 and digg_cnt <= 5:
            is_fyp = True
            is_shadow = True
            shadow_reason = f"Kẹt trần phân phối {play_cnt} view (Nghi vấn mất FYP / Shadowban)"

        pen_details_str = str(it.get("penalty_details") or "")
        if not pen_details_str:
            pen_details_str = dup_reason or shadow_reason or ""
        if not shadow_reason and is_shadow:
            shadow_reason = pen_details_str or "Video bị hạn chế xuất hiện trên FYP"

        cover_url = ""
        all_cover_urls = []
        if isinstance(it.get("cover_urls"), list):
            for u in it["cover_urls"]:
                if u and str(u).startswith("http") and u not in all_cover_urls:
                    all_cover_urls.append(str(u))
        if it.get("cover_url"):
            u_str = str(it["cover_url"]).strip()
            if u_str.startswith("http") and u_str not in all_cover_urls:
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
        if group_id and group_id != aweme_id:
            origin_url = f"https://www.tiktok.com/@tiktok/video/{group_id}"
        elif is_dup and dup_origin_vid:
            origin_url = f"https://www.tiktok.com/@{clean_u}/video/{dup_origin_vid}" if clean_u else f"https://www.tiktok.com/video/{dup_origin_vid}"

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
            "source": "📱 App" if is_phone else "🌐 Web",
            "region": region,
            "region_display": format_full_country_display(region),
            "shadow": is_shadow,
            "shadowReason": shadow_reason,
            "is_dup": is_dup,
            "is_fyp_restricted": is_fyp,
            "has_penalty": has_pen,
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

    if not video_list:
        ch_vids = int(channel_info.get("videoCount") or 0)
        if ch_vids > 0:
            return {
                "success": False,
                "channel": channel_info,
                "user_info": check_user_info,
                "error": f"Kênh @{username} có {ch_vids} video nhưng TikTok tạm thời giới hạn tải danh sách. Vui lòng bấm 'Kiểm tra ngay' lại sau vài giây!"
            }
        elif not channel_info.get("followers") and not sec_uid and not is_self_account:
            return {
                "success": False,
                "error": f"Không thể kết nối hoặc tìm thấy kênh TikTok @{username}. Vui lòng kiểm tra lại đường dẫn / ID!"
            }

    total_loaded = len(video_list)
    dup_count = sum(1 for v in video_list if v.get("is_dup"))
    fyp_pen_count = sum(1 for v in video_list if v.get("is_fyp_restricted") or v.get("shadow") or v.get("has_penalty"))
    bad_count = sum(1 for v in video_list if v.get("is_dup") or v.get("is_fyp_restricted") or v.get("shadow") or v.get("has_penalty"))
    clean_count = max(0, total_loaded - bad_count)

    stats_summary = {
        "total_videos": total_loaded,
        "duplicate_count": dup_count,
        "penalty_count": fyp_pen_count,
        "clean_count": clean_count,
        "dup_percentage": round((bad_count / total_loaded * 100), 1) if total_loaded > 0 else 0,
    }

    channel_info["followers_fmt"] = format_count_compact(channel_info.get("followers", 0))

    return {
        "success": True,
        "channel": channel_info,
        "secUid": sec_uid,
        "list": video_list,
        "stats_summary": stats_summary,
        "user_info": check_user_info,
        "hasMore": has_more,
        "maxCursor": next_cursor
    }
