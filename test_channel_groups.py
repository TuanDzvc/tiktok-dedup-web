# -*- coding: utf-8 -*-
"""Tests for per-account Check Nhóm Kênh store, legacy v1 migration, and API endpoints."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
import channel_groups_store as cgs


class ChannelGroupsStoreTests(unittest.TestCase):
    def test_extract_tiktok_handle(self):
        self.assertEqual(cgs.extract_tiktok_handle("@MoodyBfPisw"), "moodybfpisw")
        self.assertEqual(cgs.extract_tiktok_handle("https://www.tiktok.com/@seitezhm99d?is_from_webapp=1"), "seitezhm99d")
        self.assertEqual(cgs.extract_tiktok_handle("tiktok.com/@user9434878202398/video/123456"), "user9434878202398")
        self.assertEqual(cgs.extract_tiktok_handle("https://www.youtube.com/@JeromeASF"), "")
        self.assertEqual(cgs.extract_tiktok_handle(""), "")

    def test_infer_top_group_name_matches_extension_rules(self):
        self.assertEqual(cgs.infer_top_group_name("US 1"), "US")
        self.assertEqual(cgs.infer_top_group_name("US Game"), "US")
        self.assertEqual(cgs.infer_top_group_name("ĐỨC 2"), "ĐỨC")
        self.assertEqual(cgs.infer_top_group_name("MEXICO"), "MEXICO")
        self.assertEqual(cgs.infer_top_group_name("INDO WEB"), "INDO WEB")
        self.assertEqual(cgs.infer_top_group_name("INDO WEB 2"), "INDO WEB")
        self.assertEqual(cgs.infer_top_group_name("Nuôi Follow US"), "Nuôi Follow US")

    def test_authenticated_user_check(self):
        self.assertTrue(cgs.is_authenticated_user({"username": "tuandzvcc", "role": "admin"}))
        self.assertTrue(cgs.is_authenticated_user({"username": "voitenlua1", "role": "vip"}))
        self.assertTrue(cgs.is_authenticated_user({"username": "free_member", "role": "member"}))
        self.assertFalse(cgs.is_authenticated_user({"username": ""}))
        self.assertFalse(cgs.is_authenticated_user(None))

    def test_legacy_v1_migrates_to_tuandzvcc_and_merges_per_account(self):
        legacy_v1 = {
            "updated_at": 100,
            "top_tab_order": ["top-us"],
            "groups": [{"id": "g1", "name": "US 1", "channels": [{"username": "chan_owner"}]}],
        }
        remote_v2 = {
            "version": 2,
            "users": {
                "tuandzvcc": {"updated_at": 0, "groups": []},
                "voitenlua1": {
                    "updated_at": 200,
                    "top_tab_order": ["top-duc"],
                    "groups": [{"id": "g2", "name": "ĐỨC 1", "channels": [{"username": "chan_vip"}]}],
                },
            },
        }
        merged = cgs.merge_channel_groups_state(legacy_v1, remote_v2)
        self.assertEqual(merged["version"], 2)
        self.assertEqual(merged["users"]["tuandzvcc"]["groups"][0]["channels"][0]["username"], "chan_owner")
        self.assertEqual(merged["users"]["voitenlua1"]["groups"][0]["channels"][0]["username"], "chan_vip")


class ChannelGroupsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cg_file = Path(self.tmp.name) / "channel_groups.json"

    async def call_api(self, method, body=None, user=None, history=None):
        import server
        encoded = json.dumps(body or {}).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": encoded}

        req = Request({"type": "http", "method": method, "path": "/api/channel-groups", "headers": []}, receive)
        with ExitStack() as stack:
            stack.enter_context(patch.object(server, "CHANNEL_GROUPS_FILE", self.cg_file))
            stack.enter_context(patch.object(server, "_extract_vip_user", return_value=user))
            stack.enter_context(patch.object(server, "_load_check_history_unlocked", return_value=history or {}))
            stack.enter_context(patch.object(server, "_schedule_peer_sync_push"))
            if method == "GET":
                resp = server.api_get_channel_groups(req)
            else:
                resp = await server.api_save_channel_groups(req)
            return resp.status_code, json.loads(resp.body)

    async def test_unauthenticated_rejected_and_accounts_are_strictly_isolated(self):
        st_anon, _ = await self.call_api("GET", user=None)
        self.assertEqual(st_anon, 401)
        st_anon_post, _ = await self.call_api("POST", {"groups": []}, user=None)
        self.assertEqual(st_anon_post, 401)

        # Pre-seed legacy v1 file on disk -> tuandzvcc should automatically inherit it, while voitenlua1 sees empty
        self.cg_file.write_text(json.dumps({
            "updated_at": 500,
            "top_tab_order": ["top-us"],
            "groups": [{"id": "g_owner", "name": "US 1", "channels": [{"username": "online.comedy0.t"}]}],
        }), encoding="utf-8")

        owner = {"username": "tuandzvcc", "role": "admin"}
        vip_user = {"username": "voitenlua1", "role": "vip"}

        st_owner_get, owner_board = await self.call_api("GET", user=owner)
        self.assertEqual(st_owner_get, 200)
        self.assertEqual(len(owner_board["groups"]), 1)
        self.assertEqual(owner_board["groups"][0]["channels"][0]["username"], "online.comedy0.t")

        st_vip_get0, vip_board0 = await self.call_api("GET", user=vip_user)
        self.assertEqual(st_vip_get0, 200)
        self.assertEqual(vip_board0["groups"], [])

        # VIP customer saves their own channel group -> does NOT touch owner's group
        vip_payload = {
            "top_tab_order": ["top-duc"],
            "groups": [{
                "id": "g_duc1",
                "name": "ĐỨC 1",
                "channels": [{"username": "https://www.tiktok.com/@seitezhm99d"}],
            }],
        }
        st_save, saved_vip = await self.call_api("POST", vip_payload, user=vip_user)
        self.assertEqual(st_save, 200)
        self.assertEqual(saved_vip["groups"][0]["topGroupName"], "ĐỨC")
        self.assertEqual(saved_vip["groups"][0]["channels"][0]["username"], "seitezhm99d")

        # Accidental empty overwrite blocked without allow_empty
        st_empty, _ = await self.call_api("POST", {"groups": []}, user=vip_user)
        self.assertEqual(st_empty, 409)

        # Owner still only has owner's channels
        _, owner_after = await self.call_api("GET", user=owner)
        self.assertEqual(len(owner_after["groups"]), 1)
        self.assertEqual(owner_after["groups"][0]["channels"][0]["username"], "online.comedy0.t")

        # VIP GET hydrates avatar + lastCheck from VIP's own check_history only
        fake_hist = {
            "voitenlua1": {
                "entries": {
                    "seitezhm99d": {
                        "username": "seitezhm99d",
                        "nickname": "Seite Official",
                        "avatar": "https://example.com/av.jpg",
                        "checked_at": 9999999999,
                        "stats": {"total": 18, "dup": 2, "fyp": 1, "clean": 15},
                    }
                }
            }
        }
        st_get, got = await self.call_api("GET", user=vip_user, history=fake_hist)
        self.assertEqual(st_get, 200)
        ch = got["groups"][0]["channels"][0]
        self.assertEqual(ch["name"], "Seite Official")
        self.assertEqual(ch["avatarUrl"], "https://example.com/av.jpg")
        self.assertEqual(ch["lastCheck"]["dup"], 2)
        self.assertEqual(ch["lastCheck"]["fyp"], 1)


if __name__ == "__main__":
    unittest.main()
