import asyncio
import copy
import gzip
import json
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request
import server
from sync_transport import content_hash, wire_response, decode_body, ConditionalJSONSnapshot, pull_peer, push_peer


SNAPSHOT = {'exported_at':100,'users':{'fixture':{'account_balance_vnd':50000,'updated_at':100}},
            'check_history':{'fixture':{'entries':{'channel':{'videos':[{'id':'123','description':'fixture '*2000}]}}}}}


class TransportTests(unittest.TestCase):
    def test_hash_ignores_only_export_time_and_detects_wallet_and_history_changes(self):
        other = copy.deepcopy(SNAPSHOT)
        other['exported_at'] = 200
        self.assertEqual(content_hash(other), content_hash(SNAPSHOT))
        other['users']['fixture']['account_balance_vnd'] = 40000
        self.assertNotEqual(content_hash(other), content_hash(SNAPSHOT))
        other = copy.deepcopy(SNAPSHOT)
        other['check_history']['fixture']['entries']['channel']['videos'].append({'id':'456'})
        self.assertNotEqual(content_hash(other), content_hash(SNAPSHOT))

    def test_head_and_304_have_no_snapshot_body(self):
        code, body, headers = wire_response(SNAPSHOT, 'pc', {}, 'HEAD')
        self.assertEqual((code, body), (200, b''))
        self.assertEqual(headers['X-Sync-Protocol'], '2')
        code, body, _ = wire_response(SNAPSHOT, 'pc', {'if-none-match':headers['ETag']})
        self.assertEqual((code, body), (304, b''))

    def test_compression_round_trips_every_field_without_truncating_history(self):
        code, body, headers = wire_response(SNAPSHOT, 'pc', {'accept-encoding':'gzip, deflate'})
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        original = json.dumps({'success':True,'role':'pc','snapshot':SNAPSHOT}).encode()
        self.assertLess(len(body), len(original)/5)
        self.assertEqual(json.loads(gzip.decompress(body))['snapshot'], SNAPSHOT)
        self.assertEqual(decode_body(body, 'gzip')['snapshot'], SNAPSHOT)

    def test_invalid_or_oversized_compressed_data_is_rejected(self):
        for payload in [b'not gzip', gzip.compress(b'x'*1000), gzip.compress(b'{}')[:-3]]:
            with self.assertRaises(ValueError):
                decode_body(payload, 'gzip', limit=100)

    def test_unchanged_peer_issues_only_head_not_get_or_post(self):
        http = Mock()
        http.head.return_value = Mock(status_code=200, headers={'X-Sync-Protocol':'2','X-Snapshot-Hash':content_hash(SNAPSHOT)})
        peer = pull_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, True)
        result = push_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, peer)
        self.assertTrue(result['ok'])
        http.get.assert_not_called()
        http.post.assert_not_called()

    def test_old_peer_uses_durable_storage_without_full_download(self):
        http = Mock()
        http.head.return_value = Mock(status_code=405, headers={})
        peer = pull_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, True)
        self.assertEqual(peer['skipped'], 'shared_durable_store')
        push_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, peer)
        http.get.assert_not_called()
        http.post.assert_not_called()

    def test_old_peer_is_still_usable_when_durable_storage_is_down(self):
        http = Mock()
        http.head.return_value = Mock(status_code=405, headers={})
        http.get.return_value = Mock(status_code=200)
        http.get.return_value.json.return_value = {'success':True,'snapshot':SNAPSHOT}
        peer = pull_peer(http, 'https://fixture.test/sync', {}, {}, False)
        self.assertEqual(peer['snapshot'], SNAPSHOT)
        http.get.assert_called_once()

    def test_merged_updates_use_compressed_post_only_when_different(self):
        http = Mock()
        http.post.return_value = Mock(status_code=200)
        http.post.return_value.json.return_value = {'success':True,'changed':True}
        self.assertTrue(push_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT,
            {'ok':True,'modern':True,'hash':'different'})['changed'])
        kw = http.post.call_args.kwargs
        self.assertEqual(kw['headers']['Content-Encoding'], 'gzip')
        self.assertEqual(decode_body(kw['data'], 'gzip')['snapshot'], SNAPSHOT)

    def test_failed_pull_never_pushes_a_potentially_stale_snapshot(self):
        http = Mock()
        http.head.side_effect = TimeoutError()
        peer = pull_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, False)
        push_peer(http, 'https://fixture.test/sync', {}, SNAPSHOT, peer)
        http.post.assert_not_called()

    def test_supabase_304_returns_independent_cached_copy(self):
        http = Mock()
        first = Mock(status_code=200, headers={'ETag':'"version-1"'})
        first.json.return_value = copy.deepcopy(SNAPSHOT)
        http.get.side_effect = [first, Mock(status_code=304)]
        cache = ConditionalJSONSnapshot()
        loaded = cache.read(http, 'https://fixture.test/object', {})
        loaded['users'].clear()
        loaded = cache.read(http, 'https://fixture.test/object', {})
        self.assertEqual(loaded, SNAPSHOT)
        self.assertEqual(cache.last_status, 'not_modified')
        self.assertEqual(http.get.call_args.kwargs['headers']['If-None-Match'], '"version-1"')


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def request(self, method='GET', headers=None, body=b''):
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        values = {'x-sync-token':'fixture-key', **(headers or {})}
        return Request({'type':'http','method':method,'headers':[(k.encode(),v.encode()) for k,v in values.items()]}, receive)

    async def test_auth_precedes_export_including_head(self):
        with patch.object(server, '_get_sync_token', return_value='other'), \
             patch.object(server, '_export_sync_snapshot_unlocked') as export:
            response = await server.api_internal_get_sync_state(self.request('HEAD'))
        self.assertEqual(response.status_code, 403)
        export.assert_not_called()

    async def test_gzip_post_passes_the_exact_snapshot_to_existing_merge(self):
        body = gzip.compress(json.dumps({'snapshot':SNAPSHOT}).encode())
        with patch.object(server, '_get_sync_token', return_value='fixture-key'), \
             patch.object(server, '_is_cloud_runtime', return_value=False), \
             patch.object(server, '_merge_sync_state_unlocked', return_value={'changed':True,'user_count':1}) as merge:
            response = await server.api_internal_post_sync_state(self.request('POST', {'content-encoding':'gzip'}, body))
        self.assertEqual(response.status_code, 200)
        merge.assert_called_once_with(SNAPSHOT)

    async def test_bad_gzip_is_rejected_before_merge(self):
        with patch.object(server, '_get_sync_token', return_value='fixture-key'), \
             patch.object(server, '_is_cloud_runtime', return_value=False), \
             patch.object(server, '_merge_sync_state_unlocked') as merge:
            response = await server.api_internal_post_sync_state(self.request('POST', {'content-encoding':'gzip'}, b'bad'))
        self.assertEqual(response.status_code, 400)
        merge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
