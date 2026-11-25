import asyncio
import ast
import copy
import hmac
import json
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core_dedup as core
import native_dedup as native


def page():
    author = {'uid': '7000000000000000000', 'unique_id': 'seitezhm99d', 'nickname': 'Test', 'region': 'US'}
    return {
        'success': True, 'channel': {'uniqueId': 'seitezhm99d', 'nickname': 'Test', 'followers': 100, 'videoCount': 284},
        'secUid': 'sec-test', 'hasMore': True, 'maxCursor': 1795000000000,
        'rawItems': [
            {'aweme_id': '7710000000000000001', 'group_id': '7710000000000000001', 'author': author,
             'create_time': 1795000000, 'video': {'duration': 120000}, 'statistics': {'play_count': 454}},
            {'aweme_id': '7710000000000000002', 'group_id': '7700000000000000001', 'author': author,
             'create_time': 1794000000, 'video': {'duration': 120000}, 'is_nff_or_nr': True},
            {'aweme_id': '7710000000000000003', 'group_id': '7710000000000000003', 'author': author,
             'create_time': 1793000000, 'video': {'duration': 120000}, 'status': {'video_status': 140}},
        ],
    }


class CoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'DEDUP_PROVIDER': 'native'}))
        for name in ('sync_items_tikhub_groupid', '_fetch_tikhub_multi_awemes',
                     '_fetch_channel_info_and_posts_legacy', '_fetch_origin_videos_meta_free',
                     'fetch_external_channel_videos_creator_api'):
            self.stack.enter_context(patch.object(core, name, side_effect=AssertionError('Unexpected provider call: ' + name)))
        self.stack.enter_context(patch.object(core, '_load_persistent_groupid_cache', return_value={}))
        self.stack.enter_context(patch.object(core, '_load_origin_meta_cache', return_value={}))
        self.stack.enter_context(patch.object(core, '_load_tiktokmanager_cache', side_effect=AssertionError('stale manager cache')))

    def test_native_uses_existing_schema_and_never_tikhub(self):
        with patch.object(native, 'fetch_native_page', return_value=page()) as fetch:
            result = core.fetch_channel_info_and_posts('seitezhm99d', allow_paid_groupid=True)
        self.assertTrue(result['success'])
        self.assertEqual(result['channel']['videoCount'], 284)
        self.assertEqual(result['list'][0]['play_count'], 454)
        self.assertEqual(result['list'][1]['group_id'], '7700000000000000001')
        self.assertTrue(result['list'][1]['origin_url'])
        self.assertTrue(result['list'][1]['is_fyp_restricted'])
        self.assertFalse(result['list'][2]['is_takedown'])
        self.assertEqual(result['stats_summary']['duplicate_count'], 1)
        self.assertEqual(result['maxCursor'], 1795000000000)
        self.assertEqual(result['_internal_cost']['api_calls'], 0)
        self.assertEqual(result['_internal_cost']['cost_usd'], 0)
        self.assertEqual(result['_internal_cost']['provider'], 'native')
        fetch.assert_called_once_with('seitezhm99d', '', 0, 18)

    def test_native_error_does_not_charge_or_fall_back(self):
        with patch.object(native, 'fetch_native_page', side_effect=native.NativeReaderError('license expired')):
            result = core.fetch_channel_info_and_posts('test', allow_paid_groupid=True)
        self.assertFalse(result['success'])
        self.assertIn('license expired', result['error'])
        self.assertEqual(result['_internal_cost']['api_calls'], 0)

    def test_free_quota_lock_still_hides_duplicate_fields(self):
        with patch.object(native, 'fetch_native_page', return_value=page()):
            result = core.fetch_channel_info_and_posts('test', allow_paid_groupid=False)
        self.assertTrue(result['success'])
        self.assertFalse(result['vip_mode'])
        self.assertTrue(all(not row['group_id'] for row in result['list']))

    def test_pagination_passes_opaque_mobile_cursor_without_offset_math(self):
        with patch.object(native, 'fetch_native_page', return_value=page()) as fetch:
            result = core.fetch_channel_info_and_posts('seitezhm99d', sec_uid='sec-test',
                                                      max_cursor=1795000000000, allow_paid_groupid=True)
        self.assertEqual(len(result['list']), 3)
        fetch.assert_called_once_with('seitezhm99d', 'sec-test', 1795000000000, 18)

    def test_force_external_does_not_use_supplied_own_cookie(self):
        with patch.object(native, 'fetch_native_page', return_value=page()), \
             patch.object(core, 'sync_items_penalty_data_native', side_effect=AssertionError('own-cookie leak')):
            result = core.fetch_channel_info_and_posts('!test', cookie_str='test-cookie', allow_paid_groupid=True)
        self.assertTrue(result['success'])

    def test_paid_mode_is_only_used_when_operator_explicitly_selects_it(self):
        with patch.dict(os.environ, {'DEDUP_PROVIDER': 'tikhub'}), \
             patch.object(core, '_fetch_channel_info_and_posts_legacy', return_value={'success': True}) as legacy, \
             patch.object(native, 'fetch_native_page', side_effect=AssertionError('wrong provider')):
            result = core.fetch_channel_info_and_posts('test', allow_paid_groupid=True)
        self.assertTrue(result['success'])
        legacy.assert_called_once()

    def test_full_video_link_keeps_requested_video_first_without_paid_lookup(self):
        target = '7710000000000000002'
        with patch.object(native, 'fetch_native_page', return_value=page()):
            result = core.fetch_channel_info_and_posts('https://www.tiktok.com/@seitezhm99d/video/' + target,
                                                      allow_paid_groupid=True)
        self.assertEqual(result['list'][0]['aweme_id'], target)

    def test_unreadable_target_video_is_not_reported_clean(self):
        with patch.object(native, 'fetch_native_page', return_value=page()) as fetch:
            result = core.fetch_channel_info_and_posts('https://www.tiktok.com/@seitezhm99d/video/7700000000000000099',
                                                      allow_paid_groupid=True)
        self.assertFalse(result['success'])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result['_internal_cost']['api_calls'], 0)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        native._CACHE.clear()
        native._CHANNELS.clear()

    def test_duplicate_scans_reuse_cache_without_mutating_it(self):
        with patch.object(native, 'setting', side_effect=lambda name, default='': default), \
             patch.object(native, '_local_fetch', return_value=page()) as fetch:
            first = native.fetch_native_page('test')
            first['rawItems'].clear()
            again = native.fetch_native_page('test')
        self.assertEqual(len(again['rawItems']), 3)
        fetch.assert_called_once()

    def test_linux_local_reader_fails_closed(self):
        with patch.object(native.sys, 'platform', 'linux'):
            with self.assertRaises(native.NativeReaderError):
                native._local_fetch({'channel': 'test'})

    def test_pc_never_relays_back_to_itself_even_if_bridge_is_configured(self):
        with patch.object(native.sys, 'platform', 'win32'), \
             patch.object(native, '_remote_fetch', side_effect=AssertionError('PC relay loop')), \
             patch.object(native, '_local_fetch', return_value=page()) as local:
            native.fetch_native_page('test')
        local.assert_called_once()

    def test_relay_uses_auth_header_and_no_redirects(self):
        values = {'DEDUP_NATIVE_BRIDGE_URL': 'https://pc.example.test', 'VIP_AUTH_SECRET': 'test-only-secret'}
        with patch.object(native, 'setting', side_effect=lambda key, default='': values.get(key, default)), \
             patch.object(native.requests, 'post', return_value=SimpleNamespace(status_code=200, json=page)) as post:
            result = native._remote_fetch({'channel': 'test'})
        self.assertTrue(result['success'])
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertTrue(post.call_args.kwargs['headers']['x-native-reader-token'])
        self.assertNotIn('test-only-secret', post.call_args.args[0])


class RelayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_requires_token_and_never_relays_from_cloud(self):
        # Compile the actual route without importing server.py's payment/sync state.
        from starlette.responses import JSONResponse
        source = ast.parse((native.ROOT / 'server.py').read_text(encoding='utf-8'))
        route = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'api_internal_native_dedup')
        route.decorator_list = []
        namespace = {'Request': object, 'asyncio': asyncio, 'JSONResponse': JSONResponse,
                     'hmac': hmac, 'sys': SimpleNamespace(platform='win32'), '_is_cloud_runtime': lambda: False}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), 'route', 'exec'), namespace)
        class Request:
            def __init__(self, token): self.headers = {'x-native-reader-token': token}
            async def json(self): return {'channel': 'test'}
        with patch.object(native, 'bridge_token', return_value='test-token'), \
             patch.object(native, 'fetch_native_page', return_value=page()) as fetch:
            self.assertEqual((await namespace[route.name](Request('bad'))).status_code, 403)
            fetch.assert_not_called()
            response = await namespace[route.name](Request('test-token'))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(fetch.call_args.kwargs['local_only'])
            namespace['_is_cloud_runtime'] = lambda: True
            self.assertEqual((await namespace[route.name](Request('test-token'))).status_code, 503)


if __name__ == '__main__':
    unittest.main()
