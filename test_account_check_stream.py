import asyncio
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

import core_dedup
import server


def profile(name, ok=True):
    return {'ok': ok, 'unique_id': name, 'input': name,
            'recommend_status': 'ok' if ok else '', '_api_calls': 1}


class CoreProgressTests(unittest.TestCase):
    def test_fast_profile_is_published_while_first_profile_still_running(self):
        release = threading.Event()
        published = threading.Event()
        events = []

        def check(name, api_key=''):
            if name == 'slow':
                release.wait(4)
            return profile(name)

        def notify(event):
            events.append(event)
            if event['result']['unique_id'] == 'fast':
                published.set()

        with patch.object(core_dedup, '_resolve_bulk_account_handle', side_effect=lambda value: value), \
             patch.object(core_dedup, '_get_tikhub_api_key', return_value='fixture'), \
             patch.object(core_dedup, 'check_single_account_profile_tikhub', side_effect=check), \
             ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(core_dedup.check_accounts_batch_tikhub, ['slow', 'fast'], 2, notify)
            try:
                self.assertTrue(published.wait(2))
                self.assertFalse(task.done())
                self.assertEqual(events[0]['index'], 1)
                self.assertNotIn('_api_calls', events[0]['result'])
            finally:
                release.set()
            final = task.result(timeout=3)
        self.assertEqual([r['unique_id'] for r in final['results']], ['slow', 'fast'])
        self.assertEqual(final['api_calls'], 2)

    def test_resolved_duplicate_only_billed_and_reported_once(self):
        events = []
        with patch.object(core_dedup, '_resolve_bulk_account_handle', return_value='same'), \
             patch.object(core_dedup, '_get_tikhub_api_key', return_value='fixture'), \
             patch.object(core_dedup, 'check_single_account_profile_tikhub', return_value=profile('same')):
            final = core_dedup.check_accounts_batch_tikhub(['@same', 'https://www.tiktok.com/@same'], 2, events.append)
        self.assertEqual(len(events), 1)
        self.assertEqual(final['duplicate_lines_skipped'], 1)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.users = {'fixture': {'username': 'fixture', 'role': 'member', 'active': True,
                                  'account_balance_vnd': 1000, 'expires_at': 0}}
        self.history = []
        self.stack.enter_context(patch.object(server, '_load_vip_users_unlocked', side_effect=lambda: self.users))
        self.stack.enter_context(patch.object(server, '_save_vip_users_unlocked'))
        self.stack.enter_context(patch.object(server, '_record_account_check_history_for_user',
            side_effect=lambda username, results: self.history.extend(copy.deepcopy(results))))
        self.stack.enter_context(patch.object(server, '_ACCOUNT_CHECK_USER_LOCKS', {}))

    async def finish(self, response):
        return [json.loads(line) async for line in response.body_iterator]

    async def test_result_is_billed_and_saved_before_remaining_batch_finishes(self):
        release = threading.Event()

        def batch(inputs, workers, notify):
            notify({'index': 1, 'total': 2, 'result': {'ok': True, 'unique_id': 'fast'},
                    'api_calls': 1, 'cached_country_hits': 0})
            release.wait(4)
            notify({'index': 0, 'total': 2, 'result': {'ok': False, 'input': 'slow', 'error': 'Unavailable'},
                    'api_calls': 1, 'cached_country_hits': 0})
            return {'success': True, 'results': [{'ok':False}, {'ok':True}], 'summary': {'total': 2}}

        self.stack.enter_context(patch.object(server, 'check_accounts_batch_tikhub', side_effect=batch))
        response = await server._stream_account_checks('fixture', ['slow', 'fast'], 0)
        iterator = response.body_iterator
        self.assertEqual(json.loads(await anext(iterator))['type'], 'start')
        try:
            first = json.loads(await asyncio.wait_for(anext(iterator), timeout=2))
            self.assertEqual(first['type'], 'result')
            self.assertEqual(first['index'], 1)
            self.assertEqual(first['completed'], 1)
            self.assertEqual(first['user_billing']['balance_vnd'], 750)
            self.assertEqual(self.history[0]['unique_id'], 'fast')
            self.assertTrue(server._get_user_account_check_lock('fixture').locked())
        finally:
            release.set()
        tail = [json.loads(line) async for line in iterator]
        self.assertEqual(tail[-1]['type'], 'done')
        self.assertEqual(tail[0]['user_billing']['charged_vnd'], 250)
        self.assertEqual(tail[0]['user_billing']['error_skipped_count'], 1)
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 750)
        self.assertFalse(server._get_user_account_check_lock('fixture').locked())

    async def test_disconnect_does_not_lose_billing_or_history(self):
        release = threading.Event()

        def batch(inputs, workers, notify):
            release.wait(4)
            notify({'index':0, 'total':1, 'result':{'ok':True, 'unique_id':'finished'},
                    'api_calls':1, 'cached_country_hits':0})
            return {'success':True, 'results':[{'ok':True}]}

        self.stack.enter_context(patch.object(server, 'check_accounts_batch_tikhub', side_effect=batch))
        response = await server._stream_account_checks('fixture', ['finished'], 0)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(server._ACCOUNT_STREAM_TASKS)), timeout=3)
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 750)
        self.assertEqual(len(self.history), 1)

    async def test_insufficient_balance_does_not_start_work_and_releases_lock(self):
        self.users['fixture']['account_balance_vnd'] = 0
        with patch.object(server, 'check_accounts_batch_tikhub') as check:
            response = await server._stream_account_checks('fixture', ['one'], 0)
        self.assertEqual(response.status_code, 402)
        check.assert_not_called()
        self.assertFalse(server._get_user_account_check_lock('fixture').locked())

    async def test_admin_keeps_balance_and_gets_final_api_cost(self):
        self.users['fixture']['role'] = 'admin'

        def batch(inputs, workers, notify):
            notify({'index':0, 'total':1, 'result':{'ok':True}, 'api_calls':1, 'cached_country_hits':0})
            return {'success':True, 'results':[{'ok':True}], 'cost_usd':0.001}

        self.stack.enter_context(patch.object(server, 'check_accounts_batch_tikhub', side_effect=batch))
        self.stack.enter_context(patch.object(server, '_get_tikhub_live_balance', return_value={
            'balance_known':True, 'balance_stale':False, 'total_usd':1, 'total_vnd':25500, 'est_scans_left':1000}))
        response = await server._stream_account_checks('fixture', ['one'], 0)
        events = await self.finish(response)
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 1000)
        self.assertEqual(events[1]['user_billing']['charged_vnd'], 0)
        self.assertEqual(events[-1]['admin_billing']['scan_cost_usd'], 0.001)

    async def test_failed_batch_keeps_already_completed_result_and_releases_lock(self):
        def batch(inputs, workers, notify):
            notify({'index':0, 'total':2, 'result':{'ok':True, 'unique_id':'saved'},
                    'api_calls':1, 'cached_country_hits':0})
            raise RuntimeError('synthetic interruption')

        self.stack.enter_context(patch.object(server, 'check_accounts_batch_tikhub', side_effect=batch))
        self.stack.enter_context(patch.object(server.logger, 'exception'))
        response = await server._stream_account_checks('fixture', ['saved', 'pending'], 0)
        events = await self.finish(response)
        self.assertEqual([event['type'] for event in events], ['start', 'result', 'error'])
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 750)
        self.assertEqual(len(self.history), 1)
        self.assertFalse(server._get_user_account_check_lock('fixture').locked())

    async def test_missing_user_releases_preflight_lock(self):
        self.users.clear()
        response = await server._stream_account_checks('fixture', ['one'], 0)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(server._get_user_account_check_lock('fixture').locked())


if __name__ == '__main__':
    unittest.main()
