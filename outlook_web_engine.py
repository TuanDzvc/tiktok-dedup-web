"""HT Browser + Microsoft OAuth2 (PKCE) + @fviainboxes.com engine extracted from TikFlow."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import random
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import psutil
import requests

# Exact OAuth2 constants used by TikFlow (outlook_oauth_enrollment.py)
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REDIRECT_URI = "https://localhost"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPES = "openid offline_access https://graph.microsoft.com/Mail.Read"
RECOVERY_DOMAIN = "fviainboxes.com"


class CancelledError(RuntimeError):
    """Raised when the user clicks Stop."""


@dataclass
class MailAccountRow:
    row_id: str
    email: str
    password: str
    recovery_email: str = ""
    proxy: str = ""
    oauth2_token: str = ""
    client_id: str = CLIENT_ID
    last_code: str = ""
    last_code_info: str = ""
    status: str = "Chờ chạy"
    checked: bool = True


def parse_proxy_string(raw_proxy: str) -> dict[str, Any]:
    """Parse IP:PORT, IP:PORT:USER:PASS, or scheme://user:pass@host:port."""
    raw = str(raw_proxy or "").strip()
    if not raw:
        return {}
    if "://" in raw:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname or not parsed.port:
            raise ValueError(f"Proxy không hợp lệ: {raw}")
        return {
            "type": "socks5" if scheme == "socks5h" else scheme,
            "host": parsed.hostname,
            "port": int(parsed.port),
            "user": parsed.username or "",
            "password": parsed.password or "",
        }

    # Support IP:PORT or IP:PORT:USER:PASS or USER:PASS@IP:PORT
    if "@" in raw:
        parsed = urlparse(f"http://{raw}")
        if parsed.hostname and parsed.port:
            return {
                "type": "http",
                "host": parsed.hostname,
                "port": int(parsed.port),
                "user": parsed.username or "",
                "password": parsed.password or "",
            }

    parts = raw.split(":")
    if len(parts) == 2:
        host, port_s = parts[0].strip(), parts[1].strip()
        return {"type": "http", "host": host, "port": int(port_s), "user": "", "password": ""}
    if len(parts) >= 4:
        host = parts[0].strip()
        port_s = parts[1].strip()
        user = parts[2].strip()
        password = ":".join(parts[3:]).strip()
        return {"type": "http", "host": host, "port": int(port_s), "user": user, "password": password}

    raise ValueError(f"Định dạng Proxy không hợp lệ: {raw}")


def proxy_display_ip(raw_proxy: str) -> str:
    try:
        cfg = parse_proxy_string(raw_proxy)
        if cfg.get("host") and cfg.get("port"):
            return f"{cfg['host']}:{cfg['port']}"
    except Exception:
        pass
    return str(raw_proxy or "").strip()


def requests_proxies_from_cfg(proxy_cfg: dict[str, Any]) -> dict[str, str]:
    if not proxy_cfg or not proxy_cfg.get("host") or not proxy_cfg.get("port"):
        return {}
    scheme = str(proxy_cfg.get("type") or "http").lower()
    if scheme == "socks5":
        scheme = "socks5h"
    host = proxy_cfg["host"]
    port = proxy_cfg["port"]
    user = proxy_cfg.get("user") or ""
    pw = proxy_cfg.get("password") or ""
    auth = f"{user}:{pw}@" if user and pw else ""
    url = f"{scheme}://{auth}{host}:{port}"
    return {"http": url, "https": url}


FP_BROWSER_VERSION = "148.0.7778.215"
FP_BROWSER_ARCHIVE = "ungoogled-chromium_148.0.7778.215-1.1_windows_x64.zip"
FP_BROWSER_URL = (
    f"https://github.com/adryfish/fingerprint-chromium/releases/download/{FP_BROWSER_VERSION}/{FP_BROWSER_ARCHIVE}"
)
FP_BROWSER_SHA256 = "9ef3f471b7a6641b4224532522b29141ce3746e27d55788d88e2fd951f362579"
_RUNTIME_INSTALL_LOCK = threading.Lock()


def _fp_runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
    return base / "TikFlowUploader" / "fingerprint-chromium"


def resolve_ht_browser_path() -> Path:
    """Return pinned stable Fingerprint Chromium 148.0.7778.215 chrome.exe path."""
    runtime_base = _fp_runtime_root() / "runtime"
    pinned_dir = runtime_base / FP_BROWSER_VERSION / "browser"
    if pinned_dir.is_dir():
        pinned_found = sorted(pinned_dir.rglob("chrome.exe"))
        if pinned_found:
            return pinned_found[0]
    return pinned_dir / "chrome.exe"


def ensure_fingerprint_chromium(on_progress: Callable[[str], None] | None = None) -> Path:
    """Ensure Fingerprint Chromium is present in %LOCALAPPDATA%, auto-downloading once if missing."""
    existing = resolve_ht_browser_path()
    if existing.is_file():
        return existing

    with _RUNTIME_INSTALL_LOCK:
        existing = resolve_ht_browser_path()
        if existing.is_file():
            return existing

        root = _fp_runtime_root()
        target_runtime = root / "runtime" / FP_BROWSER_VERSION
        browser_dir = target_runtime / "browser"
        downloads_dir = root / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        target_runtime.mkdir(parents=True, exist_ok=True)

        archive_path = downloads_dir / FP_BROWSER_ARCHIVE
        partial_path = archive_path.with_suffix(".zip.part")

        def _sha256_file(p: Path) -> str:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        need_download = True
        if archive_path.is_file():
            if _sha256_file(archive_path).casefold() == FP_BROWSER_SHA256.casefold():
                need_download = False
            else:
                archive_path.unlink(missing_ok=True)

        if need_download:
            if on_progress:
                on_progress("lần đầu mở: đang tải nhân Fingerprint Chromium (chỉ tải 1 lần duy nhất)...")
            partial_path.unlink(missing_ok=True)
            last_report = 0.0
            with requests.get(FP_BROWSER_URL, stream=True, timeout=(25, 180)) as resp:
                resp.raise_for_status()
                total_bytes = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with partial_path.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1024 * 512):
                        if not chunk:
                            continue
                        out.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if on_progress and now - last_report >= 1.5:
                            last_report = now
                            mb_done = downloaded / (1024 * 1024)
                            if total_bytes > 0:
                                pct = int(downloaded * 100 / total_bytes)
                                mb_tot = total_bytes / (1024 * 1024)
                                on_progress(f"đang tải Fingerprint Chromium: {pct}% ({mb_done:.0f}/{mb_tot:.0f} MB)...")
                            else:
                                on_progress(f"đang tải Fingerprint Chromium: {mb_done:.0f} MB...")

            if _sha256_file(partial_path).casefold() != FP_BROWSER_SHA256.casefold():
                partial_path.unlink(missing_ok=True)
                raise RuntimeError("Gói Fingerprint Chromium tải về bị lỗi SHA-256, hãy bấm thử lại.")
            os.replace(partial_path, archive_path)

        if on_progress:
            on_progress("đang giải nén nhân Fingerprint Chromium vào %LOCALAPPDATA%...")
        if browser_dir.exists():
            shutil.rmtree(browser_dir, ignore_errors=True)
        staging = Path(tempfile.mkdtemp(prefix="fp-browser-", dir=target_runtime))
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(staging)
            os.replace(staging, browser_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            archive_path.unlink(missing_ok=True)

        final_exe = resolve_ht_browser_path()
        if not final_exe.is_file():
            raise RuntimeError("Giải nén Fingerprint Chromium xong nhưng không thấy chrome.exe.")
        return final_exe


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalProxyAuthForwarder:
    """Silent local loopback proxy bridge (127.0.0.1:port -> upstream IP:PORT:USER:PASS).

    Eliminates 100% of Chromium proxy authentication popups for both HTTP and SOCKS5 proxies.
    """

    def __init__(self, proxy_cfg: dict[str, Any]) -> None:
        self.proxy_cfg = proxy_cfg
        self.scheme = str(proxy_cfg.get("type") or "http").lower()
        self.up_host = str(proxy_cfg["host"]).strip()
        self.up_port = int(proxy_cfg["port"])
        self.user = str(proxy_cfg.get("user") or "")
        self.password = str(proxy_cfg.get("password") or "")
        self._auth_b64 = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        self._closed = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(128)
        self.local_port = int(self._srv.getsockname()[1])
        self._accept_thread = threading.Thread(target=self._accept_loop, name=f"ProxyAuth-{self.local_port}", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._closed.set()
        try:
            self._srv.close()
        except Exception:
            pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                client_sock, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(32768)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _connect_socks5_upstream(self, target_host: str, target_port: int) -> socket.socket:
        up = socket.create_connection((self.up_host, self.up_port), timeout=15.0)
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.user:
            up.sendall(b"\x05\x02\x00\x02")
        else:
            up.sendall(b"\x05\x01\x00")
        resp = up.recv(2)
        if len(resp) < 2 or resp[0] != 5:
            up.close()
            raise OSError("Invalid SOCKS5 greeting")
        if resp[1] == 2:
            ub = self.user.encode("utf-8")
            pb = self.password.encode("utf-8")
            up.sendall(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
            aresp = up.recv(2)
            if len(aresp) < 2 or aresp[1] != 0:
                up.close()
                raise OSError("SOCKS5 auth rejected")
        elif resp[1] != 0:
            up.close()
            raise OSError("SOCKS5 unsupported auth method")

        hb = target_host.encode("idna")
        req = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", target_port)
        up.sendall(req)
        r_hdr = up.recv(4)
        if len(r_hdr) < 4 or r_hdr[1] != 0:
            up.close()
            raise OSError("SOCKS5 connect failed")
        atyp = r_hdr[3]
        if atyp == 1:
            up.recv(6)
        elif atyp == 3:
            ln = up.recv(1)[0]
            up.recv(ln + 2)
        elif atyp == 4:
            up.recv(18)
        return up

    def _handle_client(self, client_sock: socket.socket) -> None:
        up_sock: socket.socket | None = None
        try:
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client_sock.settimeout(15.0)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
            if b"\r\n\r\n" not in buf:
                return

            head_bytes, rest_body = buf.split(b"\r\n\r\n", 1)
            lines = head_bytes.decode("latin-1").split("\r\n")
            first_line = lines[0]
            parts = first_line.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]

            if self.scheme in {"socks5", "socks5h", "socks4"}:
                if method == "CONNECT":
                    h_part, _, p_part = target.rpartition(":")
                    t_host = h_part.strip("[]")
                    t_port = int(p_part or 443)
                    up_sock = self._connect_socks5_upstream(t_host, t_port)
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if rest_body:
                        up_sock.sendall(rest_body)
                else:
                    parsed_u = urlparse(target)
                    t_host = parsed_u.hostname or ""
                    t_port = parsed_u.port or (443 if parsed_u.scheme == "https" else 80)
                    up_sock = self._connect_socks5_upstream(t_host, t_port)
                    rel_path = (parsed_u.path or "/") + (f"?{parsed_u.query}" if parsed_u.query else "")
                    rebuilt = [f"{method} {rel_path} {parts[2] if len(parts) > 2 else 'HTTP/1.1'}"]
                    rebuilt.extend(l for l in lines[1:] if not l.lower().startswith("proxy-"))
                    up_sock.sendall(("\r\n".join(rebuilt) + "\r\n\r\n").encode("latin-1") + rest_body)
            else:
                up_sock = socket.create_connection((self.up_host, self.up_port), timeout=15.0)
                up_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                filtered = [lines[0]] + [l for l in lines[1:] if not l.lower().startswith("proxy-authorization:")]
                if self.user or self.password:
                    filtered.insert(1, f"Proxy-Authorization: Basic {self._auth_b64}")
                payload = ("\r\n".join(filtered) + "\r\n\r\n").encode("latin-1") + rest_body
                up_sock.sendall(payload)

            client_sock.settimeout(None)
            up_sock.settimeout(None)
            t_up = threading.Thread(target=self._pipe, args=(client_sock, up_sock), daemon=True)
            t_down = threading.Thread(target=self._pipe, args=(up_sock, client_sock), daemon=True)
            t_up.start()
            t_down.start()
            t_up.join()
            t_down.join()
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass
            if up_sock is not None:
                try:
                    up_sock.close()
                except Exception:
                    pass


class DisposableHTBrowserPool:
    """Manages isolated disposable Fingerprint Chromium profiles with built-in silent Proxy Auth."""

    def __init__(self, work_root: Path | None = None) -> None:
        base = work_root or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OutlookOAuthHTTool")
        self.work_root = base.resolve()
        self.profiles_dir = self.work_root / "temp_fp_profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: dict[str, tuple[subprocess.Popen[Any], int, Path, LocalProxyAuthForwarder | None]] = {}

    def browser_path(self) -> Path:
        return resolve_ht_browser_path()

    @staticmethod
    def _new_seed() -> int:
        return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF

    @staticmethod
    def _prepare_mail_preferences(profile_dir: Path, x: int, y: int, width: int, height: int) -> None:
        prefs_path = profile_dir / "Default" / "Preferences"
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {}
        if prefs_path.is_file():
            try:
                payload = json.loads(prefs_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        payload["credentials_enable_service"] = False
        prof = payload.setdefault("profile", {})
        prof["password_manager_enabled"] = False
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        prof["default_zoom_level"] = 0.0
        payload["session"] = {"restore_on_startup": 1}
        payload.setdefault("browser", {})["window_placement"] = {
            "bottom": max(200, y + height),
            "left": max(0, x),
            "maximized": False,
            "right": max(200, x + width),
            "top": max(0, y),
            "work_area_bottom": 1080,
            "work_area_left": 0,
            "work_area_right": 1920,
            "work_area_top": 0,
        }
        prefs_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _debugger_ready(port: int) -> bool:
        try:
            res = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
            return res.ok and bool(res.json().get("webSocketDebuggerUrl"))
        except Exception:
            return False

    @staticmethod
    def _slot_geometry(slot_index: int, total_slots: int) -> tuple[int, int, int, int]:
        cols = 1 if total_slots <= 1 else (2 if total_slots <= 2 else (3 if total_slots <= 6 else 4))
        rows = max(1, (min(total_slots, 8) + cols - 1) // cols)
        idx = slot_index % (cols * rows)
        col = idx % cols
        row = idx // cols
        screen_w, screen_h = 1920, 1030
        w = max(520, min(920, screen_w // cols))
        h = max(520, min(780, screen_h // rows))
        x = col * (screen_w // cols) + 10
        y = row * (screen_h // rows) + 10
        return x, y, w, h

    def launch_disposable(
        self,
        email_addr: str,
        raw_proxy: str = "",
        *,
        slot_index: int = 0,
        total_slots: int = 1,
        headless_offscreen: bool = False,
        on_stage: Callable[[str], None] | None = None,
    ) -> tuple[str, str, Path]:
        """Ensure Fingerprint Chromium runtime, spin up silent Proxy Auth bridge if needed, and start browser."""
        exe = ensure_fingerprint_chromium(on_progress=on_stage)

        proxy_cfg = parse_proxy_string(raw_proxy)
        profile_id = f"mail-{uuid.uuid4().hex[:12]}"
        profile_dir = self.profiles_dir / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        if headless_offscreen:
            x, y, w, h = 40, 40, 1280, 820
        else:
            x, y, w, h = self._slot_geometry(slot_index, max(1, total_slots))

        self._prepare_mail_preferences(profile_dir, x, y, w, h)
        port = _free_port()
        seed = self._new_seed()

        cmd = [
            str(exe),
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--fingerprint={seed}",
            "--fingerprint-platform=windows",
            "--fingerprint-brand=Chrome",
            "--disable-non-proxied-udp",
            f"--window-position={x},{y}",
            f"--window-size={w},{h}",
            "--disable-backgrounding-occluded-windows",
            "--disable-session-crashed-bubble",
            "--disable-ipc-flooding-protection",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
        ]
        if headless_offscreen:
            cmd.append("--headless=new")

        proxy_bridge: LocalProxyAuthForwarder | None = None
        if proxy_cfg.get("host") and proxy_cfg.get("port"):
            p_scheme = str(proxy_cfg.get("type") or "http").strip().lower()
            p_u = str(proxy_cfg.get("user") or "").strip()
            p_pw = str(proxy_cfg.get("password") or "").strip()
            if p_u or p_pw:
                proxy_bridge = LocalProxyAuthForwarder(proxy_cfg)
                cmd.append(f"--proxy-server=http://127.0.0.1:{proxy_bridge.local_port}")
            else:
                cmd.append(f"--proxy-server={p_scheme}://{proxy_cfg['host']}:{proxy_cfg['port']}")

        cmd.append("about:blank")

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        si = None

        proc = subprocess.Popen(
            cmd,
            cwd=str(exe.parent),
            creationflags=creation_flags,
            startupinfo=si,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with self._lock:
            self._active[profile_id] = (proc, port, profile_dir, proxy_bridge)

        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if self._debugger_ready(port):
                return profile_id, f"127.0.0.1:{port}", profile_dir
            if proc.poll() is not None:
                self.cleanup_profile(profile_id)
                raise RuntimeError(f"Fingerprint Chromium thoát sớm (mã {proc.returncode}).")
            time.sleep(0.2)

        self.cleanup_profile(profile_id)
        raise TimeoutError(f"Hết thời gian chờ cổng CDP {port} của Fingerprint Chromium.")

    def cleanup_profile(self, profile_id: str) -> None:
        with self._lock:
            entry = self._active.pop(profile_id, None)
        if entry is None:
            return
        proc, port, profile_dir, proxy_bridge = entry
        if proxy_bridge is not None:
            proxy_bridge.stop()
        try:
            version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.0).json()
            ws_url = str(version.get("webSocketDebuggerUrl") or "")
            parsed = urlparse(ws_url)
            if parsed.scheme == "ws" and parsed.hostname and parsed.port:
                key = base64.b64encode(os.urandom(16)).decode("ascii")
                req = (
                    f"GET {parsed.path or '/'} HTTP/1.1\r\n"
                    f"Host: {parsed.hostname}:{parsed.port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
                with socket.create_connection((parsed.hostname, parsed.port), timeout=1.5) as sock:
                    sock.sendall(req)
                    sock.recv(1024)
                    payload = b'{"id":1,"method":"Browser.close"}'
                    mask = os.urandom(4)
                    header = bytearray([0x81, 0x80 | len(payload)])
                    header.extend(mask)
                    sock.sendall(bytes(header) + bytes(val ^ mask[i % 4] for i, val in enumerate(payload)))
                time.sleep(0.25)
        except Exception:
            pass

        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        for _ in range(6):
            if not profile_dir.exists():
                break
            shutil.rmtree(profile_dir, ignore_errors=True)
            if profile_dir.exists():
                time.sleep(0.2)

    def cleanup_all(self) -> None:
        with self._lock:
            ids = list(self._active.keys())
        for pid in ids:
            self.cleanup_profile(pid)
        # Sweep stale temp profiles from previous crashed runs
        if self.profiles_dir.is_dir():
            for child in self.profiles_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)


class AuthorizationPKCE:
    """Exact Microsoft OAuth2 PKCE flow from TikFlow."""

    def __init__(self, mailbox: str) -> None:
        self.mailbox = mailbox.strip().lower()
        self.verifier = secrets.token_urlsafe(48)
        self.state = secrets.token_urlsafe(32)

    @property
    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        return "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?" + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "response_mode": "query",
                "scope": SCOPES,
                "login_hint": self.mailbox,
                "state": self.state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    def redeem(self, callback_url: str, raw_proxy: str = "") -> str:
        url = urlparse(callback_url)
        query = parse_qs(url.query)
        if url.scheme != "https" or url.hostname != "localhost" or url.path not in ("", "/"):
            raise RuntimeError("Địa chỉ trả kết quả OAuth2 không hợp lệ.")
        if not secrets.compare_digest(query.get("state", [""])[0], self.state):
            raise RuntimeError("OAuth2 state không khớp phiên đăng nhập.")
        if query.get("error") or not query.get("code"):
            err_desc = query.get("error_description", query.get("error", ["unknown"]))[0]
            raise RuntimeError(f"Microsoft từ chối cấp quyền: {err_desc[:120]}")

        proxy_cfg = parse_proxy_string(raw_proxy) if raw_proxy else {}
        proxies = requests_proxies_from_cfg(proxy_cfg)

        def _do_exchange(use_proxies: dict[str, str]) -> str:
            with requests.Session() as session:
                if use_proxies:
                    session.proxies.update(use_proxies)
                response = session.post(
                    TOKEN_URL,
                    data={
                        "client_id": CLIENT_ID,
                        "grant_type": "authorization_code",
                        "code": query["code"][0],
                        "redirect_uri": REDIRECT_URI,
                        "code_verifier": self.verifier,
                    },
                    timeout=25,
                )
                data = response.json()
                if not response.ok or not data.get("refresh_token") or not data.get("access_token"):
                    err = data.get("error_description") or data.get("error") or f"HTTP {response.status_code}"
                    raise RuntimeError(f"Không đổi được OAuth2 token: {str(err)[:120]}")

                # Verify actual Graph Mail.Read inbox access just like TikFlow
                result = session.get(
                    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                    headers={"Authorization": "Bearer " + data["access_token"]},
                    params={"$top": 1, "$select": "id"},
                    timeout=20,
                )
                if not result.ok or not isinstance(result.json().get("value"), list):
                    raise RuntimeError("Đã lấy token nhưng kiểm tra đọc Inbox Graph thất bại.")
                return str(data["refresh_token"])

        try:
            return _do_exchange(proxies)
        except requests.RequestException:
            if proxies:
                return _do_exchange({})
            raise


def _pick(page: Any, names: tuple[str, ...], role: str = "button") -> Any:
    from playwright.sync_api import Error as PlaywrightError

    patterns = [re.compile(unicodedata.normalize("NFC", name), re.I) for name in names]
    try:
        controls = page.get_by_role(role).element_handles()
        if role == "button":
            controls += page.get_by_role("link").element_handles()
    except PlaywrightError:
        return None
    for control in controls:
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            label = unicodedata.normalize(
                "NFC",
                (control.get_attribute("aria-label") or control.get_attribute("value") or control.inner_text() or "").strip(),
            )
        except PlaywrightError:
            continue
        if any(pattern.search(label) for pattern in patterns):
            return control
    return None


def _visible_code_or_text_inputs(page: Any) -> list[Any]:
    from playwright.sync_api import Error as PlaywrightError

    try:
        boxes = [box for box in page.get_by_role("textbox").all() if box.is_visible()]
        if boxes:
            return boxes
        fallback = page.locator(
            "input:visible:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio'])"
        ).all()
        return [b for b in fallback if b.is_visible()]
    except PlaywrightError:
        return []


def new_recovery_message(records: list[dict[str, Any]], baseline_ids: set[str], requested_at: float) -> dict[str, Any] | None:
    candidates = [
        record
        for record in records
        if str(record.get("id")) not in baseline_ids
        and float(record.get("createdAt", 0)) >= int(requested_at)
        and "account-security-noreply" in str(record.get("from", "")).casefold()
        and any(
            label in unicodedata.normalize("NFC", str(record.get("subject", ""))).casefold()
            for label in ("single-use code", "security code", "mã bảo mật", "verify your", "xác minh")
        )
    ]
    return max(candidates, key=lambda record: float(record.get("createdAt", 0)), default=None)


def extract_microsoft_code(payload: Any) -> str:
    text = unicodedata.normalize("NFC", html.unescape(re.sub(r"<[^>]+>", " ", str(payload))))
    match = re.search(
        r"(?:your\s+single[- ]use\s+code\s+is|security\s+code(?:\s+is)?|mã\s+bảo\s+mật(?:\s+của\s+bạn\s+là)?|verification\s+code(?:\s+is)?)"
        r"\s*:?\s*(\d{6})(?!\d)",
        text,
        re.I,
    )
    if match:
        return match.group(1)
    # Fallback standalone 6-digit code in Microsoft security email body
    fallback = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    return fallback[0] if len(fallback) == 1 else ""


def recovery_screen(body: str, page: Any = None) -> str:
    lower = unicodedata.normalize("NFC", body).casefold()
    if page is not None and _pick(page, ("^gửi mã$", "^send code$")):
        boxes = _visible_code_or_text_inputs(page)
        if len(boxes) == 1:
            label = " ".join(
                filter(
                    None,
                    (
                        boxes[0].get_attribute("type"),
                        boxes[0].get_attribute("aria-label"),
                        boxes[0].get_attribute("placeholder"),
                        boxes[0].get_attribute("name"),
                        boxes[0].get_attribute("id"),
                    ),
                )
            ).casefold()
            if "email" in label or "verify your email" in lower or "xác minh email của bạn" in lower:
                return "confirm_email"
    if any(
        text in lower
        for text in (
            "nhập mã",
            "enter code",
            "enter the code",
            "enter your code",
            "we sent a code",
            "đã gửi mã",
            "security code",
            "mã bảo mật",
        )
    ):
        return "code"
    if any(
        text in lower
        for text in (
            "chúng tôi nên gửi mã của bạn ở đâu",
            "where should we send your code",
            "where do you want us to send",
        )
    ):
        return "destination"
    return ""


def matches_recovery_destination(label: str, recovery: str) -> bool:
    text = unicodedata.normalize("NFC", label).casefold()
    text = re.sub("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", text)
    addresses = re.findall(r"[\w.+*•…-]+@[\w.-]+", text)
    wanted_local, wanted_domain = recovery.casefold().split("@", 1)
    for address in addresses:
        local, domain = address.split("@", 1)
        domain = domain.rstrip(".")
        if domain != wanted_domain or not any(char not in "*•…" for char in local):
            continue
        pattern = "".join(".*" if char in "*•…" else re.escape(char) for char in local)
        if re.fullmatch(pattern, wanted_local):
            return True
    return False


def optional_passkey_button(page: Any, body: str) -> Any:
    lower = unicodedata.normalize("NFC", body).casefold()
    url_l = page.url.lower()
    if (
        "fido/create" not in url_l
        and "passkey" not in url_l
        and not any(
            text in lower
            for text in (
                "create a passkey",
                "set up a passkey",
                "sign in faster",
                "break free from your passwords",
                "tạo khóa truy cập",
                "tạo mã truy cập",
                "tạo khoá truy cập",
                "thiết lập khóa truy cập",
                "đăng nhập nhanh hơn",
            )
        )
    ):
        return None
    return _pick(
        page,
        (
            "^hủy$",
            "^cancel$",
            "^skip for now$",
            "^not now$",
            "^no thanks$",
            "^bỏ qua$",
            "^để sau$",
            "^lúc khác$",
            "^không phải bây giờ$",
        ),
    )


def submit_recovery_code(page: Any, code: str, fill_fn: Callable[..., None], click_fn: Callable[..., None], submitted: set[Any], challenge_key: int) -> bool:
    key = ("recovery-code", challenge_key)
    if key in submitted or not re.fullmatch(r"\d{6}", code):
        return False
    boxes = _visible_code_or_text_inputs(page)
    if len(boxes) == 6:
        submitted.add(key)
        for box, digit in zip(boxes, code):
            fill_fn(box, digit, short=True)
        return True
    if len(boxes) == 1:
        next_button = _pick(page, ("^tiếp theo$", "^next$", "^verify$", "^xác minh$", "^submit$", "^gửi$"))
        if next_button is None:
            return False
        submitted.add(key)
        fill_fn(boxes[0], code)
        click_fn(next_button)
        return True
    return False


def _is_add_recovery_email_screen(page: Any, lower_body: str) -> bool:
    url_l = page.url.lower()
    if "proofs/add" in url_l or "proofs/manage" in url_l or "let's protect your account" in lower_body or "help us protect your account" in lower_body:
        # Ensure if there's a <select> proof type (Phone vs Email), we select Email
        try:
            selects = page.locator("select:visible").all()
            for sel in selects:
                opts = sel.locator("option").all()
                for opt in opts:
                    val = (opt.get_attribute("value") or "").lower()
                    txt = (opt.inner_text() or "").lower()
                    if "email" in val or "email" in txt:
                        sel.select_option(value=opt.get_attribute("value"))
                        break
        except Exception:
            pass
        return True
    return any(
        t in lower_body
        for t in (
            "thêm địa chỉ email",
            "thêm địa chỉ email",
            "add an email address",
            "bảo vệ tài khoản của bạn",
            "giúp chúng tôi bảo vệ tài khoản",
            "an alternate email",
            "email thay thế",
            "someone@example.com",
        )
    )


def run_outlook_oauth_in_ht_browser(
    pool: DisposableHTBrowserPool,
    account: MailAccountRow,
    *,
    slot_index: int = 0,
    total_slots: int = 1,
    timeout: int = 240,
    headless_offscreen: bool = True,
    check_cancelled: Callable[[], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Launch HT Browser, login Outlook, auto-add/verify @fviainboxes.com recovery email, and return OAuth2 refresh_token."""
    from playwright.sync_api import sync_playwright

    def _cancel_guard() -> None:
        if check_cancelled is not None:
            check_cancelled()

    def _pause(page_obj: Any, minimum: float = 0.30, maximum: float = 0.75) -> None:
        remaining_ms = random.uniform(minimum, maximum) * 1000
        while remaining_ms > 0:
            _cancel_guard()
            step = min(100.0, remaining_ms)
            page_obj.wait_for_timeout(step)
            remaining_ms -= step
        _cancel_guard()

    announced: set[str] = set()

    def stage(msg: str) -> None:
        if on_stage and msg not in announced:
            announced.add(msg)
            on_stage(msg)

    email_clean = account.email.strip()
    preset_recovery = account.recovery_email.strip().lower()
    has_preset_kp = bool(preset_recovery and "@" in preset_recovery)
    default_fvia = f"{email_clean.split('@', 1)[0].lower()}@{RECOVERY_DOMAIN}"
    recovery = preset_recovery if has_preset_kp else default_fvia
    recovery_local_part, recovery_domain = recovery.split("@", 1)
    is_fvia_recovery = recovery_domain.lower() == RECOVERY_DOMAIN
    confirmed_recovery = preset_recovery

    _cancel_guard()
    stage("đang mở Fingerprint Chromium...")
    profile_id, cdp_address, _ = pool.launch_disposable(
        email_clean,
        account.proxy,
        slot_index=slot_index,
        total_slots=total_slots,
        headless_offscreen=headless_offscreen,
        on_stage=stage,
    )

    transaction = AuthorizationPKCE(email_clean)
    deadline = time.monotonic() + timeout
    password_attempted = False
    code_mode = False
    recovery_used = False
    last_recovery_refresh = 0.0
    recovery_requested_at = 0.0
    recovery_baseline_ids: set[str] = set()
    submitted: set[Any] = set()
    recovery_challenge = 0

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://" + cdp_address, timeout=12000)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            def click(control: Any) -> None:
                control.click(timeout=5000)
                _pause(page)

            def fill(control: Any, value: str, password: bool = False, short: bool = False) -> None:
                pause_range = (0.07, 0.15) if short else (0.30, 0.70)
                control.click(timeout=5000)
                control.fill(value, timeout=5000)
                _pause(page, *pause_range)

            callback: list[str] = []

            def capture(route: Any) -> None:
                callback.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<html><body style='font-family:sans-serif;background:#0f172a;color:#22c55e;padding:32px'><h3>✓ Đã nhận mã OAuth2 Outlook thành công.</h3></body></html>",
                )

            page.route("https://localhost/**", capture)
            temp_page: Any = None
            recovery_records: list[dict[str, Any]] = []

            def refresh_recovery_mailbox() -> None:
                nonlocal recovery_records
                try:
                    with temp_page.expect_response(lambda resp: "/messages?" in resp.url, timeout=10000) as resp_info:
                        btn = temp_page.get_by_role("button", name="Get Email", exact=True)
                        if btn.count() == 0:
                            btn = temp_page.locator("button:has-text('Get Email'), button:has-text('Refresh')").first
                        btn.click(timeout=5000)
                    payload = resp_info.value.json()
                    recovery_records = payload.get("result", []) if isinstance(payload, dict) else []
                    _pause(temp_page, 0.25, 0.5)
                except Exception:
                    pass

            def open_recovery_mailbox() -> None:
                nonlocal temp_page, recovery_used, recovery_requested_at, recovery_baseline_ids
                if not is_fvia_recovery:
                    recovery_used = True
                    return
                if temp_page is None:
                    stage(f"mở fviainboxes ({recovery})...")
                    temp_page = context.new_page()
                    temp_page.goto("https://fviainboxes.com/", wait_until="domcontentloaded", timeout=25000)
                    _pause(temp_page, 0.5, 1.0)
                    email_in = temp_page.get_by_role("textbox", name="Email", exact=True)
                    if email_in.count() == 0:
                        email_in = temp_page.locator("input[placeholder*='Email' i], input[type='text']:visible").first
                    email_in.fill(recovery_local_part, timeout=6000)
                    _pause(temp_page, 0.25, 0.5)
                    refresh_recovery_mailbox()
                    recovery_baseline_ids = {str(item.get("id")) for item in recovery_records}
                    recovery_requested_at = time.time()
                    page.bring_to_front()
                recovery_used = True

            def prepare_recovery_request() -> None:
                nonlocal recovery_requested_at, recovery_baseline_ids, recovery_challenge
                open_recovery_mailbox()
                if temp_page is not None:
                    refresh_recovery_mailbox()
                recovery_baseline_ids = {str(item.get("id")) for item in recovery_records}
                recovery_requested_at = time.time()
                recovery_challenge += 1
                page.bring_to_front()

            def collect_callback() -> None:
                urls = [page.url]
                try:
                    urls.extend(
                        item.get("url", "")
                        for item in requests.get(f"http://{cdp_address}/json", timeout=2).json()
                        if item.get("type") == "page"
                    )
                except Exception:
                    pass
                for value in urls:
                    returned = urlparse(value)
                    params = parse_qs(returned.query)
                    if (
                        returned.scheme == "https"
                        and returned.hostname == "localhost"
                        and params.get("state", [""])[0] == transaction.state
                        and params.get("code")
                    ):
                        callback.append(value)
                        return

            try:
                stage("mở trang đăng nhập Microsoft...")
                try:
                    page.goto(transaction.url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    for _ in range(12):
                        _cancel_guard()
                        collect_callback()
                        if callback:
                            break
                        page.wait_for_timeout(200)
                    if not callback:
                        raise RuntimeError("Không mở được trang cấp quyền Microsoft (kiểm tra mạng/proxy).") from None

                if not callback:
                    _pause(page, 0.4, 0.8)

                ensured_proof_page = False
                saved_token_before_proof = ""

                while time.monotonic() < deadline:
                    _cancel_guard()
                    if not callback:
                        collect_callback()
                    if callback:
                        stage("đã có code, đang đổi lấy token...")
                        token = transaction.redeem(callback[-1], account.proxy)
                        if not has_preset_kp and not recovery_used and not ensured_proof_page:
                            # Only when NO mail_kp was provided in input AND Microsoft didn't prompt during login:
                            # visit proofs/Add once to check/auto-add @fviainboxes.com!
                            ensured_proof_page = True
                            saved_token_before_proof = token
                            callback.clear()
                            stage(f"kiểm tra / tự thêm mail KP ({recovery})...")
                            try:
                                page.goto("https://account.live.com/proofs/Add?mkt=en-US", wait_until="domcontentloaded", timeout=20000)
                                _pause(page, 0.5, 0.9)
                                p_body = page.locator("body").inner_text(timeout=4000)
                                if matches_recovery_destination(p_body, recovery):
                                    # Account already has <user>@fviainboxes.com attached!
                                    return {
                                        "refresh_token": saved_token_before_proof,
                                        "client_id": CLIENT_ID,
                                        "recovery_email": recovery,
                                    }
                                masked_found = [
                                    m.rstrip(".")
                                    for m in re.findall(r"[\w.+*•…-]+@[\w.-]+", p_body)
                                    if any(c in m for c in "*•…") and m.casefold() != email_clean.casefold()
                                ]
                                if masked_found or _pick(page, ("gửi mã đến", "send a code to")):
                                    # Account already has another recovery email; keep the valid OAuth2 token!
                                    return {
                                        "refresh_token": saved_token_before_proof,
                                        "client_id": CLIENT_ID,
                                        "recovery_email": masked_found[0] if masked_found else confirmed_recovery,
                                    }
                                # Otherwise account truly has NO recovery email -> let loop add @fviainboxes.com!
                                continue
                            except Exception:
                                return {
                                    "refresh_token": saved_token_before_proof,
                                    "client_id": CLIENT_ID,
                                    "recovery_email": confirmed_recovery,
                                }
                        return {
                            "refresh_token": token,
                            "client_id": CLIENT_ID,
                            "recovery_email": recovery,
                        }

                    host = (urlparse(page.url).hostname or "").lower()
                    if host == "localhost":
                        collect_callback()
                        if callback:
                            continue
                    if host and not (
                        host == "localhost"
                        or host.endswith(".live.com")
                        or host.endswith(".microsoftonline.com")
                        or host.endswith(".microsoft.com")
                        or host in {"live.com", "microsoft.com"}
                    ):
                        raise RuntimeError(f"Chuyển hướng ngoài luồng Microsoft ({host}).")

                    try:
                        body = page.locator("body").inner_text(timeout=4000)
                    except Exception:
                        page.wait_for_timeout(300)
                        continue

                    lower = unicodedata.normalize("NFC", body).casefold()
                    screen = recovery_screen(body, page)

                    # Account locked / suspended detection
                    if any(
                        t in lower
                        for t in (
                            "your account has been locked",
                            "tài khoản của bạn đã bị khóa",
                            "account has been suspended",
                            "unlock your account",
                            "mở khóa tài khoản",
                        )
                    ) and any(t in lower for t in ("phone number", "số điện thoại", "violates", "vi phạm")):
                        raise RuntimeError("Tài khoản bị khóa bắt xác minh SĐT (Locked).")

                    if any(
                        t in lower
                        for t in (
                            "mã đó không chính xác",
                            "mã đó không có tác dụng",
                            "that code is incorrect",
                            "code has expired",
                            "that code didn't work",
                        )
                    ):
                        raise RuntimeError("Microsoft từ chối mã khôi phục.")

                    if any(
                        t in lower
                        for t in (
                            "mật khẩu không đúng",
                            "mật khẩu không chính xác",
                            "mật khẩu đó không đúng",
                            "your account or password is incorrect",
                            "password is incorrect",
                            "that password is incorrect",
                        )
                    ):
                        send_alt = _pick(page, ("gửi mã đến", "send a code to"))
                        if not send_alt:
                            raise RuntimeError("Sai mật khẩu Outlook/Hotmail.")

                    password_unavailable = any(
                        t in lower
                        for t in (
                            "không thể đăng nhập bằng mật khẩu",
                            "can't sign in with a password",
                            "mật khẩu không đúng",
                            "mật khẩu không chính xác",
                            "mật khẩu đó không đúng",
                            "password is incorrect",
                            "that password is incorrect",
                        )
                    )

                    # Password first: never request or confirm recovery mail
                    # before submitting this account's password.
                    if not password_attempted:
                        password_option = _pick(page, ("use your password", "use password instead", "sign in with a password", "sử dụng mật khẩu", "nhập mật khẩu"))
                        password_box_first = page.locator("input[type=password]:visible").first
                        password_next = _pick(page, ("^next$", "^sign in$", "^continue$", "^tiếp theo$", "^đăng nhập$"))
                        if password_box_first.count() and password_box_first.is_visible() and password_next:
                            stage("nhập mật khẩu trước khi xác minh mail KP...")
                            fill(password_box_first, account.password, password=True)
                            password_attempted = True
                            click(password_next)
                            continue
                        if password_option:
                            stage("chọn đăng nhập bằng mật khẩu...")
                            click(password_option)
                            continue
                        if screen in {"code", "destination", "confirm_email"} or _pick(page, ("send a code to", "^send code$", "gửi mã đến", "^gửi mã$")):
                            alternative = _pick(page, ("other ways to sign in", "sign-in options", "sign in another way", "didn.t get a code"))
                            stage("chờ chọn đăng nhập bằng mật khẩu; chưa xin mã mail KP...")
                            if alternative:
                                click(alternative)
                            else:
                                page.wait_for_timeout(700)
                            continue
                    elif page.locator("input[type=password]:visible").count():
                        page.wait_for_timeout(500)
                        continue

                    send = _pick(page, ("gửi mã đến", "send a code to"))
                    password_choice = _pick(page, ("use your password", "sử dụng mật khẩu", "nhập mật khẩu"))

                    if send and (password_attempted or password_unavailable or code_mode or not password_choice):
                        label = (
                            send.get_attribute("aria-label")
                            or send.get_attribute("value")
                            or send.inner_text()
                            or ""
                        ).strip()
                        if not matches_recovery_destination(label, recovery):
                            raise RuntimeError(f"Mail KP trên tài khoản ({label}) khác @fviainboxes.com.")
                        key = ("existing-recovery-request", page.url)
                        if key not in submitted:
                            stage(f"xác minh mail KP có sẵn ({recovery})...")
                            prepare_recovery_request()
                            submitted.add(key)
                            code_mode = True
                            confirmed_recovery = recovery
                            click(send)
                        else:
                            page.wait_for_timeout(500)
                        continue

                    if any(t in lower for t in ("captcha", "prove you", "robot", "unusual activity", "hoạt động bất thường")):
                        stage("chờ giải xác minh/Captcha trên trình duyệt...")
                        page.wait_for_timeout(700)
                        continue

                    if "consent/" in page.url.lower() or any(
                        t in lower for t in ("let this app access your info", "cho phép ứng dụng này truy cập")
                    ):
                        if any(t in lower for t in ("gửi email thay mặt", "send mail as you", "xóa email", "delete mail")):
                            raise RuntimeError("Microsoft yêu cầu quyền vượt quá Mail.Read.")
                        accept = _pick(page, ("chấp nhận", "chấp nhận", "^accept$", "^yes$", "^có$"))
                        if accept:
                            stage("bấm Chấp nhận cấp quyền OAuth2...")
                            click(accept)
                            continue

                    cancel_passkey = optional_passkey_button(page, body)
                    if cancel_passkey:
                        stage("bỏ qua lời mời tạo Passkey...")
                        click(cancel_passkey)
                        continue

                    if "privacynotice." in host:
                        ok = _pick(page, ("^OK$", "^continue$", "^tiếp tục$"))
                        if ok:
                            click(ok)
                            continue

                    no = _pick(page, ("^không$", "^no$"))
                    if no and any(t in lower for t in ("duy trì", "duy trì", "stay signed")):
                        stage("bỏ qua Duy trì đăng nhập...")
                        click(no)
                        continue

                    # Look good / Done after adding security proof
                    done_btn = _pick(page, ("^done$", "^hoàn tất$", "^xong$", "looks good", "trông ổn", "có vẻ ổn"))
                    if done_btn and ("proofs" in page.url.lower() or "security info" in lower or "thông tin bảo mật" in lower):
                        click(done_btn)
                        if ensured_proof_page and saved_token_before_proof:
                            # Proof added after initial OAuth! Re-authorize cleanly so token stays fresh.
                            transaction = AuthorizationPKCE(email_clean)
                            callback.clear()
                            page.goto(transaction.url, wait_until="domcontentloaded", timeout=25000)
                        continue

                    if ensured_proof_page and saved_token_before_proof and recovery_used and (
                        "account.microsoft.com" in host or "/proofs/manage" in page.url.lower()
                    ) and screen != "code" and not _is_add_recovery_email_screen(page, lower):
                        # Security proof addition finished & landed on account dashboard; refresh OAuth2 token!
                        transaction = AuthorizationPKCE(email_clean)
                        callback.clear()
                        page.goto(transaction.url, wait_until="domcontentloaded", timeout=25000)
                        continue

                    password_option = _pick(page, ("use your password", "sử dụng mật khẩu", "nhập mật khẩu"))
                    if password_option and not recovery_used and not code_mode and not password_attempted:
                        click(password_option)
                        continue

                    add_email_btn = _pick(page, ("^thêm email$", "^add email$", "add a new way to sign in or verify"))
                    if add_email_btn:
                        stage("chọn Thêm email khôi phục...")
                        click(add_email_btn)
                        continue

                    password_box = page.locator("input[type=password]:visible").first
                    visible_boxes = _visible_code_or_text_inputs(page)
                    email_box = visible_boxes[0] if visible_boxes else None
                    next_button = _pick(page, ("^tiếp theo$", "^next$", "^đăng nhập$", "^sign in$"))

                    if screen == "destination" and next_button:
                        key = ("proof-destination", page.url)
                        if key not in submitted:
                            if not matches_recovery_destination(body, recovery) and recovery.casefold() not in lower:
                                raise RuntimeError("Microsoft yêu cầu email khôi phục khác @fviainboxes.com.")
                            stage(f"yêu cầu gửi mã tới {recovery}...")
                            prepare_recovery_request()
                            confirmed_recovery = recovery
                            submitted.add(key)
                            click(next_button)
                        else:
                            page.wait_for_timeout(500)
                        continue

                    confirm_recovery = _pick(page, ("^gửi mã$", "^send code$"))
                    confirm_key = ("confirm-recovery", page.url)
                    if (
                        screen != "code"
                        and (recovery_used or code_mode or screen == "confirm_email")
                        and email_box is not None
                        and len(visible_boxes) == 1
                        and confirm_recovery
                        and confirm_key not in submitted
                    ):
                        if screen == "confirm_email" and not matches_recovery_destination(body, recovery):
                            raise RuntimeError("Email khôi phục cần xác minh không khớp @fviainboxes.com.")
                        stage(f"nhập xác nhận mail KP {recovery} & gửi mã...")
                        prepare_recovery_request()
                        fill(email_box, recovery)
                        confirmed_recovery = recovery
                        submitted.add(confirm_key)
                        click(confirm_recovery)
                        continue

                    if screen == "confirm_email":
                        page.wait_for_timeout(400)
                        continue

                    if not code_mode and not password_attempted and password_box.count() and password_box.is_visible() and next_button:
                        key = ("password", page.url)
                        if key not in submitted:
                            stage("đang nhập mật khẩu...")
                            fill(password_box, account.password, password=True)
                            password_attempted = True
                            submitted.add(key)
                            click(next_button)
                            continue

                    if screen != "code" and email_box is not None and next_button:
                        is_recovery = _is_add_recovery_email_screen(page, lower)
                        value = recovery if is_recovery else email_clean
                        key = ("email", value, page.url)
                        if key not in submitted:
                            if is_recovery:
                                stage(f"đang tự thêm mail KP ({recovery})...")
                                prepare_recovery_request()
                                confirmed_recovery = recovery
                            else:
                                stage("đang nhập địa chỉ mail...")
                            fill(email_box, value)
                            submitted.add(key)
                            click(next_button)
                            continue

                    if recovery_used and temp_page and screen == "code":
                        stage("đang chờ mã 6 số từ fviainboxes.com...")
                        if time.monotonic() - last_recovery_refresh >= 4.0:
                            refresh_recovery_mailbox()
                            last_recovery_refresh = time.monotonic()
                        eligible_records = [item for item in recovery_records if not password_attempted or "single-use code" not in str(item.get("subject", "")).casefold()]
                        newest = new_recovery_message(eligible_records, recovery_baseline_ids, recovery_requested_at)
                        if newest:
                            try:
                                item = temp_page.get_by_role("listitem").filter(has_text=str(newest.get("subject", ""))).first
                                if item.count() == 0:
                                    item = temp_page.locator(f"text={newest.get('subject', '')}").first
                                with temp_page.expect_response(
                                    lambda resp: "/message?" in resp.url
                                    and parse_qs(urlparse(resp.url).query).get("id", [""])[0] == str(newest["id"]),
                                    timeout=10000,
                                ) as resp_msg:
                                    item.click(timeout=5000)
                                payload = resp_msg.value.json()
                                _pause(temp_page, 0.2, 0.4)
                                code = extract_microsoft_code(payload)
                                if code and submit_recovery_code(page, code, fill, click, submitted, recovery_challenge):
                                    stage(f"đã nhập mã xác minh [{code}], chờ duyệt...")
                                    confirmed_recovery = recovery
                                    _pause(page, 0.4, 0.8)
                                    page.bring_to_front()
                                    continue
                            except Exception:
                                pass
                        page.wait_for_timeout(900)
                        continue

                    page.wait_for_timeout(450)

                raise TimeoutError("Hết thời gian chờ đăng nhập & cấp quyền OAuth2.")
            finally:
                try:
                    page.unroute("https://localhost/**", capture)
                except Exception:
                    pass
                if temp_page:
                    try:
                        temp_page.close()
                    except Exception:
                        pass
    finally:
        pool.cleanup_profile(profile_id)


def verify_saved_refresh_token(refresh_token: str, client_id: str = CLIENT_ID, raw_proxy: str = "") -> tuple[bool, str]:
    """Quickly test if an existing OAuth2 refresh_token is still alive and can read Outlook Inbox."""
    if not refresh_token.strip():
        return False, "Chưa có OAuth2 Token"
    proxy_cfg = parse_proxy_string(raw_proxy) if raw_proxy else {}
    proxies = requests_proxies_from_cfg(proxy_cfg)
    with requests.Session() as session:
        if proxies:
            session.proxies.update(proxies)
        res = session.post(
            TOKEN_URL,
            data={
                "client_id": client_id.strip() or CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.strip(),
            },
            timeout=20,
        )
        try:
            payload = res.json()
        except ValueError:
            payload = {}
        if not res.ok or not payload.get("access_token"):
            err = payload.get("error") or f"HTTP {res.status_code}"
            return False, f"Token chết ({err})"
        inbox = session.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            headers={"Authorization": "Bearer " + str(payload["access_token"])},
            params={"$top": 5, "$select": "subject,from,receivedDateTime"},
            timeout=15,
        )
        if not inbox.ok:
            return False, f"Lỗi đọc Inbox ({inbox.status_code})"
        msgs = inbox.json().get("value") or []
        return True, f"OK (Inbox đọc tốt · {len(msgs)} thư gần nhất)"


def _parse_iso_timestamp(raw: Any) -> float:
    s = str(raw or "").strip()
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _format_age(ts: float) -> str:
    if ts <= 0:
        return ""
    diff = max(0, int(time.time() - ts))
    if diff < 60:
        return f"{diff}s trước"
    if diff < 3600:
        return f"{diff // 60}p trước"
    if diff < 86400:
        return f"{diff // 3600}h trước"
    return f"{diff // 86400} ngày trước"


def _extract_otp_from_message(subject: str, body_text: str, sender_str: str) -> str:
    """Extract verification / OTP code (4-8 digits) from both service OTP emails and plain number emails."""
    clean_subj = unicodedata.normalize("NFC", html.unescape(re.sub(r"<[^>]+>", " ", subject or ""))).strip()
    # Strip style/script blocks before stripping HTML tags so CSS hex/px numbers never pollute
    cleaned_html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", body_text or "")
    clean_body = unicodedata.normalize("NFC", html.unescape(re.sub(r"<[^>]+>", " ", cleaned_html)))
    # Strip URLs and email addresses so query IDs or digits inside addresses don't false-match
    clean_body_no_urls = re.sub(r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+", " ", clean_body).strip()
    combined = f"{clean_subj}\n{clean_body_no_urls}"

    # 1. Explicit contextual patterns (TikTok, Microsoft, Google, FB, general OTP)
    contextual_patterns = (
        r"(?:verification\s+code|security\s+code|single[- ]use\s+code|login\s+code|confirmation\s+code|"
        r"your\s+code\s+is|code\s+is|otp(?:\s+code)?|mã\s+xác\s+minh|mã\s+bảo\s+mật|mã\s+xác\s+nhận|mã\s+của\s+bạn(?:\s+là)?)"
        r"[^0-9A-Za-z]{0,18}(\d{4,8})(?!\d)",
        r"(?<!\d)(\d{4,8})(?=\s+is\s+your\s+(?:verification|tiktok|security|login|confirmation)\s+code)",
    )
    for pat in contextual_patterns:
        m = re.search(pat, combined, re.I)
        if m:
            return m.group(1)

    # 2. Subject line standalone 4-8 digit code (ignoring 4-digit years if mail has long text)
    for m_sub in re.finditer(r"(?<![#\d])(\d{4,8})(?!\d)", clean_subj):
        cand = m_sub.group(1)
        if len(cand) == 6 or len(clean_subj) <= 20:
            return cand

    # 3. Standalone 6-digit code anywhere in visible plain body (works for both OTP mails & plain manual mails like "123456")
    for m_b6 in re.finditer(r"(?<![#/\d.-])(\d{6})(?![\d.-])", clean_body_no_urls):
        return m_b6.group(1)

    # 4. Standalone 4-8 digit code in body if verification keywords exist OR plain short message
    lower_all = f"{sender_str} {clean_subj} {clean_body_no_urls}".casefold()
    is_code_mail = len(clean_body_no_urls) <= 120 or any(
        kw in lower_all
        for kw in (
            "tiktok",
            "verify",
            "verification",
            "code",
            "otp",
            "xác minh",
            "xác nhận",
            "bảo mật",
            "security",
            "passcode",
        )
    )
    if is_code_mail:
        for m_b4 in re.finditer(r"(?<![#/\d.-])(\d{4,8})(?![\d.-])", clean_body_no_urls):
            cand = m_b4.group(1)
            if len(cand) == 4 and 1990 <= int(cand) <= 2035 and len(clean_body_no_urls) > 20:
                continue
            return cand
    return ""


def fetch_recent_messages_oauth2(
    refresh_token: str,
    client_id: str = CLIENT_ID,
    raw_proxy: str = "",
    top_n: int = 15,
) -> tuple[list[dict[str, Any]], str]:
    """Exchange OAuth2 refresh_token and read recent Inbox + Junk messages via Microsoft Graph.

    Returns (sorted_messages_newest_first, rotated_refresh_token).
    """
    if not refresh_token.strip():
        raise RuntimeError("Dòng này chưa có OAuth2 Token.")

    proxy_cfg = parse_proxy_string(raw_proxy) if raw_proxy else {}
    proxies = requests_proxies_from_cfg(proxy_cfg)

    def _run_fetch(use_proxies: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        with requests.Session() as session:
            if use_proxies:
                session.proxies.update(use_proxies)
            res = session.post(
                TOKEN_URL,
                data={
                    "client_id": client_id.strip() or CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token.strip(),
                },
                timeout=20,
            )
            try:
                payload = res.json()
            except ValueError:
                payload = {}
            if not res.ok or not payload.get("access_token"):
                err = payload.get("error") or f"HTTP {res.status_code}"
                raise RuntimeError(f"OAuth2 lỗi ({err})")

            access_token = str(payload["access_token"])
            rotated_rt = str(payload.get("refresh_token") or refresh_token.strip())

            collected: dict[str, dict[str, Any]] = {}
            endpoints = (
                ("Inbox", "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"),
                ("Junk", "https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages"),
            )
            for folder_label, ep in endpoints:
                r_msg = session.get(
                    ep,
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "$top": top_n,
                        "$orderby": "receivedDateTime desc",
                        "$select": "id,subject,bodyPreview,body,from,receivedDateTime",
                    },
                    timeout=20,
                )
                if not r_msg.ok:
                    continue
                for item in r_msg.json().get("value") or []:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or "")
                    if mid and mid not in collected:
                        item["_folder"] = folder_label
                        item["_ts"] = _parse_iso_timestamp(item.get("receivedDateTime"))
                        collected[mid] = item

            ordered = sorted(collected.values(), key=lambda x: float(x.get("_ts") or 0.0), reverse=True)
            return ordered, rotated_rt

    try:
        return _run_fetch(proxies)
    except requests.RequestException:
        if proxies:
            return _run_fetch({})
        raise


def fetch_latest_mail_code_oauth2(
    refresh_token: str,
    client_id: str = CLIENT_ID,
    raw_proxy: str = "",
) -> dict[str, str]:
    """Read Inbox + Junk via OAuth2 and extract the newest verification code sent to this mailbox."""
    messages, rotated_rt = fetch_recent_messages_oauth2(refresh_token, client_id=client_id, raw_proxy=raw_proxy, top_n=20)
    if not messages:
        return {
            "code": "",
            "info": "Hộp thư trống (0 thư)",
            "subject": "",
            "sender": "",
            "rotated_token": rotated_rt,
        }

    for msg in messages:
        sender_obj = ((msg.get("from") or {}).get("emailAddress") or {})
        sender_addr = str(sender_obj.get("address") or "")
        sender_name = str(sender_obj.get("name") or sender_addr or "Unknown")
        subj = str(msg.get("subject") or "")
        body_content = str((msg.get("body") or {}).get("content") or msg.get("bodyPreview") or "")
        code = _extract_otp_from_message(subj, body_content, f"{sender_name} {sender_addr}")
        if code:
            age = _format_age(float(msg.get("_ts") or 0.0))
            folder = str(msg.get("_folder") or "Inbox")
            info = f"{sender_name} ({folder} · {age}) — {subj[:60]}"
            return {
                "code": code,
                "info": info,
                "subject": subj,
                "sender": sender_name,
                "age": age,
                "rotated_token": rotated_rt,
            }

    newest = messages[0]
    newest_subj = str(newest.get("subject") or "(Không tiêu đề)")[:45]
    newest_age = _format_age(float(newest.get("_ts") or 0.0))
    return {
        "code": "",
        "info": f"Chưa thấy mã số (Thư mới nhất {newest_age}: {newest_subj})",
        "subject": newest_subj,
        "sender": "",
        "age": newest_age,
        "rotated_token": rotated_rt,
    }
