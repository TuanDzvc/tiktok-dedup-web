"""Thread-safe persistent store & background batch runner for checktiktok.site Outlook OAuth2 tab (tuandzvcc only)."""
from __future__ import annotations

import concurrent.futures
import html
import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from outlook_web_engine import (
    CLIENT_ID,
    RECOVERY_DOMAIN,
    CancelledError,
    DisposableHTBrowserPool,
    MailAccountRow,
    _format_age,
    fetch_latest_mail_code_oauth2,
    fetch_recent_messages_oauth2,
    proxy_display_ip,
    resolve_ht_browser_path,
    run_outlook_oauth_in_ht_browser,
    verify_saved_refresh_token,
)

WEB_ROOT = Path(__file__).resolve().parent
STORE_PATH = WEB_ROOT / "outlook_oauth_data.json"
LEGACY_DESKTOP_DEFAULT = Path(r"E:\Tool\outlook_oauth_tool\data\folders\Default.json")


def _clean_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "").strip())
    return cleaned[:48] or "Default"


def _short_mail_status_from_error(msg: str) -> str:
    low = (msg or "").casefold()
    if "sai mật khẩu" in low or "password is incorrect" in low:
        return "Sai mật khẩu"
    if "khóa" in low or "locked" in low or "suspended" in low:
        return "Khóa SĐT"
    if "mail kp" in low or "khôi phục" in low:
        return "Khác Mail KP"
    if "timeout" in low or "hết thời gian" in low:
        return "Timeout"
    if "mạng" in low or "proxy" in low:
        return "Lỗi Proxy/Mạng"
    if "token" in low:
        return "Token lỗi"
    return "Lỗi"


def parse_bulk_mail_lines(raw_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip().rstrip("|").strip()
        if not line:
            continue
        sep = "|" if "|" in line else ("\t" if "\t" in line else ":")
        parts = [p.strip() for p in line.split(sep)]
        if len(parts) < 2:
            continue
        email_addr = parts[0]
        if "@" not in email_addr:
            continue
        password = parts[1]
        recovery = ""
        token = ""
        client_id = CLIENT_ID

        for extra in parts[2:]:
            if not extra:
                continue
            if (extra.startswith("M.") and len(extra) >= 20) or (
                len(extra) >= 60 and "@" not in extra and " " not in extra
            ):
                token = extra
            elif re.fullmatch(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", extra):
                client_id = extra
            elif "@" in extra:
                recovery = extra.strip()

        rows.append(
            {
                "row_id": uuid.uuid4().hex,
                "email": email_addr,
                "password": password,
                "recovery_email": recovery,
                "proxy": "",
                "oauth2_token": token,
                "client_id": client_id,
                "last_code": "",
                "last_code_info": "",
                "status": "OK" if token else "Chờ chạy",
                "checked": not bool(token),
                "active_running": False,
            }
        )
    return rows


class OutlookWebService:
    def __init__(self, store_path: Path = STORE_PATH) -> None:
        self.store_path = store_path
        self._persist_callback = None
        self._client_worker = None
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._load_initial_state()
        self._running = False
        self._done_count = 0
        self._total_count = 0
        self._ok_count = 0
        self._err_count = 0
        self._cancel_event = threading.Event()
        self._pool = DisposableHTBrowserPool()
        self._logs: list[dict[str, str]] = []
        self._log_seq = 0

        exe = resolve_ht_browser_path()
        if exe.is_file():
            self._add_log(f"Đã sẵn sàng nhân trình duyệt Fingerprint Chromium: {exe.name}", "summary")
        else:
            self._add_log("Chạy bằng worker trên máy khách; server không mở trình duyệt.", "summary")

    def _load_initial_state(self) -> dict[str, Any]:
        if self.store_path.is_file():
            try:
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("folders"), dict):
                    st = raw.setdefault("settings", {})
                    st.setdefault("threads", 3)
                    st.setdefault("timeout", 240)
                    st.setdefault("proxies_text", "")
                    if "show_browser_v2" not in st:
                        st["show_browser"] = True
                        st["show_browser_v2"] = True
                    raw.setdefault("current_folder", "Default")
                    if not raw["folders"]:
                        raw["folders"] = {"Default": []}
                    valid_statuses = {
                        "OK", "Chờ chạy", "Đã dừng", "Lỗi", "Sai mật khẩu",
                        "Khóa SĐT", "Khác Mail KP", "Timeout", "Lỗi Proxy/Mạng", "Token lỗi", "Token chết",
                    }
                    for rows in raw["folders"].values():
                        for r in rows:
                            r["active_running"] = False
                            cur_st = str(r.get("status") or "")
                            if cur_st not in valid_statuses:
                                r["status"] = "OK" if r.get("oauth2_token") else "Chờ chạy"
                    return raw
            except Exception:
                pass

        # Migrate existing Default.json from desktop tool if present so user sees their existing account right away!
        initial_rows: list[dict[str, Any]] = []
        if LEGACY_DESKTOP_DEFAULT.is_file():
            try:
                d_raw = json.loads(LEGACY_DESKTOP_DEFAULT.read_text(encoding="utf-8"))
                for item in d_raw.get("rows", []):
                    initial_rows.append(
                        {
                            "row_id": str(item.get("row_id") or uuid.uuid4().hex),
                            "email": str(item.get("email") or ""),
                            "password": str(item.get("password") or ""),
                            "recovery_email": str(item.get("recovery_email") or ""),
                            "proxy": str(item.get("proxy") or ""),
                            "oauth2_token": str(item.get("oauth2_token") or ""),
                            "client_id": str(item.get("client_id") or CLIENT_ID),
                            "last_code": str(item.get("last_code") or ""),
                            "last_code_info": str(item.get("last_code_info") or ""),
                            "status": str(item.get("status") or "Chờ chạy"),
                            "checked": bool(item.get("checked", True)),
                            "active_running": False,
                        }
                    )
            except Exception:
                pass

        state = {
            "current_folder": "Default",
            "settings": {
                "threads": 3,
                "timeout": 240,
                "proxies_text": "",
                "show_browser": True,
                "show_browser_v2": True,
            },
            "folders": {"Default": initial_rows},
        }
        self._save_unlocked(state)
        return state

    def _save_unlocked(self, state: dict[str, Any] | None = None) -> None:
        target = state if state is not None else self._state
        clean_copy = {
            "current_folder": target.get("current_folder", "Default"),
            "settings": dict(target.get("settings") or {}),
            "client_workers": dict(target.get('client_workers') or {}),
            "client_jobs": dict(target.get('client_jobs') or {}),
            "client_batch": dict(target.get('client_batch') or {}),
            "folders": {
                fn: [{k: v for k, v in r.items() if k != "active_running"} for r in rows]
                for fn, rows in (target.get("folders") or {}).items()
            },
        }
        if self._persist_callback:
            self._persist_callback(clean_copy)
        try:
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean_copy, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
        except Exception:
            pass

    def _add_log(self, text: str, kind: str = "info") -> None:
        with self._lock:
            self._log_seq += 1
            self._logs.append(
                {
                    "seq": str(self._log_seq),
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "text": text,
                    "kind": kind,
                }
            )
            if len(self._logs) > 300:
                del self._logs[:-300]

    @staticmethod
    def _parsed_proxies(proxies_text: str) -> list[str]:
        return [l.strip() for l in (proxies_text or "").splitlines() if l.strip()]

    def get_snapshot(self, folder: str = "") -> dict[str, Any]:
        with self._lock:
            if self._client_worker:
                self._client_worker.refresh_status()
            folders_map = self._state.setdefault("folders", {"Default": []})
            if not folders_map:
                folders_map["Default"] = []
            cur = folder.strip() if folder and folder.strip() in folders_map else self._state.get("current_folder", "Default")
            if cur not in folders_map:
                cur = next(iter(folders_map.keys()))
            self._state["current_folder"] = cur
            rows_out = []
            for r in folders_map.get(cur, []):
                rc = dict(r)
                rc["ip_display"] = proxy_display_ip(rc.get("proxy", ""))
                rows_out.append(rc)
            return {
                "success": True,
                "folders": sorted(folders_map.keys(), key=lambda x: (0 if x == "Default" else 1, x.lower())),
                "current_folder": cur,
                "settings": dict(self._state.get("settings") or {}),
                "rows": rows_out,
                "job": {
                    "running": self._running,
                    "done": self._done_count,
                    "total": self._total_count,
                    "ok_count": self._ok_count,
                    "err_count": self._err_count,
                },
                "logs": list(self._logs[-150:]),
                "workers": self._client_worker.public_workers() if self._client_worker else [],
                "execution_mode": "client_worker",
            }

    def manage_folder(self, action: str, name: str = "", new_name: str = "") -> dict[str, Any]:
        with self._lock:
            if self._running and action in {"rename", "delete"}:
                return {"success": False, "error": "Hãy dừng lượt chạy hiện tại trước khi sửa/xóa thư mục."}
            folders = self._state.setdefault("folders", {"Default": []})
            if action == "switch":
                target = _clean_folder_name(name)
                if target in folders:
                    self._state["current_folder"] = target
                    self._save_unlocked()
            elif action == "add":
                target = _clean_folder_name(name)
                if target not in folders:
                    folders[target] = []
                self._state["current_folder"] = target
                self._save_unlocked()
                self._add_log(f"Đã tạo và chuyển sang thư mục '{target}'.", "summary")
            elif action == "rename":
                old_f = _clean_folder_name(name or self._state.get("current_folder", "Default"))
                new_f = _clean_folder_name(new_name)
                if old_f not in folders:
                    return {"success": False, "error": "Không tìm thấy thư mục cần đổi tên."}
                if new_f != old_f and new_f in folders:
                    return {"success": False, "error": f"Thư mục '{new_f}' đã tồn tại."}
                if new_f != old_f:
                    folders[new_f] = folders.pop(old_f)
                    self._state["current_folder"] = new_f
                    self._save_unlocked()
                    self._add_log(f"Đã đổi tên thư mục '{old_f}' → '{new_f}'.", "summary")
            elif action == "delete":
                target = _clean_folder_name(name or self._state.get("current_folder", "Default"))
                folders.pop(target, None)
                if not folders:
                    folders["Default"] = []
                self._state["current_folder"] = next(iter(folders.keys()))
                self._save_unlocked()
                self._add_log(f"Đã xóa thư mục '{target}'.", "info")
            return self.get_snapshot()

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            st = self._state.setdefault("settings", {})
            if "threads" in payload:
                st["threads"] = max(1, min(12, int(payload.get("threads") or 3)))
            if "timeout" in payload:
                st["timeout"] = max(60, min(600, int(payload.get("timeout") or 240)))
            if "proxies_text" in payload:
                st["proxies_text"] = str(payload.get("proxies_text") or "")
            if "show_browser" in payload:
                st["show_browser"] = bool(payload.get("show_browser"))
            self._save_unlocked()
            return self.get_snapshot()

    def modify_rows(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        with self._lock:
            cur = self._state.get("current_folder", "Default")
            rows: list[dict[str, Any]] = self._state.setdefault("folders", {}).setdefault(cur, [])
            proxies = self._parsed_proxies(str((self._state.get("settings") or {}).get("proxies_text") or ""))

            if action == "import":
                parsed = parse_bulk_mail_lines(str(payload.get("raw_text") or ""))
                if not parsed:
                    return {"success": False, "error": "Chưa có dòng mail|pass hợp lệ nào để nạp."}
                by_email = {str(r.get("email") or "").casefold(): r for r in rows}
                added, updated = 0, 0
                for item in parsed:
                    key = item["email"].casefold()
                    if key in by_email:
                        existing = by_email[key]
                        existing["password"] = item["password"]
                        if item["recovery_email"]:
                            existing["recovery_email"] = item["recovery_email"]
                        if item["oauth2_token"]:
                            existing["oauth2_token"] = item["oauth2_token"]
                            existing["client_id"] = item["client_id"]
                            existing["status"] = "OK"
                            existing["checked"] = False
                        updated += 1
                    else:
                        rows.append(item)
                        by_email[key] = item
                        added += 1
                if proxies:
                    for idx, r in enumerate(rows):
                        if not r.get("proxy"):
                            r["proxy"] = proxies[idx % len(proxies)]
                self._save_unlocked()
                self._add_log(f"Đã nạp {added} mail mới (cập nhật {updated} mail trùng) vào '{cur}'.", "summary")

            elif action == "apply_proxies":
                if "proxies_text" in payload:
                    self._state.setdefault("settings", {})["proxies_text"] = str(payload.get("proxies_text") or "")
                    proxies = self._parsed_proxies(str(payload.get("proxies_text") or ""))
                if not proxies:
                    for r in rows:
                        r["proxy"] = ""
                    self._add_log("Đã xóa Proxy khỏi bảng (chạy mạng trực tiếp).", "info")
                else:
                    for idx, r in enumerate(rows):
                        r["proxy"] = proxies[idx % len(proxies)]
                    self._add_log(f"Đã gán xoay vòng {len(proxies)} Proxy cho {len(rows)} mail.", "summary")
                self._save_unlocked()

            elif action == "set_checked":
                ids = set(payload.get("row_ids") or [])
                checked_val = bool(payload.get("checked", True))
                for r in rows:
                    if not ids or r.get("row_id") in ids:
                        r["checked"] = checked_val
                self._save_unlocked()

            elif action == "delete":
                if self._running:
                    return {"success": False, "error": "Đang chạy batch, hãy dừng trước khi xóa dòng."}
                ids = set(payload.get("row_ids") or [])
                before = len(rows)
                if payload.get("all"):
                    rows.clear()
                else:
                    rows[:] = [r for r in rows if r.get("row_id") not in ids]
                removed = before - len(rows)
                self._save_unlocked()
                self._add_log(f"Đã xóa {removed} dòng khỏi thư mục '{cur}'.", "info")

            return self.get_snapshot()

    def start_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._client_worker:
            return {"success": False, "error": "Worker máy khách chưa sẵn sàng. Tải và mở file .bat trên máy muốn chạy."}
        return self._client_worker.start(payload)

    def stop_batch(self) -> dict[str, Any]:
        if self._client_worker:
            return self._client_worker.stop()
        self._cancel_event.set()
        self._add_log("Đang gửi lệnh dừng và đóng toàn bộ trình duyệt ngầm...", "err")
        threading.Thread(target=self._pool.cleanup_all, daemon=True).start()
        return self.get_snapshot()

    def _find_row_unlocked(self, folder_name: str, row_id: str) -> dict[str, Any] | None:
        for r in (self._state.get("folders") or {}).get(folder_name, []):
            if r.get("row_id") == row_id:
                return r
        return None

    def _run_batch_thread(
        self,
        folder_name: str,
        targets: list[dict[str, Any]],
        max_workers: int,
        timeout_sec: int,
        headless_offscreen: bool,
    ) -> None:
        slot_queue = list(range(max_workers))
        slot_lock = threading.Lock()

        def acquire_slot() -> int:
            with slot_lock:
                return slot_queue.pop(0) if slot_queue else 0

        def release_slot(s: int) -> None:
            with slot_lock:
                if s not in slot_queue:
                    slot_queue.append(s)
                    slot_queue.sort()

        def check_cancelled() -> None:
            if self._cancel_event.is_set():
                raise CancelledError("Đã dừng")

        def process_one(item: dict[str, Any]) -> None:
            if self._cancel_event.is_set():
                return
            rid = str(item["row_id"])
            slot = acquire_slot()
            t0 = time.monotonic()
            short_name = str(item["email"]).split("@", 1)[0]

            with self._lock:
                ref = self._find_row_unlocked(folder_name, rid)
                if ref is not None:
                    ref["active_running"] = True
                    ref["status"] = "Đang chạy"

            def on_stage(msg: str) -> None:
                elapsed = int(time.monotonic() - t0)
                clean = str(msg or "").strip()
                if clean:
                    clean = clean[0].upper() + clean[1:]
                self._add_log(f"• {short_name}: {clean} [{elapsed}s]", "info")

            acc_obj = MailAccountRow(
                row_id=rid,
                email=str(item.get("email") or ""),
                password=str(item.get("password") or ""),
                recovery_email=str(item.get("recovery_email") or ""),
                proxy=str(item.get("proxy") or ""),
                oauth2_token=str(item.get("oauth2_token") or ""),
                client_id=str(item.get("client_id") or CLIENT_ID),
            )

            try:
                res = run_outlook_oauth_in_ht_browser(
                    self._pool,
                    acc_obj,
                    slot_index=slot,
                    total_slots=max_workers,
                    timeout=timeout_sec,
                    headless_offscreen=headless_offscreen,
                    check_cancelled=check_cancelled,
                    on_stage=on_stage,
                )
                with self._lock:
                    self._done_count += 1
                    self._ok_count += 1
                    r_ref = self._find_row_unlocked(folder_name, rid)
                    if r_ref is not None:
                        r_ref["active_running"] = False
                        r_ref["oauth2_token"] = res["refresh_token"]
                        r_ref["client_id"] = res.get("client_id") or CLIENT_ID
                        if res.get("recovery_email"):
                            r_ref["recovery_email"] = res["recovery_email"]
                        r_ref["status"] = "OK"
                        r_ref["checked"] = False
                    self._save_unlocked()
                self._add_log(f"✓ {acc_obj.email}", "ok")
            except CancelledError:
                with self._lock:
                    r_ref = self._find_row_unlocked(folder_name, rid)
                    if r_ref is not None:
                        r_ref["active_running"] = False
                        r_ref["status"] = "Đã dừng"
                    self._save_unlocked()
            except Exception as exc:
                elapsed = int(time.monotonic() - t0)
                msg = str(exc).strip() or type(exc).__name__
                short_st = _short_mail_status_from_error(msg)
                with self._lock:
                    self._done_count += 1
                    self._err_count += 1
                    r_ref = self._find_row_unlocked(folder_name, rid)
                    if r_ref is not None:
                        r_ref["active_running"] = False
                        r_ref["status"] = short_st
                    self._save_unlocked()
                self._add_log(f"✗ {acc_obj.email} — {msg} [{elapsed}s]", "err")
            finally:
                release_slot(slot)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_one, it) for it in targets]
                for fut in futures:
                    try:
                        fut.result()
                    except Exception:
                        pass
        finally:
            self._pool.cleanup_all()
            with self._lock:
                for r in (self._state.get("folders") or {}).get(folder_name, []):
                    r["active_running"] = False
                self._running = False
                ok_final, err_final = self._ok_count, self._err_count
                was_stopped = self._cancel_event.is_set()
                self._save_unlocked()
            self._add_log(
                f"— {'Đã dừng' if was_stopped else 'Hoàn thành'} — OK: {ok_final} | Lỗi: {err_final}",
                "summary",
            )

    def fetch_codes(self, row_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            cur = self._state.get("current_folder", "Default")
            rows = (self._state.get("folders") or {}).get(cur, [])
            id_set = set(row_ids or [])
            targets = [dict(r) for r in rows if (not id_set or r.get("row_id") in id_set) and r.get("oauth2_token")]

        if not targets:
            return {"success": False, "error": "Chưa chọn dòng nào đã có OAuth2 Token để lấy Code."}

        results_map: dict[str, dict[str, Any]] = {}

        def _one(item: dict[str, Any]) -> None:
            rid = str(item["row_id"])
            try:
                res = fetch_latest_mail_code_oauth2(
                    str(item.get("oauth2_token") or ""),
                    client_id=str(item.get("client_id") or CLIENT_ID),
                    raw_proxy=str(item.get("proxy") or ""),
                )
                results_map[rid] = {"ok": True, **res}
            except Exception as exc:
                results_map[rid] = {"ok": False, "error": str(exc)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
            list(ex.map(_one, targets))

        single_code = ""
        with self._lock:
            cur_now = self._state.get("current_folder", "Default")
            for r in (self._state.get("folders") or {}).get(cur_now, []):
                rid = str(r.get("row_id") or "")
                if rid not in results_map:
                    continue
                outcome = results_map[rid]
                if outcome.get("ok"):
                    if outcome.get("rotated_token"):
                        r["oauth2_token"] = outcome["rotated_token"]
                    r["last_code_info"] = outcome.get("info", "")
                    code_val = outcome.get("code", "")
                    if code_val:
                        r["last_code"] = code_val
                        single_code = code_val
                        self._add_log(f"📩 {r['email']} → Code: [{code_val}] ({r['last_code_info']})", "code")
                    else:
                        self._add_log(f"• {r['email']}: {r['last_code_info']}", "info")
                else:
                    self._add_log(f"✗ {r['email']} đọc Code lỗi: {outcome.get('error')}", "err")
            self._save_unlocked()

        snap = self.get_snapshot()
        snap["single_code"] = single_code if len(targets) == 1 else ""
        snap["results"] = results_map
        return snap

    def verify_tokens(self, row_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            cur = self._state.get("current_folder", "Default")
            rows = (self._state.get("folders") or {}).get(cur, [])
            id_set = set(row_ids or [])
            targets = [dict(r) for r in rows if (not id_set or r.get("row_id") in id_set) and r.get("oauth2_token")]

        if not targets:
            return {"success": False, "error": "Không có dòng nào có OAuth2 Token để kiểm tra."}

        outcomes: dict[str, tuple[bool, str]] = {}

        def _chk(it: dict[str, Any]) -> None:
            ok, msg = verify_saved_refresh_token(
                str(it.get("oauth2_token") or ""),
                str(it.get("client_id") or CLIENT_ID),
                str(it.get("proxy") or ""),
            )
            outcomes[str(it["row_id"])] = (ok, msg)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
            list(ex.map(_chk, targets))

        ok_n, err_n = 0, 0
        with self._lock:
            cur_now = self._state.get("current_folder", "Default")
            for r in (self._state.get("folders") or {}).get(cur_now, []):
                rid = str(r.get("row_id") or "")
                if rid in outcomes:
                    ok, msg = outcomes[rid]
                    r["status"] = "OK" if ok else "Token chết"
                    self._add_log(f"{'✓' if ok else '✗'} {r.get('email')}: {msg}", "ok" if ok else "err")
                    if ok:
                        ok_n += 1
                    else:
                        err_n += 1
            self._save_unlocked()
        self._add_log(f"Kiểm tra OAuth2 Token hoàn tất — Sống: {ok_n} | Lỗi: {err_n}", "summary")
        return self.get_snapshot()

    def get_mailbox_messages(self, row_id: str) -> dict[str, Any]:
        with self._lock:
            cur = self._state.get("current_folder", "Default")
            row = self._find_row_unlocked(cur, row_id)
            if not row or not row.get("oauth2_token"):
                return {"success": False, "error": "Dòng này chưa có OAuth2 Token."}
            snap_row = dict(row)

        msgs, rotated = fetch_recent_messages_oauth2(
            str(snap_row.get("oauth2_token") or ""),
            str(snap_row.get("client_id") or CLIENT_ID),
            str(snap_row.get("proxy") or ""),
            top_n=15,
        )
        if rotated and rotated != snap_row.get("oauth2_token"):
            with self._lock:
                ref = self._find_row_unlocked(self._state.get("current_folder", "Default"), row_id)
                if ref is not None:
                    ref["oauth2_token"] = rotated
                    self._save_unlocked()

        formatted = []
        for m in msgs:
            sender_obj = ((m.get("from") or {}).get("emailAddress") or {})
            sender_name = str(sender_obj.get("name") or "")
            sender_addr = str(sender_obj.get("address") or "")
            raw_body = str((m.get("body") or {}).get("content") or m.get("bodyPreview") or "")
            cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_body)
            plain = html.unescape(re.sub(r"<[^>]+>", " ", cleaned))
            plain = re.sub(r"[ \t]+", " ", plain)
            plain = re.sub(r"\n\s*\n+", "\n\n", plain).strip()
            formatted.append(
                {
                    "folder": str(m.get("_folder") or "Inbox"),
                    "age": _format_age(float(m.get("_ts") or 0.0)),
                    "sender": f"{sender_name} <{sender_addr}>".strip(" <>"),
                    "subject": str(m.get("subject") or "(Không tiêu đề)"),
                    "received_at": str(m.get("receivedDateTime") or ""),
                    "body": plain,
                }
            )
        return {"success": True, "email": snap_row["email"], "messages": formatted}


OUTLOOK_WEB_SERVICE = OutlookWebService()
