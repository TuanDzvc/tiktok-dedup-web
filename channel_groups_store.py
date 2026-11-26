# -*- coding: utf-8 -*-
"""Per-account storage, normalization, migration, and 2-way sync helpers for Check Nhóm Kênh."""

import copy
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

LEGACY_OWNER_USERNAME = "tuandzvcc"


def is_authenticated_user(user: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(user, dict):
        return False
    uname = str(user.get("username") or "").strip().lower()
    return bool(uname)


def is_owner_admin(user: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(user, dict):
        return False
    role = str(user.get("role") or "").strip().lower()
    uname = str(user.get("username") or "").strip().lower()
    return role == "admin" or uname == LEGACY_OWNER_USERNAME


def extract_tiktok_handle(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")) or "tiktok.com/" in s.lower():
        if not s.startswith(("http://", "https://")):
            s = "https://" + s.lstrip("/")
        try:
            parsed = urlsplit(s)
            host = (parsed.hostname or "").lower()
            if host and not (host == "tiktok.com" or host.endswith(".tiktok.com")):
                return ""
            parts = [p for p in (parsed.path or "").split("/") if p]
            if parts and parts[0].startswith("@"):
                s = parts[0][1:]
            else:
                return ""
        except Exception:
            return ""
    s = s.lstrip("@").strip().lower()
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not re.fullmatch(r"[a-z0-9._]{2,64}", s):
        return ""
    return s


def clean_label(value: Any, max_len: int = 80) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]


def top_group_key(name: Any) -> str:
    s = clean_label(name, 80).replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"top-{s[:64]}" if s else ""


def infer_top_group_name(group_name: Any) -> str:
    val = clean_label(group_name, 80)
    if not val:
        return "KHÁC"
    base = re.sub(r"\s+\d+$", "", val).strip()
    if re.fullmatch(r"indo\s+web", base, flags=re.IGNORECASE):
        return "INDO WEB"
    m = re.fullmatch(
        r"(US|UK|INDO|ĐỨC|DUC|MEXICO|JP|KR|VN|BR|FR|ES|IT|TH|PH|MY|TW)(?:\s+(?:GAME|WEB))?",
        base,
        flags=re.IGNORECASE,
    )
    if m:
        raw_c = m.group(1).upper()
        return "ĐỨC" if raw_c in ("DUC", "ĐỨC") else raw_c
    return base or "KHÁC"


def empty_board_payload() -> Dict[str, Any]:
    return {"updated_at": 0, "top_tab_order": [], "groups": []}


def normalize_board_payload(raw: Any, now_ts: Optional[int] = None) -> Dict[str, Any]:
    ts = int(now_ts if now_ts is not None else time.time())
    if not isinstance(raw, dict):
        return empty_board_payload()

    raw_groups = raw.get("groups") if isinstance(raw.get("groups"), list) else []
    norm_groups: List[Dict[str, Any]] = []
    seen_group_ids = set()

    for g_idx, g in enumerate(raw_groups[:300]):
        if not isinstance(g, dict):
            continue
        g_name = clean_label(g.get("name"), 80)
        if not g_name:
            continue
        gid = re.sub(r"[^a-zA-Z0-9_.-]", "", str(g.get("id") or ""))[:64] or f"grp_{ts}_{g_idx}"
        if gid in seen_group_ids:
            gid = f"{gid}_{g_idx}"[:64]
        seen_group_ids.add(gid)

        explicit_top = clean_label(g.get("topGroupName"), 80)
        top_name = explicit_top or infer_top_group_name(g_name)
        row_id = re.sub(r"[^a-zA-Z0-9_.-]", "", str(g.get("rowId") or g.get("boardRowId") or ""))[:64] or gid

        raw_channels = g.get("channels") if isinstance(g.get("channels"), list) else []
        norm_channels: List[Dict[str, Any]] = []
        seen_handles = set()
        for ch in raw_channels[:500]:
            if not isinstance(ch, dict):
                continue
            handle = extract_tiktok_handle(ch.get("username") or ch.get("url") or ch.get("name"))
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)
            cid = re.sub(r"[^a-zA-Z0-9_.-]", "", str(ch.get("id") or ""))[:64] or f"ch_{handle}"
            disp_name = clean_label(ch.get("name"), 80) or handle
            avatar_url = str(ch.get("avatarUrl") or ch.get("avatar") or "").strip()[:1000]
            if avatar_url and not avatar_url.startswith(("http://", "https://")):
                avatar_url = ""
            ch_entry: Dict[str, Any] = {
                "id": cid,
                "username": handle,
                "name": disp_name,
                "url": f"https://www.tiktok.com/@{handle}",
                "avatarUrl": avatar_url,
                "addedAt": max(0, int(ch.get("addedAt") or ts)),
            }
            lc = ch.get("lastCheck")
            if isinstance(lc, dict) and int(lc.get("checkedAt") or 0) > 0:
                ch_entry["lastCheck"] = {
                    "checkedAt": int(lc.get("checkedAt") or 0),
                    "total": max(0, int(lc.get("total") or 0)),
                    "dup": max(0, int(lc.get("dup") or 0)),
                    "fyp": max(0, int(lc.get("fyp") or 0)),
                    "clean": max(0, int(lc.get("clean") or 0)),
                }
            norm_channels.append(ch_entry)

        norm_groups.append({
            "id": gid,
            "name": g_name,
            "topGroupName": top_name,
            "rowId": row_id,
            "channels": norm_channels,
        })

    raw_order = raw.get("top_tab_order") if isinstance(raw.get("top_tab_order"), list) else []
    clean_order: List[str] = []
    seen_order = set()
    for item in raw_order[:200]:
        k = str(item or "").strip()[:80]
        if k and k not in seen_order:
            seen_order.add(k)
            clean_order.append(k)

    upd = max(0, int(raw.get("updated_at") or 0))
    return {
        "updated_at": upd,
        "top_tab_order": clean_order,
        "groups": norm_groups,
    }


def normalize_store_payload(raw: Any, legacy_owner: str = LEGACY_OWNER_USERNAME) -> Dict[str, Any]:
    """Normalize multi-user store {'version': 2, 'users': {username: board}}.
    Transparently migrates v1 root-level {'groups': [...]} into `legacy_owner`."""
    if not isinstance(raw, dict):
        return {"version": 2, "users": {}}

    users_out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw.get("users"), dict):
        for raw_u, raw_board in raw["users"].items():
            uname = str(raw_u or "").strip().lower()
            if not uname or not isinstance(raw_board, dict):
                continue
            norm_b = normalize_board_payload(raw_board)
            if norm_b["groups"] or norm_b["updated_at"] > 0:
                users_out[uname] = norm_b
        return {"version": 2, "users": users_out}

    # Legacy v1 single-board format -> migrate to legacy_owner (tuandzvcc)
    if "groups" in raw or "top_tab_order" in raw or "updated_at" in raw:
        norm_legacy = normalize_board_payload(raw)
        owner_key = str(legacy_owner or LEGACY_OWNER_USERNAME).strip().lower()
        if owner_key and (norm_legacy["groups"] or norm_legacy["updated_at"] > 0):
            users_out[owner_key] = norm_legacy

    return {"version": 2, "users": users_out}


def get_user_board_from_store(store: Any, username: str) -> Dict[str, Any]:
    uname = str(username or "").strip().lower()
    if not uname:
        return empty_board_payload()
    norm_store = normalize_store_payload(store)
    board = norm_store["users"].get(uname)
    return copy.deepcopy(board) if isinstance(board, dict) else empty_board_payload()


def set_user_board_in_store(store: Any, username: str, board: Any, now_ts: Optional[int] = None) -> Dict[str, Any]:
    uname = str(username or "").strip().lower()
    norm_store = normalize_store_payload(store)
    if not uname:
        return norm_store
    norm_board = normalize_board_payload(board, now_ts=now_ts)
    norm_store["users"][uname] = norm_board
    return norm_store


def merge_single_board_state(local_raw: Any, remote_raw: Any) -> Dict[str, Any]:
    loc = normalize_board_payload(local_raw)
    rem = normalize_board_payload(remote_raw)
    l_ts = int(loc.get("updated_at") or 0)
    r_ts = int(rem.get("updated_at") or 0)
    l_has = bool(loc.get("groups"))
    r_has = bool(rem.get("groups"))
    if l_has and not r_has and r_ts == 0:
        return copy.deepcopy(loc)
    if r_has and not l_has and l_ts == 0:
        return copy.deepcopy(rem)
    if r_ts > l_ts:
        return copy.deepcopy(rem)
    return copy.deepcopy(loc)


def merge_channel_groups_state(
    local_raw: Any,
    remote_raw: Any,
    deleted_users: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """2-way merge across all accounts (supports both v1 legacy payloads and v2 multi-user stores)."""
    loc_store = normalize_store_payload(local_raw)
    rem_store = normalize_store_payload(remote_raw)
    del_map = deleted_users if isinstance(deleted_users, dict) else {}

    merged_users: Dict[str, Dict[str, Any]] = {}
    all_keys = sorted(set(loc_store["users"].keys()) | set(rem_store["users"].keys()))
    for uname in all_keys:
        if uname != LEGACY_OWNER_USERNAME and int(del_map.get(uname) or 0) > 0:
            # If user was deleted after their last board edit, drop stale board
            l_u = loc_store["users"].get(uname) or empty_board_payload()
            r_u = rem_store["users"].get(uname) or empty_board_payload()
            max_upd = max(int(l_u.get("updated_at") or 0), int(r_u.get("updated_at") or 0))
            if int(del_map.get(uname) or 0) >= max_upd:
                continue
        m_board = merge_single_board_state(
            loc_store["users"].get(uname),
            rem_store["users"].get(uname),
        )
        if m_board["groups"] or m_board["updated_at"] > 0:
            merged_users[uname] = m_board

    return {"version": 2, "users": merged_users}


def load_channel_groups_file(file_path: Path) -> Dict[str, Any]:
    if file_path.exists():
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            return normalize_store_payload(raw)
        except Exception:
            pass
    return {"version": 2, "users": {}}


def save_channel_groups_file(file_path: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    norm = normalize_store_payload(state)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if file_path.exists():
        try:
            bak2 = file_path.with_suffix(".json.bak2")
            bak1 = file_path.with_suffix(".json.bak1")
            if bak1.exists():
                os.replace(bak1, bak2)
            bak1.write_bytes(file_path.read_bytes())
        except Exception:
            pass
    tmp = file_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, file_path)
    return norm
