import unittest

from usage_counts import ensure_usage_counts, record_account_results, record_dedup_success


class UsageCountsTests(unittest.TestCase):
    def test_legacy_counts_preserved_with_estimate_marker(self):
        user = dict(scan_count=540, account_check_count=52, account_balance_vnd=24312)
        self.assertTrue(ensure_usage_counts(user))
        self.assertEqual(user['dedup_check_count'], 488)
        self.assertTrue(user['dedup_count_estimated'])
        self.assertFalse(ensure_usage_counts(user))
        self.assertEqual(user['account_balance_vnd'], 24312)

    def test_account_batch_never_increments_dedup(self):
        user = dict(scan_count=65, account_check_count=62)
        record_account_results(user, success_count=2, result_count=3)
        self.assertEqual(user['dedup_check_count'], 3)
        self.assertEqual(user['account_check_count'], 64)
        record_account_results(user, success_count=0, result_count=2)
        self.assertEqual(user['dedup_check_count'], 3)
        self.assertEqual(user['account_check_count'], 64)

    def test_dedup_never_increments_account_count(self):
        user = dict(scan_count=0, account_check_count=0)
        record_dedup_success(user)
        self.assertEqual(user['dedup_check_count'], 1)
        self.assertEqual(user['account_check_count'], 0)
        self.assertFalse(user['dedup_count_estimated'])
        record_account_results(user, 5, 5)
        record_dedup_success(user)
        self.assertEqual(user['dedup_check_count'], 2)
        self.assertEqual(user['account_check_count'], 5)

    def test_explicit_counts_win_over_legacy_total(self):
        user = dict(scan_count=999, account_check_count=5, dedup_check_count=3)
        record_account_results(user, 2, 2)
        self.assertEqual(user['dedup_check_count'], 3)


if __name__ == '__main__':
    unittest.main()
