# -*- coding: utf-8 -*-
"""
Core TikTok Deduplication Engine - Web Edition
Hệ thống kiểm tra trùng lặp & hạn chế FYP / Shadowban video TikTok chuyên sâu
Tích hợp đồng bộ ConfigStore, TikTok Studio Cookie, Passport API & giải mã UID Snowflake.
"""

import os
import sys
import re
import json
import time
import random
import string
import datetime
from typing import Dict, Any, List, Optional, Union
from pathlib import Path
import requests

# Thêm path tới Tool gốc để import ConfigStore và api_get_post_list
TOOL_ROOT = Path(__file__).resolve().parent.parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
UPLOADER_DIR = TOOL_ROOT / "tiktok_uploader_desktop"
if str(UPLOADER_DIR) not in sys.path:
    sys.path.insert(0, str(UPLOADER_DIR))

try:
    from tiktok_uploader_desktop.main import ConfigStore, unprotect_secret
except ImportError:
    try:
        from main import ConfigStore, unprotect_secret
    except ImportError:
        ConfigStore = None
        unprotect_secret = None

try:
    from tiktok_info_checker_gui import api_get_post_list, sync_items_penalty_data
except ImportError:
    try:
        from tiktok_uploader_desktop.tiktok_info_checker_gui import api_get_post_list, sync_items_penalty_data
    except ImportError:
        api_get_post_list = None
        sync_items_penalty_data = None

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

COUNTRY_NAMES = {
    "DE": ("Đức", "🇩🇪"),
    "US": ("Mỹ", "🇺🇸"),
    "GB": ("Anh", "🇬🇧"),
    "UK": ("Anh", "🇬🇧"),
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
        return "Quốc tế 🌐"
    c = raw.upper().replace("_TTP", "")
    base_c = c.split("-")[0].strip() if "-" in c else c
    if base_c in COUNTRY_NAMES:
        name_vn, flag = COUNTRY_NAMES[base_c]
        return f"{name_vn} ({base_c}) {flag}"
    if base_c == "EU":
        return "Châu Âu (EU) 🇪🇺"
    if base_c == "US":
        return "Mỹ (US) 🇺🇸"
    return f"{c} 🌐"

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

def fetch_external_channel_videos_curl(
    channel_query: str,
    max_videos: int = 18,
    proxy_str: str = "",
    sec_uid: str = "",
    max_cursor: int = 0
) -> Dict[str, Any]:
    """Sử dụng curl_cffi giả lập Chrome TLS fingerprint để lấy thông tin profile và danh sách video."""
    query = channel_query.strip()
    username = query
    if "tiktok.com/@" in query:
        username = query.split("tiktok.com/@")[-1].split("?")[0].split("/")[0]
    elif query.startswith("@"):
        username = query[1:]

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

    proxies = {}
    if proxy_str:
        p_clean = proxy_str.strip()
        if not p_clean.startswith(("http://", "https://", "socks5://")):
            p_clean = f"http://{p_clean}"
        proxies = {"http": p_clean, "https": p_clean}

    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        curl_requests = None

    if not curl_requests:
        return {"success": False, "error": "Thư viện curl_cffi chưa được cài đặt."}

    s = curl_requests.Session(impersonate="chrome120", proxies=proxies if proxies else None)
    profile_url = f"https://www.tiktok.com/@{username}"

    if not sec_uid:
        try:
            r1 = s.get(profile_url, timeout=15)
            if r1.status_code == 200:
                html = r1.text
                m_rehyd = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>', html)
                if m_rehyd:
                    try:
                        d = json.loads(m_rehyd.group(1))
                        scope = d.get("__DEFAULT_SCOPE__", {})
                        ud = scope.get("webapp.user-detail", {})
                        st_code = ud.get("statusCode")
                        if st_code and int(st_code) != 0:
                            return {"success": False, "error": f"Kênh TikTok @{username} không tồn tại hoặc đã bị khoá / đổi tên (Mã lỗi {st_code})."}
                        ui = ud.get("userInfo", {})
                        u = ui.get("user", {})
                        if u.get("secret"):
                            return {"success": False, "error": f"Kênh TikTok @{username} đang để chế độ tài khoản riêng tư (Private)."}
                        st = ui.get("stats", {})
                        channel_info["nickname"] = u.get("nickname") or username
                        channel_info["uniqueId"] = u.get("uniqueId") or username
                        channel_info["avatar"] = u.get("avatarLarger") or u.get("avatarThumb") or ""
                        channel_info["followers"] = int(st.get("followerCount") or 0)
                        channel_info["videoCount"] = int(st.get("videoCount") or 0)
                        sec_uid = u.get("secUid", "")
                        
                        cts = int(u.get("createTime") or 0)
                        if cts > 0:
                            channel_info["createDate"] = datetime.datetime.fromtimestamp(cts).strftime("%d/%m/%Y")
                        else:
                            u_num = u.get("id") or u.get("uid") or ""
                            sf_date = decode_snowflake_timestamp(u_num)
                            if sf_date:
                                channel_info["createDate"] = sf_date

                        app_ctx = scope.get("webapp.app-context", {})
                        app_user = app_ctx.get("user", {})
                        c_ssr = (u.get("region") or u.get("country") or "").strip().upper()
                        if not c_ssr:
                            c_user = (app_user.get("storeRegion") or app_user.get("ageGateRegion") or "").strip().upper()
                            if c_user and c_user not in ("NONE", "AUTO", "EU") and not c_user.endswith("_TTP"):
                                c_ssr = c_user
                        if c_ssr and c_ssr not in ("NONE", "AUTO", "EU") and not c_ssr.endswith("_TTP"):
                            channel_info["region"] = c_ssr
                    except Exception:
                        pass
                if not sec_uid:
                    m_sec = re.search(r'"secUid":"([^"]+)"', html)
                    if m_sec:
                        sec_uid = m_sec.group(1)
        except Exception as e:
            return {"success": False, "error": f"Không thể kết nối đến TikTok: {e}"}

    if not sec_uid:
        return {"success": False, "error": f"Không tìm thấy kênh TikTok @{username} hoặc kênh đã bị khóa / riêng tư."}

    all_items = []
    cursor = int(max_cursor) if max_cursor else int(time.time() * 1000)
    has_more = True

    while len(all_items) < max_videos and has_more:
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
            "count": str(max_videos),
            "cursor": cursor,
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
            "region": channel_info.get("region", "US"),
            "screen_height": "1080",
            "screen_width": "1920",
            "secUid": sec_uid,
            "type": "1",
            "tz_name": "UTC",
            "verifyFp": vfp,
            "webcast_language": "en"
        }
        api_url = "https://www.tiktok.com/api/creator/item_list/"
        try:
            r2 = s.get(api_url, params=query_params, timeout=15)
            if r2.status_code == 200:
                data = r2.json()
                items = data.get("itemList") or data.get("items") or []
                if not items:
                    break
                for it in items:
                    all_items.append(it)
                    if len(all_items) >= max_videos:
                        break
                has_more = bool(data.get("hasMore", False))
                cursor = int(data.get("cursor") or (cursor + max_videos))
            else:
                break
        except Exception:
            break

    return {
        "success": True,
        "channel": channel_info,
        "items": all_items,
        "sec_uid": sec_uid,
        "has_more": has_more,
        "max_cursor": cursor
    }

def fetch_external_channel_videos_playwright(
    channel_url: str,
    cookie_str: str = "",
    proxy_str: str = "",
    max_videos: int = 18,
    channel_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Mở Playwright không đầu để bắt gói tin API /api/post/item_list/ từ TikTok."""
    captured_items = []
    captured_seen = set()

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            browser = p.chromium.launch(headless=True, args=launch_args)
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

            ctx = browser.new_context(**ctx_opts)
            page = ctx.new_page()

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
                page.goto(channel_url, timeout=20000)
            except Exception:
                pass

            # Chờ gói tin đầu tiên
            for _ in range(25):
                if captured_items:
                    break
                page.wait_for_timeout(200)

            # Cuộn thêm nếu cần quét nhiều hơn 24 video
            last_len = len(captured_items)
            idle_rounds = 0
            while len(captured_items) < max_videos and idle_rounds < 2:
                try:
                    page.mouse.wheel(0, 1800)
                    page.wait_for_timeout(400)
                    if len(captured_items) > last_len:
                        last_len = len(captured_items)
                        idle_rounds = 0
                    else:
                        idle_rounds += 1
                except Exception:
                    break

            if channel_info is not None:
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

            browser.close()
    except Exception:
        pass

    return captured_items

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
    - Hỗ trợ lấy dữ liệu Check user (Tên, Handle, Email, SĐT, Quốc gia, UID, Ngày tạo).
    - Hỗ trợ phân trang: mặc định quét 18 video, có thể quét thêm các đợt 18 tiếp theo.
    """
    query = channel_query.strip()
    username = query
    if "tiktok.com/@" in query:
        username = query.split("tiktok.com/@")[-1].split("?")[0].split("/")[0]
    elif query.startswith("@"):
        username = query[1:]

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

    # Đối tượng thông tin Check User bên phải
    check_user_info = {
        "screen_name": username,
        "username": f"@{username}".replace("@@", "@"),
        "email": "— chưa gắn email",
        "is_email_verified": False,
        "create_date": "—",
        "mobile": "chưa gắn",
        "country": "United States",
        "uid": "---",
        "last_nickname_change": "—",
        "is_self": False,
    }

    matched_acc_cookie = cookie_str
    matched_acc_proxy = proxy_str
    matched_acc_ua = ua_str or DEFAULT_UA
    is_self_account = bool(matched_acc_cookie)

    # 1. Tìm trong ConfigStore của tool xem có nick này không (để lấy Cookie Studio kiểm tra án phạt)
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

                    if p_data.get("email"):
                        check_user_info["email"] = p_data["email"]
                        check_user_info["is_email_verified"] = bool(p_data.get("is_email_verified"))

                    if p_data.get("mobile"):
                        check_user_info["mobile"] = p_data["mobile"]
                    elif p_data.get("phone_collected"):
                        check_user_info["mobile"] = "đã gắn"

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
        except Exception as e_pass:
            print(f"[CheckUser] Lỗi gọi Passport API: {e_pass}")

    session = requests.Session()
    if matched_acc_proxy:
        p_clean = matched_acc_proxy.strip()
        if not p_clean.startswith(("http://", "https://", "socks5://")):
            p_clean = f"http://{p_clean}"
        session.proxies.update({"http": p_clean, "https": p_clean})

    headers = {
        "User-Agent": matched_acc_ua,
        "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if effective_cookie:
        headers["Cookie"] = effective_cookie

    # 3. Quét thông tin profile công khai
    if username:
        try:
            profile_url = f"https://www.tiktok.com/@{username}"
            r = session.get(profile_url, headers=headers, timeout=12)
            if r.status_code == 200:
                html = r.text
                m_rehyd = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>', html)
                if m_rehyd:
                    try:
                        d = json.loads(m_rehyd.group(1))
                        scope = d.get("__DEFAULT_SCOPE__", {})
                        ud = scope.get("webapp.user-detail", {})
                        st_code = ud.get("statusCode", 0)
                        if st_code != 0:
                            return {"success": False, "error": f"Kênh TikTok @{username} không tồn tại hoặc đã bị khoá / đổi tên (Mã lỗi {st_code})."}
                        ui = ud.get("userInfo", {})
                        u = ui.get("user", {})
                        if u.get("secret"):
                            return {"success": False, "error": f"Kênh TikTok @{username} đang để chế độ tài khoản riêng tư (Private)."}
                        st = ui.get("stats", {})
                        if not channel_info.get("nickname") or channel_info.get("nickname") == username:
                            channel_info["nickname"] = u.get("nickname") or username
                            check_user_info["screen_name"] = channel_info["nickname"]
                        channel_info["uniqueId"] = u.get("uniqueId") or username
                        channel_info["avatar"] = u.get("avatarLarger") or u.get("avatarThumb") or ""
                        channel_info["followers"] = int(st.get("followerCount") or 0)
                        channel_info["videoCount"] = int(st.get("videoCount") or 0)
                        c_sec = u.get("secUid", "")
                        if c_sec:
                            sec_uid = c_sec

                        create_ts = int(u.get("createTime") or 0)
                        if create_ts > 0:
                            channel_info["createDate"] = datetime.datetime.fromtimestamp(create_ts).strftime("%d/%m/%Y")
                            check_user_info["create_date"] = channel_info["createDate"]
                        else:
                            u_num = u.get("id") or u.get("uid") or ""
                            if u_num and check_user_info["uid"] == "---":
                                check_user_info["uid"] = str(u_num)
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
                # Quét đồng bộ thông tin án phạt & hạn chế FYP chuẩn 100% như tool
                if sync_items_penalty_data:
                    try:
                        sync_items_penalty_data(all_studio_items, effective_cookie, matched_acc_ua, matched_acc_proxy, channel_info["region"])
                    except Exception as pen_err:
                        print(f"[CheckDupWeb] Lỗi sync_items_penalty_data: {pen_err}")

                # Phân trang: Lấy tối đa max_videos (mặc định 18 bài)
                if max_cursor == 0:
                    raw_items = all_studio_items[:max_videos]
                    has_more = len(all_studio_items) > max_videos or bool(posts_res.get("has_more"))
                    next_cursor = len(raw_items)
                else:
                    # Đã có cursor: lấy phần tiếp theo
                    offset = int(max_cursor)
                    raw_items = all_studio_items[offset:offset + max_videos]
                    has_more = len(all_studio_items) > (offset + max_videos) or bool(posts_res.get("has_more"))
                    next_cursor = offset + len(raw_items)
        except Exception as e_studio:
            print(f"[CheckDupWeb] Lỗi api_get_post_list: {e_studio}")

    # Trường hợp B: Kênh ngoài hoặc Studio chưa lấy được -> Thử curl_cffi
    if not raw_items and username:
        try:
            curl_res = fetch_external_channel_videos_curl(
                channel_query=username,
                max_videos=max_videos,
                proxy_str=matched_acc_proxy or proxy_str,
                sec_uid=sec_uid,
                max_cursor=max_cursor
            )
            if curl_res.get("success"):
                curl_items = curl_res.get("items") or []
                if curl_items:
                    raw_items = curl_items[:max_videos]
                    has_more = bool(curl_res.get("has_more", False)) or (len(curl_items) >= max_videos)
                    next_cursor = int(curl_res.get("max_cursor") or (max_cursor + len(raw_items)))
                c_data = curl_res.get("channel") or {}
                if c_data:
                    for k in ["nickname", "avatar", "followers", "videoCount", "createDate"]:
                        if c_data.get(k) and not channel_info.get(k):
                            channel_info[k] = c_data[k]
                    if c_data.get("region"):
                        channel_info["region"] = c_data["region"]
                    if not sec_uid and curl_res.get("sec_uid"):
                        sec_uid = curl_res["sec_uid"]
        except Exception as e_curl:
            print(f"[DedupWeb] Lỗi curl_cffi: {e_curl}")

    # Trường hợp C: Fallback Playwright
    if not raw_items and username:
        channel_url = f"https://www.tiktok.com/@{username}"
        pw_items = fetch_external_channel_videos_playwright(
            channel_url,
            cookie_str=effective_cookie,
            proxy_str=matched_acc_proxy or proxy_str,
            max_videos=max_videos + max_cursor,
            channel_info=channel_info,
        )
        if pw_items:
            offset = int(max_cursor)
            raw_items = pw_items[offset:offset + max_videos]
            has_more = len(pw_items) > (offset + max_videos)
            next_cursor = offset + len(raw_items)

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

    # Trích xuất Ngày tạo từ UID Snowflake
    if channel_info.get("createDate") in ("", "—") and raw_items:
        for it in raw_items:
            auth = it.get("author") if isinstance(it.get("author"), dict) else {}
            u_id = auth.get("id") or auth.get("uid") or it.get("author_user_id") or it.get("authorUserId") or ""
            if u_id:
                if check_user_info["uid"] == "---":
                    check_user_info["uid"] = str(u_id)
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

    for it in raw_items:
        aweme_id = str(it.get("id") or it.get("aweme_id") or it.get("item_id") or "")
        if not aweme_id:
            continue

        desc = it.get("desc") or it.get("title") or ""
        stats = it.get("stats") or it.get("statistics") or {}
        play_cnt = int(it.get("play_count") or stats.get("playCount") or stats.get("play_count") or 0)
        c_time = int(it.get("createTime") or it.get("create_time") or it.get("post_time") or 0)

        device_plat = str(it.get("device_platform") or "").lower()
        is_phone = device_plat in ("iphone", "android") or it.get("shoot_tab") or it.get("source") == "📱 App" or "App" in str(it.get("source", ""))
        device = "phone" if is_phone else "web"

        ch_reg = str(channel_info.get("region") or "US").upper()
        region = str(it.get("region") or it.get("locationCreated") or it.get("country_code") or it.get("geo") or ch_reg).upper()

        group_id = str(it.get("group_id") or it.get("groupId") or "")
        penalty_ctx = it.get("penaltyContext") or {}
        display_penalty_type = int(penalty_ctx.get("display_penalty_type") or 0)
        display_policy = int(penalty_ctx.get("display_policy") or 0)
        item_ctrl = it.get("item_control") or {}
        can_repost = item_ctrl.get("can_repost", True)
        is_prohibited = bool(it.get("isProhibited", False))

        status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
        is_priv = bool(status_dict.get("is_private") or it.get("is_private"))
        is_fyp = bool(it.get("is_fyp_restricted", False) or (can_repost is False))
        has_pen = bool(it.get("has_penalty", False) or (display_penalty_type != 0) or (display_policy != 0))
        is_shadow = bool(it.get("is_shadow") or it.get("shadowban") or it.get("shadow") or is_fyp or (can_repost is False))

        # Check trùng lặp: Bao gồm cả cờ Mất FYP (trùng lặp nội dung không nguyên bản)
        is_dup = bool(
            (group_id and group_id != aweme_id)
            or it.get("is_dup")
            or it.get("isDup")
            or (display_penalty_type != 0)
            or (display_policy != 0)
            or is_prohibited
            or is_fyp
            or has_pen
        )

        if group_id:
            if group_id in seen_group_ids:
                is_dup = True
            seen_group_ids[group_id] = aweme_id

        # Cover url
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

        # Duration
        dur = it.get("duration") or (it.get("video", {}).get("duration") if isinstance(it.get("video"), dict) else 0) or 0
        try:
            dur_sec = int(dur) // 1000 if int(dur) > 1000 else int(dur)
            duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else ""
        except Exception:
            duration_str = ""

        # Social metrics
        digg_cnt = int(it.get("digg_count") or stats.get("diggCount") or stats.get("digg_count") or 0)
        comment_cnt = int(it.get("comment_count") or stats.get("commentCount") or stats.get("comment_count") or 0)
        share_cnt = int(it.get("share_count") or stats.get("shareCount") or stats.get("share_count") or 0)
        collect_cnt = int(it.get("collect_count") or stats.get("collectCount") or stats.get("collect_count") or 0)

        clean_u = (username or "").lstrip("@").strip()
        share_url = f"https://www.tiktok.com/@{clean_u}/video/{aweme_id}" if clean_u else f"https://www.tiktok.com/video/{aweme_id}"

        status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
        is_priv = bool(status_dict.get("is_private") or it.get("is_private"))
        is_fyp = bool(it.get("is_fyp_restricted", False) or (can_repost is False))
        has_pen = bool(it.get("has_penalty", False) or (display_penalty_type != 0) or (display_policy != 0))
        is_shadow = bool(it.get("is_shadow") or it.get("shadowban") or it.get("shadow") or is_fyp or (can_repost is False))

        shadow_reason = str(it.get("shadow_reason") or it.get("shadowReason") or ("Video bị hạn chế xuất hiện trên FYP" if is_shadow else ""))
        pen_details_str = str(it.get("penalty_details") or "")
        if not pen_details_str:
            if is_dup:
                pen_details_str = "Nội dung không nguyên bản / Trùng lặp (Không đủ điều kiện FYP)"
            elif is_shadow:
                pen_details_str = "Hạn chế phân phối trên Bảng tin Cho bạn (Shadowban)"

        date_local = ""
        date_utc = ""
        if c_time > 0:
            try:
                date_utc = datetime.datetime.utcfromtimestamp(c_time).strftime("%d/%m/%Y %H:%M")
                date_local = datetime.datetime.fromtimestamp(c_time).strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass

        post_data = {
            "awemeId": aweme_id,
            "aweme_id": aweme_id,
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

    if not video_list and not channel_info.get("followers") and not sec_uid and not is_self_account:
        return {
            "success": False,
            "error": f"Không thể kết nối hoặc tìm thấy kênh TikTok @{username}. Vui lòng kiểm tra lại đường dẫn / ID!"
        }

    total_loaded = len(video_list)
    dup_count = sum(1 for v in video_list if v.get("is_dup"))
    fyp_pen_count = sum(1 for v in video_list if v.get("is_fyp_restricted") or v.get("shadow"))
    clean_count = total_loaded - dup_count

    stats_summary = {
        "total_videos": total_loaded,
        "duplicate_count": dup_count,
        "penalty_count": fyp_pen_count,
        "clean_count": max(0, clean_count),
        "dup_percentage": round((dup_count / total_loaded * 100), 1) if total_loaded > 0 else 0,
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
