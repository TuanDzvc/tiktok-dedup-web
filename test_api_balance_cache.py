import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from api_balance_cache import ApiBalanceCache


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.
        self.http = Mock()
        self.path = Path(self.temp.name) / 'balance.json'
        self.cache = ApiBalanceCache(self.path, http=self.http, clock=lambda: self.now)

    def response(self, data, status=200):
        self.http.get.side_effect = None
        self.http.get.return_value = Mock(status_code=status, json=Mock(return_value=data))

    def test_timeout_without_previous_balance_is_unknown_not_zero(self):
        self.http.get.side_effect = TimeoutError()
        result = self.cache.get('synthetic')
        self.assertFalse(result['balance_known'])
        self.assertIsNone(result['total_usd'])
        self.assertIsNone(result['total_vnd'])

    def test_missing_api_key_is_unknown_not_zero(self):
        self.assertFalse(self.cache.get('')['balance_known'])
        self.http.get.assert_not_called()

    def test_malformed_success_body_cannot_be_saved_as_zero(self):
        self.response({'user_data': {}})
        self.assertFalse(self.cache.get('synthetic')['balance_known'])
        self.assertFalse(self.path.exists())

    def test_last_confirmed_balance_survives_failure_and_process_restart(self):
        self.response({'user_data': {'balance': 3.798, 'free_credit': .1}})
        valid = self.cache.get('synthetic')
        self.assertEqual(valid['total_usd'], 3.898)
        self.now += 20
        self.http.get.side_effect = TimeoutError()
        stale = self.cache.get('synthetic', force=True)
        self.assertTrue(stale['balance_stale'])
        self.assertEqual(stale['total_usd'], 3.898)
        restarted = ApiBalanceCache(self.path, http=self.http, clock=lambda: self.now)
        self.assertEqual(restarted.get('synthetic')['total_usd'], 3.898)

    def test_real_zero_is_valid_and_can_replace_previous_balance(self):
        self.response({'user_data': {'balance': 3}})
        self.cache.get('synthetic')
        self.now += 20
        self.response({'user_data': {'balance': 0, 'free_credit': 0}})
        zero = self.cache.get('synthetic', force=True)
        self.assertTrue(zero['balance_known'])
        self.assertEqual(zero['total_usd'], 0)
        self.assertFalse(zero['balance_stale'])

    def test_key_change_never_reuses_another_account_balance(self):
        self.response({'user_data': {'balance': 3}})
        self.cache.get('synthetic-old')
        self.http.get.side_effect = TimeoutError()
        self.assertFalse(self.cache.get('synthetic-new')['balance_known'])
        self.assertNotIn('synthetic-old', self.path.read_text())

    def test_failure_cooldown_and_recovery(self):
        self.http.get.side_effect = TimeoutError()
        self.cache.get('synthetic')
        self.cache.get('synthetic')
        self.assertEqual(self.http.get.call_count, 1)
        self.now += 4
        self.response({'user_data': {'balance': 5}})
        self.assertEqual(self.cache.get('synthetic')['total_usd'], 5)


if __name__ == '__main__':
    unittest.main()
