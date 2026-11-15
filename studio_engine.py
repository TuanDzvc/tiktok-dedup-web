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
import re
import time
import json
import uuid
import base64
import datetime
import threading
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


def _pw_qr_worker(session_id: str):
    sess = QR_SESSIONS.get(session_id)
    if not sess:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()
            page.goto("https://www.tiktok.com/login", timeout=25000)
            page.wait_for_timeout(2000)

            # Bấm vào tùy chọn đăng nhập bằng mã QR
            try:
                page.locator("text=QR").first.click(timeout=5000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            el = page.locator("canvas, img[src*='qrcode'], div[data-e2e='qr-code']").first
            if el.is_visible():
                img_b = el.screenshot()
                b64_str = f"data:image/png;base64,{base64.b64encode(img_b).decode('ascii')}"
                sess["qr_image"] = b64_str
                sess["ready"] = True
            else:
                sess["ready"] = True
                sess["status"] = "error"
                sess["error"] = "Không thể tải mã QR từ TikTok."
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

    # Đợi tối đa 10s cho Playwright tải xong trang và trích xuất ảnh mã QR
    for _ in range(25):
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
    proxies = {}
    if proxy:
        px = proxy.strip()
        if not px.startswith(("http://", "https://", "socks5://")):
            px = f"http://{px}"
        proxies = {"http": px, "https": px}

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
                avatar = data.get("avatar_url") or data.get("avatar_thumb") or ""
                country = (data.get("store_country") or data.get("country") or data.get("region") or "US").upper()
                c_ts = int(data.get("create_time") or 0)
                c_date = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y") if c_ts > 0 else "-"

                return {
                    "valid": True,
                    "uid": user_id,
                    "username": f"@{u_name}".replace("@@", "@"),
                    "uniqueId": u_name,
                    "nickname": nick,
                    "avatar": avatar,
                    "country": country,
                    "create_date": c_date,
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


def get_studio_post_list(cookie_str: str, proxy: str = "", count: int = 50, max_cursor: int = 0) -> Dict[str, Any]:
    """
    Lấy danh sách video Creator Studio đầy đủ 100%:
    Video Công khai, Video Riêng tư, Video Bạn bè và trạng thái vi phạm.
    """
    if not cookie_str:
        return {"success": False, "error": "Chưa có Cookie đăng nhập.", "items": []}

    cdict = parse_cookie_dict(cookie_str)
    csrf_token = cdict.get("tt_csrf_token", "")
    cookie_header = cookie_to_header_str(cdict)

    url_studio = (
        "https://www.tiktok.com/tiktok/creator/manage/item_list/v1/?"
        f"locale=vi&aid=1988&priority_region=US&region=US&"
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
        "count": count,
        "cursor": int(max_cursor),
        "post_status": 0,  # 0 = Tất cả video
        "type": 0
    }
    proxies = {}
    if proxy:
        px = proxy.strip()
        if not px.startswith(("http://", "https://", "socks5://")):
            px = f"http://{px}"
        proxies = {"http": px, "https": px}

    try:
        r = requests.post(url_studio, headers=headers, json=payload, proxies=proxies if proxies else None, timeout=15)
        if r.status_code == 200:
            res_data = r.json()
            code = res_data.get("code", 0)
            status_code = res_data.get("status_code", 0)

            if code == 8 or status_code == 8 or "login expired" in str(res_data).lower():
                return {"success": False, "error": "Phiên đăng nhập đã hết hạn. Vui lòng nạp lại Cookie hoặc quét QR mới.", "items": []}

            data = res_data.get("data") or {}
            raw_items = data.get("item_list") or data.get("items") or res_data.get("item_list") or []
            has_more = bool(data.get("has_more") or res_data.get("has_more", False))
            next_cursor = int(data.get("cursor") or (max_cursor + len(raw_items)))

            parsed_list = []
            for it in raw_items:
                aid = str(it.get("id") or it.get("item_id") or it.get("aweme_id") or "")
                if not aid:
                    continue

                desc = it.get("desc") or it.get("title") or "(không có mô tả)"
                stats = it.get("stats") or it.get("statistics") or {}
                play_cnt = int(it.get("play_count") or stats.get("play_count") or stats.get("playCount") or 0)
                digg_cnt = int(it.get("digg_count") or it.get("like_count") or stats.get("digg_count") or stats.get("diggCount") or 0)
                cmt_cnt = int(it.get("comment_count") or stats.get("comment_count") or stats.get("commentCount") or 0)
                share_cnt = int(it.get("share_count") or stats.get("share_count") or stats.get("shareCount") or 0)

                # Thumbnail
                cover_url = ""
                video_obj = it.get("video") if isinstance(it.get("video"), dict) else {}
                if video_obj.get("cover") and isinstance(video_obj["cover"], dict):
                    ul = video_obj["cover"].get("url_list") or []
                    if ul:
                        cover_url = ul[0]
                if not cover_url:
                    cover_url = it.get("cover_url") or it.get("cover") or ""

                # Thời gian tạo
                c_ts = int(it.get("create_time") or it.get("post_time") or 0)
                date_str = datetime.datetime.fromtimestamp(c_ts).strftime("%d/%m/%Y %H:%M") if c_ts > 0 else "-"

                # Thời lượng
                dur_ms = int(it.get("duration") or video_obj.get("duration") or 0)
                dur_sec = dur_ms // 1000 if dur_ms > 1000 else dur_ms
                dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else ""

                # Quyền riêng tư (Visibility)
                status_dict = it.get("status") if isinstance(it.get("status"), dict) else {}
                vis_type = status_dict.get("video_visibility") or it.get("visibility") or 1
                is_priv = bool(status_dict.get("is_private") or it.get("is_private") or vis_type == 2)
                is_frd = bool(status_dict.get("is_friend") or it.get("is_friend") or vis_type in (3, 4, 5))

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
                penalty_ctx = it.get("penaltyContext") if isinstance(it.get("penaltyContext"), dict) else {}
                has_penalty = bool(
                    int(penalty_ctx.get("display_penalty_type") or 0) != 0
                    or int(penalty_ctx.get("display_policy") or 0) != 0
                    or bool(it.get("isProhibited"))
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
                    "visibility_code": visibility_code,
                    "visibility_label": visibility_label,
                    "visibility_badge": visibility_badge,
                    "is_dup": is_dup,
                    "group_id": group_id,
                    "origin_url": f"https://www.tiktok.com/@tiktok/video/{group_id}" if is_dup else "",
                    "has_penalty": has_penalty,
                    "share_url": f"https://www.tiktok.com/video/{aid}"
                })

            return {
                "success": True,
                "items": parsed_list,
                "has_more": has_more,
                "cursor": next_cursor,
                "total_count": len(parsed_list)
            }
        else:
            return {"success": False, "error": f"HTTP {r.status_code} từ TikTok Studio API.", "items": []}
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
    proxies = {}
    if proxy:
        px = proxy.strip()
        if not px.startswith(("http://", "https://", "socks5://")):
            px = f"http://{px}"
        proxies = {"http": px, "https": px}

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
    proxies = {}
    if proxy:
        px = proxy.strip()
        if not px.startswith(("http://", "https://", "socks5://")):
            px = f"http://{px}"
        proxies = {"http": px, "https": px}

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
